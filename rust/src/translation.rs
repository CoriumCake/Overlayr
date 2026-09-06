use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};

use crate::services::{ServiceResult, TranslationBackend};
use crate::types::LanguagePair;

/// OpenAI-compatible, self-hostable translation boundary based on the
/// LibreTranslate HTTP API. Keeping the endpoint configurable supports a
/// local/private deployment for production use and avoids hard-coding a
/// consumer web endpoint into the application.
#[derive(Clone)]
pub struct LibreTranslate {
    client: Client,
    endpoint: String,
    api_key: Option<String>,
}

#[derive(Serialize)]
struct Request<'a> {
    q: &'a str,
    source: &'a str,
    target: &'a str,
    format: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    api_key: Option<&'a str>,
}

#[derive(Deserialize)]
struct Response {
    #[serde(rename = "translatedText")]
    translated_text: String,
}

impl LibreTranslate {
    pub fn new(endpoint: impl Into<String>, api_key: Option<String>) -> ServiceResult<Self> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .build()
            .map_err(|error| format!("could not create translation client: {error}"))?;
        Ok(Self {
            client,
            endpoint: endpoint.into(),
            api_key,
        })
    }
}

impl TranslationBackend for LibreTranslate {
    fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
        if text.trim().is_empty() || pair.source == pair.target {
            return Ok(text.to_string());
        }
        let request = Request {
            q: text,
            source: &pair.source,
            target: &pair.target,
            format: "text",
            api_key: self.api_key.as_deref(),
        };
        let response = self
            .client
            .post(&self.endpoint)
            .json(&request)
            .send()
            .map_err(|error| format!("translation request failed: {error}"))?;
        let response = response
            .error_for_status()
            .map_err(|error| format!("translation service rejected request: {error}"))?;
        response
            .json::<Response>()
            .map(|body| body.translated_text)
            .map_err(|error| format!("invalid translation response: {error}"))
    }
}
