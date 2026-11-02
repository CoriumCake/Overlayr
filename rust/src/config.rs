use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// User-configurable settings shared by the desktop UI and processing core.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct AppConfig {
    pub appearance_mode: String,
    pub source_lang: String,
    pub target_lang: String,
    pub grouping_mode: String,
    pub capture_interval_ms: u64,
    pub minimum_confidence: f32,
    pub translate_only_when_changed: bool,
    /// Text similarity (0–1) at which recognised text reuses an existing
    /// translation instead of being translated again.
    pub match_threshold: f32,
    /// Pixel similarity (0–1) at which moved text is followed without being
    /// read again.
    pub follow_threshold: f32,
    /// Fraction of the window (0–1) that must change, unexplained by known
    /// text, before text is read again.
    pub detect_threshold: f32,
    /// Path of the translation pack to play; empty for machine translation.
    pub active_pack: String,
    /// Collects and shows what the pipeline is doing.
    pub debug: bool,
    pub translation_endpoint: String,
    /// Set once the old Docker default has moved to hosted translation, so
    /// a self-hosted server on that address is kept afterwards. Settings
    /// saved before the field existed load it as false.
    #[serde(default)]
    pub translation_migrated: bool,
    pub keybinds: Keybinds,
    /// Drafts translations in the studio: "libretranslate", or an AI
    /// provider ("claude", "chatgpt", "gemini", "deepseek").
    pub draft_translator: String,
    /// Machine translation while the overlay runs: "libretranslate", or an
    /// AI provider ("claude", "chatgpt", "gemini", "deepseek").
    pub live_translator: String,
    /// Model per AI provider, when not its default.
    pub ai_models: std::collections::BTreeMap<String, String>,
    /// How much CPU the overlay may use, traded against how fast it reacts.
    pub performance: Performance,
}

/// Speed versus CPU use, so the overlay suits slow and fast computers.
/// `capture_interval_ms` (how often the screen is checked) belongs here too
/// but stays top-level for older settings files.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct Performance {
    /// Checks the screen more often while translations are shown, so they
    /// clear quickly when their text goes away.
    pub fast_clear: bool,
    /// Capture interval while translations are shown, with `fast_clear`.
    pub fast_clear_interval_ms: u64,
    /// Redraws a translation whenever the picture under it changes (e.g. a
    /// hover highlight), so its background keeps matching.
    pub follow_highlights: bool,
    /// Waits until new text stops changing (fading in) before reading it.
    pub wait_for_settle: bool,
    /// Pauses after each OCR read about as long as the read took.
    pub rest_between_reads: bool,
    /// CPU threads per OCR model.
    pub ocr_threads: usize,
    /// Runs capture and OCR at low priority, so the game comes first.
    pub low_priority: bool,
}

impl Default for Performance {
    fn default() -> Self {
        Self::BALANCED
    }
}

impl Performance {
    pub const LIGHT: Self = Self {
        fast_clear: false,
        fast_clear_interval_ms: 40,
        follow_highlights: false,
        wait_for_settle: true,
        rest_between_reads: true,
        ocr_threads: 1,
        low_priority: true,
    };
    pub const BALANCED: Self = Self {
        fast_clear: true,
        fast_clear_interval_ms: 40,
        follow_highlights: true,
        wait_for_settle: true,
        rest_between_reads: true,
        ocr_threads: 2,
        low_priority: true,
    };
    pub const FAST: Self = Self {
        fast_clear: true,
        fast_clear_interval_ms: 25,
        follow_highlights: true,
        wait_for_settle: true,
        rest_between_reads: false,
        ocr_threads: 4,
        low_priority: true,
    };

    /// Settings that apply process-wide: OCR threads and thread priority.
    /// Takes effect for OCR engines and threads started afterwards.
    pub fn apply(&self) {
        crate::neural::set_threads(self.ocr_threads);
        crate::services::set_low_priority(self.low_priority);
    }
}

/// Ready-made performance settings: (name, description, capture interval
/// in ms, settings).
pub const PERFORMANCE_PRESETS: [(&str, &str, u64, Performance); 3] = [
    (
        "Light",
        "For slower computers: least CPU, translations react more slowly.",
        250,
        Performance::LIGHT,
    ),
    (
        "Balanced",
        "Quick to react without taking much from the game.",
        100,
        Performance::BALANCED,
    ),
    (
        "Fast",
        "For powerful computers: reacts fastest, uses the most CPU.",
        33,
        Performance::FAST,
    ),
];

impl AppConfig {
    /// The model to draft with on `provider`.
    pub fn ai_model(&self, provider: crate::ai::Provider) -> String {
        self.ai_models
            .get(provider.id())
            .map(|model| model.trim())
            .filter(|model| !model.is_empty())
            .unwrap_or(provider.default_model())
            .to_string()
    }
}

/// Desktop shortcuts, as Hyprland key combinations ("SUPER + SHIFT + O");
/// empty turns one off.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct Keybinds {
    /// Starts or stops the overlay.
    pub toggle_overlay: String,
    /// Hides or shows the translations.
    pub toggle_translations: String,
}

