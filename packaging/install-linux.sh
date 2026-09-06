#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cargo build --release --manifest-path "$project_dir/rust/Cargo.toml"
install -Dm755 "$project_dir/rust/target/release/overlayr" "${HOME}/.local/bin/overlayr"
install -Dm644 "$project_dir/packaging/overlayr.desktop" "${HOME}/.local/share/applications/overlayr.desktop"
install -Dm644 "$project_dir/packaging/overlayr.svg" "${HOME}/.local/share/icons/hicolor/scalable/apps/overlayr.svg"
echo "Overlayr installed to ${HOME}/.local/bin/overlayr"
