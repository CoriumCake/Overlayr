use std::fmt;

/// A screen or window that can be selected as a capture source.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Source {
    pub id: String,
    pub name: String,
    pub kind: SourceKind,
    pub bounds: Rect,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceKind {
    Screen,
    Window,
}

/// A rectangle in physical screen pixels.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
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
}

impl Frame {
    pub fn new(sequence: u64, bounds: Rect, width: u32, height: u32, rgba: Vec<u8>) -> Self {
        Self {
            sequence,
            bounds,
            width,
            height,
            rgba,
        }
    }
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
}

impl TextBlock {
    pub fn new(bounds: Rect, source_text: impl Into<String>, confidence: f32) -> Self {
        Self {
            bounds,
            source_text: source_text.into(),
            translated_text: None,
            confidence,
        }
    }
}
