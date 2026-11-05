//! Recording for the creator studio.
//!
//! While a creator plays, the recorder captures the chosen application and
//! saves a keyframe each time new text appears, using the same tracker
//! thresholds as playback. Each keyframe keeps the image and the text detected
//! in it, which the studio shows for editing.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use std::path::{Path, PathBuf};

use image::codecs::png::{CompressionType, FilterType, PngEncoder};
use image::{ImageFormat, RgbaImage};

use crate::services::{CaptureBackend, OcrBackend};
use crate::text::is_valid_text;
use crate::tracker::{Tracker, TrackerSettings, Update, normalize, text_similarity};
use crate::types::{Frame, Rect, Source};

/// Keyframes kept per recording; older ones are dropped first.
const MAX_KEYFRAMES: usize = 300;
const THUMBNAIL_WIDTH: u32 = 160;
const RECORD_INTERVAL: Duration = Duration::from_millis(200);
/// How long new content must stay still before it is read, and how long
/// content that keeps changing is waited for.
const SETTLE_TIME: Duration = Duration::from_millis(150);
const MAX_SETTLE_WAIT: Duration = Duration::from_millis(1000);
/// Start of the status shown while recording a window squeezed by tiling.
pub const SQUEEZED_STATUS: &str = "Recording a squeezed window";

/// Text detected in a keyframe, in the keyframe's image pixels.
#[derive(Clone, Debug, PartialEq)]
pub struct DetectedText {
    pub text: String,
    pub pixels: Rect,
    pub confidence: f32,
    /// Lines of text, as read; 0 when unknown.
    pub lines: u32,
}

pub struct Keyframe {
    pub id: u64,
    pub width: u32,
    pub height: u32,
    /// Where the captured area was on screen.
    pub bounds: Rect,
    /// The PNG-encoded capture, kept on disk so long recordings don't fill
    /// memory.
    image: StoredImage,
    /// Small RGBA preview for the timeline.
    pub thumbnail: (u32, u32, Vec<u8>),
    pub texts: Vec<DetectedText>,
    /// Whether the window had its full size. A window squeezed by tiling
    /// lays text out differently from what players see, so its keyframes
    /// give text to translate but no positions to lock onto.
    pub full_size: bool,
}

/// Where a keyframe's PNG is: in a cache file, or in memory if the file
/// couldn't be written.
enum StoredImage {
    Disk(PathBuf),
    Memory(Vec<u8>),
}

impl Drop for StoredImage {
    fn drop(&mut self) {
        if let Self::Disk(path) = self {
            let _ = std::fs::remove_file(path);
        }
    }
}

/// This process's folder for keyframe images. Folders left by earlier runs
/// (e.g. after a crash) are removed the first time it is used.
fn keyframe_dir() -> Option<PathBuf> {
    static DIR: std::sync::OnceLock<Option<PathBuf>> = std::sync::OnceLock::new();
    DIR.get_or_init(|| {
        // Windows has no HOME; caches go in %LOCALAPPDATA%\Overlayr.
        let cache = if cfg!(windows) {
            std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .filter(|path| path.is_absolute())?
                .join("Overlayr")
        } else {
            std::env::var_os("XDG_CACHE_HOME")
                .map(PathBuf::from)
                .filter(|path| path.is_absolute())
                .or_else(|| {
                    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache"))
                })?
                .join("overlayr")
        };
        let root = cache.join("keyframes");
        let own = std::process::id().to_string();
        if let Ok(entries) = std::fs::read_dir(&root) {
            for entry in entries.flatten() {
                let name = entry.file_name();
                let alive = name
                    .to_str()
                    .is_some_and(|pid| pid == own || Path::new("/proc").join(pid).exists());
                if !alive {
                    let _ = std::fs::remove_dir_all(entry.path());
                }
            }
        }
        let dir = root.join(own);
        std::fs::create_dir_all(&dir).ok()?;
        Some(dir)
    })
    .clone()
}

