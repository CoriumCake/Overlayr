//! OCR programs and models shipped inside the installer, so text reading
//! works straight after installing, with nothing else to install.
//!
//! The release builds put them in an `ocr` folder (see
//! `packaging/fetch-ocr.sh`):
//!
//! - Windows: next to overlayr.exe
//! - macOS: in Overlayr.app/Contents/Resources
//!
//! ```text
//! ocr/
//!   onnxruntime.dll | libonnxruntime.dylib
//!   PP-OCRv6_det_small.onnx
//!   PP-OCRv6_rec_small.onnx
//!   tesseract/tesseract(.exe) and its libraries
//! ```

use std::path::PathBuf;

/// The bundled `ocr` folder, if this build has one that runs on this
/// computer. The macOS bundle holds Apple Silicon programs only, so Intel
/// Macs fall back to downloads and Homebrew.
pub fn ocr_dir() -> Option<PathBuf> {
    if cfg!(target_os = "macos") && std::env::consts::ARCH != "aarch64" {
        return None;
    }
    let executable = std::env::current_exe().ok()?;
    let directory = executable.parent()?;
    #[cfg(target_os = "macos")]
    let directory = directory.parent()?.join("Resources");
    Some(directory.join("ocr")).filter(|path| path.is_dir())
}

/// The bundled Tesseract command line.
pub fn tesseract() -> Option<PathBuf> {
    let name = if cfg!(windows) {
        "tesseract.exe"
    } else {
        "tesseract"
    };
    Some(ocr_dir()?.join("tesseract").join(name)).filter(|path| path.is_file())
}
