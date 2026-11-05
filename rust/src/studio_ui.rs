//! The creator studio: record an application, then translate and style the
//! text found in its keyframes to build a translation pack.
//!
//! Edits belong to text entries, not to frames: an entry applies wherever its
//! text appears, in any order, which is what players will see during
//! playback. The timeline is for moving between recorded keyframes.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use eframe::egui;
use egui::{Color32, CornerRadius, RichText};

use crate::capture::PlatformCapture;
use crate::config::AppConfig;
use crate::pack::{Anchor, Background, Pack, PackEntry, TextAlign, TextStyle};
use crate::studio::{Keyframe, Recorder, RecorderOptions};
use crate::tracker::{TrackerSettings, normalize, text_similarity};
use crate::types::{Frame, Source, SourceKind, TextBlock};
use crate::ui::{
    ACCENT, DANGER, LineSizes, SUCCESS, WARNING, average_color, contrasting, paint_translation,
    primary_button, rgba, section, source_card, source_detail,
};

/// What the studio asks the main window to do.
pub enum StudioAction {
    /// Play this pack over the application it was made for.
    Play {
        pack: PathBuf,
        source: Option<String>,
    },
}

/// Anchors recorded per entry; a few positions cover a text that appears in
/// several places.
/// Outline and badge colour of auto-translated drafts awaiting a check.
const DRAFT: Color32 = Color32::from_rgb(160, 130, 255);

/// How long live AI translation waits after the last new keyframe, so text
/// that appears in quick succession goes in one request.
const LIVE_TRANSLATE_DELAY: std::time::Duration = std::time::Duration::from_millis(1200);

/// How long after an edit it is saved automatically.
const AUTOSAVE_DELAY: std::time::Duration = std::time::Duration::from_millis(800);
const MAX_ANCHORS: usize = 3;
/// Below this window width the studio shows one pane at a time instead of a
/// side panel, so it stays usable in narrow tiled windows.
const NARROW_WIDTH: f32 = 900.0;

/// Canvas zoom limits, relative to fitting the whole keyframe.
const MAX_ZOOM: f32 = 8.0;
const ZOOM_STEP: f32 = 1.25;
/// Zoom per point of scrolling; one mouse-wheel notch is about one
/// [`ZOOM_STEP`].
const SCROLL_ZOOM_SPEED: f32 = 0.004;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Pane {
    Canvas,
    Entries,
}

/// A unique text across all keyframes.
struct TextSummary {
    text: String,
    /// Every spelling OCR produced for this text across keyframes.
    variants: Vec<String>,
    /// Indices of keyframes showing it.
    keyframes: Vec<usize>,
    /// The pack entry translating it, if any.
    entry: Option<usize>,
}

pub struct StudioView {
    /// The program or game being recorded.
    source_id: Option<String>,
    /// Shows the application picker over the canvas.
    choosing_source: bool,
    narrow: bool,
    pane: Pane,
    /// Canvas zoom relative to fitting the keyframe (1 = fit), and pan offset
    /// in points. Kept across keyframes to compare the same spot.
    zoom: f32,
    pan: egui::Vec2,
    /// The entry being edited inline on the preview, and whether its text
    /// field still needs keyboard focus.
    editing: Option<usize>,
    focus_inline: bool,
    pack: Pack,
    saved_pack: Option<Pack>,
    recorder: Option<Recorder>,
    keyframes: Vec<Keyframe>,
    next_keyframe_id: u64,
    current: Option<usize>,
    /// Decoded current keyframe and its texture.
    canvas: Option<(u64, Frame, egui::TextureHandle)>,
    thumbnails: HashMap<u64, egui::TextureHandle>,
    selected_entry: Option<usize>,
    /// Shows the untouched keyframe instead of the translations.
    show_original: bool,
    texts: Vec<TextSummary>,
    texts_dirty: bool,
    filter: String,
    message: Option<(bool, String)>,
    /// The game window moved to its own full-screen workspace while
    /// recording; dropping it puts the window back.
    staged: Option<StagedWindow>,
    /// While recording, show the live capture instead of a keyframe.
    live: bool,
    live_view: Option<(Frame, egui::TextureHandle)>,
    /// Boxes drawn on the canvas last frame, for starting a drag.
    canvas_boxes: Vec<(String, egui::Rect)>,
    /// The entry whose translation is being dragged.
    dragging: Option<usize>,
    /// When the pack first differed from the saved copy; changes are saved
    /// automatically shortly after.
    unsaved_since: Option<std::time::Instant>,
    /// Drafting translations in the background.
    auto_translate: Option<AutoTranslate>,
    /// Drafts new text with AI as it is recorded, from keyframes with at
    /// least this id, so a translator can play without switching back.
    live_translate: Option<u64>,
    /// Texts live translation already sent, so one that came back empty
    /// isn't sent again and again.
    live_sent: HashSet<String>,
    /// After a failed draft, live translation waits until then and tries
    /// again; failures in a row lengthen the wait.
    live_retry_at: Option<std::time::Instant>,
    live_failures: u32,
    /// When the last keyframe arrived, to wait for text that follows.
    last_keyframe_at: Option<std::time::Instant>,
    /// The app's settings this frame, for actions deep in the UI.
    settings: AppConfig,
    /// Pack entry matching each text, as [`Pack::find_index`] finds it.
    /// Fuzzy-matching every keyframe's text against every entry each frame
    /// kept the UI thread busy, so results are kept until the entries'
    /// source texts change (see [`Self::refresh_entry_cache`]).
    entry_cache: std::cell::RefCell<HashMap<String, Option<usize>>>,
    entry_cache_key: u64,
    /// Rebuilt backgrounds of the current keyframe's texts, by index.
    backdrops: HashMap<usize, Option<std::sync::Arc<crate::types::Backdrop>>>,
}

/// A Hyprland window made full screen where it is while recording, so it
/// renders the full-screen layout players see. It stays on its own
/// workspace, and since Hyprland windows are captured from their own buffer,
/// recording goes on while the creator uses other apps or workspaces.
/// Its fullscreen state is restored when dropped.
struct StagedWindow {
    address: String,
    fullscreen: i64,
}

impl StagedWindow {
    #[cfg(unix)]
    fn stage(address: &str) -> Result<Self, String> {
        use crate::hyprland;
        let client = hyprland::clients()?
            .into_iter()
            .find(|client| client.address == address)
            .ok_or_else(|| "the window was closed".to_string())?;
        let staged = Self {
            address: address.to_string(),
            fullscreen: client.fullscreen,
        };
        // Best effort: without it some games stop drawing while hidden.
        let _ = hyprland::keep_rendering(&client.class);
        staged.make_full_screen()?;
        Ok(staged)
    }

    /// Makes the window full screen again, e.g. after focusing another
    /// window on its workspace took it out of full screen.
    #[cfg(unix)]
    fn make_full_screen(&self) -> Result<(), String> {
        crate::hyprland::set_fullscreen(&self.address, 2)
    }

    #[cfg(not(unix))]
    fn make_full_screen(&self) -> Result<(), String> {
        Err("only available on Hyprland".into())
    }

    #[cfg(not(unix))]
    fn stage(_address: &str) -> Result<Self, String> {
        Err("only available on Hyprland".into())
    }

    /// Switches to the game's workspace so the creator can play.
    fn focus(&self) {
        #[cfg(unix)]
        let _ = crate::hyprland::focus_window(&self.address);
    }
}

impl Drop for StagedWindow {
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            use crate::hyprland;
            if self.fullscreen != 2 {
                let _ = hyprland::set_fullscreen(&self.address, self.fullscreen);
            }
            let _ = hyprland::stop_keeping_rendering();
        }
    }
}

impl Default for StudioView {
    fn default() -> Self {
        Self {
            source_id: None,
            choosing_source: false,
            narrow: false,
            pane: Pane::Canvas,
            zoom: 1.0,
            pan: egui::Vec2::ZERO,
            editing: None,
            focus_inline: false,
            pack: Pack {
                name: "Untitled pack".into(),
                ..Pack::default()
            },
            saved_pack: None,
            recorder: None,
            keyframes: Vec::new(),
            next_keyframe_id: 1,
            current: None,
            canvas: None,
            thumbnails: HashMap::new(),
            selected_entry: None,
            show_original: false,
            texts: Vec::new(),
            texts_dirty: false,
            filter: String::new(),
            message: None,
            staged: None,
            live: false,
            live_view: None,
            backdrops: HashMap::new(),
            entry_cache: std::cell::RefCell::new(HashMap::new()),
            entry_cache_key: 0,
            canvas_boxes: Vec::new(),
            auto_translate: None,
            live_translate: None,
            live_sent: HashSet::new(),
            live_retry_at: None,
            live_failures: 0,
            last_keyframe_at: None,
            settings: AppConfig::default(),
            dragging: None,
            unsaved_since: None,
        }
    }
}

impl StudioView {
    pub fn is_recording(&self) -> bool {
        self.recorder.is_some()
    }

    /// The application chosen for recording and testing.
    pub fn source_id(&self) -> Option<&str> {
        self.source_id.as_deref()
    }

    /// The pack being edited, including unsaved changes.
    pub fn pack(&self) -> &Pack {
        &self.pack
    }