impl Keyframe {
    fn new(id: u64, frame: &Frame, texts: Vec<DetectedText>, full_size: bool) -> Option<Self> {
        let image = RgbaImage::from_raw(frame.width, frame.height, frame.rgba.clone())?;
        let mut png = Vec::new();
        // Fast compression: a keyframe is saved every time text appears.
        image
            .write_with_encoder(PngEncoder::new_with_quality(
                &mut png,
                CompressionType::Fast,
                FilterType::Adaptive,
            ))
            .ok()?;
        let stored = keyframe_dir()
            .map(|dir| dir.join(format!("{id}.png")))
            .filter(|path| std::fs::write(path, &png).is_ok())
            .map_or(StoredImage::Memory(png), StoredImage::Disk);
        let height = (u64::from(frame.height) * u64::from(THUMBNAIL_WIDTH)
            / u64::from(frame.width.max(1)))
        .max(1) as u32;
        let thumbnail = image::imageops::thumbnail(&image, THUMBNAIL_WIDTH, height);
        Some(Self {
            id,
            width: frame.width,
            height: frame.height,
            bounds: frame.bounds,
            image: stored,
            thumbnail: (thumbnail.width(), thumbnail.height(), thumbnail.into_raw()),
            texts,
            full_size,
        })
    }

    /// Decodes the full capture.
    pub fn frame(&self) -> Option<Frame> {
        let png = match &self.image {
            StoredImage::Disk(path) => std::borrow::Cow::Owned(std::fs::read(path).ok()?),
            StoredImage::Memory(png) => std::borrow::Cow::Borrowed(png),
        };
        let image = image::load_from_memory_with_format(&png, ImageFormat::Png)
            .ok()?
            .to_rgba8();
        Some(Frame::new(
            self.id,
            self.bounds,
            image.width(),
            image.height(),
            image.into_raw(),
        ))
    }
}

#[derive(Default)]
struct RecorderState {
    new_keyframes: Vec<Keyframe>,
    /// The latest full-size capture, for the studio's live view.
    live: Option<Frame>,
    status: String,
    error: Option<String>,
}

/// Records keyframes on a background thread until stopped or dropped.
pub struct Recorder {
    state: Arc<Mutex<RecorderState>>,
    stop: Arc<AtomicBool>,
    capture_now: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

pub struct RecorderOptions {
    pub language: String,
    pub minimum_confidence: f32,
    pub tracker: TrackerSettings,
    /// Id for the first keyframe, so ids stay unique across recordings.
    pub first_id: u64,
    /// Texts already in earlier keyframes, which don't need another one.
    pub known_texts: Vec<String>,
}

impl Recorder {
    pub fn start<C, O>(source: Source, mut capture: C, mut ocr: O, options: RecorderOptions) -> Self
    where
        C: CaptureBackend + 'static,
        O: OcrBackend + 'static,
    {
        let state = Arc::new(Mutex::new(RecorderState {
            status: "Recording".into(),
            ..RecorderState::default()
        }));
        let stop = Arc::new(AtomicBool::new(false));
        let capture_now = Arc::new(AtomicBool::new(false));
        let handle = {
            let (state, stop, capture_now) = (
                Arc::clone(&state),
                Arc::clone(&stop),
                Arc::clone(&capture_now),
            );
            thread::spawn(move || {
                crate::services::lower_thread_priority();
                record(
                    &source,
                    &mut capture,
                    &mut ocr,
                    &options,
                    &state,
                    &stop,
                    &capture_now,
                );
            })
        };
        Self {
            state,
            stop,
            capture_now,
            handle: Some(handle),
        }
    }

    /// Keyframes recorded since the last call.
    pub fn take_keyframes(&self) -> Vec<Keyframe> {
        self.state
            .lock()
            .map(|mut state| std::mem::take(&mut state.new_keyframes))
            .unwrap_or_default()
    }

