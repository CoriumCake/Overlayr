use std::io;

#[cfg(unix)]
use std::fs::{File, OpenOptions};
#[cfg(unix)]
use std::os::fd::AsRawFd;

/// Cross-platform process lock. The underlying crate uses a native mutex on
/// Windows, an abstract Unix socket on Linux, and an advisory file lock on
/// macOS.
pub struct SingleInstance {
    _inner: Option<single_instance::SingleInstance>,
    #[cfg(unix)]
    _fallback: Option<File>,
}

impl SingleInstance {
    pub fn acquire() -> io::Result<Option<Self>> {
        let identifier = if cfg!(target_os = "macos") {
            std::env::temp_dir()
                .join("overlayr.lock")
                .to_string_lossy()
                .into_owned()
        } else {
            "overlayr".into()
        };
        match single_instance::SingleInstance::new(&identifier) {
            Ok(inner) if inner.is_single() => Ok(Some(Self {
                _inner: Some(inner),
                #[cfg(unix)]
                _fallback: None,
            })),
            Ok(_) => Ok(None),
            Err(_error) => {
                #[cfg(unix)]
                {
                    acquire_file_lock().map(|file| {
                        file.map(|file| Self {
                            _inner: None,
                            _fallback: Some(file),
                        })
                    })
                }
                #[cfg(not(unix))]
                {
                    Err(io::Error::other(_error.to_string()))
                }
            }
        }
    }
}

#[cfg(unix)]
fn acquire_file_lock() -> io::Result<Option<File>> {
    let runtime_path = std::env::var_os("XDG_RUNTIME_DIR")
        .map(std::path::PathBuf::from)
        .map(|path| path.join("overlayr.lock"));
    let temp_path = std::env::temp_dir().join("overlayr.lock");
    if let Some(path) = runtime_path {
        match open_file_lock(&path) {
            Ok(lock) => return Ok(lock),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::NotFound
                        | io::ErrorKind::PermissionDenied
                        | io::ErrorKind::ReadOnlyFilesystem
                ) => {}
            Err(error) => return Err(error),
        }
    }
    open_file_lock(&temp_path)
}

#[cfg(unix)]
fn open_file_lock(path: &std::path::Path) -> io::Result<Option<File>> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)?;
    // LOCK_EX | LOCK_NB keeps the descriptor-owned lock alive for the entire
    // process and releases it automatically if the process exits abruptly.
    let result = unsafe { flock(file.as_raw_fd(), 2 | 4) };
    if result == 0 {
        return Ok(Some(file));
    }
    let error = io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(11) | Some(35)) {
        Ok(None)
    } else {
        Err(error)
    }
}

#[cfg(unix)]
unsafe extern "C" {
    fn flock(
        file_descriptor: std::os::raw::c_int,
        operation: std::os::raw::c_int,
    ) -> std::os::raw::c_int;
}
