//! Platform-neutral building blocks for Overlayr.
//!
//! Capture, OCR, translation, and UI integrations should depend on these
//! contracts rather than on a specific operating system implementation.

pub mod ai;
pub mod auth;
pub mod bundle;
pub mod capture;
pub mod config;
pub mod control;
pub mod diagnostics;
#[cfg(target_os = "macos")]
pub mod hotkeys;
#[cfg(unix)]
pub mod hyprland;
pub mod installer;
#[cfg(target_os = "macos")]
pub mod macos;
pub mod marketplace;
pub mod marketplace_ui;
pub mod neural;
pub mod ocr;
pub mod pack;
pub mod pipeline;
pub mod resources;
pub mod resources_ui;
pub mod runtime;
pub mod services;
pub mod single_instance;
pub mod studio;
pub mod studio_ui;
pub mod text;
pub mod tracker;
pub mod translation;
pub mod types;
pub mod ui;

#[cfg(feature = "native-capture")]
pub use capture::XCapCapture;
pub use capture::{GrimCapture, ImageMagickCapture, PlatformCapture};
pub use config::AppConfig;
#[cfg(feature = "native-ocr")]
pub use ocr::NativeTesseractOcr;
pub use ocr::TesseractOcr;
pub use pipeline::{LatestFrame, TranslationCache};
pub use runtime::{PipelineSnapshot, PipelineWorker};
pub use services::{CaptureBackend, OcrBackend, TranslationBackend, TranslationCoordinator};
pub use translation::LibreTranslate;
pub use types::{Frame, LanguagePair, Rect, Source, SourceKind, TextBlock};
