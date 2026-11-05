use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::config::AppConfig;
use crate::pack::Pack;
use crate::pipeline::LatestFrame;
use crate::services::{CaptureBackend, OcrBackend, ServiceResult, TranslationBackend};
use crate::text::{is_valid_text, trim_icon_misreads};
use crate::tracker::{Tracker, TrackerSettings, Translated, Update, normalize, text_similarity};
use crate::types::{Frame, LanguagePair, Rect, Source, TextBlock};

#[derive(Clone, Debug, Default)]
pub struct PipelineSnapshot {
    pub running: bool,
    pub status: String,
    pub frames_processed: u64,
    pub frames_per_second: f32,
    /// How many times OCR ran; the tracker skips it for known text.
    pub detections: u64,
    pub capture_bounds: Option<Rect>,
    pub source_active: bool,
    pub last_error: Option<String>,
    pub blocks: Vec<TextBlock>,
    /// What the pipeline is doing, when debug mode is on.
    pub debug: Option<Arc<DebugInfo>>,
}

/// What happened to a piece of text the OCR found.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Verdict {
    Translated,
    /// Kept, but with no translation (text a pack doesn't cover).
    NotTranslated,
    LowConfidence,
    InvalidText,
    /// Our own translation, read back from a screen capture.
    OverlayEcho,
    /// Inside one of Overlayr's own windows.
    OwnWindow,
    /// Discarded by OCR as icons, bars or fragments.
    Noise,
    /// Hidden by the player.
    Hidden,
}

impl Verdict {
    pub fn label(self) -> &'static str {
        match self {
            Self::Translated => "Translated",
            Self::NotTranslated => "Not translated",
            Self::LowConfidence => "Low confidence",
            Self::InvalidText => "Invalid text",
            Self::OverlayEcho => "Overlay echo",
            Self::OwnWindow => "Overlayr window",
            Self::Noise => "Noise",
            Self::Hidden => "Hidden by you",
        }
    }
}

#[derive(Clone, Debug)]
pub struct DebugText {
    pub text: String,
    pub bounds: Rect,
    pub confidence: f32,
    pub verdict: Verdict,
    /// The translation, or why the text was dropped.
    pub note: String,
}

/// A view into the pipeline for debug mode.
#[derive(Clone, Debug, Default)]
pub struct DebugInfo {
    /// The frame OCR last read, and what it found in it.
    pub frame: Option<Arc<Frame>>,
    pub texts: Vec<DebugText>,
    /// The tracker's decision for the latest frame.
    pub decision: &'static str,
    pub tracked: usize,
    pub capture_ms: f32,
    pub track_ms: f32,
    pub ocr_ms: f32,
    pub translate_ms: f32,
    /// Recent events, oldest first.
    pub log: Vec<String>,
}

const DEBUG_LOG_LENGTH: usize = 200;
/// How often a hidden window is checked for coming back.
const HIDDEN_POLL: Duration = Duration::from_millis(250);
/// Texts whose pack match is cached during playback.
const MAX_PACK_LOOKUPS: usize = 4_096;

struct SharedState {
    snapshot: Mutex<PipelineSnapshot>,
    stop: AtomicBool,
    /// A replacement pack waiting to be picked up by the pipeline.
    pack_update: Mutex<Option<Pack>>,
    /// Screen areas whose text is never translated: Overlayr's own windows,
    /// which appear in captures of a whole screen.
    excluded: Mutex<Vec<Rect>>,
    /// Texts the player hid, normalized; `None` once the pipeline took an
    /// update.
    hidden_update: Mutex<Option<std::collections::HashSet<String>>>,
    /// Texts whose translation the player wants redone.
    retranslate: Mutex<Vec<String>>,
    /// Duration of the latest capture, in microseconds, for debug mode.
    capture_micros: std::sync::atomic::AtomicU64,
    /// Translations are on screen, so capture checks more often whether
    /// their text is gone.
    showing_text: AtomicBool,
    /// Wakes the UI when the translations change, so they are redrawn (or
    /// cleared) at once instead of at its next scheduled repaint.
    waker: Mutex<Option<Box<dyn Fn() + Send>>>,
}

/// Grows a read region (in frame pixels) to a 32-pixel grid. OCR then
/// samples a crop exactly like the whole frame, so its unchanged lines are
/// found in the same place and their earlier readings are reused.
fn align_region(region: Rect) -> Rect {
    const GRID: i32 = 32;
    let left = region.left.max(0) / GRID * GRID;
    let top = region.top.max(0) / GRID * GRID;
    let right = (region.right().max(0) + GRID - 1) / GRID * GRID;
    let bottom = (region.bottom().max(0) + GRID - 1) / GRID * GRID;
    // `Frame::crop` clips anything past the frame's edge.
    Rect::new(left, top, (right - left) as u32, (bottom - top) as u32)
}

/// Spawns a thread with a name, which debug mode's Resources tab shows.
fn spawn_named<T: Send + 'static>(
    name: &str,
    work: impl FnOnce() -> T + Send + 'static,
) -> thread::JoinHandle<T> {
    thread::Builder::new()
        .name(name.into())
        .spawn(work)
        .expect("failed to spawn thread")
}

/// How long new content must stay still before it is read.
const SETTLE_TIME: Duration = Duration::from_millis(90);
/// Content that keeps changing (animation) is read after this long anyway.
const MAX_SETTLE_WAIT: Duration = Duration::from_millis(700);

pub struct PipelineWorker {
    state: Arc<SharedState>,
    frames: Arc<LatestFrame<Frame>>,
    capture_handle: Option<JoinHandle<()>>,
    pipeline_handle: Option<JoinHandle<()>>,
}

impl PipelineWorker {
    pub fn start<C, O, T>(
        config: AppConfig,
        source: Source,
        capture: C,
        ocr: O,
        translator: T,
    ) -> Self
    where
        C: CaptureBackend + 'static,
        O: OcrBackend + 'static,
        T: TranslationBackend + 'static,
    {
        Self::start_with_pack(config, source, capture, ocr, translator, None, false)
    }

    /// Like [`PipelineWorker::start`], but with a translation pack: text is
    /// translated by the pack's entries, in their style. Text the pack
    /// doesn't cover is translated by `translator` when `fill_pack_gaps` is
    /// set, and otherwise left in the original language.
    pub fn start_with_pack<C, O, T>(
        config: AppConfig,
        source: Source,
        mut capture: C,
        mut ocr: O,
        translator: T,
        pack: Option<Pack>,
        fill_pack_gaps: bool,
    ) -> Self
    where
        C: CaptureBackend + 'static,
        O: OcrBackend + 'static,
        T: TranslationBackend + 'static,
    {
        let state = Arc::new(SharedState {
            snapshot: Mutex::new(PipelineSnapshot {
                running: true,
                source_active: true,
                status: "Starting pipeline".into(),
                ..PipelineSnapshot::default()
            }),
            stop: AtomicBool::new(false),
            pack_update: Mutex::new(None),
            excluded: Mutex::new(Vec::new()),
            hidden_update: Mutex::new(None),
            retranslate: Mutex::new(Vec::new()),
            capture_micros: std::sync::atomic::AtomicU64::new(0),
            showing_text: AtomicBool::new(false),
            waker: Mutex::new(None),
        });
        let frames = Arc::new(LatestFrame::new());
        let capture_frames = Arc::clone(&frames);
        let capture_state = Arc::clone(&state);
        let capture_config = config.clone();
        let capture_handle = spawn_named("capture", move || {
            crate::services::lower_thread_priority();
            run_capture(
                capture_config,
                source,
                &mut capture,
                capture_frames,
                capture_state,
            );
        });
        let pipeline_frames = Arc::clone(&frames);
        let pipeline_state = Arc::clone(&state);
        let pipeline_handle = spawn_named("pipeline", move || {
            crate::services::lower_thread_priority();
            run_pipeline(
                config,
                &mut ocr,
                translator,
                pack,
                fill_pack_gaps,
                pipeline_frames,
                pipeline_state,
            );
        });
        Self {
            state,
            frames,
            capture_handle: Some(capture_handle),
            pipeline_handle: Some(pipeline_handle),
        }
    }

    /// Sets screen areas to ignore, such as Overlayr's own windows.
    pub fn set_excluded(&self, areas: Vec<Rect>) {
        if let Ok(mut excluded) = self.state.excluded.lock() {
            *excluded = areas;
        }
    }

    /// Hides texts (as [`normalize`] gives them) from now on; text already
    /// on screen is read again.
    pub fn set_hidden(&self, texts: &[String]) {
        if let Ok(mut update) = self.state.hidden_update.lock() {
            *update = Some(texts.iter().map(|text| normalize(text)).collect());
        }
    }

