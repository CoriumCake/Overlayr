use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};

use image::ImageEncoder;

use crate::services::{OcrBackend, ServiceResult};
use crate::text::{
    clean_text, correct_text, has_word, is_latin_text, is_valid_text, is_word_like, join_words,
};
use crate::tracker::{normalize, text_similarity};
use crate::types::{Frame, RejectedText, TextBlock};

/// Overlayr's per-user data directory, for downloads that don't need
/// administrator access.
pub fn data_dir() -> Option<PathBuf> {
    // Windows has no HOME; per-user program data goes in %LOCALAPPDATA%.
    if cfg!(windows) {
        return std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .map(|path| path.join("Overlayr"));
    }
    let data_home = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/share"))
        })?;
    Some(data_home.join("overlayr"))
}

/// Per-user directory for Tesseract's high-accuracy ("best") models.
pub fn accurate_model_dir() -> Option<PathBuf> {
    Some(data_dir()?.join("tessdata"))
}

/// The best OCR engine installed for `language`: the neural engine when its
/// models and runtime are present and it reads the language, else Tesseract.
pub fn engine(language: &str, grouping_mode: &str) -> Box<dyn OcrBackend> {
    let neural = crate::neural::NeuralOcr::new(grouping_mode, language);
    if crate::neural::is_installed(language) && neural.validate_language(language).is_ok() {
        return Box::new(neural);
    }
    tesseract_engine(grouping_mode)
}

pub fn tesseract_engine(grouping_mode: &str) -> Box<dyn OcrBackend> {
    #[cfg(feature = "native-ocr")]
    return Box::new(NativeTesseractOcr::new(grouping_mode));
    #[cfg(not(feature = "native-ocr"))]
    Box::new(TesseractOcr::new(tesseract_executable(), grouping_mode))
}

/// The Tesseract command line to run: the one shipped with Overlayr, else
/// the system's. Apps opened from the macOS Finder get a minimal PATH
/// without Homebrew's folders, so a bare "tesseract" would not be found
/// there; look it up the way the diagnostics do.
#[cfg(not(feature = "native-ocr"))]
fn tesseract_executable() -> String {
    std::env::var("OVERLAYR_TESSERACT_BIN").unwrap_or_else(|_| {
        crate::bundle::tesseract()
            .or_else(|| crate::diagnostics::find_command("tesseract"))
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_else(|| "tesseract".into())
    })
}

/// Whether a Tesseract engine exists, so downloading a language model for
/// it is enough to read that language.
pub fn tesseract_available() -> bool {
    #[cfg(feature = "native-ocr")]
    return true;
    #[cfg(not(feature = "native-ocr"))]
    crate::diagnostics::command_available(&tesseract_executable())
}

