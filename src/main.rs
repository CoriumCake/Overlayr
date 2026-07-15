#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")] // Hide console window in release builds

mod capture;
mod ocr;
mod translation;
mod source_manager;

use std::io::{self, BufRead};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use serde::{Deserialize, Serialize};
use serde_json::json;

use capture::WgcCapture;
use ocr::RustOCREngine;
use translation::Translator;
use windows::Win32::Foundation::HWND;
use windows::Win32::Graphics::Gdi::HMONITOR;

pub static mut GLOBAL_CAPTURING: bool = false;

#[derive(Serialize, Deserialize, Debug, Clone)]
struct RoiRect {
    x: f32,
    y: f32,
    w: f32,
    h: f32,
}

#[derive(Deserialize, Debug)]
#[serde(tag = "action")]
enum StdinCommand {
    #[serde(rename = "start")]
    Start {
        source_type: String,
        source_id: isize,
        roi: Option<RoiRect>,
        src_lang: String,
        tgt_lang: String,
    },
    #[serde(rename = "stop")]
    Stop,
    #[serde(rename = "exit")]
    Exit,
}

#[derive(Serialize, Clone, Debug)]
struct TranslationResult {
    bbox: Vec<Vec<i32>>,
    text: String,
    translated: String,
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 && args[1] == "--backend" {
        run_backend();
    } else {
        launch_gui();
    }
}

fn launch_gui() {
    let exe_dir = match std::env::current_exe() {
        Ok(path) => match path.parent() {
            Some(parent) => parent.to_path_buf(),
            None => std::path::PathBuf::from("."),
        },
        Err(_) => std::path::PathBuf::from("."),
    };

    let log_path = exe_dir.join("launcher_debug.log");
    let mut log_file = std::fs::File::create(&log_path).ok();
    if let Some(ref mut f) = log_file {
        use std::io::Write;
        let _ = writeln!(f, "exe_dir: {:?}", exe_dir);
    }

    let python_path = exe_dir.join(".venv/Scripts/pythonw.exe");
    let alt_path = exe_dir.join(".venv/Scripts/python.exe");
    let main_py = exe_dir.join("main.py");
    
    if let Some(ref mut f) = log_file {
        use std::io::Write;
        let _ = writeln!(f, "python_path exists: {}", python_path.exists());
        let _ = writeln!(f, "alt_path exists: {}", alt_path.exists());
        let _ = writeln!(f, "main_py exists: {}", main_py.exists());
    }
    
    if python_path.exists() && main_py.exists() {
        let spawn_res = std::process::Command::new(&python_path)
            .current_dir(&exe_dir)
            .arg("main.py")
            .spawn();
        if let Some(ref mut f) = log_file {
            use std::io::Write;
            let _ = writeln!(f, "spawn pythonw result: {:?}", spawn_res);
        }
    } else if alt_path.exists() && main_py.exists() {
        let spawn_res = std::process::Command::new(&alt_path)
            .current_dir(&exe_dir)
            .arg("main.py")
            .spawn();
        if let Some(ref mut f) = log_file {
            use std::io::Write;
            let _ = writeln!(f, "spawn python result: {:?}", spawn_res);
        }
    } else {
        if let Some(ref mut f) = log_file {
            use std::io::Write;
            let _ = writeln!(f, "No valid Python/main.py configuration found.");
        }
    }
}

fn run_backend() {
    // Disable stdout buffering to ensure Python receives JSON lines instantly
    use std::io::Write;
    let mut stdout = io::stdout();
    let _ = stdout.flush();

    println!("{}", json!({"status": "loading", "message": "Loading OCR models..."}));

    let det_path = std::path::Path::new("assets/models/Detec_Manga-English.onnx");
    let rec_path = std::path::Path::new("assets/models/PaddleOCR.onnx");
    let dict_path = std::path::Path::new("assets/models/en_dict.txt");

    let ocr_engine = match RustOCREngine::new(det_path, rec_path, dict_path) {
        Ok(engine) => {
            let shared = Arc::new(Mutex::new(engine));
            println!("{}", json!({"status": "ready", "message": "OCR models loaded successfully."}));
            shared
        }
        Err(e) => {
            println!("{}", json!({"status": "error", "message": format!("Failed to load OCR models: {:?}", e)}));
            std::process::exit(1);
        }
    };

    let stdin = io::stdin();
    let mut capture_thread_handle: Option<thread::JoinHandle<()>> = None;

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };

        let cmd: StdinCommand = match serde_json::from_str(&line) {
            Ok(c) => c,
            Err(e) => {
                println!("{}", json!({"status": "error", "message": format!("Invalid JSON command: {}", e)}));
                continue;
            }
        };

        match cmd {
            StdinCommand::Start {
                source_type,
                source_id,
                roi,
                src_lang,
                tgt_lang,
            } => {
                // Stop any existing capture first
                unsafe {
                    GLOBAL_CAPTURING = false;
                }
                if let Some(handle) = capture_thread_handle.take() {
                    let _ = handle.join();
                }

                // Start new capture thread
                unsafe {
                    GLOBAL_CAPTURING = true;
                }

                let ocr_clone = Arc::clone(&ocr_engine);
                let thread_handle = thread::spawn(move || {
                    run_capture_loop(source_type, source_id, roi, src_lang, tgt_lang, ocr_clone);
                });
                capture_thread_handle = Some(thread_handle);
            }
            StdinCommand::Stop => {
                unsafe {
                    GLOBAL_CAPTURING = false;
                }
                if let Some(handle) = capture_thread_handle.take() {
                    let _ = handle.join();
                }
                println!("{}", json!({"status": "info", "message": "Capture stopped."}));
            }
            StdinCommand::Exit => {
                unsafe {
                    GLOBAL_CAPTURING = false;
                }
                if let Some(handle) = capture_thread_handle.take() {
                    let _ = handle.join();
                }
                println!("{}", json!({"status": "info", "message": "Exiting backend."}));
                break;
            }
        }
    }
}

