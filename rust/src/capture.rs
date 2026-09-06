use std::process::Command;

use image::ImageReader;

use crate::services::{CaptureBackend, ServiceResult};
use crate::types::{Frame, Rect, Source, SourceKind};

/// Capture backend for Wayland compositors that expose the `grim` utility.
///
/// `grim` is intentionally kept behind this small adapter. That means the
/// rest of the pipeline does not depend on a compositor-specific protocol and
/// can later use native PipeWire/X11/Windows backends without changes.
#[derive(Debug)]
pub struct GrimCapture {
    command: String,
    sequence: u64,
}

impl Default for GrimCapture {
    fn default() -> Self {
        Self {
            command: "grim".to_string(),
            sequence: 0,
        }
    }
}

impl GrimCapture {
    pub fn new(command: impl Into<String>) -> Self {
        Self {
            command: command.into(),
            ..Self::default()
        }
    }

    /// Ask the compositor's region picker for a rectangle. This is the
    /// Linux/Wayland equivalent of selecting a window or screen area in the
    /// legacy desktop UI.
    pub fn select_region(&self) -> ServiceResult<Source> {
        let output = Command::new("slurp")
            .args(["-f", "%x,%y %wx%h"])
            .output()
            .map_err(|error| format!("failed to start slurp: {error}"))?;
        if !output.status.success() {
            return Err("region selection was cancelled".into());
        }
        let selection = String::from_utf8_lossy(&output.stdout);
        let (position, size) = selection
            .trim()
            .split_once(' ')
            .ok_or_else(|| "slurp returned an invalid region".to_string())?;
        let (left, top) = position
            .split_once(',')
            .ok_or_else(|| "slurp returned an invalid position".to_string())?;
        let (width, height) = size
            .split_once('x')
            .ok_or_else(|| "slurp returned an invalid size".to_string())?;
        let bounds = Rect::new(
            left.parse().map_err(|_| "invalid region x coordinate")?,
            top.parse().map_err(|_| "invalid region y coordinate")?,
            width.parse().map_err(|_| "invalid region width")?,
            height.parse().map_err(|_| "invalid region height")?,
        );
        Ok(Source {
            id: format!(
                "region-{}-{}-{}-{}",
                bounds.left, bounds.top, bounds.width, bounds.height
            ),
            name: format!("Selected region ({}×{})", bounds.width, bounds.height),
            kind: SourceKind::Screen,
            bounds,
        })
    }
}

impl CaptureBackend for GrimCapture {
    fn sources(&mut self) -> ServiceResult<Vec<Source>> {
        if std::env::var_os("WAYLAND_DISPLAY").is_none() {
            return Err("Wayland is not active; Grim capture is unavailable".into());
        }

        Ok(vec![Source {
            id: "wayland-desktop".into(),
            name: "Wayland desktop".into(),
            kind: SourceKind::Screen,
            // Grim returns the compositor's full logical output. The exact
            // dimensions are filled in by the first capture.
            bounds: Rect::default(),
        }])
    }

    fn capture(&mut self, source: &Source) -> ServiceResult<Frame> {
        if source.kind != SourceKind::Screen {
            return Err("Grim currently supports screen sources only".into());
        }

        let mut command = Command::new(&self.command);
        if !source.bounds.is_empty() {
            command.arg("-g").arg(format!(
                "{},{} {}x{}",
                source.bounds.left, source.bounds.top, source.bounds.width, source.bounds.height
            ));
        }
        let output = command
            .arg("-")
            .output()
            .map_err(|error| format!("failed to start {}: {error}", self.command))?;
        if !output.status.success() {
            return Err(format!(
                "{} failed: {}",
                self.command,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }

        let image = ImageReader::new(std::io::Cursor::new(output.stdout))
            .with_guessed_format()
            .map_err(|error| format!("invalid screenshot format: {error}"))?
            .decode()
            .map_err(|error| format!("could not decode screenshot: {error}"))?
            .to_rgba8();
        let (width, height) = image.dimensions();
        self.sequence = self.sequence.saturating_add(1);

        Ok(Frame::new(
            self.sequence,
            Rect::new(0, 0, width, height),
            width,
            height,
            image.into_raw(),
        ))
    }
}
