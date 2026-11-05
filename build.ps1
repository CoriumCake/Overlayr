param(
    [switch]$Native,
    [string]$Target
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifest = Join-Path $projectRoot "rust/Cargo.toml"
$features = if ($Native) { "native-production" } else { $null }

Write-Host "Building Overlayr with Rust..."
$cargoArgs = @("build", "--release", "--manifest-path", $manifest)
if ($features) {
    $cargoArgs += @("--features", $features)
}
if ($Target) {
    $cargoArgs += @("--target", $Target)
}

& cargo @cargoArgs
if ($LASTEXITCODE -ne 0) {
    throw "Cargo release build failed with exit code $LASTEXITCODE"
}

$targetDirectory = if ($Target) {
    Join-Path $projectRoot "rust/target/$Target/release"
} else {
    Join-Path $projectRoot "rust/target/release"
}
$binaryName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "overlayr.exe" } else { "overlayr" }
$binary = Join-Path $targetDirectory $binaryName
$dist = Join-Path $projectRoot "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
Copy-Item -Force $binary (Join-Path $dist $binaryName)

Write-Host "Build complete: $dist/$binaryName"
