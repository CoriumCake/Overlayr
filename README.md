# Overlayr

Overlayr is a Python-based screen translation tool that provides real-time Japanese to Thai translation overlay. It uses OCR (Optical Character Recognition) to extract text from a selected screen area and translates it using a translation service.

## Features

- **Screen Selection**: Select any area on your screen to monitor.
- **Real-time Mode**: Automatically detects changes in the selected area and translates new text.
- **Manual Capture**: Manually trigger capture and translation.
- **Compact Mode**: Shows a transparent overlay with the translated text near the source.
- **Overlay**: Displays translated text in a non-intrusive overlay window.

## Requirements

- Python 3.x
- Tesseract OCR (referenced by `ocr_engine.py`, make sure it's installed or configured if using a wrapper like `pytesseract` or `easyocr`)
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

2.  **Select Area**: Click "Select Area" and drag to draw a box around the Japanese text you want to translate.
3.  **Capture & Translate**: Click to translate the current content of the selection.
4.  **Real-time Mode**: Check this box to enable continuous monitoring and automatic translation when text changes.
5.  **Compact Mode**: Minimizes the main window and shows a floating overlay near the text.

## Structure

- `main.py`: Main application entry point and GUI logic (Tkinter).
- `ocr_engine.py`: Handles OCR processing (likely using EasyOCR or Tesseract).
- `translator.py`: Handles text translation (Japanese to Thai).
- `image_utils.py`: Utilities for image comparison (for detecting changes).
- `convert_icon.py`: Helper script for icon management.

## Building

To build the executable (using PyInstaller), run:

```bash
pyinstaller ScreenTranslator.spec
```
