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
    if bounds.is_empty() {
        return Ok(frame_from_raw(
            sequence,
            bounds,
            width,
            height,
            image.into_raw(),
        ));
    }
    // Keep the requested screen-space bounds: grim captures at the highest
    // output scale, so the image may be larger than the area it covers.
    Ok(Frame::new(
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

fn source_from_region_selection(selection: &[u8]) -> ServiceResult<Source> {
    let text = String::from_utf8_lossy(selection);
    let mut fields = text.split_whitespace();
    let position = fields
        .next()
        .ok_or_else(|| "slurp returned an empty region".to_string())?;
    let size = fields
        .next()
        .ok_or_else(|| "slurp returned an invalid region size".to_string())?;
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
    if bounds.is_empty() {
        return Err("slurp returned an empty region".into());
    }
    Ok(Source {
        id: format!(
            "region-{}-{}-{}-{}",
            bounds.left, bounds.top, bounds.width, bounds.height
        ),
        name: format!("Selected region ({}×{})", bounds.width, bounds.height),
        kind: SourceKind::Screen,
        bounds,
        detail: "Screen region".into(),
    })
}

const HYPR_WINDOW_PREFIX: &str = "hypr-window:";

/// The Hyprland window address of a window source.
pub fn hyprland_address(source: &Source) -> Option<&str> {
    source.id.strip_prefix(HYPR_WINDOW_PREFIX)
}
const HYPR_MONITOR_PREFIX: &str = "hypr-monitor:";

/// Lists Hyprland windows (most recently focused first) followed by monitors.
#[cfg(unix)]
fn hyprland_sources() -> ServiceResult<Vec<Source>> {
    let monitors = crate::hyprland::monitors()?;
    let mut clients = crate::hyprland::clients()?;
    clients.sort_by_key(|client| (client.focus_history_id < 0, client.focus_history_id));
    let own_pid = i64::from(std::process::id());
    let mut sources = clients
        .iter()
        .filter(|client| {
            client.mapped
                && client.pid != own_pid
                && !client.title.trim().is_empty()
                && !client.bounds().is_empty()
        })
        .map(|client| Source {
            id: format!("{HYPR_WINDOW_PREFIX}{}", client.address),
            name: client.title.clone(),
            kind: SourceKind::Window,
            bounds: client.bounds(),
            detail: if client.class.is_empty() {
                format!("Workspace {}", client.workspace.name)
            } else {
                client.class.clone()
            },
        })
        .collect::<Vec<_>>();
    sources.extend(monitors.iter().map(|monitor| Source {
        id: format!("{HYPR_MONITOR_PREFIX}{}", monitor.name),
        name: monitor.name.clone(),
        kind: SourceKind::Screen,
        bounds: monitor.logical_bounds(),
        detail: monitor.description.clone(),
    }));
    Ok(sources)
}

struct WindowState {
    bounds: Rect,
    visible: bool,
    /// The window, or Overlayr itself, has focus.
    focused: bool,
    full_size: bool,
    /// Identifier for capturing the window's own contents with `grim -T`.
    toplevel: String,
}

/// Current state of a Hyprland window source, or `None` for sources that are
/// not Hyprland windows. Looked up on every call so capture follows the
/// window when it moves or resizes.
fn hyprland_window_state(source: &Source) -> Option<ServiceResult<WindowState>> {
    let address = source.id.strip_prefix(HYPR_WINDOW_PREFIX)?;
    #[cfg(unix)]
    {
        Some((|| {
            let client = crate::hyprland::clients()?
                .into_iter()
                .find(|client| client.address == address)
                .ok_or_else(|| "the selected window was closed".to_string())?;
            let monitors = crate::hyprland::monitors()?;
            Ok(WindowState {
                bounds: client.bounds(),
                visible: client.is_visible(&monitors),
                focused: crate::hyprland::is_focused(address).unwrap_or(true),
                full_size: client.is_full_size(&monitors),
                toplevel: client.stable_id,
            })
        })())
    }
    #[cfg(not(unix))]
    {
        let _ = address;
        Some(Err("Hyprland windows are only available on Linux".into()))
    }
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
    supports_toplevel: Option<bool>,
}

impl Default for GrimCapture {
    fn default() -> Self {
        Self {
            command: "grim".to_string(),
            sequence: 0,
            supports_toplevel: None,
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
        source_from_region_selection(&output.stdout)
    }
}

impl GrimCapture {
    /// grim 1.5 added `-T`, which captures a window's own buffer. Unlike a
    /// screen region, that excludes Overlayr's overlay on top of the window
    /// and works while the window is covered or on another workspace.
    fn supports_toplevel(&mut self) -> bool {
        *self.supports_toplevel.get_or_insert_with(|| {
            Command::new(&self.command)
                .arg("-h")
                .output()
                .is_ok_and(|output| {
                    String::from_utf8_lossy(&output.stdout).contains("-T ")
                        || String::from_utf8_lossy(&output.stderr).contains("-T ")
                })
        })
    }

    fn grab(&mut self, source: &Source, scale: Option<f32>) -> ServiceResult<Frame> {
        let window = hyprland_window_state(source).transpose()?;
        let bounds = window
            .as_ref()
            .map_or(source.bounds, |window| window.bounds);
        let mut command = Command::new(&self.command);
        // Raw PPM: PNG takes grim ~400 ms to compress on a 1440p frame, and
        // even uncompressed PNG costs time to write and decode again.
        command.args(["-t", "ppm"]);
        if let Some(scale) = scale {
            // Thumbnails also downscale in the compositor.
            command.args(["-s", &format!("{scale:.3}")]);
        }
        if let Some(window) = &window
            && !window.toplevel.is_empty()
            && self.supports_toplevel()
        {
            command.args(["-T", &window.toplevel]);
        } else if let Some(output) = source.id.strip_prefix(HYPR_MONITOR_PREFIX) {
            command.args(["-o", output]);
        } else if !bounds.is_empty() {
            command.arg("-g").arg(format!(
                "{},{} {}x{}",
                bounds.left, bounds.top, bounds.width, bounds.height
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
        decode_frame(self.sequence, bounds, &output.stdout)
    }
}

impl CaptureBackend for GrimCapture {
    fn sources(&mut self) -> ServiceResult<Vec<Source>> {
        if std::env::var_os("WAYLAND_DISPLAY").is_none() {
            return Err("Wayland is not active; Grim capture is unavailable".into());
        }

        #[cfg(unix)]
        if crate::hyprland::is_available()
            && let Ok(sources) = hyprland_sources()
            && !sources.is_empty()
        {
            return Ok(sources);
        }

        Ok(vec![Source {
            id: "wayland-desktop".into(),
            name: "Wayland desktop".into(),
            kind: SourceKind::Screen,
            // Grim returns the compositor's full logical output. The exact
            // dimensions are filled in by the first capture.
            bounds: Rect::default(),
            detail: "Entire desktop".into(),
        }])
    }

    fn capture(&mut self, source: &Source) -> ServiceResult<Frame> {
        self.grab(source, None)
    }

    fn source_is_active(&mut self, source: &Source) -> ServiceResult<bool> {
        match hyprland_window_state(source) {
            // Translations hide while another app has focus, even one in
            // front of the game on the same workspace.
            Some(state) => state.map(|window| window.visible && window.focused),
            None => Ok(true),
        }
    }

    fn source_is_full_size(&mut self, source: &Source) -> ServiceResult<bool> {
        match hyprland_window_state(source) {
            Some(state) => state.map(|window| window.full_size),
            None => Ok(true),
        }
    }

    fn can_capture_hidden(&mut self, source: &Source) -> bool {
        // `grim -T` reads the window's own buffer, wherever it is.
        source.id.starts_with(HYPR_WINDOW_PREFIX) && self.supports_toplevel()
    }

    fn thumbnail(&mut self, source: &Source, max_width: u32) -> ServiceResult<Frame> {
        let width = match hyprland_window_state(source) {
            Some(state) => {
                let window = state?;
                // Without toplevel capture, the pixels at a hidden window's
                // position belong to whatever is on screen now.
                if !window.visible && !(self.supports_toplevel() && !window.toplevel.is_empty()) {
                    return Err("window is not visible on screen".into());
                }
                window.bounds.width
            }
            None => source.bounds.width,
        };
        let scale = if width == 0 {
            0.15
        } else {
            (max_width as f32 / width as f32).min(1.0)
        };
        self.grab(source, Some(scale))
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
            detail: "Entire desktop".into(),
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
/// How many of xcap's coordinate units make one screen point, the unit the
/// overlay window is placed in. macOS already reports points; Windows reports
/// pixels, so 150% display scaling has 1.5 units per point. Captured images
/// are always in pixels.
#[cfg(feature = "native-capture")]
fn units_per_point(monitor: Option<&xcap::Monitor>) -> f32 {
    #[cfg(windows)]
    {
        monitor
            .and_then(|monitor| monitor.scale_factor().ok())
            .filter(|scale| *scale > 0.0)
            .unwrap_or(1.0)
    }
    #[cfg(not(windows))]
    {
        let _ = monitor;
        1.0
    }
}

/// An area xcap reports, in screen points.
#[cfg(feature = "native-capture")]
fn to_points(x: i32, y: i32, width: u32, height: u32, units_per_point: f32) -> Rect {
    let point = |value: f32| (value / units_per_point).round();
    Rect::new(
        point(x as f32) as i32,
        point(y as f32) as i32,
        point(width as f32) as u32,
        point(height as f32) as u32,
    )
}

#[cfg(feature = "native-capture")]
#[derive(Debug, Default)]
pub struct XCapCapture {
    sequence: u64,
}

#[cfg(feature = "native-capture")]
impl XCapCapture {
    pub fn select_region(&self) -> ServiceResult<Source> {
        if cfg!(any(target_os = "macos", windows)) {
            return Err(
                "Selecting a region isn't available here yet; choose a window or screen".into(),
            );
        }
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
        source_from_region_selection(&output.stdout)
    }

    fn monitor_source(monitor: &xcap::Monitor) -> ServiceResult<Source> {
        let id = monitor.id().map_err(|error| error.to_string())?;
        let bounds = Self::monitor_bounds(monitor)?;
        let name = monitor
            .friendly_name()
            .or_else(|_| monitor.name())
            .map_err(|error| error.to_string())?;
        Ok(Source {
            id: format!("monitor:{id}"),
            name,
            kind: SourceKind::Screen,
            bounds,
            detail: "Screen".into(),
        })
    }

    fn monitor_bounds(monitor: &xcap::Monitor) -> ServiceResult<Rect> {
        Ok(to_points(
            monitor.x().map_err(|error| error.to_string())?,
            monitor.y().map_err(|error| error.to_string())?,
            monitor.width().map_err(|error| error.to_string())?,
            monitor.height().map_err(|error| error.to_string())?,
            units_per_point(Some(monitor)),
        ))
    }

    /// Where the window is now, in screen points.
    fn window_bounds(window: &xcap::Window) -> ServiceResult<Rect> {
        let monitor = window.current_monitor().ok();
        Ok(to_points(
            window.x().map_err(|error| error.to_string())?,
            window.y().map_err(|error| error.to_string())?,
            window.width().map_err(|error| error.to_string())?,
            window.height().map_err(|error| error.to_string())?,
            units_per_point(monitor.as_ref()),
        ))
    }

    fn window_source(window: &xcap::Window) -> ServiceResult<Source> {
        let id = window.id().map_err(|error| error.to_string())?;
        let title = window.title().map_err(|error| error.to_string())?;
        let app_name = window.app_name().unwrap_or_default();
        Ok(Source {
            id: format!("window:{id}"),
            name: title,
            detail: app_name,
            kind: SourceKind::Window,
            bounds: Self::window_bounds(window)?,
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
                let is_overlayr = source.name.to_ascii_lowercase().contains("overlayr")
                    || source.detail.to_ascii_lowercase().contains("overlayr");
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
        // The captured area in screen points; the image is in pixels, which
        // differ on Retina and scaled displays.
        let mut bounds = source.bounds;
        let image = if let Some(id) = source.id.strip_prefix("monitor:") {
            let id = id.parse::<u32>().map_err(|_| "invalid monitor id")?;
            let monitor = xcap::Monitor::all()
                .map_err(|error| error.to_string())?
                .into_iter()
                .find(|monitor| monitor.id().ok() == Some(id))
                .ok_or_else(|| "selected monitor is no longer available".to_string())?;
            bounds = Self::monitor_bounds(&monitor)?;
            monitor.capture_image().map_err(|error| error.to_string())?
        } else if let Some(id) = source.id.strip_prefix("window:") {
            let id = id.parse::<u32>().map_err(|_| "invalid window id")?;
            let window = xcap::Window::all()
                .map_err(|error| error.to_string())?
                .into_iter()
                .find(|window| window.id().ok() == Some(id))
                .ok_or_else(|| "selected window is no longer available".to_string())?;
            bounds = Self::window_bounds(&window)?;
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
        let (width, height) = image.dimensions();
        if cfg!(any(target_os = "macos", windows)) {
            return Ok(Frame::new(
                self.sequence,
                bounds,
                width,
                height,
                image.into_raw(),
            ));
        }
        Ok(frame_from_raw(
            self.sequence,
            source.bounds,
            width,
            height,
            image.into_raw(),
        ))
    }

    fn source_is_active(&mut self, source: &Source) -> ServiceResult<bool> {
        let Some(id) = source.id.strip_prefix("window:") else {
            return Ok(true);
        };
        let id = id.parse::<u32>().map_err(|_| "invalid window id")?;
        let window = xcap::Window::all()
            .map_err(|error| error.to_string())?
            .into_iter()
            .find(|window| window.id().ok() == Some(id))
            .ok_or_else(|| "selected window is no longer available".to_string())?;
        // Shown unless minimised: switching to another application must not
        // stop translation.
        window
            .is_minimized()
            .map(|minimized| !minimized)
            .map_err(|error| error.to_string())
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
            Self::Native(XCapCapture::default())
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

    fn source_is_active(&mut self, source: &Source) -> ServiceResult<bool> {
        match self {
            #[cfg(feature = "native-capture")]
            Self::Native(capture) => capture.source_is_active(source),
            Self::Wayland(capture) => capture.source_is_active(source),
            Self::X11(capture) => capture.source_is_active(source),
        }
    }

    fn source_is_full_size(&mut self, source: &Source) -> ServiceResult<bool> {
        match self {
            #[cfg(feature = "native-capture")]
            Self::Native(capture) => capture.source_is_full_size(source),
            Self::Wayland(capture) => capture.source_is_full_size(source),
            Self::X11(capture) => capture.source_is_full_size(source),
        }
    }

    fn can_capture_hidden(&mut self, source: &Source) -> bool {
        match self {
            #[cfg(feature = "native-capture")]
            Self::Native(capture) => capture.can_capture_hidden(source),
            Self::Wayland(capture) => capture.can_capture_hidden(source),
            Self::X11(capture) => capture.can_capture_hidden(source),
        }
    }

    fn thumbnail(&mut self, source: &Source, max_width: u32) -> ServiceResult<Frame> {
        match self {
            #[cfg(feature = "native-capture")]
            Self::Native(capture) => capture.thumbnail(source, max_width),
            Self::Wayland(capture) => capture.thumbnail(source, max_width),
            Self::X11(capture) => capture.thumbnail(source, max_width),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(feature = "native-capture")]
    #[test]
    fn scaled_display_areas_are_converted_to_points() {
        // A window at (300, 150) sized 1920x1080 pixels on a 150% display.
        assert_eq!(
            to_points(300, 150, 1920, 1080, 1.5),
            Rect::new(200, 100, 1280, 720)
        );
        assert_eq!(
            to_points(-50, 0, 800, 600, 1.0),
            Rect::new(-50, 0, 800, 600)
        );
    }

    #[test]
    fn parses_slurp_output_with_whitespace_and_negative_coordinates() {
        let source = source_from_region_selection(b"\n -120,45   800x600\n")
            .expect("region output should parse");
        assert_eq!(source.bounds, Rect::new(-120, 45, 800, 600));
        assert_eq!(source.id, "region--120-45-800-600");
    }

    #[test]
    fn rejects_malformed_slurp_output() {
        assert!(source_from_region_selection(b"100,200").is_err());
        assert!(source_from_region_selection(b"100x200 400x300").is_err());
        assert!(source_from_region_selection(b"100,200 0x300").is_err());
    }
}
