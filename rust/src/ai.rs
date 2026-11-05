//! Draft translations from AI models, for the creator studio.
//!
//! The studio sends all of a screen's untranslated texts at once with the
//! game's name and the creator's own translations as a glossary, so drafts
//! read naturally and keep names and terms consistent. The creator then
//! fixes what's wrong instead of translating everything by hand.
//!
//! Claude is called through Anthropic's Messages API; ChatGPT, Gemini and
//! DeepSeek through their OpenAI-compatible chat APIs. There are no official
//! SDKs for Rust, so both go over HTTP.

use std::path::PathBuf;
use std::time::Duration;

use serde_json::{Value, json};

use crate::services::ServiceResult;
use crate::types::LanguagePair;

const API_URL: &str = "https://api.anthropic.com/v1/messages";
pub const DEFAULT_MODEL: &str = "claude-opus-5";

/// An AI service that drafts translations.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Provider {
    Claude,
    ChatGpt,
    Gemini,
    DeepSeek,
}

impl Provider {
    pub const ALL: [Self; 4] = [Self::Claude, Self::ChatGpt, Self::Gemini, Self::DeepSeek];

    /// Name used in the settings file and for the key file.
    pub fn id(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::ChatGpt => "chatgpt",
            Self::Gemini => "gemini",
            Self::DeepSeek => "deepseek",
        }
    }

    pub fn from_id(id: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|provider| provider.id() == id)
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Claude => "Claude (Anthropic)",
            Self::ChatGpt => "ChatGPT (OpenAI)",
            Self::Gemini => "Gemini (Google)",
            Self::DeepSeek => "DeepSeek",
        }
    }

    /// The model used unless the settings name another; models change
    /// often, so the settings let creators pick any.
    pub fn default_model(self) -> &'static str {
        match self {
            Self::Claude => DEFAULT_MODEL,
            Self::ChatGpt => "gpt-5-mini",
            Self::Gemini => "gemini-3.8-flash",
            Self::DeepSeek => "deepseek-chat",
        }
    }

    /// Environment variable that overrides the saved key.
    pub fn key_variable(self) -> &'static str {
        match self {
            Self::Claude => "ANTHROPIC_API_KEY",
            Self::ChatGpt => "OPENAI_API_KEY",
            Self::Gemini => "GEMINI_API_KEY",
            Self::DeepSeek => "DEEPSEEK_API_KEY",
        }
    }

    /// Where to get a key.
    pub fn key_page(self) -> &'static str {
        match self {
            Self::Claude => "https://platform.claude.com/settings/keys",
            Self::ChatGpt => "https://platform.openai.com/api-keys",
            Self::Gemini => "https://aistudio.google.com/apikey",
            Self::DeepSeek => "https://platform.deepseek.com/api_keys",
        }
    }

    /// OpenAI-compatible chat completions endpoint.
    fn chat_url(self) -> &'static str {
        match self {
            Self::Claude => API_URL,
            Self::ChatGpt => "https://api.openai.com/v1/chat/completions",
            Self::Gemini => {
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            }
            Self::DeepSeek => "https://api.deepseek.com/chat/completions",
        }
    }
}

/// The models `provider` offers with the saved key, sorted, for choosing
/// one in the settings (models are retired often).
pub fn list_models(provider: Provider) -> ServiceResult<Vec<String>> {
    let key =
        api_key(provider).ok_or_else(|| format!("Save a {} API key first", provider.label()))?;
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(20))
        .build()
        .map_err(|error| error.to_string())?;
    let request = match provider {
        Provider::Claude => client
            .get("https://api.anthropic.com/v1/models?limit=100")
            .header("x-api-key", &key)
            .header("anthropic-version", "2023-06-01"),
        Provider::ChatGpt => client
            .get("https://api.openai.com/v1/models")
            .bearer_auth(&key),
        Provider::Gemini => client
            .get("https://generativelanguage.googleapis.com/v1beta/openai/models")
            .bearer_auth(&key),
        Provider::DeepSeek => client
            .get("https://api.deepseek.com/models")
            .bearer_auth(&key),
    };
    let response = request
        .send()
        .map_err(|error| format!("could not reach {}: {error}", provider.label()))?;
    let status = response.status();
    let body: Value = response
        .json()
        .map_err(|error| format!("unexpected reply from {}: {error}", provider.label()))?;
    if !status.is_success() {
        let error = body.get(0).unwrap_or(&body);
        let message = error["error"]["message"]
            .as_str()
            .unwrap_or("unknown error");
        return Err(format!("{} returned {status}: {message}", provider.label()));
    }
    Ok(model_ids(provider, &body))
}