    pub fn show(
        &mut self,
        ctx: &egui::Context,
        config: &AppConfig,
        sources: &[Source],
        thumbnails: &HashMap<String, egui::TextureHandle>,
    ) -> Option<StudioAction> {
        let source = self
            .source_id
            .as_ref()
            .and_then(|id| sources.iter().find(|source| &source.id == id));
        self.settings = config.clone();
        self.refresh_entry_cache();
        self.collect_keyframes();
        self.collect_drafts(ctx);
        if self.texts_dirty {
            self.rebuild_texts();
        }
        self.live_translate(ctx, config);
        let mut action = None;
        self.narrow = ctx.content_rect().width() < NARROW_WIDTH;

        self.autosave(ctx);
        // Picking the program comes first; the editor (toolbar, timeline
        // and entries) appears once one is chosen.
        if self.is_picking() {
            egui::TopBottomPanel::top("studio_toolbar")
                .frame(
                    egui::Frame::side_top_panel(&ctx.style())
                        .inner_margin(egui::Margin::symmetric(16, 8)),
                )
                .show(ctx, |ui| self.picker_toolbar(ui, config));
            egui::CentralPanel::default().show(ctx, |ui| {
                self.source_picker(ui, sources, thumbnails);
            });
            return action;
        }
        egui::TopBottomPanel::top("studio_toolbar")
            .frame(
                egui::Frame::side_top_panel(&ctx.style())
                    .inner_margin(egui::Margin::symmetric(16, 8)),
            )
            .show(ctx, |ui| action = self.toolbar(ui, config, sources, source));
        egui::TopBottomPanel::bottom("studio_timeline")
            .resizable(false)
            .exact_height(if self.narrow { 100.0 } else { 132.0 })
            .show(ctx, |ui| self.timeline(ui));
        if !self.narrow {
            egui::SidePanel::right("studio_entries")
                .resizable(true)
                .default_width(360.0)
                .width_range(300.0..=520.0)
                .show(ctx, |ui| self.entries_pane(ui));
        }
        egui::CentralPanel::default().show(ctx, |ui| {
            if self.narrow && self.pane == Pane::Entries {
                self.entries_pane(ui);
            } else {
                self.canvas_ui(ui);
            }
        });
        action
    }

    fn entries_pane(&mut self, ui: &mut egui::Ui) {
        egui::ScrollArea::vertical()
            .auto_shrink(false)
            .show(ui, |ui| self.entry_list(ui));
    }

    /// Whether the program picker is shown instead of the editor.
    fn is_picking(&self) -> bool {
        // An opened pack goes straight to the editor; its program can be
        // picked there.
        let has_work =
            self.source_id.is_some() || !self.keyframes.is_empty() || self.saved_pack.is_some();
        self.recorder.is_none() && (self.choosing_source || !has_work)
    }

    /// The picker's toolbar: open an existing pack, or go back to the
    /// editor when there is one.
    fn picker_toolbar(&mut self, ui: &mut egui::Ui, config: &AppConfig) {
        ui.horizontal_wrapped(|ui| {
            let can_return =
                self.source_id.is_some() || !self.keyframes.is_empty() || self.saved_pack.is_some();
            if can_return
                && ui
                    .button(RichText::new("←").size(16.0))
                    .on_hover_text("Back to the editor")
                    .clicked()
            {
                self.choosing_source = false;
            }
            self.open_menu(ui, config);
        });
        if let Some((is_error, message)) = &self.message {
            let color = if *is_error { DANGER } else { SUCCESS };
            ui.label(RichText::new(message).small().color(color));
        }
    }

    fn toolbar(
        &mut self,
        ui: &mut egui::Ui,
        config: &AppConfig,
        sources: &[Source],
        source: Option<&Source>,
    ) -> Option<StudioAction> {
        let mut action = None;
        // Wraps onto more lines when the window is narrow.
        ui.horizontal_wrapped(|ui| {
            if ui
                .add_enabled(
                    self.recorder.is_none(),
                    egui::Button::new(RichText::new("←").size(16.0)),
                )
                .on_hover_text("Choose another program or game")
                .on_disabled_hover_text("Stop recording first")
                .clicked()
            {
                self.choosing_source = true;
            }
            if self.narrow {
                ui.selectable_value(&mut self.pane, Pane::Canvas, "Canvas");
                ui.selectable_value(&mut self.pane, Pane::Entries, "Entries");
                ui.separator();
            }
            ui.add(
                egui::TextEdit::singleline(&mut self.pack.name)
                    .desired_width(if self.narrow { 140.0 } else { 200.0 })
                    .hint_text("Pack name"),
            );
            self.application_menu(ui, sources, source);
            if let Some(recorder) = &self.recorder {
                let (status, error) = recorder.status();
                let stop = ui.add(primary_button("■ Stop recording", DANGER)).clicked();
                if ui
                    .button("Capture frame")
                    .on_hover_text("Save the current screen as a keyframe")
                    .clicked()
                {
                    recorder.capture_now();
                }
                let (text, color) = match error {
                    Some(error) => (format!("{status}: {error}"), DANGER),
                    None => (status, SUCCESS),
                };
                ui.label(RichText::new(text).color(color).small());
                if stop {
                    self.stop_recording();
                }
            } else {
                let response = ui.add_enabled(source.is_some(), primary_button("● Record", ACCENT));
                let response = if source.is_none() {
                    response.on_disabled_hover_text("Choose a program or game to record first")
                } else {
                    response.on_hover_text(
                        "Play normally; a keyframe is saved whenever new text appears",
                    )
                };
                if response.clicked()
                    && let Some(source) = source
                {
                    self.start_recording(config, source);
                }
            }

            ui.separator();
            self.open_menu(ui, config);
            ui.checkbox(&mut self.show_original, "Show original")
                .on_hover_text("Hide translations to compare with the original keyframe");
            if let Some(path) = self.pack.library_path().filter(|path| path.exists())
                && ui
                    .button("▶ Play pack")
                    .on_hover_text("Use this pack in Translate")
                    .clicked()
            {
                action = Some(StudioAction::Play {
                    pack: path,
                    source: self.source_id.clone(),
                });
            }
            self.auto_translate_menu(ui, config);
            let mut live = self.live_translate.is_some();
            if ui
                .checkbox(&mut live, "Live AI translate")
                .on_hover_text(
                    "While recording, new text is drafted by AI as soon as it appears, so you \
                     can keep playing and check the drafts afterwards",
                )
                .changed()
            {
                self.live_translate = live.then_some(self.next_keyframe_id);
                self.live_sent.clear();
                self.live_retry_at = None;
                self.live_failures = 0;
            }
            let dirty = self.saved_pack.as_ref() != Some(&self.pack);
            if ui
                .add_enabled(dirty, primary_button("Save pack", ACCENT))
                .clicked()
            {
                self.save();
            }
        });
        if let Some((is_error, message)) = &self.message {
            let color = if *is_error { DANGER } else { SUCCESS };
            ui.label(RichText::new(message).small().color(color));
        }
        action
    }

    /// Quick switcher for the recorded application; "Choose…" opens the
    /// card picker with live previews.
    fn application_menu(&mut self, ui: &mut egui::Ui, sources: &[Source], source: Option<&Source>) {
        ui.add_enabled_ui(self.recorder.is_none(), |ui| {
            let selected = source.map_or_else(
                || "Choose application".to_string(),
                |source| source.name.chars().take(32).collect(),
            );
            egui::ComboBox::from_id_salt("studio_application")
                .selected_text(selected)
                .width(if self.narrow { 160.0 } else { 220.0 })
                .show_ui(ui, |ui| {
                    if ui
                        .selectable_label(self.choosing_source, "Choose…")
                        .clicked()
                    {
                        self.choosing_source = true;
                    }
                    ui.separator();
                    for candidate in sources {
                        let label = format!("{} — {}", candidate.name, source_detail(candidate));
                        let selected = self.source_id.as_ref() == Some(&candidate.id);
                        if ui.selectable_label(selected, label).clicked() {
                            self.choose_source(candidate);
                        }
                    }
                });
        });
    }

    fn choose_source(&mut self, source: &Source) {
        self.source_id = Some(source.id.clone());
        self.choosing_source = false;
        if self.pack.application.is_empty() {
            self.pack.application = source.name.clone();
        }
    }

    fn source_picker(
        &mut self,
        ui: &mut egui::Ui,
        sources: &[Source],
        thumbnails: &HashMap<String, egui::TextureHandle>,
    ) {
        egui::ScrollArea::vertical()
            .auto_shrink(false)
            .show(ui, |ui| {
                if sources.is_empty() {
                    ui.label("Looking for windows and screens…");
                    return;
                }
                for (title, kind) in [
                    ("Windows", SourceKind::Window),
                    ("Screens", SourceKind::Screen),
                ] {
                    let group = sources
                        .iter()
                        .filter(|source| source.kind == kind)
                        .collect::<Vec<_>>();
                    if group.is_empty() {
                        continue;
                    }
                    ui.label(RichText::new(title).strong());
                    ui.horizontal_wrapped(|ui| {
                        ui.spacing_mut().item_spacing = egui::vec2(12.0, 12.0);
                        for source in group {
                            let selected = self.source_id.as_ref() == Some(&source.id);
                            if source_card(ui, source, thumbnails.get(&source.id), selected)
                                .clicked()
                            {
                                self.choose_source(source);
                            }
                        }
                    });
                    ui.add_space(8.0);
                }
            });
    }

    fn open_menu(&mut self, ui: &mut egui::Ui, config: &AppConfig) {
        egui::ComboBox::from_id_salt("studio_open")
            .selected_text("Open…")
            .width(120.0)
            .show_ui(ui, |ui| {
                if ui.selectable_label(false, "New pack").clicked() {
                    self.pack = Pack {
                        name: "Untitled pack".into(),
                        source_lang: config.source_lang.clone(),
                        target_lang: config.target_lang.clone(),
                        ..Pack::default()
                    };
                    self.saved_pack = None;
                    self.selected_entry = None;
                    self.texts_dirty = true;
                }
                for (_, pack) in crate::pack::library() {
                    let label = format!("{} ({} entries)", pack.name, pack.entries.len());
                    if ui
                        .selectable_label(pack.id == self.pack.id, label)
                        .clicked()
                    {
                        self.saved_pack = Some(pack.clone());
                        self.pack = pack;
                        self.selected_entry = None;
                        self.texts_dirty = true;
                    }
                }
            });
    }

