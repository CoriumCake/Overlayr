use std::path::PathBuf;
use std::time::Duration;

use eframe::egui;

use crate::capture::PlatformCapture;
use crate::config::AppConfig;
use crate::ocr::TesseractOcr;
use crate::runtime::PipelineWorker;
use crate::translation::LibreTranslate;
use crate::types::Source;

pub struct OverlayrApp {
    config: AppConfig,
    config_path: PathBuf,
    sources: Vec<Source>,
    selected_source: usize,
    worker: Option<PipelineWorker>,
    active_source: Option<Source>,
    error: Option<String>,
    saved: bool,
}

impl OverlayrApp {
    pub fn new() -> Self {
        let config_path = AppConfig::default_path();
        let (config, error) = match AppConfig::load(&config_path) {
            Ok(config) => (config, None),
            Err(error) => (
                AppConfig::default(),
                Some(format!("Could not load settings: {error}")),
            ),
        };
        let mut app = Self {
            config,
            config_path,
            sources: Vec::new(),
            selected_source: 0,
            worker: None,
            active_source: None,
            error,
            saved: false,
        };
        app.refresh_sources();
        app
    }

    fn refresh_sources(&mut self) {
        match <PlatformCapture as crate::services::CaptureBackend>::sources(
            &mut PlatformCapture::default(),
        ) {
            Ok(sources) => {
                self.sources = sources;
                self.selected_source = self
                    .selected_source
                    .min(self.sources.len().saturating_sub(1));
            }
            Err(error) => self.error = Some(error),
        }
    }

    fn save(&mut self) {
        match self.config.save(&self.config_path) {
            Ok(()) => {
                self.error = None;
                self.saved = true;
            }
            Err(error) => self.error = Some(format!("Could not save settings: {error}")),
        }
    }

    fn start(&mut self) {
        if self.worker.is_some() {
            return;
        }
        let Some(source) = self.sources.get(self.selected_source).cloned() else {
            self.error = Some(
                "No capture source is available. Check your desktop session and capture utility."
                    .into(),
            );
            return;
        };
        let translator = match LibreTranslate::new(
            self.config.translation_endpoint.clone(),
            std::env::var("OVERLAYR_TRANSLATION_API_KEY").ok(),
        ) {
            Ok(translator) => translator,
            Err(error) => {
                self.error = Some(error);
                return;
            }
        };
        self.save();
        self.worker = Some(PipelineWorker::start(
            self.config.clone(),
            source.clone(),
            PlatformCapture::default(),
            TesseractOcr::new(
                std::env::var("OVERLAYR_TESSERACT_BIN").unwrap_or_else(|_| "tesseract".into()),
                self.config.grouping_mode.clone(),
            ),
            translator,
        ));
        self.active_source = Some(source);
    }

    fn stop(&mut self) {
        self.worker.take();
        self.active_source = None;
    }

    fn select_region(&mut self) {
        match PlatformCapture::default().select_region() {
            Ok(source) => {
                self.sources = vec![source];
                self.selected_source = 0;
                self.error = None;
            }
            Err(error) => self.error = Some(error),
        }
    }
}

impl Default for OverlayrApp {
    fn default() -> Self {
        Self::new()
    }
}

