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
            // "100.0k": a zero after a decimal point is still a digit.
            let previous_is_digit = index > 0
                && (chars[index - 1].is_ascii_digit()
                    || (index > 1
                        && matches!(chars[index - 1], '.' | ',')
                        && chars[index - 2].is_ascii_digit()));
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
    if is_icon_noise(text) {
        return false;
    }

    true
}

/// Whether text is OCR's reading of a row of icons rather than words. Round
/// icons such as hearts, armour and food read as round letters ("TOCT",
/// "COUOO", "TUWUWCO", "OO"), so text made only of those letters is noise,
/// apart from the few real words they spell.
pub fn is_icon_noise(text: &str) -> bool {
    const ROUND: &str = "cotuvw";
    const WORDS: [&str; 16] = [
        "to", "too", "two", "out", "cut", "cot", "cow", "wow", "tow", "coo", "tot", "toot", "tout",
        "coot", "otto", "tutu",
    ];
    let words = text
        .split(|character: char| !character.is_alphanumeric())
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>();
    !words.is_empty()
        && words.iter().all(|word| {
            let word = word.to_lowercase();
            word.chars().all(|character| ROUND.contains(character))
                && !WORDS.contains(&word.as_str())
        })
}

/// Drops East Asian characters at the ends of text in a language written
/// without them: OCR reads a menu icon next to a word as one ("中Storage").
pub fn trim_icon_misreads(text: &str, source_lang: &str) -> String {
    if !matches!(source_lang, "en") {
        return text.to_string();
    }
    let trimmed = text
        .trim()
        .trim_matches(|character: char| is_east_asian(character) || character.is_whitespace());
    // Only when a real word is left: all-Thai text (the overlay's own) stays
    // as it is, and invalid.
    if trimmed.chars().any(is_east_asian) || !trimmed.chars().any(char::is_alphabetic) {
        return text.to_string();
    }
    trimmed.to_string()
}

fn is_east_asian(character: char) -> bool {
    matches!(character,
        '\u{3040}'..='\u{30ff}' |
        '\u{3400}'..='\u{9fff}' |
        '\u{ac00}'..='\u{d7af}' |
        '\u{0e00}'..='\u{0e7f}')
}

/// Whether text contains something word-like: East Asian script, or a run
/// of at least three letters. This drops icon fragments that OCR reads as
/// short letter pairs such as "QO" or "Vv".
pub fn has_word(text: &str) -> bool {
    if text.chars().any(is_east_asian) {
        return true;
    }
    let mut run = 0;
    for character in text.chars() {
        if character.is_alphabetic() {
            run += 1;
            if run >= 3 {
                return true;
            }
        } else {
            run = 0;
        }
    }
    false
}

/// Whether a word written in a Latin-script language reads as a real word,
/// abbreviation or number. OCR of icons, health bars and pixel art produces
/// tokens like "rrr'/", "VOJ'/E@Q'" or "L3" that fail this.
pub fn is_word_like(token: &str) -> bool {
    let mut parts = token.split_whitespace().peekable();
    parts.peek().is_some() && parts.all(is_single_word_like)
}

fn is_single_word_like(token: &str) -> bool {
    let core = token.trim_matches(|character: char| !character.is_alphanumeric());
    if core.is_empty() {
        return false;
    }
    if core.chars().all(|character| {
        character.is_ascii_digit() || matches!(character, ',' | '.' | '%' | ':' | '/')
    }) {
        return core.chars().any(|character| character.is_ascii_digit());
    }
    // A quantity with a short unit: "87.4k", "100.0k", "64mB", "20s", "3x".
    let digits = core
        .find(|character: char| !(character.is_ascii_digit() || matches!(character, ',' | '.')))
        .unwrap_or(core.len());
    let (number, unit) = core.split_at(digits);
    if number.starts_with(|character: char| character.is_ascii_digit())
        && (1..=3).contains(&unit.chars().count())
        && unit
            .chars()
            .all(|character| character.is_ascii_alphabetic())
    {
        return true;
    }
    if !core
        .chars()
        .all(|character| character.is_alphabetic() || matches!(character, '\'' | '-'))
    {
        return false;
    }
    let letters = core
        .chars()
        .filter(|character| character.is_alphabetic())
        .collect::<Vec<_>>();
    // Real words rarely repeat a letter three times ("rrr" is a health bar).
    if letters.windows(3).any(|window| {
        window[0].eq_ignore_ascii_case(&window[1]) && window[1].eq_ignore_ascii_case(&window[2])
    }) {
        return false;
    }
    match letters.len() {
        1 => matches!(letters[0], 'a' | 'A' | 'I'),
        // Short all-caps abbreviations common in games: HP, XP, NPC.
        2..=3 if letters.iter().all(|letter| letter.is_uppercase()) => true,
        _ => letters
            .iter()
            .any(|letter| !letter.is_ascii() || "aeiouyAEIOUY".contains(*letter)),
    }
}