    fn start_recording(&mut self, config: &AppConfig, source: &Source) {
        if self.pack.source_lang.is_empty() {
            self.pack.source_lang = config.source_lang.clone();
            self.pack.target_lang = config.target_lang.clone();
        }
        if self.pack.application.is_empty() {
            self.pack.application = source.name.clone();
        }
        config.performance.apply();
        let ocr = crate::ocr::engine(&self.pack.source_lang, &config.grouping_mode);
        if let Err(error) = ocr.validate_language(&self.pack.source_lang) {
            self.message = Some((true, format!("OCR is not ready: {error}")));
            return;
        }
        self.message = None;
        self.recorder = Some(Recorder::start(
            source.clone(),
            PlatformCapture::default(),
            ocr,
            RecorderOptions {
                language: self.pack.source_lang.clone(),
                minimum_confidence: config.minimum_confidence,
                tracker: TrackerSettings {
                    match_threshold: config.match_threshold,
                    follow_threshold: config.follow_threshold,
                    detect_threshold: config.detect_threshold,
                    follow_highlights: config.performance.follow_highlights,
                    ..TrackerSettings::default()
                },
                first_id: self.next_keyframe_id,
                known_texts: self
                    .keyframes
                    .iter()
                    .flat_map(|keyframe| &keyframe.texts)
                    .map(|detected| detected.text.clone())
                    .collect(),
            },
        ));
        self.live = true;
        // Run the game full screen on its own workspace, so keyframes show
        // the layout players see while the studio keeps the screen.
        if let Some(address) = crate::capture::hyprland_address(source) {
            match StagedWindow::stage(address) {
                Ok(staged) => self.staged = Some(staged),
                Err(error) => {
                    self.message =
                        Some((true, format!("Couldn't make the game full screen: {error}")));
                }
            }
        }
    }

    fn stop_recording(&mut self) {
        if let Some(mut recorder) = self.recorder.take() {
            recorder.stop();
            self.keyframes.extend(recorder.take_keyframes());
            self.after_new_keyframes();
        }
        // Puts the game window back where it was.
        self.staged = None;
        self.live = false;
        self.live_view = None;
    }

    /// The game as it is right now, full screen, while recording.
    fn live_canvas(&mut self, ui: &mut egui::Ui) {
        if let Some(frame) = self.recorder.as_ref().and_then(Recorder::take_live_frame) {
            let image = egui::ColorImage::from_rgba_unmultiplied(
                [frame.width as usize, frame.height as usize],
                &frame.rgba,
            );
            let options = egui::TextureOptions {
                magnification: egui::TextureFilter::Nearest,
                ..egui::TextureOptions::LINEAR
            };
            match &mut self.live_view {
                Some((current, texture)) => {
                    texture.set(image, options);
                    *current = frame;
                }
                None => {
                    let texture = ui.ctx().load_texture("studio-live", image, options);
                    self.live_view = Some((frame, texture));
                }
            }
        }
        ui.ctx()
            .request_repaint_after(std::time::Duration::from_millis(200));
        ui.horizontal(|ui| {
            ui.label(
                RichText::new("● LIVE")
                    .color(Color32::from_rgb(235, 80, 90))
                    .strong(),
            );
            ui.label(
                RichText::new(if self.staged.is_some() {
                    "The game runs full screen and keeps recording while you use other apps. \
                     New text becomes a keyframe."
                } else {
                    "New text on screen becomes a keyframe."
                })
                .weak(),
            );
            if let Some(staged) = &self.staged
                && ui
                    .button("Play game")
                    .on_hover_text("Switch to the game's workspace; come back to translate")
                    .clicked()
            {
                staged.focus();
            }
            let squeezed = self.recorder.as_ref().is_some_and(|recorder| {
                recorder
                    .status()
                    .0
                    .starts_with(crate::studio::SQUEEZED_STATUS)
            });
            if let Some(staged) = &self.staged
                && squeezed
                && ui
                    .button("Full screen again")
                    .on_hover_text(
                        "The game left full screen; put it back for the layout players see",
                    )
                    .clicked()
                && let Err(error) = staged.make_full_screen()
            {
                self.message = Some((true, format!("Couldn't make the game full screen: {error}")));
            }
        });
        let Some((_, texture)) = &self.live_view else {
            ui.centered_and_justified(|ui| {
                ui.spinner();
            });
            return;
        };
        let available = ui.available_rect_before_wrap();
        let image_size = texture.size_vec2();
        let fit = (available.width() / image_size.x).min(available.height() / image_size.y);
        let rect = egui::Rect::from_center_size(available.center(), image_size * fit);
        ui.painter().image(
            texture.id(),
            rect,
            egui::Rect::from_min_max(egui::pos2(0.0, 0.0), egui::pos2(1.0, 1.0)),
            Color32::WHITE,
        );
        ui.allocate_rect(available, egui::Sense::hover());
    }

    fn collect_keyframes(&mut self) {
        let Some(recorder) = &self.recorder else {
            return;
        };
        let new = recorder.take_keyframes();
        if !new.is_empty() {
            self.keyframes.extend(new);
            self.after_new_keyframes();
            self.last_keyframe_at = Some(std::time::Instant::now());
            // Follow the recording.
            self.current = Some(self.keyframes.len() - 1);
        }
    }

    fn after_new_keyframes(&mut self) {
        if let Some(last) = self.keyframes.last() {
            self.next_keyframe_id = last.id + 1;
        }
        if self.current.is_none() && !self.keyframes.is_empty() {
            self.current = Some(0);
        }
        self.texts_dirty = true;
    }

    /// Records where each translated entry's text appears in the keyframes
    /// and how it looks, so playback recognises it by its pixels and locks
    /// the translation there (see [`crate::pack::Anchor`]).
    pub fn record_anchors(&mut self) {
        let mut frames: HashMap<usize, Option<Frame>> = HashMap::new();
        for (index, keyframe) in self.keyframes.iter().enumerate() {
            if !keyframe.full_size {
                continue;
            }
            for detected in &keyframe.texts {
                let Some(entry) = self.pack.find_index(&detected.text) else {
                    continue;
                };
                let known = &self.pack.entries[entry];
                if known.translation.trim().is_empty() || known.anchors.len() >= MAX_ANCHORS {
                    continue;
                }
                // Already anchored here: no need to decode the keyframe.
                if known.anchors.iter().any(|anchor| {
                    (anchor.frame_width, anchor.frame_height) == (keyframe.width, keyframe.height)
                        && (anchor.area[0] - detected.pixels.left).abs() <= 6
                        && (anchor.area[1] - detected.pixels.top).abs() <= 6
                }) {
                    continue;
                }
                let Some(frame) = frames.entry(index).or_insert_with(|| keyframe.frame()) else {
                    continue;
                };
                let Some(anchor) = Anchor::new(frame, detected.pixels) else {
                    continue;
                };
                if !known
                    .anchors
                    .iter()
                    .any(|existing| existing.same_place(&anchor))
                {
                    self.pack.entries[entry].anchors.push(anchor);
                }
            }
        }
    }

    /// Drafts text from newly recorded keyframes once recording settles for a
    /// moment. Text that turns up while a draft is running goes in the next.
    fn live_translate(&mut self, ctx: &egui::Context, config: &AppConfig) {
        let Some(first_id) = self.live_translate else {
            return;
        };
        if self.auto_translate.is_some() || self.texts_dirty {
            return;
        }
        if let Some(at) = self.live_retry_at {
            let now = std::time::Instant::now();
            if now < at {
                ctx.request_repaint_after(at - now);
                return;
            }
            self.live_retry_at = None;
        }
        if let Some(at) = self.last_keyframe_at {
            let waited = at.elapsed();
            if waited < LIVE_TRANSLATE_DELAY {
                ctx.request_repaint_after(LIVE_TRANSLATE_DELAY - waited);
                return;
            }
        }
        let texts = self
            .texts
            .iter()
            .filter(|summary| !self.is_translated(summary.entry))
            .filter(|summary| {
                summary
                    .keyframes
                    .iter()
                    .any(|&index| self.keyframes.get(index).is_some_and(|k| k.id >= first_id))
            })
            .filter(|summary| !self.live_sent.contains(&summary.text))
            .map(|summary| summary.text.clone())
            .collect::<Vec<_>>();
        if texts.is_empty() {
            return;
        }
        self.live_sent.extend(texts.iter().cloned());
        self.start_auto_translate(config, texts, false);
        if self.auto_translate.is_none() {
            // Couldn't start (no key, no service): don't retry every frame.
            self.live_translate = None;
        }
    }

    /// Texts with no translation yet: on the current keyframe, or all.
    fn untranslated_texts(&self, current_only: bool) -> Vec<String> {
        let current = self.current.filter(|_| current_only);
        self.texts
            .iter()
            .filter(|summary| !self.is_translated(summary.entry))
            .filter(|summary| current.is_none_or(|index| summary.keyframes.contains(&index)))
            .map(|summary| summary.text.clone())
            .collect()
    }

    fn auto_translate_menu(&mut self, ui: &mut egui::Ui, config: &AppConfig) {
        if let Some(job) = &self.auto_translate {
            ui.spinner();
            ui.label(format!("Drafting {}/{}", job.done, job.total));
            if ui.button("Cancel").clicked() {
                job.cancel.store(true, std::sync::atomic::Ordering::Relaxed);
                self.auto_translate = None;
                // Otherwise live translation starts the next one at once.
                self.live_translate = None;
            }
            return;
        }
        let drafts = self.pack.entries.iter().filter(|entry| entry.draft).count();
        ui.menu_button("✨ AI translate", |ui| {
            ui.set_min_width(260.0);
            let note = match crate::ai::Provider::from_id(&config.draft_translator) {
                Some(provider) => format!(
                    "Drafts with {}, using the game's name and your translations as a glossary.",
                    provider.label()
                ),
                None => "Drafts with LibreTranslate. AI services (Claude, ChatGPT, Gemini, \
                         DeepSeek) give better drafts: choose one in Settings."
                    .into(),
            };
            ui.label(RichText::new(note).small().weak());
            ui.add_space(4.0);
            ui.label(RichText::new("Context for AI").strong())
                .on_hover_text("Sent with every AI translation of this pack");
            ui.add(
                egui::TextEdit::multiline(&mut self.pack.ai_context)
                    .desired_rows(4)
                    .desired_width(320.0)
                    .hint_text(
                        "What the game is and how to translate it, e.g.\n\
                         FTB StoneBlock 4 is a Minecraft skyblock modpack. Keep mod and item \
                         names in English. Friendly, casual Thai for players.",
                    ),
            );
            ui.label(
                RichText::new("Saved with the pack and sent with every AI translation.")
                    .small()
                    .weak(),
            );
            ui.separator();
            ui.add_space(4.0);
            for (label, current_only) in [("This keyframe", true), ("All untranslated", false)] {
                let texts = self.untranslated_texts(current_only);
                if ui
                    .add_enabled(
                        !texts.is_empty(),
                        egui::Button::new(format!("{label} ({})", texts.len())),
                    )
                    .clicked()
                {
                    self.start_auto_translate(config, texts, false);
                    ui.close();
                }
            }
            if drafts > 0 {
                ui.separator();
                if ui
                    .button(format!("Accept all {drafts} drafts"))
                    .on_hover_text("Mark every draft as checked")
                    .clicked()
                {
                    for entry in &mut self.pack.entries {
                        entry.draft = false;
                    }
                    ui.close();
                }
            }
        })
        .response
        .on_hover_text("Draft translations for you to check and fix");
    }

