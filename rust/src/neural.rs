//! Neural scene-text OCR using the PaddleOCR (PP-OCRv6) models through
//! ONNX Runtime.
//!
//! Unlike Tesseract, which was trained on scanned documents, these models
//! were trained on text in photos and screenshots, so they read pixel and
//! stylised game fonts without per-font tuning. Text is found by a detection
//! model (a probability map of where text is), then each found line is read
//! by a recognition model.
//!
//! ONNX Runtime is loaded at run time, so Overlayr still starts without it
//! and falls back to Tesseract.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use ort::session::Session;
use ort::session::builder::GraphOptimizationLevel;
use ort::value::Tensor;

use crate::ocr::{OcrWord, blocks_from_words};
use crate::services::{OcrBackend, ServiceResult};
use crate::text::clean_text;
use crate::types::{Frame, RejectedText, TextBlock};

pub const DETECTION_MODEL: &str = "PP-OCRv6_det_small.onnx";
pub const RECOGNITION_MODEL: &str = "PP-OCRv6_rec_small.onnx";
#[cfg(target_os = "macos")]
pub const RUNTIME_LIBRARY: &str = "libonnxruntime.dylib";
#[cfg(windows)]
pub const RUNTIME_LIBRARY: &str = "onnxruntime.dll";
#[cfg(not(any(target_os = "macos", windows)))]
pub const RUNTIME_LIBRARY: &str = "libonnxruntime.so";
/// Where the system's package manager puts ONNX Runtime.
#[cfg(target_os = "macos")]
const SYSTEM_RUNTIME_LIBRARIES: &[&str] = &[
    "/opt/homebrew/lib/libonnxruntime.dylib",
    "/usr/local/lib/libonnxruntime.dylib",
];
#[cfg(windows)]
const SYSTEM_RUNTIME_LIBRARIES: &[&str] = &[];
#[cfg(not(any(target_os = "macos", windows)))]
const SYSTEM_RUNTIME_LIBRARIES: &[&str] = &[
    "/usr/lib/libonnxruntime.so",
    "/usr/lib64/libonnxruntime.so",
    "/usr/local/lib/libonnxruntime.so",
    "/usr/lib/x86_64-linux-gnu/libonnxruntime.so",
];

/// Detection runs with the image's shorter side scaled to about this many
/// pixels: large captures (1440p and up) are scaled down, which halves the
/// time without missing text; small ones are scaled up.
const DETECTION_SIDE: f32 = 900.0;
/// Upper bound on the detection input, which keeps 4K captures fast.
const MAX_DETECTION_SIDE: f32 = 2000.0;
/// Small crops are scaled up at most this much.
const MAX_DETECTION_SCALE: f32 = 2.0;
/// Probability above which a detection-map pixel counts as text.
const TEXT_PIXEL_THRESHOLD: f32 = 0.3;
/// Mean probability a found region needs to count as text.
const TEXT_REGION_THRESHOLD: f32 = 0.5;
/// How far a found region is grown, relative to its area over its perimeter.
/// The model marks a shrunken core of each line.
const UNCLIP_RATIO: f32 = 1.6;
const RECOGNITION_HEIGHT: u32 = 48;
const RECOGNITION_MIN_WIDTH: u32 = 320;
const RECOGNITION_BATCH: usize = 6;
/// Readings below this mean character probability are discarded.
const MIN_LINE_CONFIDENCE: f32 = 0.5;

/// Languages the multilingual PP-OCRv6 recognition model reads.
const PP_OCR_V6_LANGUAGES: &[&str] = &[
    "en", "zh", "zh-CN", "zh-TW", "ja", "af", "az", "bs", "ca", "cs", "cy", "da", "de", "es", "et",
    "eu", "fi", "fr", "ga", "gl", "hr", "hu", "id", "is", "it", "ku", "la", "lb", "lt", "lv", "mi",
    "ms", "mt", "nl", "no", "nb", "oc", "pl", "pt", "qu", "rm", "ro", "sk", "sl", "sq", "sv", "sw",
    "tl", "tr", "uz", "vi",
];

/// A text recognition model: its file, the model release folder it is
/// published in, and the languages it reads. PaddleOCR's own table of which
/// model reads which language (`paddleocr/_utils/langs.py`), in ISO 639
/// codes; earlier entries win.
pub struct RecognitionModel {
    pub file: &'static str,
    pub release: &'static str,
    languages: &'static [&'static str],
    /// The model reads right-to-left scripts in visual order, left to
    /// right, so its output is reversed into reading order.
    right_to_left: bool,
}

const fn script_model(
    file: &'static str,
    languages: &'static [&'static str],
    right_to_left: bool,
) -> RecognitionModel {
    RecognitionModel {
        file,
        release: "PP-OCRv5",
        languages,
        right_to_left,
    }
}

const RECOGNITION_MODELS: &[RecognitionModel] = &[
    RecognitionModel {
        file: RECOGNITION_MODEL,
        release: "PP-OCRv6",
        languages: PP_OCR_V6_LANGUAGES,
        right_to_left: false,
    },
    script_model("korean_PP-OCRv5_rec_mobile.onnx", &["ko"], false),
    script_model("th_PP-OCRv5_rec_mobile.onnx", &["th"], false),
    script_model("el_PP-OCRv5_rec_mobile.onnx", &["el"], false),
    script_model("ta_PP-OCRv5_rec_mobile.onnx", &["ta"], false),
    script_model("te_PP-OCRv5_rec_mobile.onnx", &["te"], false),
    // East Slavic languages have their own, more accurate model.
    script_model("eslav_PP-OCRv5_rec_mobile.onnx", &["ru", "uk", "be"], false),
    script_model(
        "cyrillic_PP-OCRv5_rec_mobile.onnx",
        &[
            "bg", "sr", "mk", "mn", "kk", "ky", "tg", "tt", "ba", "cv", "os", "kv", "av", "ce",
            "sah", "udm", "kbd", "ady", "inh", "lez", "bua", "tyv", "kaa",
        ],
        false,
    ),
    script_model(
        "arabic_PP-OCRv5_rec_mobile.onnx",
        &["ar", "fa", "ur", "ug", "ps", "sd", "bal"],
        true,
    ),
    script_model(
        "devanagari_PP-OCRv5_rec_mobile.onnx",
        &["hi", "mr", "ne", "sa", "mai", "bho", "new", "gom"],
        false,
    ),
];