/// Chat models from a model list, without embedding, audio or image ones.
fn model_ids(provider: Provider, body: &Value) -> Vec<String> {
    let mut models = body["data"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|model| model["id"].as_str())
        .map(|id| id.trim_start_matches("models/").to_string())
        .filter(|id| {
            let excluded = [
                "embed",
                "tts",
                "audio",
                "image",
                "imagen",
                "whisper",
                "moderation",
                "dall-e",
                "realtime",
                "transcribe",
                "search",
                "live",
                "robotics",
                "banana",
                "computer-use",
                "customtools",
            ];
            !excluded.iter().any(|word| id.contains(word))
                && match provider {
                    Provider::ChatGpt => id.starts_with("gpt") || id.starts_with('o'),
                    Provider::Gemini => id.starts_with("gemini"),
                    _ => true,
                }
        })
        .collect::<Vec<_>>();
    models.sort();
    models.dedup();
    models
}

/// Sends a request, retrying a few times when the service is busy (429,
/// 503, 529): popular models are often briefly overloaded.
fn send_with_retry(
    name: &str,
    request: impl Fn() -> reqwest::blocking::RequestBuilder,
) -> ServiceResult<reqwest::blocking::Response> {
    const WAITS: [u64; 4] = [2, 5, 10, 20];
    let mut attempt = 0;
    loop {
        let response = request()
            .send()
            .map_err(|error| format!("could not reach {name}: {error}"))?;
        let busy = matches!(response.status().as_u16(), 429 | 503 | 529);
        match WAITS.get(attempt) {
            Some(&wait) if busy => {
                std::thread::sleep(Duration::from_secs(wait));
                attempt += 1;
            }
            _ => return Ok(response),
        }
    }
}

/// Drafts translations for a batch of texts. Shared between threads, so
/// one screen's texts can go out as several requests at once.
pub trait DraftTranslator: Send + Sync {
    fn translate(
        &self,
        pair: &LanguagePair,
        context: &Context<'_>,
        texts: &[String],
    ) -> ServiceResult<Vec<String>>;
}

/// The translator for `provider` with its saved key, or why there is none.
pub fn translator(provider: Provider, model: &str) -> ServiceResult<Box<dyn DraftTranslator>> {
    let key = api_key(provider).ok_or_else(|| {
        format!(
            "Add your {} API key in Settings → AI translation",
            provider.label()
        )
    })?;
    let model = if model.trim().is_empty() {
        provider.default_model()
    } else {
        model.trim()
    };
    Ok(match provider {
        Provider::Claude => Box::new(ClaudeTranslator::new(key, model)?),
        other => Box::new(ChatTranslator::new(other, key, model)?),
    })
}
/// Machine translation by an AI service while the overlay runs. A screen's
/// new texts go out as a few requests at once, each reported as soon as it
/// arrives: a model writes one reply's translations one after another, so
/// one request for the whole screen would make every text wait for all.
pub struct LiveTranslator {
    translator: Box<dyn DraftTranslator>,
    /// The window being translated, so translations fit the game.
    application: String,
}

impl LiveTranslator {
    pub fn new(translator: Box<dyn DraftTranslator>, application: impl Into<String>) -> Self {
        Self {
            translator,
            application: application.into(),
        }
    }
}

