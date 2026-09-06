use std::process::Command;

use crate::services::{CaptureBackend, ServiceResult};
use crate::types::{Frame, Rect, Source, SourceKind};

fn decode_frame(sequence: u64, bounds: Rect, bytes: &[u8]) -> ServiceResult<Frame> {
    let image = image::ImageReader::new(std::io::Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|error| format!("invalid screenshot format: {error}"))?
        .decode()
        .map_err(|error| format!("could not decode screenshot: {error}"))?
        .to_rgba8();
    let (width, height) = image.dimensions();
    Ok(frame_from_raw(
        sequence,
        bounds,
        width,
        height,
        image.into_raw(),
    ))
}

fn frame_from_raw(sequence: u64, bounds: Rect, width: u32, height: u32, rgba: Vec<u8>) -> Frame {
    Frame::new(
        sequence,
        Rect::new(bounds.left, bounds.top, width, height),
        width,
        height,
        rgba,
    )
}

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

        self.sequence = self.sequence.saturating_add(1);
        decode_frame(self.sequence, source.bounds, &output.stdout)
    }
}

/// Capture backend for X11 sessions using ImageMagick's `import` utility.
/// It is a dependency-light fallback while a native XShm adapter is added.
#[derive(Debug)]
pub struct ImageMagickCapture {
    command: String,
    sequence: u64,
}

impl Default for ImageMagickCapture {
    fn default() -> Self {
        Self {
            command: "import".into(),
            sequence: 0,
        }
    }
}

impl CaptureBackend for ImageMagickCapture {
    fn sources(&mut self) -> ServiceResult<Vec<Source>> {
        if std::env::var_os("DISPLAY").is_none() {
            return Err("X11 is not active; ImageMagick capture is unavailable".into());
        }
        Ok(vec![Source {
            id: "x11-desktop".into(),
            name: "X11 desktop".into(),
            kind: SourceKind::Screen,
            bounds: Rect::default(),
        }])
    }