    /// Drafts translations for `texts`. With `replace`, a text that already
    /// has a translation gets a new draft too (the creator asked for it).
    fn start_auto_translate(&mut self, config: &AppConfig, mut texts: Vec<String>, replace: bool) {
        use std::sync::atomic::{AtomicBool, Ordering};
        let pair = crate::types::LanguagePair::new(
            if self.pack.source_lang.is_empty() {
                config.source_lang.clone()
            } else {
                self.pack.source_lang.clone()
            },
            if self.pack.target_lang.is_empty() {
                config.target_lang.clone()
            } else {
                self.pack.target_lang.clone()
            },
        );
        // Short labels first: they come back quickest.
        texts.sort_by_key(String::len);
        let (sender, results) = std::sync::mpsc::channel();
        let cancel = std::sync::Arc::new(AtomicBool::new(false));
        let total = texts.len();
        if let Some(provider) = crate::ai::Provider::from_id(&config.draft_translator) {
            let translator = match crate::ai::translator(provider, &config.ai_model(provider)) {
                Ok(translator) => translator,
                Err(error) => {
                    self.message = Some((true, error));
                    return;
                }
            };
            let application = self.pack.application.clone();
            let notes = self.pack.ai_context.clone();
            let glossary = self
                .pack
                .entries
                .iter()
                .filter(|entry| !entry.draft && !entry.translation.trim().is_empty())
                .map(|entry| (entry.source_text.clone(), entry.translation.clone()))
                .collect::<Vec<_>>();
            let cancel = std::sync::Arc::clone(&cancel);
            std::thread::spawn(move || {
                let context = crate::ai::Context {
                    application: &application,
                    glossary: &glossary,
                    notes: &notes,
                };
                for batch in texts.chunks(crate::ai::BATCH_SIZE) {
                    if cancel.load(Ordering::Relaxed) {
                        return;
                    }
                    match translator.translate(&pair, &context, batch) {
                        Ok(translations) => {
                            for (text, translation) in batch.iter().zip(translations) {
                                if sender.send((text.clone(), Ok(translation))).is_err() {
                                    return;
                                }
                            }
                        }
                        Err(error) => {
                            let _ = sender.send((String::new(), Err(error)));
                            return;
                        }
                    }
                }
            });
        } else {
            let translator = match crate::translation::LibreTranslate::new(
                config.translation_endpoint.clone(),
                std::env::var("OVERLAYR_TRANSLATION_API_KEY").ok(),
            ) {
                Ok(translator) => translator,
                Err(error) => {
                    self.message = Some((true, error));
                    return;
                }
            };
            let cancel = std::sync::Arc::clone(&cancel);
            std::thread::spawn(move || {
                use crate::services::TranslationBackend;
                let mut translator = translator;
                translator.translate_each(&pair, &texts, &mut |index, result| {
                    if !cancel.load(Ordering::Relaxed) {
                        let _ = sender.send((texts[index].clone(), result));
                    }
                });
            });
        }
        self.message = None;
        self.auto_translate = Some(AutoTranslate {
            results,
            replace,
            total,
            done: 0,
            error: None,
            cancel,
        });
    }

    /// Fills in drafts as they arrive. Text the creator translated in the
    /// meantime is left alone.
    fn collect_drafts(&mut self, ctx: &egui::Context) {
        let Some(job) = &mut self.auto_translate else {
            return;
        };
        let arrived = job.results.try_iter().collect::<Vec<_>>();
        let mut finished = false;
        for (text, result) in arrived {
            match result {
                Ok(translation) => {
                    job.done += 1;
                    let translation = translation.trim().to_string();
                    if translation.is_empty() {
                        continue;
                    }
                    let index = match self.pack.find_index(&text) {
                        Some(index) => index,
                        None => {
                            self.pack.entries.push(PackEntry {
                                source_text: text.clone(),
                                ..PackEntry::default()
                            });
                            self.pack.entries.len() - 1
                        }
                    };
                    let entry = &mut self.pack.entries[index];
                    if job.replace || entry.translation.trim().is_empty() {
                        entry.translation = translation;
                        entry.draft = true;
                        self.texts_dirty = true;
                    }
                }
                Err(error) => {
                    job.error = Some(error);
                    finished = true;
                }
            }
        }
        let Some(job) = &self.auto_translate else {
            return;
        };
        if finished || job.done >= job.total {
            self.message = Some(match &job.error {
                Some(error) if self.live_translate.is_some() => {
                    // Busy services (e.g. Gemini's 503 "high demand") recover
                    // soon; try again later instead of giving up, waiting
                    // longer after each failure so a broken key isn't hammered.
                    self.live_failures += 1;
                    let wait = (15u64 << (self.live_failures - 1).min(5)).min(300);
                    self.live_retry_at =
                        Some(std::time::Instant::now() + std::time::Duration::from_secs(wait));
                    // Texts that didn't get a draft are sent again then.
                    self.live_sent.clear();
                    (
                        true,
                        format!("Live AI translate retries in {wait} s. {error}"),
                    )
                }
                Some(error) => (true, format!("Auto-translate stopped: {error}")),
                None => {
                    self.live_failures = 0;
                    (
                        false,
                        format!(
                            "Drafted {} translations. Check the ones marked draft.",
                            job.done
                        ),
                    )
                }
            });
            self.auto_translate = None;
        } else {
            ctx.request_repaint_after(std::time::Duration::from_millis(150));
        }
    }

    /// Saves edits a moment after they are made, once the pack has
    /// translations (or was saved before), so nothing is lost.
    fn autosave(&mut self, ctx: &egui::Context) {
        let has_work = self.saved_pack.is_some()
            || self
                .pack
                .entries
                .iter()
                .any(|entry| !entry.translation.trim().is_empty());
        if !has_work || self.saved_pack.as_ref() == Some(&self.pack) {
            self.unsaved_since = None;
            return;
        }
        let since = *self
            .unsaved_since
            .get_or_insert_with(std::time::Instant::now);
        let waited = since.elapsed();
        if waited >= AUTOSAVE_DELAY && self.dragging.is_none() {
            self.unsaved_since = None;
            self.save_pack(true);
        } else {
            ctx.request_repaint_after(AUTOSAVE_DELAY.saturating_sub(waited));
        }
    }

    fn save(&mut self) {
        self.save_pack(false);
    }

    fn save_pack(&mut self, automatic: bool) {
        self.record_anchors();
        let Some(path) = self.pack.library_path() else {
            self.message = Some((true, "Could not find your user data directory".into()));
            return;
        };
        match self.pack.save(&path) {
            Ok(()) => {
                self.saved_pack = Some(self.pack.clone());
                self.message = Some((
                    false,
                    if automatic {
                        "All changes saved".into()
                    } else {
                        format!("Saved to {}", path.display())
                    },
                ));
            }
            Err(error) => self.message = Some((true, format!("Could not save the pack: {error}"))),
        }
    }

    /// Groups the text of all keyframes into unique entries, using the
    /// pack's match threshold, and links each to its pack entry.
    fn rebuild_texts(&mut self) {
        let threshold = self.pack.match_threshold;
        // A text joins a group when it is similar to any spelling already in
        // it, so OCR variants of one text stay together.
        let mut texts: Vec<(Vec<String>, TextSummary)> = Vec::new();
        for (index, keyframe) in self.keyframes.iter().enumerate() {
            for detected in &keyframe.texts {
                let normalized = normalize(&detected.text);
                match texts.iter_mut().find(|(known, _)| {
                    known
                        .iter()
                        .any(|variant| text_similarity(variant, &normalized) >= threshold)
                }) {
                    Some((known, summary)) => {
                        if summary.keyframes.last() != Some(&index) {
                            summary.keyframes.push(index);
                        }
                        if !known.contains(&normalized) {
                            known.push(normalized);
                            summary.variants.push(detected.text.clone());
                        }
                    }
                    None => texts.push((
                        vec![normalized],
                        TextSummary {
                            text: detected.text.clone(),
                            variants: vec![detected.text.clone()],
                            keyframes: vec![index],
                            entry: None,
                        },
                    )),
                }
            }
        }
        self.texts = texts
            .into_iter()
            .map(|(_, mut summary)| {
                summary.entry = summary
                    .variants
                    .iter()
                    .find_map(|variant| self.pack.find_index(variant));
                if let Some(entry) = summary.entry {
                    learn_spellings(&mut self.pack.entries[entry], &summary.variants);
                }
                summary
            })
            .collect();
        self.texts_dirty = false;
    }

    /// Selects the entry for `text`, creating an empty one if needed.
    fn select_text(&mut self, text: &str) {
        let index = match self.pack.find_index(text) {
            Some(index) => index,
            None => {
                let mut entry = PackEntry {
                    source_text: text.to_string(),
                    ..PackEntry::default()
                };
                let normalized = normalize(text);
                if let Some(summary) = self.texts.iter().find(|summary| {
                    summary
                        .variants
                        .iter()
                        .any(|variant| normalize(variant) == normalized)
                }) {
                    learn_spellings(&mut entry, &summary.variants);
                }
                self.pack.entries.push(entry);
                self.texts_dirty = true;
                self.pack.entries.len() - 1
            }
        };
        self.selected_entry = Some(index);
    }

