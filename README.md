# Overlayr

Overlayr is a real-time translation overlay for games and other desktop apps.
It reads the text on screen, translates it, and draws the translation over the
original in the same place, colour and size. Creators can also build
hand-made **translation packs** for a game in the built-in Creator Studio, with
AI drafts to speed things up, so players get human-quality translations.

The desktop app is written in Rust and lives in `rust/`. The website lives in
`web/` and is independent of the desktop app.

## Features

### Overlay

- **Neural OCR made for game text.** PaddleOCR PP-OCRv6 models run through
  ONNX Runtime and read pixel and stylised game fonts without per-font tuning.
  Tesseract is the fallback for languages the neural models don't cover.
- **Text tracking** that runs OCR and translation only on new text. Each frame
  goes through three checks:
  - **Change:** an unchanged frame costs one comparison.
  - **Follow:** known text that moved, or was restyled (for example by a hover
    highlight), keeps its translation without being read again.
  - **Detect:** OCR runs only when new text appears.
- **Natural rendering.** Translations take the game's text colour and a
  backdrop sampled from the background. Lines in a list share one size, and
  long text wraps instead of shrinking.
- **Whole-screen updates.** A screen's machine translations appear together
  instead of line by line.
- **Focus-aware.** On Hyprland, the overlay hides as soon as the game loses
  focus.
- **Keybinds** to start or stop the overlay and to hide or show the
  translations.

### Translation packs and Creator Studio

- **Record** an application while playing. The recorder saves a keyframe each
  time new text appears and keeps recording in the background.
- **Translate and style** each text entry on a full-screen preview:
  - drag boxes into place;
  - move between boxes with the arrow keys;
  - boxes grow to fit longer translations;
  - edits save automatically.
- **Entries, not frames.** An entry applies wherever and whenever its text
  appears during playback. Text no entry covers stays in the original
  language.
- **AI draft translations** from Claude, ChatGPT, Gemini or DeepSeek. All of a
  screen's text goes in one request, together with a per-pack context prompt
  (for example the game's name) and the creator's existing translations as a
  glossary, so names and terms stay consistent. The creator then fixes only
  what's wrong.

### Desktop integration

- A settings window with a separate modal for languages, packs, AI provider
  keys, keybinds and performance options. The performance options include
  capture rate, highlight following, settle waits, OCR threads and low
  priority, so Overlayr can run on low-end machines.
- Setup checks that download the OCR models and ONNX Runtime, and can install
  Tesseract and a local LibreTranslate server. Steps that need root go through
  `pkexec`.
- `overlayr://` login callback for the web account.
- Single-instance locking, atomic settings writes, and `overlayr --diagnose`.

## Platform support

| Platform | Status |
| --- | --- |
| Linux, Wayland (Hyprland) | Primary target. Capture uses `grim`/`slurp`, and window listing and focus use Hyprland IPC. |
| Linux, other Wayland / X11 | Screen and region capture. No window picker or focus tracking. |
| Windows, macOS | Native XCap capture compiles in CI. Not yet tested on real machines. |

Windows and macOS hide the overlay from screen capture. X11 and Wayland have
no portable way to do this.

## Build and run

```bash
cd rust
cargo test --release
cargo run --release --features native-ocr

# Check desktop, OCR and translation readiness without opening the GUI
cargo run --release -- --diagnose
```

Cargo features:

| Feature | Effect |
| --- | --- |
| *(none)* | Portable build: `grim`/ImageMagick capture and CLI Tesseract. |
| `native-ocr` | Keeps Tesseract loaded in-process between reads. Used by the Linux installer. |
| `native-capture` | XCap capture. Replaces the Hyprland window picker, so don't use it on Hyprland. |
| `native-production` | `native-ocr` + `native-capture`. |

Build-time packages for `native-ocr` on Linux: `libclang-dev`,
`libtesseract-dev`, `libleptonica-dev`.

Runtime dependencies on Linux/Wayland are `grim`, `slurp`, and Tesseract with
the language packs you need. You also need a LibreTranslate-compatible
endpoint, which defaults to `http://localhost:5000/translate`. Set
`OVERLAYR_TRANSLATION_API_KEY` if your endpoint needs a key.

The setup checks download the neural OCR models (about 30 MB) and ONNX
Runtime (about 11 MB) into `~/.local/share/overlayr/`. The ONNX Runtime
download is skipped when the system provides `libonnxruntime.so`. Set
`ORT_DYLIB_PATH` to use a specific ONNX Runtime build. Without ONNX Runtime,
Overlayr still starts and uses Tesseract.

## Install

**Linux:**

```bash
packaging/install-linux.sh                          # in-process OCR build
OVERLAYR_NATIVE_BUILD=0 packaging/install-linux.sh  # portable build, no dev packages
```

The installer sets up the following for the current user:
- the `overlayr` binary on `PATH`;
- the desktop entry and icon;
- the `overlayr://` URL handler.

**Windows:** run `build.ps1`, adding `-Native` when the XCap/Tesseract
dependencies are available. The `overlayr://` handler is registered on first
launch.

## Command line

```text
overlayr                        Open the settings window
overlayr --diagnose             Check capture, OCR and translation readiness
overlayr --toggle               Start or stop the overlay of the running instance
overlayr --toggle-translations  Hide or show the translations
overlayr --version | --help
```

Wayland doesn't let apps grab global shortcuts. To use the keybinds, bind the
toggle commands in your compositor instead, for example in Hyprland:

```ini
bind = SUPER, T, exec, overlayr --toggle
bind = SUPER SHIFT, T, exec, overlayr --toggle-translations
```

## Architecture

```text
capture ─▶ tracker (change / follow / detect) ─▶ OCR thread (PaddleOCR | Tesseract)
                    │                                    │
                    ▼                                    ▼
              overlay UI  ◀──  pack matching  ◀──  machine translation
```

| Module | Role |
| --- | --- |
| `capture.rs`, `hyprland.rs` | Screen, region and window capture. Hyprland IPC for windows and focus. |
| `tracker.rs` | Follows text between frames and decides when OCR must run. |
| `neural.rs`, `ocr.rs` | PaddleOCR via ONNX Runtime, and the Tesseract backends. |
| `translation.rs` | LibreTranslate-compatible client with a bounded cache. |
| `pack.rs` | Translation packs and similarity matching. |
| `studio.rs`, `studio_ui.rs` | Creator Studio recorder and editor. |
| `ai.rs` | AI draft translations (Anthropic Messages API, plus OpenAI-compatible APIs). |
| `runtime.rs`, `pipeline.rs`, `ui.rs` | Pipeline orchestration and the egui overlay and settings. |
| `control.rs`, `single_instance.rs` | `--toggle` commands over a Unix socket, and instance locking. |
| `installer.rs`, `diagnostics.rs`, `auth.rs`, `config.rs` | Setup, readiness checks, login session, settings. |

CI runs rustfmt, strict Clippy, and the tests for the default, `native-ocr`
and `native-production` builds, then Linux release builds. Native capture is
also compile-checked on Windows and macOS.

## Legacy prototype

The original Python implementation in `src/overlayr/` is kept only as a
behavioural reference. It is not part of the launcher or the release build.
