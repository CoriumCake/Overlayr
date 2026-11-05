use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};

use crate::services::{ServiceResult, TranslationBackend};
use crate::types::LanguagePair;

/// Self-hostable translation boundary based on the LibreTranslate HTTP API.
/// Keeping the endpoint configurable supports a
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
struct LanguageEntry {
    code: String,
}

/// Maps Overlayr's language codes to LibreTranslate's, which drop the region
/// except for Traditional Chinese.
pub fn libretranslate_code(language: &str) -> String {
    match language {
        "zh-TW" | "zh-Hant" => "zt".into(),
        other => other
            .split('-')
            .next()
            .unwrap_or(other)
            .to_ascii_lowercase(),
    }
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

    /// Check that the configured translation service is reachable without
    /// sending user text. A LibreTranslate `/translate` endpoint commonly
    /// answers GET with 404/405; any HTTP response still proves the service is
    /// reachable, so only transport failures are rejected.
    pub fn check_reachable(&self) -> ServiceResult<()> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(3))
            .build()
            .map_err(|error| format!("could not create translation health client: {error}"))?;
        client
            .get(&self.endpoint)
            .send()
            .map(|_| ())
            .map_err(|error| format!("translation endpoint is unreachable: {error}"))
    }

    /// Language codes the server has models for, from LibreTranslate's
    /// `/languages` endpoint next to `/translate`.
    pub fn languages(&self) -> ServiceResult<Vec<String>> {
        let base = self
            .endpoint
            .trim_end_matches('/')
            .strip_suffix("/translate")
            .ok_or_else(|| "endpoint is not a LibreTranslate /translate URL".to_string())?;
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(3))
            .build()
            .map_err(|error| format!("could not create translation health client: {error}"))?;
        client
            .get(format!("{base}/languages"))
            .send()
            .and_then(reqwest::blocking::Response::error_for_status)
            .map_err(|error| format!("could not list languages: {error}"))?
            .json::<Vec<LanguageEntry>>()
            .map(|entries| entries.into_iter().map(|entry| entry.code).collect())
            .map_err(|error| format!("invalid language list: {error}"))
    }
}

/// Requests sent to the translation service at the same time.
const CONCURRENT_REQUESTS: usize = 4;

impl TranslationBackend for LibreTranslate {
    fn translate(&mut self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
        self.request(pair, text)
    }

    fn translate_many(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
    ) -> ServiceResult<Vec<String>> {
        // LibreTranslate serves several requests at once; sending them in
        // parallel cuts the wait for a screen full of new text.
        let this = &*self;
        let mut results = Vec::with_capacity(texts.len());
        for chunk in texts.chunks(CONCURRENT_REQUESTS) {
            let translated = std::thread::scope(|scope| {
                let requests = chunk
                    .iter()
                    .map(|text| scope.spawn(move || this.request(pair, text)))
                    .collect::<Vec<_>>();
                requests
                    .into_iter()
                    .map(|request| {
                        request
                            .join()
                            .unwrap_or_else(|_| Err("translation request crashed".into()))
                    })
                    .collect::<Vec<_>>()
            });
            for result in translated {
                results.push(result?);
            }
        }
        Ok(results)
    }

    fn translate_each(
        &mut self,
        pair: &LanguagePair,
        texts: &[String],
        done: &mut dyn FnMut(usize, ServiceResult<String>),
    ) {
        // A few requests run at once; each result is reported the moment
        // it arrives, and the next text starts in its place.
        let this = &*self;
        let next = std::sync::atomic::AtomicUsize::new(0);
        let (sender, results) = std::sync::mpsc::channel();
        std::thread::scope(|scope| {
            for _ in 0..CONCURRENT_REQUESTS.min(texts.len()) {
                let (sender, next) = (sender.clone(), &next);
                scope.spawn(move || {
                    loop {
                        let index = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        let Some(text) = texts.get(index) else {
                            return;
                        };
                        if sender.send((index, this.request(pair, text))).is_err() {
                            return;
                        }
                    }
                });
            }
            drop(sender);
            for (index, result) in results {
                done(index, result);
            }
        });
    }
}

impl LibreTranslate {
    fn request(&self, pair: &LanguagePair, text: &str) -> ServiceResult<String> {
        if text.trim().is_empty() || pair.source == pair.target {
            return Ok(text.to_string());
        }
        let source = libretranslate_code(&pair.source);
        let target = libretranslate_code(&pair.target);
        let request = Request {
            q: text,
            source: &source,
            target: &target,
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

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    use super::*;

    #[test]
    fn translates_through_libretranslate_compatible_http_api() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("test server bind failed");
        let address = listener.local_addr().expect("test server address failed");
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("test request was not received");
            let mut request = [0_u8; 4096];
            let bytes_read = stream.read(&mut request).expect("test request read failed");
            let request = String::from_utf8_lossy(&request[..bytes_read]);
            assert!(request.starts_with("POST /translate HTTP/1.1"));
            assert!(request.contains("\"source\":\"ja\""));
            assert!(request.contains("\"target\":\"th\""));

            let body = r#"{"translatedText":"แมว"}"#.as_bytes();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
            .expect("test response headers failed");
            stream.write_all(body).expect("test response body failed");
        });

        let mut provider = LibreTranslate::new(format!("http://{address}/translate"), None)
            .expect("provider creation failed");
        let translated = provider
            .translate(&LanguagePair::new("ja", "th"), "猫")
            .expect("translation request failed");
        assert_eq!(translated, "แมว");
        server.join().expect("test server panicked");
    }

    #[test]
    fn avoids_http_request_for_empty_or_same_language_text() {
        let mut provider = LibreTranslate::new("http://127.0.0.1:1/translate", None)
            .expect("provider creation failed");
        let pair = LanguagePair::new("ja", "ja");
        assert_eq!(provider.translate(&pair, "猫").unwrap(), "猫");
        assert_eq!(
            provider
                .translate(&LanguagePair::new("ja", "th"), " ")
                .unwrap(),
            " "
        );
    }

    #[test]
    fn reports_unreachable_translation_endpoint() {
        let provider = LibreTranslate::new("http://127.0.0.1:1/translate", None)
            .expect("provider creation failed");
        assert!(provider.check_reachable().is_err());
    }
}
