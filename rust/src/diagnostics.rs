use std::fmt;
use std::path::{Path, PathBuf};

use crate::capture::PlatformCapture;
use crate::config::AppConfig;
use crate::services::CaptureBackend;
use crate::translation::LibreTranslate;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DiagnosticCheck {
    pub name: String,
    pub passed: bool,
    pub detail: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct DiagnosticReport {
    pub checks: Vec<DiagnosticCheck>,
}

impl DiagnosticReport {
    pub fn is_healthy(&self) -> bool {
        self.checks.iter().all(|check| check.passed)
    }
}

impl fmt::Display for DiagnosticReport {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(formatter, "Overlayr diagnostics")?;
        for check in &self.checks {
            let marker = if check.passed { "OK" } else { "FAIL" };
            writeln!(formatter, "[{marker}] {}: {}", check.name, check.detail)?;
        }
        Ok(())
    }
}

pub fn collect(config_path: &Path, config_result: Result<AppConfig, String>) -> DiagnosticReport {
    let mut report = DiagnosticReport::default();

    let config = match config_result {
        Ok(config) => {
            let detail = match config.validate() {
                Ok(()) => format!("loaded from {}", config_path.display()),
                Err(error) => {
                    report.checks.push(DiagnosticCheck {
                        name: "configuration".into(),
                        passed: false,
                        detail: error,
                    });
                    return report;
                }
            };
            report.checks.push(DiagnosticCheck {
                name: "configuration".into(),
                passed: true,
                detail,
            });
            config
        }
        Err(error) => {
            report.checks.push(DiagnosticCheck {
                name: "configuration".into(),
                passed: false,
                detail: error,
            });
            return report;
        }
    };

    report.checks.push(capture_dependency_check());
    #[cfg(target_os = "macos")]
    report.checks.push(screen_recording_check());

    let mut capture = PlatformCapture::default();
    match capture.sources() {
        Ok(sources) if !sources.is_empty() => report.checks.push(DiagnosticCheck {
            name: "capture sources".into(),
            passed: true,
            detail: format!("{} source(s) available", sources.len()),
        }),
        Ok(_) => report.checks.push(DiagnosticCheck {
            name: "capture sources".into(),
            passed: false,
            detail: "no screen or window sources are available".into(),
        }),
        Err(error) => report.checks.push(DiagnosticCheck {
            name: "capture sources".into(),
            passed: false,
            detail: error,
        }),
    }

    report.checks.push(ocr_check(&config.source_lang));
    report.checks.push(translation_check(
        &config.translation_endpoint,
        &config.source_lang,
        &config.target_lang,
    ));

    report
}

fn translation_check(endpoint: &str, source: &str, target: &str) -> DiagnosticCheck {
    let result = LibreTranslate::new(endpoint, None).and_then(|provider| {
        provider.check_reachable()?;
        Ok(provider)
    });
    match result {
        // Endpoints that don't expose LibreTranslate's language list can still
        // be valid, so the language check only applies when the list loads.
        Ok(provider) => match provider.languages() {
            Ok(languages) => {
                let missing = [source, target]
                    .into_iter()
                    .map(crate::translation::libretranslate_code)
                    .filter(|code| !languages.contains(code))
                    .collect::<Vec<_>>();
                if missing.is_empty() {
                    DiagnosticCheck {
                        name: "translation service".into(),
                        passed: true,
                        detail: format!("{endpoint} supports {source} → {target}"),
                    }
                } else {
                    DiagnosticCheck {
                        name: "translation service".into(),
                        passed: false,
                        detail: format!(
                            "{endpoint} does not have the {} language model loaded (available: {})",
                            missing.join(", "),
                            languages.join(", ")
                        ),
                    }
                }
            }
            Err(_) => DiagnosticCheck {
                name: "translation service".into(),
                passed: true,
                detail: format!("{endpoint} is reachable"),
            },
        },
        Err(error) => DiagnosticCheck {
            name: "translation service".into(),
            passed: false,
            detail: error,
        },
    }
}

