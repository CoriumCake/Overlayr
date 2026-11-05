//! Translation packs: handmade translations for one application.
//!
//! A pack is a list of entries, each pairing a source text with its
//! translation and style. During playback the tracker matches on-screen text
//! to entries by similarity, so an entry applies wherever and whenever its
//! text appears; text no entry covers is left in the original language.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;

use crate::tracker::{AnchorTarget, Translated, normalize, text_similarity};
use crate::types::{Frame, Rect};

/// File extension for shareable pack files.
pub const PACK_EXTENSION: &str = "overlayr-pack";
const FORMAT_VERSION: u32 = 1;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct Pack {
    pub format: u32,
    pub id: String,
    pub name: String,
    pub author: String,
    /// The application the pack was made for, as shown in the source picker.
    pub application: String,
    pub source_lang: String,
    pub target_lang: String,
    /// Default text similarity (0–1) for entries without their own threshold.
    pub match_threshold: f32,
    pub entries: Vec<PackEntry>,
    /// The creator's notes for AI translation: what the game is and how to
    /// translate it (tone, names to keep). Sent with every AI request.
    pub ai_context: String,
}

impl Default for Pack {
    fn default() -> Self {
        Self {
            format: FORMAT_VERSION,
            id: new_id(),
            name: String::new(),
            author: String::new(),
            application: String::new(),
            source_lang: String::new(),
            target_lang: String::new(),
            match_threshold: 0.85,
            entries: Vec::new(),
            ai_context: String::new(),
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct PackEntry {
    pub source_text: String,
    /// Other spellings of the same on-screen text, as OCR read it in
    /// different frames ("Game Menu", "Game Meng"). Matching uses all of them.
    pub aliases: Vec<String>,
    pub translation: String,
    /// Overrides the pack's match threshold, e.g. stricter for short words.
    pub match_threshold: Option<f32>,
    pub style: TextStyle,
    /// Where the creator saw this text and how it looked, so playback can
    /// recognise it by its pixels and lock the translation there.
    pub anchors: Vec<Anchor>,
    /// Drafted by auto-translate and not yet checked by the creator.
    pub draft: bool,
}

/// A confirmed appearance of an entry's text: its area in a recorded
/// keyframe and a small grayscale picture of it. During playback, text whose
/// pixels match an anchor is translated without OCR, so OCR misreads can't
/// replace or hide it.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct Anchor {
    /// Size of the captured window when recorded; anchors only apply when
    /// the window has the same size.
    pub frame_width: u32,
    pub frame_height: u32,
    /// The text's area in the capture, in pixels: left, top, width, height.
    pub area: [i32; 4],
    /// The area's brightest-channel grayscale pixels as a base64 PNG.
    pub image: String,
}

impl Anchor {
    /// Records the area of `frame` (in image pixels).
    pub fn new(frame: &Frame, area: Rect) -> Option<Self> {
        let crop = frame.crop(area);
        if crop.width == 0 || crop.height == 0 {
            return None;
        }
        let gray = crop
            .rgba
            .as_chunks::<4>()
            .0
            .iter()
            .map(|[red, green, blue, _]| *red.max(green).max(blue))
            .collect::<Vec<_>>();
        let image = image::GrayImage::from_raw(crop.width, crop.height, gray)?;
        let mut png = std::io::Cursor::new(Vec::new());
        image.write_to(&mut png, image::ImageFormat::Png).ok()?;
        Some(Self {
            frame_width: frame.width,
            frame_height: frame.height,
            area: [
                area.left.max(0),
                area.top.max(0),
                crop.width as i32,
                crop.height as i32,
            ],
            image: BASE64.encode(png.into_inner()),
        })
    }

    pub fn rect(&self) -> Rect {
        let [left, top, width, height] = self.area;
        Rect::new(left, top, width.max(0) as u32, height.max(0) as u32)
    }

    /// The recorded grayscale pixels: width, height and values.
    pub fn pixels(&self) -> Option<(u32, u32, Vec<u8>)> {
        let png = BASE64.decode(&self.image).ok()?;
        let image = image::load_from_memory_with_format(&png, image::ImageFormat::Png)
            .ok()?
            .to_luma8();
        Some((image.width(), image.height(), image.into_raw()))
    }

    /// Whether this anchor is about the same place as `other`.
    pub fn same_place(&self, other: &Self) -> bool {
        self.frame_width == other.frame_width
            && self.frame_height == other.frame_height
            && (self.area[0] - other.area[0]).abs() <= 6
            && (self.area[1] - other.area[1]).abs() <= 6
    }
}

/// How a translation is drawn over the original text.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct TextStyle {
    /// Font size in points; `None` fits the text to the original's area.
    pub font_size: Option<f32>,
    /// Text colour (RGBA); `None` picks black or white for contrast with
    /// the background.
    pub color: Option<[u8; 4]>,
    pub outline: Option<[u8; 4]>,
    pub align: TextAlign,
    pub background: Background,
    /// Moves the translation from where the original text was, in screen
    /// points (right, down). The original is still erased in place.
    pub offset: [f32; 2],
}

impl Default for TextStyle {
    fn default() -> Self {
        Self {
            font_size: None,
            color: None,
            outline: None,
            align: TextAlign::Left,
            background: Background::Auto,
            offset: [0.0, 0.0],
        }
    }
}

impl TextStyle {
    pub fn is_moved(&self) -> bool {
        self.offset != [0.0, 0.0]
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub enum TextAlign {
    #[default]
    Left,
    Center,
}

/// How the original text is hidden behind the translation.
#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Serialize)]
pub enum Background {
    /// Filled from the colours around the original text, so it blends in.
    #[default]
    Auto,
    Solid([u8; 4]),
    /// Drawn without hiding the original, for text over empty space.
    None,
}

impl Pack {
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        let pack: Self = serde_json::from_slice(&fs::read(path)?)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if pack.format > FORMAT_VERSION {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "pack format {} needs a newer Overlayr (this one reads up to {FORMAT_VERSION})",
                    pack.format
                ),
            ));
        }
        Ok(pack)
    }

    /// Writes the pack atomically, so a crash never leaves a partial file.
    pub fn save(&self, path: impl AsRef<Path>) -> io::Result<()> {
        let path = path.as_ref();
        if let Some(directory) = path.parent() {
            fs::create_dir_all(directory)?;
        }
        let json = serde_json::to_vec_pretty(self).map_err(io::Error::other)?;
        let partial = path.with_extension("partial");
        fs::write(&partial, json)?;
        fs::rename(partial, path)
    }

    /// The entry whose source text best matches `text`, if any is similar
    /// enough under its threshold.
    pub fn find(&self, text: &str) -> Option<&PackEntry> {
        self.find_index(text).map(|index| &self.entries[index])
    }

    /// Index of the entry [`Pack::find`] would return.
    pub fn find_index(&self, text: &str) -> Option<usize> {
        let text = normalize(text);
        self.entries
            .iter()
            .enumerate()
            .map(|(index, entry)| {
                let similarity = std::iter::once(&entry.source_text)
                    .chain(&entry.aliases)
                    .map(|source| text_similarity(&text, &normalize(source)))
                    .fold(0.0, f32::max);
                (similarity, index, entry)
            })
            .filter(|(similarity, _, entry)| {
                *similarity >= entry.match_threshold.unwrap_or(self.match_threshold)
            })
            .max_by(|left, right| left.0.total_cmp(&right.0))
            .map(|(_, index, _)| index)
    }

    /// Everything playback needs to recognise the pack's anchored texts by
    /// their pixels.
    pub fn anchor_targets(&self) -> Vec<AnchorTarget> {
        self.entries
            .iter()
            .filter(|entry| !entry.translation.trim().is_empty())
            .flat_map(|entry| {
                entry.anchors.iter().filter_map(move |anchor| {
                    let (width, height, pixels) = anchor.pixels()?;
                    Some(AnchorTarget {
                        frame_width: anchor.frame_width,
                        frame_height: anchor.frame_height,
                        area: anchor.rect(),
                        width,
                        height,
                        pixels,
                        source_text: entry.source_text.clone(),
                        translation: Translated {
                            text: entry.translation.clone(),
                            style: Some(entry.style.clone()),
                        },
                    })
                })
            })
            .collect()
    }

    /// Where this pack is stored in the user's pack library.
    pub fn library_path(&self) -> Option<PathBuf> {
        Some(packs_dir()?.join(format!("{}.{PACK_EXTENSION}", self.id)))
    }
}

