//! Minimal Hyprland IPC client used to enumerate monitors and windows.
//!
//! Wayland does not let ordinary clients list or capture other windows, so
//! window selection relies on the compositor's own IPC. This talks to the
//! Hyprland request socket directly to avoid spawning `hyprctl` every frame.

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::Duration;

use serde::Deserialize;
use serde::de::DeserializeOwned;

use crate::services::ServiceResult;
use crate::types::Rect;

#[derive(Clone, Debug, Deserialize)]
pub struct WorkspaceRef {
    pub id: i64,
    pub name: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Monitor {
    pub id: i64,
    pub name: String,
    #[serde(default)]
    pub description: String,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub scale: f64,
    #[serde(default)]
    pub transform: u8,
    pub active_workspace: WorkspaceRef,
    pub special_workspace: WorkspaceRef,
}

impl Monitor {
    /// Bounds in the compositor's logical layout, which is the coordinate
    /// space `grim -g` and window positions use.
    pub fn logical_bounds(&self) -> Rect {
        let (width, height) = if self.transform % 2 == 1 {
            (self.height, self.width)
        } else {
            (self.width, self.height)
        };
        let scale = if self.scale > 0.0 { self.scale } else { 1.0 };
        Rect::new(
            self.x,
            self.y,
            (f64::from(width) / scale).round() as u32,
            (f64::from(height) / scale).round() as u32,
        )
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct Client {
    pub address: String,
    pub mapped: bool,
    pub hidden: bool,
    pub at: [i32; 2],
    pub size: [i32; 2],
    pub workspace: WorkspaceRef,
    pub monitor: i64,
    pub class: String,
    pub title: String,
    pub pid: i64,
    #[serde(rename = "focusHistoryID", default)]
    pub focus_history_id: i64,
    #[serde(default)]
    pub floating: bool,
    /// Fullscreen state; non-zero when fullscreen or maximised.
    #[serde(default)]
    pub fullscreen: i64,
    /// The window's ext-foreign-toplevel identifier, which `grim -T` accepts.
    #[serde(rename = "stableId", default)]
    pub stable_id: String,
}

impl Client {
    pub fn bounds(&self) -> Rect {
        Rect::new(
            self.at[0],
            self.at[1],
            self.size[0].max(0) as u32,
            self.size[1].max(0) as u32,
        )
    }

    /// Whether the window has its full size: fullscreen, maximised,
    /// floating at a size of its own choosing, or tiled alone so it fills
    /// most of its monitor. A tiled window sharing the screen is squeezed and
    /// games lay out their interface differently then.
    pub fn is_full_size(&self, monitors: &[Monitor]) -> bool {
        if self.fullscreen != 0 || self.floating {
            return true;
        }
        let Some(monitor) = monitors.iter().find(|monitor| monitor.id == self.monitor) else {
            return true;
        };
        let screen = monitor.logical_bounds();
        // Gaps and a status bar take a little room even when tiled alone.
        self.size[0] as f32 >= screen.width as f32 * 0.85
            && self.size[1] as f32 >= screen.height as f32 * 0.85
    }

    /// A window is capturable only while its workspace is shown on a monitor;
    /// otherwise the screen pixels at its position belong to something else.
    pub fn is_visible(&self, monitors: &[Monitor]) -> bool {
        self.mapped
            && !self.hidden
            && monitors.iter().any(|monitor| {
                monitor.id == self.monitor
                    && (monitor.active_workspace.id == self.workspace.id
                        || monitor.special_workspace.id == self.workspace.id)
            })
    }
}

fn socket_path() -> Option<PathBuf> {
    let signature = std::env::var_os("HYPRLAND_INSTANCE_SIGNATURE")?;
    let runtime = std::env::var_os("XDG_RUNTIME_DIR")?;
    Some(
        PathBuf::from(runtime)
            .join("hypr")
            .join(signature)
            .join(".socket.sock"),
    )
}

/// Calls `on_change` whenever a window opens or closes, until Hyprland's
/// event socket closes. Runs on its own thread.
pub fn watch_windows(on_change: impl Fn() + Send + 'static) -> ServiceResult<()> {
    watch_events(is_window_change, on_change)
}

/// Calls `on_change` whenever focus moves to another window, workspace or
/// monitor, so the overlay can hide the moment the game is left.
pub fn watch_focus(on_change: impl Fn() + Send + 'static) -> ServiceResult<()> {
    watch_events(is_focus_change, on_change)
}

/// Whether a Hyprland event line moves focus or what is shown.
fn is_focus_change(line: &str) -> bool {
    matches!(
        line.split_once(">>").map(|(event, _)| event),
        Some(
            "activewindowv2"
                | "workspacev2"
                | "focusedmonv2"
                | "closewindow"
                | "fullscreen"
                | "activespecialv2"
        )
    )
}

/// Whether the focused window is `address` or one of Overlayr's own, i.e.
/// whether translations should show. Hidden while another app has focus.
pub fn is_focused(address: &str) -> ServiceResult<bool> {
    let window: serde_json::Value = request("activewindow")?;
    Ok(window["address"].as_str() == Some(address)
        || window["pid"].as_u64() == Some(u64::from(std::process::id())))
}

fn watch_events(
    wanted: fn(&str) -> bool,
    on_change: impl Fn() + Send + 'static,
) -> ServiceResult<()> {
    use std::io::BufRead;
    let path = socket_path()
        .map(|path| path.with_file_name(".socket2.sock"))
        .ok_or_else(|| "Hyprland is not running".to_string())?;
    let stream = UnixStream::connect(&path)
        .map_err(|error| format!("could not listen to Hyprland events: {error}"))?;
    std::thread::spawn(move || {
        for line in std::io::BufReader::new(stream).lines() {
            let Ok(line) = line else {
                return;
            };
            if wanted(&line) {
                on_change();
            }
        }
    });
    Ok(())
}

/// Whether a Hyprland event line opens or closes a window.
fn is_window_change(line: &str) -> bool {
    matches!(
        line.split_once(">>").map(|(event, _)| event),
        Some("openwindow" | "closewindow")
    )
}

pub fn is_available() -> bool {
    socket_path().is_some_and(|path| path.exists())
}

fn send(message: &str) -> ServiceResult<Vec<u8>> {
    let path = socket_path().ok_or_else(|| "Hyprland is not running".to_string())?;
    let mut stream = UnixStream::connect(&path)
        .map_err(|error| format!("could not connect to Hyprland: {error}"))?;
    let timeout = Some(Duration::from_secs(1));
    stream
        .set_read_timeout(timeout)
        .and_then(|()| stream.set_write_timeout(timeout))
        .map_err(|error| format!("could not configure Hyprland socket: {error}"))?;
    stream
        .write_all(message.as_bytes())
        .map_err(|error| format!("could not query Hyprland: {error}"))?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("could not read Hyprland response: {error}"))?;
    Ok(response)
}

fn request<T: DeserializeOwned>(command: &str) -> ServiceResult<T> {
    let response = send(&format!("j/{command}"))?;
    serde_json::from_slice(&response)
        .map_err(|error| format!("unexpected Hyprland {command} response: {error}"))
}

/// Sends a command whose only reply is `ok` or an error message.
fn command(message: &str) -> ServiceResult<()> {
    let response = send(message)?;
    let response = String::from_utf8_lossy(&response);
    if response.trim() == "ok" {
        Ok(())
    } else {
        Err(response.trim().to_string())
    }
}

/// Tries Hyprland's Lua IPC (0.55+) first, then the older dispatcher syntax.
fn command_with_fallback(lua: &str, legacy: &[String]) -> ServiceResult<()> {
    command(lua).or_else(|lua_error| {
        legacy
            .iter()
            .try_for_each(|message| command(message))
            .map_err(|_| lua_error)
    })
}

/// Adds a window rule that makes Overlayr's overlay window float above the
/// captured window: pinned, undecorated and never focused. Without it the
/// compositor tiles the overlay like any other window, since Wayland clients
/// cannot position their own windows.
pub fn install_overlay_rule(title: &str) -> ServiceResult<()> {
    let pattern = format!("^{}$", regex_escape(title));
    let lua = format!(
        "eval hl.window_rule({{ name = \"overlayr-overlay\", match = {{ title = {pattern:?} }}, \
         float = true, pin = true, no_focus = true, no_initial_focus = true, border_size = 0, \
         rounding = 0, no_shadow = true, no_blur = true, no_anim = true, decorate = false }})"
    );
    let legacy = [
        "float",
        "pin",
        "nofocus",
        "noinitialfocus",
        "noborder",
        "noshadow",
        "noblur",
        "noanim",
        "rounding 0",
    ]
    .map(|effect| format!("keyword windowrulev2 {effect},title:{pattern}"));
    command_with_fallback(&lua, &legacy)
}

/// Moves and resizes a window to exact layout coordinates.
pub fn place_window(address: &str, bounds: Rect) -> ServiceResult<()> {
    let window = format!("address:{address}");
    let Rect {
        left,
        top,
        width,
        height,
    } = bounds;
    command_with_fallback(
        &format!("dispatch hl.dsp.window.float({{ window = {window:?}, action = \"enable\" }})"),
        &[format!("dispatch setfloating {window}")],
    )?;
    command_with_fallback(
        &format!(
            "dispatch hl.dsp.window.resize({{ window = {window:?}, x = {width}, y = {height}, exact = true }})"
        ),
        &[format!(
            "dispatch resizewindowpixel exact {width} {height},{window}"
        )],
    )?;
    command_with_fallback(
        &format!(
            "dispatch hl.dsp.window.move({{ window = {window:?}, x = {left}, y = {top}, exact = true }})"
        ),
        &[format!(
            "dispatch movewindowpixel exact {left} {top},{window}"
        )],
    )
}

/// Keeps windows of `class` rendering while their workspace is hidden or
/// they lose focus; games otherwise stop drawing and the studio's live view
/// freezes while the creator does something else.
pub fn keep_rendering(class: &str) -> ServiceResult<()> {
    let pattern = format!("^{}$", regex_escape(class));
    command_with_fallback(
        &format!(
            "eval hl.window_rule({{ name = \"overlayr-studio-game\", match = {{ class = {pattern:?} }}, \
             render_unfocused = true }})"
        ),
        &[format!(
            "keyword windowrulev2 renderunfocused,class:{pattern}"
        )],
    )
}

/// Undoes [`keep_rendering`] once recording stops, so the game goes back to
/// pausing its drawing while hidden and doesn't use the GPU for nothing.
pub fn stop_keeping_rendering() -> ServiceResult<()> {
    command(
        "eval hl.window_rule({ name = \"overlayr-studio-game\", match = { class = \"^$\" }, \
         enabled = false })",
    )
}

/// Moves a window to `workspace` without following it there.
pub fn move_to_workspace(address: &str, workspace: &str) -> ServiceResult<()> {
    let window = format!("address:{address}");
    command_with_fallback(
        &format!(
            "dispatch hl.dsp.window.move({{ window = {window:?}, workspace = {workspace:?}, follow = false }})"
        ),
        &[format!(
            "dispatch movetoworkspacesilent {workspace},{window}"
        )],
    )
}

/// Sets a window's fullscreen state: 0 normal, 1 maximised, 2 fullscreen.
pub fn set_fullscreen(address: &str, state: i64) -> ServiceResult<()> {
    let window = format!("address:{address}");
    command(&format!(
        "dispatch hl.dsp.window.fullscreen_state({{ window = {window:?}, internal = {state}, client = {state} }})"
    ))
}

/// Focuses a window, switching to its workspace.
pub fn focus_window(address: &str) -> ServiceResult<()> {
    let window = format!("address:{address}");
    command_with_fallback(
        &format!("dispatch hl.dsp.focus({{ window = {window:?} }})"),
        &[format!("dispatch focuswindow {window}")],
    )
}

/// Binds `keys` ("SUPER + SHIFT + O") to run `command`.
pub fn bind_command(keys: &str, command: &str) -> ServiceResult<()> {
    command_with_fallback(
        &format!("eval hl.bind({keys:?}, hl.dsp.exec_cmd({command:?}))"),
        &[format!("keyword bind {},exec,{command}", legacy_keys(keys))],
    )
}

/// Removes the binding of `keys`.
pub fn unbind(keys: &str) -> ServiceResult<()> {
    command_with_fallback(
        &format!("eval hl.unbind({keys:?})"),
        &[format!("keyword unbind {}", legacy_keys(keys))],
    )
}

/// "SUPER + SHIFT + O" in the older config syntax: "SUPER SHIFT,O".
fn legacy_keys(keys: &str) -> String {
    let mut parts = keys.split('+').map(str::trim).collect::<Vec<_>>();
    let key = parts.pop().unwrap_or_default();
    format!("{},{key}", parts.join(" "))
}

/// Finds this process's window with the given title.
pub fn own_window(title: &str) -> ServiceResult<Option<Client>> {
    let pid = i64::from(std::process::id());
    Ok(clients()?
        .into_iter()
        .find(|client| client.pid == pid && client.title == title))
}

fn regex_escape(text: &str) -> String {
    text.chars()
        .flat_map(|character| {
            let escape = "\\.^$|?*+()[]{}".contains(character);
            escape.then_some('\\').into_iter().chain([character])
        })
        .collect()
}

pub fn monitors() -> ServiceResult<Vec<Monitor>> {
    request("monitors")
}

pub fn clients() -> ServiceResult<Vec<Client>> {
    request("clients")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn monitors_fixture() -> Vec<Monitor> {
        serde_json::from_str(
            r#"[{"id":0,"name":"eDP-1","description":"Panel","x":2982,"y":180,
                 "width":2560,"height":1440,"scale":1.6,"transform":0,
                 "activeWorkspace":{"id":2,"name":"2"},
                 "specialWorkspace":{"id":0,"name":""}},
                {"id":1,"name":"HDMI-A-1","x":0,"y":0,"width":1080,"height":1920,
                 "scale":1.0,"transform":1,
                 "activeWorkspace":{"id":11,"name":"11"},
                 "specialWorkspace":{"id":-98,"name":"special:scratch"}}]"#,
        )
        .expect("monitor fixture should parse")
    }

