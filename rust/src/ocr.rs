use std::io::Write;
use std::process::{Command, Stdio};

use image::{DynamicImage, ImageFormat, RgbaImage};

use crate::services::{OcrBackend, ServiceResult};
use crate::text::{clean_text, correct_text, is_valid_text, merge_blocks};
use crate::types::{Frame, Rect, TextBlock};

/// OCR adapter for a locally installed Tesseract binary.
///
/// The image is streamed over stdin, so no captured screen contents are
/// written to disk. Install the language packs required by the user (for
/// example `tesseract-ocr-jpn` and `tesseract-ocr-tha` on Debian/Ubuntu).
#[derive(Debug, Clone)]
pub struct TesseractOcr {
    executable: String,
    grouping_mode: String,
}

#[cfg(feature = "native-ocr")]
pub struct NativeTesseractOcr {
    engine: Option<leptess::LepTess>,
    language: String,
    grouping_mode: String,
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
        }
    }
}

impl TesseractOcr {
    pub fn new(executable: impl Into<String>, grouping_mode: impl Into<String>) -> Self {
        Self {
            executable: executable.into(),
            grouping_mode: grouping_mode.into(),
        }
    }

    fn image_bytes(frame: &Frame) -> ServiceResult<Vec<u8>> {
        let image = RgbaImage::from_raw(frame.width, frame.height, frame.rgba.clone())
            .ok_or_else(|| "frame pixel buffer has an invalid RGBA length".to_string())?;
        let mut bytes = std::io::Cursor::new(Vec::new());
        DynamicImage::ImageRgba8(image)
            .write_to(&mut bytes, ImageFormat::Png)
            .map_err(|error| format!("could not encode frame for OCR: {error}"))?;
        Ok(bytes.into_inner())
    }

    fn language(language: &str) -> &str {
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
        let output = Command::new(&self.executable)
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

    fn parse_tsv(&self, frame: &Frame, tsv: &str) -> Vec<TextBlock> {
        let mut blocks = Vec::new();
        for line in tsv.lines().skip(1) {
            let fields: Vec<&str> = line.split('\t').collect();
            if fields.len() < 12 || fields[0] != "5" {
                continue;
            }
            let left = fields[6].parse::<i32>().unwrap_or_default();
            let top = fields[7].parse::<i32>().unwrap_or_default();
            let width = fields[8].parse::<u32>().unwrap_or_default();
            let height = fields[9].parse::<u32>().unwrap_or_default();
            let confidence = fields[10].parse::<f32>().unwrap_or_default() / 100.0;
            let text = correct_text(&clean_text(fields[11]));
            if width == 0 || height == 0 || confidence <= 0.0 || !is_valid_text(&text, "auto") {
                continue;
            }
            blocks.push(TextBlock::new(
                Rect::new(
                    frame.bounds.left + left,
                    frame.bounds.top + top,
                    width,
                    height,
                ),
                text,
                confidence,
            ));
        }
        merge_blocks(blocks, &self.grouping_mode)
    }
}

#[cfg(feature = "native-ocr")]
impl NativeTesseractOcr {
    pub fn new(grouping_mode: impl Into<String>) -> Self {
        Self {
            engine: None,
            language: String::new(),
            grouping_mode: grouping_mode.into(),
        }
    }

    pub fn validate_language(&self, language: &str) -> ServiceResult<()> {
        let code = TesseractOcr::language(language);
        leptess::LepTess::new(None, code)
            .map(|_| ())
            .map_err(|error| format!("Tesseract language '{code}' is unavailable: {error}"))
    }

    fn engine_for(&mut self, language: &str) -> ServiceResult<&mut leptess::LepTess> {
        let code = TesseractOcr::language(language).to_string();
        if self.language != code || self.engine.is_none() {
            self.engine =
                Some(leptess::LepTess::new(None, &code).map_err(|error| {
                    format!("could not initialize Tesseract '{code}': {error}")
                })?);
            self.language = code;
        }
        self.engine
            .as_mut()
            .ok_or_else(|| "Tesseract engine was not initialized".into())
    }
}

impl OcrBackend for TesseractOcr {
    fn recognize(&mut self, frame: &Frame, language: &str) -> ServiceResult<Vec<TextBlock>> {
        let bytes = Self::image_bytes(frame)?;
        let mut child = Command::new(&self.executable)
            .args([
                "stdin",
                "stdout",
                "--psm",
                "6",
                "-l",
                Self::language(language),
                "tsv",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| format!("failed to start {}: {error}", self.executable))?;
        child
            .stdin
            .take()
            .ok_or_else(|| "could not open OCR stdin".to_string())?
            .write_all(&bytes)
            .map_err(|error| format!("could not send frame to OCR: {error}"))?;
        let output = child
            .wait_with_output()
            .map_err(|error| format!("OCR process failed: {error}"))?;
        if !output.status.success() {
            return Err(format!(
                "{} failed: {}",
                self.executable,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(self.parse_tsv(frame, &String::from_utf8_lossy(&output.stdout)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn maps_ui_languages_to_tesseract_codes() {
        assert_eq!(TesseractOcr::language("Japanese"), "jpn");
        assert_eq!(TesseractOcr::language("ja"), "jpn");
        assert_eq!(TesseractOcr::language("zh-TW"), "chi_tra");
        assert_eq!(TesseractOcr::language("th-TH"), "tha");
    }
}

#[cfg(feature = "native-ocr")]
impl OcrBackend for NativeTesseractOcr {
    fn recognize(&mut self, frame: &Frame, language: &str) -> ServiceResult<Vec<TextBlock>> {
        let bytes = TesseractOcr::image_bytes(frame)?;
        let grouping_mode = self.grouping_mode.clone();
        let engine = self.engine_for(language)?;
        engine
            .set_image_from_mem(&bytes)
            .map_err(|error| format!("could not load frame into Tesseract: {error}"))?;
        engine.set_fallback_source_resolution(300);
        let tsv = engine
            .get_tsv_text(0)
            .map_err(|error| format!("Tesseract returned invalid UTF-8: {error}"))?;
        let parser = TesseractOcr::new("", grouping_mode);
        Ok(parser.parse_tsv(frame, &tsv))
    }
}
