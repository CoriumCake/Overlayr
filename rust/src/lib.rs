//! Platform-neutral building blocks for Overlayr.
//!
//! Capture, OCR, translation, and UI integrations should depend on these
//! contracts rather than on a specific operating system implementation.

pub mod pipeline;
pub mod types;

pub use pipeline::{LatestFrame, TranslationCache};
pub use types::{LanguagePair, Rect, TextBlock};
