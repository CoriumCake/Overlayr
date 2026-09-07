use std::io;

/// Cross-platform process lock. The underlying crate uses a native mutex on
/// Windows, an abstract Unix socket on Linux, and an advisory file lock on
/// macOS.
pub struct SingleInstance {
    _inner: single_instance::SingleInstance,
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
        let inner = single_instance::SingleInstance::new(&identifier)
            .map_err(|error| io::Error::other(error.to_string()))?;
        if inner.is_single() {
            Ok(Some(Self { _inner: inner }))
        } else {
            Ok(None)
        }
    }
}
