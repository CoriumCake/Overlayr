//! The translation pack marketplace: browse, install and publish packs.
//!
//! Installed packs stay encrypted on disk. Each install is encrypted for the
//! user under its own licence key, which the server hands out only to that
//! logged-in user, each time the pack starts. The pack is decrypted in memory
//! and never written out in plain text, and the studio can't open it, so
//! players can use a creator's work without getting a copy of the file.
//!
//! This stops copying the pack file. It can't stop a determined attacker
//! from reading the running app's memory, as the overlay has to show the
//! translations somewhere.

use std::fs;
use std::io;
use std::path::PathBuf;
use std::time::Duration;

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::pack::Pack;
use crate::services::ServiceResult;

/// `AppConfig::active_pack` value for an installed marketplace pack.
pub const ACTIVE_PREFIX: &str = "market:";
const LOCKED_EXTENSION: &str = "overlayr-locked";
const DEFAULT_API: &str = "https://overlayr.app";

/// The Overlayr website, which hosts the marketplace API.
pub fn api_base() -> String {
    std::env::var("OVERLAYR_API_URL")
        .ok()
        .map(|url| url.trim_end_matches('/').to_owned())
        .filter(|url| !url.is_empty())
        .unwrap_or_else(|| DEFAULT_API.into())
}

/// Where the website's login sends the session back to the app.
pub fn login_url() -> String {
    format!("{}/login?callback=desktop", api_base())
}

/// The website page that pays for `id` and comes back to say it's bought.
pub fn buy_url(id: &str) -> String {
    format!("{}/buy?pack={}", api_base(), url_encode(id))
}

/// Where creators set up payouts before selling packs.
pub fn payouts_url() -> String {
    format!("{}/profile#creator", api_base())
}

/// A pack as the marketplace shows it; never includes its translations.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Listing {
    pub id: String,
    pub title: String,
    pub description: String,
    pub application: String,
    pub source_lang: String,
    pub target_lang: String,
    /// Translated entries.
    pub entries: u32,
    pub author: String,
    pub owner_uid: String,
    /// The cover picture, relative to the API (empty without one).
    pub cover_url: String,
    /// In satang (1/100 baht); 0 is free.
    pub price: u32,
    pub version: u32,
    pub downloads: u64,
    pub size: u64,
    pub created_at: String,
    pub updated_at: String,
}

impl Listing {
    /// "Free", or the price in baht, such as "฿49" or "฿49.50".
    pub fn price_label(&self) -> String {
        match (self.price / 100, self.price % 100) {
            (0, 0) => "Free".into(),
            (baht, 0) => format!("฿{baht}"),
            (baht, satang) => format!("฿{baht}.{satang:02}"),
        }
    }
}

/// An installed pack: its listing and the pack encrypted for this user.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Installed {
    pub listing: Listing,
    nonce: String,
    ciphertext: String,
}

fn installed_dir() -> Option<PathBuf> {
    Some(crate::pack::packs_dir()?.parent()?.join("marketplace"))
}

fn installed_path(id: &str) -> Option<PathBuf> {
    valid_id(id).then(|| installed_dir().map(|dir| dir.join(format!("{id}.{LOCKED_EXTENSION}"))))?
}

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

/// Installed packs, sorted by title.
pub fn installed() -> Vec<Installed> {
    let Some(entries) = installed_dir().and_then(|dir| fs::read_dir(dir).ok()) else {
        return Vec::new();
    };
    let mut packs = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().is_some_and(|ext| ext == LOCKED_EXTENSION))
        .filter_map(|path| serde_json::from_slice::<Installed>(&fs::read(path).ok()?).ok())
        .collect::<Vec<_>>();
    packs.sort_by_key(|pack| pack.listing.title.to_lowercase());
    packs
}

pub fn installed_listing(id: &str) -> Option<Listing> {
    let path = installed_path(id)?;
    serde_json::from_slice::<Installed>(&fs::read(path).ok()?)
        .ok()
        .map(|installed| installed.listing)
}

pub fn uninstall(id: &str) -> io::Result<()> {
    let path = installed_path(id).ok_or_else(|| io::Error::other("invalid pack id"))?;
    match fs::remove_file(path) {
        Err(error) if error.kind() != io::ErrorKind::NotFound => Err(error),
        _ => Ok(()),
    }
}

fn client() -> ServiceResult<reqwest::blocking::Client> {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|error| error.to_string())
}

