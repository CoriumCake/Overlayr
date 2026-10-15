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
pub const RUNTIME_LIBRARY: &str = "libonnxruntime.so";

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

/// Languages the multilingual PP-OCRv6 models read.
const SUPPORTED_LANGUAGES: &[&str] = &[
    "en", "zh", "zh-CN", "zh-TW", "ja", "af", "az", "bs", "ca", "cs", "cy", "da", "de", "es", "et",
    "eu", "fi", "fr", "ga", "gl", "hr", "hu", "id", "is", "it", "ku", "la", "lb", "lt", "lv", "mi",
    "ms", "mt", "nl", "no", "nb", "oc", "pl", "pt", "qu", "rm", "ro", "sk", "sl", "sq", "sv", "sw",
    "tl", "tr", "uz", "vi",
];

/// Per-user directory holding the models and, when the system doesn't
/// provide one, the ONNX Runtime library.
pub fn model_dir() -> Option<PathBuf> {
    Some(crate::ocr::data_dir()?.join("models"))
}

pub fn runtime_dir() -> Option<PathBuf> {
    Some(crate::ocr::data_dir()?.join("lib"))
}

/// Finds the ONNX Runtime library: an explicit `ORT_DYLIB_PATH`, the copy
/// Overlayr downloaded, then the system's.
pub fn runtime_library() -> Option<PathBuf> {
    let explicit = std::env::var_os("ORT_DYLIB_PATH").map(PathBuf::from);
    let downloaded = runtime_dir().map(|directory| directory.join(RUNTIME_LIBRARY));
    let system = [
        "/usr/lib/libonnxruntime.so",
        "/usr/lib64/libonnxruntime.so",
        "/usr/local/lib/libonnxruntime.so",
        "/usr/lib/x86_64-linux-gnu/libonnxruntime.so",
    ]
    .map(PathBuf::from);
    explicit
        .into_iter()
        .chain(downloaded)
        .chain(system)
        .find(|path| path.is_file())
}

pub fn has_models() -> bool {
    model_dir().is_some_and(|directory| {
        directory.join(DETECTION_MODEL).is_file() && directory.join(RECOGNITION_MODEL).is_file()
    })
}

/// Whether the neural engine can run: models and runtime are installed.
pub fn is_installed() -> bool {
    has_models() && runtime_library().is_some()
}

pub fn supports_language(language: &str) -> bool {
    SUPPORTED_LANGUAGES
        .iter()
        .any(|supported| supported.eq_ignore_ascii_case(language))
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

/// Threads per model session, from the performance settings. Four were
/// barely faster than two (650 vs 820 ms per screen) but used 60% more CPU,
/// which games need more.
static SESSION_THREADS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(2);

/// Sets the threads per model session for models loaded afterwards.
pub fn set_threads(threads: usize) {
    SESSION_THREADS.store(threads.clamp(1, 16), std::sync::atomic::Ordering::Relaxed);
}
/// Recognition sessions reading batches in parallel.
const RECOGNITION_WORKERS: usize = 2;

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
}

impl Models {
    fn load() -> ServiceResult<Self> {
        load_runtime()?;
        let directory = model_dir()
            .ok_or_else(|| "could not determine your user data directory".to_string())?;
        let threads = SESSION_THREADS.load(std::sync::atomic::Ordering::Relaxed);
        let detection = open_session(&directory.join(DETECTION_MODEL), threads)?;
        let recognition = (0..RECOGNITION_WORKERS)
            .map(|_| open_session(&directory.join(RECOGNITION_MODEL), threads))
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
        })
    }
}

/// PaddleOCR text detection and recognition. The models load on first use.
pub struct NeuralOcr {
    grouping_mode: String,
    models: Option<Models>,
    rejected: Vec<RejectedText>,
}

impl NeuralOcr {
    pub fn new(grouping_mode: impl Into<String>) -> Self {
        Self {
            grouping_mode: grouping_mode.into(),
            models: None,
            rejected: Vec::new(),
        }
    }

    fn models(&mut self) -> ServiceResult<&mut Models> {
        if self.models.is_none() {
            self.models = Some(Models::load()?);
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
fn detection_size(width: u32, height: u32) -> (f32, u32, u32) {
    let (short, long) = (width.min(height) as f32, width.max(height) as f32);
    let scale = (DETECTION_SIDE / short)
        .min(MAX_DETECTION_SCALE)
        .min(MAX_DETECTION_SIDE / long);
    // The network needs sides that are multiples of 32.
    let round = |side: u32| (((side as f32 * scale) / 32.0).round() as u32).max(1) * 32;
    (scale, round(width), round(height))
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
    let (_, width, height) = detection_size(frame.width, frame.height);
    let mut input = vec![0.0_f32; (3 * width * height) as usize];
    let whole = PixelBox {
        left: 0,
        top: 0,
        right: frame.width,
        bottom: frame.height,
    };
    write_planar(frame, whole, width, height, width, height, &mut input);
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
            let scale_x = frame.width as f32 / map_width as f32;
            let scale_y = frame.height as f32 / map_height as f32;
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
    let ratio = |area: &PixelBox| area.width() as f32 / area.height().max(1) as f32;
    let mut order = (0..boxes.len()).collect::<Vec<_>>();
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
            .collect::<Vec<ServiceResult<Vec<Line>>>>()
    });
    let mut lines = Vec::with_capacity(boxes.len());
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
) -> ServiceResult<Vec<Line>> {
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
            Some(Line {
                area: boxes[index],
                text,
                confidence,
            })
        })
        .collect())
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

/// Splits a recognised line into words with estimated positions, so the
/// shared noise filters and paragraph grouping can work on it.
fn words_of(line: &Line) -> Vec<OcrWord> {
    let area = glyph_bounds(line.area);
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
            kept.push(words_of(&line));
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
        if !has_models() {
            return Err("the neural OCR models are not installed".into());
        }
        load_runtime()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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

    #[test]
    fn detection_input_is_a_multiple_of_32_and_bounded() {
        // A 1440p capture is scaled down to about 900 pixels tall.
        let (scale, width, height) = detection_size(2560, 1440);
        assert_eq!((width % 32, height % 32), (0, 0));
        assert!(
            scale < 1.0 && height.abs_diff(900) <= 32,
            "{width}x{height}"
        );
        let (scale, _, _) = detection_size(3840, 2160);
        assert!(scale < 0.5);
        // Small crops are scaled up, within limits.
        let (scale, width, _) = detection_size(300, 40);
        assert!(scale <= MAX_DETECTION_SCALE && width <= 608);
        let (_, width, _) = detection_size(2560, 120);
        assert!(width <= 2016);
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
        let words = words_of(&line);
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