    fn client(workspace: i64, monitor: i64, hidden: bool) -> Client {
        Client {
            address: "0x1".into(),
            mapped: true,
            hidden,
            at: [10, 20],
            size: [300, 200],
            workspace: WorkspaceRef {
                id: workspace,
                name: workspace.to_string(),
            },
            monitor,
            class: "kitty".into(),
            title: "shell".into(),
            pid: 1,
            focus_history_id: 0,
            floating: false,
            fullscreen: 0,
            stable_id: String::new(),
        }
    }

    #[test]
    fn converts_shortcuts_to_the_older_bind_syntax() {
        assert_eq!(legacy_keys("SUPER + SHIFT + O"), "SUPER SHIFT,O");
        assert_eq!(legacy_keys("F8"), ",F8");
    }

    #[test]
    fn notices_windows_opening_and_closing_only() {
        assert!(is_window_change("openwindow>>55eed9ff1620,2,kitty,kitty"));
        assert!(is_window_change("closewindow>>55eed9ff1620"));
        assert!(!is_window_change("windowtitlev2>>55eed9ff1620,vim"));
        assert!(!is_window_change("activewindow>>kitty,shell"));
    }

    #[test]
    fn escapes_window_title_patterns() {
        assert_eq!(regex_escape("Overlayr overlay"), "Overlayr overlay");
        assert_eq!(regex_escape("a.b (c)"), "a\\.b \\(c\\)");
    }