impl eframe::App for OverlayrApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        ctx.request_repaint_after(Duration::from_millis(100));
        let snapshot = self.worker.as_ref().map(PipelineWorker::snapshot);

        egui::TopBottomPanel::top("header").show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.heading("Overlayr");
                ui.separator();
                ui.label("Rust translation overlay");
                if let Some(snapshot) = &snapshot {
                    ui.separator();
                    ui.label(format!(
                        "{} · {:.1} FPS",
                        snapshot.status, snapshot.frames_per_second
                    ));
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if self.worker.is_some() {
                        if ui.button("Stop").clicked() {
                            self.stop();
                        }
                    } else if ui.button("Start overlay").clicked() {
                        self.start();
                    }
                });
            });
        });

        egui::SidePanel::left("settings")
            .min_width(280.0)
            .show(ctx, |ui| {
                ui.heading("Settings");
                ui.separator();
                ui.label("Capture source");
                if self.sources.is_empty() {
                    ui.colored_label(egui::Color32::YELLOW, "No source detected");
                } else {
                    egui::ComboBox::from_id_salt("source")
                        .selected_text(&self.sources[self.selected_source].name)
                        .show_ui(ui, |ui| {
                            for (index, source) in self.sources.iter().enumerate() {
                                ui.selectable_value(&mut self.selected_source, index, &source.name);
                            }
                        });
                }
                if ui.button("Refresh sources").clicked() {
                    self.refresh_sources();
                }
                if ui.button("Select region").clicked() {
                    self.select_region();
                }

                ui.add_space(12.0);
                ui.label("Source language");
                ui.text_edit_singleline(&mut self.config.source_lang);
                ui.label("Target language");
                ui.text_edit_singleline(&mut self.config.target_lang);
                ui.label("Translation endpoint");
                ui.text_edit_singleline(&mut self.config.translation_endpoint);
                ui.label("OCR grouping");
                egui::ComboBox::from_id_salt("grouping")
                    .selected_text(&self.config.grouping_mode)
                    .show_ui(ui, |ui| {
                        ui.selectable_value(&mut self.config.grouping_mode, "line".into(), "Line");
                        ui.selectable_value(
                            &mut self.config.grouping_mode,
                            "paragraph".into(),
                            "Paragraph",
                        );
                    });
                ui.add(
                    egui::Slider::new(&mut self.config.minimum_confidence, 0.0..=1.0)
                        .text("Confidence"),
                );
                ui.add(
                    egui::Slider::new(&mut self.config.capture_interval_ms, 16..=1000)
                        .text("Interval (ms)"),
                );
                ui.checkbox(
                    &mut self.config.translate_only_when_changed,
                    "Skip unchanged frames",
                );

                ui.add_space(8.0);
                if ui.button("Save settings").clicked() {
                    self.save();
                }
                if self.saved {
                    ui.small("Settings saved");
                }
            });

        egui::CentralPanel::default().show(ctx, |ui| {
            if let Some(error) = &self.error {
                ui.colored_label(egui::Color32::LIGHT_RED, error);
                ui.add_space(8.0);
            }
            if let Some(snapshot) = snapshot.clone() {
                if let Some(error) = snapshot.last_error {
                    ui.colored_label(egui::Color32::LIGHT_RED, error);
                }
                ui.heading("Live translations");
                if snapshot.blocks.is_empty() {
                    ui.label("No text detected yet. Start the overlay and ensure Tesseract has the selected language pack.");
                }
                for block in snapshot.blocks {
                    egui::Frame::group(ui.style()).show(ui, |ui| {
                        ui.label(&block.source_text);
                        ui.strong(block.translated_text.as_deref().unwrap_or("Translation pending"));
                        ui.small(format!("confidence {:.0}%", block.confidence * 100.0));
                    });
                    ui.add_space(5.0);
                }
            } else {
                ui.heading("Ready");
                ui.label("Select a source, configure OCR and translation, then start the pipeline.");
                ui.add_space(12.0);
                ui.label("Linux/Wayland capture uses grim. OCR uses local Tesseract. Translation uses a configurable LibreTranslate-compatible endpoint.");
            }
        });

        if let (Some(snapshot), Some(source)) = (snapshot, self.active_source.clone()) {
            let blocks = snapshot.blocks;
            let Some(overlay_bounds) = snapshot
                .capture_bounds
                .or_else(|| (!source.bounds.is_empty()).then_some(source.bounds))
            else {
                return;
            };
            let viewport_id = egui::ViewportId::from_hash_of("overlayr-translation-overlay");
            let builder = egui::ViewportBuilder::default()
                .with_title("Overlayr overlay")
                .with_position([overlay_bounds.left as f32, overlay_bounds.top as f32])
                .with_inner_size([
                    overlay_bounds.width.max(320) as f32,
                    overlay_bounds.height.max(180) as f32,
                ])
                .with_decorations(false)
                .with_transparent(true)
                .with_always_on_top()
                .with_mouse_passthrough(true);
            ctx.show_viewport_deferred(viewport_id, builder, move |overlay_ctx, _| {
                egui::CentralPanel::default()
                    .frame(egui::Frame::NONE.fill(egui::Color32::TRANSPARENT))
                    .show(overlay_ctx, |ui| {
                        for block in &blocks {
                            let x = (block.bounds.left - overlay_bounds.left) as f32;
                            let y = (block.bounds.top - overlay_bounds.top) as f32;
                            let text = block.translated_text.as_deref().unwrap_or("…");
                            egui::Area::new(egui::Id::new((
                                "translated",
                                block.bounds.left,
                                block.bounds.top,
                            )))
                            .fixed_pos([x, y])
                            .order(egui::Order::Foreground)
                            .show(ui.ctx(), |ui| {
                                let font_size =
                                    (block.bounds.height as f32 * 0.9).clamp(10.0, 80.0);
                                egui::Frame::NONE
                                    .fill(egui::Color32::from_rgba_unmultiplied(0, 0, 0, 190))
                                    .corner_radius(egui::CornerRadius::same(4))
                                    .inner_margin(egui::Margin::same(5))
                                    .show(ui, |ui| {
                                        ui.add(
                                            egui::Label::new(
                                                egui::RichText::new(text)
                                                    .color(egui::Color32::WHITE)
                                                    .size(font_size),
                                            )
                                            .wrap(),
                                        );
                                    });
                            });
                        }
                    });
            });
        }
    }
}
