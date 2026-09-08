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
    pub translation_endpoint: String,
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
            translation_endpoint: "http://localhost:5000/translate".into(),
        }
    }
}

impl AppConfig {
    fn migrate_legacy_values(&mut self) {
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
        if !path.exists() {
            return Ok(Self::default());
        }

        let raw = fs::read_to_string(path)?;
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
    fn defaults_match_product_baseline() {
        let config = AppConfig::default();
        assert_eq!(config.source_lang, "ja");
        assert_eq!(config.target_lang, "th");
        assert!(config.translate_only_when_changed);
        assert_eq!(
            config.translation_endpoint,
            "http://localhost:5000/translate"
        );
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
            r#"{"source_lang":"Japanese","target_lang":"Thai","grouping_mode":"Group (Manga)"}"#,
        )
        .expect("legacy config write failed");
        let migrated = AppConfig::load(&path).expect("legacy config load failed");
        assert_eq!(migrated.source_lang, "ja");
        assert_eq!(migrated.target_lang, "th");
        assert_eq!(migrated.grouping_mode, "paragraph");
        fs::remove_dir_all(directory).expect("test cleanup failed");
    }
}
