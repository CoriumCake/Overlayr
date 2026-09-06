# Overlayr

Overlayr is a real-time screen translation overlay. The project is currently
migrating from a Windows-only Python prototype to a performance-oriented Rust
core.

## Migration status

The Rust foundation lives in `rust/` and currently contains platform-neutral
pipeline primitives:

- latest-frame scheduling so OCR cannot build an unbounded backlog;
- language-aware bounded translation caching;
- shared geometry and OCR text contracts.

The existing Python desktop app remains temporarily available as a reference
implementation while capture, OCR, translation, and UI adapters are migrated.

## Build the Rust core

```bash
cd rust
cargo test
cargo run
```

## Planned architecture

```text
platform capture -> frame scheduler -> image preprocessing -> OCR adapter
                  -> text stabilization -> translation adapter -> overlay UI
```

Platform-specific capture and overlay implementations will be added behind
the Rust interfaces. The website remains under `web/` and is independent from
the desktop core.

## Legacy prototype

The Python implementation is under `src/overlayr/`. It is retained during the
migration so behavior can be compared against the Rust implementation before
components are removed.
