//! Updates Overlayr from overlayr.app, so players never download installers
//! by hand.
//!
//! Each release publishes `latest.json` (see `.github/workflows/release.yml`)
//! with the installer for each platform and its SHA-256. In the background,
//! Overlayr checks it, downloads a newer installer and verifies it; once it
//! is ready the app offers to restart into the new version:
//!
//! - Windows: runs the installer silently, which closes Overlayr, replaces
//!   it and starts it again.
//! - macOS: copies Overlayr.app out of the disk image and swaps it in once
//!   Overlayr has quit.
//! - Linux: replaces the AppImage with the new one, or, for an install from
//!   the tarball, the program with the one from the archive.
//!
//! Only release builds update themselves (CI sets `OVERLAYR_RELEASE`); a
//! build from source would otherwise be replaced by the release.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Where releases come from; `OVERLAYR_UPDATE_SERVER` points elsewhere
/// for testing.
fn server() -> String {
    std::env::var("OVERLAYR_UPDATE_SERVER").unwrap_or_else(|_| "https://overlayr.app".into())
}
/// How often a running Overlayr looks for a new version.
const CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

/// Whether this build updates itself.
pub fn enabled() -> bool {
    option_env!("OVERLAYR_RELEASE").is_some()
        && std::env::var_os("OVERLAYR_NO_UPDATE").is_none()
        && !package_managed()
}

/// Installed by a package manager (the AUR's overlayr-bin puts it in
/// /usr/bin), which updates it instead; the program isn't ours to replace.
fn package_managed() -> bool {
    cfg!(target_os = "linux")
        && std::env::current_exe().is_ok_and(|executable| executable.starts_with("/usr"))
}

/// This computer's name in `latest.json`.
fn platform() -> Option<&'static str> {
    if cfg!(windows) {
        Some("windows")
    } else if cfg!(target_os = "macos") {
        Some("macos")
    } else if cfg!(target_os = "linux") && std::env::consts::ARCH == "x86_64" {
        if crate::bundle::appimage().is_some() {
            Some("appimage")
        } else {
            Some("linux")
        }
    } else {
        None
    }
}

/// A version's numbers, so 0.10.0 sorts after 0.9.0. Anything after a `-`
/// (a test build) is ignored.
fn version_numbers(version: &str) -> Vec<u64> {
    version
        .split('-')
        .next()
        .unwrap_or_default()
        .split('.')
        .map(|part| part.parse().unwrap_or(0))
        .collect()
}

fn is_newer(candidate: &str, current: &str) -> bool {
    version_numbers(candidate) > version_numbers(current)
}

/// The version an installer holds, from its name: Overlayr-0.1.1-windows-….
/// A release can reuse an older installer for a platform that didn't build,
/// so the release's own version isn't enough.
fn file_version(name: &str) -> Option<&str> {
    name.strip_prefix("Overlayr-")?.split('-').next()
}

#[derive(Clone, Debug, PartialEq)]
pub struct Update {
    pub version: String,
    pub notes: Vec<String>,
    /// The downloaded, verified installer.
    pub path: PathBuf,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub enum State {
    #[default]
    Idle,
    Checking,
    Downloading {
        version: String,
        received: u64,
        size: u64,
    },
    Ready(Update),
    Installing,
    Failed(String),
}

/// Checks for updates in the background.
#[derive(Default)]
pub struct Updater {
    state: Arc<Mutex<State>>,
    last_check: Option<Instant>,
    /// The player chose "Later" for this version.
    dismissed: Option<String>,
}

impl Updater {
    pub fn state(&self) -> State {
        self.state
            .lock()
            .map(|state| state.clone())
            .unwrap_or_default()
    }

    /// The update to offer, unless the player put it off.
    pub fn offer(&self) -> Option<Update> {
        match self.state() {
            State::Ready(update) if self.dismissed.as_ref() != Some(&update.version) => {
                Some(update)
            }
            _ => None,
        }
    }

    pub fn dismiss(&mut self) {
        if let State::Ready(update) = self.state() {
            self.dismissed = Some(update.version);
        }
    }