/// A Tesseract process. Overlayr is a windowed program on Windows, so a
/// console program it starts would open a console window on every frame
/// unless told not to.
fn tesseract_command(executable: &str) -> Command {
    #[allow(unused_mut)]
    let mut command = Command::new(executable);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

pub fn accurate_model_path(language: &str) -> Option<PathBuf> {
    Some(accurate_model_dir()?.join(format!("{}.traineddata", TesseractOcr::language(language))))
}

pub fn has_accurate_model(language: &str) -> bool {
    accurate_model_path(language).is_some_and(|path| path.is_file())
}

/// OCR adapter for a locally installed Tesseract binary.
///
/// The image is streamed over stdin, so no captured screen contents are
/// written to disk. Install the language packs required by the user (for
/// example `tesseract-ocr-jpn` and `tesseract-ocr-tha` on Debian/Ubuntu).
#[derive(Debug, Clone)]
pub struct TesseractOcr {
    executable: String,
    grouping_mode: String,
    /// Lines the last parse discarded, for debug mode.
    rejected: std::cell::RefCell<Vec<RejectedText>>,
}

#[cfg(feature = "native-ocr")]
pub struct NativeTesseractOcr {
    /// One engine per OCR pass, so the passes can run at the same time.
    engines: Vec<NativeEngine>,
    language: String,
    grouping_mode: String,
    rejected: Vec<RejectedText>,
}

#[cfg(feature = "native-ocr")]
// LepTess is owned and used exclusively by the pipeline thread. The native
// backend never shares the Tesseract handle concurrently.
unsafe impl Send for NativeTesseractOcr {}

impl Default for TesseractOcr {
    fn default() -> Self {
        Self {
            executable: "tesseract".into(),
            grouping_mode: "paragraph".into(),
            rejected: std::cell::RefCell::default(),
        }
    }
}

impl TesseractOcr {
    pub fn new(executable: impl Into<String>, grouping_mode: impl Into<String>) -> Self {
        Self {
            executable: executable.into(),
            grouping_mode: grouping_mode.into(),
            rejected: std::cell::RefCell::default(),
        }
    }

    /// The frame prepared for OCR in two ways, each read separately:
    ///
    /// 1. A bright-text mask: pixels whose brightest channel is near white
    ///    become black text on white. Games draw UI text in white or bright
    ///    colours over textured buttons and busy scenes, which confuse OCR;
    ///    on a Minecraft menu this reads every button where grayscale read
    ///    almost nothing.
    /// 2. Depending on the scene:
    ///    - dark scenes get a dim-text mask with a much lower cut-off, for
    ///      text in muted colours (completed or locked entries in a quest
    ///      list) that the bright mask misses;
    ///    - lighter scenes get each pixel's brightest channel as is, for dark
    ///      or coloured text on light backgrounds.
    ///
    /// Masks are enlarged 1.5× (keeping hard pixel edges): pixel fonts read
    /// with clearly higher confidence that way, for about 15% more time.
    /// Each image comes with a stand-in frame of its size, so OCR positions
    /// map back to the screen.
    fn prepared_images(frame: &Frame) -> ServiceResult<Vec<(Vec<u8>, Frame)>> {
        if frame.rgba.len() != (frame.width * frame.height * 4) as usize {
            return Err("frame pixel buffer has an invalid RGBA length".into());
        }
        let brightest = frame
            .rgba
            .as_chunks::<4>()
            .0
            .iter()
            .map(|[red, green, blue, _]| *red.max(green).max(blue))
            .collect::<Vec<_>>();
        let mask = |level: u8| {
            brightest
                .iter()
                .map(|&value| if value >= level { 0 } else { 255 })
                .collect::<Vec<_>>()
        };
        let mean = brightest.iter().map(|&value| u64::from(value)).sum::<u64>()
            / brightest.len().max(1) as u64;
        let enlarged = |pixels: Vec<u8>| -> ServiceResult<(Vec<u8>, Frame)> {
            let (width, height) = (frame.width * 3 / 2, frame.height * 3 / 2);
            let scaled = (0..height)
                .flat_map(|y| (0..width).map(move |x| (x * 2 / 3, y * 2 / 3)))
                .map(|(x, y)| pixels[(y * frame.width + x) as usize])
                .collect();
            Ok((
                encode_gray(width, height, scaled)?,
                Frame::new(frame.sequence, frame.bounds, width, height, Vec::new()),
            ))
        };
        let mut images = vec![enlarged(mask(BRIGHT_TEXT_LEVEL))?];
        if mean < DARK_SCENE_LEVEL {
            images.push(enlarged(mask(DIM_TEXT_LEVEL))?);
            // Very dim text (disabled or locked entries drawn in dark grey)
            // sits just above the background, so a third cut-off follows the
            // scene's own background level. It only adds a pass when it
            // differs enough from the dim cut-off.
            let faint = faint_text_level(&brightest);
            if faint + 10 < DIM_TEXT_LEVEL {
                images.push(enlarged(mask(faint))?);
            }
        } else {
            images.push((
                encode_gray(frame.width, frame.height, brightest.clone())?,
                Frame::new(
                    frame.sequence,
                    frame.bounds,
                    frame.width,
                    frame.height,
                    Vec::new(),
                ),
            ));
        }
        Ok(images)
    }

    pub(crate) fn language(language: &str) -> &str {
        match language {
            "ja" | "ja-JP" | "Japanese" => "jpn",
            "th" | "th-TH" | "Thai" => "tha",
            "ko" | "ko-KR" | "Korean" => "kor",
            "zh" | "zh-CN" | "Chinese" => "chi_sim",
            "zh-TW" => "chi_tra",
            "en" | "en-US" | "English" => "eng",
            other => other,
        }
    }

    pub fn validate_language(&self, language: &str) -> ServiceResult<()> {
        if has_accurate_model(language) && crate::diagnostics::command_available(&self.executable) {
            return Ok(());
        }
        let output = tesseract_command(&self.executable)
            .arg("--list-langs")
            .output()
            .map_err(|error| format!("failed to start {}: {error}", self.executable))?;
        if !output.status.success() {
            return Err(format!(
                "{} could not list installed languages: {}",
                self.executable,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        let expected = Self::language(language);
        if String::from_utf8_lossy(&output.stdout)
            .lines()
            .skip(1)
            .any(|installed| installed.trim() == expected)
        {
            Ok(())
        } else {
            Err(format!(
                "Tesseract language pack '{expected}' is not installed"
            ))
        }
    }

    /// Builds translation units from Tesseract's TSV output. Words are
    /// grouped into Tesseract's lines; in paragraph mode, lines are then
    /// joined into paragraphs by layout, so whole sentences are translated
    /// rather than single words.
    fn parse_tsv(&self, frame: &Frame, tsv: &str) -> Vec<TextBlock> {
        let mut lines: Vec<(LineKey, Vec<OcrWord>)> = Vec::new();
        let mut split = 0;
        for line in tsv.lines().skip(1) {
            let fields: Vec<&str> = line.split('\t').collect();
            if fields.len() < 12 || fields[0] != "5" {
                continue;
            }
            let number = |index: usize| fields[index].parse::<u32>().unwrap_or_default();
            let word = OcrWord {
                left: fields[6].parse().unwrap_or_default(),
                top: fields[7].parse().unwrap_or_default(),
                width: number(8),
                height: number(9),
                confidence: fields[10].parse::<f32>().unwrap_or(-1.0) / 100.0,
                text: clean_text(fields[11]),
            };
            if word.width == 0 || word.height == 0 || word.confidence < 0.0 || word.text.is_empty()
            {
                continue;
            }
            // Lone low-confidence symbols are almost always icons or borders.
            if word.confidence < 0.5 && !word.text.chars().any(char::is_alphanumeric) {
                continue;
            }
            // Split a line at wide horizontal gaps: Tesseract sometimes joins
            // a title with distant buttons ("x", "+") or separate columns.
            let tesseract_line = (number(2), number(3), number(4));
            if let Some(((block, paragraph, line, _), words)) = lines.last()
                && (*block, *paragraph, *line) == tesseract_line
                && let Some(previous) = words.last()
            {
                let gap = word.left - (previous.left + previous.width as i32);
                if gap * 2 > previous.height.max(word.height) as i32 * 5 {
                    split += 1;
                }
            }
            let key = (tesseract_line.0, tesseract_line.1, tesseract_line.2, split);
            match lines.last_mut() {
                Some((previous, words)) if *previous == key => words.push(word),
                _ => lines.push((key, vec![word])),
            }
        }
        let (blocks, rejected) = blocks_from_words(
            frame,
            lines.into_iter().map(|(_, words)| words).collect(),
            &self.grouping_mode,
        );
        *self.rejected.borrow_mut() = rejected;
        blocks
    }
}

/// Turns recognised lines of words into translation units: drops lines that
/// are only noise or far off the typical text size, then joins lines into
/// paragraphs unless `grouping_mode` is "line".
pub(crate) fn blocks_from_words(
    frame: &Frame,
    lines: Vec<Vec<OcrWord>>,
    grouping_mode: &str,
) -> (Vec<TextBlock>, Vec<RejectedText>) {
    let mut rejected = Vec::new();
    let lines = lines
        .into_iter()
        .filter_map(|words| {
            let summary = rejected_text(
                frame,
                &words,
                "Filtered as noise (icons, bars or fragments)",
            );
            let line = OcrLine::new(words);
            if line.is_none() {
                rejected.extend(summary);
            }
            line
        })
        .collect::<Vec<_>>();
    // Text on one screen comes in a few sizes. A short "word" far taller
    // or smaller than the typical line is OCR reading icons or pixel art.
    let mut heights = lines.iter().map(OcrLine::height).collect::<Vec<_>>();
    heights.sort_unstable();
    let typical = heights.get(heights.len() / 2).copied().unwrap_or_default();
    let (lines, wrong_size): (Vec<_>, Vec<_>) = lines.into_iter().partition(|line| {
        let letters = line
            .words
            .iter()
            .flat_map(|word| word.text.chars())
            .filter(|character| character.is_alphabetic())
            .count();
        letters >= 8
            || typical == 0
            || (line.height() * 100 >= typical * 55 && line.height() <= typical * 2)
    });
    for line in &wrong_size {
        rejected.extend(rejected_text(
            frame,
            &line.words,
            "Wrong size for text (likely icons)",
        ));
    }
    let groups = if grouping_mode == "line" {
        lines.into_iter().map(|line| vec![line]).collect()
    } else {
        group_paragraphs(lines)
    };
    let blocks = groups
        .iter()
        .filter_map(|paragraph| {
            let block = block_from_lines(frame, paragraph);
            if block.is_none() {
                let words = paragraph
                    .iter()
                    .flat_map(|line| &line.words)
                    .collect::<Vec<_>>();
                rejected.extend(rejected_text(frame, words, "Not recognisable text"));
            }
            block
        })
        .collect();
    (blocks, rejected)
}

/// Brightest-channel level at or above which a pixel counts as bright UI
/// text for the first OCR pass.
const BRIGHT_TEXT_LEVEL: u8 = 217;
/// Cut-off for faint text: a margin above the scene's background. Dark game
/// UIs often have several background shades (a sidebar darker than the main
/// panel), so the 75th percentile stands in for the brightest of them.
fn faint_text_level(brightest: &[u8]) -> u8 {
    let mut histogram = [0_usize; 256];
    for &value in brightest {
        histogram[usize::from(value)] += 1;
    }
    let quarter_from_top = brightest.len() * 3 / 4;
    let mut seen = 0;
    let background = histogram
        .iter()
        .position(|&count| {
            seen += count;
            seen > quarter_from_top
        })
        .unwrap_or_default() as u8;
    background.saturating_add(FAINT_TEXT_MARGIN)
}

/// How much brighter than the background faint text must be.
const FAINT_TEXT_MARGIN: u8 = 16;
/// Text similarity at which two OCR passes count as reading the same text.
const PASS_AGREEMENT: f32 = 0.8;
/// Cut-off for the dim-text mask used in dark scenes.
const DIM_TEXT_LEVEL: u8 = 100;
/// Average brightest-channel level below which a scene counts as dark.
const DARK_SCENE_LEVEL: u64 = 100;

fn encode_gray(width: u32, height: u32, pixels: Vec<u8>) -> ServiceResult<Vec<u8>> {
    let image = image::GrayImage::from_raw(width, height, pixels)
        .ok_or_else(|| "could not prepare the frame for OCR".to_string())?;
    let mut bytes = Vec::new();
    // Fast compression: the image is decoded again immediately.
    image::codecs::png::PngEncoder::new_with_quality(
        &mut bytes,
        image::codecs::png::CompressionType::Fast,
        image::codecs::png::FilterType::NoFilter,
    )
    .write_image(&image, width, height, image::ExtendedColorType::L8)
    .map_err(|error| format!("could not encode frame for OCR: {error}"))?;
    Ok(bytes)
}

/// Combines the results of several OCR passes over the same frame. Where
/// passes found the same text, the more confident reading wins.
fn merge_passes(passes: Vec<Vec<TextBlock>>) -> Vec<TextBlock> {
    let mut merged: Vec<TextBlock> = Vec::new();
    for block in passes.into_iter().flatten() {
        match merged
            .iter_mut()
            .find(|known| same_place(known.bounds, block.bounds))
        {
            Some(known) => {
                // Two differently prepared images reading the same text is
                // strong evidence it's real, even when Tesseract scores each
                // reading low (common with pixel fonts).
                let agreed = text_similarity(
                    &normalize(&known.source_text),
                    &normalize(&block.source_text),
                ) >= PASS_AGREEMENT;
                let combined = 1.0 - (1.0 - known.confidence) * (1.0 - block.confidence);
                if block.confidence > known.confidence {
                    *known = block;
                }
                if agreed {
                    known.confidence = combined;
                }
            }
            None => merged.push(block),
        }
    }
    merged
}

/// Whether two boxes cover mostly the same area (at least half of the
/// smaller one).
fn same_place(first: crate::types::Rect, second: crate::types::Rect) -> bool {
    let width = (first.right().min(second.right()) - first.left.max(second.left)).max(0);
    let height = (first.bottom().min(second.bottom()) - first.top.max(second.top)).max(0);
    let overlap = i64::from(width) * i64::from(height);
    let smaller = (i64::from(first.width) * i64::from(first.height))
        .min(i64::from(second.width) * i64::from(second.height));
    smaller > 0 && overlap * 2 >= smaller
}

/// Summarises discarded words for debug mode.
fn rejected_text<'a>(
    frame: &Frame,
    words: impl IntoIterator<Item = &'a OcrWord>,
    reason: &'static str,
) -> Option<RejectedText> {
    let words = words.into_iter().collect::<Vec<_>>();
    let left = words.iter().map(|word| word.left).min()?;
    let top = words.iter().map(|word| word.top).min()?;
    let right = words
        .iter()
        .map(|word| word.left + word.width as i32)
        .max()?;
    let bottom = words
        .iter()
        .map(|word| word.top + word.height as i32)
        .max()?;
    Some(RejectedText {
        text: join_words(words.iter().map(|word| word.text.as_str())),
        bounds: frame.to_screen(left, top, (right - left) as u32, (bottom - top) as u32),
        confidence: weighted_confidence(words.iter().copied()),
        reason,
    })
}

/// Tesseract block, paragraph and line, plus a counter for gap splits.
type LineKey = (u32, u32, u32, u32);

pub(crate) struct OcrWord {
    pub(crate) left: i32,
    pub(crate) top: i32,
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) confidence: f32,
    pub(crate) text: String,
}

impl OcrWord {
    /// A one-character ASCII token that isn't a word or number on its own.
    fn is_stray_glyph(&self) -> bool {
        let mut characters = self.text.chars();
        match (characters.next(), characters.next()) {
            (Some(character), None) => {
                character.is_ascii() && !character.is_ascii_digit() && !"aAI".contains(character)
            }
            _ => false,
        }
    }
}

/// One line of words as Tesseract segmented it, with its pixel bounds.
struct OcrLine {
    words: Vec<OcrWord>,
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}

impl OcrLine {
    /// Returns `None` for lines that are only OCR noise from icons and
    /// borders, so they cannot bridge two real paragraphs.
    fn new(mut words: Vec<OcrWord>) -> Option<Self> {
        let latin = is_latin_text(&join_words(words.iter().map(|word| word.text.as_str())));
        // Trim noise at either end: a close button's "x", a "+" or "»"
        // beside a title, or, in Latin-script text, icon fragments such as
        // "'f" or "L3" read next to real words.
        let is_noise =
            |word: &OcrWord| word.is_stray_glyph() || (latin && !is_word_like(&word.text));
        while words.last().is_some_and(is_noise) {
            words.pop();
        }
        let leading = words.iter().take_while(|word| is_noise(word)).count();
        words.drain(..leading);
        // Lines that are mostly fragments ("rrrrrrrrr" from a health bar,
        // "VOJ'/E@Q'" from an icon) aren't text.
        if latin {
            let real = words.iter().filter(|word| is_word_like(&word.text)).count();
            if real * 2 < words.len() {
                return None;
            }
        }
        let text = join_words(words.iter().map(|word| word.text.as_str()));
        let has_letters = text.chars().any(char::is_alphabetic);
        if !has_word(&text) && !(has_letters && weighted_confidence(&words) >= 0.6) {
            return None;
        }
        Some(Self {
            left: words.iter().map(|word| word.left).min()?,
            top: words.iter().map(|word| word.top).min()?,
            right: words
                .iter()
                .map(|word| word.left + word.width as i32)
                .max()?,
            bottom: words
                .iter()
                .map(|word| word.top + word.height as i32)
                .max()?,
            words,
        })
    }

    fn height(&self) -> i32 {
        self.bottom - self.top
    }

    /// Whether `next` reads as the following line of the same paragraph:
    /// horizontally overlapping, starting just below, at a similar size.
    fn continues_with(&self, next: &Self) -> bool {
        self.touches(next) && self.reads_on(next)
    }

    /// Whether `next` sits just below, at a similar size.
    fn touches(&self, next: &Self) -> bool {
        let overlaps = next.left < self.right && self.left < next.right;
        let gap = next.top - self.bottom;
        let (small, large) = if self.height() < next.height() {
            (self.height(), next.height())
        } else {
            (next.height(), self.height())
        };
        overlaps && gap * 4 <= large * 3 && gap * 2 > -large && large <= small * 2
    }

    /// Whether the text flows on into `next`, as wrapped prose does, rather
    /// than `next` being a separate item of a list or menu: either `next`
    /// starts mid-sentence (lowercase), or this line is a full line of prose,
    /// several words long and at least about as wide as the next.
    fn reads_on(&self, next: &Self) -> bool {
        let starts_lowercase = next
            .words
            .first()
            .and_then(|word| {
                word.text
                    .chars()
                    .find(|character| character.is_alphabetic())
            })
            .is_some_and(char::is_lowercase);
        let text = join_words(self.words.iter().map(|word| word.text.as_str()));
        let long_enough = if text.chars().any(char::is_alphabetic) && is_latin_text(&text) {
            // Symbols such as "&" or "-" aren't words.
            text.split_whitespace()
                .filter(|word| word.chars().any(char::is_alphabetic))
                .count()
                >= 4
        } else {
            text.chars()
                .filter(|character| !character.is_whitespace())
                .count()
                >= 12
        };
        let width = self.right - self.left;
        // Wrapped prose keeps its margin: the next line starts at the same
        // left edge. A heading or list item that starts further left or
        // right is separate, even when it happens to be centred below.
        let tolerance = self.height().max(next.height()) * 2 / 3;
        let aligned = (next.left - self.left).abs() <= tolerance;
        let full = long_enough && aligned && self.right - next.right >= -(width / 10);
        starts_lowercase || full
    }
}

/// Joins lines into paragraphs. Tesseract's own paragraphs are unreliable
/// in sparse mode and on UI layouts, so this works from geometry alone.
fn group_paragraphs(mut lines: Vec<OcrLine>) -> Vec<Vec<OcrLine>> {
    lines.sort_by_key(|line| (line.top, line.left));
    let mut paragraphs: Vec<Vec<OcrLine>> = Vec::new();
    for line in lines {
        let paragraph = paragraphs.iter_mut().rev().find(|paragraph| {
            paragraph
                .last()
                .is_some_and(|previous| previous.continues_with(&line))
        });
        match paragraph {
            Some(paragraph) => paragraph.push(line),
            None => paragraphs.push(vec![line]),
        }
    }
    paragraphs
}

/// Average word confidence weighted by length: Tesseract often scores short
/// correct words very low, so a plain minimum would discard good sentences.
fn weighted_confidence<'a>(words: impl IntoIterator<Item = &'a OcrWord>) -> f32 {
    let (sum, total_weight) = words.into_iter().fold((0.0, 0.0), |(sum, total), word| {
        let weight = word.text.chars().count() as f32;
        (sum + word.confidence * weight, total + weight)
    });
    sum / f32::max(total_weight, 1.0)
}

fn block_from_lines(frame: &Frame, lines: &[OcrLine]) -> Option<TextBlock> {
    let words = || lines.iter().flat_map(|line| &line.words);
    let text = correct_text(&join_words(words().map(|word| word.text.as_str())));
    if !is_valid_text(&text, "auto") || !has_word(&text) {
        return None;
    }
    let confidence = weighted_confidence(words());
    let left = lines.iter().map(|line| line.left).min()?;
    let top = lines.iter().map(|line| line.top).min()?;
    let right = lines.iter().map(|line| line.right).max()?;
    let bottom = lines.iter().map(|line| line.bottom).max()?;
    Some(TextBlock {
        lines: lines.len() as u32,
        ..TextBlock::new(
            frame.to_screen(left, top, (right - left) as u32, (bottom - top) as u32),
            text,
            confidence,
        )
    })
}

/// A Tesseract instance used by one OCR pass at a time.
#[cfg(feature = "native-ocr")]
struct NativeEngine(leptess::LepTess);

// Each engine is a separate Tesseract instance, only ever used by the one
// thread running its pass.
#[cfg(feature = "native-ocr")]
unsafe impl Send for NativeEngine {}

#[cfg(feature = "native-ocr")]
impl NativeEngine {
    fn read(&mut self, image: &[u8]) -> ServiceResult<String> {
        self.0
            .set_image_from_mem(image)
            .map_err(|error| format!("could not load frame into Tesseract: {error}"))?;
        self.0.set_fallback_source_resolution(300);
        self.0
            .get_tsv_text(0)
            .map_err(|error| format!("Tesseract returned invalid UTF-8: {error}"))
    }
}

#[cfg(feature = "native-ocr")]
fn accurate_model_data_path(language: &str) -> Option<String> {
    has_accurate_model(language)
        .then(accurate_model_dir)
        .flatten()
        .map(|path| path.to_string_lossy().into_owned())
}

#[cfg(feature = "native-ocr")]
impl NativeTesseractOcr {
    pub fn new(grouping_mode: impl Into<String>) -> Self {
        Self {
            engines: Vec::new(),
            language: String::new(),
            grouping_mode: grouping_mode.into(),
            rejected: Vec::new(),
        }
    }

    pub fn validate_language(&self, language: &str) -> ServiceResult<()> {
        let code = TesseractOcr::language(language);
        leptess::LepTess::new(accurate_model_data_path(language).as_deref(), code)
            .map(|_| ())
            .map_err(|error| format!("Tesseract language '{code}' is unavailable: {error}"))
    }

    fn engines_for(&mut self, language: &str, count: usize) -> ServiceResult<&mut [NativeEngine]> {
        let code = TesseractOcr::language(language).to_string();
        if self.language != code {
            self.engines.clear();
            self.language = code.clone();
        }
        while self.engines.len() < count {
            let mut engine =
                leptess::LepTess::new(accurate_model_data_path(language).as_deref(), &code)
                    .map_err(|error| format!("could not initialize Tesseract '{code}': {error}"))?;
            // Same sparse-text segmentation as the command-line backend.
            engine
                .set_variable(leptess::Variable::TesseditPagesegMode, "11")
                .map_err(|_| "could not configure Tesseract page segmentation".to_string())?;
            self.engines.push(NativeEngine(engine));
        }
        Ok(&mut self.engines[..count])
    }
}

impl OcrBackend for TesseractOcr {
    fn take_rejected(&mut self) -> Vec<RejectedText> {
        self.rejected.take()
    }

    fn validate_language(&self, language: &str) -> ServiceResult<()> {
        TesseractOcr::validate_language(self, language)
    }

    fn name(&self) -> &'static str {
        "Tesseract"
    }

    fn recognize(&mut self, frame: &Frame, language: &str) -> ServiceResult<Vec<TextBlock>> {
        let images = Self::prepared_images(frame)?;
        // The passes are independent, so they run at the same time.
        let executable = self.executable.as_str();
        let outputs = std::thread::scope(|scope| {
            let runs = images
                .iter()
                .map(|(image, _)| scope.spawn(move || run_tesseract(executable, image, language)))
                .collect::<Vec<_>>();
            runs.into_iter()
                .map(|run| {
                    run.join()
                        .unwrap_or_else(|_| Err("OCR pass crashed".into()))
                })
                .collect::<Vec<_>>()
        });
        let mut passes = Vec::new();
        let mut rejected = Vec::new();
        for ((_, layout), tsv) in images.iter().zip(outputs) {
            passes.push(self.parse_tsv(layout, &tsv?));
            rejected.extend(self.rejected.take());
        }
        *self.rejected.borrow_mut() = rejected;
        Ok(merge_passes(passes))
    }
}