impl crate::services::TranslationBackend for LiveTranslator {
    fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
        self.translate_many(pair, &[text.to_string()])?
            .pop()
            .ok_or_else(|| "the AI returned no translation".to_string())
    }

    fn translate_many(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        let mut results = vec![None; texts.len()];
        self.translate_each(pair, texts, &mut |index, result| {
            results[index] = Some(result);
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
        if pair.source == pair.target {
            for (index, text) in texts.iter().enumerate() {
                done(index, Ok(text.clone()));
            }
            return;
        }
        let context = Context {
            application: &self.application,
            glossary: &[],
            notes: "",
        };
        let translator = &*self.translator;
        std::thread::scope(|scope| {
            let (sender, results) = std::sync::mpsc::channel();
            for indices in parallel_requests(texts.len()) {
                let sender = sender.clone();
                let context = &context;
                scope.spawn(move || {
                    let batch = indices
                        .iter()
                        .map(|&index| texts[index].clone())
                        .collect::<Vec<_>>();
                    let _ = sender.send((indices, translator.translate(pair, context, &batch)));
                });
            }
            drop(sender);
            // In the order the requests finish.
            for (indices, result) in results {
                match result {
                    Ok(translations) if translations.len() == indices.len() => {
                        for (index, translation) in indices.into_iter().zip(translations) {
                            done(index, Ok(translation));
                        }
                    }
                    Ok(_) => {
                        for index in indices {
                            done(
                                index,
                                Err("the AI returned the wrong number of translations".into()),
                            );
                        }
                    }
                    Err(error) => {
                        for index in indices {
                            done(index, Err(error.clone()));
                        }
                    }
                }
            }
        });
    }
}

/// Requests sent at once for one screen.
pub const PARALLEL_REQUESTS: usize = 4;

/// Splits `count` texts into requests sent at once: up to
/// [`PARALLEL_REQUESTS`], more only to keep each within [`BATCH_SIZE`].
/// Texts are dealt out in turn, so each request gets a share of the short
/// ones, which then arrive early.
fn parallel_requests(count: usize) -> Vec<Vec<usize>> {
    let requests = count.div_ceil(BATCH_SIZE).max(count.min(PARALLEL_REQUESTS));
    (0..requests)
        .map(|first| (first..count).step_by(requests).collect())
        .collect()
}

/// Texts per request; larger screens are split.
pub const BATCH_SIZE: usize = 40;
/// Glossary entries sent with each request.
const MAX_GLOSSARY: usize = 150;

pub(crate) fn config_dir() -> Option<PathBuf> {
    // Windows has no HOME; settings live in %APPDATA%\Overlayr.
    if cfg!(windows) {
        return std::env::var_os("APPDATA")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .map(|path| path.join("Overlayr"));
    }
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
    Some(config.join("overlayr"))
}

/// Where a provider's API key is kept: readable only by the user, not in
/// the settings file.
pub fn key_path(provider: Provider) -> Option<PathBuf> {
    Some(config_dir()?.join("keys").join(provider.id()))
}

/// The API key: the provider's environment variable, else the saved one.
pub fn api_key(provider: Provider) -> Option<String> {
    let saved = || {
        std::fs::read_to_string(key_path(provider)?)
            .ok()
            // Claude's key was first saved here.
            .or_else(|| {
                (provider == Provider::Claude)
                    .then(|| std::fs::read_to_string(config_dir()?.join("anthropic_api_key")).ok())
                    .flatten()
            })
    };
    std::env::var(provider.key_variable())
        .ok()
        .or_else(saved)
        .map(|key| key.trim().to_string())
        .filter(|key| !key.is_empty())
}

/// Saves (or, when empty, removes) a provider's API key.
pub fn save_api_key(provider: Provider, key: &str) -> ServiceResult<()> {
    let path =
        key_path(provider).ok_or_else(|| "could not find your config directory".to_string())?;
    let key = key.trim();
    if key.is_empty() {
        let _ = std::fs::remove_file(&path);
        if provider == Provider::Claude
            && let Some(directory) = config_dir()
        {
            let _ = std::fs::remove_file(directory.join("anthropic_api_key"));
        }
        return Ok(());
    }
    if let Some(directory) = path.parent() {
        std::fs::create_dir_all(directory).map_err(|error| error.to_string())?;
    }
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&path)
            .map_err(|error| format!("could not save the key: {error}"))?;
        file.write_all(key.as_bytes())
            .map_err(|error| format!("could not save the key: {error}"))
    }
    #[cfg(not(unix))]
    std::fs::write(&path, key).map_err(|error| format!("could not save the key: {error}"))
}

/// What the drafts are for, so they fit the game.
pub struct Context<'a> {
    /// The game or application.
    pub application: &'a str,
    /// The creator's confirmed translations: (source, translation).
    pub glossary: &'a [(String, String)],
    /// The creator's notes on the game and how to translate it.
    pub notes: &'a str,
}

pub struct ClaudeTranslator {
    client: reqwest::blocking::Client,
    api_key: String,
    model: String,
}