    #[test]
    fn monitor_bounds_use_logical_size_and_rotation() {
        let monitors = monitors_fixture();
        assert_eq!(
            monitors[0].logical_bounds(),
            Rect::new(2982, 180, 1600, 900)
        );
        assert_eq!(monitors[1].logical_bounds(), Rect::new(0, 0, 1920, 1080));
    }

    #[test]
    fn windows_are_visible_only_on_shown_workspaces() {
        let monitors = monitors_fixture();
        assert!(client(2, 0, false).is_visible(&monitors));
        assert!(client(-98, 1, false).is_visible(&monitors));
        assert!(!client(3, 0, false).is_visible(&monitors));
        assert!(!client(2, 1, false).is_visible(&monitors));
        assert!(!client(2, 0, true).is_visible(&monitors));
    }

    #[test]
    fn tiled_windows_sharing_the_screen_are_not_full_size() {
        let monitors = monitors_fixture();
        let sized = |width, height, floating, fullscreen| Client {
            size: [width, height],
            floating,
            fullscreen,
            ..client(2, 0, false)
        };
        // A 1600x900 logical screen with gaps and a bar.
        assert!(sized(1556, 818, false, 0).is_full_size(&monitors));
        assert!(!sized(771, 818, false, 0).is_full_size(&monitors));
        assert!(sized(771, 818, false, 1).is_full_size(&monitors));
        assert!(sized(800, 600, true, 0).is_full_size(&monitors));
    }

    #[test]
    fn client_parses_hyprctl_fields() {
        let clients: Vec<Client> = serde_json::from_str(
            r#"[{"address":"0x55","mapped":true,"hidden":false,"at":[5,6],"size":[70,80],
                 "workspace":{"id":2,"name":"2"},"monitor":0,"class":"firefox",
                 "title":"Docs","pid":42,"focusHistoryID":3,"stableId":"1800000e",
                 "floating":false}]"#,
        )
        .expect("client fixture should parse");
        assert_eq!(clients[0].bounds(), Rect::new(5, 6, 70, 80));
        assert_eq!(clients[0].focus_history_id, 3);
        assert_eq!(clients[0].stable_id, "1800000e");
    }
}
