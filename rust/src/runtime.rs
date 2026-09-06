use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::config::AppConfig;
use crate::services::{
    CaptureBackend, OcrBackend, ServiceResult, TranslationBackend, TranslationCoordinator,
};
use crate::text::is_valid_text;
use crate::types::{LanguagePair, Rect, Source, TextBlock};

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
    handle: Option<JoinHandle<()>>,
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
        let thread_state = Arc::clone(&state);
        let handle = thread::spawn(move || {
            run_pipeline(
                config,
                source,
                &mut capture,
                &mut ocr,
                translator,
                thread_state,
            );
        });
        Self {
            state,
            handle: Some(handle),
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
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

impl Drop for PipelineWorker {
    fn drop(&mut self) {
        self.stop();
    }
}

fn run_pipeline<C, O, T>(
    config: AppConfig,
    source: Source,
    capture: &mut C,
    ocr: &mut O,
    translator: T,
    state: Arc<SharedState>,
) where
    C: CaptureBackend,
    O: OcrBackend,
    T: TranslationBackend,
{
    let interval = Duration::from_millis(config.capture_interval_ms.max(16));
    let pair = LanguagePair::new(config.source_lang.clone(), config.target_lang.clone());
    let mut coordinator = TranslationCoordinator::new(translator, 512);
    let mut previous_signature = None;
    let mut frames_processed = 0;
    let mut window_start = Instant::now();

    while !state.stop.load(Ordering::Acquire) {
        let started = Instant::now();
        let result: ServiceResult<Option<(Rect, Vec<TextBlock>)>> = (|| {
            let frame = capture.capture(&source)?;
            let capture_bounds = frame.bounds;
            let signature = frame_signature(&frame.rgba);
            if config.translate_only_when_changed && previous_signature == Some(signature) {
                return Ok(None);
            }
            previous_signature = Some(signature);
            let blocks = ocr
                .recognize(&frame, &config.source_lang)?
                .into_iter()
                .filter(|block| block.confidence >= config.minimum_confidence)
                .filter(|block| is_valid_text(&block.source_text, &config.source_lang))
                .collect::<Vec<_>>();
            Ok(Some((
                capture_bounds,
                coordinator.translate_blocks(&pair, blocks)?,
            )))
        })();

        match result {
            Ok(Some((capture_bounds, blocks))) => {
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
            Ok(None) => {}
            Err(error) => {
                if let Ok(mut snapshot) = state.snapshot.lock() {
                    snapshot.status = "Pipeline error".into();
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
