//! Detects and installs Overlayr's external runtime components: the OCR
//! engines (PaddleOCR models, or Tesseract with its language data) and a
//! local LibreTranslate server.
//!
//! Installs that need root go through `pkexec`, so the desktop's polkit agent
//! shows its usual password prompt instead of Overlayr handling credentials.

use std::path::{Path, PathBuf};
use std::process::Command;

use crate::diagnostics::find_command;
use crate::ocr::{TesseractOcr, accurate_model_path};
use crate::services::ServiceResult;
use crate::translation::libretranslate_code;

pub const LIBRETRANSLATE_CONTAINER: &str = "overlayr-libretranslate";
const LIBRETRANSLATE_IMAGE: &str = "docker.io/libretranslate/libretranslate:latest";

/// A command that installs or starts a missing component.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InstallPlan {
    /// One-line description shown next to the install button.
    pub summary: String,
    pub program: PathBuf,
    pub args: Vec<String>,
}

impl InstallPlan {
    /// The command as a shell line, for users who prefer to run it themselves.
    pub fn command_line(&self) -> String {
        std::iter::once(self.program.to_string_lossy().into_owned())
            .chain(self.args.iter().cloned())
            .map(|part| shell_quote(&part))
            .collect::<Vec<_>>()
            .join(" ")
    }

    pub fn needs_password(&self) -> bool {
        self.program.ends_with("pkexec")
    }