/// Calls an API route; returns the status and JSON body.
fn call(path: &str, body: Option<Value>, signed_in: bool) -> ServiceResult<(StatusCode, Value)> {
    let base = api_base();
    let client = client()?;
    let mut request = match &body {
        Some(body) => client.post(format!("{base}{path}")).json(body),
        None => client.get(format!("{base}{path}")),
    };
    if signed_in {
        request = request.bearer_auth(crate::auth::id_token(&base)?);
    }
    let response = request
        .send()
        .map_err(|error| format!("could not reach the marketplace: {error}"))?;
    let status = response.status();
    let body = response
        .json::<Value>()
        .map_err(|error| format!("unexpected reply from the marketplace ({status}): {error}"))?;
    Ok((status, body))
}

fn api_error(status: StatusCode, body: &Value) -> String {
    body["error"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| format!("the marketplace returned {status}"))
}

/// One page of the marketplace.
#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Page {
    pub listings: Vec<Listing>,
    /// Where the next page starts, if there is one.
    pub next_offset: Option<u32>,
}

/// A page of packs, newest first, matching `query` if not empty.
pub fn list(query: &str, offset: u32) -> ServiceResult<Page> {
    let mut path = format!("/api/marketplace/list?offset={offset}");
    if !query.trim().is_empty() {
        path.push_str(&format!("&q={}", url_encode(query.trim())));
    }
    let (status, body) = call(&path, None, false)?;
    if !status.is_success() {
        return Err(api_error(status, &body));
    }
    serde_json::from_value(body).map_err(|error| error.to_string())
}

/// Largest cover picture accepted.
const MAX_COVER_BYTES: u64 = 1_000_000;

/// Downloads a listing's cover picture (PNG).
pub fn cover(listing: &Listing) -> ServiceResult<Vec<u8>> {
    let url = match &listing.cover_url {
        url if url.is_empty() => return Err("no cover".into()),
        url if url.starts_with('/') => format!("{}{url}", api_base()),
        url => url.clone(),
    };
    let response = client()?
        .get(url)
        .send()
        .and_then(reqwest::blocking::Response::error_for_status)
        .map_err(|error| format!("could not download the cover: {error}"))?;
    if response
        .content_length()
        .is_some_and(|length| length > MAX_COVER_BYTES)
    {
        return Err("the cover is too large".into());
    }
    let bytes = response.bytes().map_err(|error| error.to_string())?;
    Ok(bytes.to_vec())
}

/// Publishes `pack`, or a new version of it, at `price` satang (0 for free).
/// Players who installed an older version get the new one the next time they
/// start it. Selling needs payouts set up on the website first.
pub fn publish(
    pack: &Pack,
    title: &str,
    description: &str,
    cover_png: Option<&[u8]>,
    price: u32,
) -> ServiceResult<Listing> {
    let pack_json = serde_json::to_string(pack).map_err(|error| error.to_string())?;
    let (status, body) = call(
        "/api/marketplace/publish",
        Some(json!({
            "pack": pack_json,
            "title": title,
            "description": description,
            "cover": cover_png.map(|png| BASE64.encode(png)).unwrap_or_default(),
            "price": price,
        })),
        true,
    )?;
    if !status.is_success() {
        return Err(api_error(status, &body));
    }
    serde_json::from_value(body["listing"].clone()).map_err(|error| error.to_string())
}

/// IDs of the packs this account bought.
pub fn owned() -> ServiceResult<Vec<String>> {
    let (status, body) = call("/api/marketplace/owned", None, true)?;
    if !status.is_success() {
        return Err(api_error(status, &body));
    }
    serde_json::from_value(body["packs"].clone()).map_err(|error| error.to_string())
}

/// Removes the user's own pack from the marketplace.
pub fn unpublish(id: &str) -> ServiceResult<()> {
    let (status, body) = call(
        "/api/marketplace/unpublish",
        Some(json!({ "id": id })),
        true,
    )?;
    if !status.is_success() {
        return Err(api_error(status, &body));
    }
    Ok(())
}

/// Downloads the pack, encrypted for this user, into the installed packs.
pub fn install(id: &str) -> ServiceResult<Listing> {
    let path = installed_path(id).ok_or("invalid pack id")?;
    let (status, body) = call("/api/marketplace/download", Some(json!({ "id": id })), true)?;
    if !status.is_success() {
        return Err(api_error(status, &body));
    }
    let installed: Installed = serde_json::from_value(body).map_err(|error| error.to_string())?;
    if installed.listing.id != id {
        return Err("the marketplace sent a different pack".into());
    }
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|error| error.to_string())?;
    }
    let temp = path.with_extension("tmp");
    let bytes = serde_json::to_vec(&installed).map_err(|error| error.to_string())?;
    fs::write(&temp, bytes)
        .and_then(|()| fs::rename(&temp, &path))
        .map_err(|error| format!("could not save the pack: {error}"))?;
    Ok(installed.listing)
}