    /// Starts a check when one is due. Call it every frame.
    pub fn tick(&mut self) {
        if !enabled() || platform().is_none() {
            return;
        }
        let due = self
            .last_check
            .is_none_or(|checked| checked.elapsed() >= CHECK_INTERVAL);
        let busy = matches!(
            self.state(),
            State::Checking | State::Downloading { .. } | State::Installing
        );
        if !due || busy {
            return;
        }
        self.last_check = Some(Instant::now());
        let state = Arc::clone(&self.state);
        set(&state, State::Checking);
        std::thread::spawn(move || {
            let result = check_and_download(&state);
            let next = match result {
                Ok(Some(update)) => State::Ready(update),
                Ok(None) => State::Idle,
                Err(error) => {
                    eprintln!("Update check failed: {error}");
                    State::Failed(error)
                }
            };
            set(&state, next);
        });
    }

    /// Installs the downloaded update and quits, so the new version starts.
    pub fn install(&mut self) -> Result<(), String> {
        let State::Ready(update) = self.state() else {
            return Err("no update is ready".into());
        };
        set(&self.state, State::Installing);
        match install(&update.path) {
            Ok(()) => std::process::exit(0),
            Err(error) => {
                set(&self.state, State::Failed(error.clone()));
                Err(error)
            }
        }
    }
}

fn set(state: &Mutex<State>, value: State) {
    if let Ok(mut state) = state.lock() {
        *state = value;
    }
}

#[derive(serde::Deserialize)]
struct LatestResponse {
    release: Option<Release>,
}

#[derive(serde::Deserialize)]
struct Release {
    version: String,
    #[serde(default)]
    notes: Vec<String>,
    files: std::collections::HashMap<String, ReleaseFile>,
}

#[derive(serde::Deserialize)]
struct ReleaseFile {
    name: String,
    sha256: String,
}

fn client(seconds: u64) -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(seconds))
        .user_agent(concat!("Overlayr/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|error| format!("could not create HTTP client: {error}"))
}

/// The newer installer for this computer, downloaded and verified, or None
/// when Overlayr is up to date.
fn check_and_download(state: &Mutex<State>) -> Result<Option<Update>, String> {
    let platform = platform().ok_or("updates aren't available for this computer")?;
    let latest: LatestResponse = client(30)?
        .get(format!("{}/api/releases/latest", server()))
        .send()
        .and_then(reqwest::blocking::Response::error_for_status)
        .and_then(reqwest::blocking::Response::json)
        .map_err(|error| format!("could not check for updates: {error}"))?;
    let Some(release) = latest.release else {
        return Ok(None);
    };
    let Some(file) = release.files.get(platform) else {
        return Ok(None);
    };
    let version = file_version(&file.name)
        .unwrap_or(&release.version)
        .to_string();
    let directory = crate::ocr::data_dir()
        .ok_or("no folder for downloads")?
        .join("updates");
    if !is_newer(&version, env!("CARGO_PKG_VERSION")) {
        // The installer of the update that's now running.
        let _ = std::fs::remove_dir_all(&directory);
        return Ok(None);
    }
    std::fs::create_dir_all(&directory)
        .map_err(|error| format!("could not create {}: {error}", directory.display()))?;
    let path = directory.join(&file.name);
    let notes = if version == release.version {
        release.notes
    } else {
        Vec::new()
    };
    let update = Update {
        version: version.clone(),
        notes,
        path: path.clone(),
    };
    // Downloaded on an earlier run.
    if sha256_file(&path).is_ok_and(|hash| hash.eq_ignore_ascii_case(&file.sha256)) {
        return Ok(Some(update));
    }
    remove_old_downloads(&directory, &file.name);
    let hash = download(
        state,
        &format!("{}/download/{platform}", server()),
        &path,
        &version,
    )?;
    if !hash.eq_ignore_ascii_case(&file.sha256) {
        let _ = std::fs::remove_file(&path);
        return Err(format!("the downloaded {} is damaged", file.name));
    }
    Ok(Some(update))
}

/// Installers from earlier updates.
fn remove_old_downloads(directory: &Path, keep: &str) {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        if entry.file_name() != keep {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

/// Downloads `url` into `path` and returns its SHA-256.
fn download(state: &Mutex<State>, url: &str, path: &Path, version: &str) -> Result<String, String> {
    use sha2::Digest;
    let failed = |error: &dyn std::fmt::Display| format!("could not download the update: {error}");
    let mut response = client(30 * 60)?
        .get(url)
        .send()
        .and_then(reqwest::blocking::Response::error_for_status)
        .map_err(|error| failed(&error))?;
    let size = response.content_length().unwrap_or(0);
    let partial = path.with_extension("part");
    let mut output = std::fs::File::create(&partial).map_err(|error| failed(&error))?;
    let mut hasher = sha2::Sha256::new();
    let mut received = 0;
    let mut chunk = vec![0; 256 * 1024];
    loop {
        let read = match response.read(&mut chunk) {
            Ok(0) => break,
            Ok(read) => read,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => {
                let _ = std::fs::remove_file(&partial);
                return Err(failed(&error));
            }
        };
        hasher.update(&chunk[..read]);
        output
            .write_all(&chunk[..read])
            .map_err(|error| failed(&error))?;
        received += read as u64;
        set(
            state,
            State::Downloading {
                version: version.to_string(),
                received,
                size,
            },
        );
    }
    drop(output);
    std::fs::rename(&partial, path).map_err(|error| failed(&error))?;
    Ok(hex(&hasher.finalize()))
}

fn sha256_file(path: &Path) -> std::io::Result<String> {
    use sha2::Digest;
    let mut file = std::fs::File::open(path)?;
    let mut hasher = sha2::Sha256::new();
    std::io::copy(&mut file, &mut hasher)?;
    Ok(hex(&hasher.finalize()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Starts installing `installer`; the caller quits right after.
#[cfg(windows)]
fn install(installer: &Path) -> Result<(), String> {
    // Inno Setup: no windows, close Overlayr if it's still open, and start
    // it again afterwards (packaging/windows/overlayr.iss).
    std::process::Command::new(installer)
        .args([
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
            "/RELAUNCH=1",
        ])
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("could not start the installer: {error}"))
}

/// The Overlayr.app this program runs from.
#[cfg(target_os = "macos")]
fn app_bundle() -> Result<PathBuf, String> {
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    executable
        .ancestors()
        .find(|path| path.extension().is_some_and(|extension| extension == "app"))
        .map(Path::to_path_buf)
        .ok_or_else(|| "Overlayr isn't running from Overlayr.app".to_string())
}

#[cfg(target_os = "macos")]
fn install(disk_image: &Path) -> Result<(), String> {
    use std::process::Command;
    let bundle = app_bundle()?;
    let staged = bundle.with_extension("app.new");
    let mount = std::env::temp_dir().join(format!("overlayr-update-{}", std::process::id()));
    let run = |command: &mut Command, what: &str| -> Result<(), String> {
        let status = command
            .status()
            .map_err(|error| format!("could not {what}: {error}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!("could not {what}"))
        }
    };
    run(
        Command::new("hdiutil")
            .args([
                "attach",
                "-nobrowse",
                "-readonly",
                "-noautoopen",
                "-mountpoint",
            ])
            .arg(&mount)
            .arg(disk_image),
        "open the update",
    )?;
    let _ = std::fs::remove_dir_all(&staged);
    let copied = run(
        Command::new("ditto")
            .arg(mount.join("Overlayr.app"))
            .arg(&staged),
        "copy the update (is Overlayr in a folder you can change?)",
    );
    let _ = Command::new("hdiutil")
        .args(["detach", "-quiet"])
        .arg(&mount)
        .status();
    copied?;
    swap_after_exit(
        r#"rm -rf "$1" && mv "$2" "$1" && open "$1""#,
        &[&bundle, &staged],
    )
}

#[cfg(target_os = "linux")]
fn install(archive: &Path) -> Result<(), String> {
    if let Some(appimage) = crate::bundle::appimage() {
        return replace_program(archive, &appimage);
    }
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let unpacked = archive.with_extension("unpacked");
    let _ = std::fs::remove_dir_all(&unpacked);
    std::fs::create_dir_all(&unpacked).map_err(|error| error.to_string())?;
    let status = std::process::Command::new("tar")
        .arg("-xzf")
        .arg(archive)
        .arg("-C")
        .arg(&unpacked)
        .status()
        .map_err(|error| format!("could not unpack the update: {error}"))?;
    if !status.success() {
        return Err("could not unpack the update".into());
    }
    let program = std::fs::read_dir(&unpacked)
        .map_err(|error| error.to_string())?
        .flatten()
        .map(|entry| entry.path().join("overlayr"))
        .find(|path| path.is_file())
        .ok_or("the update has no overlayr program")?;
    let replaced = replace_program(&program, &executable);
    let _ = std::fs::remove_dir_all(&unpacked);
    replaced
}

/// Puts `new` in place of `program` and starts it once Overlayr exits.
#[cfg(target_os = "linux")]
fn replace_program(new: &Path, program: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    // Next to the running program, so the rename that replaces it stays on
    // one file system; Linux keeps the running copy until it exits.
    let staged = program.with_extension("new");
    std::fs::copy(new, &staged)
        .and_then(|_| std::fs::set_permissions(&staged, std::fs::Permissions::from_mode(0o755)))
        .and_then(|()| std::fs::rename(&staged, program))
        .map_err(|error| {
            let _ = std::fs::remove_file(&staged);
            format!("could not replace {}: {error}", program.display())
        })?;
    swap_after_exit(r#"exec "$1""#, &[program])
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
fn install(_installer: &Path) -> Result<(), String> {
    Err("updates aren't available for this computer".into())
}

/// Runs `script` once this process has exited, with `paths` as $1, $2, ….
/// Overlayr lets only one copy run, so the new one must wait for this one.
#[cfg(unix)]
fn swap_after_exit(script: &str, paths: &[&Path]) -> Result<(), String> {
    use std::os::unix::process::CommandExt;
    let waiting = format!(
        "while kill -0 {} 2>/dev/null; do sleep 0.2; done; {script}",
        std::process::id()
    );
    std::process::Command::new("/bin/sh")
        .arg("-c")
        .arg(waiting)
        .arg("overlayr-update")
        .args(paths)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .process_group(0)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("could not restart Overlayr: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compares_versions_by_number() {
        assert!(is_newer("0.1.1", "0.1.0"));
        assert!(is_newer("0.10.0", "0.9.9"));
        assert!(is_newer("1.0.0", "0.99.0"));
        assert!(!is_newer("0.1.0", "0.1.0"));
        assert!(!is_newer("0.1.0", "0.1.1"));
        assert!(!is_newer("0.1.1-test.abc1234", "0.1.1"));
    }

    #[test]
    fn reads_the_version_from_installer_names() {
        assert_eq!(
            file_version("Overlayr-0.1.1-windows-x64-setup.exe"),
            Some("0.1.1")
        );
        assert_eq!(file_version("Overlayr-0.1.0-macos.dmg"), Some("0.1.0"));
        assert_eq!(
            file_version("Overlayr-0.2.0-linux-x86_64.tar.gz"),
            Some("0.2.0")
        );
        assert_eq!(
            file_version("Overlayr-0.2.0-linux-x86_64.AppImage"),
            Some("0.2.0")
        );
        assert_eq!(file_version("setup.exe"), None);
    }

    /// Against a test server: OVERLAYR_UPDATE_SERVER=http://127.0.0.1:8765
    /// cargo test --lib updater::tests::downloads_and_verifies -- --ignored
    #[test]
    #[ignore]
    fn downloads_and_verifies() {
        let state = Mutex::new(State::Idle);
        let update = check_and_download(&state).unwrap().expect("an update");
        println!("{update:?}");
        assert!(update.path.is_file());
    }

    /// Installs from the test server into a copy of this test program, which
    /// is replaced by the archive's `overlayr` and started again. Run the
    /// copy, not cargo, so the real build isn't replaced.
    #[test]
    #[ignore]
    #[cfg(target_os = "linux")]
    fn installs_and_restarts() {
        let state = Mutex::new(State::Idle);
        let update = check_and_download(&state).unwrap().expect("an update");
        install(&update.path).unwrap();
    }

    #[test]
    fn hashes_files() {
        let path = std::env::temp_dir().join(format!("overlayr-hash-{}", std::process::id()));
        std::fs::write(&path, b"abc").unwrap();
        assert_eq!(
            sha256_file(&path).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let _ = std::fs::remove_file(path);
    }
}
