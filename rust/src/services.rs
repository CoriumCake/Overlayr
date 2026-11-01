use crate::pipeline::TranslationCache;
use crate::types::{Frame, LanguagePair, RejectedText, Source, TextBlock};

pub type ServiceResult<T> = Result<T, String>;

/// OS-specific screen/window capture implementation.
pub trait CaptureBackend: Send {
    fn sources(&mut self) -> ServiceResult<Vec<Source>>;
    fn capture(&mut self, source: &Source) -> ServiceResult<Frame>;

    /// Whether the source is on screen, so translations can be shown over
    /// it. Backends that cannot tell should keep the default.
    fn source_is_active(&mut self, _source: &Source) -> ServiceResult<bool> {
        Ok(true)
    }

    /// Whether `source` can be captured correctly while it isn't visible,
    /// so translation can keep running in the background.
    fn can_capture_hidden(&mut self, _source: &Source) -> bool {
        false
    }

    /// Whether the window is at its full size rather than squeezed by a
    /// tiling layout (for example sharing a workspace with Overlayr), so
    /// studio keyframes always show the layout players will see.
    fn source_is_full_size(&mut self, _source: &Source) -> ServiceResult<bool> {
        Ok(true)
    }

    /// A preview image for source pickers, ideally about `max_width` pixels
    /// wide. Backends that cannot capture cheaply at reduced size fall back
    /// to a full capture, which callers are expected to downscale.
    fn thumbnail(&mut self, source: &Source, _max_width: u32) -> ServiceResult<Frame> {
        self.capture(source)
    }
}

/// OCR implementation. The production app can select a native or ONNX backend.
pub trait OcrBackend: Send {
    fn recognize(&mut self, frame: &Frame, language: &str) -> ServiceResult<Vec<TextBlock>>;

    /// Text the last [`OcrBackend::recognize`] found but discarded as noise,
    /// for debug mode. Backends that don't track this return nothing.
    fn take_rejected(&mut self) -> Vec<RejectedText> {
        Vec::new()
    }

    /// Checks the engine can read `language`, so problems show before
    /// translation starts.
    fn validate_language(&self, _language: &str) -> ServiceResult<()> {
        Ok(())
    }

    /// Short engine name for status and debug views.
    fn name(&self) -> &'static str {
        "OCR"
    }
}

impl OcrBackend for Box<dyn OcrBackend> {
    fn recognize(&mut self, frame: &Frame, language: &str) -> ServiceResult<Vec<TextBlock>> {
        (**self).recognize(frame, language)
    }

    fn take_rejected(&mut self) -> Vec<RejectedText> {
        (**self).take_rejected()
    }

    fn validate_language(&self, language: &str) -> ServiceResult<()> {
        (**self).validate_language(language)
    }

    fn name(&self) -> &'static str {
        (**self).name()
    }
}

/// Translation implementation. Implementations may be remote or offline.
pub trait TranslationBackend: Send {
    fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String>;

    /// Translates several texts. Providers that can handle requests
    /// concurrently should, since new screens often show many texts at once.
    fn translate_many(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        texts
            .iter()
            .map(|text| self.translate(pair, text))
            .collect()
    }

    /// Translates several texts, reporting each by its index as soon as it
    /// is done, so callers can show results without waiting for the
    /// slowest. Providers that translate concurrently should report in
    /// completion order.
    fn translate_each(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
        done: &mut dyn FnMut(usize, ServiceResult<String>),
    ) {
        for (index, text) in texts.iter().enumerate() {
            done(index, self.translate(pair, text));
        }
    }
}

impl TranslationBackend for Box<dyn TranslationBackend> {
    fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
        (**self).translate(pair, text)
    }

    fn translate_many(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        (**self).translate_many(pair, texts)
    }

    fn translate_each(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
        done: &mut dyn FnMut(usize, ServiceResult<String>),
    ) {
        (**self).translate_each(pair, texts, done);
    }
}

/// Applies cache, translation, and result assembly consistently for all providers.
pub struct TranslationCoordinator<P> {
    provider: P,
    cache: TranslationCache,
}