    /// Forgets cached matches once anything [`Pack::find_index`] looks at
    /// has changed: the entries, their spellings or thresholds.
    fn refresh_entry_cache(&mut self) {
        use std::hash::{Hash, Hasher};
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.pack.match_threshold.to_bits().hash(&mut hasher);
        for entry in &self.pack.entries {
            entry.source_text.hash(&mut hasher);
            entry.aliases.hash(&mut hasher);
            entry.match_threshold.map(f32::to_bits).hash(&mut hasher);
        }
        let key = hasher.finish();
        if key != self.entry_cache_key {
            self.entry_cache_key = key;
            self.entry_cache.get_mut().clear();
        }
    }

    /// [`Pack::find_index`], cached. Entries can change during a frame
    /// before the cache is refreshed, so the index is checked.
    fn entry_for(&self, text: &str) -> Option<usize> {
        let found = *self
            .entry_cache
            .borrow_mut()
            .entry(text.to_string())
            .or_insert_with(|| self.pack.find_index(text));
        found.filter(|&index| index < self.pack.entries.len())
    }

    fn is_translated(&self, entry: Option<usize>) -> bool {
        entry
            .and_then(|index| self.pack.entries.get(index))
            .is_some_and(|entry| !entry.translation.trim().is_empty())
    }

    fn canvas_ui(&mut self, ui: &mut egui::Ui) {
        if self.recorder.is_some() && self.live {
            self.live_canvas(ui);
            return;
        }
        let Some(index) = self.current.filter(|&index| index < self.keyframes.len()) else {
            ui.centered_and_justified(|ui| {
                ui.label(
                    RichText::new(
                        "Press Record and play the game. Each screen with new text becomes a \
                         keyframe you can translate here.",
                    )
                    .weak(),
                );
            });
            return;
        };
        self.edit_toolbar(ui);
        let keyframe_id = self.keyframes[index].id;
        if self.canvas.as_ref().map(|(id, _, _)| *id) != Some(keyframe_id)
            && let Some(frame) = self.keyframes[index].frame()
        {
            let image = egui::ColorImage::from_rgba_unmultiplied(
                [frame.width as usize, frame.height as usize],
                &frame.rgba,
            );
            // Nearest-neighbour magnification keeps pixel fonts crisp when
            // zoomed in.
            let options = egui::TextureOptions {
                magnification: egui::TextureFilter::Nearest,
                ..egui::TextureOptions::LINEAR
            };
            let texture = ui.ctx().load_texture("studio-canvas", image, options);
            self.canvas = Some((keyframe_id, frame, texture));
            self.backdrops.clear();
        }
        let Some((_, frame, texture)) = &self.canvas else {
            return;
        };

        let available = ui.available_size();
        let image_size = texture.size_vec2();
        let (response, painter) = ui.allocate_painter(available, egui::Sense::click_and_drag());
        let area = response.rect;
        let fit = (area.width() / image_size.x).min(area.height() / image_size.y);

        // Scrolling (with or without Ctrl) or pinching zooms around the
        // pointer, never below fitting the whole keyframe; dragging pans.
        // Ctrl + / Ctrl − / Ctrl 0 zoom around the centre, unless the
        // creator is typing a translation.
        if !ui.ctx().wants_keyboard_input() {
            use egui::gui_zoom::kb_shortcuts;
            let pressed = |shortcut| ui.input_mut(|input| input.consume_shortcut(&shortcut));
            let zoom_to =
                if pressed(kb_shortcuts::ZOOM_IN) || pressed(kb_shortcuts::ZOOM_IN_SECONDARY) {
                    Some(self.zoom * ZOOM_STEP)
                } else if pressed(kb_shortcuts::ZOOM_OUT) {
                    Some(self.zoom / ZOOM_STEP)
                } else if pressed(kb_shortcuts::ZOOM_RESET) {
                    Some(1.0)
                } else {
                    None
                };
            if let Some(zoom) = zoom_to {
                let zoom = zoom.clamp(1.0, MAX_ZOOM);
                self.pan *= zoom / self.zoom;
                self.zoom = zoom;
            }
        }
        if let Some(pointer) = response.hover_pos() {
            let (pinch, scroll) = ui.input(|input| (input.zoom_delta(), input.smooth_scroll_delta));
            let zoom_delta = pinch * (scroll.y * SCROLL_ZOOM_SPEED).exp();
            if zoom_delta != 1.0 {
                let zoom = (self.zoom * zoom_delta).clamp(1.0, MAX_ZOOM);
                self.pan = pan_for_zoom(
                    self.pan,
                    fit * self.zoom,
                    fit * zoom,
                    pointer - area.center(),
                );
                self.zoom = zoom;
            }
        }
        // Dragging a box moves its translation; dragging anywhere else pans.
        let mut drag_start = None;
        if response.drag_started()
            && let Some(pointer) = response.interact_pointer_pos()
        {
            drag_start = self
                .canvas_boxes
                .iter()
                .rev()
                .find(|(_, rect)| rect.expand(2.0).contains(pointer))
                .map(|(text, _)| text.clone());
        }
        if response.drag_stopped() {
            self.dragging = None;
        }
        if response.dragged() && drag_start.is_none() {
            let screen_scale = fit * self.zoom * image_size.x / frame.bounds.width.max(1) as f32;
            match self
                .dragging
                .and_then(|index| self.pack.entries.get_mut(index))
            {
                Some(entry) => {
                    let delta = response.drag_delta() / screen_scale;
                    entry.style.offset[0] += delta.x;
                    entry.style.offset[1] += delta.y;
                }
                None => self.pan += response.drag_delta(),
            }
        }
        let scale = fit * self.zoom;
        let size = image_size * scale;
        // Keep the image covering the view; at fit size it stays centred.
        let limit = ((size - area.size()) / 2.0).max(egui::Vec2::ZERO);
        self.pan = self.pan.clamp(-limit, limit);
        if self.zoom > 1.0 {
            let icon = if response.dragged() {
                egui::CursorIcon::Grabbing
            } else {
                egui::CursorIcon::Grab
            };
            response.clone().on_hover_cursor(icon);
        }
        let canvas = egui::Rect::from_center_size(area.center() + self.pan, size);
        painter.image(
            texture.id(),
            canvas,
            egui::Rect::from_min_max(egui::pos2(0.0, 0.0), egui::pos2(1.0, 1.0)),
            Color32::WHITE,
        );

        // Translations are drawn as players will see them, scaled to the
        // canvas; text being edited is replaced by an inline text field.
        let zoom = canvas.width() / frame.bounds.width.max(1) as f32;
        let mut clicked_text = None;
        let mut double_clicked_text = None;
        let mut inline_edit = None;
        // Every box on the canvas, for keyboard navigation.
        let mut boxes = Vec::with_capacity(self.keyframes[index].texts.len());
        // Fresh per frame: the keyframe's texts are always seen in the
        // same order, so sizes are stable.
        let line_heights = LineSizes::default().line_heights(
            self.keyframes[index]
                .texts
                .iter()
                .map(|detected| (detected.pixels.height as f32 * scale, detected.lines)),
        );
        for (text_index, detected) in self.keyframes[index].texts.iter().enumerate() {
            let rect = egui::Rect::from_min_size(
                canvas.min
                    + egui::vec2(detected.pixels.left as f32, detected.pixels.top as f32) * scale,
                egui::vec2(detected.pixels.width as f32, detected.pixels.height as f32) * scale,
            );
            let line_height = line_heights[text_index];
            let entry_index = self.entry_for(&detected.text);
            let entry = entry_index.map(|index| &self.pack.entries[index]);
            let mut style = entry.map(|entry| entry.style.clone()).unwrap_or_default();
            style.font_size = style.font_size.map(|size| size * zoom);
            style.offset = style.offset.map(|value| value * zoom);
            // Where the translation is shown: moved boxes are dragged away
            // from the original text, which is still erased in place.
            let shown = rect.translate(egui::vec2(style.offset[0], style.offset[1]));
            boxes.push((detected.text.clone(), shown, entry_index));
            let screen_bounds = frame.to_screen(
                detected.pixels.left,
                detected.pixels.top,
                detected.pixels.width,
                detected.pixels.height,
            );
            let (background, foreground) = crate::runtime::sample_colors(frame, screen_bounds);
            let backdrop = self
                .backdrops
                .entry(text_index)
                .or_insert_with(|| {
                    crate::runtime::rebuild_backdrop(frame, screen_bounds).map(std::sync::Arc::new)
                })
                .clone();
            let block = |text: String| TextBlock {
                bounds: crate::types::Rect::new(
                    rect.left() as i32,
                    rect.top() as i32,
                    rect.width() as u32,
                    rect.height() as u32,
                ),
                translated_text: Some(text),
                style: Some(style.clone()),
                background: Some(background),
                foreground,
                backdrop: backdrop.clone(),
                ..TextBlock::new(crate::types::Rect::default(), detected.text.clone(), 1.0)
            };
            let editing = entry_index.is_some() && entry_index == self.editing;
            // While typing, the box grows with the translation.
            let mut outline = shown.expand(2.0);
            if editing && let Some(entry) = entry {
                let (field, font_size) = inline_field(ui, shown, entry, &style);
                // Erase the original, and the background under the whole
                // field; the text field draws the translation. The painter
                // pads blocks by 4.
                let in_place = TextStyle {
                    offset: [0.0, 0.0],
                    ..style.clone()
                };
                let inner = field.shrink(4.0);
                let mut erase = block(String::new());
                erase.style = Some(in_place.clone());
                if style.is_moved() {
                    paint_translation(&painter, &erase, egui::Pos2::ZERO, 0.0);
                }
                erase.bounds = crate::types::Rect::new(
                    inner.left() as i32,
                    inner.top() as i32,
                    inner.width() as u32,
                    inner.height() as u32,
                );
                paint_translation(&painter, &erase, egui::Pos2::ZERO, 0.0);
                outline = field;
                inline_edit = entry_index.map(|entry| InlineEdit {
                    entry,
                    field,
                    font_size,
                    background,
                    style: style.clone(),
                });
            } else if !self.show_original
                && let Some(entry) = entry.filter(|entry| !entry.translation.trim().is_empty())
            {
                paint_translation(
                    &painter,
                    &block(entry.translation.clone()),
                    egui::Pos2::ZERO,
                    line_height,
                );
            }

            let selected = entry_index.is_some() && entry_index == self.selected_entry;
            let draft = entry.is_some_and(|entry| entry.draft);
            let color = if selected {
                ACCENT
            } else if draft && self.is_translated(entry_index) {
                DRAFT
            } else if self.is_translated(entry_index) {
                SUCCESS
            } else if entry_index.is_some() {
                WARNING
            } else {
                Color32::from_gray(150)
            };
            let width: f32 = if selected { 2.5 } else { 1.5 };
            painter.rect_stroke(
                outline,
                CornerRadius::same(3),
                egui::Stroke::new(width, color),
                egui::StrokeKind::Outside,
            );
            let pointer_on_text = |pointer: Option<egui::Pos2>| {
                pointer.is_some_and(|pointer| shown.expand(2.0).contains(pointer))
            };
            if pointer_on_text(response.interact_pointer_pos()) {
                if response.double_clicked() {
                    double_clicked_text = Some(detected.text.clone());
                } else if response.clicked() {
                    clicked_text = Some(detected.text.clone());
                }
            }
            if !editing && pointer_on_text(response.hover_pos()) {
                response
                    .clone()
                    .on_hover_cursor(egui::CursorIcon::Move)
                    .on_hover_text(format!(
                        "{}\nDouble-click to translate · drag to move",
                        detected.text
                    ));
            }
        }

        if let Some(text) = double_clicked_text {
            self.select_text(&text);
            self.editing = self.selected_entry;
            self.focus_inline = true;
        } else if let Some(text) = clicked_text {
            self.select_text(&text);
            if self.editing != self.selected_entry {
                self.editing = None;
            }
        } else if response.clicked() {
            // Clicking empty space finishes editing and clears the selection.
            self.editing = None;
            self.selected_entry = None;
        }
        self.keyboard_navigation(ui, &boxes, area);
        self.canvas_boxes = boxes
            .iter()
            .map(|(text, rect, _)| (text.clone(), *rect))
            .collect();
        if let Some(text) = drag_start {
            self.select_text(&text);
            self.dragging = self.selected_entry;
            self.editing = None;
        }
        if ui.input(|input| input.key_pressed(egui::Key::Escape)) {
            if self.editing.is_some() {
                self.editing = None;
            } else {
                self.selected_entry = None;
            }
        }
        // Keyboard navigation may have moved editing to another box; its
        // field appears from the next frame.
        match inline_edit {
            Some(edit) if self.editing == Some(edit.entry) => self.inline_editor(ui, &edit, area),
            _ if self.editing.is_some() => ui.ctx().request_repaint(),
            _ => {}
        }
    }

