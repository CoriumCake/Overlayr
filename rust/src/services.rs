use crate::pipeline::TranslationCache;
use crate::types::{Frame, LanguagePair, Source, TextBlock};

pub type ServiceResult<T> = Result<T, String>;

/// OS-specific screen/window capture implementation.
pub trait CaptureBackend: Send {
    fn sources(&mut self) -> ServiceResult<Vec<Source>>;
    fn capture(&mut self, source: &Source) -> ServiceResult<Frame>;
}

/// OCR implementation. The production app can select a native or ONNX backend.
pub trait OcrBackend: Send {
    fn recognize(&mut self, frame: &Frame, language: &str) -> ServiceResult<Vec<TextBlock>>;
}

/// Translation implementation. Implementations may be remote or offline.
pub trait TranslationBackend: Send {
    fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String>;
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
            let translation = if let Some(cached) = self.cache.get(pair, &block.source_text) {
                cached
            } else {
                let translated = self.provider.translate(pair, &block.source_text)?;
                self.cache
                    .insert(pair, block.source_text.clone(), translated.clone());
                translated
            };
            block.translated_text = Some(translation);
        }
        Ok(blocks)
    }

    pub fn cache_len(&self) -> usize {
        self.cache.len()
    }
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