    fn capture(&mut self, source: &Source) -> ServiceResult<Frame> {
        let mut command = Command::new(&self.command);
        command.arg("-window").arg("root");
        if !source.bounds.is_empty() {
            command.arg("-crop").arg(format!(
                "{}x{}+{}+{}",
                source.bounds.width, source.bounds.height, source.bounds.left, source.bounds.top
            ));
        }
        let output = command
            .arg("png:-")
            .output()
            .map_err(|error| format!("failed to start {}: {error}", self.command))?;
        if !output.status.success() {
            return Err(format!(
                "{} failed: {}",
                self.command,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        self.sequence = self.sequence.saturating_add(1);
        decode_frame(self.sequence, source.bounds, &output.stdout)
    }
}

/// Native cross-platform capture using XCap. Unlike the command adapters
/// above, this keeps capture in-process and exposes both monitors and windows
/// on Linux, Windows, and macOS.
#[cfg(feature = "native-capture")]
#[derive(Debug, Default)]
pub struct XCapCapture {
    sequence: u64,
}

#[cfg(feature = "native-capture")]
impl XCapCapture {
    pub fn select_region(&self) -> ServiceResult<Source> {
        if std::env::var_os("WAYLAND_DISPLAY").is_none() {
            return Err("interactive region selection currently requires Wayland and slurp".into());
        }
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

    fn monitor_source(monitor: &xcap::Monitor) -> ServiceResult<Source> {
        let id = monitor.id().map_err(|error| error.to_string())?;
        let bounds = Rect::new(
            monitor.x().map_err(|error| error.to_string())?,
            monitor.y().map_err(|error| error.to_string())?,
            monitor.width().map_err(|error| error.to_string())?,
            monitor.height().map_err(|error| error.to_string())?,
        );
        let name = monitor
            .friendly_name()
            .or_else(|_| monitor.name())
            .map_err(|error| error.to_string())?;
        Ok(Source {
            id: format!("monitor:{id}"),
            name,
            kind: SourceKind::Screen,
            bounds,
        })
    }

    fn window_source(window: &xcap::Window) -> ServiceResult<Source> {
        let id = window.id().map_err(|error| error.to_string())?;
        let title = window.title().map_err(|error| error.to_string())?;
        let app_name = window.app_name().unwrap_or_default();
        let name = if app_name.is_empty() {
            title.clone()
        } else {
            format!("{app_name}: {title}")
        };
        Ok(Source {
            id: format!("window:{id}"),
            name,
            kind: SourceKind::Window,
            bounds: Rect::new(
                window.x().map_err(|error| error.to_string())?,
                window.y().map_err(|error| error.to_string())?,
                window.width().map_err(|error| error.to_string())?,
                window.height().map_err(|error| error.to_string())?,
            ),
        })
    }
}

#[cfg(feature = "native-capture")]
impl CaptureBackend for XCapCapture {
    fn sources(&mut self) -> ServiceResult<Vec<Source>> {
        let mut sources = xcap::Monitor::all()
            .map_err(|error| error.to_string())?
            .iter()
            .filter_map(|monitor| Self::monitor_source(monitor).ok())
            .collect::<Vec<_>>();

        // Window enumeration is optional on some compositors. Monitors should
        // remain usable when a window portal is unavailable.
        if let Ok(windows) = xcap::Window::all() {
            sources.extend(windows.iter().filter_map(|window| {
                let source = Self::window_source(window).ok()?;
                let is_overlayr = source.name.to_ascii_lowercase().contains("overlayr");
                if is_overlayr || source.name.trim().is_empty() || source.bounds.is_empty() {
                    None
                } else {
                    Some(source)
                }
            }));
        }
        if sources.is_empty() {
            return Err("no monitors or windows are available for capture".into());
        }
        Ok(sources)
    }

    fn capture(&mut self, source: &Source) -> ServiceResult<Frame> {
        let image = if let Some(id) = source.id.strip_prefix("monitor:") {
            let id = id.parse::<u32>().map_err(|_| "invalid monitor id")?;
            let monitor = xcap::Monitor::all()
                .map_err(|error| error.to_string())?
                .into_iter()
                .find(|monitor| monitor.id().ok() == Some(id))
                .ok_or_else(|| "selected monitor is no longer available".to_string())?;
            monitor.capture_image().map_err(|error| error.to_string())?
        } else if let Some(id) = source.id.strip_prefix("window:") {
            let id = id.parse::<u32>().map_err(|_| "invalid window id")?;
            let window = xcap::Window::all()
                .map_err(|error| error.to_string())?
                .into_iter()
                .find(|window| window.id().ok() == Some(id))
                .ok_or_else(|| "selected window is no longer available".to_string())?;
            window.capture_image().map_err(|error| error.to_string())?
        } else if source.id.starts_with("region-") {
            let monitor = xcap::Monitor::from_point(source.bounds.left, source.bounds.top)
                .map_err(|error| error.to_string())?;
            let monitor_x = monitor.x().map_err(|error| error.to_string())?;
            let monitor_y = monitor.y().map_err(|error| error.to_string())?;
            let (x, y) = if std::env::var_os("WAYLAND_DISPLAY").is_some() {
                (source.bounds.left, source.bounds.top)
            } else {
                (
                    source.bounds.left - monitor_x,
                    source.bounds.top - monitor_y,
                )
            };
            monitor
                .capture_region(
                    x.max(0) as u32,
                    y.max(0) as u32,
                    source.bounds.width,
                    source.bounds.height,
                )
                .map_err(|error| error.to_string())?
        } else {
            return Err("unknown capture source".into());
        };
        self.sequence = self.sequence.saturating_add(1);
        Ok(frame_from_raw(
            self.sequence,
            source.bounds,
            image.width(),
            image.height(),
            image.into_raw(),
        ))
    }
}

/// Selects the capture implementation from the active desktop session.
#[derive(Debug)]
pub enum PlatformCapture {
    #[cfg(feature = "native-capture")]
    Native(XCapCapture),
    Wayland(GrimCapture),
    X11(ImageMagickCapture),
}

impl Default for PlatformCapture {
    fn default() -> Self {
        #[cfg(feature = "native-capture")]
        {
            return Self::Native(XCapCapture::default());
        }
        #[cfg(not(feature = "native-capture"))]
        {
            if std::env::var_os("WAYLAND_DISPLAY").is_some() {
                Self::Wayland(GrimCapture::default())
            } else {
                Self::X11(ImageMagickCapture::default())
            }
        }
    }
}

impl PlatformCapture {
    pub fn select_region(&self) -> ServiceResult<Source> {
        match self {
            #[cfg(feature = "native-capture")]
            Self::Native(capture) => capture.select_region(),
            Self::Wayland(capture) => capture.select_region(),
            Self::X11(_) => {
                Err("X11 region selection is not configured; select a full desktop source".into())
            }
        }
    }
}

impl CaptureBackend for PlatformCapture {
    fn sources(&mut self) -> ServiceResult<Vec<Source>> {
        match self {
            #[cfg(feature = "native-capture")]
            Self::Native(capture) => capture.sources(),
            Self::Wayland(capture) => capture.sources(),
            Self::X11(capture) => capture.sources(),
        }
    }

    fn capture(&mut self, source: &Source) -> ServiceResult<Frame> {
        match self {
            #[cfg(feature = "native-capture")]
            Self::Native(capture) => capture.capture(source),
            Self::Wayland(capture) => capture.capture(source),
            Self::X11(capture) => capture.capture(source),
        }
    }
}
