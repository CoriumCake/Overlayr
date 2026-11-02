#!/bin/bash
# Gathers the OCR programs and models the installers ship, so Overlayr reads
# text straight after installing (see rust/src/bundle.rs for the layout):
#
#   packaging/fetch-ocr.sh windows <dest>   # on Windows, in Git Bash
#   packaging/fetch-ocr.sh macos <dest>     # on an Apple Silicon Mac
#
# <dest> becomes the `ocr` folder: ONNX Runtime and the PaddleOCR models for
# the neural engine, plus Tesseract for languages those models can't read.
# Tesseract's language models aren't included; the app downloads the one a
# game needs.
#
# Keep the versions in step with rust/src/installer.rs.
set -euo pipefail

ONNX_RUNTIME_VERSION=1.31.0
NEURAL_MODEL_URL=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv6
TESSERACT_WINDOWS=https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe

platform=$1
dest=$2
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$dest/tesseract"

fetch() {
    curl --fail --location --retry 3 --silent --show-error --output "$2" "$1"
}

# The neural engine's models.
for kind in det rec; do
    fetch "$NEURAL_MODEL_URL/$kind/PP-OCRv6_${kind}_small.onnx" "$dest/PP-OCRv6_${kind}_small.onnx"
done

case "$platform" in
windows)
    name="onnxruntime-win-x64-$ONNX_RUNTIME_VERSION"
    fetch "https://github.com/microsoft/onnxruntime/releases/download/v$ONNX_RUNTIME_VERSION/$name.zip" "$work/onnx.zip"
    7z e -y -o"$dest" "$work/onnx.zip" "$name/lib/onnxruntime.dll" >/dev/null

    # UB Mannheim's Tesseract build, unpacked from its installer: the
    # program and the libraries next to it.
    fetch "$TESSERACT_WINDOWS" "$work/tesseract-setup.exe"
    7z x -y -o"$work/tesseract" "$work/tesseract-setup.exe" >/dev/null
    cp "$work/tesseract/tesseract.exe" "$work/tesseract/"*.dll "$dest/tesseract/"
    "$dest/tesseract/tesseract.exe" --version
    ;;
macos)
    if [ "$(uname -m)" != arm64 ]; then
        echo "Run this on an Apple Silicon Mac: the bundle is for Apple Silicon" >&2
        exit 1
    fi
    name="onnxruntime-osx-arm64-$ONNX_RUNTIME_VERSION"
    fetch "https://github.com/microsoft/onnxruntime/releases/download/v$ONNX_RUNTIME_VERSION/$name.tgz" "$work/onnx.tgz"
    tar -xzf "$work/onnx.tgz" -C "$work"
    cp "$work/$name/lib/libonnxruntime.$ONNX_RUNTIME_VERSION.dylib" "$dest/libonnxruntime.dylib"

    # Homebrew's Tesseract, with every library it links copied next to it
    # and its references rewritten to point there.
    brew list tesseract >/dev/null 2>&1 || brew install tesseract
    brew list dylibbundler >/dev/null 2>&1 || brew install dylibbundler
    cp -L "$(brew --prefix)/bin/tesseract" "$dest/tesseract/tesseract"
    chmod u+w "$dest/tesseract/tesseract"
    dylibbundler --overwrite-dir --bundle-deps --create-dir \
        --fix-file "$dest/tesseract/tesseract" \
        --dest-dir "$dest/tesseract/libs" \
        --install-path @executable_path/libs/
    # Rewriting invalidates signatures, and Apple Silicon won't run unsigned
    # code: sign each piece ad hoc before the app itself is signed.
    codesign --force --sign - "$dest/tesseract/libs/"*.dylib "$dest/tesseract/tesseract" \
        "$dest/libonnxruntime.dylib"
    # Proves no library still points into Homebrew.
    if otool -L "$dest/tesseract/tesseract" "$dest/tesseract/libs/"*.dylib | grep -E '/(opt/homebrew|usr/local)/'; then
        echo "Tesseract still links Homebrew libraries" >&2
        exit 1
    fi
    "$dest/tesseract/tesseract" --version
    ;;
*)
    echo "usage: $0 windows|macos <dest>" >&2
    exit 2
    ;;
esac

echo "OCR bundle in $dest:"
du -sh "$dest"