/// The recognition model that reads `language` ("th", "pt-BR"), matching the
/// full code first, then its primary language.
pub fn recognition_model(language: &str) -> Option<&'static RecognitionModel> {
    let reads = |code: &str| {
        RECOGNITION_MODELS.iter().find(|model| {
            model
                .languages
                .iter()
                .any(|known| known.eq_ignore_ascii_case(code))
        })
    };
    let language = language.trim();
    reads(language).or_else(|| reads(language.split(['-', '_']).next()?))
}

/// Per-user directory holding the models and, when the system doesn't
/// provide one, the ONNX Runtime library.
pub fn model_dir() -> Option<PathBuf> {
    Some(crate::ocr::data_dir()?.join("models"))
}

pub fn runtime_dir() -> Option<PathBuf> {
    Some(crate::ocr::data_dir()?.join("lib"))
}

/// Finds the ONNX Runtime library: an explicit `ORT_DYLIB_PATH`, the copy
/// Overlayr downloaded, the one shipped with Overlayr, then the system's.
pub fn runtime_library() -> Option<PathBuf> {
    let explicit = std::env::var_os("ORT_DYLIB_PATH").map(PathBuf::from);
    let downloaded = runtime_dir().map(|directory| directory.join(RUNTIME_LIBRARY));
    let bundled = crate::bundle::program_dir().map(|directory| directory.join(RUNTIME_LIBRARY));
    let system = SYSTEM_RUNTIME_LIBRARIES.iter().map(PathBuf::from);
    explicit
        .into_iter()
        .chain(downloaded)
        .chain(bundled)
        .chain(system)
        .find(|path| path.is_file())
}

/// The model file `name`: a downloaded copy (which may be newer), else the
/// one shipped with Overlayr.
pub fn model_file(name: &str) -> Option<PathBuf> {
    model_dir()
        .map(|directory| directory.join(name))
        .into_iter()
        .chain(crate::bundle::ocr_dir().map(|directory| directory.join(name)))
        .find(|path| path.is_file())
}

/// Whether the models that read `language` are present.
pub fn has_models(language: &str) -> bool {
    model_file(DETECTION_MODEL).is_some()
        && recognition_model(language).is_some_and(|model| model_file(model.file).is_some())
}

/// Whether the neural engine can read `language` now: its models and the
/// runtime are installed.
pub fn is_installed(language: &str) -> bool {
    has_models(language) && runtime_library().is_some()
}

/// Whether the neural engine can read `language` on this computer. ONNX
/// Runtime isn't built for Intel Macs, which use Tesseract.
pub fn supports_language(language: &str) -> bool {
    let intel_mac = cfg!(target_os = "macos") && std::env::consts::ARCH == "x86_64";
    !intel_mac && recognition_model(language).is_some()
}

/// Loads ONNX Runtime once per process.
fn load_runtime() -> ServiceResult<()> {
    static LOADED: OnceLock<Result<(), String>> = OnceLock::new();
    LOADED
        .get_or_init(|| {
            let path = runtime_library()
                .ok_or_else(|| "the ONNX Runtime library is not installed".to_string())?;
            ort::init_from(&path)
                .map_err(|error| format!("could not load {}: {error}", path.display()))?
                .with_name("overlayr")
                .commit();
            Ok(())
        })
        .clone()
}

/// The OCR thread setting from the performance settings.
static SESSION_THREADS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(2);

/// Sets the OCR threads for models loaded afterwards.
pub fn set_threads(threads: usize) {
    SESSION_THREADS.store(threads.clamp(1, 16), std::sync::atomic::Ordering::Relaxed);
}

/// Threads for the detection session. Measured on a 16-thread Ryzen with a
/// game screen: 4 threads took 210 ms and 600 ms of CPU, 8 took the same
/// time and 1,050 ms.
fn detection_threads(threads: usize) -> usize {
    threads.min(4)
}

/// Recognition sessions reading batches in parallel, one thread each.
/// Threads inside a session mostly wait on each other on these small
/// inputs: 6 single-threaded sessions read 82 lines in 450 ms with 2.3 s of
/// CPU, where 2 sessions of 8 threads took 435 ms and 3.4 s. Gains stop
/// after about 6.
fn recognition_workers(threads: usize) -> usize {
    (threads * 2).clamp(2, 6)
}

/// Recognised lines remembered by their pixels.
const RECOGNITION_CACHE: usize = 4_096;

fn open_session(path: &Path, threads: usize) -> ServiceResult<Session> {
    let mut builder = (|| -> ort::Result<_> {
        Ok(Session::builder()?
            .with_optimization_level(GraphOptimizationLevel::Level3)?
            .with_intra_threads(threads)?
            // Idle threads sleep instead of spinning, leaving the CPU to
            // the game between reads.
            .with_intra_op_spinning(false)?
            // Every frame and text line has its own size. The CPU arena and
            // memory patterns keep the largest buffers each size needed, so
            // memory grew with each new size; plain allocations are freed.
            .with_memory_pattern(false)?
            .with_execution_providers([ort::ep::CPU::default()
                .with_arena_allocator(false)
                .build()])?)
    })()
    .map_err(|error| error.to_string())?;
    builder
        .commit_from_file(path)
        .map_err(|error| format!("could not load {}: {error}", path.display()))
}

