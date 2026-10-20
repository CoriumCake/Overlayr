use std::fs;
use std::io;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// The desktop login callback is intentionally kept small: the web app owns
/// authentication, while the desktop app only needs to persist the session
/// returned through the `overlayr://` protocol.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct AuthSession {
    pub uid: String,
    pub access_token: String,
    pub timestamp: u64,
    /// Renews `access_token`, which expires after an hour. Older
    /// logins don't have one and must log in again once the token expires.
    #[serde(default)]
    pub refresh_token: String,
}

/// ID tokens live an hour; renew a little before that.
const TOKEN_LIFETIME_SECS: u64 = 50 * 60;

pub fn handle_launch_args<I, S>(args: I) -> io::Result<bool>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut handled = false;
    for argument in args {
        if let Some(session) = parse_deep_link(argument.as_ref()) {
            save_session(&session)?;
            handled = true;
        }
    }
    Ok(handled)
}

pub fn parse_deep_link(url: &str) -> Option<AuthSession> {
    let query = url.strip_prefix("overlayr://auth?")?;
    let mut uid = None;
    let mut access_token = None;
    let mut refresh_token = String::new();
    for pair in query.split('&') {
        let (key, value) = pair.split_once('=')?;
        let value = percent_decode(value)?;
        match key {
            "uid" => uid = Some(value),
            "token" => access_token = Some(value),
            "refresh" => refresh_token = value,
            _ => {}
        }
    }

    let uid = uid?.trim().to_owned();
    let access_token = access_token?.trim().to_owned();
    if uid.is_empty() || access_token.is_empty() {
        return None;
    }

    Some(AuthSession {
        uid,
        access_token,
        timestamp: now()?,
        refresh_token: refresh_token.trim().to_owned(),
    })
}

fn now() -> Option<u64> {
    Some(SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs())
}

/// The saved login, if the user has logged in on this computer.
pub fn load_session() -> Option<AuthSession> {
    serde_json::from_slice(&fs::read(session_path()).ok()?).ok()
}

/// Forgets the saved login, and asks `api_base` in the background to revoke
/// its refresh token so a copied session file stops working too.
pub fn logout(api_base: &str) -> io::Result<()> {
    if let Some(session) = load_session().filter(|session| !session.refresh_token.is_empty()) {
        let url = format!("{api_base}/api/auth/logout");
        std::thread::spawn(move || {
            let _ = reqwest::blocking::Client::new()
                .post(url)
                .timeout(std::time::Duration::from_secs(15))
                .json(&serde_json::json!({ "refreshToken": session.refresh_token }))
                .send();
        });
    }
    match fs::remove_file(session_path()) {
        Err(error) if error.kind() != io::ErrorKind::NotFound => Err(error),
        _ => Ok(()),
    }
}

/// A valid ID token for the Overlayr API, renewed through `api_base` when
/// the saved one has expired.
pub fn id_token(api_base: &str) -> Result<String, String> {
    let mut session = load_session().ok_or("Log in to use the marketplace")?;
    let age = now().unwrap_or(0).saturating_sub(session.timestamp);
    if age < TOKEN_LIFETIME_SECS {
        return Ok(session.access_token);
    }
    if session.refresh_token.is_empty() {
        return Err("Your login expired. Log in again".into());
    }
    #[derive(Deserialize)]
    struct Refreshed {
        token: String,
        #[serde(rename = "refreshToken")]
        refresh_token: String,
    }
    let response = reqwest::blocking::Client::new()
        .post(format!("{api_base}/api/auth/refresh"))
        .timeout(std::time::Duration::from_secs(15))
        .json(&serde_json::json!({ "refreshToken": session.refresh_token }))
        .send()
        .map_err(|error| format!("could not reach Overlayr: {error}"))?;
    if !response.status().is_success() {
        return Err("Your login expired. Log in again".into());
    }
    let refreshed: Refreshed = response
        .json()
        .map_err(|error| format!("unexpected reply from Overlayr: {error}"))?;
    session.access_token = refreshed.token;
    session.refresh_token = refreshed.refresh_token;
    session.timestamp = now().unwrap_or(0);
    save_session(&session).map_err(|error| format!("could not save the login: {error}"))?;
    Ok(session.access_token)
}

pub fn session_path() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".overlayr")
        .join("auth_session.json")
}

fn save_session(session: &AuthSession) -> io::Result<()> {
    let path = session_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let temp_path = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(session)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    fs::write(&temp_path, bytes)?;
    #[cfg(windows)]
    if path.exists() {
        fs::remove_file(&path)?;
    }
    fs::rename(temp_path, path)
}

fn percent_decode(value: &str) -> Option<String> {
    let mut decoded = Vec::with_capacity(value.len());
    let bytes = value.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => decoded.push(b' '),
            b'%' if index + 2 < bytes.len() => {
                let high = hex_value(bytes[index + 1])?;
                let low = hex_value(bytes[index + 2])?;
                decoded.push(high * 16 + low);
                index += 2;
            }
            byte => decoded.push(byte),
        }
        index += 1;
    }
    String::from_utf8(decoded).ok()
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

#[cfg(windows)]
pub fn register_protocol() -> io::Result<()> {
    let executable = std::env::current_exe()?;
    let command = format!(r#""{}" "%1""#, executable.display());
    let key = r"HKCU\Software\Classes\overlayr";
    let command_key = r"HKCU\Software\Classes\overlayr\shell\open\command";

    let status = std::process::Command::new("reg")
        .args(["add", key, "/ve", "/d", "URL:Overlayr Protocol", "/f"])
        .status()?;
    if !status.success() {
        return Err(io::Error::other("could not register overlayr protocol"));
    }
    let status = std::process::Command::new("reg")
        .args([
            "add",
            key,
            "/v",
            "URL Protocol",
            "/t",
            "REG_SZ",
            "/d",
            "",
            "/f",
        ])
        .status()?;
    if !status.success() {
        return Err(io::Error::other("could not register overlayr URL protocol"));
    }
    let status = std::process::Command::new("reg")
        .args(["add", command_key, "/ve", "/d"])
        .arg(&command)
        .arg("/f")
        .status()?;
    if !status.success() {
        return Err(io::Error::other(
            "could not register overlayr protocol command",
        ));
    }
    Ok(())
}

#[cfg(not(windows))]
pub fn register_protocol() -> io::Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_encoded_auth_callback() {
        let session = parse_deep_link("overlayr://auth?token=abc%2B123&uid=user%40example.com")
            .expect("deep link should parse");
        assert_eq!(session.uid, "user@example.com");
        assert_eq!(session.access_token, "abc+123");
        assert!(session.refresh_token.is_empty());
    }

    #[test]
    fn keeps_the_refresh_token() {
        let session = parse_deep_link("overlayr://auth?token=t&uid=u&refresh=r%2Fs")
            .expect("deep link should parse");
        assert_eq!(session.refresh_token, "r/s");
    }

    #[test]
    fn rejects_non_auth_urls() {
        assert!(parse_deep_link("https://example.com/callback").is_none());
        assert!(parse_deep_link("overlayr://auth?uid=user").is_none());
    }
}
