use std::fmt;

/// A screen or window that can be selected as a capture source.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Source {
    pub id: String,
    pub name: String,
    pub kind: SourceKind,
    pub bounds: Rect,
    /// Secondary label for pickers, such as the owning application.
    pub detail: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceKind {
    Screen,
    Window,
}

/// A rectangle in physical screen pixels.
#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct Rect {
    pub left: i32,
    pub top: i32,
    pub width: u32,
    pub height: u32,
}

impl Rect {
    pub const fn new(left: i32, top: i32, width: u32, height: u32) -> Self {
        Self {
            left,
            top,
            width,
            height,
        }
    }

    pub const fn right(self) -> i32 {
        self.left + self.width as i32
    }

    pub const fn bottom(self) -> i32 {
        self.top + self.height as i32
    }

    pub const fn is_empty(self) -> bool {
        self.width == 0 || self.height == 0
    }
}

/// Captured pixels and the screen-space bounds they came from.
#[derive(Clone, Debug, PartialEq)]
pub struct Frame {
    pub sequence: u64,
    pub bounds: Rect,
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
    /// For a crop, where it sits in the capture it came from and that
    /// capture's size, in pixels; a capture is its own source. OCR scales
    /// crops like their capture, so text reads the same in both.
    pub origin: (u32, u32),
    pub source_size: (u32, u32),
}

impl Frame {
    pub fn new(sequence: u64, bounds: Rect, width: u32, height: u32, rgba: Vec<u8>) -> Self {
        Self {
            sequence,
            bounds,
            width,
            height,
            rgba,
            origin: (0, 0),
            source_size: (width, height),
        }
    }

    /// Maps screen coordinates back to image pixels; the inverse of
    /// [`Frame::to_screen`].
    pub fn to_pixels(&self, rect: Rect) -> Rect {
        let scale_x = if self.bounds.width == 0 {
            1.0
        } else {
            f64::from(self.width) / f64::from(self.bounds.width)
        };
        let scale_y = if self.bounds.height == 0 {
            1.0
        } else {
            f64::from(self.height) / f64::from(self.bounds.height)
        };
        Rect::new(
            (f64::from(rect.left - self.bounds.left) * scale_x).round() as i32,
            (f64::from(rect.top - self.bounds.top) * scale_y).round() as i32,
            (f64::from(rect.width) * scale_x).round() as u32,
            (f64::from(rect.height) * scale_y).round() as u32,
        )
    }

    /// A copy of part of the frame, given in image pixels; its bounds are
    /// the matching screen area, so OCR results map back to the screen.
    pub fn crop(&self, area: Rect) -> Frame {
        let left = area.left.clamp(0, self.width as i32) as u32;
        let top = area.top.clamp(0, self.height as i32) as u32;
        let right = (area.right().max(0) as u32).min(self.width);
        let bottom = (area.bottom().max(0) as u32).min(self.height);
        let (width, height) = (right.saturating_sub(left), bottom.saturating_sub(top));
        let mut rgba = Vec::with_capacity((width * height * 4) as usize);
        for y in top..bottom {
            let start = ((y * self.width + left) * 4) as usize;
            rgba.extend_from_slice(&self.rgba[start..start + (width * 4) as usize]);
        }
        let mut crop = Frame::new(
            self.sequence,
            self.to_screen(left as i32, top as i32, width, height),
            width,
            height,
            rgba,
        );
        crop.origin = (self.origin.0 + left, self.origin.1 + top);
        crop.source_size = self.source_size;
        crop
    }

    /// Paints an area (in image pixels) with a solid colour.
    pub fn fill(&mut self, area: Rect, color: [u8; 4]) {
        let left = area.left.clamp(0, self.width as i32) as u32;
        let top = area.top.clamp(0, self.height as i32) as u32;
        let right = (area.right().max(0) as u32).min(self.width);
        let bottom = (area.bottom().max(0) as u32).min(self.height);
        for y in top..bottom {
            for x in left..right {
                let offset = ((y * self.width + x) * 4) as usize;
                if let Some(pixel) = self.rgba.get_mut(offset..offset + 4) {
                    pixel.copy_from_slice(&color);
                }
            }
        }
    }