struct Models {
    detection: Session,
    /// Separate sessions read batches in parallel: one session doesn't
    /// speed up with more threads on these small inputs.
    recognition: Vec<Session>,
    /// Index 0 is the CTC blank; the last entry is a space.
    characters: Vec<String>,
    /// Readings are in visual order and are reversed; see
    /// [`RecognitionModel::right_to_left`].
    right_to_left: bool,
    /// Text and confidence of lines already read, by [`line_key`]. The
    /// same pixels always read the same, so lines that didn't change (most
    /// of them, when one row of a menu is hovered) skip recognition.
    cache: HashMap<u64, (String, f32)>,
}

impl Models {
    fn load(language: &str) -> ServiceResult<Self> {
        load_runtime()?;
        let model = recognition_model(language)
            .ok_or_else(|| format!("the neural OCR does not read '{language}'"))?;
        let missing = || "the neural OCR models are not installed".to_string();
        let detection_model = model_file(DETECTION_MODEL).ok_or_else(missing)?;
        let recognition_model = model_file(model.file).ok_or_else(missing)?;
        let threads = SESSION_THREADS.load(std::sync::atomic::Ordering::Relaxed);
        let detection = open_session(&detection_model, detection_threads(threads))?;
        let recognition = (0..recognition_workers(threads))
            .map(|_| open_session(&recognition_model, 1))
            .collect::<ServiceResult<Vec<_>>>()?;
        let dictionary = recognition[0]
            .metadata()
            .ok()
            .and_then(|metadata| metadata.custom("character"))
            .ok_or_else(|| "the recognition model has no character list".to_string())?;
        let characters = std::iter::once(String::new())
            .chain(dictionary.lines().map(str::to_string))
            .chain(std::iter::once(" ".to_string()))
            .collect();
        Ok(Self {
            detection,
            recognition,
            characters,
            right_to_left: model.right_to_left,
            cache: HashMap::new(),
        })
    }
}

/// PaddleOCR text detection and recognition. The models load on first use.
pub struct NeuralOcr {
    grouping_mode: String,
    language: String,
    models: Option<Models>,
    rejected: Vec<RejectedText>,
}

impl NeuralOcr {
    /// Reads `language`, which picks the recognition model.
    pub fn new(grouping_mode: impl Into<String>, language: impl Into<String>) -> Self {
        Self {
            grouping_mode: grouping_mode.into(),
            language: language.into(),
            models: None,
            rejected: Vec::new(),
        }
    }

    fn models(&mut self) -> ServiceResult<&mut Models> {
        if self.models.is_none() {
            self.models = Some(Models::load(&self.language)?);
        }
        Ok(self.models.as_mut().expect("models were just loaded"))
    }

    /// Reads every line of text in `frame`, in frame pixels.
    fn read_lines(&mut self, frame: &Frame) -> ServiceResult<Vec<Line>> {
        let models = self.models()?;
        let boxes = detect(&mut models.detection, frame)?;
        recognize(models, frame, &boxes)
    }
}

/// A recognised line of text in frame pixels.
#[derive(Debug)]
struct Line {
    area: PixelBox,
    text: String,
    confidence: f32,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct PixelBox {
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
}

impl PixelBox {
    fn width(self) -> u32 {
        self.right - self.left
    }

