//! Keyboard shortcuts on macOS, which work while another app (the game) has
//! focus. Hyprland binds shortcuts itself and runs `overlayr --toggle`; see
//! [`crate::control`].

use std::str::FromStr;
use std::sync::mpsc;

use global_hotkey::hotkey::HotKey;
use global_hotkey::{GlobalHotKeyEvent, GlobalHotKeyManager, HotKeyState};

use crate::config::Keybinds;
use crate::control::Command;

/// The registered shortcuts. Dropping it unregisters them.
pub struct Hotkeys {
    manager: GlobalHotKeyManager,
    registered: Vec<(HotKey, Command)>,
    pressed: mpsc::Receiver<u32>,
}

impl Hotkeys {
    /// Must run on the main thread, which receives the key presses. `wake`
    /// is called after each press, so a UI can act on it without waiting for
    /// its next repaint.
    pub fn start(wake: impl Fn() + Send + Sync + 'static) -> Result<Self, String> {
        let manager = GlobalHotKeyManager::new().map_err(|error| error.to_string())?;
        let (sender, pressed) = mpsc::channel();
        // The handler can only be set once per process; Overlayr starts
        // shortcuts once.
        GlobalHotKeyEvent::set_event_handler(Some(move |event: GlobalHotKeyEvent| {
            if event.state() == HotKeyState::Pressed && sender.send(event.id()).is_ok() {
                wake();
            }
        }));
        Ok(Self {
            manager,
            registered: Vec::new(),
            pressed,
        })
    }

    /// Replaces the registered shortcuts with `keybinds`; empty ones are off.
    pub fn bind(&mut self, keybinds: &Keybinds) -> Result<(), String> {
        for (hotkey, _) in self.registered.drain(..) {
            let _ = self.manager.unregister(hotkey);
        }
        let mut errors = Vec::new();
        for (keys, command) in [
            (&keybinds.toggle_overlay, Command::ToggleOverlay),
            (&keybinds.toggle_translations, Command::ToggleTranslations),
        ] {
            if keys.trim().is_empty() {
                continue;
            }
            match parse(keys).and_then(|hotkey| {
                self.manager
                    .register(hotkey)
                    .map(|()| hotkey)
                    .map_err(|error| error.to_string())
            }) {
                Ok(hotkey) => self.registered.push((hotkey, command)),
                Err(error) => errors.push(format!("{keys}: {error}")),
            }
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }

    /// Commands whose shortcut was pressed since the last call.
    pub fn received(&self) -> Vec<Command> {
        self.pressed
            .try_iter()
            .filter_map(|id| {
                self.registered
                    .iter()
                    .find(|(hotkey, _)| hotkey.id() == id)
                    .map(|(_, command)| *command)
            })
            .collect()
    }
}

/// A stored shortcut ("SUPER + SHIFT + O", with XKB key names as Hyprland
/// knows them) as a hotkey. SUPER is the Command key.
fn parse(keys: &str) -> Result<HotKey, String> {
    let tokens = keys
        .split('+')
        .map(str::trim)
        .map(|token| match token {
            "Return" => "ENTER",
            "Prior" => "PAGEUP",
            "Next" => "PAGEDOWN",
            "apostrophe" => "QUOTE",
            "grave" => "BACKQUOTE",
            other => other,
        })
        .collect::<Vec<_>>();
    HotKey::from_str(&tokens.join("+")).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use global_hotkey::hotkey::{Code, Modifiers};

    #[test]
    fn parses_stored_shortcuts() {
        assert_eq!(
            parse("SUPER + SHIFT + O").unwrap(),
            HotKey::new(Some(Modifiers::META | Modifiers::SHIFT), Code::KeyO)
        );
        assert_eq!(
            parse("CTRL + ALT + Prior").unwrap(),
            HotKey::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::PageUp)
        );
        assert_eq!(
            parse("SUPER + grave").unwrap(),
            HotKey::new(Some(Modifiers::META), Code::Backquote)
        );
        for key in [
            "space",
            "Return",
            "Escape",
            "BackSpace",
            "left",
            "bracketleft",
            "apostrophe",
            "F8",
            "1",
            "minus",
        ] {
            assert!(parse(&format!("SUPER + {key}")).is_ok(), "{key}");
        }
    }
}
