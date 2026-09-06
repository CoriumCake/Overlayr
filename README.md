# Overlayr

Overlayr is a real-time screen translation overlay. The project is currently
migrating from a Windows-only Python prototype to a performance-oriented Rust
core.

## Migration status

The Rust application lives in `rust/` and currently contains:

- latest-frame scheduling so OCR cannot build an unbounded backlog;
- language-aware bounded translation caching;
- persisted settings with atomic writes;
- a native egui settings and live-results window;
- Wayland screen/region capture through `grim` and `slurp`;
- local Tesseract OCR with language-pack validation;
- a configurable LibreTranslate-compatible translation service;
- background processing with unchanged-frame skipping, confidence filtering,
  and single-instance locking.

The existing Python desktop app remains temporarily available as a reference
implementation while capture, OCR, translation, and UI adapters are migrated.

## Build the Rust core

```bash
cd rust
cargo test --release
cargo run --release
```

On Linux/Wayland, install `grim`, `slurp`, `tesseract`, and the language packs
you need. Set `OVERLAYR_TRANSLATION_API_KEY` when the configured translation
endpoint requires one. The desktop launcher is `packaging/overlayr.desktop`;
install the release binary as `overlayr` on `PATH`, then copy that file to
`~/.local/share/applications/`. The repository also provides
`packaging/install-linux.sh` to build and install the binary, desktop entry,
and icon for the current user.

## Planned architecture

```text
platform capture -> frame scheduler -> image preprocessing -> OCR adapter
                  -> text stabilization -> translation adapter -> overlay UI
```

The website remains under `web/` and is independent from the desktop core.

Every Rust change is checked in CI with rustfmt, strict Clippy, tests, and a
release build.

## Legacy prototype

The Python implementation is under `src/overlayr/`. It is retained as a
behavioral reference until the Rust adapters have been validated on Windows
and macOS as well as Linux.