    /// The newest capture since the last call, for a live view.
    pub fn take_live_frame(&self) -> Option<Frame> {
        self.state
            .lock()
            .ok()
            .and_then(|mut state| state.live.take())
    }

    /// Saves the next captured frame as a keyframe, whatever changed.
    pub fn capture_now(&self) {
        self.capture_now.store(true, Ordering::Release);
    }

    pub fn status(&self) -> (String, Option<String>) {
        self.state
            .lock()
            .map(|state| (state.status.clone(), state.error.clone()))
            .unwrap_or_else(|_| ("Stopped".into(), None))
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

impl Drop for Recorder {
    fn drop(&mut self) {
        self.stop();
    }
}

fn record<C: CaptureBackend, O: OcrBackend>(
    source: &Source,
    capture: &mut C,
    ocr: &mut O,
    options: &RecorderOptions,
    state: &Mutex<RecorderState>,
    stop: &AtomicBool,
    capture_now: &AtomicBool,
) {
    let mut tracker = Tracker::new(options.tracker);
    let mut next_id = options.first_id;
    // Every text recorded so far. Comparing with all of them, not just the
    // last keyframe, stops text that flickers in and out of OCR, or a row
    // that changes while hovered, from saving the same screen again.
    let mut seen = options
        .known_texts
        .iter()
        .map(|text| normalize(text))
        .collect::<Vec<_>>();
    let set_status = |status: &str, error: Option<String>| {
        if let Ok(mut state) = state.lock() {
            state.status = status.into();
            state.error = error;
        }
    };
    let mut rest = Duration::ZERO;
    let mut pending_since: Option<std::time::Instant> = None;
    while !stop.load(Ordering::Acquire) {
        thread::sleep(RECORD_INTERVAL + std::mem::take(&mut rest));
        // Hyprland windows are captured from their own buffer, so recording
        // goes on while the creator plays on another workspace.
        let visible = capture.source_is_active(source).unwrap_or(true);
        if !visible && !capture.can_capture_hidden(source) {
            set_status("Paused: the window isn't visible", None);
            continue;
        }
        // A window squeezed by tiling (e.g. next to another app) shows a
        // different layout from the one players see. Its text is still worth
        // translating, so recording goes on rather than missing it.
        let full_size = capture.source_is_full_size(source).unwrap_or(true);
        let recording = if !full_size {
            "Recording a squeezed window: make the game full screen (or give it its own \
             workspace) for the layout players see"
        } else if visible {
            "Recording"
        } else {
            "Recording in the background"
        };
        let frame = match capture.capture(source) {
            Ok(frame) => frame,
            Err(error) => {
                set_status("Capture error", Some(error));
                continue;
            }
        };
        if let Ok(mut state) = state.lock() {
            state.live = Some(frame.clone());
        }
        let manual = capture_now.swap(false, Ordering::AcqRel);
        if manual {
            tracker.request_detection();
        }
        if tracker.update(&frame) != Update::Detect {
            pending_since = None;
            set_status(recording, None);
            continue;
        }
        // Wait for new content to settle, so keyframes don't catch a menu
        // halfway through fading in.
        let waited = *pending_since.get_or_insert_with(std::time::Instant::now);
        if !manual && tracker.settled_for() < SETTLE_TIME && waited.elapsed() < MAX_SETTLE_WAIT {
            continue;
        }
        pending_since = None;
        // Read only the area that changed, as playback does: OCR of the whole
        // screen each time something moved kept the CPU busy.
        let ocr_started = std::time::Instant::now();
        let recognized = match tracker.detection_region() {
            Some(region) => ocr.recognize(&frame.crop(region), &options.language),
            None => ocr.recognize(&frame, &options.language),
        };
        // Rest about as long as the read took, so a game that animates all
        // the time doesn't keep OCR running back to back beside it.
        rest = ocr_started.elapsed().min(Duration::from_secs(1));
        let blocks = match recognized {
            Ok(blocks) => blocks
                .into_iter()
                .filter(|block| block.confidence >= options.minimum_confidence)
                .filter(|block| is_valid_text(&block.source_text, &options.language))
                .collect::<Vec<_>>(),
            Err(error) => {
                tracker.request_detection();
                set_status("OCR error", Some(error));
                continue;
            }
        };
        // Keep frames that show text no keyframe has yet, or that the
        // creator asked for. Detections caused by animation re-read the same
        // text and are skipped.
        let threshold = options.tracker.match_threshold;
        let has_new_text = blocks
            .iter()
            .any(|block| !is_known(&seen, &normalize(&block.source_text), threshold));
        let _ = tracker.reconcile_region(&frame, blocks, |texts| Ok(vec![None; texts.len()]));
        // The keyframe lists all text on screen: what was just read plus
        // what the tracker still follows outside the changed area.
        let texts = tracker
            .blocks(&frame)
            .into_iter()
            .map(|block| DetectedText {
                pixels: frame.to_pixels(block.bounds),
                confidence: block.confidence,
                lines: block.lines,
                text: block.source_text,
            })
            .collect::<Vec<_>>();
        if (has_new_text || manual)
            && let Some(keyframe) = Keyframe::new(next_id, &frame, texts, full_size)
        {
            for detected in &keyframe.texts {
                let text = normalize(&detected.text);
                if !is_known(&seen, &text, threshold) {
                    seen.push(text);
                }
            }
            next_id += 1;
            if let Ok(mut state) = state.lock() {
                state.new_keyframes.push(keyframe);
                let excess = state.new_keyframes.len().saturating_sub(MAX_KEYFRAMES);
                state.new_keyframes.drain(..excess);
            }
        }
        set_status(recording, None);
    }
    set_status("Stopped", None);
}

/// Whether `text` (normalised) matches one already recorded.
fn is_known(seen: &[String], text: &str, threshold: f32) -> bool {
    seen.iter()
        .any(|known| text_similarity(text, known) >= threshold)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::ServiceResult;
    use crate::types::{SourceKind, TextBlock};

    struct StaticCapture;

    impl CaptureBackend for StaticCapture {
        fn sources(&mut self) -> ServiceResult<Vec<Source>> {
            Ok(Vec::new())
        }

        fn capture(&mut self, _source: &Source) -> ServiceResult<Frame> {
            let mut rgba = vec![30; 64 * 32 * 4];
            for pixel in rgba.chunks_mut(4).step_by(3) {
                pixel.copy_from_slice(&[240, 240, 240, 255]);
            }
            Ok(Frame::new(1, Rect::new(0, 0, 64, 32), 64, 32, rgba))
        }
    }

    struct OneText;

    impl OcrBackend for OneText {
        fn recognize(&mut self, _frame: &Frame, _language: &str) -> ServiceResult<Vec<TextBlock>> {
            Ok(vec![TextBlock::new(
                Rect::new(4, 4, 40, 12),
                "Quest log",
                0.9,
            )])
        }
    }

    /// A screen that changes every capture, as a hover highlight or an
    /// animation does.
    struct FlickeringCapture(u8);

    impl CaptureBackend for FlickeringCapture {
        fn sources(&mut self) -> ServiceResult<Vec<Source>> {
            Ok(Vec::new())
        }

        fn capture(&mut self, _source: &Source) -> ServiceResult<Frame> {
            self.0 = self.0.wrapping_add(1);
            // Text-like strokes that shift every capture.
            let spacing = if self.0.is_multiple_of(2) { 3 } else { 5 };
            let mut rgba = vec![30; 64 * 32 * 4];
            for pixel in rgba.chunks_mut(4).step_by(spacing) {
                pixel.copy_from_slice(&[240, 240, 240, 255]);
            }
            Ok(Frame::new(1, Rect::new(0, 0, 64, 32), 64, 32, rgba))
        }
    }

    /// OCR that misses one of two texts every other read.
    struct FlickeringOcr(Arc<std::sync::atomic::AtomicUsize>);

    impl OcrBackend for FlickeringOcr {
        fn recognize(&mut self, _frame: &Frame, _language: &str) -> ServiceResult<Vec<TextBlock>> {
            let reads = self.0.fetch_add(1, Ordering::Relaxed);
            let mut blocks = vec![TextBlock::new(Rect::new(4, 4, 40, 12), "Quest log", 0.9)];
            if reads.is_multiple_of(2) {
                blocks.push(TextBlock::new(Rect::new(4, 18, 40, 12), "Rewards", 0.9));
            }
            Ok(blocks)
        }
    }

    #[test]
    fn flickering_readings_do_not_repeat_keyframes() {
        let source = Source {
            id: "test".into(),
            name: "Test".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 64, 32),
            detail: String::new(),
        };
        let reads = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let mut recorder = Recorder::start(
            source,
            FlickeringCapture(0),
            FlickeringOcr(Arc::clone(&reads)),
            RecorderOptions {
                language: "en".into(),
                minimum_confidence: 0.3,
                tracker: TrackerSettings::default(),
                first_id: 1,
                known_texts: vec!["Quest log".into()],
            },
        );
        // A screen that never settles is read at most once a second.
        thread::sleep(Duration::from_millis(4000));
        recorder.stop();
        let keyframes = recorder.take_keyframes();
        assert!(reads.load(Ordering::Relaxed) >= 3, "the screen was re-read");
        assert_eq!(keyframes.len(), 1, "only the first sight of \"Rewards\"");
        assert_eq!(keyframes[0].texts.len(), 2);
    }

    #[test]
    fn keyframe_images_live_on_disk_until_dropped() {
        let frame = StaticCapture.capture(&test_source()).unwrap();
        let keyframe = Keyframe::new(u64::MAX, &frame, Vec::new(), true).unwrap();
        let StoredImage::Disk(path) = &keyframe.image else {
            panic!("expected the image in the cache folder");
        };
        let path = path.clone();
        assert!(path.exists());
        assert_eq!(keyframe.frame().unwrap().rgba, frame.rgba);
        drop(keyframe);
        assert!(!path.exists(), "the file goes with the keyframe");
    }

    fn test_source() -> Source {
        Source {
            id: "test".into(),
            name: "Test".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 64, 32),
            detail: String::new(),
        }
    }