    /// Forgets the machine translation of `text`, saved ones included, and
    /// translates it again.
    pub fn retranslate(&self, text: String) {
        if let Ok(mut texts) = self.state.retranslate.lock() {
            texts.push(text);
        }
    }

    /// Replaces the running pack, e.g. while a creator edits it in the
    /// studio. Text on screen is re-read and re-translated with the new pack.
    pub fn set_pack(&self, pack: Pack) {
        if let Ok(mut update) = self.state.pack_update.lock() {
            *update = Some(pack);
        }
    }

    /// Sets what wakes the UI when the translations change, unless set.
    pub fn set_waker(&self, wake: impl Fn() + Send + 'static) {
        if let Ok(mut waker) = self.state.waker.lock()
            && waker.is_none()
        {
            *waker = Some(Box::new(wake));
        }
    }

    pub fn snapshot(&self) -> PipelineSnapshot {
        self.state
            .snapshot
            .lock()
            .map(|snapshot| snapshot.clone())
            .unwrap_or_default()
    }

    pub fn stop(&mut self) {
        self.state.stop.store(true, Ordering::Release);
        self.frames.close();
        self.join_handles();
    }

    /// Signal shutdown without blocking the caller on an in-flight capture or
    /// HTTP request. The handles are reaped by a short-lived background thread
    /// so the desktop UI remains responsive during provider timeouts.
    pub fn stop_async(&mut self) -> JoinHandle<()> {
        self.state.stop.store(true, Ordering::Release);
        self.frames.close();
        let capture_handle = self.capture_handle.take();
        let pipeline_handle = self.pipeline_handle.take();
        thread::spawn(move || {
            if let Some(handle) = capture_handle {
                let _ = handle.join();
            }
            if let Some(handle) = pipeline_handle {
                let _ = handle.join();
            }
        })
    }

    fn join_handles(&mut self) {
        if let Some(handle) = self.capture_handle.take() {
            let _ = handle.join();
        }
        if let Some(handle) = self.pipeline_handle.take() {
            let _ = handle.join();
        }
    }
}

impl Drop for PipelineWorker {
    fn drop(&mut self) {
        self.stop();
    }
}

fn run_capture<C>(
    config: AppConfig,
    source: Source,
    capture: &mut C,
    frames: Arc<LatestFrame<Frame>>,
    state: Arc<SharedState>,
) where
    C: CaptureBackend,
{
    let configured = Duration::from_millis(config.capture_interval_ms.max(16));
    // Capture interval while translations are shown: text that vanishes is
    // cleared within about this long.
    let showing_text = config
        .performance
        .fast_clear
        .then(|| Duration::from_millis(config.performance.fast_clear_interval_ms.max(16)));
    while !state.stop.load(Ordering::Acquire) {
        let mut interval = configured;
        if let Ok(active) = capture.source_is_active(&source) {
            // Check often for vanished text only while it is on screen.
            if let Some(showing_text) = showing_text
                && active
                && state.showing_text.load(Ordering::Relaxed)
            {
                interval = configured.min(showing_text);
            }
            if let Ok(mut snapshot) = state.snapshot.lock() {
                snapshot.source_active = active;
            }
            // A window on no monitor (another workspace, minimised) shows
            // no translations, so nothing is captured, read or translated
            // until it's back. Its text is still tracked then, so its
            // translations return at once.
            if !active {
                thread::sleep(interval.max(HIDDEN_POLL));
                continue;
            }
        }
        let started = Instant::now();
        match capture.capture(&source) {
            Ok(frame) => {
                state
                    .capture_micros
                    .store(started.elapsed().as_micros() as u64, Ordering::Relaxed);
                frames.submit(frame);
            }
            Err(error) => {
                if let Ok(mut snapshot) = state.snapshot.lock() {
                    snapshot.status = "Capture error".into();
                    snapshot.last_error = Some(error);
                }
                thread::sleep(Duration::from_millis(250));
            }
        }
        thread::sleep(capture_rest(started.elapsed(), interval));
    }
}

/// How long to wait before the next capture. Each capture makes the
/// compositor copy the game's frame, so rest at least as long as the capture
/// took: back-to-back captures (a short interval with a slow capture, such
/// as 30 ms for a 1440p window) made games drop frames.
fn capture_rest(capture_took: Duration, interval: Duration) -> Duration {
    interval.saturating_sub(capture_took).max(capture_took)
}

