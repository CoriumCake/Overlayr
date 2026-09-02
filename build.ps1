# Build script for Overlayr

Write-Host "Building Overlayr..."

# Activate virtual environment if it exists
if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

# The detector has to travel with the executable. Without it the packaged app
# still starts and still reads text, but every comic page silently falls back
# to contour bubble detection and reads far worse than the same code run from
# source - the kind of failure that shows up in a user's screenshots and
# nowhere in the test suite (CLAUDE.md 9.31).
foreach ($required in @(
    "Model\Detec_Manga-English_lite.onnx",
    "Model\Rec_PPOCRv5.onnx",
    "Model\Rec_PPOCRv5_dict.txt")) {
    if (-not (Test-Path $required)) {
        Write-Error "$required is missing. The build would ship without it and degrade in silence."
        exit 1
    }
}

# Clean previous builds
if (Test-Path "dist") { Remove-Item -Path "dist" -Recurse -Force }
if (Test-Path "build") { Remove-Item -Path "build" -Recurse -Force }

# Run PyInstaller
# --onefile: Create a single exe
# --noconsole: Don't show terminal window (GUI app)
# --name: Output filename
# --icon: Application icon
# --paths: Where the overlayr package lives
# --add-data: Include assets and the detector model
#
# Only the lite detector is shipped. The larger model stays in the repository
# as the entry the code falls back to, which is a rollback path for measuring
# locally rather than something a user's build has any use for - carrying it
# would add 66MB to the executable for a file nothing would ever open.
# --collect-all: Collect all dependencies for these packages
#
# --paths "src" is what makes this build contain the application at all. The
# project is not pip-installed; main.py puts src/ on sys.path at runtime, which
# PyInstaller cannot follow because it reads the import graph rather than
# running the program. Without it the analysis stopped at the first
# `from overlayr...` and produced an executable with no cv2, no onnxruntime and
# no winsdk in it - one that starts and then fails at everything it exists to do.
#
# Given a resolvable import graph, neither onnxruntime nor winsdk needs a
# --collect-all: a bundled hook already gathers the provider DLLs out of
# onnxruntime/capi, and winsdk's namespaces are now reached by ordinary imports.

pyinstaller `
    --noconsole `
    --onefile `
    --name "Overlayr" `
    --icon "icon.ico" `
    --paths "src" `
    --add-data "assets;assets" `
    --add-data "Model\Detec_Manga-English_lite.onnx;Model" `
    --add-data "Model\Rec_PPOCRv5.onnx;Model" `
    --add-data "Model\Rec_PPOCRv5_dict.txt;Model" `
    --collect-all "symspellpy" `
    --collect-all "customtkinter" `
    main.py

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

Write-Host "Build complete. Check dist/Overlayr.exe"