impl Default for Keybinds {
    /// Windows keeps most Windows-key shortcuts for itself, so Overlayr's
    /// use Ctrl there.
    fn default() -> Self {
        let modifier = if cfg!(windows) { "CTRL" } else { "SUPER" };
        Self {
            toggle_overlay: format!("{modifier} + SHIFT + O"),
            toggle_translations: format!("{modifier} + SHIFT + H"),
        }
    }
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            appearance_mode: "Dark".into(),
            source_lang: "ja".into(),
            target_lang: "th".into(),
            grouping_mode: "paragraph".into(),
            capture_interval_ms: 100,
            minimum_confidence: 0.35,
            translate_only_when_changed: true,
            match_threshold: 0.85,
            follow_threshold: 0.8,
            detect_threshold: 0.001,
            active_pack: String::new(),
            debug: false,
            translation_endpoint: HOSTED_TRANSLATION_ENDPOINT.into(),
            // New settings start on hosted translation.
            translation_migrated: true,
            keybinds: Keybinds::default(),
            draft_translator: "libretranslate".into(),
            live_translator: "libretranslate".into(),
            ai_models: std::collections::BTreeMap::new(),
            performance: Performance::default(),
        }
    }
}

/// Free translation hosted by overlayr.app, so players don't need to run a
/// server. It speaks the LibreTranslate API, like a self-hosted server.
pub const HOSTED_TRANSLATION_ENDPOINT: &str = "https://overlayr.app/api/translate";
/// The old default: a LibreTranslate server the player had to run in Docker.
const LEGACY_LOCAL_ENDPOINT: &str = "http://localhost:5000/translate";

impl AppConfig {
    fn migrate_legacy_values(&mut self) {
        if !self.translation_migrated {
            if self.translation_endpoint == LEGACY_LOCAL_ENDPOINT {
                self.translation_endpoint = HOSTED_TRANSLATION_ENDPOINT.into();
            }
            self.translation_migrated = true;
        }
        self.source_lang = normalize_language(&self.source_lang);
        self.target_lang = normalize_language(&self.target_lang);
        if self.grouping_mode.eq_ignore_ascii_case("group (manga)") {
            self.grouping_mode = "paragraph".into();
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        for (name, value) in [
            ("source language", &self.source_lang),
            ("target language", &self.target_lang),
        ] {
            if value.is_empty()
                || value.len() > 16
                || !value
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || character == '-')
            {
                return Err(format!("invalid {name}: {value}"));
            }
        }
        if !matches!(self.grouping_mode.as_str(), "line" | "paragraph") {
            return Err("grouping mode must be line or paragraph".into());
        }
        if !(16..=5_000).contains(&self.capture_interval_ms) {
            return Err("capture interval must be between 16 and 5000 ms".into());
        }
        if !(16..=1_000).contains(&self.performance.fast_clear_interval_ms) {
            return Err("fast clear interval must be between 16 and 1000 ms".into());
        }
        if !(1..=16).contains(&self.performance.ocr_threads) {
            return Err("OCR threads must be between 1 and 16".into());
        }
        for (name, value) in [
            ("match threshold", self.match_threshold),
            ("follow threshold", self.follow_threshold),
            ("detection threshold", self.detect_threshold),
        ] {
            if !value.is_finite() || !(0.0..=1.0).contains(&value) {
                return Err(format!("{name} must be between 0 and 1"));
            }
        }
        if !self.minimum_confidence.is_finite() || !(0.0..=1.0).contains(&self.minimum_confidence) {
            return Err("confidence must be between 0 and 1".into());
        }
        if !(self.translation_endpoint.starts_with("http://")
            || self.translation_endpoint.starts_with("https://"))
        {
            return Err("translation endpoint must use http:// or https://".into());
        }
        Ok(())
    }

    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        let path = path.as_ref();
        let source_path = if path.exists() {
            path.to_path_buf()
        } else if let Some(legacy_path) = legacy_settings_path(path) {
            legacy_path
        } else {
            return Ok(Self::default());
        };

        let raw = fs::read_to_string(source_path)?;
        let mut config: Self = serde_json::from_str(&raw)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        config.migrate_legacy_values();
        Ok(config)
    }

    /// Save through a temporary file so a process interruption cannot leave
    /// invalid JSON. Unix uses an atomic replacement; Windows removes the
    /// previous destination first because `rename` cannot replace it there.
    pub fn save(&self, path: impl AsRef<Path>) -> io::Result<()> {
        self.validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }

        let temp_path = path.with_extension("json.tmp");
        let serialized = serde_json::to_vec_pretty(self)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        fs::write(&temp_path, serialized)?;
        #[cfg(windows)]
        if path.exists() {
            fs::remove_file(path)?;
        }
        fs::rename(temp_path, path)
    }

    pub fn default_path() -> PathBuf {
        if cfg!(target_os = "windows") {
            std::env::var_os("APPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("."))
                .join("Overlayr")
                .join("settings.json")
        } else if let Some(config_home) = std::env::var_os("XDG_CONFIG_HOME") {
            PathBuf::from(config_home)
                .join("overlayr")
                .join("settings.json")
        } else if cfg!(target_os = "macos") {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("."))
                .join("Library")
                .join("Application Support")
                .join("Overlayr")
                .join("settings.json")
        } else {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("."))
                .join(".config")
                .join("overlayr")
                .join("settings.json")
        }
    }
}

