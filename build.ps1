# Build script for Screen Translator

Write-Host "Building Screen Translator..."

# Clean previous builds
if (Test-Path "dist") { Remove-Item -Path "dist" -Recurse -Force }
if (Test-Path "build") { Remove-Item -Path "build" -Recurse -Force }

# Run PyInstaller
# --onefile: Create a single exe
# --noconsole: Don't show terminal window (GUI app)
# --name: Output filename
# --collect-all easyocr: Collects all data/dependencies for easyocr (important!)

& "C:\Users\Kaotok\AppData\Roaming\Python\Python314\Scripts\pyinstaller.exe" --noconsole --onefile --name "ScreenTranslator" --icon "Icon.ico" --add-data "Icon.png;." --collect-all "easyocr" main.py

Write-Host "Build complete. Check dist/ScreenTranslator.exe"