    /// Arrow keys move the selection to the nearest box in that direction
    /// and Enter starts typing its translation. While typing (the arrows
    /// move the cursor then), Tab and Shift+Tab finish and go on to the
    /// next or previous box in reading order.
    fn keyboard_navigation(
        &mut self,
        ui: &mut egui::Ui,
        boxes: &[(String, egui::Rect, Option<usize>)],
        area: egui::Rect,
    ) {
        if boxes.is_empty() {
            return;
        }
        let current = self
            .selected_entry
            .and_then(|entry| boxes.iter().position(|(_, _, index)| *index == Some(entry)));
        let target = if self.editing.is_some() {
            let (next, previous) = ui.input_mut(|input| {
                (
                    input.consume_key(egui::Modifiers::NONE, egui::Key::Tab),
                    input.consume_key(egui::Modifiers::SHIFT, egui::Key::Tab),
                )
            });
            if !next && !previous {
                return;
            }
            let mut order = (0..boxes.len()).collect::<Vec<_>>();
            order.sort_by(|&a, &b| reading_order(boxes[a].1, boxes[b].1));
            let position = current.and_then(|current| order.iter().position(|&i| i == current));
            let step = |position: usize| {
                if next {
                    (position + 1) % order.len()
                } else {
                    (position + order.len() - 1) % order.len()
                }
            };
            Some(order[position.map_or(0, step)])
        } else {
            if ui.ctx().wants_keyboard_input() {
                return;
            }
            let key = ui.input_mut(|input| {
                [
                    egui::Key::ArrowUp,
                    egui::Key::ArrowDown,
                    egui::Key::ArrowLeft,
                    egui::Key::ArrowRight,
                    egui::Key::Enter,
                ]
                .into_iter()
                .find(|&key| input.consume_key(egui::Modifiers::NONE, key))
            });
            match (key, current) {
                (None, _) => return,
                (Some(egui::Key::Enter), Some(current)) => {
                    self.select_text(&boxes[current].0);
                    self.editing = self.selected_entry;
                    self.focus_inline = true;
                    return;
                }
                // Nothing selected yet: start at the first box.
                (Some(_), None) => {
                    (0..boxes.len()).min_by(|&a, &b| reading_order(boxes[a].1, boxes[b].1))
                }
                (Some(key), Some(current)) => {
                    let direction = match key {
                        egui::Key::ArrowUp => egui::vec2(0.0, -1.0),
                        egui::Key::ArrowDown => egui::vec2(0.0, 1.0),
                        egui::Key::ArrowLeft => egui::vec2(-1.0, 0.0),
                        _ => egui::vec2(1.0, 0.0),
                    };
                    nearest_in_direction(boxes, current, direction).or(Some(current))
                }
            }
        };
        let Some(target) = target else {
            return;
        };
        let was_editing = self.editing.is_some();
        if was_editing {
            self.texts_dirty = true;
        }
        self.select_text(&boxes[target].0);
        if was_editing {
            self.editing = self.selected_entry;
            self.focus_inline = true;
        }
        // Bring the box into view when zoomed in.
        let rect = boxes[target].1.expand(12.0);
        let inside = area.shrink(8.0);
        let mut shift = egui::Vec2::ZERO;
        if rect.left() < inside.left() {
            shift.x = inside.left() - rect.left();
        } else if rect.right() > inside.right() {
            shift.x = inside.right() - rect.right();
        }
        if rect.top() < inside.top() {
            shift.y = inside.top() - rect.top();
        } else if rect.bottom() > inside.bottom() {
            shift.y = inside.bottom() - rect.bottom();
        }
        if shift != egui::Vec2::ZERO {
            self.pan += shift;
            ui.ctx().request_repaint();
        }
    }

