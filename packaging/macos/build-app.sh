#!/bin/sh
# Builds Overlayr.app and Overlayr.dmg for macOS, as one binary for Apple
# Silicon and Intel Macs. Run on a Mac from anywhere in the repository:
#
#   packaging/macos/build-app.sh
#
# Needs an Apple Silicon Mac with Homebrew, the Rust targets
# aarch64-apple-darwin and x86_64-apple-darwin, and rsvg-convert
# (`brew install librsvg`) for the icon.
#
# Set OVERLAYR_INTEL_OCR to a folder made on an Intel Mac by
# `packaging/fetch-ocr.sh macos-intel` to include Tesseract for Intel Macs.
#
# Set OVERLAYR_SIGN_IDENTITY to a Developer ID Application identity in the
# keychain ("Developer ID Application: Name (TEAMID)") to sign for release;
# without it the app gets an ad-hoc signature, which macOS warns about.
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
out="$root/rust/target/macos"
version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/rust/Cargo.toml" | head -n 1)

cd "$root/rust"
for target in aarch64-apple-darwin x86_64-apple-darwin; do
    cargo build --release --features native-capture --target "$target"
done

app="$out/Overlayr.app"
rm -rf "$out"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
lipo -create -output "$app/Contents/MacOS/overlayr" \
    target/aarch64-apple-darwin/release/overlayr \
    target/x86_64-apple-darwin/release/overlayr
sed "s/@VERSION@/$version/g" "$root/packaging/macos/Info.plist" >"$app/Contents/Info.plist"
plutil -lint "$app/Contents/Info.plist"

# Tesseract and the neural OCR engine, so text reading needs no installs.
"$root/packaging/fetch-ocr.sh" macos "$app/Contents/Resources/ocr"
if [ -n "${OVERLAYR_INTEL_OCR:-}" ]; then
    cp -R "$OVERLAYR_INTEL_OCR" "$app/Contents/Resources/ocr/x86_64"
    test -x "$app/Contents/Resources/ocr/x86_64/tesseract/tesseract"
fi

# The icon, rendered at every size macOS uses.
if command -v rsvg-convert >/dev/null 2>&1; then
    iconset="$out/overlayr.iconset"
    mkdir -p "$iconset"
    for size in 16 32 128 256 512; do
        rsvg-convert -w "$size" -h "$size" "$root/packaging/overlayr.svg" \
            -o "$iconset/icon_${size}x${size}.png"
        rsvg-convert -w $((size * 2)) -h $((size * 2)) "$root/packaging/overlayr.svg" \
            -o "$iconset/icon_${size}x${size}@2x.png"
    done
    iconutil -c icns "$iconset" -o "$app/Contents/Resources/overlayr.icns"
    rm -rf "$iconset"
else
    echo "rsvg-convert not found: building without an icon (brew install librsvg)" >&2
fi

if [ -n "${OVERLAYR_SIGN_IDENTITY:-}" ]; then
    # Every program and library inside first, then the app, with the
    # hardened runtime notarization requires.
    find "$app/Contents/Resources/ocr" -type f \( -name '*.dylib' -o -perm -u+x \) |
        while read -r file; do
            codesign --force --timestamp --options runtime --sign "$OVERLAYR_SIGN_IDENTITY" "$file"
        done
    codesign --force --timestamp --options runtime --sign "$OVERLAYR_SIGN_IDENTITY" "$app"
    codesign --verify --strict --deep --verbose=2 "$app"
else
    # An ad-hoc signature: Apple Silicon refuses to run unsigned code.
    codesign --force --sign - "$app"
fi

# A disk image with an Applications shortcut to drag the app onto.
staging="$out/dmg"
mkdir -p "$staging"
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"
hdiutil create -volname Overlayr -srcfolder "$staging" -ov -format UDZO "$out/Overlayr.dmg"
rm -rf "$staging"
if [ -n "${OVERLAYR_SIGN_IDENTITY:-}" ]; then
    codesign --force --timestamp --sign "$OVERLAYR_SIGN_IDENTITY" "$out/Overlayr.dmg"
fi

echo "Built $app and $out/Overlayr.dmg"