fn run_pipeline<O, T>(
    config: AppConfig,
    ocr: &mut O,
    translator: T,
    mut pack: Option<Pack>,
    fill_pack_gaps: bool,
    frames: Arc<LatestFrame<Frame>>,
    state: Arc<SharedState>,
) where
    O: OcrBackend,
    T: TranslationBackend + 'static,
{
    let source_lang = pack
        .as_ref()
        .map(|pack| pack.source_lang.clone())
        .filter(|language| !language.is_empty())
        .unwrap_or_else(|| config.source_lang.clone());
    let pair = LanguagePair::new(source_lang.clone(), config.target_lang.clone());
    // Machine translation runs in the background: text is tracked as soon
    // as it is read, and each translation is drawn when it arrives, so one
    // slow paragraph doesn't hold back the rest of the screen.
    let mut background = BackgroundTranslator::start(translator, pair.clone());
    let mut cache = crate::pipeline::TranslationCache::new(512);
    let tracker_settings = TrackerSettings {
        match_threshold: config.match_threshold,
        follow_threshold: config.follow_threshold,
        detect_threshold: config.detect_threshold,
        follow_highlights: config.performance.follow_highlights,
        ..TrackerSettings::default()
    };
    let mut tracker = Tracker::new(tracker_settings);
    if let Some(pack) = &pack {
        tracker.set_anchors(pack.anchor_targets());
    }
    let mut pack_lookups: std::collections::HashMap<String, Option<Translated>> =
        std::collections::HashMap::new();
    // Colours and backdrop of each text on screen, with a fingerprint of
    // the pixels they came from.
    type Look = (
        u64,
        [[u8; 4]; 4],
        Option<[u8; 4]>,
        Option<Arc<crate::types::Backdrop>>,
    );
    let mut backdrops: std::collections::HashMap<Rect, Look> = std::collections::HashMap::new();
    let mut hidden: std::collections::HashSet<String> = config
        .hidden_texts
        .iter()
        .map(|text| normalize(text))
        .collect();
    let started = Instant::now();
    let mut debug = config.debug.then(DebugInfo::default);
    let log = |debug: &mut Option<DebugInfo>, message: String| {
        if let Some(debug) = debug {
            debug.log.push(format!(
                "{:>7.1}s  {message}",
                started.elapsed().as_secs_f32()
            ));
            let excess = debug.log.len().saturating_sub(DEBUG_LOG_LENGTH);
            debug.log.drain(..excess);
        }
    };
    log(
        &mut debug,
        match &pack {
            Some(pack) => format!(
                "Started with the pack \"{}\" ({} entries){}",
                pack.name,
                pack.entries.len(),
                if fill_pack_gaps {
                    "; AI translates what it doesn't cover"
                } else {
                    ""
                }
            ),
            None => "Started with machine translation".into(),
        },
    );
    // Publish right away, so debug mode shows something even before the
    // first frame (e.g. while the window isn't visible).
    if let (Some(info), Ok(mut snapshot)) = (&debug, state.snapshot.lock()) {
        snapshot.debug = Some(Arc::new(info.clone()));
    }
    let mut frames_checked = 0_u64;
    let mut detections = 0_u64;
    let mut window_frames = 0_u32;
    let mut window_start = Instant::now();
    let mut rest_until: Option<Instant> = None;
    // When the screen first needed reading since the last read started.
    let mut pending_since: Option<Instant> = None;
    let mut limit_logged = false;
    // OCR reads on its own thread, so tracking (and clearing text that
    // vanished) never waits for it. One read runs at a time.
    let (jobs, read_requests) = std::sync::mpsc::channel::<Frame>();
    let (read_results, results) = std::sync::mpsc::channel::<ReadOutcome>();
    let mut job: Option<ReadJob> = None;
    let read_language = source_lang.clone();
    thread::scope(|scope| {
        let reader = thread::Builder::new().name("ocr".into());
        let _reader = reader.spawn_scoped(scope, move || {
            while let Ok(area) = read_requests.recv() {
                let started = Instant::now();
                let result = ocr.recognize(&area, &read_language);
                let outcome = ReadOutcome {
                    result,
                    rejected: ocr.take_rejected(),
                    took: started.elapsed(),
                };
                if read_results.send(outcome).is_err() {
                    return;
                }
            }
        });
        while !state.stop.load(Ordering::Acquire) {
            let Some(mut frame) = frames.recv() else {
                break;
            };
            // Blank out Overlayr's own windows (in whole-screen captures) before
            // anything looks at the frame: their constantly updating interface
            // would otherwise trigger re-reads and cost OCR time.
            if let Ok(excluded) = state.excluded.lock() {
                for area in excluded.iter() {
                    frame.fill(frame.to_pixels(*area), [0, 0, 0, 255]);
                }
            }
            frames_checked += 1;
            window_frames += 1;
            if let Some(updated) = state
                .pack_update
                .lock()
                .ok()
                .and_then(|mut update| update.take())
            {
                // Start over so no translation from the old pack is reused.
                log(
                    &mut debug,
                    format!(
                        "Pack updated ({} entries); reading the screen again",
                        updated.entries.len()
                    ),
                );
                tracker = Tracker::new(tracker_settings);
                tracker.set_anchors(updated.anchor_targets());
                pack = Some(updated);
                pack_lookups.clear();
            }
            if let Some(updated) = state
                .hidden_update
                .lock()
                .ok()
                .and_then(|mut update| update.take())
            {
                // Start over so hidden text on screen disappears.
                log(
                    &mut debug,
                    format!("{} hidden text(s); reading the screen again", updated.len()),
                );
                tracker = Tracker::new(tracker_settings);
                if let Some(pack) = &pack {
                    tracker.set_anchors(pack.anchor_targets());
                }
                hidden = updated;
            }
            let retranslate = state
                .retranslate
                .lock()
                .map(|mut texts| std::mem::take(&mut *texts))
                .unwrap_or_default();
            if !retranslate.is_empty() {
                // Starting over drops the tracker's copy too; the texts are
                // then read and, missing from every cache, translated again.
                log(
                    &mut debug,
                    format!("Translating {} text(s) again", retranslate.len()),
                );
                for text in &retranslate {
                    cache.remove(&pair, text);
                }
                background.forget(retranslate);
                tracker = Tracker::new(tracker_settings);
                if let Some(pack) = &pack {
                    tracker.set_anchors(pack.anchor_targets());
                }
            }
            if !config.translate_only_when_changed {
                tracker.request_detection();
            }

            // A read that finished in the background since the last frame.
            let finished = job.as_ref().and_then(|_| results.try_recv().ok());
            let track_started = Instant::now();
            let update = tracker.update(&frame);
            let track_ms = track_started.elapsed().as_secs_f32() * 1000.0;
            // Tracking runs on every frame, also while text is being read, so
            // text that vanished (a closed menu) is cleared at once.
            let mut result: ServiceResult<bool> = Ok(update != Update::Unchanged);
            if update == Update::Moved {
                log(
                    &mut debug,
                    format!(
                        "Followed moving text ({} tracked)",
                        tracker.blocks(&frame).len()
                    ),
                );
            }
            let read_now = finished.is_some();
            if let Some(outcome) = finished {
                let job = job.take().expect("a read was running");
                // Rest about as long as the read took before the next one (half
                // as long when set to react fastest). Games with constant
                // animation otherwise keep OCR running back to back, taking
                // several cores from the game and making it stutter.
                let rest = if config.performance.rest_between_reads {
                    outcome.took
                } else {
                    outcome.took / 2
                };
                rest_until = Some(Instant::now() + rest.min(Duration::from_secs(1)));
                let region = job.detection.region;
                let ocr_ms = outcome.took.as_secs_f32() * 1000.0;
                result = (|| {
                    let recognized = outcome.result?;
                    let excluded = state
                        .excluded
                        .lock()
                        .map(|excluded| excluded.clone())
                        .unwrap_or_default();
                    // Each filter has a verdict, so debug mode can show why text
                    // was dropped.
                    let mut dropped = Vec::new();
                    let mut blocks = Vec::new();
                    for mut block in recognized {
                        block.source_text = trim_icon_misreads(&block.source_text, &source_lang);
                        let verdict = if block.confidence < config.minimum_confidence {
                            Some(Verdict::LowConfidence)
                        } else if !is_valid_text(&block.source_text, &source_lang) {
                            Some(Verdict::InvalidText)
                        } else if hidden.contains(&normalize(&block.source_text)) {
                            Some(Verdict::Hidden)
                        } else if is_overlay_echo(&block, &job.rendered, &pair) {
                            Some(Verdict::OverlayEcho)
                        } else if excluded
                            .iter()
                            .any(|area| mostly_inside(block.bounds, *area))
                        {
                            Some(Verdict::OwnWindow)
                        } else {
                            None
                        };
                        match verdict {
                            Some(verdict) => {
                                if std::env::var_os("OVERLAYR_TRACE").is_some() {
                                    eprintln!(
                                        "trace: dropped {:?} at {:?}: {}",
                                        block.source_text.chars().take(32).collect::<String>(),
                                        block.bounds,
                                        verdict.label()
                                    );
                                }
                                dropped.push((block, verdict))
                            }
                            None => blocks.push(block),
                        }
                    }
                    let found = blocks.len() + dropped.len();
                    let translate_started = Instant::now();
                    let mut requests = 0;
                    tracker.reconcile_detection(&job.frame, blocks, job.detection, |texts| {
                        requests += texts.len();
                        match &pack {
                            // Matching against every entry is cached per text,
                            // including text the pack doesn't cover.
                            Some(pack) => {
                                let mut results = texts
                                    .iter()
                                    .map(|text| {
                                        // OCR misreads and changing numbers make new
                                        // texts all the time; don't keep them forever.
                                        if pack_lookups.len() >= MAX_PACK_LOOKUPS {
                                            pack_lookups.clear();
                                        }
                                        pack_lookups
                                            .entry(normalize(text))
                                            .or_insert_with(|| {
                                                pack.find(text)
                                                    .filter(|entry| {
                                                        !entry.translation.trim().is_empty()
                                                    })
                                                    .map(|entry| Translated {
                                                        text: entry.translation.clone(),
                                                        style: Some(entry.style.clone()),
                                                    })
                                            })
                                            .clone()
                                    })
                                    .collect::<Vec<_>>();
                                // Text the pack doesn't cover goes to the AI.
                                // Pack translations show at once; the AI's
                                // are drawn when they arrive.
                                if fill_pack_gaps {
                                    let mut missing = Vec::new();
                                    for (text, result) in texts.iter().zip(&mut results) {
                                        if result.is_none() {
                                            match cache.get(&pair, text) {
                                                Some(cached) => {
                                                    *result = Some(Translated::plain(cached));
                                                }
                                                None => missing.push(text.clone()),
                                            }
                                        }
                                    }
                                    if !missing.is_empty() {
                                        background.request(missing);
                                    }
                                }
                                Ok(results)
                            }
                            // Texts translated before show at once; the rest are
                            // translated in the background and each is drawn as
                            // soon as it arrives, so short labels don't wait for
                            // a long paragraph.
                            None => {
                                let results = texts
                                    .iter()
                                    .map(|text| cache.get(&pair, text).map(Translated::plain))
                                    .collect::<Vec<_>>();
                                background.request(
                                    texts
                                        .iter()
                                        .zip(&results)
                                        .filter(|(_, cached)| cached.is_none())
                                        .map(|(text, _)| text.clone()),
                                );
                                Ok(results)
                            }
                        }
                    })?;
                    detections += 1;
                    if let Some(info) = &mut debug {
                        let kept = tracker.blocks(&job.frame);
                        let rejected = outcome.rejected;
                        info.ocr_ms = ocr_ms;
                        info.translate_ms = translate_started.elapsed().as_secs_f32() * 1000.0;
                        info.frame = Some(Arc::new(job.frame.clone()));
                        let not_translated = if pack.is_some() {
                            "Not in the pack"
                        } else {
                            "No translation"
                        };
                        info.texts = kept
                            .iter()
                            .map(|block| DebugText {
                                text: block.source_text.clone(),
                                bounds: block.bounds,
                                confidence: block.confidence,
                                verdict: if block.translated_text.is_some() {
                                    Verdict::Translated
                                } else {
                                    Verdict::NotTranslated
                                },
                                note: block
                                    .translated_text
                                    .clone()
                                    .unwrap_or_else(|| not_translated.into()),
                            })
                            .chain(dropped.iter().map(|(block, verdict)| DebugText {
                                text: block.source_text.clone(),
                                bounds: block.bounds,
                                confidence: block.confidence,
                                verdict: *verdict,
                                note: match verdict {
                                    Verdict::LowConfidence => format!(
                                        "Below the {:.0}% minimum confidence",
                                        config.minimum_confidence * 100.0
                                    ),
                                    other => other.label().into(),
                                },
                            }))
                            .chain(rejected.iter().map(|text| DebugText {
                                text: text.text.clone(),
                                bounds: text.bounds,
                                confidence: text.confidence,
                                verdict: Verdict::Noise,
                                note: text.reason.into(),
                            }))
                            .collect();
                        let translated = kept
                            .iter()
                            .filter(|block| block.translated_text.is_some())
                            .count();
                        let area = match region {
                            Some(region) => format!("{}×{} region", region.width, region.height),
                            None => "whole window".into(),
                        };
                        let message = format!(
                            "Read text in {area}: {found} found, {} kept, {translated} translated, {requests} looked up, {} noise (OCR {:.0} ms, translation {:.0} ms)",
                            kept.len(),
                            rejected.len(),
                            info.ocr_ms,
                            info.translate_ms
                        );
                        log(&mut debug, message);
                    }
                    Ok(true)
                })();
                if result.is_err() {
                    // Read again on the next frame.
                    tracker.request_detection();
                }
            }
            // Start the next read when one is needed and OCR is free, once
            // the new content has settled (or kept changing too long to wait).
            if update == Update::Detect {
                pending_since.get_or_insert_with(Instant::now);
            }
            let settled = !config.performance.wait_for_settle
                || tracker.settled_for() >= SETTLE_TIME
                || pending_since.is_some_and(|since| since.elapsed() >= MAX_SETTLE_WAIT);
            if job.is_none()
                && update == Update::Detect
                && settled
                && rest_until.is_none_or(|until| Instant::now() >= until)
            {
                pending_since = None;
                let rendered = tracker.blocks(&frame);
                let detection = tracker.take_detection();
                // Read only the area that changed, when the tracker knows it.
                let area = match detection.region {
                    Some(region) => frame.crop(align_region(region)),
                    None => frame.clone(),
                };
                if jobs.send(area).is_ok() {
                    job = Some(ReadJob {
                        frame: frame.clone(),
                        detection,
                        rendered,
                    });
                }
            }
            if let Some(info) = &mut debug {
                info.decision = match update {
                    _ if read_now => "Read text (OCR)",
                    Update::Unchanged => "Unchanged",
                    Update::Moved => "Followed moving text",
                    Update::Detect => "Reading text (OCR)",
                };
                info.track_ms = track_ms;
                info.tracked = tracker.blocks(&frame).len();
                info.capture_ms = state.capture_micros.load(Ordering::Relaxed) as f32 / 1000.0;
                if let Err(error) = &result {
                    let message = format!("Error: {error}");
                    log(&mut debug, message);
                }
            }

            // Translations that finished since the last frame.
            let mut arrived = Vec::new();
            for (text, outcome) in background.finished() {
                match outcome {
                    Ok(translation) => {
                        cache.insert(&pair, text.clone(), translation.clone());
                        arrived.push((text, Translated::plain(translation)));
                    }
                    // Retrying wouldn't help; new text is tried when it appears.
                    Err(error) if error == crate::ai_cache::LIMIT_REACHED => {
                        if !limit_logged {
                            limit_logged = true;
                            log(&mut debug, format!("Stopped translating: {error}"));
                        }
                    }
                    Err(error) => {
                        // Read the screen again later to retry.
                        tracker.request_detection();
                        log(
                            &mut debug,
                            format!("Translation failed for \"{text}\": {error}"),
                        );
                    }
                }
            }
            let translated_now = !arrived.is_empty() && tracker.apply_translations(&arrived);
            if let Some(info) = &mut debug {
                for text in &mut info.texts {
                    if text.verdict == Verdict::NotTranslated
                        && let Some((_, translation)) = arrived
                            .iter()
                            .find(|(source, _)| normalize(source) == normalize(&text.text))
                    {
                        text.verdict = Verdict::Translated;
                        text.note = translation.text.clone();
                    }
                }
            }
            if translated_now {
                log(
                    &mut debug,
                    format!("{} translation(s) arrived", arrived.len()),
                );
            }
            let result = result.map(|changed| changed || translated_now);

            let Ok(mut snapshot) = state.snapshot.lock() else {
                continue;
            };
            if window_start.elapsed() >= Duration::from_secs(1) {
                snapshot.frames_per_second =
                    window_frames as f32 / window_start.elapsed().as_secs_f32();
                window_frames = 0;
                window_start = Instant::now();
            }
            snapshot.frames_processed = frames_checked;
            snapshot.detections = detections;
            if let Some(info) = &debug {
                snapshot.debug = Some(Arc::new(info.clone()));
            }
            match result {
                Ok(changed) => {
                    // Failed detections leave the tracker requesting detection
                    // again, so transient OCR or provider failures are retried
                    // even on an otherwise unchanged screen.
                    if changed {
                        // Rebuilding a backdrop is the costliest part of a
                        // redraw; one whose pixels didn't change is reused.
                        let mut reused = std::collections::HashMap::new();
                        snapshot.blocks = tracker
                            .blocks(&frame)
                            .into_iter()
                            .map(|mut block| {
                                let fingerprint = area_fingerprint(&frame, block.bounds);
                                let look = match backdrops.get(&block.bounds) {
                                    Some(look) if look.0 == fingerprint => look.clone(),
                                    _ => {
                                        let (background, foreground) =
                                            sample_colors(&frame, block.bounds);
                                        (
                                            fingerprint,
                                            background,
                                            foreground,
                                            rebuild_backdrop(&frame, block.bounds).map(Arc::new),
                                        )
                                    }
                                };
                                block.background = Some(look.1);
                                block.foreground = look.2;
                                block.backdrop = look.3.clone();
                                reused.insert(block.bounds, look);
                                block
                            })
                            .collect();
                        backdrops = reused;
                        state
                            .showing_text
                            .store(!snapshot.blocks.is_empty(), Ordering::Relaxed);
                    }
                    snapshot.capture_bounds = Some(frame.bounds);
                    snapshot.status = "Running".into();
                    snapshot.last_error = None;
                    drop(snapshot);
                    if changed
                        && let Ok(waker) = state.waker.lock()
                        && let Some(wake) = waker.as_ref()
                    {
                        wake();
                    }
                }
                Err(error) => {
                    snapshot.status = "Pipeline error".into();
                    snapshot.last_error = Some(error);
                    drop(snapshot);
                    thread::sleep(Duration::from_millis(250));
                }
            }
        }
        // Lets the OCR thread finish.
        drop(jobs);
    });

    if let Ok(mut snapshot) = state.snapshot.lock() {
        snapshot.running = false;
        snapshot.status = "Stopped".into();
    }
}