    /// Edits a translation in place on the preview, in the entry's own size,
    /// colour and alignment. Enter finishes; Shift+Enter adds a new line.
    fn inline_editor(&mut self, ui: &mut egui::Ui, edit: &InlineEdit, area: egui::Rect) {
        let InlineEdit {
            entry: entry_index,
            field,
            font_size,
            background,
            ref style,
        } = *edit;
        let text_color = style.color.map(rgba).unwrap_or_else(|| {
            let corners = match style.background {
                Background::Solid(color) => [rgba(color); 4],
                _ => background.map(rgba),
            };
            contrasting(average_color(&corners))
        });
        let align = match style.align {
            TextAlign::Left => egui::Align::LEFT,
            TextAlign::Center => egui::Align::Center,
        };
        let finish =
            ui.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter));
        let Some(entry) = self.pack.entries.get_mut(entry_index) else {
            self.editing = None;
            return;
        };
        let font = egui::FontId::proportional(font_size);
        let response = ui
            .scope_builder(egui::UiBuilder::new().max_rect(field), |ui| {
                ui.set_clip_rect(area);
                ui.add_sized(
                    field.size(),
                    egui::TextEdit::multiline(&mut entry.translation)
                        .frame(false)
                        .margin(egui::Margin::same(4))
                        .desired_width(f32::INFINITY)
                        .font(font)
                        .text_color(text_color)
                        .horizontal_align(align)
                        .hint_text(
                            RichText::new(&entry.source_text)
                                .color(text_color.gamma_multiply(0.45)),
                        ),
                )
            })
            .inner;
        // Changing a draft, or confirming it with Enter, marks it checked.
        if response.changed() || finish {
            entry.draft = false;
        }
        if std::mem::take(&mut self.focus_inline) {
            response.request_focus();
        } else if finish || response.lost_focus() {
            self.editing = None;
            self.texts_dirty = true;
        }
    }

    fn edit_toolbar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            let default_threshold = self.pack.match_threshold;
            let selected = self
                .selected_entry
                .filter(|&index| index < self.pack.entries.len());
            let mut delete = false;
            ui.add_enabled_ui(selected.is_some(), |ui| {
                let mut placeholder = PackEntry::default();
                let entry = match selected {
                    Some(index) => &mut self.pack.entries[index],
                    None => &mut placeholder,
                };
                delete = style_toolbar(ui, entry, default_threshold);
            });
            if delete && let Some(index) = selected {
                self.pack.entries.remove(index);
                self.selected_entry = None;
                self.texts_dirty = true;
            }
            // AI-translate just the selected text.
            let busy = self.auto_translate.is_some();
            let source = selected
                .filter(|_| !delete)
                .and_then(|index| self.pack.entries.get(index))
                .map(|entry| entry.source_text.clone());
            if ui
                .add_enabled(
                    source.is_some() && !busy,
                    egui::Button::new(RichText::new("✨").size(16.0)),
                )
                .on_hover_text("AI translate this text (replaces its translation with a draft)")
                .on_disabled_hover_text("Select a text to AI translate it")
                .clicked()
                && let Some(source) = source
            {
                let settings = self.settings.clone();
                self.start_auto_translate(&settings, vec![source], true);
            }
            if !self.narrow {
                ui.separator();
                let hint = if selected.is_some() {
                    "Scroll to zoom · drag to pan"
                } else {
                    "Double-click text to translate it · scroll to zoom · drag to pan"
                };
                ui.label(RichText::new(hint).small().weak());
            }
        });
    }

    fn timeline(&mut self, ui: &mut egui::Ui) {
        if self.keyframes.is_empty() {
            ui.centered_and_justified(|ui| {
                ui.label(RichText::new("Recorded keyframes appear here.").weak());
            });
            return;
        }
        egui::ScrollArea::horizontal()
            .auto_shrink(false)
            .stick_to_right(self.recorder.is_some())
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    if self.recorder.is_some() {
                        self.live_tile(ui);
                    }
                    let mut delete = None;
                    for index in 0..self.keyframes.len() {
                        if self.timeline_item(ui, index) {
                            delete = Some(index);
                        }
                    }
                    // Delete removes the current keyframe when no text box
                    // is selected and nothing is being typed.
                    if delete.is_none()
                        && self.selected_entry.is_none()
                        && !ui.ctx().wants_keyboard_input()
                        && ui.input_mut(|input| {
                            input.consume_key(egui::Modifiers::NONE, egui::Key::Delete)
                        })
                    {
                        delete = self.current;
                    }
                    if let Some(index) = delete {
                        self.delete_keyframe(index);
                    }
                });
            });
    }

    /// Timeline button returning to the live view while recording.
    fn live_tile(&mut self, ui: &mut egui::Ui) {
        let size = egui::vec2(96.0, 108.0);
        let (rect, response) = ui.allocate_exact_size(size, egui::Sense::click());
        let painter = ui.painter();
        painter.rect_filled(rect, CornerRadius::same(6), ui.visuals().faint_bg_color);
        let stroke = if self.live {
            egui::Stroke::new(2.0_f32, ACCENT)
        } else {
            ui.visuals().widgets.noninteractive.bg_stroke
        };
        painter.rect_stroke(
            rect,
            CornerRadius::same(6),
            stroke,
            egui::StrokeKind::Inside,
        );
        painter.text(
            rect.center(),
            egui::Align2::CENTER_CENTER,
            "● LIVE",
            egui::FontId::proportional(15.0),
            Color32::from_rgb(235, 80, 90),
        );
        if response.on_hover_text("Watch the game live").clicked() {
            self.live = true;
        }
    }

    /// Removes a keyframe from the timeline. Translations made on it stay
    /// in the pack.
    fn delete_keyframe(&mut self, index: usize) {
        if index >= self.keyframes.len() {
            return;
        }
        let keyframe = self.keyframes.remove(index);
        self.thumbnails.remove(&keyframe.id);
        if self
            .canvas
            .as_ref()
            .is_some_and(|(id, _, _)| *id == keyframe.id)
        {
            self.canvas = None;
        }
        self.editing = None;
        // Stay at the same place in the timeline: show the next keyframe, or
        // the previous one when the last was deleted.
        self.current = match self.current {
            _ if self.keyframes.is_empty() => None,
            Some(current) if current > index => Some(current - 1),
            Some(current) => Some(current.min(self.keyframes.len() - 1)),
            None => None,
        };
        self.texts_dirty = true;
    }

    /// Draws one timeline thumbnail; returns whether it should be deleted.
    fn timeline_item(&mut self, ui: &mut egui::Ui, index: usize) -> bool {
        let keyframe = &self.keyframes[index];
        let untranslated = keyframe
            .texts
            .iter()
            .filter(|detected| !self.is_translated(self.entry_for(&detected.text)))
            .count();
        let texture = self.thumbnails.entry(keyframe.id).or_insert_with(|| {
            let (width, height, rgba) = &keyframe.thumbnail;
            ui.ctx().load_texture(
                format!("keyframe-{}", keyframe.id),
                egui::ColorImage::from_rgba_unmultiplied([*width as usize, *height as usize], rgba),
                egui::TextureOptions::LINEAR,
            )
        });
        let size = egui::vec2(140.0, 108.0);
        let (rect, response) = ui.allocate_exact_size(size, egui::Sense::click());
        let painter = ui.painter();
        let selected = self.current == Some(index);
        painter.rect_filled(rect, CornerRadius::same(6), ui.visuals().faint_bg_color);
        let image_size = texture.size_vec2();
        let thumb_area = rect.shrink(6.0);
        let scale = (thumb_area.width() / image_size.x).min(thumb_area.height() / image_size.y);
        painter.image(
            texture.id(),
            egui::Rect::from_center_size(thumb_area.center(), image_size * scale),
            egui::Rect::from_min_max(egui::pos2(0.0, 0.0), egui::pos2(1.0, 1.0)),
            Color32::WHITE,
        );
        let stroke = if selected {
            egui::Stroke::new(2.0_f32, ACCENT)
        } else {
            ui.visuals().widgets.noninteractive.bg_stroke
        };
        painter.rect_stroke(
            rect,
            CornerRadius::same(6),
            stroke,
            egui::StrokeKind::Inside,
        );
        let (badge, color) = if untranslated == 0 {
            ("✔".to_string(), SUCCESS)
        } else {
            (untranslated.to_string(), WARNING)
        };
        let center = rect.right_top() + egui::vec2(-14.0, 14.0);
        painter.circle_filled(center, 10.0, color);
        painter.text(
            center,
            egui::Align2::CENTER_CENTER,
            badge,
            egui::FontId::proportional(11.0),
            Color32::BLACK,
        );
        let mut delete = false;
        // A delete button in the corner while the pointer is over the
        // thumbnail.
        let hovered = ui.rect_contains_pointer(rect);
        if hovered {
            let button = egui::Rect::from_center_size(
                rect.left_top() + egui::vec2(14.0, 14.0),
                egui::vec2(20.0, 20.0),
            );
            let close = ui
                .interact(
                    button,
                    ui.id().with(("delete-keyframe", self.keyframes[index].id)),
                    egui::Sense::click(),
                )
                .on_hover_text("Delete keyframe");
            let fill = if close.hovered() {
                Color32::from_rgb(220, 60, 70)
            } else {
                Color32::from_black_alpha(170)
            };
            ui.painter().circle_filled(button.center(), 10.0, fill);
            ui.painter().text(
                button.center(),
                egui::Align2::CENTER_CENTER,
                "×",
                egui::FontId::proportional(11.0),
                Color32::WHITE,
            );
            delete |= close.clicked();
        }
        let response = response.on_hover_text(format!(
            "Keyframe {} · {} texts, {untranslated} untranslated",
            index + 1,
            self.keyframes[index].texts.len()
        ));
        response.context_menu(|ui| {
            if ui.button("Delete keyframe").clicked() {
                delete = true;
                ui.close();
            }
        });
        if response.clicked() && !delete {
            self.current = Some(index);
            self.live = false;
        }
        delete
    }

    fn entry_list(&mut self, ui: &mut egui::Ui) {
        let translated = self
            .texts
            .iter()
            .filter(|summary| self.is_translated(summary.entry))
            .count();
        section(ui, "Text entries", |ui| {
            ui.label(
                RichText::new(format!("{translated} of {} translated", self.texts.len()))
                    .small()
                    .weak(),
            );
            if !self.texts.is_empty() {
                ui.add(
                    egui::ProgressBar::new(translated as f32 / self.texts.len() as f32)
                        .desired_height(4.0)
                        .fill(SUCCESS),
                );
            }
            ui.add(
                egui::TextEdit::singleline(&mut self.filter)
                    .hint_text("Search")
                    .desired_width(f32::INFINITY),
            );
            let filter = self.filter.to_lowercase();
            let mut clicked = None;
            for (index, summary) in self.texts.iter().enumerate() {
                if !filter.is_empty() && !summary.text.to_lowercase().contains(&filter) {
                    continue;
                }
                let done = self.is_translated(summary.entry);
                let draft = summary
                    .entry
                    .and_then(|entry| self.pack.entries.get(entry))
                    .is_some_and(|entry| entry.draft);
                let (icon, color) = if done && draft {
                    ("◆", DRAFT)
                } else if done {
                    ("✔", SUCCESS)
                } else {
                    ("○", WARNING)
                };
                let selected = summary.entry.is_some() && summary.entry == self.selected_entry;
                let translation = summary
                    .entry
                    .and_then(|entry| self.pack.entries.get(entry))
                    .map(|entry| entry.translation.trim())
                    .filter(|translation| !translation.is_empty());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon).color(color));
                    // Translated entries show the translation, with the
                    // original underneath.
                    let mut label = egui::text::LayoutJob::default();
                    let body = egui::TextFormat::simple(
                        egui::FontId::proportional(14.0),
                        ui.visuals().text_color(),
                    );
                    let detail = egui::TextFormat::simple(
                        egui::FontId::proportional(11.0),
                        ui.visuals().weak_text_color(),
                    );
                    let original = summary.text.chars().take(60).collect::<String>();
                    let anchored = summary
                        .entry
                        .and_then(|entry| self.pack.entries.get(entry))
                        .is_some_and(|entry| !entry.anchors.is_empty());
                    let count = format!(
                        "  ×{}{}{}",
                        summary.keyframes.len(),
                        if anchored { " · anchored" } else { "" },
                        if draft { " · draft" } else { "" }
                    );
                    match translation {
                        Some(translation) => {
                            let translation = translation.chars().take(60).collect::<String>();
                            label.append(&translation, 0.0, body);
                            label.append(&format!("\n{original}{count}"), 0.0, detail);
                        }
                        None => {
                            label.append(&original, 0.0, body);
                            label.append(&count, 0.0, detail);
                        }
                    }
                    // A quiet highlight keeps the selected entry readable;
                    // the accent fill would drown its text.
                    let visuals = ui.visuals_mut();
                    visuals.selection.bg_fill = ACCENT.gamma_multiply(0.16);
                    visuals.selection.stroke = egui::Stroke::new(1.0_f32, ACCENT);
                    if ui.selectable_label(selected, label).clicked() {
                        clicked = Some(index);
                    }
                });
            }
            if let Some(index) = clicked {
                let text = self.texts[index].text.clone();
                let keyframe = self.texts[index].keyframes.first().copied();
                self.select_text(&text);
                if let Some(keyframe) = keyframe {
                    self.current = Some(keyframe);
                }
                self.pane = Pane::Canvas;
            }
        });
    }
}