    fn height(self) -> u32 {
        self.bottom - self.top
    }
}

/// Scale and padded size of the detection input for a frame.
///
/// The scale comes from the whole capture, so a crop of it is scaled the
/// same: text reads the same in both, and a small crop costs little instead
/// of being enlarged. It is a multiple of 1/32, so a crop starting on a
/// 32-pixel grid samples exactly the capture's pixels.
fn detection_size(frame: &Frame) -> (f32, u32, u32) {
    let (width, height) = frame.source_size;
    let (short, long) = (width.min(height) as f32, width.max(height) as f32);
    let scale = (DETECTION_SIDE / short)
        .min(MAX_DETECTION_SCALE)
        .min(MAX_DETECTION_SIDE / long);
    let scale = ((scale * 32.0).floor() / 32.0).max(1.0 / 32.0);
    // The network needs sides that are multiples of 32; the image is
    // padded to them.
    let pad = |side: u32| ((side as f32 * scale / 32.0).ceil() as u32).max(1) * 32;
    (scale, pad(frame.width), pad(frame.height))
}

/// Samples the frame at `(x, y)` with bilinear filtering, as BGR normalised
/// to -1..1 like the models were trained on.
fn sample_bgr(frame: &Frame, x: f32, y: f32) -> [f32; 3] {
    let max_x = frame.width.saturating_sub(1) as f32;
    let max_y = frame.height.saturating_sub(1) as f32;
    let (x, y) = (x.clamp(0.0, max_x), y.clamp(0.0, max_y));
    let (x0, y0) = (x.floor(), y.floor());
    let (x1, y1) = ((x0 + 1.0).min(max_x), (y0 + 1.0).min(max_y));
    let (fx, fy) = (x - x0, y - y0);
    let pixel = |px: f32, py: f32| {
        let index = ((py as u32 * frame.width + px as u32) * 4) as usize;
        &frame.rgba[index..index + 3]
    };
    let (a, b, c, d) = (pixel(x0, y0), pixel(x1, y0), pixel(x0, y1), pixel(x1, y1));
    let mut bgr = [0.0; 3];
    for (channel, value) in bgr.iter_mut().enumerate() {
        let source = 2 - channel;
        let top = f32::from(a[source]) * (1.0 - fx) + f32::from(b[source]) * fx;
        let bottom = f32::from(c[source]) * (1.0 - fx) + f32::from(d[source]) * fx;
        *value = (top * (1.0 - fy) + bottom * fy) / 127.5 - 1.0;
    }
    bgr
}

/// Writes `area` of the frame, resized to `width` x `height`, into a planar
/// BGR tensor buffer whose rows are `stride` wide.
fn write_planar(
    frame: &Frame,
    area: PixelBox,
    width: u32,
    height: u32,
    stride: u32,
    rows: u32,
    output: &mut [f32],
) {
    let scale_x = area.width() as f32 / width as f32;
    let scale_y = area.height() as f32 / height as f32;
    let plane = (stride * rows) as usize;
    for y in 0..height {
        let source_y = area.top as f32 + (y as f32 + 0.5) * scale_y - 0.5;
        for x in 0..width {
            let source_x = area.left as f32 + (x as f32 + 0.5) * scale_x - 0.5;
            let bgr = sample_bgr(frame, source_x, source_y);
            let index = (y * stride + x) as usize;
            for (channel, value) in bgr.into_iter().enumerate() {
                output[channel * plane + index] = value;
            }
        }
    }
}

/// Finds text lines with the detection model.
fn detect(session: &mut Session, frame: &Frame) -> ServiceResult<Vec<PixelBox>> {
    if frame.width < 4 || frame.height < 4 {
        return Ok(Vec::new());
    }
    let (scale, width, height) = detection_size(frame);
    let mut input = vec![0.0_f32; (3 * width * height) as usize];
    let plane = (width * height) as usize;
    let content_width = ((frame.width as f32 * scale).ceil() as u32).min(width);
    let content_height = ((frame.height as f32 * scale).ceil() as u32).min(height);
    for y in 0..content_height {
        let source_y = (y as f32 + 0.5) / scale - 0.5;
        for x in 0..content_width {
            let source_x = (x as f32 + 0.5) / scale - 0.5;
            let index = (y * width + x) as usize;
            for (channel, value) in sample_bgr(frame, source_x, source_y)
                .into_iter()
                .enumerate()
            {
                input[channel * plane + index] = value;
            }
        }
    }
    let tensor = Tensor::from_array(([1_usize, 3, height as usize, width as usize], input))
        .map_err(|error| error.to_string())?;
    let outputs = session
        .run(ort::inputs![tensor])
        .map_err(|error| format!("text detection failed: {error}"))?;
    let (shape, map) = outputs[0]
        .try_extract_tensor::<f32>()
        .map_err(|error| error.to_string())?;
    let (map_height, map_width) = match shape.as_ref() {
        [.., h, w] => (*h as u32, *w as u32),
        _ => return Err("unexpected text detection output".into()),
    };
    Ok(boxes_from_map(map, map_width, map_height)
        .into_iter()
        .filter_map(|(left, top, right, bottom)| {
            // Map pixels to frame pixels; the map may be smaller than the input.
            let scale_x = width as f32 / map_width as f32 / scale;
            let scale_y = height as f32 / map_height as f32 / scale;
            let area = PixelBox {
                left: (left * scale_x).round().clamp(0.0, frame.width as f32) as u32,
                top: (top * scale_y).round().clamp(0.0, frame.height as f32) as u32,
                right: (right * scale_x).round().clamp(0.0, frame.width as f32) as u32,
                bottom: (bottom * scale_y).round().clamp(0.0, frame.height as f32) as u32,
            };
            (area.width() > 3 && area.height() > 3).then_some(area)
        })
        .collect())
}

/// Turns the detection probability map into text boxes, in map pixels:
/// connected regions of likely text, kept when confident enough on average
/// and grown back to the full line height.
fn boxes_from_map(map: &[f32], width: u32, height: u32) -> Vec<(f32, f32, f32, f32)> {
    let (w, h) = (width as usize, height as usize);
    if map.len() < w * h {
        return Vec::new();
    }
    // Threshold, then dilate by a 2x2 square so letters with thin gaps
    // between them join into one region.
    let text = map
        .iter()
        .map(|&p| p > TEXT_PIXEL_THRESHOLD)
        .collect::<Vec<_>>();
    let mut mask = text.clone();
    for y in 0..h {
        for x in 0..w {
            if text[y * w + x] {
                continue;
            }
            mask[y * w + x] = (x > 0 && text[y * w + x - 1])
                || (y > 0 && text[(y - 1) * w + x])
                || (x > 0 && y > 0 && text[(y - 1) * w + x - 1]);
        }
    }
    let mut label = vec![false; w * h];
    let mut boxes = Vec::new();
    let mut stack = Vec::new();
    for start in 0..w * h {
        if !mask[start] || label[start] {
            continue;
        }
        label[start] = true;
        stack.push(start);
        let (mut left, mut top, mut right, mut bottom) = (w, h, 0, 0);
        let mut pixels = Vec::new();
        while let Some(index) = stack.pop() {
            let (x, y) = (index % w, index / w);
            pixels.push(index);
            left = left.min(x);
            right = right.max(x);
            top = top.min(y);
            bottom = bottom.max(y);
            for (dx, dy) in [
                (-1, -1),
                (0, -1),
                (1, -1),
                (-1, 0),
                (1, 0),
                (-1, 1),
                (0, 1),
                (1, 1),
            ] {
                let (nx, ny) = (x as isize + dx, y as isize + dy);
                if nx < 0 || ny < 0 || nx >= w as isize || ny >= h as isize {
                    continue;
                }
                let next = ny as usize * w + nx as usize;
                if mask[next] && !label[next] {
                    label[next] = true;
                    stack.push(next);
                }
            }
        }
        let (box_width, box_height) = ((right - left + 1) as f32, (bottom - top + 1) as f32);
        if box_width.min(box_height) < 3.0 {
            continue;
        }
        let mut score = 0.0;
        for y in top..=bottom {
            for x in left..=right {
                score += map[y * w + x];
            }
        }
        if score / (box_width * box_height) < TEXT_REGION_THRESHOLD {
            continue;
        }
        let distance = box_width * box_height * UNCLIP_RATIO / (2.0 * (box_width + box_height));
        if box_width.min(box_height) + 2.0 * distance < 5.0 {
            continue;
        }
        boxes.push((
            (left as f32 - distance).max(0.0),
            (top as f32 - distance).max(0.0),
            (right as f32 + 1.0 + distance).min(width as f32),
            (bottom as f32 + 1.0 + distance).min(height as f32),
        ));
    }
    boxes
}

/// Reads each box with the recognition model, in batches of similar widths.
fn recognize(models: &mut Models, frame: &Frame, boxes: &[PixelBox]) -> ServiceResult<Vec<Line>> {
    let keys = boxes
        .iter()
        .map(|area| line_key(frame, *area))
        .collect::<Vec<_>>();
    let mut lines = Vec::with_capacity(boxes.len());
    let mut unread = Vec::new();
    for (index, key) in keys.iter().enumerate() {
        match models.cache.get(key) {
            Some((text, confidence)) => lines.push(Line {
                area: boxes[index],
                text: text.clone(),
                confidence: *confidence,
            }),
            None => unread.push(index),
        }
    }
    let read = read_boxes(models, frame, boxes, unread)?;
    if models.cache.len() + read.len() > RECOGNITION_CACHE {
        models.cache.clear();
    }
    for (index, mut line) in read {
        if models.right_to_left {
            line.text = reading_order(&line.text);
        }
        models
            .cache
            .insert(keys[index], (line.text.clone(), line.confidence));
        lines.push(line);
    }
    Ok(lines)
}

/// Grid, in capture pixels, that cached lines are snapped to.
const LINE_KEY_GRID: u32 = 8;

/// Identifies a line by the pixels around it: its box (plus the pixel of
/// border that resampling reads) grown to an 8-pixel grid of the capture.
/// Detection boxes shift by a pixel between reads; on the grid an
/// unchanged line nearly always gets the same key. The same pixels hold the
/// same text, so its earlier reading can be reused.
fn line_key(frame: &Frame, area: PixelBox) -> u64 {
    use std::hash::{Hash, Hasher};
    let (origin_x, origin_y) = frame.origin;
    let snap_down = |value: u32, origin: u32| {
        ((origin + value).saturating_sub(1) / LINE_KEY_GRID * LINE_KEY_GRID).max(origin) - origin
    };
    let snap_up = |value: u32, origin: u32, limit: u32| {
        ((origin + value + 1).div_ceil(LINE_KEY_GRID) * LINE_KEY_GRID - origin).min(limit)
    };
    let left = snap_down(area.left, origin_x);
    let top = snap_down(area.top, origin_y);
    let right = snap_up(area.right, origin_x, frame.width);
    let bottom = snap_up(area.bottom, origin_y, frame.height);
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    (right - left, bottom - top).hash(&mut hasher);
    for y in top..bottom {
        let start = ((y * frame.width + left) * 4) as usize;
        let end = ((y * frame.width + right) * 4) as usize;
        hasher.write(&frame.rgba[start..end]);
    }
    hasher.finish()
}

/// Reads the boxes at `indices` with the recognition sessions in parallel,
/// returning each line with its index.
fn read_boxes(
    models: &mut Models,
    frame: &Frame,
    boxes: &[PixelBox],
    mut order: Vec<usize>,
) -> ServiceResult<Vec<(usize, Line)>> {
    if order.is_empty() {
        return Ok(Vec::new());
    }
    let ratio = |area: &PixelBox| area.width() as f32 / area.height().max(1) as f32;
    order.sort_by(|&a, &b| ratio(&boxes[a]).total_cmp(&ratio(&boxes[b])));
    // Widest (slowest) batches first, so the workers finish together.
    let batches = order.chunks(RECOGNITION_BATCH).rev().collect::<Vec<_>>();
    let next = std::sync::atomic::AtomicUsize::new(0);
    let characters = &models.characters;
    let results = std::thread::scope(|scope| {
        let workers = models
            .recognition
            .iter_mut()
            .map(|session| {
                let (batches, next) = (&batches, &next);
                scope.spawn(move || {
                    let mut lines = Vec::new();
                    loop {
                        let index = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        let Some(batch) = batches.get(index) else {
                            return Ok(lines);
                        };
                        lines.extend(recognize_batch(session, characters, frame, boxes, batch)?);
                    }
                })
            })
            .collect::<Vec<_>>();
        workers
            .into_iter()
            .map(|worker| {
                worker
                    .join()
                    .unwrap_or_else(|_| Err("text recognition crashed".to_string()))
            })
            .collect::<Vec<ServiceResult<Vec<(usize, Line)>>>>()
    });
    let mut lines = Vec::with_capacity(order.len());
    for result in results {
        lines.extend(result?);
    }
    Ok(lines)
}

/// Reads one batch of similarly shaped boxes.
fn recognize_batch(
    session: &mut Session,
    characters: &[String],
    frame: &Frame,
    boxes: &[PixelBox],
    batch: &[usize],
) -> ServiceResult<Vec<(usize, Line)>> {
    let ratio = |area: &PixelBox| area.width() as f32 / area.height().max(1) as f32;
    let widest = batch.iter().map(|&index| ratio(&boxes[index])).fold(
        RECOGNITION_MIN_WIDTH as f32 / RECOGNITION_HEIGHT as f32,
        f32::max,
    );
    let stride = (RECOGNITION_HEIGHT as f32 * widest).ceil() as u32;
    let image_size = (3 * RECOGNITION_HEIGHT * stride) as usize;
    let mut input = vec![0.0_f32; image_size * batch.len()];
    for (slot, &index) in batch.iter().enumerate() {
        let area = boxes[index];
        let width = ((RECOGNITION_HEIGHT as f32 * ratio(&area)).ceil() as u32).clamp(1, stride);
        write_planar(
            frame,
            area,
            width,
            RECOGNITION_HEIGHT,
            stride,
            RECOGNITION_HEIGHT,
            &mut input[slot * image_size..(slot + 1) * image_size],
        );
    }
    let shape = [batch.len(), 3, RECOGNITION_HEIGHT as usize, stride as usize];
    let tensor = Tensor::from_array((shape, input)).map_err(|error| error.to_string())?;
    let outputs = session
        .run(ort::inputs![tensor])
        .map_err(|error| format!("text recognition failed: {error}"))?;
    let (shape, scores) = outputs[0]
        .try_extract_tensor::<f32>()
        .map_err(|error| error.to_string())?;
    let (steps, classes) = match shape.as_ref() {
        [_, steps, classes] => (*steps as usize, *classes as usize),
        _ => return Err("unexpected text recognition output".into()),
    };
    Ok(batch
        .iter()
        .enumerate()
        .filter_map(|(slot, &index)| {
            let start = slot * steps * classes;
            let sequence = scores.get(start..start + steps * classes)?;
            let (text, confidence) = decode(sequence, classes, characters);
            Some((
                index,
                Line {
                    area: boxes[index],
                    text,
                    confidence,
                },
            ))
        })
        .collect())
}

/// A right-to-left reading in visual order (left to right) put into
/// reading order. Runs of Latin letters, digits and their punctuation read
/// left to right inside right-to-left text, so they keep their order, as in
/// PaddleOCR's own decoder.
fn reading_order(visual: &str) -> String {
    let left_to_right =
        |character: char| character.is_ascii_alphanumeric() || " :*./%+-".contains(character);
    let mut runs: Vec<String> = Vec::new();
    let mut run = String::new();
    for character in visual.chars() {
        if left_to_right(character) {
            run.push(character);
        } else {
            if !run.is_empty() {
                runs.push(std::mem::take(&mut run));
            }
            runs.push(character.to_string());
        }
    }
    if !run.is_empty() {
        runs.push(run);
    }
    runs.into_iter().rev().collect()
}

/// Greedy CTC decoding: the likeliest class per step, with repeats and
/// blanks removed. Confidence is the mean probability of kept characters.
fn decode(sequence: &[f32], classes: usize, characters: &[String]) -> (String, f32) {
    let mut text = String::new();
    let (mut total, mut count) = (0.0, 0);
    let mut previous = usize::MAX;
    for step in sequence.chunks_exact(classes) {
        let (best, probability) = step
            .iter()
            .copied()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(&b.1))
            .unwrap_or((0, 0.0));
        if best != 0
            && best != previous
            && let Some(character) = characters.get(best)
        {
            text.push_str(character);
            total += probability;
            count += 1;
        }
        previous = best;
    }
    let confidence = if count == 0 {
        0.0
    } else {
        total / count as f32
    };
    (text.trim().to_string(), confidence)
}

