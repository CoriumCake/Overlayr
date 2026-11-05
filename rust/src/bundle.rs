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
//!   x86_64/tesseract/...   (macOS: Tesseract for Intel Macs)
//! ```
//!
//! The macOS app runs on Apple Silicon and Intel, so its programs for Intel
//! Macs sit in `x86_64`. There is no ONNX Runtime for Intel Macs, so they
//! read text with Tesseract only.

use std::path::PathBuf;

/// The AppImage Overlayr runs from, if it runs from one. Its programs run
/// from a temporary mount, so anything that starts Overlayr again (the
/// `overlayr://` handler, keybinds, updates) uses this file instead.
pub fn appimage() -> Option<PathBuf> {
    if !cfg!(target_os = "linux") {
        return None;
    }
    let path = PathBuf::from(std::env::var_os("APPIMAGE")?);
    // Set by the AppImage runtime; a program started from inside one (a
    // terminal opened by it) inherits it, so check it's the one running us.
    let mount = PathBuf::from(std::env::var_os("APPDIR")?);
    let executable = std::env::current_exe().ok()?;
    (executable.starts_with(&mount) && path.is_file()).then_some(path)
}

/// The program to run to start Overlayr: the AppImage when there is one.
pub fn program() -> std::io::Result<PathBuf> {
    appimage().map_or_else(std::env::current_exe, Ok)
}

/// Whether this is an Intel Mac, which gets its own programs.
fn intel_mac() -> bool {
    cfg!(target_os = "macos") && std::env::consts::ARCH == "x86_64"
}

/// The bundled `ocr` folder, which holds the models, if this build has one.
pub fn ocr_dir() -> Option<PathBuf> {
    let executable = std::env::current_exe().ok()?;
    let directory = executable.parent()?;
    #[cfg(target_os = "macos")]
    let directory = directory.parent()?.join("Resources");
    Some(directory.join("ocr")).filter(|path| path.is_dir())
}

/// The bundled folder with programs and libraries for this computer.
pub fn program_dir() -> Option<PathBuf> {
    let directory = ocr_dir()?;
    if intel_mac() {
        return Some(directory.join("x86_64")).filter(|path| path.is_dir());
    }
    Some(directory)
}

/// The bundled Tesseract command line.
pub fn tesseract() -> Option<PathBuf> {
    let name = if cfg!(windows) {
        "tesseract.exe"
    } else {
        "tesseract"
    };
    Some(program_dir()?.join("tesseract").join(name)).filter(|path| path.is_file())
}
