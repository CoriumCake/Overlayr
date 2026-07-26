param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$runtimeVersion = "1.24.4"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable not found: $Python"
}

# RapidOCR v3 uses the common onnxruntime module without requiring the legacy
# CPU wheel. Install DirectML last so its provider binaries are the active ones.
& $Python -m pip install --force-reinstall "onnxruntime-directml==$runtimeVersion"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $Python -c "import onnxruntime as ort; providers=ort.get_available_providers(); print('ONNX Runtime providers:', providers); raise SystemExit(0 if 'DmlExecutionProvider' in providers else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "DirectML installed, but DmlExecutionProvider is unavailable."
}