fn legacy_settings_path(current_path: &Path) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(current_dir) = std::env::current_dir() {
        candidates.push(current_dir.join("settings.json"));
    }
    if let Ok(executable) = std::env::current_exe()
        && let Some(parent) = executable.parent()
    {
        candidates.push(parent.join("settings.json"));
    }
    candidates
        .into_iter()
        .find(|candidate| candidate != current_path && candidate.is_file())
}

fn normalize_language(language: &str) -> String {
    match language.trim().to_ascii_lowercase().as_str() {
        "japanese" => "ja".into(),
        "english" => "en".into(),
        "thai" => "th".into(),
        "korean" => "ko".into(),
        "chinese" | "simplified chinese" => "zh-CN".into(),
        "traditional chinese" => "zh-TW".into(),
        _ => language.trim().into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ai_models_fall_back_to_each_providers_default() {
        let mut config = AppConfig::default();
        assert_eq!(
            config.ai_model(crate::ai::Provider::DeepSeek),
            "deepseek-chat"
        );
        config.ai_models.insert("chatgpt".into(), " gpt-5 ".into());
        assert_eq!(config.ai_model(crate::ai::Provider::ChatGpt), "gpt-5");
        config.ai_models.insert("gemini".into(), "  ".into());
        assert_eq!(
            config.ai_model(crate::ai::Provider::Gemini),
            crate::ai::Provider::Gemini.default_model()
        );
    }

    #[test]
    fn defaults_match_product_baseline() {
        let config = AppConfig::default();
        assert_eq!(config.source_lang, "ja");
        assert_eq!(config.target_lang, "th");
        assert!(config.translate_only_when_changed);
        assert_eq!(config.translation_endpoint, HOSTED_TRANSLATION_ENDPOINT);
    }

    #[test]
    fn config_round_trips_atomically() {
        let directory =
            std::env::temp_dir().join(format!("overlayr-config-{}", std::process::id()));
        let path = directory.join("settings.json");
        let expected = AppConfig {
            grouping_mode: "line".into(),
            ..AppConfig::default()
        };

        expected.save(&path).expect("config save failed");
        assert_eq!(
            AppConfig::load(&path).expect("config load failed"),
            expected
        );
        fs::remove_dir_all(directory).expect("test cleanup failed");
    }

    #[test]
    fn rejects_invalid_runtime_values() {
        let config = AppConfig {
            capture_interval_ms: 1,
            ..AppConfig::default()
        };
        assert!(config.validate().is_err());

        let config = AppConfig {
            translation_endpoint: "ftp://example.test".into(),
            ..AppConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn migrates_legacy_human_readable_settings() {
        let directory =
            std::env::temp_dir().join(format!("overlayr-legacy-config-{}", std::process::id()));
        let path = directory.join("settings.json");
        fs::create_dir_all(&directory).expect("test directory creation failed");
        fs::write(
            &path,
            r#"{"source_lang":"Japanese","target_lang":"Thai","grouping_mode":"Group (Manga)",
                "translation_endpoint":"http://localhost:5000/translate"}"#,
        )
        .expect("legacy config write failed");
        let migrated = AppConfig::load(&path).expect("legacy config load failed");
        assert_eq!(migrated.source_lang, "ja");
        assert_eq!(migrated.target_lang, "th");
        assert_eq!(migrated.grouping_mode, "paragraph");
        // The old Docker default moves to hosted translation once; a
        // self-hosted server chosen afterwards, even on that address, stays.
        assert_eq!(migrated.translation_endpoint, HOSTED_TRANSLATION_ENDPOINT);
        let self_hosted = AppConfig {
            translation_endpoint: LEGACY_LOCAL_ENDPOINT.into(),
            ..migrated
        };
        self_hosted
            .save(&path)
            .expect("self-hosted config save failed");
        assert_eq!(
            AppConfig::load(&path)
                .expect("self-hosted config load failed")
                .translation_endpoint,
            LEGACY_LOCAL_ENDPOINT
        );
        fs::remove_dir_all(directory).expect("test cleanup failed");
    }

    #[test]
    fn performance_presets_are_valid_and_old_settings_get_balanced() {
        for (name, _, interval, performance) in PERFORMANCE_PRESETS {
            let config = AppConfig {
                capture_interval_ms: interval,
                performance,
                ..AppConfig::default()
            };
            assert_eq!(config.validate(), Ok(()), "{name}");
        }
        let old: AppConfig = serde_json::from_str(r#"{"source_lang": "en"}"#).unwrap();
        assert_eq!(old.performance, Performance::BALANCED);
    }
}