impl ClaudeTranslator {
    pub fn new(api_key: String, model: impl Into<String>) -> ServiceResult<Self> {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(300))
            .build()
            .map_err(|error| format!("could not create the HTTP client: {error}"))?;
        Ok(Self {
            client,
            api_key,
            model: model.into(),
        })
    }
}

impl DraftTranslator for ClaudeTranslator {
    /// Translates `texts`, returning one translation per text in order.
    fn translate(
        &self,
        pair: &LanguagePair,
        context: &Context<'_>,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }
        let body = request_body(&self.model, pair, context, texts);
        let response = send_with_retry("Claude", || {
            let request = self
                .client
                .post(API_URL)
                .header("x-api-key", &self.api_key)
                .header("anthropic-version", "2023-06-01");
            // If the model declines, Anthropic's recommended fallback model
            // answers instead.
            let request = if body.get("fallbacks").is_some() {
                request.header("anthropic-beta", "server-side-fallback-2026-07-01")
            } else {
                request
            };
            request.json(&body)
        })?;
        let status = response.status();
        let body: Value = response
            .json()
            .map_err(|error| format!("unexpected reply from Claude: {error}"))?;
        if !status.is_success() {
            let message = body["error"]["message"].as_str().unwrap_or("unknown error");
            return Err(match status.as_u16() {
                401 => "Claude rejected the API key".to_string(),
                429 => format!("Claude is busy (rate limited): {message}"),
                _ => format!("Claude returned {status}: {message}"),
            });
        }
        parse_translations(&body, texts.len())
    }
}

/// The instructions and the texts to translate, shared by all providers.
fn prompt(pair: &LanguagePair, context: &Context<'_>, texts: &[String]) -> (String, String) {
    let glossary = context
        .glossary
        .iter()
        .take(MAX_GLOSSARY)
        .map(|(source, translation)| json!({ "source": source, "translation": translation }))
        .collect::<Vec<_>>();
    let items = texts
        .iter()
        .enumerate()
        .map(|(id, text)| json!({ "id": id, "text": text }))
        .collect::<Vec<_>>();
    let system = format!(
        "You localize video games. Translate on-screen text of \"{application}\" from {source} \
         to {target} for players.\n\
         - Menu items, buttons, labels and titles: short and natural, the way {target} games \
         phrase them; about as long as the original so they fit the same space.\n\
         - Descriptions and dialogue: fluent and natural, not word for word.\n\
         - Keep names of mods, items, characters and places consistent with the glossary. \
         Leave brand and mod names that players know in the original form unless the glossary \
         translates them.\n\
         - The text was read by OCR and may contain small misreadings; translate what was \
         clearly meant. OCR also reads icons and decorations as letters: return text that isn't \
         real words (such as \"toct\" or \"COUOO\") unchanged, never guess a meaning for it.\n\
         - Keep numbers, symbols and formatting such as \"(440)\" or \"&\".",
        application = if context.application.is_empty() {
            "the game"
        } else {
            context.application
        },
        source = language_name(&pair.source),
        target = language_name(&pair.target),
    );
    let notes = context.notes.trim();
    let system = if notes.is_empty() {
        system
    } else {
        format!(
            "{system}\n\nAbout the game and how to translate it, from the translator \
             (follow these notes; they take precedence over the general guidance above):\n{notes}"
        )
    };
    let user = format!(
        "Glossary (the creator's own translations, follow them):\n{}\n\n\
         Translate each text and return its id with the translation:\n{}",
        serde_json::to_string_pretty(&glossary).unwrap_or_default(),
        serde_json::to_string_pretty(&items).unwrap_or_default(),
    );
    (system, user)
}

/// Whether `model` takes `output_config.effort`; older models, Haiku 4.5
/// among them, reject the whole request when it's sent.
fn supports_effort(model: &str) -> bool {
    [
        "claude-fable-",
        "claude-mythos-",
        "claude-opus-5",
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-haiku-5",
    ]
    .iter()
    .any(|prefix| model.starts_with(prefix))
}

/// Whether `model` takes `fallbacks: "default"`, which lets another model
/// answer when it declines; others reject the request when it's sent.
fn supports_fallbacks(model: &str) -> bool {
    [
        "claude-fable-5-1",
        "claude-mythos-5-1",
        "claude-opus-5",
        "claude-sonnet-5-5",
    ]
    .iter()
    .any(|prefix| model.starts_with(prefix))
}

