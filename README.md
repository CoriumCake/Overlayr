# Overlayr

Overlayr is a game-aware Windows translation overlay. It uses local OCR to
extract text from a selected game or screen region, translates it in real time,
and remembers settings and preferred terminology for each game.

## Features

- **Screen Selection**: Select any area on your screen to monitor.
- **Real-time Mode**: Automatically detects changes in the selected area and translates new text.
- **Manual Capture**: Manually trigger capture and translation.
- **Compact Mode**: Shows a transparent overlay with the translated text near the source.
- **Overlay**: Displays translated text in a non-intrusive overlay window.
- **Game Profiles**: Remembers language pairs, selected regions, and corrections
  by game executable.
- **Teach Translation**: Save a preferred item, character, or quest translation
  and reuse it immediately for that game.
- **Privacy-Safe Metrics**: Stores activation and usage counts locally without
  OCR text, translations, screenshots, or window titles.

## Requirements

- Python 3.x
- See `requirements.txt` for Python dependencies.

## Installation

1.  Clone the repository:
    ```bash
    git clone <repository_url>
    cd Overlayr
    ```

2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

3.  On Windows, enable GPU OCR through DirectML after the normal dependency
    install:
    ```powershell
    .\scripts\install_gpu_runtime.ps1
    ```
    Overlayr detects `CUDAExecutionProvider` or `DmlExecutionProvider`
    automatically and falls back to CPU when neither is installed.

## Usage

1.  Run the application:
    ```bash
    python main.py
    ```

2.  **Select Area**: Select a screen/application, then press `Alt+T` (or click
    "Select Area") and drag a box around the text to translate. Press `Esc` or
    right-click to cancel; use "Clear Area" to return to the full source.
3.  **Capture & Translate**: Click to translate the current content of the selection.
4.  **Real-time Mode**: Check this box to enable continuous monitoring and automatic translation when text changes.
5.  **Compact Mode**: Minimizes the main window and shows a floating overlay near the text.
6.  **Teach Translation**: After a translation is visible, select an application
    source and click `Teach Translation` to save preferred wording to its game
    profile.

## Structure

- `main.py`: Main application entry point and GUI logic (Tkinter).
- `ocr_engine.py`: Handles OCR processing (likely using EasyOCR or Tesseract).
- `translator.py`: Handles text translation (Japanese to Thai).
- `image_utils.py`: Utilities for image comparison (for detecting changes).
- `src/overlayr/services/game_profiles.py`: Local per-game context and
  correction store.
- `src/overlayr/services/product_analytics.py`: Privacy-safe local product
  funnel events.

## Product metrics

Print the local funnel summary:

```powershell
.\.venv\Scripts\python.exe .\scripts\product_metrics.py
```

See `docs/PMF_90_DAY_PLAN.md` for the alpha, pricing, and distribution gates.

## Building

To build the executable (using PyInstaller), run:

```bash
pyinstaller Overlayr.spec
```
