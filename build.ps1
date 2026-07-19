# Build script for Overlayr

Write-Host "Building Overlayr..."

# Activate virtual environment if it exists
if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

# Clean previous builds
if (Test-Path "dist") { Remove-Item -Path "dist" -Recurse -Force }
if (Test-Path "build") { Remove-Item -Path "build" -Recurse -Force }

# Run PyInstaller
# --onefile: Create a single exe
# --noconsole: Don't show terminal window (GUI app)
# --name: Output filename
# --icon: Application icon
# --add-data: Include assets folder
# --collect-all: Collect all dependencies for these packages

pyinstaller `
    --noconsole `
    --onefile `
    --name "Overlayr" `
    --icon "icon.ico" `
    --paths "src" `
    --add-data "assets;assets" `
    --add-data "src;src" `
    --collect-all "rust_ocr" `
    --collect-all "symspellpy" `
    --collect-all "customtkinter" `
    --collect-all "cv2" `
    --collect-all "deep_translator" `
    --collect-all "PIL" `
    main.py

Write-Host "Build complete. Check dist/Overlayr.exe"