/// Runs the Tesseract command line on an encoded image and returns its TSV.
fn run_tesseract(executable: &str, bytes: &[u8], language: &str) -> ServiceResult<String> {
    let mut command = tesseract_command(executable);
    if has_accurate_model(language)
        && let Some(directory) = accurate_model_dir()
    {
        command.arg("--tessdata-dir").arg(directory);
    }
    let mut child = command
        .args([
            "stdin",
            "stdout",
            // Sparse-text segmentation finds isolated UI labels that
            // automatic layout analysis skips; paragraphs are rebuilt
            // from line geometry afterwards.
            "--psm",
            "11",
            "-l",
            TesseractOcr::language(language),
            // Equivalent to the `tsv` config, which only exists in the
            // system tessdata directory.
            "-c",
            "tessedit_create_tsv=1",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("failed to start {executable}: {error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "could not open OCR stdin".to_string())?
        .write_all(bytes)
        .map_err(|error| format!("could not send frame to OCR: {error}"))?;
    let output = child
        .wait_with_output()
        .map_err(|error| format!("OCR process failed: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "{executable} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

#[cfg(feature = "native-ocr")]
impl OcrBackend for NativeTesseractOcr {
    fn recognize(&mut self, frame: &Frame, language: &str) -> ServiceResult<Vec<TextBlock>> {
        let parser = TesseractOcr::new("", self.grouping_mode.clone());
        let images = TesseractOcr::prepared_images(frame)?;
        let engines = self.engines_for(language, images.len())?;
        // Each pass has its own engine, so they run at the same time.
        let outputs = std::thread::scope(|scope| {
            let runs = engines
                .iter_mut()
                .zip(&images)
                .map(|(engine, (image, _))| scope.spawn(move || engine.read(image)))
                .collect::<Vec<_>>();
            runs.into_iter()
                .map(|run| {
                    run.join()
                        .unwrap_or_else(|_| Err("OCR pass crashed".into()))
                })
                .collect::<Vec<_>>()
        });
        let mut passes = Vec::new();
        let mut rejected = Vec::new();
        for ((_, layout), tsv) in images.iter().zip(outputs) {
            passes.push(parser.parse_tsv(layout, &tsv?));
            rejected.extend(parser.rejected.take());
        }
        self.rejected = rejected;
        Ok(merge_passes(passes))
    }

    fn take_rejected(&mut self) -> Vec<RejectedText> {
        std::mem::take(&mut self.rejected)
    }

    fn validate_language(&self, language: &str) -> ServiceResult<()> {
        NativeTesseractOcr::validate_language(self, language)
    }

    fn name(&self) -> &'static str {
        "Tesseract"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Rect;

    #[test]
    fn parses_tsv_words_into_screen_space_blocks() {
        let parser = TesseractOcr::new("tesseract", "line");
        let frame = Frame::new(
            1,
            Rect::new(100, 200, 320, 180),
            320,
            180,
            vec![0; 320 * 180 * 4],
        );
        let tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t10\t20\t80\t30\t94.5\t猫です\n";
        let blocks = parser.parse_tsv(&frame, tsv);

        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].source_text, "猫です");
        assert_eq!(blocks[0].bounds, Rect::new(110, 220, 80, 30));
        assert!((blocks[0].confidence - 0.945).abs() < 0.001);
    }

    #[test]
    fn groups_words_into_paragraphs_and_drops_icon_noise() {
        let parser = TesseractOcr::new("tesseract", "paragraph");
        let frame = Frame::new(1, Rect::new(0, 0, 400, 300), 400, 300, Vec::new());
        let row = |block: u32, par: u32, line: u32, left: i32, conf: f32, text: &str| {
            format!(
                "5\t1\t{block}\t{par}\t{line}\t1\t{left}\t{}\t40\t20\t{conf}\t{text}\n",
                100 + line * 25
            )
        };
        let tsv = [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n".to_string(),
            row(1, 1, 1, 10, 75.7, "QO"),
            row(1, 1, 2, 10, 32.7, "+"),
            row(2, 1, 1, 10, 85.4, "the"),
            row(2, 1, 1, 60, 2.5, "ways"),
            row(2, 1, 2, 10, 80.7, "to"),
            row(2, 1, 2, 60, 85.4, "craft,"),
        ]
        .concat();
        let blocks = parser.parse_tsv(&frame, &tsv);

        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].source_text, "the ways to craft,");
        assert_eq!(blocks[0].bounds, Rect::new(10, 125, 90, 45));
        assert!(blocks[0].confidence > 0.5);

        let lines = TesseractOcr::new("tesseract", "line").parse_tsv(&frame, &tsv);
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[1].source_text, "to craft,");
    }

    #[test]
    fn splits_paragraphs_at_large_vertical_gaps() {
        let parser = TesseractOcr::new("tesseract", "paragraph");
        let frame = Frame::new(1, Rect::new(0, 0, 900, 900), 900, 900, Vec::new());
        let tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n\
                   5\t1\t2\t1\t1\t1\t414\t510\t150\t24\t64\tGearing\n\
                   5\t1\t2\t1\t1\t2\t600\t510\t200\t24\t52\tAdventure!\n\
                   5\t1\t2\t1\t2\t1\t741\t558\t123\t21\t60\tRewards\n";
        let blocks = parser.parse_tsv(&frame, tsv);

        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].source_text, "Gearing Adventure!");
        assert_eq!(blocks[1].source_text, "Rewards");
    }

    #[test]
    fn rebuilds_paragraphs_from_sparse_lines_and_keeps_labels_apart() {
        let parser = TesseractOcr::new("tesseract", "paragraph");
        let frame = Frame::new(1, Rect::new(0, 0, 1200, 900), 1200, 900, Vec::new());
        // Sparse mode puts every line in its own block.
        let tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n\
                   5\t1\t20\t1\t1\t1\t387\t585\t90\t21\t88\tTasks\n\
                   5\t1\t21\t1\t1\t1\t741\t585\t123\t21\t60\tRewards\n\
                   5\t1\t24\t1\t1\t1\t249\t717\t180\t24\t84\tboots provide\n\
                   5\t1\t25\t1\t1\t1\t1148\t690\t20\t48\t38\tK\n\
                   5\t1\t26\t1\t1\t1\t249\t744\t160\t24\t90\tfall damage.\n";
        let blocks = parser.parse_tsv(&frame, tsv);
        let texts = blocks
            .iter()
            .map(|block| block.source_text.as_str())
            .collect::<Vec<_>>();

        assert_eq!(texts, ["Tasks", "Rewards", "boots provide fall damage."]);
    }

    #[test]
    fn drops_stray_glyphs_and_distant_buttons() {
        let parser = TesseractOcr::new("tesseract", "paragraph");
        let frame = Frame::new(1, Rect::new(0, 0, 1200, 900), 1200, 900, Vec::new());
        let tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n\
                   5\t1\t1\t1\t1\t1\t300\t40\t300\t21\t80\tBookshelf of Stability\n\
                   5\t1\t1\t1\t1\t2\t920\t40\t15\t21\t60\t+\n\
                   5\t1\t1\t1\t1\t3\t960\t40\t15\t21\t60\tx\n\
                   5\t1\t2\t1\t1\t1\t300\t200\t15\t21\t60\t>\n\
                   5\t1\t2\t1\t1\t2\t320\t200\t200\t21\t90\tthere is a\n\
                   5\t1\t2\t1\t1\t3\t530\t200\t15\t21\t90\ta\n";
        let blocks = parser.parse_tsv(&frame, tsv);

        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].source_text, "Bookshelf of Stability");
        assert_eq!(blocks[1].source_text, "there is a a");
    }

    #[test]
    fn merges_passes_keeping_the_more_confident_reading() {
        let bright = vec![
            TextBlock::new(Rect::new(100, 20, 150, 20), "Game Menu", 0.6),
            TextBlock::new(Rect::new(100, 80, 190, 20), "Back to Game", 0.7),
        ];
        let normal = vec![
            TextBlock::new(Rect::new(102, 21, 148, 19), "Eaméflenu", 0.5),
            TextBlock::new(Rect::new(30, 300, 400, 90), "Tip Old gold", 0.6),
        ];
        let merged = merge_passes(vec![bright, normal]);
        let texts = merged
            .iter()
            .map(|block| block.source_text.as_str())
            .collect::<Vec<_>>();
        assert_eq!(texts, ["Game Menu", "Back to Game", "Tip Old gold"]);
    }

    #[test]
    fn keeps_list_items_apart_but_joins_wrapped_prose() {
        let parser = TesseractOcr::new("tesseract", "paragraph");
        let frame = Frame::new(1, Rect::new(0, 0, 900, 900), 900, 900, Vec::new());
        let row = |block: u32, top: i32, width: u32, text: &str| {
            let left = if block == 7 || block == 9 { 20 } else { 40 };
            format!("5\t1\t{block}\t1\t1\t1\t{left}\t{top}\t{width}\t24\t90\t{text}\n")
        };
        let tsv = [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n".to_string(),
            // A menu: short items stacked closely.
            row(1, 100, 160, "Exploration"),
            row(2, 130, 150, "Gearing Up"),
            row(3, 160, 300, "Generating Resources"),
            // A list item, then an outdented heading.
            row(6, 220, 228, "Useful Items and Tips"),
            row(7, 250, 264, "Forgotten Technology"),
            // A long list item, then an outdented heading.
            row(8, 310, 400, "Logistics and Automation for Beginners"),
            row(9, 340, 380, "Ancient Machines"),
            // Wrapped prose.
            row(4, 400, 700, "The Leviathan can be found within the Water Orb"),
            row(5, 430, 140, "structure."),
        ]
        .concat();
        let texts = parser
            .parse_tsv(&frame, &tsv)
            .into_iter()
            .map(|block| block.source_text)
            .collect::<Vec<_>>();
        assert_eq!(
            texts,
            [
                "Exploration",
                "Gearing Up",
                "Generating Resources",
                "Useful Items and Tips",
                "Forgotten Technology",
                "Logistics and Automation for Beginners",
                "Ancient Machines",
                "The Leviathan can be found within the Water Orb structure."
            ]
        );
    }

    #[test]
    fn passes_that_agree_raise_confidence() {
        let first = vec![TextBlock::new(
            Rect::new(70, 420, 200, 30),
            "Gearing Up",
            0.3,
        )];
        let second = vec![TextBlock::new(
            Rect::new(68, 421, 205, 29),
            "Gearing Up",
            0.25,
        )];
        let merged = merge_passes(vec![first, second]);
        assert_eq!(merged.len(), 1);
        // Neither reading passes a 35% filter alone; together they do.
        assert!(merged[0].confidence > 0.45, "{}", merged[0].confidence);
    }

    #[test]
    fn faint_text_cut_off_sits_just_above_the_background() {
        // A dark sidebar (41) beside a slightly lighter panel (58–60), as in
        // the FTB quest book, with grey "locked" text at 86.
        let mut pixels = vec![41_u8; 300];
        pixels.extend(std::iter::repeat_n(58, 500));
        pixels.extend(std::iter::repeat_n(60, 150));
        pixels.extend(std::iter::repeat_n(86, 50));
        let level = faint_text_level(&pixels);
        assert!(level > 60 && level <= 86, "{level}");
    }

    #[test]
    fn drops_short_words_far_off_the_typical_text_size() {
        let parser = TesseractOcr::new("tesseract", "line");
        let frame = Frame::new(1, Rect::new(0, 0, 900, 900), 900, 900, Vec::new());
        let row = |block: u32, top: i32, height: u32, text: &str| {
            format!("5\t1\t{block}\t1\t1\t1\t40\t{top}\t200\t{height}\t90\t{text}\n")
        };
        let tsv = [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n".to_string(),
            row(1, 100, 22, "Storage"),
            row(2, 140, 24, "Magic"),
            row(3, 180, 22, "General Tech"),
            row(4, 300, 150, "POOR"),
            row(5, 500, 5, "rie"),
            row(6, 600, 60, "Forgotten Technology"),
        ]
        .concat();
        let texts = parser
            .parse_tsv(&frame, &tsv)
            .into_iter()
            .map(|block| block.source_text)
            .collect::<Vec<_>>();
        assert_eq!(
            texts,
            ["Storage", "Magic", "General Tech", "Forgotten Technology"]
        );
    }

    #[test]
    fn scales_hidpi_pixels_back_to_screen_space() {
        let parser = TesseractOcr::new("tesseract", "line");
        // A 200x100 screen area captured at 1.6x scale.
        let frame = Frame::new(1, Rect::new(1000, 50, 200, 100), 320, 160, Vec::new());
        let tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t160\t80\t64\t32\t90\tこんにちは\n";
        let blocks = parser.parse_tsv(&frame, tsv);

        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].bounds, Rect::new(1100, 100, 40, 20));
    }

    #[test]
    fn maps_ui_languages_to_tesseract_codes() {
        assert_eq!(TesseractOcr::language("Japanese"), "jpn");
        assert_eq!(TesseractOcr::language("ja"), "jpn");
        assert_eq!(TesseractOcr::language("zh-TW"), "chi_tra");
        assert_eq!(TesseractOcr::language("th-TH"), "tha");
    }
}