/// A read of the screen running on the OCR thread.
struct ReadJob {
    /// The frame it reads, for placing the text found.
    frame: Frame,
    detection: crate::tracker::Detection,
    /// The overlay as drawn when the read started, to ignore OCR of it.
    rendered: Vec<TextBlock>,
}

struct ReadOutcome {
    result: ServiceResult<Vec<TextBlock>>,
    rejected: Vec<crate::types::RejectedText>,
    took: Duration,
}

/// Translates texts on its own thread and hands results back one by one as
/// they finish. Shorter texts start first, so labels and buttons are
/// translated in a fraction of a second even while a paragraph takes longer.
enum BackgroundJob {
    Translate(Vec<String>),
    Forget(Vec<String>),
}

struct BackgroundTranslator {
    requests: Option<std::sync::mpsc::Sender<BackgroundJob>>,
    results: std::sync::mpsc::Receiver<(String, ServiceResult<String>)>,
    pending: std::collections::HashSet<String>,
    handle: Option<thread::JoinHandle<()>>,
}

impl BackgroundTranslator {
    fn start<T: TranslationBackend + 'static>(mut provider: T, pair: LanguagePair) -> Self {
        let (requests, incoming) = std::sync::mpsc::channel::<BackgroundJob>();
        let (outgoing, results) = std::sync::mpsc::channel();
        let handle = spawn_named("translate", move || {
            while let Ok(job) = incoming.recv() {
                let mut texts = Vec::new();
                // Merge requests that queued up meanwhile, in order.
                for job in std::iter::once(job).chain(incoming.try_iter()) {
                    match job {
                        BackgroundJob::Translate(more) => texts.extend(more),
                        BackgroundJob::Forget(forgotten) => {
                            for text in &forgotten {
                                provider.forget(&pair, text);
                            }
                        }
                    }
                }
                if texts.is_empty() {
                    continue;
                }
                texts.sort_by_key(String::len);
                let mut closed = false;
                provider.translate_each(&pair, &texts, &mut |index, result| {
                    closed |= outgoing.send((texts[index].clone(), result)).is_err();
                });
                if closed {
                    return;
                }
            }
        });
        Self {
            requests: Some(requests),
            results,
            pending: std::collections::HashSet::new(),
            handle: Some(handle),
        }
    }

    /// Queues texts that aren't already being translated.
    fn request(&mut self, texts: impl IntoIterator<Item = String>) {
        let new = texts
            .into_iter()
            .filter(|text| self.pending.insert(text.clone()))
            .collect::<Vec<_>>();
        if !new.is_empty()
            && let Some(requests) = &self.requests
        {
            let _ = requests.send(BackgroundJob::Translate(new));
        }
    }

    /// Has the provider forget saved translations of `texts`.
    fn forget(&mut self, texts: Vec<String>) {
        if let Some(requests) = &self.requests {
            let _ = requests.send(BackgroundJob::Forget(texts));
        }
    }

    /// Translations finished since the last call.
    fn finished(&mut self) -> Vec<(String, ServiceResult<String>)> {
        let results = self.results.try_iter().collect::<Vec<_>>();
        for (text, _) in &results {
            self.pending.remove(text);
        }
        results
    }
}

