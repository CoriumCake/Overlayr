# Build script for Overlayr

Write-Host "Building Overlayr..."

# Clean previous builds
if (Test-Path "dist") { Remove-Item -Path "dist" -Recurse -Force }
if (Test-Path ".venv\target_build") { Remove-Item -Path ".venv\target_build" -Recurse -Force }

# Run Cargo release build
cargo build --release --target-dir .venv\target_build -j 2

# Copy build to dist/
New-Item -ItemType Directory -Path "dist" -Force | Out-Null
Copy-Item -Path ".venv\target_build\release\overlayr.exe" -Destination "dist\Overlayr.exe" -Force

Write-Host "Build complete. Check dist/Overlayr.exe"
