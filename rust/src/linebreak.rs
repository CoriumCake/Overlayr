//! Line breaking for scripts written without spaces between words, such as
//! Thai. egui breaks lines only at spaces, so a long Thai sentence would be
//! cut in the middle of a word; the Unicode line-breaking rules, with a
//! dictionary for these scripts, give the places a line may end instead.

use icu_segmenter::LineSegmenter;
use icu_segmenter::options::LineBreakOptions;

/// Whether `text` has a script that needs a dictionary to find where its
/// words end: Thai, Lao, Khmer or Myanmar.
pub fn needs_breaking(text: &str) -> bool {
    text.chars().any(|character| {
        matches!(character,
            '\u{0E00}'..='\u{0EFF}' | '\u{1000}'..='\u{109F}' | '\u{1780}'..='\u{17FF}')
    })
}

/// Byte offsets where a line may end, the end of the text included.
pub fn break_points(text: &str) -> Vec<usize> {
    LineSegmenter::new_dictionary(LineBreakOptions::default())
        .segment_str(text)
        .filter(|&offset| offset > 0)
        .collect()
}

/// `text` with a line break at each place a line has to end to stay within
/// `width`, by `measure`d width. A piece wider than a whole line is left
/// for the caller's own wrapping.
pub fn wrap(text: &str, width: f32, measure: impl Fn(&str) -> f32) -> String {
    let mut lines: Vec<String> = Vec::new();
    for paragraph in text.split('\n') {
        let mut line = String::new();
        let mut line_width = 0.0;
        let mut start = 0;
        for end in break_points(paragraph) {
            let piece = &paragraph[start..end];
            start = end;
            let piece_width = measure(piece.trim_end());
            if !line.is_empty() && line_width + piece_width > width {
                lines.push(line.trim_end().to_string());
                line = String::new();
                line_width = 0.0;
            }
            line.push_str(piece);
            line_width += measure(piece);
        }
        lines.push(line.trim_end().to_string());
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thai_breaks_between_words() {
        let text = "เนื่องจากการเพิ่มความเร็วขึ้นหลายเท่าจากการหลอมแบบขนาน";
        let points = break_points(text);
        // "หลอม" (to smelt) is never split.
        let smelt = text.find("หลอม").expect("in the text");
        assert!(!points.contains(&(smelt + "หล".len())));
        assert!(points.contains(&smelt) || points.contains(&(smelt + "หลอม".len())));
        assert!(points.len() > 5, "{points:?}");
    }

    #[test]
    fn wraps_thai_at_word_boundaries() {
        let text = "เนื่องจากการเพิ่มความเร็วขึ้นหลายเท่าจากการหลอมแบบขนาน";
        let wrapped = wrap(text, 20.0, |piece| piece.chars().count() as f32);
        assert!(wrapped.contains('\n'));
        assert_eq!(wrapped.replace('\n', ""), text);
        for line in wrapped.lines() {
            assert!(!line.starts_with("มแบบ"), "{wrapped}");
        }
    }

    #[test]
    fn only_thai_like_scripts_need_breaking() {
        assert!(needs_breaking("Arc Furnace สามารถ"));
        assert!(!needs_breaking("Arc Furnace"));
        assert!(!needs_breaking("中文"));
    }
}
