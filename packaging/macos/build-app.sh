#!/bin/sh
# Builds Overlayr.app and Overlayr.dmg for macOS, as one binary for Apple
# Silicon and Intel Macs. Run on a Mac from anywhere in the repository:
#
#   packaging/macos/build-app.sh
#
# Needs the Rust targets aarch64-apple-darwin and x86_64-apple-darwin, and
# rsvg-convert (`brew install librsvg`) for the icon.
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

# An ad-hoc signature: Apple Silicon refuses to run unsigned code. Public
# releases need a Developer ID signature and notarization instead.
codesign --force --sign - "$app"

# A disk image with an Applications shortcut to drag the app onto.
staging="$out/dmg"
mkdir -p "$staging"
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"
hdiutil create -volname Overlayr -srcfolder "$staging" -ov -format UDZO "$out/Overlayr.dmg"
rm -rf "$staging"

echo "Built $app and $out/Overlayr.dmg"