/// Detection boxes are padded around the glyphs, by about a sixth of their
/// height on each side. Layout (paragraph joining, background clearing,
/// font size) needs the glyphs' own bounds.
fn glyph_bounds(area: PixelBox) -> PixelBox {
    let padding = area.height() / 6;
    PixelBox {
        left: (area.left + padding).min(area.right - 1),
        top: area.top + padding,
        right: area.right.saturating_sub(padding).max(area.left + 1),
        bottom: area.bottom - padding,
    }
}

/// Narrows a line's bounds to the columns holding its text. Detection boxes
/// of long lines keep a few pixels of padding at the ends, more so when the
/// capture was scaled down for detection, and a translation drawn from them
/// starts left of the original. Columns that differ from the line's
/// background (its most common colour) hold text; only the padding is
/// trimmed, so busy backgrounds leave the bounds as they were.
fn ink_columns(frame: &Frame, area: PixelBox) -> PixelBox {
    const INK: i32 = 48;
    let right = area.right.min(frame.width);
    let bottom = area.bottom.min(frame.height);
    if area.left + 4 >= right || area.top + 2 >= bottom {
        return area;
    }
    let pixel = |x: u32, y: u32| {
        let index = ((y * frame.width + x) * 4) as usize;
        [
            i32::from(frame.rgba[index]),
            i32::from(frame.rgba[index + 1]),
            i32::from(frame.rgba[index + 2]),
        ]
    };
    let mut counts = std::collections::HashMap::<[i32; 3], u32>::new();
    for y in area.top..bottom {
        for x in area.left..right {
            *counts
                .entry(pixel(x, y).map(|channel| channel >> 4))
                .or_default() += 1;
        }
    }
    let Some(background) = counts
        .into_iter()
        .max_by_key(|(_, count)| *count)
        .map(|(bucket, _)| bucket.map(|channel| (channel << 4) + 8))
    else {
        return area;
    };
    let has_ink = |x: u32| {
        (area.top..bottom).any(|y| {
            pixel(x, y)
                .iter()
                .zip(background)
                .any(|(channel, back)| (channel - back).abs() > INK)
        })
    };
    let most = (bottom - area.top) / 2;
    let left = (area.left..right).find(|&x| has_ink(x));
    let last = (area.left..right).rev().find(|&x| has_ink(x));
    match (left, last) {
        (Some(left), Some(last)) if left < last => PixelBox {
            left: if left - area.left <= most {
                left
            } else {
                area.left
            },
            right: if right - 1 - last <= most {
                last + 1
            } else {
                area.right
            },
            ..area
        },
        _ => area,
    }
}