    /// The pixel at image coordinates, clamped to the frame.
    pub fn pixel(&self, x: i32, y: i32) -> [u8; 4] {
        if self.width == 0 || self.height == 0 {
            return [0; 4];
        }
        let x = x.clamp(0, self.width as i32 - 1) as usize;
        let y = y.clamp(0, self.height as i32 - 1) as usize;
        let offset = (y * self.width as usize + x) * 4;
        self.rgba
            .get(offset..offset + 4)
            .and_then(|pixel| pixel.try_into().ok())
            .unwrap_or([0; 4])
    }

    /// Maps a rectangle in image pixels to screen coordinates. On HiDPI
    /// outputs the image can have more pixels than the screen area it covers.
    pub fn to_screen(&self, left: i32, top: i32, width: u32, height: u32) -> Rect {
        let scale_x = if self.width == 0 {
            1.0
        } else {
            f64::from(self.bounds.width) / f64::from(self.width)
        };
        let scale_y = if self.height == 0 {
            1.0
        } else {
            f64::from(self.bounds.height) / f64::from(self.height)
        };
        Rect::new(
            self.bounds.left + (f64::from(left) * scale_x).round() as i32,
            self.bounds.top + (f64::from(top) * scale_y).round() as i32,
            (f64::from(width) * scale_x).round() as u32,
            (f64::from(height) * scale_y).round() as u32,
        )
    }
}

/// Text OCR found but discarded, kept for debug mode.
#[derive(Clone, Debug, PartialEq)]
pub struct RejectedText {
    pub text: String,
    pub bounds: Rect,
    pub confidence: f32,
    pub reason: &'static str,
}

/// Source/target languages are part of every translation identity.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct LanguagePair {
    pub source: String,
    pub target: String,
}

impl LanguagePair {
    pub fn new(source: impl Into<String>, target: impl Into<String>) -> Self {
        Self {
            source: source.into(),
            target: target.into(),
        }
    }
}

impl fmt::Display for LanguagePair {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} -> {}", self.source, self.target)
    }
}

/// OCR output that can be passed to translation and rendering adapters.
#[derive(Clone, Debug, PartialEq)]
pub struct TextBlock {
    pub bounds: Rect,
    pub source_text: String,
    pub translated_text: Option<String>,
    pub confidence: f32,
    /// How many lines of text the block was read from; 0 when unknown.
    pub lines: u32,
    /// Styling from a translation pack; `None` for machine translations.
    pub style: Option<crate::pack::TextStyle>,
    /// Background colours around the original text (top-left, top-right,
    /// bottom-right, bottom-left), used to blend the translation in.
    pub background: Option<[[u8; 4]; 4]>,
    /// The original text's colour, when it stands out from the background,
    /// so translations can keep it (a yellow heading stays yellow).
    pub foreground: Option<[u8; 4]>,
    /// The background behind the text, rebuilt from the pixels around it,
    /// so the translation covers the original seamlessly.
    pub backdrop: Option<std::sync::Arc<Backdrop>>,
}

/// An image of what is likely behind a text: the pixels around the text's
/// box blended inwards. It covers the text's bounds plus
/// [`Backdrop::PADDING`] on every side, in screen units.
pub struct Backdrop {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
    /// GPU texture, created the first time it is drawn.
    pub texture: std::sync::OnceLock<eframe::egui::TextureHandle>,
}

impl Backdrop {
    /// Padding around the text, matching the overlay's own.
    pub const PADDING: i32 = 4;
}

impl fmt::Debug for Backdrop {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Backdrop({}x{})", self.width, self.height)
    }
}

impl PartialEq for Backdrop {
    fn eq(&self, other: &Self) -> bool {
        self.width == other.width && self.height == other.height && self.rgba == other.rgba
    }
}

impl TextBlock {
    pub fn new(bounds: Rect, source_text: impl Into<String>, confidence: f32) -> Self {
        Self {
            bounds,
            source_text: source_text.into(),
            translated_text: None,
            confidence,
            lines: 0,
            style: None,
            background: None,
            foreground: None,
            backdrop: None,
        }
    }
}
