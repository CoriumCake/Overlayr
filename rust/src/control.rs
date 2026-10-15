//! Commands for the running Overlayr, such as starting or stopping the
//! overlay from a keyboard shortcut.
//!
//! Wayland doesn't let applications grab global shortcuts, so the desktop
//! runs `overlayr --toggle` on a key press, and that process passes the
//! command to the running instance over a Unix socket in the user's runtime
//! directory.

use std::io;
use std::path::PathBuf;
use std::sync::mpsc;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Command {
    /// Start the overlay, or stop it when it is running.
    ToggleOverlay,
    /// Hide or show the translations, to see the original text.
    ToggleTranslations,
}

impl Command {
    pub fn argument(self) -> &'static str {
        match self {
            Self::ToggleOverlay => "--toggle",
            Self::ToggleTranslations => "--toggle-translations",
        }
    }

    pub fn from_argument(argument: &str) -> Option<Self> {
        [Self::ToggleOverlay, Self::ToggleTranslations]
            .into_iter()
            .find(|command| command.argument() == argument)
    }

    fn name(self) -> &'static str {
        match self {
            Self::ToggleOverlay => "toggle-overlay",
            Self::ToggleTranslations => "toggle-translations",
        }
    }

    fn from_name(name: &str) -> Option<Self> {
        [Self::ToggleOverlay, Self::ToggleTranslations]
            .into_iter()
            .find(|command| command.name() == name)
    }
}

fn socket_path() -> Option<PathBuf> {
    let runtime = std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from)?;
    Some(runtime.join("overlayr.sock"))
}

/// Sends `command` to the running Overlayr.
#[cfg(unix)]
pub fn send(command: Command) -> io::Result<()> {
    use std::io::Write;
    let path = socket_path().ok_or_else(|| io::Error::other("XDG_RUNTIME_DIR is not set"))?;
    let mut stream = std::os::unix::net::UnixStream::connect(path)?;
    writeln!(stream, "{}", command.name())
}

#[cfg(not(unix))]
pub fn send(_command: Command) -> io::Result<()> {
    Err(io::Error::other("commands need a Unix socket"))
}

/// Receives commands sent with [`send`] while it is alive.
pub struct Listener {
    commands: mpsc::Receiver<Command>,
    #[cfg(unix)]
    path: PathBuf,
}

impl Listener {
    /// Starts listening; `wake` is called after each command arrives, so a
    /// UI can process it without waiting for its next repaint.
    #[cfg(unix)]
    pub fn start(wake: impl Fn() + Send + 'static) -> io::Result<Self> {
        use std::io::BufRead;
        let path = socket_path().ok_or_else(|| io::Error::other("XDG_RUNTIME_DIR is not set"))?;
        // Only one Overlayr runs at a time, so a leftover socket is stale.
        let _ = std::fs::remove_file(&path);
        let listener = std::os::unix::net::UnixListener::bind(&path)?;
        let (sender, commands) = mpsc::channel();
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let mut line = String::new();
                if io::BufReader::new(stream).read_line(&mut line).is_ok()
                    && let Some(command) = Command::from_name(line.trim())
                {
                    if sender.send(command).is_err() {
                        return;
                    }
                    wake();
                }
            }
        });
        Ok(Self { commands, path })
    }

    #[cfg(not(unix))]
    pub fn start(_wake: impl Fn() + Send + 'static) -> io::Result<Self> {
        Err(io::Error::other("commands need a Unix socket"))
    }

    /// Commands received since the last call.
    pub fn received(&self) -> Vec<Command> {
        self.commands.try_iter().collect()
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        #[cfg(unix)]
        let _ = std::fs::remove_file(&self.path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_round_trip_through_their_names_and_arguments() {
        for command in [Command::ToggleOverlay, Command::ToggleTranslations] {
            assert_eq!(Command::from_name(command.name()), Some(command));
            assert_eq!(Command::from_argument(command.argument()), Some(command));
        }
        assert_eq!(Command::from_name("rm -rf"), None);
    }
}