/// Splits a recognised line into words with estimated positions, so the
/// shared noise filters and paragraph grouping can work on it.
fn words_of(line: &Line, area: PixelBox) -> Vec<OcrWord> {
    let characters = line.text.chars().count().max(1) as f32;
    let per_character = area.width() as f32 / characters;
    let mut words = Vec::new();
    let mut offset = 0;
    for piece in line.text.split(' ') {
        let length = piece.chars().count();
        let text = clean_text(piece);
        if !text.is_empty() {
            words.push(OcrWord {
                left: area.left as i32 + (offset as f32 * per_character) as i32,
                top: area.top as i32,
                width: ((length as f32 * per_character).round() as u32).max(1),
                height: area.height(),
                confidence: line.confidence,
                text,
            });
        }
        offset += length + 1;
    }
    words
}

impl OcrBackend for NeuralOcr {
    fn recognize(&mut self, frame: &Frame, _language: &str) -> ServiceResult<Vec<TextBlock>> {
        let lines = self.read_lines(frame)?;
        let mut rejected = Vec::new();
        let mut kept = Vec::new();
        for line in lines {
            if line.text.is_empty() {
                continue;
            }
            if line.confidence < MIN_LINE_CONFIDENCE {
                rejected.push(RejectedText {
                    text: line.text.clone(),
                    bounds: frame.to_screen(
                        line.area.left as i32,
                        line.area.top as i32,
                        line.area.width(),
                        line.area.height(),
                    ),
                    confidence: line.confidence,
                    reason: "Unsure reading",
                });
                continue;
            }
            let area = ink_columns(frame, glyph_bounds(line.area));
            kept.push(words_of(&line, area));
        }
        let (blocks, filtered) = blocks_from_words(frame, kept, &self.grouping_mode);
        rejected.extend(filtered);
        self.rejected = rejected;
        Ok(blocks)
    }

