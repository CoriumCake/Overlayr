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
}