fn run_capture_loop(
    source_type: String,
    source_id: isize,
    roi: Option<RoiRect>,
    src_lang: String,
    tgt_lang: String,
    ocr_engine: Arc<Mutex<RustOCREngine>>,
) {
    // Initialize WinRT COM on this thread
    unsafe {
        let _ = windows::Win32::System::WinRT::RoInitialize(
            windows::Win32::System::WinRT::RO_INIT_TYPE(1) // RO_INIT_MULTITHREADED
        );
    }

    let mut capture = match WgcCapture::new() {
        Ok(c) => c,
        Err(e) => {
            println!("{}", json!({"status": "error", "message": format!("Capture Init Fail: {:?}", e)}));
            return;
        }
    };

    let start_res = if source_type == "screen" {
        let hmonitor = HMONITOR(source_id);
        capture.start_monitor(hmonitor)
    } else {
        let hwnd = HWND(source_id);
        capture.start_window(hwnd)
    };

    if let Err(e) = start_res {
        println!("{}", json!({"status": "error", "message": format!("Capture Start Fail: {:?}", e)}));
        return;
    }

    println!("{}", json!({"status": "info", "message": "Streaming..."}));

    let mut translator = Translator::new(&src_lang, &tgt_lang);
    let mut prev_frame: Option<image::DynamicImage> = None;
    let mut last_ocr_time = std::time::Instant::now();

    while unsafe { GLOBAL_CAPTURING } {
        let frame = match capture.get_latest_frame() {
            Ok(Some(f)) => f,
            Ok(None) => {
                thread::sleep(Duration::from_millis(16));
                continue;
            }
            Err(e) => {
                println!("{}", json!({"status": "error", "message": format!("Frame Grab Error: {:?}", e)}));
                break;
            }
        };

        // Perform crop if ROI is set
        let process_img = if let Some(ref rect) = roi {
            let x = rect.x.max(0.0) as u32;
            let y = rect.y.max(0.0) as u32;
            let w = rect.w as u32;
            let h = rect.h as u32;
            if x + w <= frame.width() && y + h <= frame.height() {
                image::DynamicImage::ImageRgba8(frame).crop_imm(x, y, w, h)
            } else {
                image::DynamicImage::ImageRgba8(frame)
            }
        } else {
            image::DynamicImage::ImageRgba8(frame)
        };

        // Smart frame diffing optimization: Skip OCR & translation if frame is static
        let mut is_similar = false;
        if let Some(ref prev) = prev_frame {
            if prev.width() == process_img.width() && prev.height() == process_img.height() {
                let mut diff = 0u64;
                let step_x = (process_img.width() / 8).max(1);
                let step_y = (process_img.height() / 8).max(1);
                for y in (0..process_img.height()).step_by(step_y as usize) {
                    for x in (0..process_img.width()).step_by(step_x as usize) {
                        use image::GenericImageView;
                        let p1 = process_img.get_pixel(x, y);
                        let p2 = prev.get_pixel(x, y);
                        diff += ((p1[0] as i32 - p2[0] as i32).abs() +
                                 (p1[1] as i32 - p2[1] as i32).abs() +
                                 (p1[2] as i32 - p2[2] as i32).abs()) as u64;
                    }
                }
                if diff < 120 {
                    is_similar = true;
                }
            }
        }

        if is_similar && last_ocr_time.elapsed() < Duration::from_millis(1500) {
            thread::sleep(Duration::from_millis(150));
            continue;
        }
        prev_frame = Some(process_img.clone());
        last_ocr_time = std::time::Instant::now();

        // Run OCR and translation
        let results = {
            let mut ocr = ocr_engine.lock().unwrap();
            match ocr.extract_text(&process_img) {
                Ok(res) => res,
                Err(e) => {
                    println!("{}", json!({"status": "error", "message": format!("OCR Error: {:?}", e)}));
                    continue;
                }
            }
        };

        let mut final_results = Vec::new();
        for (bbox, text, _conf) in results {
            if text.trim().is_empty() {
                continue;
            }
            let translated = translator.translate(&text);
            final_results.push(TranslationResult {
                bbox,
                text,
                translated,
            });
        }

        println!("{}", json!({
            "status": "results",
            "results": final_results
        }));
    }
    
    let _ = capture.stop();
}