/// Whether `text` is in a Latin-script language, where [`is_word_like`]
/// applies. Chinese, Japanese, Korean and Thai are judged differently.
pub fn is_latin_text(text: &str) -> bool {
    !text.chars().any(is_east_asian)
        && text
            .chars()
            .any(|character| character.is_ascii_alphabetic())
}

/// Joins OCR words, omitting spaces between scripts written without them
/// (Chinese, Japanese and Thai).
pub fn join_words<'a>(words: impl IntoIterator<Item = &'a str>) -> String {
    let mut text = String::new();
    for word in words {
        let needs_space = text.chars().last().is_some_and(|previous| {
            !(is_east_asian(previous) && word.chars().next().is_some_and(is_east_asian))
        });
        if needs_space {
            text.push(' ');
        }
        text.push_str(word);
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn icons_misread_as_characters_are_trimmed() {
        assert_eq!(trim_icon_misreads("中Storage", "en"), "Storage");
        assert_eq!(trim_icon_misreads("食 Mekanism", "en"), "Mekanism");
        assert_eq!(trim_icon_misreads("การผลิต", "en"), "การผลิต");
        assert_eq!(trim_icon_misreads("中Storage", "ja"), "中Storage");
    }

    #[test]
    fn validates_language_specific_text() {
        assert!(is_valid_text("こんにちは", "ja"));
        for noise in [
            "TOCT", "COUOO", "CUUOO", "TUWUWCO", "TOTTO", "TOTTOTO", "OO", "OO TOCT",
        ] {
            assert!(!is_valid_text(noise, "en"), "{noise}");
        }
        for text in [
            "Cow",
            "Too",
            "Feed to a Cow",
            "Time Wand",
            "Tool",
            "Cobblestone",
            "Out of Water",
        ] {
            assert!(is_valid_text(text, "en"), "{text}");
        }
        assert!(!is_valid_text("こんにちは", "en"));
        assert!(is_valid_text("สวัสดี", "th"));
    }

    #[test]
    fn correct_text_preserves_numbers() {
        assert_eq!(correct_text("H0ME 10th"), "HOME 10th");
        assert_eq!(correct_text("87.4k / 100.0k"), "87.4k / 100.0k");
    }

    #[test]
    fn joins_words_with_script_aware_spacing() {
        assert_eq!(join_words(["This", "chapter"]), "This chapter");
        assert_eq!(join_words(["猫", "です"]), "猫です");
        assert_eq!(join_words(["Minecraft", "の", "世界"]), "Minecraft の世界");
    }

    #[test]
    fn recognises_word_like_tokens() {
        for word in [
            "Crafting", "Water", "a", "I", "HP", "XP", "1466", "+100%", "(1)", "don't", "Game,",
            "87.4k", "100.0k", "64mB", "20s", "3x",
        ] {
            assert!(is_word_like(word), "{word} should be word-like");
        }
        for junk in [
            "rrr'/",
            "rrrrrrrrr",
            "ttr'/",
            "VOJ'/E@Q'",
            "L3",
            "h:',",
            "'f",
            "x",
            "+",
        ] {
            assert!(!is_word_like(junk), "{junk} should be junk");
        }
    }

    #[test]
    fn requires_a_word_like_run() {
        assert!(has_word("Gearing Up"));
        assert!(has_word("猫"));
        assert!(!has_word("QO"));
        assert!(!has_word("Vv \" o"));
    }
}
