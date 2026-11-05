#!/usr/bin/env bash
# Installs a downloaded Overlayr release for the current user. Run it from
# the unpacked archive:
#
#   tar -xzf Overlayr-<version>-linux-x86_64.tar.gz
#   cd Overlayr-<version>-linux-x86_64 && ./install.sh
#
# To build from source instead, use packaging/install-linux.sh.
set -euo pipefail

here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
install -Dm755 "$here/overlayr" "${HOME}/.local/bin/overlayr"
install -Dm644 "$here/overlayr.desktop" "${HOME}/.local/share/applications/overlayr.desktop"
install -Dm644 "$here/overlayr.svg" "${HOME}/.local/share/icons/hicolor/scalable/apps/overlayr.svg"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${HOME}/.local/share/applications" || true
fi
if command -v xdg-mime >/dev/null 2>&1; then
    xdg-mime default overlayr.desktop x-scheme-handler/overlayr || true
fi
echo "Overlayr installed to ${HOME}/.local/bin/overlayr"
case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *) echo "Add ${HOME}/.local/bin to your PATH to run 'overlayr' from a terminal." ;;
esac
for tool in grim slurp tesseract; do
    command -v "$tool" >/dev/null 2>&1 || echo "Optional: install '$tool' (screen capture and OCR; see the setup checks)."
done
