use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::config::AppConfig;
use crate::pipeline::LatestFrame;
use crate::services::{
    CaptureBackend, OcrBackend, ServiceResult, TranslationBackend, TranslationCoordinator,
};
use crate::text::is_valid_text;
use crate::types::{Frame, LanguagePair, Rect, Source, TextBlock};

#[derive(Clone, Debug, Default)]
pub struct PipelineSnapshot {
    pub running: bool,
    pub status: String,
    pub frames_processed: u64,
    pub frames_per_second: f32,
    pub capture_bounds: Option<Rect>,
    pub last_error: Option<String>,
    pub blocks: Vec<TextBlock>,
}

struct SharedState {
    snapshot: Mutex<PipelineSnapshot>,
    stop: AtomicBool,
}

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
        mut capture: C,
        mut ocr: O,
        translator: T,
    ) -> Self
    where
        C: CaptureBackend + 'static,
        O: OcrBackend + 'static,
        T: TranslationBackend + 'static,
    {
        let state = Arc::new(SharedState {
            snapshot: Mutex::new(PipelineSnapshot {
                running: true,
                status: "Starting pipeline".into(),
                ..PipelineSnapshot::default()
            }),
            stop: AtomicBool::new(false),
        });
        let frames = Arc::new(LatestFrame::new());
        let capture_frames = Arc::clone(&frames);
        let capture_state = Arc::clone(&state);
        let capture_config = config.clone();
        let capture_handle = thread::spawn(move || {
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
        let pipeline_handle = thread::spawn(move || {
            run_pipeline(
                config,
                &mut ocr,
                translator,
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
    let interval = Duration::from_millis(config.capture_interval_ms.max(16));
    while !state.stop.load(Ordering::Acquire) {
        let started = Instant::now();
        match capture.capture(&source) {
            Ok(frame) => {
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
        let elapsed = started.elapsed();
        if elapsed < interval {
            thread::sleep(interval - elapsed);
        }
    }
}

fn run_pipeline<O, T>(
    config: AppConfig,
    ocr: &mut O,
    translator: T,
    frames: Arc<LatestFrame<Frame>>,
    state: Arc<SharedState>,
) where
    O: OcrBackend,
    T: TranslationBackend,
{
    let pair = LanguagePair::new(config.source_lang.clone(), config.target_lang.clone());
    let mut coordinator = TranslationCoordinator::new(translator, 512);
    let mut previous_signature = None;
    let mut frames_processed = 0;
    let mut window_start = Instant::now();

    while !state.stop.load(Ordering::Acquire) {
        let Some(frame) = frames.recv() else {
            break;
        };
        let capture_bounds = frame.bounds;
        let signature = frame_signature(&frame.rgba);
        if config.translate_only_when_changed && previous_signature == Some(signature) {
            continue;
        }
        previous_signature = Some(signature);
        let result: ServiceResult<Vec<TextBlock>> = (|| {
            let blocks = ocr
                .recognize(&frame, &config.source_lang)?
                .into_iter()
                .filter(|block| block.confidence >= config.minimum_confidence)
                .filter(|block| is_valid_text(&block.source_text, &config.source_lang))
                .collect::<Vec<_>>();
            coordinator.translate_blocks(&pair, blocks)
        })();

        match result {
            Ok(blocks) => {
                frames_processed += 1;
                if let Ok(mut snapshot) = state.snapshot.lock() {
                    snapshot.blocks = blocks;
                    snapshot.capture_bounds = Some(capture_bounds);
                    snapshot.frames_processed = frames_processed;
                    snapshot.status = "Running".into();
                    snapshot.last_error = None;
                    snapshot.frames_per_second = if window_start.elapsed() >= Duration::from_secs(1)
                    {
                        let fps = frames_processed as f32 / window_start.elapsed().as_secs_f32();
                        frames_processed = 0;
                        window_start = Instant::now();
                        fps
                    } else {
                        snapshot.frames_per_second
                    };
                }
            }
            Err(error) => {
                if let Ok(mut snapshot) = state.snapshot.lock() {
                    snapshot.status = "Pipeline error".into();
                    snapshot.last_error = Some(error);
                }
                thread::sleep(Duration::from_millis(250));
            }
        }
    }

    if let Ok(mut snapshot) = state.snapshot.lock() {
        snapshot.running = false;
        snapshot.status = "Stopped".into();
    }
}

fn frame_signature(rgba: &[u8]) -> u64 {
    // A fast, deterministic sampling hash avoids OCR work when a frame is
    // unchanged while keeping the full frame out of the worker's state.
    let step = (rgba.len() / 4096).max(4);
    rgba.iter()
        .step_by(step)
        .fold(1469598103934665603_u64, |hash, byte| {
            (hash ^ u64::from(*byte)).wrapping_mul(1099511628211)
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::{CaptureBackend, OcrBackend, TranslationBackend};
    use crate::types::{Frame, Rect, SourceKind};

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
}