    /// Runs the plan to completion. This can take minutes (package downloads,
    /// container image pulls), so callers should run it off the UI thread.
    pub fn run(&self) -> ServiceResult<()> {
        let output = Command::new(&self.program)
            .args(&self.args)
            .output()
            .map_err(|error| format!("could not start {}: {error}", self.program.display()))?;
        if output.status.success() {
            return Ok(());
        }
        // pkexec reserves 126 for a dismissed prompt and 127 for a refusal.
        match (self.needs_password(), output.status.code()) {
            (true, Some(126)) => Err("the password prompt was cancelled".into()),
            (true, Some(127)) => {
                Err("not authorized; make sure a polkit authentication agent is running".into())
            }
            _ => {
                let stderr = String::from_utf8_lossy(&output.stderr);
                let tail = stderr
                    .lines()
                    .filter(|line| !line.trim().is_empty())
                    .rev()
                    .take(3)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect::<Vec<_>>()
                    .join("\n");
                Err(if tail.is_empty() {
                    format!("install command failed ({})", output.status)
                } else {
                    tail
                })
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PackageManager {
    Pacman,
    Apt,
    Dnf,
}

impl PackageManager {
    fn detect() -> Option<(Self, PathBuf)> {
        [
            (Self::Pacman, "pacman"),
            (Self::Apt, "apt-get"),
            (Self::Dnf, "dnf"),
        ]
        .into_iter()
        .find_map(|(manager, command)| find_command(command).map(|path| (manager, path)))
    }

    fn install_args(self) -> &'static [&'static str] {
        match self {
            Self::Pacman => &["-S", "--needed", "--noconfirm"],
            Self::Apt => &["install", "-y"],
            Self::Dnf => &["install", "-y"],
        }
    }

    /// Distribution package names for Tesseract plus English and the given
    /// Tesseract language data (for example `jpn` or `chi_sim`).
    fn tesseract_packages(self, language: &str) -> Vec<String> {
        let mut languages = vec!["eng"];
        if language != "eng" {
            languages.push(language);
        }
        let (engine, prefix) = match self {
            Self::Pacman => ("tesseract", "tesseract-data-"),
            Self::Apt => ("tesseract-ocr", "tesseract-ocr-"),
            Self::Dnf => ("tesseract", "tesseract-langpack-"),
        };
        std::iter::once(engine.to_string())
            .chain(languages.into_iter().map(|language| {
                // Debian packages use dashes where Tesseract uses underscores.
                let language = if self == Self::Apt {
                    language.replace('_', "-")
                } else {
                    language.to_string()
                };
                format!("{prefix}{language}")
            }))
            .collect()
    }
}

/// Plan for installing Tesseract and the OCR data for `language`.
pub fn ocr_plan(language: &str) -> ServiceResult<InstallPlan> {
    let code = TesseractOcr::language(language);
    if !is_safe_token(code) {
        return Err(format!("unsupported OCR language '{language}'"));
    }
    let (manager, manager_path) = PackageManager::detect().ok_or_else(|| {
        format!(
            "No supported package manager (pacman, apt or dnf) was found. \
             Install Tesseract and its '{code}' language data with your system's package manager."
        )
    })?;
    let packages = manager.tesseract_packages(code);
    let mut args = manager
        .install_args()
        .iter()
        .map(|arg| (*arg).to_string())
        .collect::<Vec<_>>();
    args.extend(packages.iter().cloned());
    let summary = format!("Installs {}", packages.join(", "));
    elevated(manager_path, args, summary)
}

const ACCURATE_MODEL_URL: &str = "https://github.com/tesseract-ocr/tessdata_best/raw/main";

/// Downloads Tesseract's high-accuracy ("best") model for `language` into
/// Overlayr's per-user model directory. It reads pixel and UI fonts
/// noticeably better than distribution packages, at a similar speed.
pub fn download_accurate_model(language: &str) -> ServiceResult<()> {
    let code = TesseractOcr::language(language);
    if !is_safe_token(code) {
        return Err(format!("unsupported OCR language '{language}'"));
    }
    let path = accurate_model_path(language)
        .ok_or_else(|| "could not determine your user data directory".to_string())?;
    if let Some(directory) = path.parent() {
        std::fs::create_dir_all(directory)
            .map_err(|error| format!("could not create {}: {error}", directory.display()))?;
    }
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .map_err(|error| format!("could not create download client: {error}"))?;
    let bytes = client
        .get(format!("{ACCURATE_MODEL_URL}/{code}.traineddata"))
        .send()
        .and_then(reqwest::blocking::Response::error_for_status)
        .and_then(reqwest::blocking::Response::bytes)
        .map_err(|error| format!("could not download the '{code}' model: {error}"))?;
    if bytes.len() < 64 * 1024 {
        return Err(format!("the downloaded '{code}' model is incomplete"));
    }
    // Write to a temporary name first so an interrupted download never
    // leaves a truncated model that OCR would try to load.
    let partial = path.with_extension("part");
    std::fs::write(&partial, &bytes)
        .and_then(|()| std::fs::rename(&partial, &path))
        .map_err(|error| format!("could not save the model to {}: {error}", path.display()))
}

const NEURAL_MODEL_URL: &str =
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv6";
const ONNX_RUNTIME_VERSION: &str = "1.31.0";

/// Downloads `url` into `path`, through a temporary name so an interrupted
/// download never leaves a truncated file behind.
fn download(
    client: &reqwest::blocking::Client,
    url: &str,
    path: &Path,
    minimum: usize,
) -> ServiceResult<()> {
    let name = path
        .file_name()
        .map_or_else(String::new, |name| name.to_string_lossy().into_owned());
    let bytes = client
        .get(url)
        .send()
        .and_then(reqwest::blocking::Response::error_for_status)
        .and_then(reqwest::blocking::Response::bytes)
        .map_err(|error| format!("could not download {name}: {error}"))?;
    if bytes.len() < minimum {
        return Err(format!("the downloaded {name} is incomplete"));
    }
    if let Some(directory) = path.parent() {
        std::fs::create_dir_all(directory)
            .map_err(|error| format!("could not create {}: {error}", directory.display()))?;
    }
    let partial = path.with_extension("part");
    std::fs::write(&partial, &bytes)
        .and_then(|()| std::fs::rename(&partial, path))
        .map_err(|error| format!("could not save {}: {error}", path.display()))
}

/// Downloads the PaddleOCR models and, unless the system already has one,
/// the ONNX Runtime library into Overlayr's per-user data directory. About
/// 30 MB of models plus 11 MB for the runtime; no password needed.
pub fn download_neural_ocr() -> ServiceResult<()> {
    let models = crate::neural::model_dir()
        .ok_or_else(|| "could not determine your user data directory".to_string())?;
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(1800))
        .build()
        .map_err(|error| format!("could not create download client: {error}"))?;
    for (kind, file) in [
        ("det", crate::neural::DETECTION_MODEL),
        ("rec", crate::neural::RECOGNITION_MODEL),
    ] {
        let path = models.join(file);
        if !path.is_file() {
            download(
                &client,
                &format!("{NEURAL_MODEL_URL}/{kind}/{file}"),
                &path,
                1024 * 1024,
            )?;
        }
    }
    if crate::neural::runtime_library().is_none() {
        install_onnx_runtime(&client)?;
    }
    Ok(())
}

/// Fetches Microsoft's prebuilt ONNX Runtime and keeps only its library.
fn install_onnx_runtime(client: &reqwest::blocking::Client) -> ServiceResult<()> {
    let directory = crate::neural::runtime_dir()
        .ok_or_else(|| "could not determine your user data directory".to_string())?;
    let release = format!("onnxruntime-linux-x64-{ONNX_RUNTIME_VERSION}");
    let archive = directory.join(format!("{release}.tgz"));
    download(
        client,
        &format!(
            "https://github.com/microsoft/onnxruntime/releases/download/v{ONNX_RUNTIME_VERSION}/{release}.tgz"
        ),
        &archive,
        1024 * 1024,
    )?;
    let member = format!("{release}/lib/libonnxruntime.so.{ONNX_RUNTIME_VERSION}");
    let output = Command::new("tar")
        .arg("-xzf")
        .arg(&archive)
        .arg("-C")
        .arg(&directory)
        .arg("--strip-components=2")
        .arg(&member)
        .output()
        .map_err(|error| format!("could not unpack ONNX Runtime: {error}"))?;
    let _ = std::fs::remove_file(&archive);
    if !output.status.success() {
        return Err(format!(
            "could not unpack ONNX Runtime: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    std::fs::rename(
        directory.join(format!("libonnxruntime.so.{ONNX_RUNTIME_VERSION}")),
        directory.join(crate::neural::RUNTIME_LIBRARY),
    )
    .map_err(|error| format!("could not install ONNX Runtime: {error}"))
}

/// Plan for running LibreTranslate locally in a container, serving the
/// configured endpoint's port with only the needed language models.
pub fn translation_plan(endpoint: &str, source: &str, target: &str) -> ServiceResult<InstallPlan> {
    let port = local_port(endpoint).ok_or_else(|| {
        "Automatic setup only works for a local endpoint such as \
         http://localhost:5000/translate."
            .to_string()
    })?;
    let mut languages = vec!["en".to_string()];
    for language in [source, target] {
        let code = libretranslate_code(language);
        if !is_safe_token(&code) {
            return Err(format!("unsupported translation language '{language}'"));
        }
        if !languages.contains(&code) {
            languages.push(code);
        }
    }
    let (runtime, rootless) = if let Some(docker) = find_command("docker") {
        let rootless = Command::new(&docker)
            .arg("info")
            .output()
            .is_ok_and(|output| output.status.success());
        (docker, rootless)
    } else if let Some(podman) = find_command("podman") {
        (podman, true)
    } else {
        return Err(
            "Docker or Podman is needed to run a local LibreTranslate server. \
             Install one of them, or point the endpoint at an existing server."
                .into(),
        );
    };
    let runtime_path = runtime.to_string_lossy();
    // Recreate the container so it always serves the current language pair;
    // the named volume keeps previously downloaded models.
    let script = format!(
        "{runtime} rm -f {name} >/dev/null 2>&1; \
         {runtime} run -d --name {name} --restart unless-stopped \
         -p 127.0.0.1:{port}:5000 -v {name}:/home/libretranslate/.local \
         {image} --load-only {languages}",
        runtime = shell_quote(&runtime_path),
        name = LIBRETRANSLATE_CONTAINER,
        image = LIBRETRANSLATE_IMAGE,
        languages = languages.join(","),
    );
    let summary = format!(
        "Runs LibreTranslate in a container on port {port} with {} models",
        languages.join(", ")
    );
    let shell = find_command("sh").unwrap_or_else(|| PathBuf::from("/bin/sh"));
    let args = vec!["-c".to_string(), script];
    if rootless {
        Ok(InstallPlan {
            summary,
            program: shell,
            args,
        })
    } else {
        elevated(shell, args, summary)
    }
}

fn elevated(program: PathBuf, args: Vec<String>, summary: String) -> ServiceResult<InstallPlan> {
    let pkexec = find_command("pkexec").ok_or_else(|| {
        "pkexec is not available, so Overlayr cannot ask for administrator access. \
         Run the command in a terminal instead."
            .to_string()
    })?;
    Ok(InstallPlan {
        summary,
        program: pkexec,
        args: std::iter::once(program.to_string_lossy().into_owned())
            .chain(args)
            .collect(),
    })
}

/// The port of a plain-HTTP loopback endpoint, which is the only kind of
/// endpoint a locally started server can satisfy.
fn local_port(endpoint: &str) -> Option<u16> {
    let rest = endpoint.strip_prefix("http://")?;
    let authority = rest.split('/').next()?;
    let (host, port) = match authority.strip_prefix('[') {
        Some(bracketed) => {
            let (host, after) = bracketed.split_once(']')?;
            (host, after.strip_prefix(':'))
        }
        None => match authority.split_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (authority, None),
        },
    };
    let port = port.map_or(Some(80), |port| port.parse().ok())?;
    matches!(host, "localhost" | "127.0.0.1" | "::1").then_some(port)
}

fn is_safe_token(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
}

fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "-_./:=,".contains(character))
    {
        value.to_string()
    } else {
        format!("'{}'", value.replace('\'', r"'\''"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_tesseract_packages_per_distribution() {
        assert_eq!(
            PackageManager::Pacman.tesseract_packages("jpn"),
            ["tesseract", "tesseract-data-eng", "tesseract-data-jpn"]
        );
        assert_eq!(
            PackageManager::Apt.tesseract_packages("chi_sim"),
            [
                "tesseract-ocr",
                "tesseract-ocr-eng",
                "tesseract-ocr-chi-sim"
            ]
        );
        assert_eq!(
            PackageManager::Dnf.tesseract_packages("eng"),
            ["tesseract", "tesseract-langpack-eng"]
        );
    }

    #[test]
    fn only_loopback_http_endpoints_are_installable() {
        assert_eq!(local_port("http://localhost:5000/translate"), Some(5000));
        assert_eq!(local_port("http://127.0.0.1/translate"), Some(80));
        assert_eq!(local_port("http://[::1]:5050/translate"), Some(5050));
        assert_eq!(local_port("https://localhost:5000/translate"), None);
        assert_eq!(local_port("http://example.com:5000/translate"), None);
    }

    #[test]
    fn command_line_quotes_shell_scripts() {
        let plan = InstallPlan {
            summary: String::new(),
            program: PathBuf::from("/usr/bin/pkexec"),
            args: vec!["/bin/sh".into(), "-c".into(), "echo 'hi'; true".into()],
        };
        assert_eq!(
            plan.command_line(),
            r"/usr/bin/pkexec /bin/sh -c 'echo '\''hi'\''; true'"
        );
        assert!(plan.needs_password());
    }

    #[test]
    fn rejects_unsafe_language_tokens() {
        assert!(is_safe_token("chi_sim"));
        assert!(!is_safe_token("ja;rm"));
        assert!(!is_safe_token(""));
    }
}
