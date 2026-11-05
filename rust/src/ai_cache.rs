//! Keeps AI translations on disk, so text translated once is never paid for
//! again, and caps how many requests one session may send.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

use crate::ai::BATCH_SIZE;
use crate::services::{ServiceResult, TranslationBackend};
use crate::tracker::{normalize, text_similarity};
use crate::types::LanguagePair;

/// The error given for text not sent because the session's request limit
/// was reached. The pipeline doesn't retry it.
pub const LIMIT_REACHED: &str = "the AI request limit for this session was reached";

/// What the AI translator did this session, shown while the overlay runs.
#[derive(Debug, Default)]
pub struct AiUsage {
    /// Requests sent to the AI service: each is up to `BATCH_SIZE` texts of
    /// one screen, which go out as a few calls at once
    /// (`ai::PARALLEL_REQUESTS`) and count once against the limit.
    pub requests: AtomicU64,
    /// Texts sent to the AI service.
    pub texts_sent: AtomicU64,
    /// Texts answered from the saved translations, at no cost.
    pub cache_hits: AtomicU64,
    /// The request limit stopped further requests.
    pub limit_reached: AtomicBool,
}

#[derive(Deserialize, Serialize)]
struct Line {
    /// Source text, normalized.
    s: String,
    /// Translation.
    t: String,
}

/// Translations saved per language pair, one JSON object per line, so new
/// ones are appended without rewriting the file.
pub struct DiskCache {
    entries: HashMap<String, String>,
    /// Translations with their numbers taken out, so "HP 41/50" reuses the
    /// translation of "HP 40/50": counters and timers would otherwise make
    /// a new paid request on every tick.
    templates: HashMap<String, String>,
    file: Option<File>,
}

/// Stands in for a run of digits in a template.
const NUMBER: char = '\u{1}';

/// The text with each run of digits replaced by [`NUMBER`], and the runs.
fn split_numbers(text: &str) -> (String, Vec<String>) {
    let mut template = String::with_capacity(text.len());
    let mut numbers: Vec<String> = Vec::new();
    let mut in_number = false;
    for character in text.chars() {
        if character.is_ascii_digit() {
            if in_number {
                numbers
                    .last_mut()
                    .expect("a number was started")
                    .push(character);
            } else {
                template.push(NUMBER);
                numbers.push(character.to_string());
                in_number = true;
            }
        } else {
            template.push(character);
            in_number = false;
        }
    }
    (template, numbers)
}

/// Puts `numbers` into the template's [`NUMBER`] slots, in order.
fn fill_numbers(template: &str, numbers: &[String]) -> String {
    let mut numbers = numbers.iter();
    template
        .chars()
        .map(|character| match character {
            NUMBER => numbers.next().cloned().unwrap_or_default(),
            other => other.to_string(),
        })
        .collect()
}

impl DiskCache {
    /// Where the translations for `pair` are kept.
    pub fn default_path(pair: &LanguagePair) -> Option<PathBuf> {
        let safe = |code: &str| {
            code.chars()
                .filter(|character| character.is_ascii_alphanumeric() || *character == '-')
                .collect::<String>()
        };
        Some(crate::ai::config_dir()?.join("ai-cache").join(format!(
            "{}-{}.jsonl",
            safe(&pair.source),
            safe(&pair.target)
        )))
    }

