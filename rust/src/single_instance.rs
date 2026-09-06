use std::fs::{File, OpenOptions};
use std::io;
use std::path::PathBuf;

/// Process-wide lock preventing two Overlayr pipelines from competing for
/// capture and translation resources.
pub struct SingleInstance {
    _file: File,
}

impl SingleInstance {
    pub fn acquire() -> io::Result<Option<Self>> {
        let path = lock_path();
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path)?;

        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            if result != 0 {
                if io::Error::last_os_error().kind() == io::ErrorKind::WouldBlock {
                    return Ok(None);
                }
                return Err(io::Error::last_os_error());
            }
        }

        // On Unix the advisory lock is released by the kernel on process exit,
        // including crashes. Other platforms still get a useful per-user lock
        // file until their native mutex adapter is added.
        Ok(Some(Self { _file: file }))
    }
}

fn lock_path() -> PathBuf {
    std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("overlayr.lock")
}
