use std::collections::{HashMap, VecDeque};
use std::sync::{Condvar, Mutex};

use crate::types::LanguagePair;

/// A one-slot frame handoff.
///
/// Capture can run faster than OCR. Keeping only the newest pending frame
/// prevents latency from growing when OCR or translation is temporarily slow.
pub struct LatestFrame<T> {
    state: Mutex<FrameState<T>>,
    available: Condvar,
}

struct FrameState<T> {
    latest: Option<T>,
    closed: bool,
}

impl<T> LatestFrame<T> {
    pub fn new() -> Self {
        Self {
            state: Mutex::new(FrameState {
                latest: None,
                closed: false,
            }),
            available: Condvar::new(),
        }
    }

    /// Submit a frame, returning the frame that was replaced if one existed.
    pub fn submit(&self, frame: T) -> Option<T> {
        let mut state = self.state.lock().expect("latest-frame mutex poisoned");
        if state.closed {
            return Some(frame);
        }

        let replaced = state.latest.replace(frame);
        self.available.notify_one();
        replaced
    }

    /// Wait for the newest frame, or return `None` after the queue is closed.
    pub fn recv(&self) -> Option<T> {
        let mut state = self.state.lock().expect("latest-frame mutex poisoned");
        while state.latest.is_none() && !state.closed {
            state = self
                .available
                .wait(state)
                .expect("latest-frame mutex poisoned while waiting");
        }

        state.latest.take()
    }

    /// Stop future submissions and wake a waiting consumer.
    pub fn close(&self) {
        let mut state = self.state.lock().expect("latest-frame mutex poisoned");
        state.closed = true;
        state.latest = None;
        self.available.notify_all();
    }
}

impl<T> Default for LatestFrame<T> {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct TranslationKey {
    pair: LanguagePair,
    text: String,
}

/// A bounded LRU cache whose key includes the language pair.
pub struct TranslationCache {
    capacity: usize,
    values: HashMap<TranslationKey, String>,
    order: VecDeque<TranslationKey>,
}

impl TranslationCache {
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity,
            values: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    pub fn get(&mut self, pair: &LanguagePair, text: &str) -> Option<String> {
        let key = TranslationKey {
            pair: pair.clone(),
            text: text.to_owned(),
        };
        let value = self.values.get(&key).cloned();
        if value.is_some() {
            self.touch(&key);
        }
        value
    }

    pub fn insert(
        &mut self,
        pair: &LanguagePair,
        text: impl Into<String>,
        translation: impl Into<String>,
    ) {
        if self.capacity == 0 {
            return;
        }

        let key = TranslationKey {
            pair: pair.clone(),
            text: text.into(),
        };
        self.values.insert(key.clone(), translation.into());
        self.touch(&key);

        while self.values.len() > self.capacity {
            if let Some(oldest) = self.order.pop_front() {
                self.values.remove(&oldest);
            }
        }
    }

    pub fn remove(&mut self, pair: &LanguagePair, text: &str) {
        let key = TranslationKey {
            pair: pair.clone(),
            text: text.to_owned(),
        };
        self.values.remove(&key);
        self.order.retain(|other| *other != key);
    }

    pub fn clear(&mut self) {
        self.values.clear();
        self.order.clear();
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    fn touch(&mut self, key: &TranslationKey) {
        if let Some(position) = self.order.iter().position(|existing| existing == key) {
            self.order.remove(position);
        }
        self.order.push_back(key.clone());
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::thread;

    use super::*;

    #[test]
    fn latest_frame_replaces_stale_work() {
        let frames = LatestFrame::new();
        assert_eq!(frames.submit(1), None);
        assert_eq!(frames.submit(2), Some(1));
        assert_eq!(frames.recv(), Some(2));
    }

    #[test]
    fn close_wakes_consumer() {
        let frames = Arc::new(LatestFrame::<u8>::new());
        let consumer = Arc::clone(&frames);
        let handle = thread::spawn(move || consumer.recv());

        frames.close();
        assert_eq!(handle.join().expect("consumer panicked"), None);
    }

    #[test]
    fn cache_separates_language_pairs() {
        let mut cache = TranslationCache::new(4);
        let thai = LanguagePair::new("ja", "th");
        let english = LanguagePair::new("ja", "en");

        cache.insert(&thai, "猫", "แมว");
        assert_eq!(cache.get(&thai, "猫").as_deref(), Some("แมว"));
        assert_eq!(cache.get(&english, "猫"), None);
    }

    #[test]
    fn cache_evicts_least_recently_used_entry() {
        let mut cache = TranslationCache::new(2);
        let pair = LanguagePair::new("en", "th");

        cache.insert(&pair, "one", "หนึ่ง");
        cache.insert(&pair, "two", "สอง");
        assert_eq!(cache.get(&pair, "one").as_deref(), Some("หนึ่ง"));
        cache.insert(&pair, "three", "สาม");

        assert_eq!(cache.get(&pair, "one").as_deref(), Some("หนึ่ง"));
        assert_eq!(cache.get(&pair, "two"), None);
        assert_eq!(cache.len(), 2);
    }
}