impl Drop for BackgroundTranslator {
    fn drop(&mut self) {
        // Closing the queue ends the thread once its current group is done;
        // don't wait for a slow provider.
        self.requests = None;
        self.handle.take();
    }
}

/// A cheap fingerprint of the pixels under `bounds` (screen coordinates),
/// sampled on a grid: equal when the area looks the same.
fn area_fingerprint(frame: &Frame, bounds: Rect) -> u64 {
    let area = frame.to_pixels(bounds);
    let left = area.left.clamp(0, frame.width as i32) as u32;
    let top = area.top.clamp(0, frame.height as i32) as u32;
    let right = (area.right().max(0) as u32).min(frame.width);
    let bottom = (area.bottom().max(0) as u32).min(frame.height);
    let mut hash = 0xcbf2_9ce4_8422_2325_u64;
    for y in (top..bottom).step_by(2) {
        for x in (left..right).step_by(2) {
            let index = ((y * frame.width + x) * 4) as usize;
            for &byte in &frame.rgba[index..index + 3] {
                hash = (hash ^ u64::from(byte)).wrapping_mul(0x0100_0000_01b3);
            }
        }
    }
    hash
}

/// Whether most of `inner` lies within `outer`.
fn mostly_inside(inner: Rect, outer: Rect) -> bool {
    let width = (inner.right().min(outer.right()) - inner.left.max(outer.left)).max(0);
    let height = (inner.bottom().min(outer.bottom()) - inner.top.max(outer.top)).max(0);
    let overlap = i64::from(width) * i64::from(height);
    let area = i64::from(inner.width) * i64::from(inner.height);
    area > 0 && overlap * 2 > area
}

/// The colours around and of the original text: a background gradient
/// (top-left, top-right, bottom-right, bottom-left) and the text's own
/// colour, if it stands out.
///
/// The background comes from thin strips just above and below the text,
/// outside the overlay's padding: between lines of a list or paragraph that
/// is almost always plain background, unlike the corners, which often land
/// on icons or highlight bars. Medians ignore stray pixels.
pub fn sample_colors(frame: &Frame, bounds: Rect) -> ([[u8; 4]; 4], Option<[u8; 4]>) {
    const GAP: i32 = 5;
    const THICKNESS: i32 = 3;
    let pixels = frame.to_pixels(bounds);
    let step = (pixels.width as i32 / 48).max(1) as usize;
    let strip = |top: i32| {
        let samples = (top..top + THICKNESS)
            .flat_map(|y| {
                (pixels.left..pixels.right().max(pixels.left + 1))
                    .step_by(step)
                    .map(move |x| frame.pixel(x, y))
            })
            .collect::<Vec<_>>();
        median_color(samples)
    };
    let inner_step = ((pixels.width * pixels.height / 3000) as f64)
        .sqrt()
        .max(1.0) as usize;
    let mut inside = (pixels.top..pixels.bottom())
        .step_by(inner_step)
        .flat_map(|y| {
            (pixels.left..pixels.right())
                .step_by(inner_step)
                .map(move |x| frame.pixel(x, y))
        })
        .collect::<Vec<_>>();
    // Strips just outside the text can land on a frame or highlight border
    // around a selected item. The colour filling most of the text's own box
    // is its background, so a strip that differs from it is replaced.
    let mut above = strip(pixels.top - GAP - THICKNESS);
    let mut below = strip(pixels.bottom() + GAP);
    if let Some(fill) = dominant_color(&inside) {
        for side in [&mut above, &mut below] {
            if color_distance(*side, fill) > STRIP_TOLERANCE {
                *side = fill;
            }
        }
    }
    let background = [above, above, below, below];

    // The text's colour: inside the box, the pixels least like the
    // background are the strokes.
    let reference = [
        (u16::from(above[0]) + u16::from(below[0])) / 2,
        (u16::from(above[1]) + u16::from(below[1])) / 2,
        (u16::from(above[2]) + u16::from(below[2])) / 2,
    ];
    let distance = |pixel: &[u8; 4]| {
        (0..3)
            .map(|channel| u16::from(pixel[channel]).abs_diff(reference[channel]))
            .sum::<u16>()
    };
    inside.sort_by_key(|pixel| std::cmp::Reverse(distance(pixel)));
    let strokes = inside.len() / 6;
    let foreground = (strokes > 0)
        .then(|| median_color(inside[..strokes].to_vec()))
        .filter(|color| distance(color) >= FOREGROUND_CONTRAST);
    (background, foreground)
}

/// Reconstructs the background behind text at `bounds` (screen units) from
/// the pixels just outside its padded box: each edge is sampled, cleaned of
/// stray text and borders, and the four edges are blended across the box
/// (a Coons patch). On flat panels this is a solid colour; on gradients,
/// blurred scenery or translucent panels it follows what's behind the text.
pub fn rebuild_backdrop(frame: &Frame, bounds: Rect) -> Option<crate::types::Backdrop> {
    const MAX_WIDTH: usize = 96;
    const MAX_HEIGHT: usize = 48;
    let padding = crate::types::Backdrop::PADDING;
    let padded = Rect::new(
        bounds.left - padding,
        bounds.top - padding,
        bounds.width + padding as u32 * 2,
        bounds.height + padding as u32 * 2,
    );
    let area = frame.to_pixels(padded);
    if area.width < 2 || area.height < 2 || frame.width == 0 {
        return None;
    }
    let (width, height) = (
        (area.width as usize).min(MAX_WIDTH),
        (area.height as usize).min(MAX_HEIGHT),
    );
    // Three pixels thick, just outside the box; the median of each
    // three-pixel run skips one-pixel lines.
    let edge = |count: usize, sample: &dyn Fn(usize, i32) -> [u8; 4]| {
        (0..count)
            .map(|index| median_color((1..=3).map(|depth| sample(index, depth)).collect()))
            .collect::<Vec<_>>()
    };
    let x_at = |index: usize| area.left + (index as i32 * area.width as i32) / width as i32;
    let y_at = |index: usize| area.top + (index as i32 * area.height as i32) / height as i32;
    let mut top = edge(width, &|i, depth| frame.pixel(x_at(i), area.top - depth));
    let mut bottom = edge(width, &|i, depth| {
        frame.pixel(x_at(i), area.bottom() - 1 + depth)
    });
    let mut left = edge(height, &|i, depth| frame.pixel(area.left - depth, y_at(i)));
    let mut right = edge(height, &|i, depth| {
        frame.pixel(area.right() - 1 + depth, y_at(i))
    });

    // Text or frame lines just outside the box would smear across it.
    // Where the box has a clear fill colour, edge pixels far from it are
    // replaced by it; otherwise a wide median removes short intrusions.
    let step = ((area.width * area.height / 3000) as f64).sqrt().max(1.0) as usize;
    let inside = (area.top..area.bottom())
        .step_by(step)
        .flat_map(|y| {
            (area.left..area.right())
                .step_by(step)
                .map(move |x| frame.pixel(x, y))
        })
        .collect::<Vec<_>>();
    let fill = dominant_color(&inside);
    for side in [&mut top, &mut bottom, &mut left, &mut right] {
        match fill {
            Some(fill) => {
                for pixel in side.iter_mut() {
                    if color_distance(*pixel, fill) > EDGE_TOLERANCE {
                        *pixel = fill;
                    }
                }
            }
            None => *side = median_filter(side, 3),
        }
        *side = box_blur(side, 2);
    }

    let corner = |a: [u8; 4], b: [u8; 4]| {
        [0, 1, 2].map(|channel| (f32::from(a[channel]) + f32::from(b[channel])) / 2.0)
    };
    let top_left = corner(top[0], left[0]);
    let top_right = corner(top[width - 1], right[0]);
    let bottom_left = corner(bottom[0], left[height - 1]);
    let bottom_right = corner(bottom[width - 1], right[height - 1]);
    let mut rgba = Vec::with_capacity(width * height * 4);
    for y in 0..height {
        let v = y as f32 / (height - 1) as f32;
        for x in 0..width {
            let u = x as f32 / (width - 1) as f32;
            for channel in 0..3 {
                let edges = (1.0 - v) * f32::from(top[x][channel])
                    + v * f32::from(bottom[x][channel])
                    + (1.0 - u) * f32::from(left[y][channel])
                    + u * f32::from(right[y][channel]);
                let corners = (1.0 - u) * (1.0 - v) * top_left[channel]
                    + u * (1.0 - v) * top_right[channel]
                    + (1.0 - u) * v * bottom_left[channel]
                    + u * v * bottom_right[channel];
                rgba.push((edges - corners).round().clamp(0.0, 255.0) as u8);
            }
            rgba.push(255);
        }
    }
    Some(crate::types::Backdrop {
        width: width as u32,
        height: height as u32,
        rgba,
        texture: std::sync::OnceLock::new(),
    })
}