/// The user's pack library.
pub fn packs_dir() -> Option<PathBuf> {
    let data_home = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/share"))
        })?;
    Some(data_home.join("overlayr").join("packs"))
}

/// Packs in the library, sorted by name. Unreadable files are skipped.
pub fn library() -> Vec<(PathBuf, Pack)> {
    let Some(directory) = packs_dir() else {
        return Vec::new();
    };
    let Ok(entries) = fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut packs = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().is_some_and(|ext| ext == PACK_EXTENSION))
        .filter_map(|path| Pack::load(&path).ok().map(|pack| (path, pack)))
        .collect::<Vec<_>>();
    packs.sort_by_key(|(_, pack)| pack.name.to_lowercase());
    packs
}

fn new_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    format!("{nanos:x}{:x}", std::process::id())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn styles_saved_before_moving_existed_stay_in_place() {
        let style: TextStyle =
            serde_json::from_str(r#"{"font_size":null,"align":"Center"}"#).expect("parses");
        assert_eq!(style.offset, [0.0, 0.0]);
        assert!(!style.is_moved());
        let moved = TextStyle {
            offset: [12.0, -3.0],
            ..TextStyle::default()
        };
        let saved = serde_json::to_string(&moved).expect("saves");
        let loaded: TextStyle = serde_json::from_str(&saved).expect("loads");
        assert_eq!(loaded.offset, [12.0, -3.0]);
    }

    fn pack() -> Pack {
        Pack {
            name: "FTB Quests (Thai)".into(),
            entries: vec![
                PackEntry {
                    source_text: "Tasks".into(),
                    translation: "ภารกิจ".into(),
                    match_threshold: Some(1.0),
                    ..PackEntry::default()
                },
                PackEntry {
                    source_text: "To push your Eterna even higher".into(),
                    translation: "เพื่อเพิ่ม Eterna ให้สูงขึ้น".into(),
                    ..PackEntry::default()
                },
            ],
            ..Pack::default()
        }
    }

    #[test]
    fn finds_entries_by_similarity_with_per_entry_thresholds() {
        let pack = pack();
        assert_eq!(
            pack.find("To push gour Eterna even higher")
                .map(|entry| entry.translation.as_str()),
            Some("เพื่อเพิ่ม Eterna ให้สูงขึ้น")
        );
        assert_eq!(
            pack.find("tasks").map(|e| e.translation.as_str()),
            Some("ภารกิจ")
        );
        // "Tasks" requires an exact match, so OCR noise does not match it.
        assert!(pack.find("Masks").is_none());
        assert!(pack.find("Rewards").is_none());
    }

    #[test]
    fn matches_any_spelling_of_an_entry() {
        let pack = Pack {
            entries: vec![PackEntry {
                source_text: "Oame Menu".into(),
                aliases: vec!["Game Menu".into()],
                translation: "เมนูเกม".into(),
                ..PackEntry::default()
            }],
            ..Pack::default()
        };
        // 78% similar to "Oame Menu", but 89% to the alias "Game Menu".
        assert!(pack.find("Game Meng").is_some());
    }

    #[test]
    fn anchors_keep_the_recorded_pixels() {
        let mut rgba = vec![30_u8; 100 * 50 * 4];
        for pixel in rgba.chunks_mut(4).step_by(7) {
            pixel.copy_from_slice(&[20, 240, 90, 255]);
        }
        let frame = Frame::new(1, Rect::new(0, 0, 100, 50), 100, 50, rgba);
        let anchor = Anchor::new(&frame, Rect::new(10, 5, 40, 12)).expect("anchor");
        assert_eq!(anchor.rect(), Rect::new(10, 5, 40, 12));
        let (width, height, pixels) = anchor.pixels().expect("decodes");
        assert_eq!((width, height, pixels.len()), (40, 12, 480));
        // Stored as each pixel's brightest channel, like the tracker sees it.
        assert!(pixels.contains(&240) && pixels.contains(&30));
    }

    #[test]
    fn round_trips_through_a_file() {
        let directory = std::env::temp_dir().join(format!("overlayr-pack-test-{}", new_id()));
        let path = directory.join(format!("test.{PACK_EXTENSION}"));
        let original = pack();
        original.save(&path).expect("save pack");
        assert_eq!(Pack::load(&path).expect("load pack"), original);
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn rejects_packs_from_newer_versions() {
        let directory = std::env::temp_dir().join(format!("overlayr-pack-test-{}", new_id()));
        let path = directory.join("future.overlayr-pack");
        fs::create_dir_all(&directory).expect("create directory");
        fs::write(&path, r#"{"format": 99, "name": "Future"}"#).expect("write");
        assert!(Pack::load(&path).is_err());
        let _ = fs::remove_dir_all(directory);
    }
}