    /// Keyframes the recorder made within `deadline`; waits for the first,
    /// then a moment more for any extra ones. Slow CI machines need longer
    /// than a fixed sleep allows.
    fn keyframes_within(recorder: &Recorder, deadline: Duration) -> Vec<Keyframe> {
        let started = std::time::Instant::now();
        let mut keyframes = Vec::new();
        while keyframes.is_empty() && started.elapsed() < deadline {
            thread::sleep(Duration::from_millis(50));
            keyframes = recorder.take_keyframes();
        }
        thread::sleep(Duration::from_millis(300));
        keyframes.extend(recorder.take_keyframes());
        keyframes
    }

    #[test]
    fn records_one_keyframe_per_new_text_and_on_request() {
        let source = Source {
            id: "test".into(),
            name: "Test".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 64, 32),
            detail: String::new(),
        };
        let mut recorder = Recorder::start(
            source,
            StaticCapture,
            OneText,
            RecorderOptions {
                language: "en".into(),
                minimum_confidence: 0.3,
                tracker: TrackerSettings::default(),
                first_id: 1,
                known_texts: Vec::new(),
            },
        );
        let first = keyframes_within(&recorder, Duration::from_secs(10));
        assert_eq!(first.len(), 1, "a static screen yields one keyframe");
        assert_eq!(first[0].texts[0].text, "Quest log");
        assert!(first[0].frame().is_some());

        recorder.capture_now();
        let manual = keyframes_within(&recorder, Duration::from_secs(10));
        assert_eq!(manual.len(), 1);
        assert_eq!(manual[0].id, 2);
        recorder.stop();
    }
}
