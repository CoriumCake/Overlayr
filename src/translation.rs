use anyhow::{Context, Result};
use lru::LruCache;
use serde_json::Value;
use std::num::NonZeroUsize;

pub struct Translator {
    source: String,
    target: String,
    cache: LruCache<String, String>,
}

impl Translator {
    pub fn new(source: &str, target: &str) -> Self {
        Self {
            source: source.to_string(),
            target: target.to_string(),
            cache: LruCache::new(NonZeroUsize::new(1000).unwrap()),
        }
    }

    pub fn update_languages(&mut self, source: &str, target: &str) {
        if self.source != source || self.target != target {
            self.source = source.to_string();
            self.target = target.to_string();
            self.cache.clear();
        }
    }

    pub fn translate(&mut self, text: &str) -> String {
        let text = text.trim();
        if text.is_empty() {
            return String::new();
        }

        if let Some(cached) = self.cache.get(text) {
            return cached.clone();
        }

        match self.call_api(text) {
            Ok(translated) => {
                self.cache.put(text.to_string(), translated.clone());
                translated
            }
            Err(e) => {
                eprintln!("Translation error: {:?}", e);
                format!("[Error: {}]", e)
            }
        }
    }

    fn call_api(&self, text: &str) -> Result<String> {
        let response: Value = ureq::get("https://translate.googleapis.com/translate_a/single")
            .query("client", "gtx")
            .query("sl", &self.source)
            .query("tl", &self.target)
            .query("dt", "t")
            .query("q", text)
            .call()
            .context("HTTP request failed")?
            .into_json()
            .context("Failed to parse JSON response")?;

        let sentences = response
            .get(0)
            .and_then(|v| v.as_array())
            .context("Invalid response format")?;

        let mut translated = String::new();
        for sentence in sentences {
            if let Some(parts) = sentence.as_array() {
                if let Some(trans_val) = parts.get(0) {
                    if let Some(trans_str) = trans_val.as_str() {
                        translated.push_str(trans_str);
                    }
                }
            }
        }

        Ok(translated)
    }
}
