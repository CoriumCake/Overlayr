use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use crate::capture::WgcCapture;
use crate::source_manager::{list_monitors, list_windows, SourceInfo};
use crate::ocr::RustOCREngine;
use crate::translation::Translator;
use crate::gui::overlay::{OverlayState, TranslationResult};
use crate::gui::roi_selector::RoiSelectorState;
use crate::gui::theme::{get_accent_color, get_panel_bg_color};

pub struct MainWindow {
    // Source lists
    monitors: Vec<SourceInfo>,
    windows: Vec<SourceInfo>,
    selected_source_id: Option<isize>,
    selected_source_type: String, // "screen" | "window"
    
    // UI state
    source_lang_idx: usize,
    target_lang_idx: usize,
    grouping_mode_idx: usize,
    status: String,
    
    // ROI selector & Overlay state
    roi_selector: RoiSelectorState,
    overlay: Arc<Mutex<OverlayState>>,
    
    // OCR & Translation core
    ocr_engine: Option<Arc<Mutex<RustOCREngine>>>,
    is_loading_models: bool,
    is_capturing: bool,
    capture_thread: Option<thread::JoinHandle<()>>,
    status_rx: Option<std::sync::mpsc::Receiver<String>>,
    
    // Cached window icon textures
    icon_textures: std::collections::HashMap<isize, egui::TextureHandle>,
}

const SOURCE_LANGS: &[(&str, &str)] = &[
    ("Japanese", "ja"),
    ("English", "en"),
    ("Chinese", "zh-CN"),
    ("Korean", "ko"),
];

const TARGET_LANGS: &[(&str, &str)] = &[
    ("Thai", "th"),
    ("English", "en"),
];

const GROUP_MODES: &[&str] = &[
    "Group (Manga)",
    "Document (Lines)",
];

impl MainWindow {
    pub fn new(cc: &eframe::CreationContext<'_>) -> Self {
        crate::gui::theme::apply_theme(&cc.egui_ctx);
        
        let overlay = Arc::new(Mutex::new(OverlayState::default()));
        
        let app = Self {
            monitors: list_monitors(),
            windows: list_windows(),
            selected_source_id: None,
            selected_source_type: "screen".to_string(),
            source_lang_idx: 0,
            target_lang_idx: 0,
            grouping_mode_idx: 0,
            status: "Initializing...".to_string(),
            roi_selector: RoiSelectorState::default(),
            overlay,
            ocr_engine: None,
            is_loading_models: true,
            is_capturing: false,
            capture_thread: None,
            status_rx: None,
            icon_textures: std::collections::HashMap::new(),
        };
        
        // Load OCR Models in background thread to prevent UI freezing
        let ctx = cc.egui_ctx.clone();
        thread::spawn(move || {
            let det_path = std::path::Path::new("assets/models/Detec_Manga-English.onnx");
            let rec_path = std::path::Path::new("assets/models/PaddleOCR.onnx");
            let dict_path = std::path::Path::new("assets/models/en_dict.txt");
            
            println!("Loading OCR models in background...");
            match RustOCREngine::new(det_path, rec_path, dict_path) {
                Ok(engine) => {
                    println!("OCR models loaded successfully.");
                    // We need a way to pass the engine to the app.
                    // We'll update a global lazy/mutex or let the app poll a channel or use Arc<Mutex>.
                    // For simplicity, we can use a thread-safe global pointer or save it to a lazy static.
                    // But wait, we can store it in a global thread-safe engine variable.
                    unsafe {
                        GLOBAL_OCR_ENGINE = Some(Arc::new(Mutex::new(engine)));
                        GLOBAL_OCR_LOADED = true;
                    }
                }
                Err(e) => {
                    eprintln!("Failed to load OCR models: {:?}", e);
                }
            }
            ctx.request_repaint();
        });
        
        app
    }

