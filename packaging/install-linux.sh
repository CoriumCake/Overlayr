#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
build_args=(build --release --manifest-path "$project_dir/rust/Cargo.toml")
# In-process OCR keeps the model loaded between reads. Capture stays on the
# Wayland/Hyprland backend (window picker, background capture, overlay
# placement), which the native XCap capture would replace.
if [[ "${OVERLAYR_NATIVE_BUILD:-1}" == "1" ]]; then
    build_args+=(--features native-ocr)
fi
cargo "${build_args[@]}"
install -Dm755 "$project_dir/rust/target/release/overlayr" "${HOME}/.local/bin/overlayr"
install -Dm644 "$project_dir/packaging/overlayr.desktop" "${HOME}/.local/share/applications/overlayr.desktop"
install -Dm644 "$project_dir/packaging/overlayr.svg" "${HOME}/.local/share/icons/hicolor/scalable/apps/overlayr.svg"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${HOME}/.local/share/applications" || true
fi
if command -v xdg-mime >/dev/null 2>&1; then
    xdg-mime default overlayr.desktop x-scheme-handler/overlayr || true
fi
if [[ "${OVERLAYR_NATIVE_BUILD:-1}" == "1" ]]; then
    echo "Overlayr installed with in-process OCR"
else
    echo "Overlayr installed with CLI OCR"
fi
echo "Overlayr installed to ${HOME}/.local/bin/overlayr"