    /// Loads saved translations. Without a usable file the cache still
    /// works for this session.
    pub fn open(path: Option<&Path>) -> Self {
        let mut cache = Self {
            entries: HashMap::new(),
            templates: HashMap::new(),
            file: None,
        };
        let Some(path) = path else {
            return cache;
        };
        if let Ok(file) = File::open(path) {
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                // A line cut short by a crash is skipped, and so is text
                // now known to be icons misread as words. An empty
                // translation marks one that was forgotten.
                if let Ok(line) = serde_json::from_str::<Line>(&line) {
                    if line.t.is_empty() {
                        cache.drop_entry(&line.s);
                    } else if !crate::text::is_icon_noise(&line.s) {
                        cache.remember(line.s, line.t);
                    }
                }
            }
        }
        cache.file = path
            .parent()
            .and_then(|parent| fs::create_dir_all(parent).ok())
            .and_then(|_| OpenOptions::new().create(true).append(true).open(path).ok());
        cache
    }

    /// The saved translation of `text`, or of the same text with other
    /// numbers.
    pub fn get(&self, text: &str) -> Option<String> {
        let key = normalize(text);
        if let Some(saved) = self.entries.get(&key) {
            return Some(saved.clone());
        }
        let (template, numbers) = split_numbers(&key);
        if !numbers.is_empty()
            && let Some(translation) = self.templates.get(&template)
        {
            return Some(fill_numbers(translation, &numbers));
        }
        self.similar(&key, &numbers)
    }

    /// The translation of a saved text that differs from `key` only by a
    /// few OCR misreads ("the are furnace" for "the arc furnace"), with the
    /// same numbers. Long texts only: in short ones a letter or two is
    /// another word.
    fn similar(&self, key: &str, numbers: &[String]) -> Option<String> {
        const MIN_LENGTH: usize = 16;
        const MIN_SIMILARITY: f32 = 0.92;
        let length = key.chars().count();
        if length < MIN_LENGTH {
            return None;
        }
        self.entries
            .iter()
            .filter(|(saved, _)| {
                let saved_length = saved.chars().count();
                saved_length >= MIN_LENGTH
                    && saved_length.abs_diff(length) * 100 <= length * 8
                    && split_numbers(saved).1 == numbers
            })
            .map(|(saved, translation)| (text_similarity(saved, key), translation))
            .filter(|(similarity, _)| *similarity >= MIN_SIMILARITY)
            .max_by(|first, second| first.0.total_cmp(&second.0))
            .map(|(_, translation)| translation.clone())
    }

    fn remember(&mut self, key: String, translation: String) {
        // Only a translation that keeps the numbers, in the same order, can
        // take other numbers.
        let (template, numbers) = split_numbers(&key);
        if !numbers.is_empty() {
            let (translated, translated_numbers) = split_numbers(&translation);
            if translated_numbers == numbers {
                self.templates.insert(template, translated);
            }
        }
        self.entries.insert(key, translation);
    }

    pub fn insert(&mut self, text: &str, translation: &str) {
        let key = normalize(text);
        if self
            .entries
            .get(&key)
            .is_some_and(|saved| saved == translation)
        {
            return;
        }
        if let Some(file) = &mut self.file
            && let Ok(json) = serde_json::to_string(&Line {
                s: key.clone(),
                t: translation.to_string(),
            })
        {
            let _ = writeln!(file, "{json}");
        }
        self.remember(key, translation.to_string());
    }

    /// Forgets the saved translation of `text`, so it's translated again.
    pub fn forget(&mut self, text: &str) {
        let key = normalize(text);
        if !self.entries.contains_key(&key) {
            return;
        }
        if let Some(file) = &mut self.file
            && let Ok(json) = serde_json::to_string(&Line {
                s: key.clone(),
                t: String::new(),
            })
        {
            let _ = writeln!(file, "{json}");
        }
        self.drop_entry(&key);
    }

    fn drop_entry(&mut self, key: &str) {
        if let Some(translation) = self.entries.remove(key) {
            let (template, numbers) = split_numbers(key);
            if !numbers.is_empty()
                && self.templates.get(&template) == Some(&split_numbers(&translation).0)
            {
                self.templates.remove(&template);
            }
        }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// Wraps an AI translator: saved translations are reused, only new texts
/// are sent, and no more than `limit` requests go out per session.
pub struct CachedAiTranslator<T> {
    inner: T,
    pair: LanguagePair,
    cache: DiskCache,
    usage: Arc<AiUsage>,
    /// Requests allowed this session; 0 for no limit.
    limit: u64,
}

impl<T: TranslationBackend> CachedAiTranslator<T> {
    pub fn new(
        inner: T,
        pair: LanguagePair,
        cache: DiskCache,
        usage: Arc<AiUsage>,
        limit: u64,
    ) -> Self {
        Self {
            inner,
            pair,
            cache,
            usage,
            limit,
        }
    }

    fn may_send(&self) -> bool {
        let allowed = self.limit == 0 || self.usage.requests.load(Ordering::Relaxed) < self.limit;
        if !allowed {
            self.usage.limit_reached.store(true, Ordering::Relaxed);
        }
        allowed
    }
}

impl<T: TranslationBackend> TranslationBackend for CachedAiTranslator<T> {
    fn forget(&mut self, pair: &LanguagePair, text: &str) {
        if *pair == self.pair {
            self.cache.forget(text);
        }
    }

    fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
        let mut result = Err("the AI returned no translation".to_string());
        self.translate_each(pair, &[text.to_string()], &mut |_, outcome| {
            result = outcome;
        });
        result
    }

    fn translate_many(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        let mut results = vec![None; texts.len()];
        self.translate_each(pair, texts, &mut |index, outcome| {
            results[index] = Some(outcome);
        });
        results
            .into_iter()
            .map(|result| result.unwrap_or_else(|| Err("no translation".into())))
            .collect()
    }

    fn translate_each(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
        done: &mut dyn FnMut(usize, ServiceResult<String>),
    ) {
        // The saved translations belong to one language pair.
        if *pair != self.pair {
            self.inner.translate_each(pair, texts, done);
            return;
        }
        // Each distinct new text is sent once, even if it appears twice.
        let mut missing: Vec<(String, Vec<usize>)> = Vec::new();
        let mut positions: HashMap<String, usize> = HashMap::new();
        for (index, text) in texts.iter().enumerate() {
            if let Some(saved) = self.cache.get(text) {
                self.usage.cache_hits.fetch_add(1, Ordering::Relaxed);
                done(index, Ok(saved));
                continue;
            }
            let key = normalize(text);
            match positions.get(&key) {
                Some(&slot) => missing[slot].1.push(index),
                None => {
                    positions.insert(key, missing.len());
                    missing.push((text.clone(), vec![index]));
                }
            }
        }
        for batch in missing.chunks(BATCH_SIZE) {
            if !self.may_send() {
                for (_, indices) in batch {
                    for &index in indices {
                        done(index, Err(LIMIT_REACHED.into()));
                    }
                }
                continue;
            }
            self.usage.requests.fetch_add(1, Ordering::Relaxed);
            self.usage
                .texts_sent
                .fetch_add(batch.len() as u64, Ordering::Relaxed);
            let sources = batch
                .iter()
                .map(|(text, _)| text.clone())
                .collect::<Vec<_>>();
            // Each translation is passed on as soon as it arrives.
            let cache = &mut self.cache;
            self.inner
                .translate_each(pair, &sources, &mut |position, outcome| {
                    let (text, indices) = &batch[position];
                    if let Ok(translation) = &outcome {
                        cache.insert(text, translation);
                    }
                    for &index in indices {
                        done(index, outcome.clone());
                    }
                });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Counting {
        calls: Arc<AtomicU64>,
    }

    impl TranslationBackend for Counting {
        fn translate(&mut self, _: &LanguagePair, text: &str) -> ServiceResult<String> {
            Ok(format!("[{text}]"))
        }

        fn translate_many(
            &mut self,
            _: &LanguagePair,
            texts: &[String],
        ) -> ServiceResult<Vec<String>> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            Ok(texts.iter().map(|text| format!("[{text}]")).collect())
        }

        fn translate_each(
            &mut self,
            pair: &LanguagePair,
            texts: &[String],
            done: &mut dyn FnMut(usize, ServiceResult<String>),
        ) {
            for (index, result) in self
                .translate_many(pair, texts)
                .unwrap()
                .into_iter()
                .enumerate()
            {
                done(index, Ok(result));
            }
        }
    }

    fn temp_path(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("overlayr-ai-cache-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir.join("ja-en.jsonl")
    }

    fn translator(
        path: &Path,
        limit: u64,
    ) -> (CachedAiTranslator<Counting>, Arc<AtomicU64>, Arc<AiUsage>) {
        let calls = Arc::new(AtomicU64::new(0));
        let usage = Arc::new(AiUsage::default());
        let translator = CachedAiTranslator::new(
            Counting {
                calls: Arc::clone(&calls),
            },
            LanguagePair::new("ja", "en"),
            DiskCache::open(Some(path)),
            Arc::clone(&usage),
            limit,
        );
        (translator, calls, usage)
    }

    #[test]
    fn translations_survive_a_restart() {
        let path = temp_path("restart");
        let pair = LanguagePair::new("ja", "en");
        let texts = vec!["剣".to_string(), "盾".to_string()];
        {
            let (mut first, calls, _) = translator(&path, 0);
            assert_eq!(
                first.translate_many(&pair, &texts).unwrap(),
                vec!["[剣]", "[盾]"]
            );
            assert_eq!(calls.load(Ordering::Relaxed), 1);
        }
        let (mut second, calls, usage) = translator(&path, 0);
        assert_eq!(
            second.translate_many(&pair, &texts).unwrap(),
            vec!["[剣]", "[盾]"]
        );
        assert_eq!(calls.load(Ordering::Relaxed), 0);
        assert_eq!(usage.cache_hits.load(Ordering::Relaxed), 2);
        let _ = fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn a_forgotten_translation_is_asked_again_after_a_restart() {
        let path = temp_path("forget");
        let pair = LanguagePair::new("ja", "en");
        {
            let (mut first, _, _) = translator(&path, 0);
            first.translate(&pair, "剣").unwrap();
            first.translate(&pair, "体力 7/20").unwrap();
            first.forget(&pair, "剣");
            first.forget(&pair, "体力 7/20");
        }
        let (mut second, calls, _) = translator(&path, 0);
        second.translate(&pair, "剣").unwrap();
        second.translate(&pair, "体力 8/20").unwrap();
        assert_eq!(calls.load(Ordering::Relaxed), 2);
        let _ = fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn icons_misread_as_words_are_not_loaded() {
        let path = temp_path("noise");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            &path,
            "{\"s\":\"toct\",\"t\":\"ทดสอบ\"}\n{\"s\":\"cow\",\"t\":\"วัว\"}\n",
        )
        .unwrap();
        let cache = DiskCache::open(Some(&path));
        assert_eq!(cache.get("TOCT"), None);
        assert_eq!(cache.get("Cow").as_deref(), Some("วัว"));
        let _ = fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn ocr_misreads_of_a_long_text_reuse_its_translation() {
        let mut cache = DiskCache::open(None);
        cache.insert("The Arc Furnace can smelt batches of items", "แปล");
        assert_eq!(
            cache
                .get("The Are Furnace can smelt batches of items")
                .as_deref(),
            Some("แปล")
        );
        // Short texts and other numbers are other texts.
        cache.insert("Arc Furnace", "เตาอาร์ค");
        assert_eq!(cache.get("Are Furnace"), None);
        cache.insert("Uses 120 RF per tick in batches", "ใช้ 120");
        assert_eq!(
            cache.get("Uses 150 RF per tick in batches").as_deref(),
            Some("ใช้ 150")
        );
        assert_eq!(cache.get("Uses 150 RF per tiok in batches"), None);
    }

    #[test]
    fn duplicates_are_sent_once() {
        let path = temp_path("duplicates");
        let pair = LanguagePair::new("ja", "en");
        let (mut cached, _, usage) = translator(&path, 0);
        let texts = vec!["剣".to_string(), "剣 ".to_string(), "盾".to_string()];
        assert_eq!(
            cached.translate_many(&pair, &texts).unwrap(),
            vec!["[剣]", "[剣]", "[盾]"]
        );
        assert_eq!(usage.texts_sent.load(Ordering::Relaxed), 2);
        let _ = fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn changing_numbers_reuse_a_translation() {
        let path = temp_path("numbers");
        let pair = LanguagePair::new("ja", "en");
        let (mut cached, calls, _) = translator(&path, 0);
        assert_eq!(cached.translate(&pair, "体力 7/20").unwrap(), "[体力 7/20]");
        assert_eq!(cached.translate(&pair, "体力 8/20").unwrap(), "[体力 8/20]");
        assert_eq!(
            cached.translate(&pair, "体力 10/200").unwrap(),
            "[体力 10/200]"
        );
        assert_eq!(calls.load(Ordering::Relaxed), 1);
        // Different words are still sent.
        cached.translate(&pair, "魔力 8/20").unwrap();
        assert_eq!(calls.load(Ordering::Relaxed), 2);
        let _ = fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn numbers_reworded_by_the_translation_are_not_reused() {
        let mut cache = DiskCache::open(None);
        // "3 days" → "three days": the number isn't kept, so no template.
        cache.insert("3日", "three days");
        assert_eq!(cache.get("3日").as_deref(), Some("three days"));
        assert_eq!(cache.get("4日"), None);
    }

    #[test]
    fn stops_at_the_request_limit() {
        let path = temp_path("limit");
        let pair = LanguagePair::new("ja", "en");
        let (mut cached, calls, usage) = translator(&path, 1);
        assert!(cached.translate(&pair, "剣").is_ok());
        assert_eq!(
            cached.translate(&pair, "盾").unwrap_err(),
            LIMIT_REACHED.to_string()
        );
        // Saved text is still free.
        assert_eq!(cached.translate(&pair, "剣").unwrap(), "[剣]");
        assert_eq!(calls.load(Ordering::Relaxed), 1);
        assert!(usage.limit_reached.load(Ordering::Relaxed));
        let _ = fs::remove_dir_all(path.parent().unwrap());
    }
}