    fn start_capture_loop(&mut self, ctx: egui::Context) {
        if self.is_capturing {
            return;
        }

        let selected_id = match self.selected_source_id {
            Some(id) => id,
            None => {
                self.status = "Error: No capture source selected!".to_string();
                return;
            }
        };

        let source_type = self.selected_source_type.clone();
        let src_lang = SOURCE_LANGS[self.source_lang_idx].1.to_string();
        let tgt_lang = TARGET_LANGS[self.target_lang_idx].1.to_string();
        
        // Find selected source struct
        let source_info = if source_type == "screen" {
            self.monitors.iter().find(|m| m.id == selected_id).cloned()
        } else {
            self.windows.iter().find(|w| w.id == selected_id).cloned()
        };

        let source_info = match source_info {
            Some(info) => info,
            None => {
                self.status = "Error: Captured source not found!".to_string();
                return;
            }
        };

        // Prepare overlay window position/size
        let mut overlay_pos = egui::pos2(100.0, 100.0);
        let mut overlay_size = egui::vec2(800.0, 600.0);
        
        if let Some(rect) = self.roi_selector.final_rect {
            overlay_pos = rect.min;
            overlay_size = rect.size();
        } else {
            // Fullscreen or window size
            if source_type == "screen" {
                // Parse Display details or use monitor info
                if let Some(hmon) = source_info.hmonitor {
                    let mut info = windows::Win32::Graphics::Gdi::MONITORINFOEXW::default();
                    info.monitorInfo.cbSize = std::mem::size_of::<windows::Win32::Graphics::Gdi::MONITORINFOEXW>() as u32;
                    unsafe {
                        if windows::Win32::Graphics::Gdi::GetMonitorInfoW(hmon, &mut info as *mut _ as *mut _).as_bool() {
                            let rect = info.monitorInfo.rcMonitor;
                            overlay_pos = egui::pos2(rect.left as f32, rect.top as f32);
                            overlay_size = egui::vec2((rect.right - rect.left) as f32, (rect.bottom - rect.top) as f32);
                        }
                    }
                }
            } else {
                // Window
                if let Some(hwnd) = source_info.hwnd {
                    let mut rect = windows::Win32::Foundation::RECT::default();
                    unsafe {
                        let _ = windows::Win32::UI::WindowsAndMessaging::GetWindowRect(hwnd, &mut rect);
                    }
                    overlay_pos = egui::pos2(rect.left as f32, rect.top as f32);
                    overlay_size = egui::vec2((rect.right - rect.left) as f32, (rect.bottom - rect.top) as f32);
                }
            }
        }

        // Configure overlay
        {
            let mut overlay_state = self.overlay.lock().unwrap();
            overlay_state.position = overlay_pos;
            overlay_state.size = overlay_size;
            overlay_state.is_active = true;
            overlay_state.results.clear();
        }

        self.is_capturing = true;
        self.status = "Streaming...".to_string();

        let overlay_clone = Arc::clone(&self.overlay);
        let roi_rect = self.roi_selector.final_rect;
        
        let (tx, rx) = std::sync::mpsc::channel();
        self.status_rx = Some(rx);

        let tx_err = tx.clone();
        self.capture_thread = Some(thread::spawn(move || {
            // Initialize WinRT COM on the capture thread
            unsafe {
                let _ = windows::Win32::System::WinRT::RoInitialize(
                    windows::Win32::System::WinRT::RO_INIT_TYPE(1) // RO_INIT_MULTITHREADED = 1
                );
            }

            let mut capture = match WgcCapture::new() {
                Ok(c) => c,
                Err(e) => {
                    let _ = tx_err.send(format!("Error: Capture Init Fail: {:?}", e));
                    eprintln!("Capture initialization error: {:?}", e);
                    return;
                }
            };

            // Start capture on target source
            let start_res = if source_type == "screen" {
                if let Some(hmonitor) = source_info.hmonitor {
                    capture.start_monitor(hmonitor)
                } else {
                    Err(windows::core::Error::new(windows::Win32::Foundation::E_FAIL, "No HMONITOR".into()))
                }
            } else {
                if let Some(hwnd) = source_info.hwnd {
                    capture.start_window(hwnd)
                } else {
                    Err(windows::core::Error::new(windows::Win32::Foundation::E_FAIL, "No HWND".into()))
                }
            };

            if let Err(e) = start_res {
                let _ = tx_err.send(format!("Error: Capture Start Fail: {:?}", e));
                eprintln!("Failed to start capture source: {:?}", e);
                return;
            }

            let _ = tx_err.send("Streaming...".to_string());
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
                        let _ = tx_err.send(format!("Error: Frame Grab: {:?}", e));
                        eprintln!("Frame grab error: {:?}", e);
                        break;
                    }
                };

                // Perform crop if ROI is set
                let process_img = if let Some(rect) = roi_rect {
                    let x = rect.min.x.max(0.0) as u32;
                    let y = rect.min.y.max(0.0) as u32;
                    let w = rect.width() as u32;
                    let h = rect.height() as u32;
                    if x + w <= frame.width() && y + h <= frame.height() {
                        let crop_res = image::DynamicImage::ImageRgba8(frame).crop_imm(x, y, w, h);
                        crop_res
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
                        // Under a cumulative diff of 120 (e.g. slight background changes / noise), consider static
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
                let mut results = Vec::new();
                
                let ocr_locked = unsafe {
                    if let Some(ref engine) = GLOBAL_OCR_ENGINE {
                        Some(Arc::clone(engine))
                    } else {
                        None
                    }
                };

                if let Some(engine_arc) = ocr_locked {
                    let mut engine = engine_arc.lock().unwrap();
                    if let Ok(ocr_res) = engine.extract_text(&process_img) {
                        for (bbox, text, conf) in ocr_res {
                            if text.trim().is_empty() {
                                continue;
                            }
                            let translated = translator.translate(&text);
                            results.push(TranslationResult {
                                bbox,
                                text,
                                confidence: conf,
                                translated,
                            });
                        }
                    }
                }

                // Update overlay results
                {
                    let mut overlay_state = overlay_clone.lock().unwrap();
                    overlay_state.results = results;
                }

                ctx.request_repaint();
                thread::sleep(Duration::from_millis(60));
            }

            capture.stop();
            println!("Capture thread stopped cleanly.");
        }));
    }

    fn stop_capture(&mut self) {
        if !self.is_capturing {
            return;
        }

        unsafe {
            GLOBAL_CAPTURING = false;
        }

        if let Some(handle) = self.capture_thread.take() {
            let _ = handle.join();
        }

        self.is_capturing = false;
        self.status = "Ready".to_string();

        {
            let mut overlay_state = self.overlay.lock().unwrap();
            overlay_state.is_active = false;
            overlay_state.results.clear();
        }
    }
}

