param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$runtimeVersion = "1.24.4"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable not found: $Python"
}

# rapidocr-onnxruntime declares a dependency on the CPU package name. Install
# the matching CPU metadata first, then make DirectML the final wheel writing
# the shared onnxruntime module files.
& $Python -m pip install "onnxruntime==$runtimeVersion"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $Python -m pip install --force-reinstall "onnxruntime-directml==$runtimeVersion"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $Python -c "import onnxruntime as ort; providers=ort.get_available_providers(); print('ONNX Runtime providers:', providers); raise SystemExit(0 if 'DmlExecutionProvider' in providers else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "DirectML installed, but DmlExecutionProvider is unavailable."
}
