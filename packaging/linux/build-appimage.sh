#!/usr/bin/env bash
# Builds Overlayr-<version>-linux-x86_64.AppImage from a built program:
#
#   packaging/linux/build-appimage.sh rust/target/release/overlayr 0.2.0 dist
#
# The program loads the windowing libraries from the system at run time, so
# the AppImage holds only the program, its desktop entry and icon. It runs
# without FUSE 2 (appimagetool's static runtime) and updates itself in place
# (rust/src/updater.rs).
set -euo pipefail

program="$1"
version="$2"
output="$(mkdir -p "$3" && CDPATH= cd -- "$3" && pwd)"
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
packaging="$(dirname -- "$here")"

# A fixed appimagetool, checked, instead of the moving "continuous" build.
tool_version=1.9.1
tool_sha256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tool="${APPIMAGETOOL:-}"
if [[ -z "$tool" ]]; then
    tool="$work/appimagetool"
    curl -fsSLo "$tool" \
        "https://github.com/AppImage/appimagetool/releases/download/$tool_version/appimagetool-x86_64.AppImage"
    echo "$tool_sha256  $tool" | sha256sum -c --quiet
    chmod +x "$tool"
fi

appdir="$work/Overlayr.AppDir"
install -Dm755 "$program" "$appdir/usr/bin/overlayr"
install -Dm644 "$packaging/overlayr.desktop" "$appdir/overlayr.desktop"
install -Dm644 "$packaging/overlayr.svg" "$appdir/overlayr.svg"
install -Dm644 "$packaging/overlayr.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/overlayr.svg"
ln -s overlayr.svg "$appdir/.DirIcon"
ln -s usr/bin/overlayr "$appdir/AppRun"

# Extract-and-run, since CI machines have no FUSE for appimagetool itself.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$tool" --no-appstream "$appdir" \
    "$output/Overlayr-$version-linux-x86_64.AppImage"
