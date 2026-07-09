#[derive(Default, Clone)]
pub struct RoiSelectorState {
    pub start_pos: Option<egui::Pos2>,
    pub current_pos: Option<egui::Pos2>,
    pub final_rect: Option<egui::Rect>,
    pub is_active: bool,
}

impl RoiSelectorState {
    pub fn show(&mut self, ctx: &egui::Context) -> Option<egui::Rect> {
        if !self.is_active {
            return None;
        }

        let mut completed_rect = None;

        // Open a fullscreen transparent window
        ctx.show_viewport_immediate(
            egui::ViewportId::from_hash_of("roi_selector"),
            egui::ViewportBuilder::default()
                .with_title("Overlayr ROI Selector")
                .with_transparent(true)
                .with_decorations(false)
                .with_always_on_top()
                .with_maximized(true), // Cover whole screen
            |ctx, class| {
                if class == egui::ViewportClass::Immediate {
                    egui::CentralPanel::default()
                        .frame(egui::Frame::none().fill(egui::Color32::from_black_alpha(100))) // Dark tint overlay
                        .show(ctx, |ui| {
                            // Instruction text in center
                            ui.centered_and_justified(|ui| {
                                ui.heading(
                                    egui::RichText::new("Click and drag to select capture area. Press ESC to cancel.")
                                        .color(egui::Color32::WHITE)
                                        .size(24.0)
                                );
                            });

                            // Handle pointer input
                            ui.input(|i| {
                                if i.key_pressed(egui::Key::Escape) {
                                    self.is_active = false;
                                }

                                if i.pointer.button_pressed(egui::PointerButton::Primary) {
                                    self.start_pos = i.pointer.press_origin();
                                }
                                if i.pointer.button_down(egui::PointerButton::Primary) {
                                    self.current_pos = i.pointer.latest_pos();
                                }
                                if i.pointer.button_released(egui::PointerButton::Primary) {
                                    if let (Some(start), Some(curr)) = (self.start_pos, self.current_pos) {
                                        let rect = egui::Rect::from_two_pos(start, curr);
                                        if rect.width() > 10.0 && rect.height() > 10.0 {
                                            completed_rect = Some(rect);
                                        }
                                    }
                                    self.is_active = false;
                                    self.start_pos = None;
                                    self.current_pos = None;
                                }
                            });

                            // Draw the current selection box
                            if let (Some(start), Some(curr)) = (self.start_pos, self.current_pos) {
                                let rect = egui::Rect::from_two_pos(start, curr);
                                // Draw selection borders
                                ui.painter().rect_stroke(
                                    rect,
                                    0.0,
                                    egui::Stroke::new(2.0, egui::Color32::from_rgb(0, 210, 255)),
                                );
                                // Paint inside as fully transparent
                                ui.painter().rect_filled(
                                    rect,
                                    0.0,
                                    egui::Color32::from_black_alpha(0),
                                );
                            }
                        });
                }
            }
        );

        completed_rect
    }
}