/// How far (sum over channels) an edge pixel may be from the box's fill
/// before it counts as text or a frame line rather than background.
const EDGE_TOLERANCE: u16 = 36;

/// Median over a window of `radius` on each side, by brightness.
fn median_filter(colors: &[[u8; 4]], radius: usize) -> Vec<[u8; 4]> {
    (0..colors.len())
        .map(|index| {
            let start = index.saturating_sub(radius);
            let end = (index + radius + 1).min(colors.len());
            median_color(colors[start..end].to_vec())
        })
        .collect()
}

/// Mean over a window of `radius` on each side.
fn box_blur(colors: &[[u8; 4]], radius: usize) -> Vec<[u8; 4]> {
    (0..colors.len())
        .map(|index| {
            let start = index.saturating_sub(radius);
            let end = (index + radius + 1).min(colors.len());
            let window = &colors[start..end];
            let mut mean = [0, 0, 0, 255];
            for channel in 0..3 {
                let sum = window
                    .iter()
                    .map(|color| u32::from(color[channel]))
                    .sum::<u32>();
                mean[channel] = (sum / window.len() as u32) as u8;
            }
            mean
        })
        .collect()
}

/// Largest colour difference (sum over channels) between a strip outside the
/// text and the fill inside it for the strip to count as background.
const STRIP_TOLERANCE: u16 = 60;
/// Share of the text's box the fill must cover to be trusted over the
/// strips; text strokes rarely cover this much.
const MIN_FILL_SHARE: usize = 40;

fn color_distance(first: [u8; 4], second: [u8; 4]) -> u16 {
    (0..3)
        .map(|channel| u16::from(first[channel]).abs_diff(u16::from(second[channel])))
        .sum()
}

/// The colour covering most of `samples`, when it covers enough of them to
/// be a background rather than text strokes.
fn dominant_color(samples: &[[u8; 4]]) -> Option<[u8; 4]> {
    let mut bins = std::collections::HashMap::<[u8; 3], (usize, [u32; 3])>::new();
    for pixel in samples {
        let (count, sum) = bins
            .entry([pixel[0] >> 4, pixel[1] >> 4, pixel[2] >> 4])
            .or_default();
        *count += 1;
        for channel in 0..3 {
            sum[channel] += u32::from(pixel[channel]);
        }
    }
    let (_, (count, sum)) = bins.into_iter().max_by_key(|(_, (count, _))| *count)?;
    let fill = sum.map(|channel| (channel / count as u32) as u8);
    let fill = [fill[0], fill[1], fill[2], 255];
    let covered = samples
        .iter()
        .filter(|pixel| color_distance(**pixel, fill) <= STRIP_TOLERANCE)
        .count();
    (covered * 100 >= samples.len() * MIN_FILL_SHARE).then_some(fill)
}

/// Minimum colour difference (sum over channels) for the original text's
/// colour to be reused. Low enough for greyed-out entries, whose dim colour
/// tells the player something; near this limit they were drawn white or
/// grey from one read to the next.
const FOREGROUND_CONTRAST: u16 = 60;

fn median_color(mut samples: Vec<[u8; 4]>) -> [u8; 4] {
    samples.sort_by_key(|pixel| u32::from(pixel[0]) + u32::from(pixel[1]) + u32::from(pixel[2]));
    let mut median = samples
        .get(samples.len() / 2)
        .copied()
        .unwrap_or([0, 0, 0, 255]);
    median[3] = 255;
    median
}