/// Adds OCR spellings of an entry's text that it doesn't know yet, so
/// playback matches however OCR happens to read it.
/// Draft translations being made on a background thread; each arrives on
/// `results` as soon as it is ready.
struct AutoTranslate {
    results: std::sync::mpsc::Receiver<(String, Result<String, String>)>,
    /// Redraft texts that already have a translation.
    replace: bool,
    total: usize,
    done: usize,
    error: Option<String>,
    cancel: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

/// A translation being typed on the canvas.
struct InlineEdit {
    entry: usize,
    field: egui::Rect,
    font_size: f32,
    background: [[u8; 4]; 4],
    style: TextStyle,
}

/// Font size and area of the inline editor for an entry whose original text
/// covers `rect`. The field grows to fit the translation rather than
/// wrapping it; only new lines the creator types (Shift+Enter) add lines. It
/// is at least tall enough for a full line in scripts with marks above and
/// below the letters (Thai, Vietnamese), which the original text may lack.
fn inline_field(
    ui: &egui::Ui,
    rect: egui::Rect,
    entry: &PackEntry,
    style: &TextStyle,
) -> (egui::Rect, f32) {
    let font_size = style
        .font_size
        .unwrap_or_else(|| (rect.height() * 0.8).clamp(11.0, 40.0));
    let shown = if entry.translation.is_empty() {
        &entry.source_text
    } else {
        &entry.translation
    };
    let text_size = ui
        .fonts_mut(|fonts| {
            fonts.layout_no_wrap(
                shown.clone(),
                egui::FontId::proportional(font_size),
                Color32::WHITE,
            )
        })
        .size();
    let size = egui::vec2(
        (rect.width() + 8.0).max(text_size.x + 8.0 + font_size),
        (rect.height() + 8.0)
            .max(font_size * 1.6 + 8.0)
            .max(text_size.y + 8.0 + font_size * 0.6),
    );
    let left = match style.align {
        TextAlign::Left => rect.left() - 4.0,
        TextAlign::Center => rect.center().x - size.x / 2.0,
    };
    (
        egui::Rect::from_min_size(egui::pos2(left, rect.top() - 4.0), size),
        font_size,
    )
}

/// Top-to-bottom, then left-to-right, treating boxes whose tops are within
/// half a line as one row.
fn reading_order(first: egui::Rect, second: egui::Rect) -> std::cmp::Ordering {
    let row = first.height().min(second.height()) / 2.0;
    if (first.top() - second.top()).abs() <= row {
        first.left().total_cmp(&second.left())
    } else {
        first.top().total_cmp(&second.top())
    }
}

/// The box nearest to `current` in `direction`, favouring boxes straight
/// ahead over ones off to the side.
fn nearest_in_direction(
    boxes: &[(String, egui::Rect, Option<usize>)],
    current: usize,
    direction: egui::Vec2,
) -> Option<usize> {
    let from = boxes[current].1;
    boxes
        .iter()
        .enumerate()
        .filter(|(index, _)| *index != current)
        .filter_map(|(index, (_, rect, _))| {
            let offset = rect.center() - from.center();
            let ahead = offset.dot(direction);
            // Overlapping rows or columns don't count as ahead.
            let gap = if direction.x == 0.0 {
                (rect.top() - from.bottom()).max(from.top() - rect.bottom())
            } else {
                (rect.left() - from.right()).max(from.left() - rect.right())
            };
            (ahead > 0.0 && gap > -2.0).then(|| {
                let sideways = (offset - direction * ahead).length();
                (index, ahead + sideways * 3.0)
            })
        })
        .min_by(|a, b| a.1.total_cmp(&b.1))
        .map(|(index, _)| index)
}

fn learn_spellings(entry: &mut PackEntry, variants: &[String]) {
    for variant in variants {
        let normalized = normalize(variant);
        let known = std::iter::once(&entry.source_text)
            .chain(&entry.aliases)
            .any(|spelling| normalize(spelling) == normalized);
        if !known {
            entry.aliases.push(variant.clone());
        }
    }
}

/// Icon buttons for the selected entry's style; each opens a small popup.
/// Returns true when the creator asks to delete the entry.
fn style_toolbar(ui: &mut egui::Ui, entry: &mut PackEntry, default_threshold: f32) -> bool {
    let icon = |text: &str| RichText::new(text).size(16.0);
    let style = &mut entry.style;

    ui.menu_button(icon("🗛"), |ui| {
        ui.set_min_width(220.0);
        let mut fit = style.font_size.is_none();
        if ui.checkbox(&mut fit, "Fit to the original text").changed() {
            style.font_size = if fit { None } else { Some(16.0) };
        }
        if let Some(size) = &mut style.font_size {
            ui.add(egui::Slider::new(size, 8.0..=72.0).suffix(" pt"));
        }
    })
    .response
    .on_hover_text("Font size");

    let text_color = style
        .color
        .map(|[red, green, blue, alpha]| Color32::from_rgba_unmultiplied(red, green, blue, alpha))
        .unwrap_or_else(|| ui.visuals().text_color());
    ui.menu_button(icon("A").strong().color(text_color), |ui| {
        optional_color(
            ui,
            &mut style.color,
            "Automatic (contrasts with the background)",
            [255, 255, 255, 255],
        );
    })
    .response
    .on_hover_text("Text colour");

    ui.menu_button(icon("◯").monospace(), |ui| {
        optional_color(ui, &mut style.outline, "No outline", [0, 0, 0, 255]);
    })
    .response
    .on_hover_text("Outline");

    ui.separator();
    ui.selectable_value(&mut style.align, TextAlign::Left, icon("⬅"))
        .on_hover_text("Align left");
    ui.selectable_value(&mut style.align, TextAlign::Center, icon("↔"))
        .on_hover_text("Centre");
    if ui
        .add_enabled(style.is_moved(), egui::Button::new(icon("⟲")))
        .on_hover_text("Put back where the original text is (drag a box to move it)")
        .on_disabled_hover_text("Drag a box on the preview to move its translation")
        .clicked()
    {
        style.offset = [0.0, 0.0];
    }
    ui.separator();

    ui.menu_button(icon("▣"), |ui| {
        ui.set_min_width(220.0);
        let is_solid = matches!(style.background, Background::Solid(_));
        ui.radio_value(
            &mut style.background,
            Background::Auto,
            "Auto: fill from the surrounding colours",
        );
        if ui.radio(is_solid, "Solid colour").clicked() && !is_solid {
            style.background = Background::Solid([20, 20, 28, 255]);
        }
        ui.radio_value(
            &mut style.background,
            Background::None,
            "None: keep the original visible",
        );
        if let Background::Solid(color) = &mut style.background {
            inline_color_picker(ui, color);
        }
    })
    .response
    .on_hover_text("Background: how the original text is erased");

    ui.menu_button(icon("≈"), |ui| {
        ui.set_min_width(240.0);
        ui.label(
            RichText::new("How similar on-screen text must be to use this entry. Use a stricter value for short words.")
                .small()
                .weak(),
        );
        let mut use_default = entry.match_threshold.is_none();
        if ui
            .checkbox(&mut use_default, format!("Pack default ({:.0}%)", default_threshold * 100.0))
            .changed()
        {
            entry.match_threshold = if use_default { None } else { Some(default_threshold) };
        }
        if let Some(threshold) = &mut entry.match_threshold {
            ui.add(
                egui::Slider::new(threshold, 0.5..=1.0)
                    .custom_formatter(|value, _| format!("{:.0}%", value * 100.0)),
            );
        }
    })
    .response
    .on_hover_text("Match threshold");

    ui.separator();
    ui.button(icon("🗑").color(DANGER))
        .on_hover_text("Delete this entry")
        .clicked()
}

fn optional_color(
    ui: &mut egui::Ui,
    color: &mut Option<[u8; 4]>,
    off_label: &str,
    default: [u8; 4],
) {
    let mut off = color.is_none();
    if ui.checkbox(&mut off, off_label).changed() {
        *color = if off { None } else { Some(default) };
    }
    if let Some(color) = color {
        inline_color_picker(ui, color);
    }
}

/// A colour picker drawn inside the popup, rather than a button that would
/// open a second popup and close the menu.
fn inline_color_picker(ui: &mut egui::Ui, color: &mut [u8; 4]) {
    let mut value = Color32::from_rgba_unmultiplied(color[0], color[1], color[2], color[3]);
    if egui::color_picker::color_picker_color32(
        ui,
        &mut value,
        egui::color_picker::Alpha::OnlyBlend,
    ) {
        *color = value.to_srgba_unmultiplied();
    }
}

/// The pan offset that keeps the image point under `pointer` (relative to
/// the view's centre) in place when the scale changes.
fn pan_for_zoom(
    pan: egui::Vec2,
    old_scale: f32,
    new_scale: f32,
    pointer: egui::Vec2,
) -> egui::Vec2 {
    let image_point = (pointer - pan) / old_scale;
    pointer - image_point * new_scale
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arrows_move_to_the_nearest_box_in_that_direction() {
        let at = |x: f32, y: f32| {
            (
                String::new(),
                egui::Rect::from_min_size(egui::pos2(x, y), egui::vec2(80.0, 20.0)),
                None,
            )
        };
        // A sidebar list with a title to the right of its first item.
        let boxes = [
            at(0.0, 0.0),
            at(0.0, 40.0),
            at(0.0, 80.0),
            at(300.0, 0.0),
            at(300.0, 200.0),
        ];
        let down = egui::vec2(0.0, 1.0);
        assert_eq!(nearest_in_direction(&boxes, 0, down), Some(1));
        assert_eq!(nearest_in_direction(&boxes, 1, down), Some(2));
        assert_eq!(
            nearest_in_direction(&boxes, 0, egui::vec2(1.0, 0.0)),
            Some(3)
        );
        assert_eq!(nearest_in_direction(&boxes, 3, down), Some(4));
        assert_eq!(nearest_in_direction(&boxes, 0, egui::vec2(0.0, -1.0)), None);
        let mut order = (0..boxes.len()).collect::<Vec<_>>();
        order.sort_by(|&a, &b| reading_order(boxes[a].1, boxes[b].1));
        assert_eq!(order, [0, 3, 1, 2, 4]);
    }

    #[test]
    fn zooming_keeps_the_point_under_the_pointer_fixed() {
        let pointer = egui::vec2(120.0, -40.0);
        for (pan, old_scale, new_scale) in [
            (egui::Vec2::ZERO, 0.5, 1.0),
            (egui::vec2(-30.0, 25.0), 1.0, 2.5),
            (egui::vec2(10.0, 10.0), 2.0, 0.8),
        ] {
            let image_point = (pointer - pan) / old_scale;
            let new_pan = pan_for_zoom(pan, old_scale, new_scale, pointer);
            let after = new_pan + image_point * new_scale;
            assert!(
                (after - pointer).length() < 1e-3,
                "{after:?} != {pointer:?}"
            );
        }
    }
}