// Global shared variables for simple thread-safe worker access
pub static mut GLOBAL_OCR_ENGINE: Option<Arc<Mutex<RustOCREngine>>> = None;
pub static mut GLOBAL_OCR_LOADED: bool = false;
pub static mut GLOBAL_CAPTURING: bool = false;

impl eframe::App for MainWindow {
    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        [0.0, 0.0, 0.0, 0.0]
    }

    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // Receive background thread status updates
        if let Some(ref rx) = self.status_rx {
            while let Ok(msg) = rx.try_recv() {
                self.status = msg;
            }
        }

        // Dynamic state updates from loading thread
        if self.is_loading_models && unsafe { GLOBAL_OCR_LOADED } {
            self.is_loading_models = false;
            self.ocr_engine = unsafe { GLOBAL_OCR_ENGINE.clone() };
            self.status = "Ready".to_string();
        }

        // Render transparent selector viewport
        if self.roi_selector.is_active {
            if let Some(rect) = self.roi_selector.show(ctx) {
                self.roi_selector.final_rect = Some(rect);
                self.status = format!("Area defined: {}x{} at ({},{})", rect.width() as i32, rect.height() as i32, rect.min.x as i32, rect.min.y as i32);
            }
        }

        // Render transparent overlay viewport
        if self.is_capturing {
            self.overlay.lock().unwrap().show(ctx);
        }

        // Status bar locked to bottom
        egui::TopBottomPanel::bottom("status_bar")
            .resizable(false)
            .frame(egui::Frame::none()
                .fill(get_panel_bg_color())
                .inner_margin(egui::Margin::symmetric(12.0, 8.0))
                .stroke(egui::Stroke::new(1.0, egui::Color32::from_rgb(45, 45, 55)))
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label("Status:");
                    let text_color = if self.status.starts_with("Error") {
                        egui::Color32::from_rgb(230, 50, 50)
                    } else if self.status == "Streaming..." {
                        get_accent_color()
                    } else {
                        egui::Color32::LIGHT_GRAY
                    };
                    ui.label(egui::RichText::new(&self.status).color(text_color).strong());
                });
            });

        // Render main dashboard window
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.vertical_centered(|ui| {
                ui.add_space(8.0);
                ui.heading(
                    egui::RichText::new("Overlayr - Pro Translator")
                        .color(get_accent_color())
                        .size(24.0)
                        .strong()
                );
                ui.add_space(4.0);
            });

            ui.separator();

            // Language Selector and Grouping Settings Panel
            ui.group(|ui| {
                ui.horizontal(|ui| {
                    ui.label("Source Language:");
                    egui::ComboBox::from_id_source("src_lang")
                        .selected_text(SOURCE_LANGS[self.source_lang_idx].0)
                        .show_index(ui, &mut self.source_lang_idx, SOURCE_LANGS.len(), |idx| SOURCE_LANGS[idx].0);

                    ui.label("Target Language:");
                    egui::ComboBox::from_id_source("tgt_lang")
                        .selected_text(TARGET_LANGS[self.target_lang_idx].0)
                        .show_index(ui, &mut self.target_lang_idx, TARGET_LANGS.len(), |idx| TARGET_LANGS[idx].0);
                });

                ui.add_space(4.0);

                ui.horizontal(|ui| {
                    ui.label("Grouping Mode:");
                    egui::ComboBox::from_id_source("group_mode")
                        .selected_text(GROUP_MODES[self.grouping_mode_idx])
                        .show_index(ui, &mut self.grouping_mode_idx, GROUP_MODES.len(), |idx| GROUP_MODES[idx]);
                });
            });

            ui.add_space(6.0);

            // Tab View (Screens vs Windows)
            ui.horizontal(|ui| {
                if ui.selectable_label(self.selected_source_type == "screen", "📺 Screens").clicked() {
                    self.selected_source_type = "screen".to_string();
                    self.selected_source_id = None;
                }
                if ui.selectable_label(self.selected_source_type == "window", "🪟 Application Windows").clicked() {
                    self.selected_source_type = "window".to_string();
                    self.selected_source_id = None;
                }
            });

            // Source list scroll area
            ui.add_space(2.0);
            egui::Frame::none()
                .fill(get_panel_bg_color())
                .rounding(4.0)
                .show(ui, |ui| {
                    egui::ScrollArea::vertical().max_height(150.0).show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        let list = if self.selected_source_type == "screen" {
                            &self.monitors
                        } else {
                            &self.windows
                        };

                        if list.is_empty() {
                            ui.vertical_centered(|ui| {
                                ui.add_space(20.0);
                                ui.label(egui::RichText::new("No active sources found. Click refresh.").italics());
                                ui.add_space(20.0);
                            });
                        } else {
                            for item in list {
                                ui.horizontal(|ui| {
                                    ui.add_space(4.0);
                                    if self.selected_source_type == "window" {
                                        if let Some(ref rgba) = item.icon_rgba {
                                            let texture = self.icon_textures.entry(item.id).or_insert_with(|| {
                                                let color_image = egui::ColorImage::from_rgba_unmultiplied(
                                                    [item.icon_width as usize, item.icon_height as usize],
                                                    rgba,
                                                );
                                                ctx.load_texture(
                                                    format!("window_icon_{}", item.id),
                                                    color_image,
                                                    egui::TextureOptions::default(),
                                                )
                                            });
                                            ui.add(egui::Image::new(&*texture).max_width(16.0).max_height(16.0));
                                        } else {
                                            ui.label("🪟");
                                        }
                                    } else {
                                        ui.label("📺");
                                    }
                                    ui.add_space(4.0);
                                    
                                    let mut display_name = item.name.clone();
                                    if display_name.chars().count() > 40 {
                                        let truncated: String = display_name.chars().take(37).collect();
                                        display_name = format!("{}...", truncated);
                                    }

                                    ui.selectable_value(
                                        &mut self.selected_source_id,
                                        Some(item.id),
                                        format!("{} ({})", display_name, item.details),
                                    );
                                });
                            }
                        }
                    });
                });

            // Action Buttons Footer
            ui.add_space(10.0);
            ui.horizontal(|ui| {
                if ui.button("🔄 Refresh List").clicked() {
                    self.monitors = list_monitors();
                    self.windows = list_windows();
                    self.icon_textures.clear();
                }

                if ui.button("🎯 Define Area").clicked() {
                    self.roi_selector.final_rect = None;
                    self.roi_selector.is_active = true;
                }

                if let Some(rect) = self.roi_selector.final_rect {
                    if ui.button("❌ Clear Area").clicked() {
                        self.roi_selector.final_rect = None;
                        self.status = "Area cleared. Defaulting to full source capture.".to_string();
                    }
                }

                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if self.is_capturing {
                        let stop_btn = ui.add(
                            egui::Button::new(
                                egui::RichText::new("🛑 Stop Capture")
                                    .color(egui::Color32::WHITE)
                                    .strong()
                            )
                            .fill(egui::Color32::from_rgb(230, 50, 50))
                        );
                        if stop_btn.clicked() {
                            self.stop_capture();
                        }
                    } else {
                        let can_start = !self.is_loading_models && self.selected_source_id.is_some();
                        let start_btn = ui.add_enabled(
                            can_start,
                            egui::Button::new(
                                egui::RichText::new("🚀 Start Capture")
                                    .color(egui::Color32::WHITE)
                                    .strong()
                            )
                            .fill(get_accent_color())
                        );
                        if start_btn.clicked() {
                            unsafe {
                                GLOBAL_CAPTURING = true;
                            }
                            self.start_capture_loop(ctx.clone());
                        }
                    }
                });
            });

            if self.is_capturing {
                ui.add_space(8.0);
                ui.separator();
                ui.add_space(4.0);
                ui.label(egui::RichText::new("📖 Live Readings (Debug Window):").strong().color(get_accent_color()));
                ui.add_space(2.0);
                
                egui::Frame::none()
                    .fill(get_panel_bg_color())
                    .rounding(4.0)
                    .inner_margin(egui::Margin::same(6.0))
                    .show(ui, |ui| {
                        egui::ScrollArea::vertical().max_height(80.0).show(ui, |ui| {
                            ui.set_width(ui.available_width());
                            let results = {
                                let overlay = self.overlay.lock().unwrap();
                                overlay.results.clone()
                            };
                            if results.is_empty() {
                                ui.label(egui::RichText::new("Waiting for text detection...").italics());
                            } else {
                                for item in results {
                                    ui.horizontal_wrapped(|ui| {
                                        ui.label(egui::RichText::new("OCR:").strong().color(egui::Color32::LIGHT_BLUE));
                                        ui.label(&item.text);
                                        ui.label(egui::RichText::new("→").color(egui::Color32::GRAY));
                                        ui.label(egui::RichText::new(&item.translated).color(egui::Color32::WHITE));
                                    });
                                }
                            }
                        });
                    });
            }
        });
    }
}