fn is_overlay_echo(block: &TextBlock, rendered_blocks: &[TextBlock], pair: &LanguagePair) -> bool {
    if pair.source == pair.target {
        return false;
    }
    let source_text = block.source_text.trim();
    rendered_blocks.iter().any(|rendered| {
        // Text still like the original is the game's own, even when the
        // translation keeps it (a name left as is, or with an icon misread
        // dropped).
        rendered
            .translated_text
            .as_deref()
            .is_some_and(|translated| {
                translated.trim() == source_text
                    && text_similarity(&normalize(source_text), &normalize(&rendered.source_text))
                        < 0.8
            })
            && block.bounds.left < rendered.bounds.right()
            && rendered.bounds.left < block.bounds.right()
            && block.bounds.top < rendered.bounds.bottom()
            && rendered.bounds.top < block.bounds.bottom()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::{CaptureBackend, OcrBackend, TranslationBackend};
    use crate::types::{Frame, Rect, SourceKind};

    #[test]
    fn captures_never_run_back_to_back() {
        let ms = Duration::from_millis;
        // A fast capture waits out the rest of the interval.
        assert_eq!(capture_rest(ms(10), ms(100)), ms(90));
        // A capture slower than the interval still rests as long as it took.
        assert_eq!(capture_rest(ms(30), ms(16)), ms(30));
        assert_eq!(capture_rest(ms(60), ms(100)), ms(60));
    }

    struct FakeCapture;

    impl CaptureBackend for FakeCapture {
        fn sources(&mut self) -> ServiceResult<Vec<Source>> {
            Ok(Vec::new())
        }

        fn capture(&mut self, _source: &Source) -> ServiceResult<Frame> {
            Ok(Frame::new(
                1,
                Rect::new(0, 0, 10, 10),
                1,
                1,
                vec![255, 255, 255, 255],
            ))
        }
    }

    struct FakeOcr;

    impl OcrBackend for FakeOcr {
        fn recognize(&mut self, _frame: &Frame, _language: &str) -> ServiceResult<Vec<TextBlock>> {
            Ok(vec![TextBlock::new(Rect::new(1, 1, 8, 8), "猫です", 0.95)])
        }
    }

    struct FakeTranslation;

    impl TranslationBackend for FakeTranslation {
        fn translate(&mut self, _pair: &LanguagePair, _text: &str) -> ServiceResult<String> {
            Ok("แมว".into())
        }
    }

    struct RetryTranslation {
        attempts: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    }

    impl TranslationBackend for RetryTranslation {
        fn translate(&mut self, _pair: &LanguagePair, _text: &str) -> ServiceResult<String> {
            let attempt = self
                .attempts
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            if attempt == 0 {
                Err("temporary provider failure".into())
            } else {
                Ok("retry-ok".into())
            }
        }
    }

    /// A screen whose text is drawn per `phase`: 0 one text, 1 a second text
    /// appears, 2 everything is gone.
    struct PhasedCapture(Arc<std::sync::atomic::AtomicU8>);

    impl CaptureBackend for PhasedCapture {
        fn sources(&mut self) -> ServiceResult<Vec<Source>> {
            Ok(Vec::new())
        }

        fn capture(&mut self, _source: &Source) -> ServiceResult<Frame> {
            let phase = self.0.load(Ordering::Relaxed);
            let mut rgba = vec![30; 64 * 32 * 4];
            let mut strokes = |top: usize| {
                for y in top..top + 10 {
                    for x in (4..44).step_by(3) {
                        rgba[(y * 64 + x) * 4..][..4].copy_from_slice(&[240, 240, 240, 255]);
                    }
                }
            };
            if phase < 2 {
                strokes(4);
            }
            if phase == 1 {
                strokes(18);
            }
            Ok(Frame::new(1, Rect::new(0, 0, 64, 32), 64, 32, rgba))
        }
    }

    /// Reads the first text at once; every later read takes a while.
    struct SlowAfterFirstOcr(usize);

    impl OcrBackend for SlowAfterFirstOcr {
        fn recognize(&mut self, _frame: &Frame, _language: &str) -> ServiceResult<Vec<TextBlock>> {
            self.0 += 1;
            if self.0 == 1 {
                Ok(vec![TextBlock::new(
                    Rect::new(4, 4, 40, 10),
                    "Quest log",
                    0.9,
                )])
            } else {
                std::thread::sleep(Duration::from_millis(1500));
                Ok(Vec::new())
            }
        }
    }

    #[test]
    fn vanished_text_clears_without_waiting_for_a_running_read() {
        let phase = Arc::new(std::sync::atomic::AtomicU8::new(0));
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 64, 32),
            detail: String::new(),
        };
        let worker = PipelineWorker::start(
            config,
            source,
            PhasedCapture(Arc::clone(&phase)),
            SlowAfterFirstOcr(0),
            FakeTranslation,
        );
        let shown = |worker: &PipelineWorker| !worker.snapshot().blocks.is_empty();
        let wait_for = |condition: &dyn Fn() -> bool, limit: Duration| {
            let started = Instant::now();
            while !condition() {
                if started.elapsed() > limit {
                    return false;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            true
        };
        assert!(wait_for(&|| shown(&worker), Duration::from_secs(2)));
        // New text starts a slow read; the old text vanishes meanwhile.
        phase.store(1, Ordering::Relaxed);
        std::thread::sleep(Duration::from_millis(150));
        phase.store(2, Ordering::Relaxed);
        assert!(
            wait_for(&|| !shown(&worker), Duration::from_millis(400)),
            "the translation stayed while OCR was busy"
        );
        drop(worker);
    }

    #[test]
    fn worker_publishes_translated_blocks_and_stops() {
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let worker = PipelineWorker::start(config, source, FakeCapture, FakeOcr, FakeTranslation);

        let mut found_translation = false;
        for _ in 0..50 {
            if worker
                .snapshot()
                .blocks
                .first()
                .and_then(|block| block.translated_text.as_deref())
                == Some("แมว")
            {
                found_translation = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(
            found_translation,
            "worker did not publish translated OCR output"
        );
        drop(worker);
    }

    #[test]
    fn hidden_text_leaves_the_screen_and_is_not_translated() {
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let worker = PipelineWorker::start(config, source, FakeCapture, FakeOcr, FakeTranslation);
        let wait_for = |done: &dyn Fn(&PipelineSnapshot) -> bool| {
            (0..100).any(|_| {
                std::thread::sleep(Duration::from_millis(10));
                done(&worker.snapshot())
            })
        };
        assert!(wait_for(&|snapshot| !snapshot.blocks.is_empty()));
        worker.set_hidden(&["猫です".to_string()]);
        assert!(
            wait_for(&|snapshot| snapshot.blocks.is_empty()),
            "hidden text stayed on screen"
        );
    }

    struct Numbered(Arc<std::sync::atomic::AtomicUsize>);

    impl TranslationBackend for Numbered {
        fn translate(&mut self, _pair: &LanguagePair, _text: &str) -> ServiceResult<String> {
            Ok(format!(
                "try {}",
                self.0.fetch_add(1, Ordering::Relaxed) + 1
            ))
        }
    }

    #[test]
    fn translating_again_replaces_the_translation_on_screen() {
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let worker = PipelineWorker::start(
            config,
            source,
            FakeCapture,
            FakeOcr,
            Numbered(Arc::clone(&calls)),
        );
        let shows = |expected: &str| {
            (0..100).any(|_| {
                std::thread::sleep(Duration::from_millis(10));
                worker
                    .snapshot()
                    .blocks
                    .first()
                    .and_then(|block| block.translated_text.clone())
                    .as_deref()
                    == Some(expected)
            })
        };
        assert!(shows("try 1"));
        worker.retranslate("猫です".into());
        assert!(shows("try 2"), "the old translation stayed");
    }

    #[test]
    fn worker_retries_an_unchanged_frame_after_provider_failure() {
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let attempts = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let worker = PipelineWorker::start(
            config,
            source,
            FakeCapture,
            FakeOcr,
            RetryTranslation {
                attempts: std::sync::Arc::clone(&attempts),
            },
        );

        let mut found_translation = false;
        for _ in 0..100 {
            if worker
                .snapshot()
                .blocks
                .first()
                .and_then(|block| block.translated_text.as_deref())
                == Some("retry-ok")
            {
                found_translation = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(found_translation, "worker did not retry the failed frame");
        assert!(attempts.load(std::sync::atomic::Ordering::Relaxed) >= 2);
        drop(worker);
    }

    #[test]
    fn running_worker_switches_to_an_updated_pack() {
        use crate::pack::{Pack, PackEntry};
        let pack = |translation: &str| Pack {
            entries: vec![PackEntry {
                source_text: "猫です".into(),
                translation: translation.into(),
                ..PackEntry::default()
            }],
            ..Pack::default()
        };
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let worker = PipelineWorker::start_with_pack(
            config,
            source,
            FakeCapture,
            FakeOcr,
            FakeTranslation,
            Some(pack("first")),
            false,
        );
        let shows = |text: &str| {
            (0..100).any(|_| {
                let found = worker
                    .snapshot()
                    .blocks
                    .first()
                    .and_then(|block| block.translated_text.as_deref())
                    == Some(text);
                if !found {
                    std::thread::sleep(Duration::from_millis(10));
                }
                found
            })
        };
        assert!(shows("first"));
        worker.set_pack(pack("edited"));
        assert!(
            shows("edited"),
            "an unchanged screen picks up the edited pack"
        );
        drop(worker);
    }

    #[test]
    fn ai_translates_text_the_pack_does_not_cover() {
        use crate::pack::{Pack, PackEntry};
        let pack = Pack {
            entries: vec![PackEntry {
                source_text: "something else".into(),
                translation: "อย่างอื่น".into(),
                ..PackEntry::default()
            }],
            ..Pack::default()
        };
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let start = |fill: bool| {
            PipelineWorker::start_with_pack(
                config.clone(),
                source.clone(),
                FakeCapture,
                FakeOcr,
                FakeTranslation,
                Some(pack.clone()),
                fill,
            )
        };
        let translation = |worker: &PipelineWorker| {
            (0..100).find_map(|_| {
                let found = worker
                    .snapshot()
                    .blocks
                    .first()
                    .and_then(|block| block.translated_text.clone());
                if found.is_none() {
                    std::thread::sleep(Duration::from_millis(10));
                }
                found
            })
        };
        let filled = start(true);
        assert_eq!(translation(&filled).as_deref(), Some("แมว"));
        drop(filled);
        let unfilled = start(false);
        assert_eq!(translation(&unfilled), None);
        drop(unfilled);
    }

    #[test]
    fn debug_mode_reports_what_happened_to_each_text() {
        let config = AppConfig {
            capture_interval_ms: 16,
            debug: true,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let worker = PipelineWorker::start(config, source, FakeCapture, FakeOcr, FakeTranslation);
        let mut debug = None;
        for _ in 0..100 {
            debug = worker.snapshot().debug.filter(|debug| {
                debug
                    .texts
                    .first()
                    .is_some_and(|text| text.verdict == Verdict::Translated)
            });
            if debug.is_some() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let debug = debug.expect("debug info with OCR results");
        assert_eq!(debug.texts[0].verdict, Verdict::Translated);
        assert_eq!(debug.texts[0].note, "แมว");
        assert!(debug.frame.is_some());
        assert!(debug.log.iter().any(|line| line.contains("Read text")));
        drop(worker);
    }

    struct TwoTextsOcr;

    impl OcrBackend for TwoTextsOcr {
        fn recognize(&mut self, _frame: &Frame, _language: &str) -> ServiceResult<Vec<TextBlock>> {
            Ok(vec![
                TextBlock::new(
                    Rect::new(1, 1, 8, 4),
                    "A long paragraph that takes a while",
                    0.95,
                ),
                TextBlock::new(Rect::new(1, 6, 8, 3), "Tasks", 0.95),
            ])
        }
    }

    /// Takes a while for long texts, like a real service.
    struct SlowForLongTexts;

    impl TranslationBackend for SlowForLongTexts {
        fn translate(&mut self, _pair: &LanguagePair, text: &str) -> ServiceResult<String> {
            if text.len() > 10 {
                std::thread::sleep(Duration::from_millis(800));
            }
            Ok(format!("<{text}>"))
        }
    }

    #[test]
    fn short_translations_do_not_wait_for_long_ones() {
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let worker =
            PipelineWorker::start(config, source, FakeCapture, TwoTextsOcr, SlowForLongTexts);
        let translation = |snapshot: &PipelineSnapshot, text: &str| {
            snapshot
                .blocks
                .iter()
                .find(|block| block.source_text == text)
                .and_then(|block| block.translated_text.clone())
        };
        let mut snapshot = worker.snapshot();
        for _ in 0..150 {
            snapshot = worker.snapshot();
            if snapshot
                .blocks
                .iter()
                .any(|block| block.translated_text.is_some())
            {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        // "Tasks" shows while the paragraph is still being translated.
        assert_eq!(translation(&snapshot, "Tasks").as_deref(), Some("<Tasks>"));
        assert!(
            translation(&snapshot, "A long paragraph that takes a while").is_none(),
            "the paragraph arrives later"
        );
        drop(worker);
    }

    struct HiddenCapture;

    impl CaptureBackend for HiddenCapture {
        fn sources(&mut self) -> ServiceResult<Vec<Source>> {
            Ok(Vec::new())
        }

        fn capture(&mut self, source: &Source) -> ServiceResult<Frame> {
            FakeCapture.capture(source)
        }

        fn source_is_active(&mut self, _source: &Source) -> ServiceResult<bool> {
            Ok(false)
        }

        fn can_capture_hidden(&mut self, _source: &Source) -> bool {
            true
        }
    }

    #[test]
    fn does_no_work_while_the_window_is_hidden() {
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Window,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let worker = PipelineWorker::start(config, source, HiddenCapture, FakeOcr, FakeTranslation);
        std::thread::sleep(Duration::from_millis(400));
        let snapshot = worker.snapshot();
        assert!(!snapshot.source_active, "the window is reported as hidden");
        assert_eq!(snapshot.frames_processed, 0, "nothing is captured");
        assert!(snapshot.blocks.is_empty());
        drop(worker);
    }

    #[test]
    fn samples_the_background_between_lines_and_keeps_the_text_colour() {
        let (width, height) = (200_u32, 100_u32);
        let mut frame = Frame::new(
            1,
            Rect::new(0, 0, width, height),
            width,
            height,
            [20, 60, 70, 255].repeat((width * height) as usize),
        );
        // Yellow strokes inside the text box at (60, 40) 100×20.
        for y in 40..60 {
            for x in (60..160).step_by(3) {
                frame.fill(Rect::new(x, y, 1, 1), [250, 220, 60, 255]);
            }
        }
        // A red icon just left of the text, where corner sampling used to land.
        frame.fill(Rect::new(30, 25, 28, 50), [200, 30, 30, 255]);
        let (background, foreground) = sample_colors(&frame, Rect::new(60, 40, 100, 20));
        assert!(
            background.iter().all(|color| *color == [20, 60, 70, 255]),
            "{background:?}"
        );
        assert_eq!(foreground, Some([250, 220, 60, 255]));
    }

    #[test]
    fn rebuilt_backdrop_follows_a_gradient_and_drops_the_text() {
        let (width, height) = (200_u32, 60_u32);
        // A left-to-right gradient, like a vignette or blurred scenery.
        let mut rgba = Vec::with_capacity((width * height * 4) as usize);
        for _ in 0..height {
            for x in 0..width {
                let shade = (x * 255 / width) as u8;
                rgba.extend([shade, shade / 2, 40, 255]);
            }
        }
        let mut frame = Frame::new(1, Rect::new(0, 0, width, height), width, height, rgba);
        for y in 22..38 {
            for x in (40..160).step_by(3) {
                frame.fill(Rect::new(x, y, 1, 1), [255, 255, 0, 255]);
            }
        }
        let backdrop = rebuild_backdrop(&frame, Rect::new(40, 22, 120, 16)).expect("backdrop");
        let pixel = |x: u32, y: u32| {
            let index = ((y * backdrop.width + x) * 4) as usize;
            [
                backdrop.rgba[index],
                backdrop.rgba[index + 1],
                backdrop.rgba[index + 2],
            ]
        };
        let middle = backdrop.height / 2;
        let (left, right) = (pixel(0, middle), pixel(backdrop.width - 1, middle));
        assert!(left[0] < 60 && right[0] > 190, "{left:?} .. {right:?}");
        // No trace of the yellow strokes.
        for x in 0..backdrop.width {
            assert!(pixel(x, middle)[2] < 80, "{:?}", pixel(x, middle));
        }
    }

    #[test]
    fn rebuilt_backdrop_ignores_a_panel_border_next_to_the_text() {
        let (width, height) = (200_u32, 60_u32);
        let mut frame = Frame::new(
            1,
            Rect::new(0, 0, width, height),
            width,
            height,
            [10, 35, 45, 255].repeat((width * height) as usize),
        );
        // A bright border and a brown world just left of the text.
        frame.fill(Rect::new(0, 0, 36, 60), [90, 60, 40, 255]);
        frame.fill(Rect::new(36, 0, 2, 60), [40, 160, 200, 255]);
        for y in 22..38 {
            for x in (42..160).step_by(4) {
                frame.fill(Rect::new(x, y, 1, 1), [240, 240, 240, 255]);
            }
        }
        let backdrop = rebuild_backdrop(&frame, Rect::new(42, 22, 118, 16)).expect("backdrop");
        for pixel in backdrop.rgba.as_chunks::<4>().0 {
            assert!(
                color_distance([pixel[0], pixel[1], pixel[2], 255], [10, 35, 45, 255]) <= 12,
                "{pixel:?}"
            );
        }
    }

    #[test]
    fn ignores_a_highlight_border_around_selected_text() {
        let (width, height) = (200_u32, 100_u32);
        let mut frame = Frame::new(
            1,
            Rect::new(0, 0, width, height),
            width,
            height,
            [10, 40, 50, 255].repeat((width * height) as usize),
        );
        // A selected row: grey fill with a light border close to the text.
        frame.fill(Rect::new(40, 32, 140, 36), [200, 200, 200, 255]);
        frame.fill(Rect::new(45, 37, 130, 26), [70, 75, 80, 255]);
        for y in 42..58 {
            for x in (60..160).step_by(4) {
                frame.fill(Rect::new(x, y, 1, 1), [240, 240, 240, 255]);
            }
        }
        let (background, _) = sample_colors(&frame, Rect::new(60, 42, 100, 16));
        for color in background {
            assert!(
                color_distance(color, [70, 75, 80, 255]) <= 6,
                "{background:?}"
            );
        }
    }

    #[test]
    fn excludes_text_mostly_inside_an_area() {
        let window = Rect::new(100, 100, 400, 300);
        assert!(mostly_inside(Rect::new(120, 120, 80, 20), window));
        assert!(mostly_inside(Rect::new(80, 120, 100, 20), window));
        assert!(!mostly_inside(Rect::new(20, 120, 100, 20), window));
        assert!(!mostly_inside(Rect::new(600, 120, 80, 20), window));
    }

    #[test]
    fn async_stop_returns_a_reaper_handle() {
        let config = AppConfig {
            capture_interval_ms: 16,
            ..AppConfig::default()
        };
        let source = Source {
            id: "test".into(),
            name: "Test source".into(),
            kind: SourceKind::Screen,
            bounds: Rect::new(0, 0, 10, 10),
            detail: String::new(),
        };
        let mut worker =
            PipelineWorker::start(config, source, FakeCapture, FakeOcr, FakeTranslation);
        let reaper = worker.stop_async();
        reaper.join().expect("worker reaper panicked");
        drop(worker);
    }

    #[test]
    fn original_text_close_to_its_translation_is_not_an_echo() {
        let pair = LanguagePair::new("en", "th");
        let mut rendered = TextBlock::new(Rect::new(10, 10, 80, 20), "FMekanism", 0.95);
        rendered.translated_text = Some("Mekanism".into());
        let read = TextBlock::new(Rect::new(12, 12, 70, 18), "Mekanism", 0.95);
        assert!(!is_overlay_echo(&read, &[rendered], &pair));
    }

    #[test]
    fn text_translated_as_itself_is_not_an_echo() {
        let pair = LanguagePair::new("en", "th");
        let mut rendered = TextBlock::new(Rect::new(10, 10, 80, 20), "Bounty Board", 0.95);
        rendered.translated_text = Some("Bounty Board".into());
        let read = TextBlock::new(Rect::new(12, 12, 70, 18), "Bounty Board", 0.95);
        assert!(!is_overlay_echo(&read, &[rendered], &pair));
    }

    #[test]
    fn filters_previous_translation_overlay_echoes() {
        let pair = LanguagePair::new("ja", "th");
        let mut rendered = TextBlock::new(Rect::new(10, 10, 80, 20), "猫", 0.95);
        rendered.translated_text = Some("แมว".into());
        let echo = TextBlock::new(Rect::new(12, 12, 70, 18), "แมว", 0.95);
        let unrelated = TextBlock::new(Rect::new(200, 200, 70, 18), "แมว", 0.95);

        assert!(is_overlay_echo(&echo, &[rendered.clone()], &pair));
        assert!(!is_overlay_echo(&unrelated, &[rendered], &pair));
        assert!(!is_overlay_echo(
            &echo,
            &[TextBlock {
                translated_text: Some("แมว".into()),
                ..echo.clone()
            }],
            &LanguagePair::new("th", "th")
        ));
    }
}
