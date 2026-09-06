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
            "ja" | "ja-JP" => "jpn",
            "th" | "th-TH" => "tha",
            "ko" | "ko-KR" => "kor",
            "zh" | "zh-CN" => "chi_sim",
            "zh-TW" => "chi_tra",
            "en" | "en-US" => "eng",
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