/// Asks the marketplace for this user's key and decrypts the installed pack
/// in memory. An outdated install is updated first.
pub fn unlock(id: &str) -> ServiceResult<Pack> {
    for attempt in 0..2 {
        let path = installed_path(id).ok_or("invalid pack id")?;
        let installed: Installed = fs::read(&path)
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .ok_or("This pack isn't installed. Install it from the Marketplace")?;
        let (status, body) = call(
            "/api/marketplace/key",
            Some(json!({ "id": id, "version": installed.listing.version })),
            true,
        )?;
        if status == StatusCode::CONFLICT && attempt == 0 {
            install(id)?;
            continue;
        }
        if !status.is_success() {
            return Err(api_error(status, &body));
        }
        let key = body["key"]
            .as_str()
            .and_then(|key| BASE64.decode(key).ok())
            .ok_or("the marketplace sent no key")?;
        return decrypt(&installed, &key);
    }
    Err("could not update the pack".into())
}

fn aad(listing: &Listing) -> String {
    format!("overlayr-pack:{}:{}", listing.id, listing.version)
}

fn decrypt(installed: &Installed, key: &[u8]) -> ServiceResult<Pack> {
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| "the pack key is invalid")?;
    let nonce = BASE64
        .decode(&installed.nonce)
        .ok()
        .filter(|nonce| nonce.len() == 12)
        .ok_or("the installed pack is damaged")?;
    let sealed = BASE64
        .decode(&installed.ciphertext)
        .map_err(|_| "the installed pack is damaged")?;
    let aad = aad(&installed.listing);
    let mut plain = cipher
        .decrypt(
            Nonce::from_slice(&nonce),
            Payload {
                msg: &sealed,
                aad: aad.as_bytes(),
            },
        )
        .map_err(|_| "the pack could not be unlocked. Reinstall it from the Marketplace")?;
    let pack = serde_json::from_slice::<Pack>(&plain);
    // Don't leave the plain pack lying around in freed memory.
    plain.fill(0);
    pack.map_err(|error| format!("the pack is damaged: {error}"))
}

fn url_encode(text: &str) -> String {
    text.bytes()
        .map(|byte| match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (byte as char).to_string()
            }
            _ => format!("%{byte:02X}"),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seal(key: &[u8], listing: &Listing, plain: &[u8]) -> Installed {
        let cipher = Aes256Gcm::new_from_slice(key).expect("key");
        let nonce = [7_u8; 12];
        let sealed = cipher
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: plain,
                    aad: aad(listing).as_bytes(),
                },
            )
            .expect("encrypt");
        Installed {
            listing: listing.clone(),
            nonce: BASE64.encode(nonce),
            ciphertext: BASE64.encode(sealed),
        }
    }

    fn listing() -> Listing {
        Listing {
            id: "pack1".into(),
            version: 3,
            ..Listing::default()
        }
    }

    #[test]
    fn prices_show_in_baht() {
        let priced = |price| {
            Listing {
                price,
                ..Listing::default()
            }
            .price_label()
        };
        assert_eq!(priced(0), "Free");
        assert_eq!(priced(4900), "฿49");
        assert_eq!(priced(4950), "฿49.50");
        assert_eq!(priced(1005), "฿10.05");
    }

    #[test]
    fn installed_packs_decrypt_only_with_their_key() {
        let pack = Pack {
            name: "Stoneblock".into(),
            ..Pack::default()
        };
        let key = [1_u8; 32];
        let installed = seal(&key, &listing(), &serde_json::to_vec(&pack).expect("json"));
        assert!(!installed.ciphertext.contains("Stoneblock"));
        assert_eq!(decrypt(&installed, &key).expect("unlock"), pack);
        assert!(decrypt(&installed, &[2_u8; 32]).is_err());
    }

    #[test]
    fn a_pack_cannot_be_passed_off_as_another_version() {
        let key = [1_u8; 32];
        let mut installed = seal(&key, &listing(), b"{}");
        installed.listing.version = 4;
        assert!(decrypt(&installed, &key).is_err());
    }

    #[test]
    fn pack_ids_cannot_leave_the_install_folder() {
        assert!(installed_path("../packs/x").is_none());
        assert!(installed_path("").is_none());
        assert!(installed_path("abc_DEF-1").is_some());
    }

    #[test]
    fn search_queries_are_url_encoded() {
        assert_eq!(url_encode("stone block&x"), "stone%20block%26x");
    }
}