    fn take_rejected(&mut self) -> Vec<RejectedText> {
        std::mem::take(&mut self.rejected)
    }

    fn name(&self) -> &'static str {
        "PaddleOCR"
    }

    fn validate_language(&self, language: &str) -> ServiceResult<()> {
        if !supports_language(language) {
            return Err(format!("the neural OCR does not read '{language}'"));
        }
        if !has_models(language) {
            return Err("the neural OCR models are not installed".into());
        }
        load_runtime()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Times reading a screenshot: cold, again unchanged, with one row
    /// "hovered" and as a cropped region. Run with:
    /// `OVERLAYR_BENCH_IMAGE=shot.png cargo test --release --features native-ocr --lib bench_ocr -- --ignored --nocapture`
    #[test]
    #[ignore]
    fn bench_ocr() {
        let path = std::env::var("OVERLAYR_BENCH_IMAGE").expect("OVERLAYR_BENCH_IMAGE");
        let image = image::open(&path).expect("image").to_rgba8();
        let (width, height) = image.dimensions();
        let frame = Frame::new(
            1,
            crate::types::Rect::new(0, 0, width, height),
            width,
            height,
            image.into_raw(),
        );
        let cpu = || {
            let stat = std::fs::read_to_string("/proc/self/stat").unwrap();
            let fields: Vec<&str> = stat
                .rsplit_once(')')
                .unwrap()
                .1
                .split_whitespace()
                .collect();
            (fields[11].parse::<u64>().unwrap() + fields[12].parse::<u64>().unwrap()) * 10
        };
        // A highlighted row: brighten a band across the middle.
        let mut hovered = frame.clone();
        let band = hovered.height / 2..hovered.height / 2 + 30;
        for y in band {
            for x in 0..hovered.width {
                let index = ((y * hovered.width + x) * 4) as usize;
                for channel in 0..3 {
                    hovered.rgba[index + channel] =
                        hovered.rgba[index + channel].saturating_add(40);
                }
            }
        }
        // On the 32-pixel grid, as the pipeline crops.
        let region = frame.crop(crate::types::Rect::new(
            width as i32 / 4 / 32 * 32,
            height as i32 / 4 / 32 * 32,
            width / 2,
            height / 2,
        ));
        for setting in [8, 2] {
            set_threads(setting);
            let language = std::env::var("OVERLAYR_BENCH_LANGUAGE").unwrap_or_else(|_| "en".into());
            let mut models = Models::load(&language).expect("models");
            let mut read = |label: &str, frame: &Frame| {
                let (start, cpu_start) = (std::time::Instant::now(), cpu());
                let boxes = detect(&mut models.detection, frame).unwrap();
                let (middle, cpu_middle) = (std::time::Instant::now(), cpu());
                let cached = boxes
                    .iter()
                    .filter(|area| models.cache.contains_key(&line_key(frame, **area)))
                    .count();
                recognize(&mut models, frame, &boxes).unwrap();
                println!(
                    "setting {setting} {label:>9}: {:>3} lines ({cached:>3} cached) | detect {:>4.0} ms, {:>5} ms cpu | recognize {:>4.0} ms, {:>5} ms cpu",
                    boxes.len(),
                    (middle - start).as_secs_f64() * 1000.0,
                    cpu_middle - cpu_start,
                    middle.elapsed().as_secs_f64() * 1000.0,
                    cpu() - cpu_middle,
                );
            };
            read("warm-up", &frame);
            read("unchanged", &frame);
            read("hovered", &hovered);
            read("region", &region);
        }
    }

    /// Prints what the engine reads in an image. Run with:
    /// `OVERLAYR_BENCH_IMAGE=shot.png OVERLAYR_BENCH_LANGUAGE=th cargo test --release --features native-ocr --lib reads_image -- --ignored --nocapture`
    #[test]
    #[ignore]
    fn reads_image() {
        let path = std::env::var("OVERLAYR_BENCH_IMAGE").expect("OVERLAYR_BENCH_IMAGE");
        let language = std::env::var("OVERLAYR_BENCH_LANGUAGE").unwrap_or_else(|_| "en".into());
        let image = image::open(&path).expect("image").to_rgba8();
        let (width, height) = image.dimensions();
        let frame = Frame::new(
            1,
            crate::types::Rect::new(0, 0, width, height),
            width,
            height,
            image.into_raw(),
        );
        let mut ocr = NeuralOcr::new("line", &language);
        for line in ocr.read_lines(&frame).expect("read") {
            println!("{:.2} {}", line.confidence, line.text);
        }
    }

    #[test]
    fn picks_the_model_that_reads_each_language() {
        let file = |language| recognition_model(language).map(|model| model.file);
        assert_eq!(file("ja"), Some(RECOGNITION_MODEL));
        assert_eq!(file("pt-BR"), Some(RECOGNITION_MODEL));
        assert_eq!(file("zh-TW"), Some(RECOGNITION_MODEL));
        assert_eq!(file("th"), Some("th_PP-OCRv5_rec_mobile.onnx"));
        assert_eq!(file("ko"), Some("korean_PP-OCRv5_rec_mobile.onnx"));
        assert_eq!(file("ru"), Some("eslav_PP-OCRv5_rec_mobile.onnx"));
        assert_eq!(file("bg"), Some("cyrillic_PP-OCRv5_rec_mobile.onnx"));
        assert_eq!(file("ar"), Some("arabic_PP-OCRv5_rec_mobile.onnx"));
        assert_eq!(file("hi"), Some("devanagari_PP-OCRv5_rec_mobile.onnx"));
        assert_eq!(file("am"), None);
    }

    #[test]
    fn puts_right_to_left_readings_in_reading_order() {
        assert_eq!(reading_order("cba"), "cba");
        assert_eq!(
            reading_order("\u{645}\u{644}\u{643}"),
            "\u{643}\u{644}\u{645}"
        );
        assert_eq!(
            reading_order("\u{628} HP 50\u{627}"),
            "\u{627} HP 50\u{628}"
        );
    }

    #[test]
    fn decodes_ctc_steps_without_blanks_or_repeats() {
        let characters = ["", "a", "b", " "].map(String::from).to_vec();
        // Steps: a, a, blank, a, b, space, b
        let steps = [1, 1, 0, 1, 2, 3, 2];
        let mut sequence = Vec::new();
        for best in steps {
            let mut step = vec![0.0; 4];
            step[best] = 0.9;
            sequence.extend(step);
        }
        let (text, confidence) = decode(&sequence, 4, &characters);
        assert_eq!(text, "aab b");
        assert!((confidence - 0.9).abs() < 1e-5);
    }

    #[test]
    fn finds_and_grows_text_regions() {
        let (width, height) = (40, 20);
        let mut map = vec![0.0; width * height];
        for y in 8..12 {
            for x in 5..30 {
                map[y * width + x] = 0.9;
            }
        }
        // A faint smudge isn't text.
        map[2 * width + 35] = 0.4;
        let boxes = boxes_from_map(&map, width as u32, height as u32);
        assert_eq!(boxes.len(), 1);
        let (left, top, right, bottom) = boxes[0];
        assert!(left < 5.0 && top < 8.0 && right > 30.0 && bottom > 12.0);
    }

    fn blank(width: u32, height: u32) -> Frame {
        Frame::new(
            1,
            crate::types::Rect::new(0, 0, width, height),
            width,
            height,
            vec![0; (width * height * 4) as usize],
        )
    }

    #[test]
    fn detection_input_is_a_multiple_of_32_and_bounded() {
        // A 1440p capture is scaled down to about 900 pixels tall.
        let (scale, width, height) = detection_size(&blank(2560, 1440));
        assert_eq!((width % 32, height % 32), (0, 0));
        assert!(
            scale < 1.0 && height.abs_diff(900) <= 32,
            "{width}x{height}"
        );
        let (scale, _, _) = detection_size(&blank(3840, 2160));
        assert!(scale < 0.5);
        // Small captures are scaled up, within limits.
        let (scale, width, _) = detection_size(&blank(300, 40));
        assert!(scale <= MAX_DETECTION_SCALE && width <= 608);
        let (_, width, _) = detection_size(&blank(2560, 120));
        assert!(width <= 2016);
    }

    #[test]
    fn crops_are_scaled_like_their_capture() {
        let capture = blank(2560, 1440);
        let crop = capture.crop(crate::types::Rect::new(320, 640, 600, 64));
        let (capture_scale, _, _) = detection_size(&capture);
        let (crop_scale, width, height) = detection_size(&crop);
        assert_eq!(crop_scale, capture_scale);
        assert_eq!((width % 32, height % 32), (0, 0));
        assert!(width < 600 && height <= 64, "{width}x{height}");
    }

    #[test]
    fn line_keys_ignore_small_box_shifts_but_not_new_pixels() {
        let mut capture = blank(400, 100);
        for (index, value) in capture.rgba.iter_mut().enumerate() {
            *value = (index * 7 % 251) as u8;
        }
        let crop = capture.crop(crate::types::Rect::new(32, 16, 300, 60));
        let at = |left, top, right, bottom| PixelBox {
            left,
            top,
            right,
            bottom,
        };
        // The same line, found a pixel off, in the capture and in a crop.
        let key = line_key(&capture, at(42, 30, 150, 50));
        assert_eq!(line_key(&capture, at(43, 30, 149, 51)), key);
        assert_eq!(line_key(&crop, at(10, 14, 118, 34)), key);
        // A changed pixel inside the line is a new line.
        let mut changed = capture.clone();
        let index = ((40 * 400 + 100) * 4) as usize;
        changed.rgba[index] = changed.rgba[index].wrapping_add(1);
        assert_ne!(line_key(&changed, at(42, 30, 150, 50)), key);
    }

    #[test]
    fn trims_line_bounds_to_the_text() {
        let (width, height) = (100_u32, 20_u32);
        let mut rgba = [10_u8, 30, 38, 255].repeat((width * height) as usize);
        for y in 6..14 {
            for x in (20..80).step_by(3) {
                let index = ((y * width + x) * 4) as usize;
                rgba[index..index + 3].copy_from_slice(&[240, 240, 240]);
            }
        }
        let frame = Frame::new(
            1,
            crate::types::Rect::new(0, 0, width, height),
            width,
            height,
            rgba,
        );
        let area = |left, right| PixelBox {
            left,
            top: 4,
            right,
            bottom: 16,
        };
        let trimmed = ink_columns(&frame, area(15, 82));
        assert_eq!((trimmed.left, trimmed.right), (20, 78));
        // Text beyond the padding a box can have is left alone.
        let wide = ink_columns(&frame, area(0, 99));
        assert_eq!((wide.left, wide.right), (0, 99));
    }

    #[test]
    fn splits_lines_into_positioned_words() {
        let line = Line {
            area: PixelBox {
                left: 100,
                top: 10,
                right: 200,
                bottom: 30,
            },
            text: "Back to Game".into(),
            confidence: 0.99,
        };
        let words = words_of(&line, glyph_bounds(line.area));
        assert_eq!(
            words
                .iter()
                .map(|word| word.text.as_str())
                .collect::<Vec<_>>(),
            ["Back", "to", "Game"]
        );
        assert_eq!(
            (words[0].left, words[0].top, words[0].height),
            (103, 13, 14)
        );
        assert!(words[2].left + words[2].width as i32 <= 198);
    }
}