impl<P: TranslationBackend> TranslationCoordinator<P> {
    pub fn new(provider: P, cache_capacity: usize) -> Self {
        Self {
            provider,
            cache: TranslationCache::new(cache_capacity),
        }
    }

    pub fn translate_blocks(
        &mut self,
        pair: &LanguagePair,
        mut blocks: Vec<TextBlock>,
    ) -> ServiceResult<Vec<TextBlock>> {
        for block in &mut blocks {
            block.translated_text = Some(self.translate_text(pair, &block.source_text)?);
        }
        Ok(blocks)
    }

    /// Translates one text, using the cache when the exact text was seen.
    /// Translates several texts, sending only those not in the cache, all at
    /// once.
    pub fn translate_texts(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        let mut results = texts
            .iter()
            .map(|text| self.cache.get(pair, text))
            .collect::<Vec<_>>();
        let missing = texts
            .iter()
            .zip(&results)
            .filter(|(_, cached)| cached.is_none())
            .map(|(text, _)| text.clone())
            .collect::<Vec<_>>();
        let mut translated = self.provider.translate_many(pair, &missing)?.into_iter();
        for (text, result) in texts.iter().zip(&mut results) {
            if result.is_none() {
                let translation = translated
                    .next()
                    .ok_or_else(|| "the provider returned too few translations".to_string())?;
                self.cache.insert(pair, text.clone(), translation.clone());
                *result = Some(translation);
            }
        }
        Ok(results.into_iter().flatten().collect())
    }

    pub fn translate_text(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
        if let Some(cached) = self.cache.get(pair, text) {
            return Ok(cached);
        }
        let translated = self.provider.translate(pair, text)?;
        self.cache.insert(pair, text, translated.clone());
        Ok(translated)
    }

    pub fn cache_len(&self) -> usize {
        self.cache.len()
    }
}

/// Whether `lower_thread_priority` lowers it, from the performance settings.
static LOW_PRIORITY: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(true);

pub fn set_low_priority(low: bool) {
    LOW_PRIORITY.store(low, std::sync::atomic::Ordering::Relaxed);
}

/// Runs the calling thread (and threads it starts afterwards, such as OCR
/// worker pools) at low CPU priority, so capture and OCR yield to the game
/// being translated instead of making it stutter.
pub fn lower_thread_priority() {
    let low = LOW_PRIORITY.load(std::sync::atomic::Ordering::Relaxed);
    #[cfg(target_os = "linux")]
    if low {
        const PRIO_PROCESS: i32 = 0;
        unsafe extern "C" {
            fn setpriority(which: i32, who: u32, priority: i32) -> i32;
        }
        // SAFETY: on Linux, `who = 0` with PRIO_PROCESS changes only the
        // calling thread's nice value; failure just leaves it unchanged.
        unsafe {
            setpriority(PRIO_PROCESS, 0, 15);
        }
    }
    #[cfg(target_os = "macos")]
    if low {
        // QOS_CLASS_UTILITY: below the game, but not throttled like
        // background work.
        const QOS_CLASS_UTILITY: u32 = 0x11;
        unsafe extern "C" {
            fn pthread_set_qos_class_self_np(qos_class: u32, relative_priority: i32) -> i32;
        }
        // SAFETY: changes only the calling thread's quality of service;
        // failure just leaves it unchanged.
        unsafe {
            pthread_set_qos_class_self_np(QOS_CLASS_UTILITY, 0);
        }
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    let _ = low;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Rect;

    struct FakeTranslator;

    impl TranslationBackend for FakeTranslator {
        fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
            Ok(format!("{}:{}:{}", pair.source, pair.target, text))
        }
    }

    #[test]
    fn coordinator_caches_translations() {
        let pair = LanguagePair::new("ja", "th");
        let mut coordinator = TranslationCoordinator::new(FakeTranslator, 8);
        let blocks = vec![TextBlock::new(Rect::new(0, 0, 10, 10), "猫", 0.9)];

        let first = coordinator
            .translate_blocks(&pair, blocks.clone())
            .expect("translation failed");
        let second = coordinator
            .translate_blocks(&pair, blocks)
            .expect("cached translation failed");
        assert_eq!(first, second);
        assert_eq!(coordinator.cache_len(), 1);
    }
}