fn request_body(
    model: &str,
    pair: &LanguagePair,
    context: &Context<'_>,
    texts: &[String],
) -> Value {
    let (system, user) = prompt(pair, context, texts);
    let mut body = json!({
        "model": model,
        "max_tokens": 16000,
        "system": system,
        "messages": [{ "role": "user", "content": user }],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "translations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": { "type": "integer" },
                                    "translation": { "type": "string" }
                                },
                                "required": ["id", "translation"],
                                "additionalProperties": false
                            }
                        }
                    },
                    "required": ["translations"],
                    "additionalProperties": false
                }
            }
        }
    });
    if supports_effort(model) {
        body["output_config"]["effort"] = json!("medium");
    }
    if supports_fallbacks(model) {
        body["fallbacks"] = json!("default");
    }
    body
}

/// Reads the translations out of a Messages API response, in input order.
fn parse_translations(body: &Value, count: usize) -> ServiceResult<Vec<String>> {
    match body["stop_reason"].as_str() {
        Some("refusal") => return Err("Claude declined to translate these texts".into()),
        Some("max_tokens") => return Err("Claude's reply was cut off; try fewer texts".into()),
        _ => {}
    }
    let text = body["content"]
        .as_array()
        .and_then(|blocks| {
            blocks
                .iter()
                .find(|block| block["type"] == "text")
                .and_then(|block| block["text"].as_str())
        })
        .ok_or_else(|| "Claude's reply had no text".to_string())?;
    read_translations(text, count)
}

/// Reads `{"translations": [{"id", "translation"}]}` from a model's reply
/// text, in input order. Tolerates a Markdown code fence around it.
fn read_translations(text: &str, count: usize) -> ServiceResult<Vec<String>> {
    let text = text.trim();
    let text = text
        .strip_prefix("```json")
        .or_else(|| text.strip_prefix("```"))
        .map_or(text, |inner| inner.trim_end().trim_end_matches("```"))
        .trim();
    let parsed: Value =
        serde_json::from_str(text).map_err(|error| format!("the reply wasn't JSON: {error}"))?;
    let mut translations = vec![String::new(); count];
    for item in parsed["translations"].as_array().into_iter().flatten() {
        if let (Some(id), Some(translation)) = (item["id"].as_u64(), item["translation"].as_str())
            && let Some(slot) = translations.get_mut(id as usize)
        {
            *slot = translation.trim().to_string();
        }
    }
    Ok(translations)
}

/// ChatGPT, Gemini and DeepSeek, through their OpenAI-compatible chat APIs.
pub struct ChatTranslator {
    provider: Provider,
    client: reqwest::blocking::Client,
    api_key: String,
    model: String,
}

impl ChatTranslator {
    pub fn new(
        provider: Provider,
        api_key: String,
        model: impl Into<String>,
    ) -> ServiceResult<Self> {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(300))
            .build()
            .map_err(|error| format!("could not create the HTTP client: {error}"))?;
        Ok(Self {
            provider,
            client,
            api_key,
            model: model.into(),
        })
    }
}

fn chat_body(model: &str, pair: &LanguagePair, context: &Context<'_>, texts: &[String]) -> Value {
    let (system, user) = prompt(pair, context, texts);
    json!({
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": format!(
                    "{system}\n\nReply with JSON only, in this shape: \
                     {{\"translations\": [{{\"id\": 0, \"translation\": \"...\"}}]}}"
                )
            },
            { "role": "user", "content": user }
        ],
        "response_format": { "type": "json_object" }
    })
}

impl DraftTranslator for ChatTranslator {
    fn translate(
        &self,
        pair: &LanguagePair,
        context: &Context<'_>,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }
        let name = self.provider.label();
        let body = chat_body(&self.model, pair, context, texts);
        let response = send_with_retry(name, || {
            self.client
                .post(self.provider.chat_url())
                .bearer_auth(&self.api_key)
                .json(&body)
        })?;
        let status = response.status();
        let body: Value = response
            .json()
            .map_err(|error| format!("unexpected reply from {name}: {error}"))?;
        if !status.is_success() {
            // Gemini wraps errors in a list.
            let error = body.get(0).unwrap_or(&body);
            let message = error["error"]["message"]
                .as_str()
                .unwrap_or("unknown error");
            return Err(match status.as_u16() {
                401 | 403 => format!("{name} rejected the API key: {message}"),
                429 => format!("{name} is busy or out of credit: {message}"),
                _ => format!("{name} returned {status}: {message}"),
            });
        }
        let choice = &body["choices"][0];
        if choice["finish_reason"] == "length" {
            return Err(format!("{name}'s reply was cut off; try fewer texts"));
        }
        let text = choice["message"]["content"]
            .as_str()
            .ok_or_else(|| format!("{name}'s reply had no text"))?;
        read_translations(text, texts.len()).map_err(|error| format!("{name}: {error}"))
    }
}

