use crate::types::TextBlock;

/// Remove common OCR artifacts without changing meaningful punctuation.
pub fn clean_text(input: &str) -> String {
    input
        .replace('ﬁ', "fi")
        .replace('ﬂ', "fl")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim()
        .to_owned()
}

/// Apply a small set of conservative OCR corrections.
pub fn correct_text(input: &str) -> String {
    let mut output = input.to_owned();
    for (from, to) in [("Y3AR", "YEAR"), ("Z0", "30"), ("ZO", "30")] {
        output = output.replace(from, to);
    }

    let chars: Vec<char> = output.chars().collect();
    let mut corrected = String::with_capacity(output.len());
    for (index, character) in chars.iter().enumerate() {
        if *character == '0' {
            let previous_is_letter = index > 0 && chars[index - 1].is_ascii_alphabetic();
            let next_is_letter = index + 1 < chars.len() && chars[index + 1].is_ascii_alphabetic();
            let previous_is_digit = index > 0 && chars[index - 1].is_ascii_digit();
            let next_is_digit = index + 1 < chars.len() && chars[index + 1].is_ascii_digit();
            if (previous_is_letter || next_is_letter) && !previous_is_digit && !next_is_digit {
                corrected.push('O');
                continue;
            }
        }
        corrected.push(*character);
    }
    corrected
}

pub fn is_valid_text(text: &str, source_lang: &str) -> bool {
    let text = text.trim();
    if text.chars().count() < 2 || text.chars().count() > 500 {
        return false;
    }

    let letters = text
        .chars()
        .filter(|character| character.is_alphabetic())
        .count();
    let digits = text
        .chars()
        .filter(|character| character.is_ascii_digit())
        .count();
    let symbols = text
        .chars()
        .filter(|character| !character.is_alphanumeric() && !character.is_whitespace())
        .count();
    if letters == 0 || symbols + digits > text.chars().count() * 7 / 10 {
        return false;
    }

    if source_lang == "th"
        && !text
            .chars()
            .any(|character| ('\u{0e00}'..='\u{0e7f}').contains(&character))
    {
        return false;
    }
    if source_lang == "en" && text.chars().any(is_east_asian) {
        return false;
    }

    true
}

fn is_east_asian(character: char) -> bool {
    matches!(character,
        '\u{3040}'..='\u{30ff}' |
        '\u{3400}'..='\u{9fff}' |
        '\u{ac00}'..='\u{d7af}' |
        '\u{0e00}'..='\u{0e7f}')
}

/// Merge OCR blocks according to the selected display mode.
pub fn merge_blocks(mut blocks: Vec<TextBlock>, mode: &str) -> Vec<TextBlock> {
    if blocks.is_empty() {
        return blocks;
    }
    blocks.sort_by_key(|block| (block.bounds.top, block.bounds.left));

    let mut merged: Vec<TextBlock> = Vec::new();
    for block in blocks {
        let can_merge = merged.last().is_some_and(|previous| {
            let vertical_gap = block.bounds.top - previous.bounds.bottom();
            let horizontal_overlap = block.bounds.left < previous.bounds.right()
                && previous.bounds.left < block.bounds.right();
            if mode == "line" {
                horizontal_overlap || vertical_gap.abs() < 15
            } else {
                horizontal_overlap && vertical_gap <= block.bounds.height as i32 * 2
            }
        });

        if can_merge {
            let previous = merged.last_mut().expect("merge candidate exists");
            previous.source_text.push(' ');
            previous.source_text.push_str(&block.source_text);
            let left = previous.bounds.left.min(block.bounds.left);
            let top = previous.bounds.top.min(block.bounds.top);
            let right = previous.bounds.right().max(block.bounds.right());
            let bottom = previous.bounds.bottom().max(block.bounds.bottom());
            previous.bounds =
                crate::types::Rect::new(left, top, (right - left) as u32, (bottom - top) as u32);
            previous.confidence = previous.confidence.min(block.confidence);
        } else {
            merged.push(block);
        }
    }
    merged
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Rect, TextBlock};

    #[test]
    fn validates_language_specific_text() {
        assert!(is_valid_text("こんにちは", "ja"));
        assert!(!is_valid_text("こんにちは", "en"));
        assert!(is_valid_text("สวัสดี", "th"));
    }

    #[test]
    fn correct_text_preserves_numbers() {
        assert_eq!(correct_text("H0ME 10th"), "HOME 10th");
    }

    #[test]
    fn merges_nearby_paragraph_blocks() {
        let blocks = vec![
            TextBlock::new(Rect::new(0, 0, 100, 20), "First", 0.9),
            TextBlock::new(Rect::new(0, 25, 100, 20), "Second", 0.8),
        ];
        let merged = merge_blocks(blocks, "paragraph");
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].source_text, "First Second");
    }
}