fn ocr_check(language: &str) -> DiagnosticCheck {
    let ocr = crate::ocr::engine(language, "paragraph");
    match ocr.validate_language(language) {
        Ok(()) => {
            let engine = match ocr.name() {
                "Tesseract" if crate::ocr::has_accurate_model(language) => {
                    "Tesseract, high-accuracy model"
                }
                "Tesseract" => "Tesseract, standard model",
                name => name,
            };
            DiagnosticCheck {
                name: "OCR".into(),
                passed: true,
                detail: format!("language '{language}' is ready ({engine})"),
            }
        }
        Err(error) => DiagnosticCheck {
            name: "OCR".into(),
            passed: false,
            detail: error,
        },
    }
}

/// Name of the macOS Screen Recording check, which the setup page offers to
/// fix.
pub const SCREEN_RECORDING_CHECK: &str = "screen recording";

#[cfg(target_os = "macos")]
fn screen_recording_check() -> DiagnosticCheck {
    let passed = crate::macos::can_record_screen();
    DiagnosticCheck {
        name: SCREEN_RECORDING_CHECK.into(),
        passed,
        detail: if passed {
            "allowed".into()
        } else {
            "Overlayr needs Screen Recording access to read other apps' text. Allow it, \
             then restart Overlayr"
                .into()
        },
    }
}

fn capture_dependency_check() -> DiagnosticCheck {
    let (backend, executable) = if cfg!(feature = "native-capture") {
        ("native XCap", None)
    } else if std::env::var_os("WAYLAND_DISPLAY").is_some() {
        ("Wayland grim fallback", Some("grim"))
    } else if std::env::var_os("DISPLAY").is_some() {
        ("X11 ImageMagick fallback", Some("import"))
    } else {
        return DiagnosticCheck {
            name: "capture backend".into(),
            passed: false,
            detail: "no active desktop session".into(),
        };
    };

    match executable {
        Some(executable) if !command_available(executable) => DiagnosticCheck {
            name: "capture backend".into(),
            passed: false,
            detail: format!("{backend} requires '{executable}' on PATH"),
        },
        _ => DiagnosticCheck {
            name: "capture backend".into(),
            passed: true,
            detail: backend.into(),
        },
    }
}

pub fn backend_name() -> &'static str {
    if cfg!(feature = "native-capture") {
        "native XCap"
    } else if std::env::var_os("WAYLAND_DISPLAY").is_some() {
        "Wayland grim fallback"
    } else if std::env::var_os("DISPLAY").is_some() {
        "X11 ImageMagick fallback"
    } else {
        "no active desktop session"
    }
}

pub fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

pub fn command_available(command: &str) -> bool {
    find_command(command).is_some()
}

/// Resolves a command the way a shell would, returning its full path.
/// Where Homebrew and Docker Desktop install their commands. Apps opened from
/// the Dock or Finder get a minimal `PATH` without them.
#[cfg(target_os = "macos")]
const EXTRA_COMMAND_DIRS: &[&str] = &["/opt/homebrew/bin", "/usr/local/bin"];
#[cfg(not(target_os = "macos"))]
const EXTRA_COMMAND_DIRS: &[&str] = &[];

pub fn find_command(command: &str) -> Option<PathBuf> {
    let command_path = Path::new(command);
    if command_path.components().count() > 1 {
        return command_path.is_file().then(|| command_path.to_path_buf());
    }
    std::env::var_os("PATH")
        .into_iter()
        .flat_map(|paths| std::env::split_paths(&paths).collect::<Vec<_>>())
        .chain(EXTRA_COMMAND_DIRS.iter().map(PathBuf::from))
        .map(|directory| directory.join(command))
        .find(|path| {
            if !path.is_file() {
                return false;
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                path.metadata()
                    .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
                    .unwrap_or(false)
            }
            #[cfg(not(unix))]
            {
                true
            }
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn report_health_requires_every_check() {
        let healthy = DiagnosticReport {
            checks: vec![DiagnosticCheck {
                name: "test".into(),
                passed: true,
                detail: "ready".into(),
            }],
        };
        assert!(healthy.is_healthy());

        let unhealthy = DiagnosticReport {
            checks: vec![DiagnosticCheck {
                name: "test".into(),
                passed: false,
                detail: "missing".into(),
            }],
        };
        assert!(!unhealthy.is_healthy());
    }

    #[test]
    fn version_matches_package_metadata() {
        assert_eq!(version(), "0.1.0");
    }

    #[test]
    fn command_lookup_does_not_require_spawning_processes() {
        assert!(command_available("sh"));
        assert!(!command_available("overlayr-command-that-does-not-exist"));
    }
}