fn language_name(code: &str) -> String {
    match code.to_ascii_lowercase().as_str() {
        "en" => "English",
        "ja" => "Japanese",
        "zh" | "zh-cn" => "Simplified Chinese",
        "zh-tw" => "Traditional Chinese",
        "ko" => "Korean",
        "th" => "Thai",
        "vi" => "Vietnamese",
        "id" => "Indonesian",
        "es" => "Spanish",
        "pt" => "Portuguese",
        "fr" => "French",
        "de" => "German",
        "it" => "Italian",
        "ru" => "Russian",
        other => return other.to_string(),
    }
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::TranslationBackend;

    /// Answers each text with its upper-case form and counts requests.
    struct Shouting(std::sync::Arc<std::sync::atomic::AtomicUsize>);

    impl DraftTranslator for Shouting {
        fn translate(
            &self,
            _pair: &LanguagePair,
            context: &Context<'_>,
            texts: &[String],
        ) -> ServiceResult<Vec<String>> {
            assert_eq!(context.application, "StoneBlock");
            self.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            Ok(texts.iter().map(|text| text.to_uppercase()).collect())
        }
    }

    #[test]
    fn live_translation_sends_a_few_requests_at_once_and_reports_every_text() {
        let requests = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let mut live = LiveTranslator::new(Box::new(Shouting(requests.clone())), "StoneBlock");
        let texts = (0..BATCH_SIZE + 3)
            .map(|index| format!("line {index}"))
            .collect::<Vec<_>>();
        let mut reported = vec![None; texts.len()];
        live.translate_each(
            &LanguagePair::new("en", "th"),
            &texts,
            &mut |index, result| {
                reported[index] = Some(result.unwrap());
            },
        );
        assert_eq!(
            requests.load(std::sync::atomic::Ordering::Relaxed),
            PARALLEL_REQUESTS
        );
        assert_eq!(reported[BATCH_SIZE + 2].as_deref(), Some("LINE 42"));
        assert!(reported.iter().all(Option::is_some));
    }

    #[test]
    fn splits_a_screen_into_requests_within_the_batch_size() {
        assert!(parallel_requests(0).is_empty());
        assert_eq!(parallel_requests(2), vec![vec![0], vec![1]]);
        assert_eq!(
            parallel_requests(6),
            vec![vec![0, 4], vec![1, 5], vec![2], vec![3]]
        );
        let large = parallel_requests(BATCH_SIZE * 5 + 1);
        assert_eq!(large.len(), 6);
        assert!(large.iter().all(|request| request.len() <= BATCH_SIZE));
        assert_eq!(
            large.iter().map(Vec::len).sum::<usize>(),
            BATCH_SIZE * 5 + 1
        );
    }

    /// Takes a while per request, like a real service.
    struct Slow;

    impl DraftTranslator for Slow {
        fn translate(
            &self,
            _pair: &LanguagePair,
            _context: &Context<'_>,
            texts: &[String],
        ) -> ServiceResult<Vec<String>> {
            std::thread::sleep(std::time::Duration::from_millis(300));
            Ok(texts.to_vec())
        }
    }

    #[test]
    fn requests_for_one_screen_run_at_the_same_time() {
        let mut live = LiveTranslator::new(Box::new(Slow), "StoneBlock");
        let texts = (0..4).map(|index| index.to_string()).collect::<Vec<_>>();
        let started = std::time::Instant::now();
        live.translate_many(&LanguagePair::new("en", "th"), &texts)
            .unwrap();
        assert!(started.elapsed() < std::time::Duration::from_millis(900));
    }

    #[test]
    fn request_carries_context_glossary_and_a_strict_schema() {
        let glossary = [("Game Menu".to_string(), "เมนูเกม".to_string())];
        let body = request_body(
            DEFAULT_MODEL,
            &LanguagePair::new("en", "th"),
            &Context {
                application: "FTB StoneBlock 4",
                glossary: &glossary,
                notes: "A Minecraft skyblock modpack. Keep mod names in English.",
            },
            &["Rewards".to_string(), "Tasks".to_string()],
        );
        assert_eq!(body["model"], DEFAULT_MODEL);
        assert_eq!(body["fallbacks"], "default");
        let system = body["system"].as_str().unwrap();
        assert!(system.contains("FTB StoneBlock 4") && system.contains("to Thai"));
        assert!(system.contains("Keep mod names in English"));
        let user = body["messages"][0]["content"].as_str().unwrap();
        assert!(user.contains("เมนูเกม") && user.contains("\"Rewards\""));
        assert_eq!(body["output_config"]["format"]["type"], "json_schema");
    }

    #[test]
    fn older_models_get_only_the_options_they_accept() {
        let pair = LanguagePair::new("en", "th");
        let context = Context {
            application: "",
            glossary: &[],
            notes: "",
        };
        let texts = ["Cobblestone".to_string()];
        let haiku = request_body("claude-haiku-4-5-20251001", &pair, &context, &texts);
        assert!(haiku["output_config"].get("effort").is_none());
        assert!(haiku.get("fallbacks").is_none());
        let new_haiku = request_body("claude-haiku-5-5", &pair, &context, &texts);
        assert_eq!(new_haiku["output_config"]["effort"], "medium");
        assert!(new_haiku.get("fallbacks").is_none());
        let opus = request_body("claude-opus-5-5", &pair, &context, &texts);
        assert_eq!(opus["output_config"]["effort"], "medium");
        assert_eq!(opus["fallbacks"], "default");
    }

    #[test]
    fn reads_translations_by_id_in_input_order() {
        let body = json!({
            "stop_reason": "end_turn",
            "content": [
                { "type": "thinking", "thinking": "" },
                { "type": "text", "text": "{\"translations\":[{\"id\":1,\"translation\":\"ภารกิจ\"},{\"id\":0,\"translation\":\" รางวัล \"}]}" }
            ]
        });
        assert_eq!(
            parse_translations(&body, 2).unwrap(),
            ["รางวัล".to_string(), "ภารกิจ".to_string()]
        );
    }

    #[test]
    fn chat_requests_ask_for_json_and_replies_may_be_fenced() {
        let body = chat_body(
            "gpt-5-mini",
            &LanguagePair::new("en", "th"),
            &Context {
                application: "Game",
                glossary: &[],
                notes: "",
            },
            &["Tasks".to_string()],
        );
        assert_eq!(body["response_format"]["type"], "json_object");
        assert!(
            body["messages"][0]["content"]
                .as_str()
                .unwrap()
                .contains("JSON")
        );
        let fenced = "```json\n{\"translations\":[{\"id\":0,\"translation\":\"ภารกิจ\"}]}\n```";
        assert_eq!(read_translations(fenced, 1).unwrap(), ["ภารกิจ".to_string()]);
    }

    #[test]
    fn model_lists_keep_chat_models_only() {
        let body = json!({ "data": [
            { "id": "models/gemini-3.8-flash" },
            { "id": "models/text-embedding-004" },
            { "id": "models/gemini-embedding-001" },
            { "id": "models/imagen-4" }
        ]});
        assert_eq!(model_ids(Provider::Gemini, &body), ["gemini-3.8-flash"]);
        let body =
            json!({ "data": [{ "id": "gpt-5-mini" }, { "id": "whisper-1" }, { "id": "o4" }] });
        assert_eq!(model_ids(Provider::ChatGpt, &body), ["gpt-5-mini", "o4"]);
    }

    #[test]
    fn providers_round_trip_their_ids() {
        for provider in Provider::ALL {
            assert_eq!(Provider::from_id(provider.id()), Some(provider));
        }
        assert_eq!(Provider::from_id("libretranslate"), None);
    }

    #[test]
    fn reports_refusals_and_cut_off_replies() {
        let refusal = json!({ "stop_reason": "refusal", "content": [] });
        assert!(parse_translations(&refusal, 1).is_err());
        let cut = json!({ "stop_reason": "max_tokens", "content": [] });
        assert!(parse_translations(&cut, 1).is_err());
    }
}
