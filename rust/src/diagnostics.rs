use std::fmt;
use std::path::Path;

use crate::capture::PlatformCapture;
use crate::config::AppConfig;
#[cfg(feature = "native-ocr")]
use crate::ocr::NativeTesseractOcr;
use crate::ocr::TesseractOcr;
use crate::services::CaptureBackend;

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
    report.checks.push(DiagnosticCheck {
        name: "translation configuration".into(),
        passed: config.translation_endpoint.starts_with("http://")
            || config.translation_endpoint.starts_with("https://"),
        detail: config.translation_endpoint.clone(),
    });

    report
}

fn ocr_check(language: &str) -> DiagnosticCheck {
    #[cfg(feature = "native-ocr")]
    let result = NativeTesseractOcr::new("paragraph").validate_language(language);
    #[cfg(not(feature = "native-ocr"))]
    let result = TesseractOcr::new(
        std::env::var("OVERLAYR_TESSERACT_BIN").unwrap_or_else(|_| "tesseract".into()),
        "paragraph",
    )
    .validate_language(language);

    match result {
        Ok(()) => DiagnosticCheck {
            name: "OCR".into(),
            passed: true,
            detail: format!("language '{language}' is ready"),
        },
        Err(error) => DiagnosticCheck {
            name: "OCR".into(),
            passed: false,
            detail: error,
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
    let command_path = Path::new(command);
    if command_path.components().count() > 1 {
        return command_path.is_file();
    }
    std::env::var_os("PATH")
        .into_iter()
        .flat_map(|paths| std::env::split_paths(&paths).collect::<Vec<_>>())
        .map(|directory| directory.join(command))
        .any(|path| {
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
