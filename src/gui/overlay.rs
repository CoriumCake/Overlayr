use windows::core::w;
use windows::Win32::Foundation::HWND;
use windows::Win32::UI::WindowsAndMessaging::FindWindowW;

#[derive(Clone, Debug)]
pub struct TranslationResult {
    pub bbox: Vec<Vec<i32>>, // [[x1, y1], [x2, y2], [x3, y3], [x4, y4]]
    pub text: String,
    pub confidence: f32,
    pub translated: String,
}

#[derive(Default)]
pub struct OverlayState {
    pub results: Vec<TranslationResult>,
    pub position: egui::Pos2,
    pub size: egui::Vec2,
    pub is_active: bool,
    configured_hwnd: Option<HWND>,
}

fn find_overlay_hwnd() -> Option<HWND> {
    unsafe {
        let title = w!("Overlayr Overlay");
        let hwnd = FindWindowW(None, title);
        if hwnd.0 != 0 {
            Some(hwnd)
        } else {
            None
        }
    }
}

fn configure_overlay_window(hwnd: HWND) {
    unsafe {
        // Hide from screen capture (Set Display Affinity) to prevent feedback loops
        let _ = windows::Win32::UI::WindowsAndMessaging::SetWindowDisplayAffinity(
            hwnd,
            windows::Win32::UI::WindowsAndMessaging::WDA_EXCLUDEFROMCAPTURE,
        );
        println!("Overlay window capture invisibility configured (HWND: {:?})", hwnd.0);
    }
}

impl OverlayState {
    pub fn show(&mut self, ctx: &egui::Context) {
        if !self.is_active {
            self.configured_hwnd = None;
            return;
        }

        // Configure HWND asynchronously once window is mapped
        if self.configured_hwnd.is_none() {
            if let Some(hwnd) = find_overlay_hwnd() {
                configure_overlay_window(hwnd);
                self.configured_hwnd = Some(hwnd);
            }
        }

        ctx.show_viewport_immediate(
            egui::ViewportId::from_hash_of("overlay_window"),
            egui::ViewportBuilder::default()
                .with_title("Overlayr Overlay")
                .with_transparent(true)
                .with_decorations(false)
                .with_always_on_top()
                .with_mouse_passthrough(true)
                .with_inner_size(self.size)
                .with_position(self.position),
            |ctx, class| {
                if class == egui::ViewportClass::Immediate {
                    egui::CentralPanel::default()
                        .frame(egui::Frame::none().fill(egui::Color32::from_black_alpha(0))) // Fully transparent background
                        .show(ctx, |ui| {
                            // Draw results
                            for item in &self.results {
                                if item.translated.is_empty() {
                                    continue;
                                }

                                // Calculate dimensions relative to captured area (relative to overlay origin)
                                // bbox: [[x1, y1], [x2, y2], [x3, y3], [x4, y4]]
                                let x1 = item.bbox[0][0] as f32;
                                let y1 = item.bbox[0][1] as f32;
                                let x2 = item.bbox[2][0] as f32;
                                let y2 = item.bbox[2][1] as f32;

                                let rect = egui::Rect::from_min_max(
                                    egui::pos2(x1, y1),
                                    egui::pos2(x2, y2),
                                );

                                // Heuristics to compute dynamic font size based on bounding box height
                                let box_h = (y2 - y1).abs();
                                let font_size = (box_h * 0.85).max(10.0).min(75.0);

                                // 1. Draw rounded background box behind translation text
                                ui.painter().rect_filled(
                                    rect.expand(2.0),
                                    4.0,
                                    egui::Color32::from_black_alpha(210), // Semi-transparent black background
                                );

                                // 2. Put text inside the rect with word wrapping enabled
                                ui.put(
                                    rect,
                                    egui::Label::new(
                                        egui::RichText::new(&item.translated)
                                            .color(egui::Color32::WHITE)
                                            .size(font_size)
                                            .strong()
                                    )
                                    .wrap(true)
                                );
                            }
                        });
                }
            }
        );
    }
}
