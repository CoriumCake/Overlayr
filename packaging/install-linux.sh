#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
build_args=(build --release --manifest-path "$project_dir/rust/Cargo.toml")
if [[ "${OVERLAYR_NATIVE_BUILD:-1}" == "1" ]]; then
    build_args+=(--features native-production)
fi
cargo "${build_args[@]}"
install -Dm755 "$project_dir/rust/target/release/overlayr" "${HOME}/.local/bin/overlayr"
install -Dm644 "$project_dir/packaging/overlayr.desktop" "${HOME}/.local/share/applications/overlayr.desktop"
install -Dm644 "$project_dir/packaging/overlayr.svg" "${HOME}/.local/share/icons/hicolor/scalable/apps/overlayr.svg"
if [[ "${OVERLAYR_NATIVE_BUILD:-1}" == "1" ]]; then
    echo "Overlayr installed with native capture and OCR"
else
    echo "Overlayr installed with desktop fallback capture and CLI OCR"
fi
echo "Overlayr installed to ${HOME}/.local/bin/overlayr"
