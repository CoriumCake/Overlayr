use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use eframe::egui;
use egui::{Color32, CornerRadius, Margin, RichText};

use crate::capture::PlatformCapture;
use crate::config::AppConfig;
use crate::diagnostics::DiagnosticReport;
use crate::installer::InstallPlan;
use crate::marketplace_ui::{MarketplaceAction, MarketplaceView};
use crate::pack::{Background, Pack, TextAlign, TextStyle};
use crate::resources::ResourceMonitor;
use crate::resources_ui::{Resource, resources_ui};
use crate::runtime::{DebugInfo, PipelineSnapshot, PipelineWorker, Verdict};
use crate::services::CaptureBackend;
use crate::studio_ui::{StudioAction, StudioView};
use crate::translation::LibreTranslate;
use crate::types::{Frame, Rect, Source, SourceKind, TextBlock};

pub(crate) const ACCENT: Color32 = Color32::from_rgb(226, 72, 122);
pub(crate) const SUCCESS: Color32 = Color32::from_rgb(80, 200, 120);
pub(crate) const WARNING: Color32 = Color32::from_rgb(235, 170, 60);
pub(crate) const DANGER: Color32 = Color32::from_rgb(235, 90, 90);
/// Below this window width the settings and results share one panel via tabs,
/// which keeps the UI usable in narrow tiled windows.
const NARROW_WIDTH: f32 = 900.0;
const THUMBNAIL_WIDTH: u32 = 320;
/// How often the source picker refreshes its window list and previews.
const RESCAN_INTERVAL: Duration = Duration::from_secs(3);
/// LibreTranslate downloads its language models before it starts serving,
/// so after setup the readiness checks are repeated until it answers.
const TRANSLATION_POLL_INTERVAL: Duration = Duration::from_secs(5);
const TRANSLATION_START_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const OVERLAY_TITLE: &str = "Overlayr overlay";
/// Minimum time between attempts to find and place the overlay window.
const OVERLAY_PLACEMENT_RETRY: Duration = Duration::from_millis(200);
const CARD_WIDTH: f32 = 220.0;
const THUMBNAIL_HEIGHT: f32 = 124.0;

const LANGUAGES: &[(&str, &str)] = &[
    ("en", "English"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("zh-CN", "Chinese (Simplified)"),
    ("zh-TW", "Chinese (Traditional)"),
    ("th", "Thai"),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Component {
    Ocr,
    /// The optional high-accuracy OCR model, offered once OCR works.
    OcrModel,
    Translation,
}

impl Component {
    fn from_check(name: &str) -> Option<Self> {
        match name {
            "OCR" => Some(Self::Ocr),
            "translation service" => Some(Self::Translation),
            _ => None,
        }
    }
}

/// Readiness checks plus, for each failing component, how to install it.
struct Readiness {
    report: DiagnosticReport,
    plans: HashMap<Component, Result<InstallPlan, String>>,
    model_offer: Option<ModelOffer>,
}

/// A better OCR model Overlayr can download for the source language.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ModelOffer {
    /// PaddleOCR, which reads game fonts far better than Tesseract.
    Neural,
    /// Tesseract's high-accuracy model, for languages PaddleOCR can't read.
    Tesseract,
}

enum InstallMessage {
    Info(String),
    Error(String),
}

struct InstallTask {
    component: Component,
    needs_password: bool,
    handle: JoinHandle<Result<(), String>>,
}

/// Tracks the compositor window that shows translations. On Hyprland the
/// overlay is placed through IPC, because Wayland ignores client positions.
#[derive(Default)]
struct OverlayWindow {
    /// Whether the overlay viewport exists. Viewport commands always trigger
    /// a repaint, so closing is only sent when there is something to close.
    open: bool,
    /// Set once the content-protection request has been sent to the current
    /// overlay window, so it isn't re-sent (and repainted) every frame.
    content_protected: Arc<AtomicBool>,
    rule_installed: bool,
    /// Screen areas of other windows above the translated window, where no
    /// translation is drawn.
    occluders: Vec<Rect>,
    occluders_updated: Option<Instant>,
    address: Option<String>,
    placed: Option<Rect>,
    last_attempt: Option<Instant>,
}

/// The main window's two workspaces.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    /// Translate a running application, by machine or with a pack.
    Translate,
    /// Browse, install and publish translation packs.
    Marketplace,
    /// Build translation packs.
    Studio,
}

/// Models each AI provider offers (or why they couldn't be listed), by
/// provider id; shared with the thread that loads them.
type ModelLists = Arc<Mutex<HashMap<&'static str, Result<Vec<String>, String>>>>;

/// Categories of the settings window.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SettingsPage {
    General,
    Translation,
    Ai,
    Recognition,
    Performance,
    Keybinds,
    Setup,
    Advanced,
}

impl SettingsPage {
    const ALL: [Self; 8] = [
        Self::General,
        Self::Translation,
        Self::Ai,
        Self::Recognition,
        Self::Performance,
        Self::Keybinds,
        Self::Setup,
        Self::Advanced,
    ];

    fn label(self) -> &'static str {
        match self {
            Self::General => "General",
            Self::Translation => "Translation",
            Self::Ai => "AI translation",
            Self::Recognition => "Text reading",
            Self::Performance => "Performance",
            Self::Keybinds => "Keybinds",
            Self::Setup => "Setup",
            Self::Advanced => "Advanced",
        }
    }

    fn description(self) -> &'static str {
        match self {
            Self::General => "What to translate, languages and looks",
            Self::Translation => "Machine translation or a pack, and the translation service",
            Self::Ai => "AI drafts in the studio and API keys",
            Self::Recognition => "How text is read and followed on screen",
            Self::Performance => "Speed versus CPU use, for slow and fast computers",
            Self::Keybinds => "Shortcuts that work while you play",
            Self::Setup => "Check and install what Overlayr needs",
            Self::Advanced => "Debugging",
        }
    }
}

/// Views of a running overlay.
#[derive(Clone, Copy, PartialEq, Eq)]
enum LiveTab {
    Live,
    Debug,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DebugTab {
    Pipeline,
    Resources,
}

pub struct OverlayrApp {
    config: AppConfig,
    /// Receives shortcut commands (`overlayr --toggle`).
    control: Option<crate::control::Listener>,
    control_started: bool,
    /// Hides the translations to show the original text.
    translations_hidden: bool,
    /// The shortcuts currently registered with the desktop.
    bound_keybinds: Option<crate::config::Keybinds>,
    keybind_error: Option<String>,
    /// The shortcut whose key is being picked in the settings.
    picking_key: Option<KeybindSlot>,
    /// API keys being typed, by provider; they are saved to their own
    /// files, never to the settings.
    api_key_inputs: HashMap<&'static str, String>,
    /// Models each AI provider offers, once loaded; filled from a
    /// background thread.
    model_lists: ModelLists,
    loading_models: std::collections::HashSet<&'static str>,
    api_key_message: Option<(bool, String)>,
    /// Font sizes in use on the overlay; see [`LineSizes`].
    line_sizes: LineSizes,
    saved_config: AppConfig,
    config_path: PathBuf,
    sources: Vec<Source>,
    selected_source: Option<String>,
    thumbnails: HashMap<String, egui::TextureHandle>,
    scan: Option<JoinHandle<SourceScan>>,
    last_scan: Option<Instant>,
    /// Set by the window watcher when a window opens or closes.
    windows_changed: Arc<AtomicBool>,
    /// Focus moved since the overlay last checked whether the game has it.
    focus_changed: Arc<AtomicBool>,
    /// Another app has focus, so the overlay is hidden. Updated the moment
    /// focus moves, without waiting for the pipeline's next capture.
    game_unfocused: bool,
    watching_windows: bool,
    scan_error: Option<String>,
    worker: Option<PipelineWorker>,
    stopping: Option<JoinHandle<()>>,
    active_source: Option<Source>,
    error: Option<String>,
    readiness: Option<Readiness>,
    checking: Option<JoinHandle<Readiness>>,
    last_check: Option<Instant>,
    installing: Option<InstallTask>,
    install_messages: HashMap<Component, InstallMessage>,
    translation_wait: Option<Instant>,
    overlay_window: OverlayWindow,
    applied_appearance: Option<String>,
    /// The settings window is open, showing this category.
    settings_open: bool,
    settings_page: SettingsPage,
    mode: Mode,
    studio: StudioView,
    marketplace: MarketplaceView,
    /// Window previews as images, for marketplace cover pictures.
    thumbnail_images: HashMap<String, egui::ColorImage>,
    /// A marketplace pack being unlocked before the overlay starts.
    unlocking: Option<(String, std::sync::mpsc::Receiver<Result<Pack, String>>)>,
    /// The last unlocked marketplace pack, kept in memory (never on disk) so
    /// restarting the overlay doesn't ask the server again.
    unlocked: Option<(String, Pack)>,
    /// When the overlay was started from the studio, the pack last sent to
    /// it, so edits can be pushed to the running overlay.
    studio_overlay: Option<Pack>,
    last_exclusion_update: Option<Instant>,
    /// Name of the pack the running overlay uses; `None` for machine
    /// translation.
    running_pack: Option<String>,
    live_tab: LiveTab,
    /// Texture of the frame shown in the debug view, by frame sequence.
    debug_texture: Option<(u64, egui::TextureHandle)>,
    /// Overlayr's own CPU and memory use, shown in the debug view.
    resources: ResourceMonitor,
    debug_tab: DebugTab,
    selected_resource: Resource,
    /// Display name of `config.active_pack`, cached so the pack file isn't
    /// read on every frame: (path, name).
    active_pack_label: (String, String),
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
            control: None,
            control_started: false,
            translations_hidden: false,
            bound_keybinds: None,
            keybind_error: None,
            picking_key: None,
            api_key_inputs: HashMap::new(),
            model_lists: Arc::new(Mutex::new(HashMap::new())),
            loading_models: std::collections::HashSet::new(),
            api_key_message: None,
            line_sizes: LineSizes::default(),
            saved_config: config.clone(),
            config,
            config_path,
            sources: Vec::new(),
            selected_source: None,
            thumbnails: HashMap::new(),
            scan: None,
            last_scan: None,
            windows_changed: Arc::new(AtomicBool::new(false)),
            focus_changed: Arc::new(AtomicBool::new(true)),
            game_unfocused: false,
            watching_windows: false,
            scan_error: None,
            worker: None,
            stopping: None,
            active_source: None,
            error,
            readiness: None,
            checking: None,
            last_check: None,
            installing: None,
            install_messages: HashMap::new(),
            translation_wait: None,
            overlay_window: OverlayWindow::default(),
            applied_appearance: None,
            settings_open: false,
            settings_page: SettingsPage::General,
            mode: Mode::Translate,
            studio: StudioView::default(),
            marketplace: MarketplaceView::default(),
            thumbnail_images: HashMap::new(),
            unlocking: None,
            unlocked: None,
            studio_overlay: None,
            last_exclusion_update: None,
            running_pack: None,
            live_tab: LiveTab::Live,
            debug_texture: None,
            resources: ResourceMonitor::default(),
            debug_tab: DebugTab::Pipeline,
            selected_resource: Resource::default(),
            active_pack_label: (String::new(), "Machine translation".into()),
        };
        app.start_scan();
        app.run_checks();
        app
    }

    /// Refreshes the window list as soon as a window opens or closes.
    fn watch_windows(&mut self, ctx: &egui::Context) {
        if self.watching_windows {
            return;
        }
        self.watching_windows = true;
        #[cfg(unix)]
        if crate::hyprland::is_available() {
            let changed = Arc::clone(&self.windows_changed);
            let ctx = ctx.clone();
            let _ = crate::hyprland::watch_windows({
                let ctx = ctx.clone();
                move || {
                    changed.store(true, Ordering::Relaxed);
                    ctx.request_repaint();
                }
            });
            let focus = Arc::clone(&self.focus_changed);
            let _ = crate::hyprland::watch_focus(move || {
                focus.store(true, Ordering::Relaxed);
                ctx.request_repaint();
            });
        }
    }

    /// Lists sources and captures their previews off the UI thread, since
    /// each preview is a screen capture.
    fn start_scan(&mut self) {
        if self.scan.is_some() {
            return;
        }
        self.scan = Some(std::thread::spawn(scan_sources));
    }

    fn reap_scan(&mut self, ctx: &egui::Context) {
        let finished = self
            .scan
            .as_ref()
            .is_some_and(std::thread::JoinHandle::is_finished);
        if !finished {
            return;
        }
        let Some(Ok(scan)) = self.scan.take().map(JoinHandle::join) else {
            return;
        };
        let first_scan = self.last_scan.is_none();
        self.last_scan = Some(Instant::now());
        match scan.sources {
            Ok(sources) => {
                self.sources = sources;
                self.scan_error = None;
            }
            Err(error) => {
                self.sources.clear();
                self.scan_error = Some(error);
            }
        }
        let known = self
            .picker_sources()
            .map(|source| source.id.clone())
            .collect::<Vec<_>>();
        self.thumbnails.retain(|id, _| known.contains(id));
        self.thumbnail_images.retain(|id, _| known.contains(id));
        for (id, image) in scan.thumbnails {
            self.thumbnail_images.insert(id.clone(), image.clone());
            match self.thumbnails.get_mut(&id) {
                Some(texture) => texture.set(image, egui::TextureOptions::LINEAR),
                None => {
                    let texture = ctx.load_texture(
                        format!("thumbnail:{id}"),
                        image,
                        egui::TextureOptions::LINEAR,
                    );
                    self.thumbnails.insert(id, texture);
                }
            }
        }
        if self
            .selected_source
            .as_ref()
            .is_some_and(|id| !known.contains(id))
        {
            self.selected_source = None;
        }
        if first_scan && self.selected_source.is_none() {
            self.selected_source = self
                .sources
                .iter()
                .find(|source| source.kind == SourceKind::Screen)
                .map(|source| source.id.clone());
        }
    }

    fn picker_sources(&self) -> impl Iterator<Item = &Source> {
        self.sources.iter()
    }

    fn selected(&self) -> Option<&Source> {
        let id = self.selected_source.as_ref()?;
        self.picker_sources().find(|source| &source.id == id)
    }

    fn save(&mut self) -> bool {
        match self.config.save(&self.config_path) {
            Ok(()) => {
                self.error = None;
                self.saved_config = self.config.clone();
                self.run_checks();
                true
            }
            Err(error) => {
                self.error = Some(format!("Could not save settings: {error}"));
                false
            }
        }
    }

    fn is_dirty(&self) -> bool {
        self.config != self.saved_config
    }

    /// Runs the same readiness checks as `--diagnose` off the UI thread, since
    /// they spawn processes and probe the translation endpoint.
    fn run_checks(&mut self) {
        if self.checking.is_some() {
            return;
        }
        let config_path = self.config_path.clone();
        let config = self.config.clone();
        self.checking = Some(std::thread::spawn(move || {
            let report = crate::diagnostics::collect(&config_path, Ok(config.clone()));
            let plans = report
                .checks
                .iter()
                .filter(|check| !check.passed)
                .filter_map(|check| Component::from_check(&check.name))
                .map(|component| {
                    let plan = match component {
                        Component::Ocr | Component::OcrModel => {
                            crate::installer::ocr_plan(&config.source_lang)
                        }
                        Component::Translation => crate::installer::translation_plan(
                            &config.translation_endpoint,
                            &config.source_lang,
                            &config.target_lang,
                        ),
                    };
                    (component, plan)
                })
                .collect();
            let ocr_ready = report
                .checks
                .iter()
                .any(|check| check.name == "OCR" && check.passed);
            let language = &config.source_lang;
            let model_offer = if crate::neural::supports_language(language) {
                (!crate::neural::is_installed()).then_some(ModelOffer::Neural)
            } else {
                (ocr_ready && !crate::ocr::has_accurate_model(language))
                    .then_some(ModelOffer::Tesseract)
            };
            Readiness {
                model_offer,
                report,
                plans,
            }
        }));
    }

    fn reap_checks(&mut self) {
        let finished = self
            .checking
            .as_ref()
            .is_some_and(std::thread::JoinHandle::is_finished);
        if finished && let Some(handle) = self.checking.take() {
            self.readiness = handle.join().ok();
            self.last_check = Some(Instant::now());
        }
    }

    fn component_ready(&self, component: Component) -> bool {
        self.readiness.as_ref().is_some_and(|readiness| {
            readiness
                .report
                .checks
                .iter()
                .all(|check| Component::from_check(&check.name) != Some(component) || check.passed)
        })
    }

    fn install(&mut self, component: Component, plan: InstallPlan) {
        let needs_password = plan.needs_password();
        self.run_install(component, needs_password, move || plan.run());
    }

    fn run_install(
        &mut self,
        component: Component,
        needs_password: bool,
        job: impl FnOnce() -> Result<(), String> + Send + 'static,
    ) {
        if self.installing.is_some() {
            return;
        }
        self.install_messages.remove(&component);
        self.installing = Some(InstallTask {
            component,
            needs_password,
            handle: std::thread::spawn(job),
        });
    }

    /// Offers the better OCR model for the source language.
    fn model_controls(&mut self, ui: &mut egui::Ui) {
        let downloading = self
            .installing
            .as_ref()
            .is_some_and(|task| task.component == Component::OcrModel);
        let offer = self
            .readiness
            .as_ref()
            .and_then(|readiness| readiness.model_offer);
        if downloading {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label(RichText::new("Downloading OCR model…").small());
            });
        } else if let Some(offer) = offer {
            let (label, hover, note) = match offer {
                ModelOffer::Neural => (
                    "Download game text reader",
                    "PaddleOCR scene-text models and ONNX Runtime, saved to your user folder",
                    "An AI text reader trained on text in images and screenshots. It reads \
                     pixel and stylised game fonts far more accurately than Tesseract. \
                     Downloads about 40 MB; no password needed.",
                ),
                ModelOffer::Tesseract => (
                    "Download high-accuracy model",
                    "Tesseract's \"best\" model, saved to your user folder",
                    "Reads game and UI fonts noticeably better at the same speed. \
                     Downloads 15–50 MB; no password needed.",
                ),
            };
            if ui
                .add_enabled(self.installing.is_none(), egui::Button::new(label))
                .on_hover_text(hover)
                .clicked()
            {
                let language = self.config.source_lang.clone();
                self.run_install(Component::OcrModel, false, move || match offer {
                    ModelOffer::Neural => crate::installer::download_neural_ocr(),
                    ModelOffer::Tesseract => crate::installer::download_accurate_model(&language),
                });
            }
            ui.add(egui::Label::new(RichText::new(note).small().weak()).wrap());
        }
        self.install_message_ui(ui, Component::OcrModel);
    }

    fn reap_install(&mut self) {
        let finished = self
            .installing
            .as_ref()
            .is_some_and(|task| task.handle.is_finished());
        if !finished {
            return;
        }
        let Some(task) = self.installing.take() else {
            return;
        };
        let result = task
            .handle
            .join()
            .unwrap_or_else(|_| Err("the installer stopped unexpectedly".into()));
        let message = match (result, task.component) {
            (Ok(()), Component::Ocr) => InstallMessage::Info("Tesseract installed.".into()),
            (Ok(()), Component::OcrModel) => {
                InstallMessage::Info("OCR model installed. It is used from the next start.".into())
            }
            (Ok(()), Component::Translation) => {
                self.translation_wait = Some(Instant::now());
                InstallMessage::Info(
                    "LibreTranslate is starting. The first start downloads language models, \
                     which can take several minutes."
                        .into(),
                )
            }
            (Err(error), _) => InstallMessage::Error(error),
        };
        self.install_messages.insert(task.component, message);
        self.run_checks();
    }

    /// Re-checks until a freshly started translation server answers.
    fn poll_translation_startup(&mut self) {
        let Some(started) = self.translation_wait else {
            return;
        };
        let checked_since_start = self.last_check.is_some_and(|checked| checked > started);
        if checked_since_start && self.component_ready(Component::Translation) {
            self.translation_wait = None;
            self.install_messages.insert(
                Component::Translation,
                InstallMessage::Info("Translation service is ready.".into()),
            );
        } else if started.elapsed() >= TRANSLATION_START_TIMEOUT {
            self.translation_wait = None;
            self.install_messages.insert(
                Component::Translation,
                InstallMessage::Error(format!(
                    "LibreTranslate did not respond within 30 minutes. Check its log with \
                     `docker logs {}`.",
                    crate::installer::LIBRETRANSLATE_CONTAINER
                )),
            );
        } else if self
            .last_check
            .is_none_or(|checked| checked.elapsed() >= TRANSLATION_POLL_INTERVAL)
        {
            self.run_checks();
        }
    }

    fn install_controls(&mut self, ui: &mut egui::Ui, component: Component) {
        let installing = self
            .installing
            .as_ref()
            .filter(|task| task.component == component);
        if let Some(task) = installing {
            let text = if task.needs_password {
                "Installing… approve the password prompt if one appears."
            } else {
                "Installing…"
            };
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label(RichText::new(text).small());
            });
        } else if component == Component::Translation && self.translation_wait.is_some() {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label(RichText::new("Waiting for LibreTranslate to start…").small());
            });
        } else {
            let plan = self
                .readiness
                .as_ref()
                .and_then(|readiness| readiness.plans.get(&component));
            match plan {
                Some(Ok(plan)) => {
                    let plan = plan.clone();
                    ui.horizontal(|ui| {
                        let label = match component {
                            Component::Ocr | Component::OcrModel => "Install OCR",
                            Component::Translation => "Set up translation",
                        };
                        if ui
                            .add_enabled(self.installing.is_none(), primary_button(label, ACCENT))
                            .on_hover_text(plan.command_line())
                            .clicked()
                        {
                            self.install(component, plan.clone());
                        }
                        if ui
                            .button("Copy command")
                            .on_hover_text("Copy the command to run it in a terminal yourself")
                            .clicked()
                        {
                            ui.ctx().copy_text(plan.command_line());
                        }
                    });
                    let password = if plan.needs_password() {
                        " Asks for your password."
                    } else {
                        ""
                    };
                    ui.add(
                        egui::Label::new(
                            RichText::new(format!("{}.{password}", plan.summary))
                                .small()
                                .weak(),
                        )
                        .wrap(),
                    );
                }
                Some(Err(error)) => {
                    ui.add(egui::Label::new(RichText::new(error).small().color(WARNING)).wrap());
                }
                None => {}
            }
        }
        self.install_message_ui(ui, component);
    }

    fn install_message_ui(&self, ui: &mut egui::Ui, component: Component) {
        match self.install_messages.get(&component) {
            Some(InstallMessage::Info(text)) => {
                ui.add(egui::Label::new(RichText::new(text).small().color(SUCCESS)).wrap());
            }
            Some(InstallMessage::Error(text)) => {
                ui.add(egui::Label::new(RichText::new(text).small().color(DANGER)).wrap());
            }
            None => {}
        }
    }

    /// Unlocks an installed marketplace pack in the background, then starts
    /// the overlay with it.
    fn unlock(&mut self, id: String) {
        if self.unlocking.is_some() {
            return;
        }
        let (sender, receiver) = std::sync::mpsc::channel();
        let pack_id = id.clone();
        std::thread::spawn(move || {
            let _ = sender.send(crate::marketplace::unlock(&pack_id));
        });
        self.unlocking = Some((id, receiver));
    }

    fn poll_unlock(&mut self) {
        let Some((id, receiver)) = &self.unlocking else {
            return;
        };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(std::sync::mpsc::TryRecvError::Empty) => return,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                Err("could not unlock the pack".into())
            }
        };
        let id = id.clone();
        self.unlocking = None;
        match result {
            Ok(pack) => {
                self.unlocked = Some((id, pack));
                self.start();
            }
            Err(error) => {
                self.error = Some(format!("Could not use the marketplace pack: {error}"));
            }
        }
    }

    fn start(&mut self) {
        if self.worker.is_some() || self.stopping.is_some() {
            return;
        }
        // A new session may translate a different game or resolution.
        self.line_sizes = LineSizes::default();
        // In the studio, test the pack being edited (unsaved changes
        // included) on the application being edited.
        let from_studio = self.mode == Mode::Studio;
        let source = if from_studio {
            self.studio
                .source_id()
                .and_then(|id| self.picker_sources().find(|source| source.id == id))
                .cloned()
        } else {
            self.selected().cloned()
        };
        let Some(source) = source else {
            self.error = Some(if from_studio {
                "Choose a program or game in the studio first.".into()
            } else {
                "Pick a window or screen to translate first.".into()
            });
            return;
        };
        let pack = if from_studio {
            self.studio.record_anchors();
            Some(self.studio.pack().clone())
        } else if self.config.active_pack.is_empty() {
            None
        } else if let Some(id) = self
            .config
            .active_pack
            .strip_prefix(crate::marketplace::ACTIVE_PREFIX)
        {
            match &self.unlocked {
                Some((unlocked, pack)) if unlocked == id => Some(pack.clone()),
                _ => {
                    self.unlock(id.to_owned());
                    return;
                }
            }
        } else {
            match Pack::load(&self.config.active_pack) {
                Ok(pack) => Some(pack),
                Err(error) => {
                    self.error = Some(format!("Could not open the translation pack: {error}"));
                    return;
                }
            }
        };
        self.studio_overlay = from_studio.then(|| self.studio.pack().clone());
        self.running_pack = pack.as_ref().map(|pack| pack.name.clone());
        if let Some(pack) = &pack
            && !pack
                .entries
                .iter()
                .any(|entry| !entry.translation.trim().is_empty())
        {
            self.error = Some(format!(
                "\"{}\" has no translations yet, so nothing will be translated. Add \
                 translations in the studio, or choose Machine translation in Settings.",
                pack.name
            ));
        }
        let ocr_language = pack
            .as_ref()
            .map(|pack| pack.source_lang.clone())
            .filter(|language| !language.is_empty())
            .unwrap_or_else(|| self.config.source_lang.clone());
        self.config.performance.apply();
        let ocr = crate::ocr::engine(&ocr_language, &self.config.grouping_mode);
        if let Err(error) = ocr.validate_language(&ocr_language) {
            self.error = Some(format!("OCR is not ready: {error}"));
            return;
        }
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
        // Packs translate on their own; only machine translation needs the
        // service.
        if pack.is_none()
            && let Err(error) = translator.check_reachable()
        {
            self.error = Some(format!("Translation service is not ready: {error}"));
            return;
        }
        if !self.save() {
            return;
        }
        self.worker = Some(PipelineWorker::start_with_pack(
            self.config.clone(),
            source.clone(),
            PlatformCapture::default(),
            ocr,
            translator,
            pack,
        ));
        self.active_source = Some(source);
        self.focus_changed.store(true, Ordering::Relaxed);
        self.last_exclusion_update = None;
        self.exclude_own_windows();
        self.prepare_overlay_window();
    }

    /// Registers the Hyprland window rule before the overlay window opens, so
    /// it never gets tiled or takes focus.
    fn prepare_overlay_window(&mut self) {
        #[cfg(unix)]
        if !self.overlay_window.rule_installed && crate::hyprland::is_available() {
            self.overlay_window.rule_installed =
                crate::hyprland::install_overlay_rule(OVERLAY_TITLE).is_ok();
        }
    }

    /// Finds windows stacked above the translated window (e.g. a floating
    /// window opened over a game), so translations never cover them.
    fn update_occluders(&mut self) {
        #[cfg(unix)]
        {
            let overlay = &mut self.overlay_window;
            if overlay
                .occluders_updated
                .is_some_and(|updated| updated.elapsed() < Duration::from_millis(500))
            {
                return;
            }
            overlay.occluders_updated = Some(Instant::now());
            overlay.occluders.clear();
            let Some(address) = self
                .active_source
                .as_ref()
                .and_then(crate::capture::hyprland_address)
            else {
                return;
            };
            let (Ok(clients), Ok(monitors)) =
                (crate::hyprland::clients(), crate::hyprland::monitors())
            else {
                return;
            };
            let Some(target) = clients.iter().find(|client| client.address == address) else {
                return;
            };
            let pid = i64::from(std::process::id());
            overlay.occluders = clients
                .iter()
                .filter(|client| client.address != address)
                .filter(|client| !(client.pid == pid && client.title == OVERLAY_TITLE))
                .filter(|client| client.is_visible(&monitors))
                .filter(|client| overlaps(client.bounds(), target.bounds()))
                .filter(|client| {
                    // Floating windows sit above tiled ones; among floating
                    // windows, the more recently focused one is on top.
                    client.fullscreen != 0
                        || (client.floating
                            && (!target.floating
                                || client.focus_history_id < target.focus_history_id))
                })
                .map(crate::hyprland::Client::bounds)
                .collect();
        }
    }

    /// Keeps the overlay window exactly over the captured area.
    fn place_overlay_window(&mut self, bounds: Rect) {
        #[cfg(unix)]
        {
            let overlay = &mut self.overlay_window;
            if overlay.placed == Some(bounds)
                || !crate::hyprland::is_available()
                || overlay
                    .last_attempt
                    .is_some_and(|attempt| attempt.elapsed() < OVERLAY_PLACEMENT_RETRY)
            {
                return;
            }
            overlay.last_attempt = Some(Instant::now());
            if overlay.address.is_none() {
                overlay.address = crate::hyprland::own_window(OVERLAY_TITLE)
                    .ok()
                    .flatten()
                    .map(|window| window.address);
            }
            let Some(address) = &overlay.address else {
                return;
            };
            match crate::hyprland::place_window(address, bounds) {
                Ok(()) => overlay.placed = Some(bounds),
                // The window may have been closed and reopened; look it up again.
                Err(_) => overlay.address = None,
            }
        }
        #[cfg(not(unix))]
        let _ = bounds;
    }

    /// When a whole screen is captured, Overlayr's own windows are part of
    /// it; tell the pipeline to leave their text alone. Window captures only
    /// contain the chosen window, so they need nothing.
    fn exclude_own_windows(&mut self) {
        let Some(worker) = &self.worker else {
            return;
        };
        if self
            .active_source
            .as_ref()
            .is_none_or(|source| source.kind != SourceKind::Screen)
            || self
                .last_exclusion_update
                .is_some_and(|updated| updated.elapsed() < Duration::from_millis(500))
        {
            return;
        }
        self.last_exclusion_update = Some(Instant::now());
        #[cfg(unix)]
        if crate::hyprland::is_available()
            && let Ok(clients) = crate::hyprland::clients()
        {
            let pid = i64::from(std::process::id());
            worker.set_excluded(
                clients
                    .iter()
                    .filter(|client| client.pid == pid && client.title != OVERLAY_TITLE)
                    .map(crate::hyprland::Client::bounds)
                    .collect(),
            );
        }
    }

    fn close_overlay_window(&mut self, ctx: &egui::Context) {
        if !self.overlay_window.open {
            return;
        }
        ctx.send_viewport_cmd_to(
            egui::ViewportId::from_hash_of("overlayr-translation-overlay"),
            egui::ViewportCommand::Close,
        );
        self.overlay_window.open = false;
        self.overlay_window.content_protected = Arc::default();
        self.overlay_window.address = None;
        self.overlay_window.placed = None;
    }

    fn stop(&mut self) {
        self.studio_overlay = None;
        if let Some(mut worker) = self.worker.take() {
            self.stopping = Some(worker.stop_async());
        }
        self.active_source = None;
    }

    fn reap_stopping_worker(&mut self) {
        let finished = self
            .stopping
            .as_ref()
            .is_some_and(std::thread::JoinHandle::is_finished);
        if finished && let Some(handle) = self.stopping.take() {
            let _ = handle.join();
        }
    }

    fn apply_theme(&mut self, ctx: &egui::Context) {
        if self.applied_appearance.as_deref() == Some(self.config.appearance_mode.as_str()) {
            return;
        }
        if self.applied_appearance.is_none() {
            install_fallback_fonts(ctx);
            // Zoom applies only to the studio's keyframe preview, which
            // handles these shortcuts itself; the interface keeps its size.
            ctx.options_mut(|options| options.zoom_with_keyboard = false);
            ctx.all_styles_mut(|style| {
                style.spacing.item_spacing = egui::vec2(8.0, 8.0);
                style.spacing.button_padding = egui::vec2(10.0, 5.0);
                style.spacing.interact_size.y = 26.0;
                style
                    .text_styles
                    .insert(egui::TextStyle::Body, egui::FontId::proportional(14.0));
                style
                    .text_styles
                    .insert(egui::TextStyle::Button, egui::FontId::proportional(14.0));
                style
                    .text_styles
                    .insert(egui::TextStyle::Heading, egui::FontId::proportional(20.0));
                style
                    .text_styles
                    .insert(egui::TextStyle::Small, egui::FontId::proportional(12.0));
                let visuals = &mut style.visuals;
                visuals.selection.bg_fill = ACCENT;
                visuals.selection.stroke.color = Color32::WHITE;
                visuals.hyperlink_color = ACCENT;
                visuals.slider_trailing_fill = true;
                for widget in [
                    &mut visuals.widgets.noninteractive,
                    &mut visuals.widgets.inactive,
                    &mut visuals.widgets.hovered,
                    &mut visuals.widgets.active,
                    &mut visuals.widgets.open,
                ] {
                    widget.corner_radius = CornerRadius::same(6);
                }
            });
            ctx.style_mut_of(egui::Theme::Dark, |style| {
                style.visuals.panel_fill = Color32::from_rgb(24, 24, 30);
                style.visuals.window_fill = Color32::from_rgb(24, 24, 30);
                style.visuals.faint_bg_color = Color32::from_rgb(32, 32, 40);
                style.visuals.extreme_bg_color = Color32::from_rgb(16, 16, 21);
            });
        }
        let preference = match self.config.appearance_mode.to_ascii_lowercase().as_str() {
            "light" => egui::ThemePreference::Light,
            "system" => egui::ThemePreference::System,
            _ => egui::ThemePreference::Dark,
        };
        ctx.set_theme(preference);
        self.applied_appearance = Some(self.config.appearance_mode.clone());
    }

    fn header_ui(&mut self, ui: &mut egui::Ui, snapshot: Option<&PipelineSnapshot>, narrow: bool) {
        // Below this width labels shorten and secondary details are hidden,
        // so the header never overflows a narrow tiled window.
        let compact = ui.available_width() < 600.0;
        ui.horizontal(|ui| {
            let title_size = if compact { 17.0 } else { 20.0 };
            ui.label(RichText::new("Overlayr").size(title_size).strong());
            ui.add_space(if compact { 2.0 } else { 8.0 });
            ui.selectable_value(&mut self.mode, Mode::Translate, "Translate");
            ui.selectable_value(&mut self.mode, Mode::Marketplace, "Marketplace")
                .on_hover_text("Find translation packs made by others, or share yours");
            let studio_label = if self.studio.is_recording() {
                "● Studio"
            } else {
                "Studio"
            };
            ui.selectable_value(&mut self.mode, Mode::Studio, studio_label)
                .on_hover_text("Build translation packs");
            let (status, color) = if self.worker.is_some() && self.translations_hidden {
                ("Hidden", WARNING)
            } else if self.worker.is_some() {
                ("Running", SUCCESS)
            } else if self.stopping.is_some() {
                ("Stopping…", WARNING)
            } else {
                ("Idle", ui.visuals().weak_text_color())
            };
            // Only a running overlay has a status worth showing.
            if self.worker.is_some() || self.stopping.is_some() {
                ui.add_space(4.0);
                pill(ui, status, color);
            }
            if let Some(snapshot) = snapshot
                && !narrow
            {
                ui.label(
                    RichText::new(format!(
                        "{} · {:.1} FPS",
                        snapshot.status, snapshot.frames_per_second
                    ))
                    .weak(),
                );
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                self.start_stop_button(ui, compact);
                self.settings_button(ui, compact);
            });
        });
    }

    fn start_stop_button(&mut self, ui: &mut egui::Ui, compact: bool) {
        if self.worker.is_some() {
            let label = if compact {
                "■ Stop"
            } else {
                "■ Stop overlay"
            };
            if ui
                .add(primary_button(label, DANGER))
                .on_hover_text("Stop capturing and close the overlay")
                .clicked()
            {
                self.stop();
            }
        } else {
            let label = if self.unlocking.is_some() {
                "Unlocking pack…"
            } else if compact {
                "▶ Start"
            } else {
                "▶ Start overlay"
            };
            let response = ui.add_enabled(
                self.stopping.is_none() && self.unlocking.is_none(),
                primary_button(label, ACCENT),
            );
            if response
                .on_hover_text(if self.mode == Mode::Studio {
                    "Test the pack you're editing on the studio's application; \
                             edits show up live"
                } else {
                    "Saves settings and starts translating the selected source"
                })
                .clicked()
            {
                self.start();
            }
        }
    }

    /// Settings in a window of their own over the app. Escape, a click
    /// outside or × closes it; unsaved changes stay until saved or
    /// reverted.
    fn settings_modal(&mut self, ctx: &egui::Context) {
        let screen = ctx.content_rect();
        let width = (screen.width() - 48.0).clamp(300.0, 820.0);
        let max_height = (screen.height() - 200.0).clamp(200.0, 620.0);
        let running = self.worker.is_some() || self.stopping.is_some();
        let mut close = false;
        let modal = egui::Modal::new(egui::Id::new("settings-modal")).show(ctx, |ui| {
            ui.set_width(width);
            ui.horizontal(|ui| {
                ui.heading("Settings");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("×").on_hover_text("Close (Esc)").clicked() {
                        close = true;
                    }
                });
            });
            if running {
                ui.label(
                    RichText::new("Stop the overlay to change settings.")
                        .color(WARNING)
                        .small(),
                );
            }
            ui.separator();
            let page = self.settings_page;
            let content = |this: &mut Self, ui: &mut egui::Ui| {
                ui.label(RichText::new(page.description()).small().weak());
                ui.add_space(4.0);
                egui::ScrollArea::vertical()
                    .id_salt(("settings-page", page.label()))
                    .max_height(ui.available_height().min(max_height))
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        ui.add_enabled_ui(!running, |ui| this.settings_page(ui, page));
                    });
            };
            if width < 520.0 {
                // Narrow: categories in a row above the page.
                ui.horizontal_wrapped(|ui| {
                    for candidate in SettingsPage::ALL {
                        ui.selectable_value(&mut self.settings_page, candidate, candidate.label());
                    }
                });
                ui.separator();
                let body = egui::vec2(ui.available_width(), max_height);
                ui.allocate_ui_with_layout(body, egui::Layout::top_down(egui::Align::Min), |ui| {
                    ui.set_min_size(body);
                    content(self, ui);
                });
            } else {
                // A fixed-size body: inside a modal, the divider and the
                // scroll area would otherwise grow to the whole screen.
                let body = egui::vec2(ui.available_width(), max_height);
                ui.allocate_ui_with_layout(
                    body,
                    egui::Layout::left_to_right(egui::Align::TOP),
                    |ui| {
                        ui.set_min_size(body);
                        ui.vertical(|ui| {
                            ui.set_width(150.0);
                            for candidate in SettingsPage::ALL {
                                let selected = self.settings_page == candidate;
                                let text = RichText::new(candidate.label()).size(15.0);
                                if ui
                                    .add_sized(
                                        [150.0, 30.0],
                                        egui::Button::selectable(selected, text)
                                            .frame_when_inactive(false),
                                    )
                                    .clicked()
                                {
                                    self.settings_page = candidate;
                                }
                            }
                        });
                        ui.separator();
                        ui.vertical(|ui| content(self, ui));
                    },
                );
            }
            ui.separator();
            self.save_row(ui);
        });
        // Escape is for the shortcut being picked, not for closing.
        let escape_for_picker = self.picking_key.is_some();
        if close || (modal.should_close() && !escape_for_picker) {
            self.settings_open = false;
            self.picking_key = None;
        }
    }

    fn settings_button(&mut self, ui: &mut egui::Ui, compact: bool) {
        let label = match (compact, self.is_dirty()) {
            (true, false) => "⚙".to_string(),
            (true, true) => "⚙ •".to_string(),
            (false, false) => "⚙ Settings".to_string(),
            (false, true) => "⚙ Settings •".to_string(),
        };
        let hover = if self.is_dirty() {
            "Settings (unsaved changes)"
        } else {
            "Settings"
        };
        if ui.button(label).on_hover_text(hover).clicked() {
            self.settings_open = true;
        }
    }

    fn settings_page(&mut self, ui: &mut egui::Ui, page: SettingsPage) {
        match page {
            SettingsPage::General => {
                self.general_settings(ui);
                self.appearance_settings(ui);
            }
            SettingsPage::Translation => self.translation_settings(ui),
            SettingsPage::Ai => self.ai_drafts_section(ui),
            SettingsPage::Recognition => self.recognition_settings(ui),
            SettingsPage::Performance => self.performance_settings(ui),
            SettingsPage::Keybinds => self.keybinds_section(ui),
            SettingsPage::Setup => self.setup_settings(ui),
            SettingsPage::Advanced => self.advanced_settings(ui),
        }
    }

    fn general_settings(&mut self, ui: &mut egui::Ui) {
        section(ui, "Capture", |ui| {
            match self.selected() {
                Some(source) => {
                    ui.add(egui::Label::new(RichText::new(&source.name).strong()).truncate());
                    ui.label(RichText::new(source_detail(source)).small().weak());
                }
                None => {
                    ui.colored_label(WARNING, "Nothing selected");
                }
            }
            ui.label(
                RichText::new("Pick a window or screen from the source picker.")
                    .small()
                    .weak(),
            );
        });

        section(ui, "Languages", |ui| {
            egui::Grid::new("languages")
                .num_columns(2)
                .spacing([12.0, 8.0])
                .show(ui, |ui| {
                    ui.label("From");
                    language_picker(ui, "source_lang", &mut self.config.source_lang);
                    ui.end_row();
                    ui.label("To");
                    language_picker(ui, "target_lang", &mut self.config.target_lang);
                    ui.end_row();
                });
            if ui
                .button("↕ Swap languages")
                .on_hover_text("Swap the source and target languages")
                .clicked()
            {
                std::mem::swap(&mut self.config.source_lang, &mut self.config.target_lang);
            }
        });
    }

    fn translation_settings(&mut self, ui: &mut egui::Ui) {
        section(ui, "Translate with", |ui| {
            if self.active_pack_label.0 != self.config.active_pack {
                let market = self
                    .config
                    .active_pack
                    .strip_prefix(crate::marketplace::ACTIVE_PREFIX);
                let name = if self.config.active_pack.is_empty() {
                    "Machine translation".to_string()
                } else if let Some(id) = market {
                    crate::marketplace::installed_listing(id)
                        .map_or_else(|| "Missing pack".into(), |listing| listing.title)
                } else {
                    Pack::load(&self.config.active_pack)
                        .map(|pack| pack.name)
                        .unwrap_or_else(|_| "Missing pack".into())
                };
                self.active_pack_label = (self.config.active_pack.clone(), name);
            }
            let selected = self.active_pack_label.1.clone();
            egui::ComboBox::from_id_salt("translate_with")
                .width(ui.available_width())
                .selected_text(selected)
                .show_ui(ui, |ui| {
                    ui.selectable_value(
                        &mut self.config.active_pack,
                        String::new(),
                        "Machine translation",
                    );
                    for (path, pack) in crate::pack::library() {
                        let label = format!("{} ({} entries)", pack.name, pack.entries.len());
                        ui.selectable_value(
                            &mut self.config.active_pack,
                            path.to_string_lossy().into_owned(),
                            label,
                        );
                    }
                    for installed in crate::marketplace::installed() {
                        let listing = installed.listing;
                        ui.selectable_value(
                            &mut self.config.active_pack,
                            format!("{}{}", crate::marketplace::ACTIVE_PREFIX, listing.id),
                            format!("🛒 {} (by {})", listing.title, listing.author),
                        );
                    }
                });
            let hint = if self.config.active_pack.is_empty() {
                "Every detected text is translated automatically."
            } else {
                "Only the pack's handmade translations are shown; other text stays as it is."
            };
            ui.label(RichText::new(hint).small().weak());
        });

        if self.config.active_pack.is_empty() {
            self.translation_service_section(ui);
        }
    }

    fn recognition_settings(&mut self, ui: &mut egui::Ui) {
        section(ui, "Recognition", |ui| {
            egui::Grid::new("recognition")
                .num_columns(2)
                .spacing([12.0, 10.0])
                .show(ui, |ui| {
                    ui.label("Group text by");
                    ui.horizontal(|ui| {
                        ui.selectable_value(&mut self.config.grouping_mode, "line".into(), "Line");
                        ui.selectable_value(
                            &mut self.config.grouping_mode,
                            "paragraph".into(),
                            "Paragraph",
                        );
                    });
                    ui.end_row();

                    ui.label("Min. confidence")
                        .on_hover_text("Ignore text the OCR engine is less sure about");
                    ui.add(
                        egui::Slider::new(&mut self.config.minimum_confidence, 0.0..=1.0)
                            .custom_formatter(|value, _| format!("{:.0}%", value * 100.0))
                            .custom_parser(|text| {
                                text.trim_end_matches('%')
                                    .trim()
                                    .parse::<f64>()
                                    .ok()
                                    .map(|value| value / 100.0)
                            }),
                    );
                    ui.end_row();
                });
        });

        section(ui, "Tracking", |ui| {
            ui.checkbox(
                &mut self.config.translate_only_when_changed,
                "Only re-read when new text appears",
            )
            .on_hover_text(
                "Follow known text as it moves instead of reading the whole window every frame",
            );
            ui.add_enabled_ui(self.config.translate_only_when_changed, |ui| {
                egui::Grid::new("tracking")
                    .num_columns(2)
                    .spacing([12.0, 10.0])
                    .show(ui, |ui| {
                        ui.label("Text match").on_hover_text(
                            "How similar re-read text must be to reuse its existing translation. \
                             Lower values tolerate more OCR mistakes.",
                        );
                        ui.add(percent_slider(&mut self.config.match_threshold, 0.5..=1.0));
                        ui.end_row();

                        ui.label("Follow match").on_hover_text(
                            "How closely moved text must look the same to be followed \
                             without reading it again.",
                        );
                        ui.add(percent_slider(&mut self.config.follow_threshold, 0.5..=1.0));
                        ui.end_row();

                        ui.label("Re-read after").on_hover_text(
                            "How much of the window must change, in ways known text doesn't \
                             explain, before text is read again.",
                        );
                        ui.add(
                            egui::Slider::new(&mut self.config.detect_threshold, 0.0..=0.05)
                                .custom_formatter(|value, _| format!("{:.1}%", value * 100.0))
                                .custom_parser(|text| {
                                    text.trim_end_matches('%')
                                        .trim()
                                        .parse::<f64>()
                                        .ok()
                                        .map(|value| value / 100.0)
                                }),
                        );
                        ui.end_row();
                    });
            });
        });
    }

    fn performance_settings(&mut self, ui: &mut egui::Ui) {
        section(ui, "Preset", |ui| {
            ui.horizontal(|ui| {
                for (name, description, interval, preset) in crate::config::PERFORMANCE_PRESETS {
                    let selected = self.config.capture_interval_ms == interval
                        && self.config.performance == preset;
                    if ui
                        .selectable_label(selected, name)
                        .on_hover_text(description)
                        .clicked()
                    {
                        self.config.capture_interval_ms = interval;
                        self.config.performance = preset;
                    }
                }
            });
            let current =
                crate::config::PERFORMANCE_PRESETS
                    .iter()
                    .find(|(_, _, interval, preset)| {
                        self.config.capture_interval_ms == *interval
                            && self.config.performance == *preset
                    });
            let hint = match current {
                Some((_, description, _, _)) => description.to_string(),
                None => "Custom: adjusted below.".into(),
            };
            ui.label(RichText::new(hint).small().weak());
            ui.label(
                RichText::new("Takes effect the next time the overlay or a recording starts.")
                    .small()
                    .weak(),
            );
        });

        let performance = &mut self.config.performance;
        section(ui, "Screen checks", |ui| {
            egui::Grid::new("screen_checks")
                .num_columns(2)
                .spacing([12.0, 10.0])
                .show(ui, |ui| {
                    ui.label("Check every").on_hover_text(
                        "How often the screen is checked for new text. Lower values react \
                         faster but use more CPU.",
                    );
                    ui.add(
                        egui::Slider::new(&mut self.config.capture_interval_ms, 16..=1000)
                            .logarithmic(true)
                            .suffix(" ms"),
                    );
                    ui.end_row();
                });
            ui.label(
                RichText::new(format!(
                    "Up to {:.0} checks per second",
                    1000.0 / self.config.capture_interval_ms.max(1) as f32
                ))
                .small()
                .weak(),
            );
            ui.add_space(4.0);
            ui.checkbox(&mut performance.fast_clear, "Clear translations quickly")
                .on_hover_text(
                    "While translations are shown, check more often so they disappear as soon \
                     as their text does.",
                );
            ui.add_enabled_ui(performance.fast_clear, |ui| {
                ui.horizontal(|ui| {
                    ui.label("Check every");
                    ui.add(
                        egui::Slider::new(&mut performance.fast_clear_interval_ms, 16..=200)
                            .logarithmic(true)
                            .suffix(" ms"),
                    );
                });
            });
            ui.checkbox(
                &mut performance.follow_highlights,
                "Follow hover highlights",
            )
            .on_hover_text(
                "Redraw a translation when the picture under it changes, e.g. a highlighted \
                     menu row, so its background keeps matching. Off saves CPU but may leave \
                     mismatched boxes while hovering.",
            );
        });

        section(ui, "Text reading", |ui| {
            ui.checkbox(
                &mut performance.wait_for_settle,
                "Wait for text to finish appearing",
            )
            .on_hover_text(
                "Read new text once it stops fading or sliding in. Off reads sooner but may \
                     misread text mid-animation.",
            );
            ui.checkbox(&mut performance.rest_between_reads, "Pause between reads")
                .on_hover_text(
                    "After reading the screen, wait about as long before reading again. Keeps \
                     constantly animated games from using several CPU cores.",
                );
            egui::Grid::new("ocr_threads")
                .num_columns(2)
                .spacing([12.0, 10.0])
                .show(ui, |ui| {
                    ui.label("OCR threads").on_hover_text(
                        "How many CPU threads text reading uses at once. More reads faster but \
                         takes more CPU from the game; above 4 it barely gets faster.",
                    );
                    ui.add(egui::Slider::new(&mut performance.ocr_threads, 1..=8));
                    ui.end_row();
                });
            ui.checkbox(&mut performance.low_priority, "Let the game go first")
                .on_hover_text(
                    "Run capture and text reading at low priority, so they yield to the game \
                     instead of making it stutter.",
                );
        });
    }

    fn appearance_settings(&mut self, ui: &mut egui::Ui) {
        section(ui, "Appearance", |ui| {
            ui.horizontal(|ui| {
                for mode in ["System", "Dark", "Light"] {
                    let selected = self.config.appearance_mode.eq_ignore_ascii_case(mode);
                    if ui.selectable_label(selected, mode).clicked() {
                        self.config.appearance_mode = mode.into();
                    }
                }
            });
        });
    }

    fn advanced_settings(&mut self, ui: &mut egui::Ui) {
        section(ui, "Debug", |ui| {
            ui.checkbox(&mut self.config.debug, "Debug mode");
            ui.label(
                RichText::new(
                    "Shows what the overlay is doing: the text OCR found and why it was \
                     translated or dropped, timings and an event log. Takes effect the next \
                     time the overlay starts.",
                )
                .small()
                .weak(),
            );
        });
    }

    fn ai_drafts_section(&mut self, ui: &mut egui::Ui) {
        section(ui, "AI translation", |ui| {
            ui.label(
                RichText::new(
                    "Studio's ✨ AI translate drafts translations for you to check and fix. \
                     AI services write natural translations that fit the game; \
                     LibreTranslate is free and offline but word for word.",
                )
                .small()
                .weak(),
            );
            ui.add_space(4.0);
            ui.label(RichText::new("Draft with").strong());
            ui.radio_value(
                &mut self.config.draft_translator,
                "libretranslate".to_string(),
                "LibreTranslate (free, offline)",
            );
            for provider in crate::ai::Provider::ALL {
                let ready = crate::ai::api_key(provider).is_some();
                let label = if ready {
                    provider.label().to_string()
                } else {
                    format!("{} (needs an API key)", provider.label())
                };
                ui.radio_value(
                    &mut self.config.draft_translator,
                    provider.id().to_string(),
                    label,
                );
            }
            ui.add_space(8.0);
            ui.label(RichText::new("API keys").strong());
            for provider in crate::ai::Provider::ALL {
                self.provider_key_row(ui, provider);
            }
            if let Some((is_error, message)) = &self.api_key_message {
                let color = if *is_error { DANGER } else { SUCCESS };
                ui.label(RichText::new(message).small().color(color));
            }
            ui.label(
                RichText::new(
                    "Keys are stored in ~/.config/overlayr/keys/, readable only by you, never in \
                     the settings file. Each drafted screen costs a few cents or less.",
                )
                .small()
                .weak(),
            );
        });
    }

    /// The model to draft with: typed, or picked from the provider's list.
    fn model_row(&mut self, ui: &mut egui::Ui, provider: crate::ai::Provider) {
        let loaded = self
            .model_lists
            .lock()
            .ok()
            .and_then(|lists| lists.get(provider.id()).cloned());
        if loaded.is_some() {
            self.loading_models.remove(provider.id());
        }
        let mut model = self
            .config
            .ai_models
            .get(provider.id())
            .cloned()
            .unwrap_or_default();
        let mut edited = false;
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("Model").small());
            edited |= ui
                .add(
                    egui::TextEdit::singleline(&mut model)
                        .hint_text(provider.default_model())
                        .desired_width(170.0),
                )
                .changed();
            if let Some(Ok(models)) = &loaded {
                egui::ComboBox::from_id_salt(("models", provider.id()))
                    .selected_text("Choose…")
                    .width(160.0)
                    .show_ui(ui, |ui| {
                        for candidate in models {
                            if ui
                                .selectable_label(model == *candidate, candidate)
                                .clicked()
                            {
                                model = candidate.clone();
                                edited = true;
                            }
                        }
                    });
            }
            let loading = self.loading_models.contains(provider.id());
            if loading {
                ui.spinner();
            } else if ui
                .small_button(if loaded.is_some() {
                    "Reload"
                } else {
                    "Load models"
                })
                .on_hover_text("List the models your key can use")
                .clicked()
            {
                self.loading_models.insert(provider.id());
                let lists = Arc::clone(&self.model_lists);
                let ctx = ui.ctx().clone();
                std::thread::spawn(move || {
                    let result = crate::ai::list_models(provider);
                    if let Ok(mut lists) = lists.lock() {
                        lists.insert(provider.id(), result);
                    }
                    ctx.request_repaint();
                });
                if let Ok(mut lists) = self.model_lists.lock() {
                    lists.remove(provider.id());
                }
            }
        });
        if let Some(Err(error)) = &loaded {
            ui.label(RichText::new(error).small().color(DANGER));
        }
        if edited {
            if model.trim().is_empty() {
                self.config.ai_models.remove(provider.id());
            } else {
                self.config
                    .ai_models
                    .insert(provider.id().to_string(), model);
            }
        }
    }

    /// One provider's key and model.
    fn provider_key_row(&mut self, ui: &mut egui::Ui, provider: crate::ai::Provider) {
        let from_environment = std::env::var_os(provider.key_variable()).is_some();
        let saved = crate::ai::api_key(provider).is_some();
        let status = if from_environment {
            format!("✔ from {}", provider.key_variable())
        } else if saved {
            "✔ saved".to_string()
        } else {
            "not set".to_string()
        };
        let header = RichText::new(format!("{} · {status}", provider.label()));
        egui::CollapsingHeader::new(header)
            .id_salt(("ai-provider", provider.id()))
            .show(ui, |ui| {
                let input = self.api_key_inputs.entry(provider.id()).or_default();
                ui.horizontal(|ui| {
                    ui.add(
                        egui::TextEdit::singleline(input)
                            .password(true)
                            .hint_text(if saved { "Type to replace" } else { "API key" })
                            .desired_width(200.0),
                    );
                    if ui
                        .add_enabled(!input.trim().is_empty(), egui::Button::new("Save"))
                        .clicked()
                    {
                        self.api_key_message =
                            Some(match crate::ai::save_api_key(provider, input) {
                                Ok(()) => (false, format!("{} key saved", provider.label())),
                                Err(error) => (true, error),
                            });
                        input.clear();
                    }
                    if saved && !from_environment && ui.small_button("Remove").clicked() {
                        self.api_key_message = Some(match crate::ai::save_api_key(provider, "") {
                            Ok(()) => (false, format!("{} key removed", provider.label())),
                            Err(error) => (true, error),
                        });
                    }
                });
                self.model_row(ui, provider);
                ui.hyperlink_to(RichText::new("Get a key").small(), provider.key_page());
            });
    }

    /// Runs commands sent by keyboard shortcuts.
    fn handle_commands(&mut self, ctx: &egui::Context) {
        if !self.control_started {
            self.control_started = true;
            let wake = ctx.clone();
            match crate::control::Listener::start(move || wake.request_repaint()) {
                Ok(listener) => self.control = Some(listener),
                Err(error) => self.keybind_error = Some(format!("Shortcuts are off: {error}")),
            }
        }
        let commands = self
            .control
            .as_ref()
            .map(crate::control::Listener::received)
            .unwrap_or_default();
        for command in commands {
            match command {
                crate::control::Command::ToggleOverlay => {
                    if self.worker.is_some() {
                        self.stop();
                    } else if self.stopping.is_none() {
                        self.translations_hidden = false;
                        self.start();
                    }
                }
                crate::control::Command::ToggleTranslations => {
                    self.translations_hidden = !self.translations_hidden;
                }
            }
        }
    }

    /// Registers the configured shortcuts with the desktop (Hyprland), and
    /// removes ones that changed.
    fn apply_keybinds(&mut self) {
        if self.bound_keybinds.as_ref() == Some(&self.config.keybinds) {
            return;
        }
        #[cfg(unix)]
        if crate::hyprland::is_available() {
            let previous = self
                .bound_keybinds
                .take()
                .unwrap_or_else(|| crate::config::Keybinds {
                    toggle_overlay: String::new(),
                    toggle_translations: String::new(),
                });
            for keys in [&previous.toggle_overlay, &previous.toggle_translations] {
                if !keys.is_empty() {
                    let _ = crate::hyprland::unbind(keys);
                }
            }
            let program = std::env::current_exe()
                .map(|path| path.to_string_lossy().into_owned())
                .unwrap_or_else(|_| "overlayr".into());
            let mut errors = Vec::new();
            for (keys, command) in [
                (
                    &self.config.keybinds.toggle_overlay,
                    crate::control::Command::ToggleOverlay,
                ),
                (
                    &self.config.keybinds.toggle_translations,
                    crate::control::Command::ToggleTranslations,
                ),
            ] {
                if keys.is_empty() {
                    continue;
                }
                let line = format!(
                    "'{}' {}",
                    program.replace('\'', "'\\''"),
                    command.argument()
                );
                if let Err(error) = crate::hyprland::bind_command(keys, &line) {
                    errors.push(format!("{keys}: {error}"));
                }
            }
            self.keybind_error = (!errors.is_empty())
                .then(|| format!("Couldn't set shortcuts: {}", errors.join("; ")));
        } else {
            self.keybind_error = Some(
                "Shortcuts are set through Hyprland, which isn't running. Bind keys to \
                 `overlayr --toggle` in your desktop's settings instead."
                    .into(),
            );
        }
        self.bound_keybinds = Some(self.config.keybinds.clone());
    }

    fn keybinds_section(&mut self, ui: &mut egui::Ui) {
        section(ui, "Keybinds", |ui| {
            ui.label(
                RichText::new(
                    "Shortcuts work everywhere, even while you play. Overlayr must be open.",
                )
                .small()
                .weak(),
            );
            ui.add_space(4.0);
            for slot in [KeybindSlot::ToggleOverlay, KeybindSlot::ToggleTranslations] {
                self.keybind_row(ui, slot);
            }
            if let Some(error) = &self.keybind_error {
                ui.label(RichText::new(error).small().color(DANGER));
            }
        });
    }

    fn keybind_row(&mut self, ui: &mut egui::Ui, slot: KeybindSlot) {
        let picking = self.picking_key == Some(slot);
        let keys = slot.keys_mut(&mut self.config.keybinds);
        ui.horizontal_wrapped(|ui| {
            ui.label(slot.label());
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if !keys.is_empty() && ui.small_button("Off").on_hover_text("Turn off").clicked() {
                    keys.clear();
                }
                let shown = if picking {
                    "Press a key…".to_string()
                } else if keys.is_empty() {
                    "Off".to_string()
                } else {
                    keys.clone()
                };
                if ui
                    .add(egui::Button::new(RichText::new(shown).monospace()).selected(picking))
                    .on_hover_text("Click, then press the shortcut")
                    .clicked()
                {
                    self.picking_key = if picking { None } else { Some(slot) };
                    if keys.is_empty() {
                        *keys = slot
                            .keys_mut(&mut crate::config::Keybinds::default())
                            .clone();
                    }
                }
            });
        });
        if !picking {
            return;
        }
        // egui can't see the Super key, so modifiers are chosen here and
        // the key itself is pressed.
        let mut parts = keys
            .split('+')
            .map(str::trim)
            .map(str::to_uppercase)
            .collect::<Vec<_>>();
        parts.pop();
        ui.horizontal_wrapped(|ui| {
            for modifier in ["SUPER", "CTRL", "ALT", "SHIFT"] {
                let mut on = parts.iter().any(|part| part == modifier);
                if ui.toggle_value(&mut on, modifier).changed() {
                    if on {
                        parts.push(modifier.to_string());
                    } else {
                        parts.retain(|part| part != modifier);
                    }
                }
            }
        });
        let pressed = ui.input(|input| {
            input.events.iter().find_map(|event| match event {
                egui::Event::Key {
                    key, pressed: true, ..
                } => hyprland_key(*key),
                _ => None,
            })
        });
        let order = ["SUPER", "CTRL", "ALT", "SHIFT"];
        parts.sort_by_key(|part| order.iter().position(|known| known == part));
        let current_key = keys
            .rsplit('+')
            .next()
            .map(str::trim)
            .filter(|key| !key.is_empty())
            .map(str::to_string);
        if let Some(key) = pressed.clone().or(current_key) {
            parts.push(key);
            let combined = parts.join(" + ");
            if *keys != combined {
                *keys = combined;
            }
        }
        if pressed.is_some() {
            self.picking_key = None;
        }
        ui.label(
            RichText::new(
                "Pick modifiers, then press a key. Use a modifier so the game keeps its keys.",
            )
            .small()
            .weak(),
        );
    }

    fn translation_service_section(&mut self, ui: &mut egui::Ui) {
        section(ui, "Translation service", |ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.config.translation_endpoint)
                    .desired_width(f32::INFINITY)
                    .hint_text("http://localhost:5000/translate"),
            );
            let key_hint = if std::env::var_os("OVERLAYR_TRANSLATION_API_KEY").is_some() {
                "API key loaded from OVERLAYR_TRANSLATION_API_KEY"
            } else {
                "No API key set. Use OVERLAYR_TRANSLATION_API_KEY if the endpoint needs one."
            };
            ui.label(RichText::new(key_hint).small().weak());
        });
    }

    fn save_row(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if self.is_dirty() {
                if ui
                    .add(primary_button("Save changes", ACCENT))
                    .on_hover_text("Ctrl+S")
                    .clicked()
                {
                    self.save();
                }
                if ui.button("Revert").clicked() {
                    self.config = self.saved_config.clone();
                }
            } else {
                ui.label(RichText::new("✔ All changes saved").small().weak());
            }
        });
        ui.add_space(8.0);
    }

    fn main_ui(&mut self, ui: &mut egui::Ui, snapshot: Option<PipelineSnapshot>) {
        if let Some(error) = self.error.clone() {
            let dismissed = banner(ui, &error, true);
            if dismissed {
                self.error = None;
            }
        }
        match snapshot {
            Some(snapshot) => {
                if snapshot.debug.is_some() {
                    ui.horizontal(|ui| {
                        ui.selectable_value(&mut self.live_tab, LiveTab::Live, "Live");
                        ui.selectable_value(&mut self.live_tab, LiveTab::Debug, "Debug");
                    });
                    ui.add_space(4.0);
                }
                match (&snapshot.debug, self.live_tab) {
                    (Some(debug), LiveTab::Debug) => {
                        ui.horizontal(|ui| {
                            ui.selectable_value(
                                &mut self.debug_tab,
                                DebugTab::Pipeline,
                                "Pipeline",
                            );
                            ui.selectable_value(
                                &mut self.debug_tab,
                                DebugTab::Resources,
                                "Resources",
                            );
                        });
                        ui.separator();
                        match self.debug_tab {
                            DebugTab::Pipeline => {
                                let debug = std::sync::Arc::clone(debug);
                                self.debug_ui(ui, &snapshot, &debug);
                            }
                            DebugTab::Resources => {
                                egui::ScrollArea::vertical()
                                    .auto_shrink(false)
                                    .show(ui, |ui| {
                                        resources_ui(
                                            ui,
                                            &self.resources,
                                            &mut self.selected_resource,
                                        );
                                    });
                            }
                        }
                    }
                    _ => live_ui(ui, snapshot, self.running_pack.as_deref()),
                }
            }
            None => self.idle_ui(ui),
        }
    }

    /// Everything debug mode collects: the last OCR frame with each text
    /// outlined by what happened to it, timings and an event log.
    fn debug_ui(&mut self, ui: &mut egui::Ui, snapshot: &PipelineSnapshot, debug: &DebugInfo) {
        egui::ScrollArea::vertical()
            .auto_shrink(false)
            .show(ui, |ui| {
                ui.horizontal_wrapped(|ui| {
                    stat(ui, "Tracker", debug.decision);
                    stat(ui, "Tracked texts", &debug.tracked.to_string());
                    stat(ui, "OCR runs", &snapshot.detections.to_string());
                    stat(ui, "Capture", &format!("{:.0} ms", debug.capture_ms));
                    stat(ui, "Tracking", &format!("{:.1} ms", debug.track_ms));
                    stat(ui, "OCR", &format!("{:.0} ms", debug.ocr_ms));
                    stat(ui, "Translation", &format!("{:.0} ms", debug.translate_ms));
                });
                ui.add_space(8.0);
                ui.heading("Last OCR frame");
                ui.horizontal_wrapped(|ui| {
                    for verdict in [
                        Verdict::Translated,
                        Verdict::NotTranslated,
                        Verdict::LowConfidence,
                        Verdict::InvalidText,
                        Verdict::Noise,
                        Verdict::OwnWindow,
                        Verdict::OverlayEcho,
                    ] {
                        ui.label(RichText::new("■").color(verdict_color(verdict)));
                        ui.label(RichText::new(verdict.label()).small());
                        ui.add_space(6.0);
                    }
                });
                match &debug.frame {
                    Some(frame) => self.debug_frame(ui, frame, &debug.texts),
                    None => {
                        ui.label(RichText::new("OCR hasn't run yet.").weak());
                    }
                }

                ui.add_space(8.0);
                ui.heading(format!("Detected text ({})", debug.texts.len()));
                egui::Grid::new("debug_texts")
                    .num_columns(4)
                    .striped(true)
                    .spacing([12.0, 4.0])
                    .show(ui, |ui| {
                        for text in &debug.texts {
                            ui.label(
                                RichText::new(text.verdict.label())
                                    .small()
                                    .color(verdict_color(text.verdict)),
                            );
                            ui.add(
                                egui::Label::new(text.text.chars().take(60).collect::<String>())
                                    .truncate(),
                            );
                            ui.label(
                                RichText::new(format!("{:.0}%", text.confidence * 100.0)).small(),
                            );
                            ui.add(
                                egui::Label::new(RichText::new(&text.note).small().weak())
                                    .truncate(),
                            );
                            ui.end_row();
                        }
                    });

                ui.add_space(8.0);
                ui.heading("Event log");
                egui::Frame::new()
                    .fill(ui.visuals().extreme_bg_color)
                    .corner_radius(CornerRadius::same(6))
                    .inner_margin(Margin::same(8))
                    .show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        for line in debug.log.iter().rev() {
                            ui.label(RichText::new(line).monospace().small());
                        }
                    });
            });
    }

    fn debug_frame(
        &mut self,
        ui: &mut egui::Ui,
        frame: &Frame,
        texts: &[crate::runtime::DebugText],
    ) {
        if self.debug_texture.as_ref().map(|(sequence, _)| *sequence) != Some(frame.sequence) {
            let image = egui::ColorImage::from_rgba_unmultiplied(
                [frame.width as usize, frame.height as usize],
                &frame.rgba,
            );
            let texture = ui
                .ctx()
                .load_texture("debug-frame", image, egui::TextureOptions::LINEAR);
            self.debug_texture = Some((frame.sequence, texture));
        }
        let Some((_, texture)) = &self.debug_texture else {
            return;
        };
        let image_size = texture.size_vec2();
        let width = ui.available_width();
        let scale = (width / image_size.x).min(520.0 / image_size.y);
        let (rect, response) = ui.allocate_exact_size(image_size * scale, egui::Sense::hover());
        let painter = ui.painter_at(rect);
        painter.image(
            texture.id(),
            rect,
            egui::Rect::from_min_max(egui::pos2(0.0, 0.0), egui::pos2(1.0, 1.0)),
            Color32::WHITE,
        );
        let mut hovered = None;
        for text in texts {
            let pixels = frame.to_pixels(text.bounds);
            let area = egui::Rect::from_min_size(
                rect.min + egui::vec2(pixels.left as f32, pixels.top as f32) * scale,
                egui::vec2(pixels.width as f32, pixels.height as f32) * scale,
            );
            painter.rect_stroke(
                area,
                CornerRadius::same(2),
                egui::Stroke::new(1.5_f32, verdict_color(text.verdict)),
                egui::StrokeKind::Outside,
            );
            if response
                .hover_pos()
                .is_some_and(|pointer| area.contains(pointer))
            {
                hovered = Some(text);
            }
        }
        if let Some(text) = hovered {
            response.on_hover_text(format!(
                "{}\n{} · {:.0}% confidence\n{}",
                text.text,
                text.verdict.label(),
                text.confidence * 100.0,
                text.note
            ));
        }
    }

    fn idle_ui(&mut self, ui: &mut egui::Ui) {
        egui::ScrollArea::vertical()
            .auto_shrink(false)
            .show(ui, |ui| {
                self.picker_ui(ui);
                // Setup lives in Settings; problems are pointed out here.
                let problems = self.readiness.as_ref().map(|readiness| {
                    readiness
                        .report
                        .checks
                        .iter()
                        .filter(|check| !check.passed)
                        .count()
                });
                if let Some(problems) = problems.filter(|count| *count > 0) {
                    ui.add_space(8.0);
                    ui.horizontal_wrapped(|ui| {
                        ui.label(
                            RichText::new(format!(
                                "⚠ Setup needed: {problems} thing{} to fix before translating",
                                if problems == 1 { "" } else { "s" }
                            ))
                            .color(WARNING),
                        );
                        if ui.button("Open setup").clicked() {
                            self.settings_open = true;
                            self.settings_page = SettingsPage::Setup;
                        }
                    });
                }
            });
    }

    /// Checks that capture, OCR and translation are ready, with installers
    /// for what is missing.
    fn setup_settings(&mut self, ui: &mut egui::Ui) {
        let healthy = self
            .readiness
            .as_ref()
            .map(|readiness| readiness.report.is_healthy());
        let title = match healthy {
            Some(true) => "Ready to translate",
            Some(false) => "Setup needed",
            None => "Checking your setup…",
        };
        section(ui, title, |ui| {
            let checks = self
                .readiness
                .as_ref()
                .map(|readiness| readiness.report.checks.clone());
            match checks {
                Some(checks) => {
                    for check in checks {
                        check_row(ui, &check.name, check.passed, &check.detail, |ui| {
                            if let Some(component) = Component::from_check(&check.name)
                                && (!check.passed || self.install_messages.contains_key(&component))
                            {
                                self.install_controls(ui, component);
                            }
                            if check.name == "OCR" {
                                self.model_controls(ui);
                            }
                        });
                    }
                }
                None => {
                    ui.horizontal(|ui| {
                        ui.spinner();
                        ui.label("Running checks…");
                    });
                }
            }
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                let checking = self.checking.is_some();
                if ui
                    .add_enabled(!checking, egui::Button::new("Check again"))
                    .clicked()
                {
                    self.run_checks();
                }
                if checking && self.readiness.is_some() {
                    ui.spinner();
                }
            });
        });
    }

    fn picker_ui(&mut self, ui: &mut egui::Ui) {
        // Wraps so the buttons drop below the heading in narrow windows.
        ui.horizontal_wrapped(|ui| {
            ui.heading("Choose what to translate");
            ui.add_space(8.0);
            // Background refreshes run every few seconds; only show progress
            // for the first scan so the header doesn't flicker.
            if self.last_scan.is_none() {
                ui.spinner();
            }
        });
        ui.add_space(4.0);

        let sources = self.picker_sources().cloned().collect::<Vec<_>>();
        if sources.is_empty() {
            match &self.scan_error {
                Some(error) => ui.colored_label(WARNING, error),
                None if self.last_scan.is_none() => ui.label("Looking for windows and screens…"),
                None => ui.label("No windows or screens found."),
            };
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
                    let selected = self.selected_source.as_ref() == Some(&source.id);
                    if source_card(ui, source, self.thumbnails.get(&source.id), selected).clicked()
                    {
                        self.selected_source = Some(source.id.clone());
                    }
                }
            });
            ui.add_space(8.0);
        }
    }
}

struct SourceScan {
    sources: Result<Vec<Source>, String>,
    thumbnails: Vec<(String, egui::ColorImage)>,
}

fn scan_sources() -> SourceScan {
    let mut capture = PlatformCapture::default();
    let sources = capture.sources();
    let mut thumbnails = Vec::new();
    if let Ok(list) = &sources {
        for source in list {
            // Sources that cannot be previewed right now, such as windows on
            // another workspace, are shown with a placeholder instead.
            if let Ok(frame) = capture.thumbnail(source, THUMBNAIL_WIDTH)
                && let Some(image) = thumbnail_image(frame)
            {
                thumbnails.push((source.id.clone(), image));
            }
        }
    }
    SourceScan {
        sources,
        thumbnails,
    }
}

fn thumbnail_image(frame: Frame) -> Option<egui::ColorImage> {
    let image = image::RgbaImage::from_raw(frame.width, frame.height, frame.rgba)?;
    let image = if image.width() > THUMBNAIL_WIDTH {
        let height = (u64::from(image.height()) * u64::from(THUMBNAIL_WIDTH)
            / u64::from(image.width()))
        .max(1) as u32;
        image::imageops::thumbnail(&image, THUMBNAIL_WIDTH, height)
    } else {
        image
    };
    Some(egui::ColorImage::from_rgba_unmultiplied(
        [image.width() as usize, image.height() as usize],
        image.as_raw(),
    ))
}

/// Adds system fonts that cover CJK and Thai as fallbacks. egui's bundled
/// fonts are Latin-only, so window titles and OCR text would otherwise render
/// as empty boxes.
fn install_fallback_fonts(ctx: &egui::Context) {
    let mut fonts = egui::FontDefinitions::default();
    let mut loaded = Vec::<(String, u32)>::new();
    for language in ["ja", "ko", "zh-cn", "zh-tw", "th"] {
        let Ok(output) = std::process::Command::new("fc-match")
            .args(["-f", "%{file}\n%{index}", &format!(":lang={language}")])
            .output()
        else {
            return;
        };
        let text = String::from_utf8_lossy(&output.stdout);
        let mut lines = text.lines();
        let (Some(file), index) = (lines.next(), lines.next().and_then(|i| i.parse().ok())) else {
            continue;
        };
        let key = (file.to_string(), index.unwrap_or(0));
        if loaded.contains(&key) {
            continue;
        }
        let Ok(bytes) = std::fs::read(file) else {
            continue;
        };
        let name = format!("fallback-{language}");
        let mut data = egui::FontData::from_owned(bytes);
        data.index = key.1;
        fonts.font_data.insert(name.clone(), Arc::new(data));
        for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace] {
            fonts.families.entry(family).or_default().push(name.clone());
        }
        loaded.push(key);
    }
    if !loaded.is_empty() {
        ctx.set_fonts(fonts);
    }
}

pub(crate) fn source_detail(source: &Source) -> String {
    let kind = match source.kind {
        SourceKind::Window => "Window",
        SourceKind::Screen => "Screen",
    };
    if source.detail.is_empty() {
        kind.into()
    } else {
        format!("{kind} · {}", source.detail)
    }
}

fn single_line(
    painter: &egui::Painter,
    text: &str,
    size: f32,
    color: Color32,
    width: f32,
) -> Arc<egui::Galley> {
    let mut job = egui::text::LayoutJob::single_section(
        text.to_owned(),
        egui::TextFormat::simple(egui::FontId::proportional(size), color),
    );
    job.wrap = egui::text::TextWrapping::truncate_at_width(width);
    painter.layout_job(job)
}

/// A clickable preview card, similar to a screen-sharing picker.
pub(crate) fn source_card(
    ui: &mut egui::Ui,
    source: &Source,
    thumbnail: Option<&egui::TextureHandle>,
    selected: bool,
) -> egui::Response {
    // Shrink cards to fit narrow windows, keeping their proportions.
    let card_width = CARD_WIDTH.min(ui.max_rect().width()).max(140.0);
    let thumbnail_height = THUMBNAIL_HEIGHT * card_width / CARD_WIDTH;
    let size = egui::vec2(card_width, thumbnail_height + 64.0);
    let (rect, response) = ui.allocate_exact_size(size, egui::Sense::click());
    if ui.is_rect_visible(rect) {
        let visuals = ui.visuals();
        let painter = ui.painter();
        let hovered = response.hovered();
        let (fill, stroke) = if selected {
            (
                ACCENT.gamma_multiply(0.12),
                egui::Stroke::new(2.0_f32, ACCENT),
            )
        } else if hovered {
            (
                visuals.widgets.hovered.weak_bg_fill,
                visuals.widgets.hovered.bg_stroke,
            )
        } else {
            (
                visuals.faint_bg_color,
                visuals.widgets.noninteractive.bg_stroke,
            )
        };
        painter.rect(
            rect,
            CornerRadius::same(10),
            fill,
            stroke,
            egui::StrokeKind::Inside,
        );

        let preview = egui::Rect::from_min_size(
            rect.min + egui::vec2(8.0, 8.0),
            egui::vec2(card_width - 16.0, thumbnail_height),
        );
        painter.rect_filled(preview, CornerRadius::same(6), visuals.extreme_bg_color);
        match thumbnail {
            Some(texture) => {
                let image_size = texture.size_vec2();
                let scale = (preview.width() / image_size.x).min(preview.height() / image_size.y);
                painter.image(
                    texture.id(),
                    egui::Rect::from_center_size(preview.center(), image_size * scale),
                    egui::Rect::from_min_max(egui::pos2(0.0, 0.0), egui::pos2(1.0, 1.0)),
                    Color32::WHITE,
                );
            }
            None => {
                let initial = source
                    .detail
                    .chars()
                    .chain(source.name.chars())
                    .find(|character| character.is_alphanumeric())
                    .map_or_else(
                        || "?".into(),
                        |character| character.to_uppercase().to_string(),
                    );
                painter.text(
                    preview.center() - egui::vec2(0.0, 8.0),
                    egui::Align2::CENTER_CENTER,
                    initial,
                    egui::FontId::proportional(32.0),
                    visuals.weak_text_color(),
                );
                let note = if source.kind == SourceKind::Window {
                    "Not on screen"
                } else {
                    "No preview"
                };
                painter.text(
                    preview.center() + egui::vec2(0.0, 28.0),
                    egui::Align2::CENTER_CENTER,
                    note,
                    egui::FontId::proportional(11.0),
                    visuals.weak_text_color(),
                );
            }
        }
        if selected {
            let badge = preview.right_top() + egui::vec2(-14.0, 14.0);
            painter.circle_filled(badge, 10.0, ACCENT);
            painter.text(
                badge,
                egui::Align2::CENTER_CENTER,
                "✔",
                egui::FontId::proportional(12.0),
                Color32::WHITE,
            );
        }

        let text_width = card_width - 20.0;
        let left = rect.left() + 10.0;
        let title = single_line(
            painter,
            &source.name,
            14.0,
            visuals.strong_text_color(),
            text_width,
        );
        painter.galley(
            egui::pos2(left, preview.bottom() + 10.0),
            title,
            visuals.strong_text_color(),
        );
        let detail = single_line(
            painter,
            &source_detail(source),
            12.0,
            visuals.weak_text_color(),
            text_width,
        );
        painter.galley(
            egui::pos2(left, preview.bottom() + 32.0),
            detail,
            visuals.weak_text_color(),
        );
    }
    response
        .on_hover_text(&source.name)
        .on_hover_cursor(egui::CursorIcon::PointingHand)
}

impl Default for OverlayrApp {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for OverlayrApp {
    fn drop(&mut self) {
        // The shortcuts would otherwise keep running a closed Overlayr.
        self.config.keybinds = crate::config::Keybinds {
            toggle_overlay: String::new(),
            toggle_translations: String::new(),
        };
        self.apply_keybinds();
        if let Some(mut worker) = self.worker.take() {
            worker.stop();
        }
        if let Some(handle) = self.stopping.take() {
            let _ = handle.join();
        }
    }
}

impl eframe::App for OverlayrApp {
    /// eframe's default clear color is a translucent dark gray, which would
    /// dim everything under the transparent overlay window. The main window's
    /// panels paint their own opaque background, so it is unaffected.
    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        [0.0; 4]
    }

    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.apply_theme(ctx);
        self.handle_commands(ctx);
        self.apply_keybinds();
        self.reap_stopping_worker();
        self.reap_checks();
        self.reap_scan(ctx);
        self.reap_install();
        self.poll_translation_startup();
        self.poll_unlock();
        ctx.request_repaint_after(Duration::from_millis(100));
        // Sampled all along in debug mode, so the graphs already hold the
        // last minute when the Resources tab is opened.
        if self.config.debug {
            self.resources.tick();
        }
        if self.is_dirty()
            && self.worker.is_none()
            && ctx.input_mut(|input| {
                input.consume_shortcut(&egui::KeyboardShortcut::new(
                    egui::Modifiers::COMMAND,
                    egui::Key::S,
                ))
            })
        {
            self.save();
        }
        let snapshot = self.worker.as_ref().map(PipelineWorker::snapshot);
        let narrow = ctx.content_rect().width() < NARROW_WIDTH;
        let picker_visible = match self.mode {
            Mode::Translate => self.worker.is_none() && self.stopping.is_none(),
            Mode::Studio => !self.studio.is_recording(),
            Mode::Marketplace => self.marketplace.wants_previews(),
        };
        self.watch_windows(ctx);
        if picker_visible {
            let window_changed =
                self.scan.is_none() && self.windows_changed.swap(false, Ordering::Relaxed);
            if window_changed
                || self
                    .last_scan
                    .is_some_and(|scanned| scanned.elapsed() >= RESCAN_INTERVAL)
            {
                self.start_scan();
            }
            // Keep previews fresh even when nothing else redraws the window.
            ctx.request_repaint_after(RESCAN_INTERVAL);
        }

        egui::TopBottomPanel::top("header")
            .frame(
                egui::Frame::side_top_panel(&ctx.style()).inner_margin(Margin::symmetric(16, 10)),
            )
            .show(ctx, |ui| self.header_ui(ui, snapshot.as_ref(), narrow));

        let content_frame = egui::Frame::central_panel(&ctx.style()).inner_margin(Margin::same(16));
        self.exclude_own_windows();
        if self.mode == Mode::Studio {
            // Edits in the studio show up on the running test overlay.
            if let (Some(worker), Some(sent)) = (&self.worker, &mut self.studio_overlay)
                && sent != self.studio.pack()
            {
                *sent = self.studio.pack().clone();
                worker.set_pack(sent.clone());
            }
            if let Some(error) = self.error.clone() {
                egui::TopBottomPanel::top("studio_error")
                    .frame(
                        egui::Frame::side_top_panel(&ctx.style())
                            .inner_margin(Margin::symmetric(16, 6)),
                    )
                    .show(ctx, |ui| {
                        if banner(ui, &error, true) {
                            self.error = None;
                        }
                    });
            }
            let sources = self.picker_sources().cloned().collect::<Vec<_>>();
            if let Some(StudioAction::Play { pack, source }) =
                self.studio
                    .show(ctx, &self.config, &sources, &self.thumbnails)
            {
                self.config.active_pack = pack.to_string_lossy().into_owned();
                self.save();
                // Play the pack on the application it was made for.
                if let Some(source) = source {
                    self.selected_source = Some(source);
                }
                self.mode = Mode::Translate;
            }
        } else if self.mode == Mode::Marketplace {
            let sources = self.picker_sources().cloned().collect::<Vec<_>>();
            match self
                .marketplace
                .show(ctx, &sources, &self.thumbnails, &self.thumbnail_images)
            {
                Some(MarketplaceAction::Use(id)) => {
                    self.config.active_pack = format!("{}{id}", crate::marketplace::ACTIVE_PREFIX);
                    self.save();
                    self.mode = Mode::Translate;
                }
                Some(MarketplaceAction::Uninstalled(id)) => {
                    if self.config.active_pack
                        == format!("{}{id}", crate::marketplace::ACTIVE_PREFIX)
                    {
                        self.config.active_pack.clear();
                        self.save();
                    }
                    if self
                        .unlocked
                        .as_ref()
                        .is_some_and(|(unlocked, _)| *unlocked == id)
                    {
                        self.unlocked = None;
                    }
                }
                None => {}
            }
        } else {
            egui::CentralPanel::default()
                .frame(content_frame)
                .show(ctx, |ui| self.main_ui(ui, snapshot.clone()));
        }
        if self.settings_open {
            self.settings_modal(ctx);
        }

        if let Some(worker) = &self.worker {
            let ctx = ctx.clone();
            worker.set_waker(move || ctx.request_repaint());
        }
        if let (Some(snapshot), Some(source)) = (snapshot, self.active_source.clone()) {
            #[cfg(unix)]
            if self.focus_changed.swap(false, Ordering::Relaxed) {
                self.game_unfocused = crate::capture::hyprland_address(&source)
                    .is_some_and(|address| !crate::hyprland::is_focused(address).unwrap_or(true));
            }
            if !snapshot.source_active || self.game_unfocused {
                self.close_overlay_window(ctx);
                return;
            }
            // A shortcut hides the translations to show the original text.
            let blocks = if self.translations_hidden {
                Vec::new()
            } else {
                snapshot.blocks
            };
            let line_heights = self.line_sizes.line_heights(
                blocks
                    .iter()
                    .map(|block| (block.bounds.height as f32, block.lines)),
            );
            let Some(overlay_bounds) = snapshot
                .capture_bounds
                .or_else(|| (!source.bounds.is_empty()).then_some(source.bounds))
            else {
                return;
            };
            self.place_overlay_window(overlay_bounds);
            self.update_occluders();
            let viewport_id = egui::ViewportId::from_hash_of("overlayr-translation-overlay");
            let builder = egui::ViewportBuilder::default()
                .with_title(OVERLAY_TITLE)
                .with_position([overlay_bounds.left as f32, overlay_bounds.top as f32])
                .with_inner_size([
                    overlay_bounds.width.max(1) as f32,
                    overlay_bounds.height.max(1) as f32,
                ])
                .with_decorations(false)
                .with_transparent(true)
                .with_always_on_top()
                .with_mouse_passthrough(true);
            self.overlay_window.open = true;
            let content_protected = Arc::clone(&self.overlay_window.content_protected);
            // The overlay only redraws when asked; refresh it with each update
            // so new translations appear.
            ctx.request_repaint_of(viewport_id);
            let debug_boxes = snapshot.debug.is_some();
            let occluders = self.overlay_window.occluders.clone();
            ctx.show_viewport_deferred(viewport_id, builder, move |overlay_ctx, _| {
                // Ask the window system not to include the overlay in screen
                // captures where the platform supports content protection.
                // This prevents OCR feedback loops from reading our own text.
                if !content_protected.swap(true, Ordering::Relaxed) {
                    overlay_ctx.send_viewport_cmd(egui::ViewportCommand::ContentProtected(true));
                }
                egui::CentralPanel::default()
                    .frame(egui::Frame::NONE.fill(egui::Color32::TRANSPARENT))
                    .show(overlay_ctx, |ui| {
                        let origin = ui.max_rect().min
                            - egui::vec2(overlay_bounds.left as f32, overlay_bounds.top as f32);
                        for (block, line_height) in blocks.iter().zip(&line_heights) {
                            // Only translate the selected application: skip
                            // text under windows that sit above it.
                            if occluders.iter().any(|area| overlaps(block.bounds, *area)) {
                                continue;
                            }
                            paint_translation(ui.painter(), block, origin, *line_height);
                            // Debug mode outlines every tracked text, including
                            // text left untranslated.
                            if debug_boxes {
                                let color = if block.translated_text.is_some() {
                                    SUCCESS
                                } else {
                                    Color32::from_rgb(90, 160, 255)
                                };
                                ui.painter().rect_stroke(
                                    egui::Rect::from_min_size(
                                        origin
                                            + egui::vec2(
                                                block.bounds.left as f32,
                                                block.bounds.top as f32,
                                            ),
                                        egui::vec2(
                                            block.bounds.width as f32,
                                            block.bounds.height as f32,
                                        ),
                                    )
                                    .expand(2.0),
                                    CornerRadius::same(2),
                                    egui::Stroke::new(1.5_f32, color),
                                    egui::StrokeKind::Outside,
                                );
                            }
                        }
                    });
            });
        } else {
            self.close_overlay_window(ctx);
        }
    }
}

/// Draws a translation over the original text. By default the original is
/// erased with a gradient of the colours around it, so the translation
/// blends into the game's own background; pack entries can choose their own
/// colours, size, outline and alignment.
/// `line_height` is the height of one line of the original text, so the
/// translation gets the same size; see [`LineSizes`].
pub(crate) fn paint_translation(
    painter: &egui::Painter,
    block: &TextBlock,
    origin: egui::Pos2,
    line_height: f32,
) {
    const PADDING: f32 = 4.0;
    const MIN_FONT_SIZE: f32 = 11.0;
    // Text no pack entry covers, and blocks still being translated, are left
    // untouched.
    let Some(text) = block.translated_text.as_deref() else {
        return;
    };
    let style = block.style.clone().unwrap_or_default();
    let corners = match style.background {
        Background::Auto => Some(
            block
                .background
                .map(|corners| corners.map(rgba))
                .unwrap_or([Color32::from_rgb(14, 16, 22); 4]),
        ),
        Background::Solid(color) => Some([rgba(color); 4]),
        Background::None => None,
    };
    // Keep the original text's colour when it is readable on the
    // background; otherwise black or white.
    let text_color = style
        .color
        .map(rgba)
        .or_else(|| {
            block
                .foreground
                .filter(|_| style.background == Background::Auto)
                .map(rgba)
        })
        .unwrap_or_else(|| {
            let average = corners.map_or(Color32::BLACK, |corners| average_color(&corners));
            contrasting(average)
        });
    // Text drawn straight onto the game needs an outline to stay legible.
    let outline = style
        .outline
        .map(rgba)
        .or_else(|| corners.is_none().then(|| contrasting(text_color)));

    let source = egui::Rect::from_min_size(
        origin + egui::vec2(block.bounds.left as f32, block.bounds.top as f32),
        egui::vec2(block.bounds.width as f32, block.bounds.height as f32),
    );
    let layout = |size: f32, wrap_width: f32| {
        painter.layout(
            text.to_owned(),
            egui::FontId::proportional(size),
            text_color,
            wrap_width,
        )
    };
    // Size by lines, not by fitting each box, so text of one size gets one
    // font size.
    let line = if line_height > 0.0 {
        line_height
    } else {
        source.height()
    };
    let lines = (source.height() / line).round().max(1.0);
    // From the shared line height, not this box: boxes of one font differ
    // by a few pixels from text to text and read to read.
    let target = (line * 0.85).clamp(MIN_FONT_SIZE, 48.0);
    // One size for all text of one size, whatever the translation's length:
    // shrinking long translations to fit made sizes jump from text to text.
    // Longer translations wrap and the box grows instead.
    let galley = match style.font_size {
        Some(size) => layout(size, source.width().max(60.0)),
        // One line: a longer translation may run a little past the original
        // before it wraps.
        None if lines <= 1.0 => layout(target, source.width().max(60.0) * 1.4),
        None => layout(target, source.width().max(60.0)),
    };
    // Cover the source text completely, growing only if the translation
    // still doesn't fit.
    let size = egui::vec2(
        source.width().max(galley.size().x),
        source.height().max(galley.size().y),
    ) + egui::vec2(PADDING * 2.0, PADDING * 2.0);
    let offset = egui::vec2(style.offset[0], style.offset[1]);
    let rect = egui::Rect::from_min_size(source.min - egui::vec2(PADDING, PADDING) + offset, size);
    // A moved translation still hides the original where it was.
    let erase = if style.is_moved() {
        vec![source.expand(PADDING), rect]
    } else {
        vec![rect]
    };

    for area in erase {
        paint_background(painter, block, &style, corners, area);
    }

    let horizontal = match style.align {
        TextAlign::Left => PADDING,
        TextAlign::Center => ((rect.width() - galley.size().x) / 2.0).max(PADDING),
    };
    let vertical = PADDING + ((rect.height() - PADDING * 2.0 - galley.size().y) / 2.0).max(0.0);
    let position = rect.min + egui::vec2(horizontal, vertical);
    if let Some(outline) = outline {
        for (dx, dy) in [(-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)] {
            painter.galley_with_override_text_color(
                position + egui::vec2(dx, dy),
                galley.clone(),
                outline,
            );
        }
    }
    painter.galley(position, galley, text_color);
}

/// A configurable shortcut.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum KeybindSlot {
    ToggleOverlay,
    ToggleTranslations,
}

impl KeybindSlot {
    fn label(self) -> &'static str {
        match self {
            Self::ToggleOverlay => "Start / stop overlay",
            Self::ToggleTranslations => "Hide / show translations",
        }
    }

    fn keys_mut(self, keybinds: &mut crate::config::Keybinds) -> &mut String {
        match self {
            Self::ToggleOverlay => &mut keybinds.toggle_overlay,
            Self::ToggleTranslations => &mut keybinds.toggle_translations,
        }
    }
}

/// The key's name as Hyprland (XKB) knows it.
fn hyprland_key(key: egui::Key) -> Option<String> {
    use egui::Key;
    let name = match key {
        Key::Space => "space",
        Key::Enter => "Return",
        Key::Escape => "Escape",
        Key::Tab => "Tab",
        Key::Backspace => "BackSpace",
        Key::Delete => "Delete",
        Key::Insert => "Insert",
        Key::Home => "Home",
        Key::End => "End",
        Key::PageUp => "Prior",
        Key::PageDown => "Next",
        Key::ArrowLeft => "left",
        Key::ArrowRight => "right",
        Key::ArrowUp => "up",
        Key::ArrowDown => "down",
        Key::Minus => "minus",
        Key::Equals => "equal",
        Key::Comma => "comma",
        Key::Period => "period",
        Key::Slash => "slash",
        Key::Backslash => "backslash",
        Key::Semicolon => "semicolon",
        Key::Quote => "apostrophe",
        Key::Backtick => "grave",
        Key::OpenBracket => "bracketleft",
        Key::CloseBracket => "bracketright",
        other => {
            let name = other.name();
            // Letters, digits ("Num1" → "1") and function keys ("F8").
            let name = name.strip_prefix("Num").unwrap_or(name);
            let simple = name.chars().count() == 1 && name.chars().all(char::is_alphanumeric);
            let function = name.starts_with('F') && name[1..].parse::<u8>().is_ok();
            return (simple || function).then(|| name.to_uppercase());
        }
    };
    Some(name.to_string())
}

/// Hides the original text under `rect`: with the background rebuilt
/// around it, a gradient of its surrounding colours, or a pack's colour.
fn paint_background(
    painter: &egui::Painter,
    block: &TextBlock,
    style: &TextStyle,
    corners: Option<[Color32; 4]>,
    rect: egui::Rect,
) {
    let backdrop = block
        .backdrop
        .as_ref()
        .filter(|_| style.background == Background::Auto);
    if let Some(backdrop) = backdrop {
        // The rebuilt background covers the original text and its padding;
        // a wider translation stretches it.
        let texture = backdrop.texture.get_or_init(|| {
            painter.ctx().load_texture(
                "translation-backdrop",
                egui::ColorImage::from_rgba_unmultiplied(
                    [backdrop.width as usize, backdrop.height as usize],
                    &backdrop.rgba,
                ),
                egui::TextureOptions::LINEAR,
            )
        });
        painter.image(
            texture.id(),
            rect,
            egui::Rect::from_min_max(egui::pos2(0.0, 0.0), egui::pos2(1.0, 1.0)),
            Color32::WHITE,
        );
    } else if let Some([top_left, top_right, bottom_right, bottom_left]) = corners {
        let mut mesh = egui::epaint::Mesh::default();
        mesh.colored_vertex(rect.left_top(), top_left);
        mesh.colored_vertex(rect.right_top(), top_right);
        mesh.colored_vertex(rect.right_bottom(), bottom_right);
        mesh.colored_vertex(rect.left_bottom(), bottom_left);
        mesh.add_triangle(0, 1, 2);
        mesh.add_triangle(0, 2, 3);
        painter.add(egui::Shape::mesh(mesh));
    }
}

/// Line heights seen while translating. Each text's font size comes from
/// its own line height, snapped to a size seen before when they are close,
/// so text in the same font keeps one size: OCR boxes of the same font vary
/// by a few pixels, and sizes must not change when other text appears.
#[derive(Default)]
pub(crate) struct LineSizes {
    sizes: Vec<f32>,
}

/// Line heights within this fraction of a known size use that size.
const LINE_SIZE_TOLERANCE: f32 = 0.2;
/// Text on screen within this fraction of the most common line height gets
/// exactly that height: list items whose boxes include an icon, or were
/// recorded a little taller, look like the rest. Headings are bigger.
const UNIFORM_TOLERANCE: f32 = 0.4;

impl LineSizes {
    /// [`LineSizes::line_height`] for every block on screen together, with
    /// text near the most common size given that size, so a list or a
    /// paragraph reads in one size.
    pub(crate) fn line_heights(&mut self, blocks: impl Iterator<Item = (f32, u32)>) -> Vec<f32> {
        let blocks = blocks
            .map(|(height, lines)| (height, self.line_height(height, lines)))
            .collect::<Vec<_>>();
        let heights = blocks
            .iter()
            .map(|(_, snapped)| *snapped)
            .collect::<Vec<_>>();
        let near = |first: f32, second: f32| (first - second).abs() <= second * UNIFORM_TOLERANCE;
        let common = heights
            .iter()
            .copied()
            .filter(|height| *height > 0.0)
            .max_by_key(|&height| heights.iter().filter(|&&other| near(other, height)).count());
        let Some(common) = common else {
            return heights;
        };
        // The common size is the median of the lines on screen now, not a
        // size remembered from earlier boxes: a few boxes taller with their
        // icons must not enlarge the whole list.
        let mut members = blocks
            .iter()
            .filter(|(_, snapped)| *snapped > 0.0 && near(*snapped, common))
            .map(|(height, snapped)| height / (height / snapped).round().max(1.0))
            .collect::<Vec<_>>();
        members.sort_by(f32::total_cmp);
        let median = members[members.len() / 2];
        heights
            .into_iter()
            .map(|height| {
                if height > 0.0 && near(height, common) {
                    median
                } else {
                    height
                }
            })
            .collect()
    }

    /// Height of one line of a text block `height` tall with `lines` lines
    /// (0 if unknown).
    pub(crate) fn line_height(&mut self, height: f32, lines: u32) -> f32 {
        if height <= 0.0 {
            return height;
        }
        let lines = if lines > 0 {
            lines
        } else {
            // Unknown: assume lines of the largest size seen that fits.
            self.sizes
                .iter()
                .copied()
                .filter(|size| *size <= height * (1.0 + LINE_SIZE_TOLERANCE))
                .max_by(f32::total_cmp)
                .map_or(1, |size| (height / size).round().max(1.0) as u32)
        };
        let raw = height / lines as f32;
        let known = self
            .sizes
            .iter()
            .copied()
            .filter(|size| (raw - size).abs() <= size * LINE_SIZE_TOLERANCE)
            .min_by(|a, b| (raw - a).abs().total_cmp(&(raw - b).abs()));
        known.unwrap_or_else(|| {
            self.sizes.push(raw);
            raw
        })
    }
}

pub(crate) fn rgba([red, green, blue, alpha]: [u8; 4]) -> Color32 {
    Color32::from_rgba_unmultiplied(red, green, blue, alpha)
}

pub(crate) fn average_color(colors: &[Color32]) -> Color32 {
    let count = colors.len().max(1) as u32;
    let channel = |pick: fn(&Color32) -> u8| {
        (colors
            .iter()
            .map(|color| u32::from(pick(color)))
            .sum::<u32>()
            / count) as u8
    };
    Color32::from_rgb(
        channel(Color32::r),
        channel(Color32::g),
        channel(Color32::b),
    )
}

/// Black or white, whichever reads better on `background`.
pub(crate) fn contrasting(background: Color32) -> Color32 {
    let luminance = 0.299 * f32::from(background.r())
        + 0.587 * f32::from(background.g())
        + 0.114 * f32::from(background.b());
    if luminance > 140.0 {
        Color32::BLACK
    } else {
        Color32::WHITE
    }
}

fn overlaps(first: Rect, second: Rect) -> bool {
    first.left < second.right()
        && second.left < first.right()
        && first.top < second.bottom()
        && second.top < first.bottom()
}

fn verdict_color(verdict: Verdict) -> Color32 {
    match verdict {
        Verdict::Translated => SUCCESS,
        Verdict::NotTranslated => Color32::from_rgb(90, 160, 255),
        Verdict::LowConfidence => WARNING,
        Verdict::InvalidText => Color32::from_rgb(190, 120, 255),
        Verdict::Noise => DANGER,
        Verdict::OwnWindow => Color32::from_rgb(120, 140, 160),
        Verdict::OverlayEcho => Color32::from_gray(150),
    }
}

fn live_ui(ui: &mut egui::Ui, snapshot: PipelineSnapshot, pack: Option<&str>) {
    if let Some(error) = &snapshot.last_error {
        banner(ui, error, false);
    }
    if !snapshot.source_active {
        ui.colored_label(
            WARNING,
            "The selected window isn't visible, so translations are hidden. They reappear \
             when you switch back.",
        );
    }
    ui.horizontal_wrapped(|ui| {
        stat(ui, "Status", &snapshot.status);
        stat(
            ui,
            "Speed",
            &format!("{:.1} FPS", snapshot.frames_per_second),
        );
        stat(ui, "Frames", &snapshot.frames_processed.to_string());
        ui.scope(|ui| stat(ui, "OCR runs", &snapshot.detections.to_string()))
            .response
            .on_hover_text("Text is only read again when new text appears");
        stat(ui, "Text blocks", &snapshot.blocks.len().to_string());
    });
    ui.add_space(8.0);
    ui.heading("Live translations");
    let mode = match pack {
        Some(name) => format!("Using the pack \"{name}\""),
        None => "Using machine translation".into(),
    };
    ui.label(RichText::new(mode).small().weak());
    ui.add_space(4.0);
    if snapshot.blocks.is_empty() {
        ui.label(
            RichText::new(
                "No text detected yet. Make sure text is visible in the captured area \
                 and Tesseract has the source language pack installed.",
            )
            .weak(),
        );
        return;
    }
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            for block in snapshot.blocks {
                card(ui).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    match &block.translated_text {
                        Some(text) => ui.label(RichText::new(text).size(17.0).strong()),
                        // With a pack, text it doesn't cover is left as is.
                        None if pack.is_some() => ui.label(
                            RichText::new("Not in pack – left as original")
                                .italics()
                                .weak(),
                        ),
                        None => ui.label(RichText::new("Translating…").italics().weak()),
                    };
                    ui.label(RichText::new(&block.source_text).weak());
                    ui.horizontal(|ui| {
                        let color = if block.confidence >= 0.75 {
                            SUCCESS
                        } else if block.confidence >= 0.5 {
                            WARNING
                        } else {
                            DANGER
                        };
                        ui.add(
                            egui::ProgressBar::new(block.confidence.clamp(0.0, 1.0))
                                .desired_width(90.0)
                                .desired_height(4.0)
                                .fill(color),
                        );
                        ui.label(
                            RichText::new(format!("{:.0}% confidence", block.confidence * 100.0))
                                .small()
                                .weak(),
                        );
                    });
                });
                ui.add_space(6.0);
            }
        });
}

fn card(ui: &egui::Ui) -> egui::Frame {
    egui::Frame::new()
        .fill(ui.visuals().faint_bg_color)
        .stroke(ui.visuals().widgets.noninteractive.bg_stroke)
        .corner_radius(CornerRadius::same(8))
        .inner_margin(Margin::same(12))
}

pub(crate) fn section(ui: &mut egui::Ui, title: &str, add_contents: impl FnOnce(&mut egui::Ui)) {
    card(ui).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.label(RichText::new(title).strong());
        ui.add_space(2.0);
        add_contents(ui);
    });
    ui.add_space(10.0);
}

fn pill(ui: &mut egui::Ui, text: &str, color: Color32) {
    egui::Frame::new()
        .fill(color.gamma_multiply(0.18))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::symmetric(8, 2))
        .show(ui, |ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            let (dot, _) = ui.allocate_exact_size(egui::vec2(7.0, 7.0), egui::Sense::hover());
            ui.painter().circle_filled(dot.center(), 3.5, color);
            ui.label(RichText::new(text).small().color(color));
        });
}

fn percent_slider(value: &mut f32, range: std::ops::RangeInclusive<f32>) -> egui::Slider<'_> {
    egui::Slider::new(value, range)
        .custom_formatter(|value, _| format!("{:.0}%", value * 100.0))
        .custom_parser(|text| {
            text.trim_end_matches('%')
                .trim()
                .parse::<f64>()
                .ok()
                .map(|value| value / 100.0)
        })
}

fn stat(ui: &mut egui::Ui, label: &str, value: &str) {
    card(ui)
        .inner_margin(Margin::symmetric(12, 6))
        .show(ui, |ui| {
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                // Never wrap: a full card moves to the next row instead of
                // squeezing its text into a column.
                ui.add(
                    egui::Label::new(RichText::new(label).small().weak())
                        .wrap_mode(egui::TextWrapMode::Extend),
                );
                ui.add(
                    egui::Label::new(RichText::new(value).strong())
                        .wrap_mode(egui::TextWrapMode::Extend),
                );
            });
        });
}

pub(crate) fn primary_button(text: &str, fill: Color32) -> egui::Button<'static> {
    egui::Button::new(RichText::new(text).strong().color(Color32::WHITE))
        .fill(fill)
        .corner_radius(CornerRadius::same(6))
        .min_size(egui::vec2(0.0, 30.0))
}

/// Shows an error banner. Returns true when the user dismissed it.
fn banner(ui: &mut egui::Ui, message: &str, dismissible: bool) -> bool {
    let mut dismissed = false;
    egui::Frame::new()
        .fill(DANGER.gamma_multiply(0.15))
        .stroke(egui::Stroke::new(1.0_f32, DANGER.gamma_multiply(0.6)))
        .corner_radius(CornerRadius::same(8))
        .inner_margin(Margin::symmetric(12, 8))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal_top(|ui| {
                if dismissible && ui.small_button("✖").on_hover_text("Dismiss").clicked() {
                    dismissed = true;
                }
                ui.add(egui::Label::new(RichText::new(message).color(DANGER)).wrap());
            });
        });
    ui.add_space(8.0);
    dismissed
}

fn check_row(
    ui: &mut egui::Ui,
    name: &str,
    passed: bool,
    detail: &str,
    add_actions: impl FnOnce(&mut egui::Ui),
) {
    ui.horizontal_top(|ui| {
        let (icon, color) = if passed {
            ("✔", SUCCESS)
        } else {
            ("✖", DANGER)
        };
        ui.label(RichText::new(icon).color(color).strong());
        ui.vertical(|ui| {
            ui.spacing_mut().item_spacing.y = 2.0;
            ui.label(RichText::new(capitalize(name)).strong());
            ui.add(egui::Label::new(RichText::new(detail).small().weak()).wrap());
            if !passed && let Some(hint) = check_hint(name) {
                ui.add(egui::Label::new(RichText::new(hint).small().color(WARNING)).wrap());
            }
            add_actions(ui);
        });
    });
}

fn check_hint(name: &str) -> Option<&'static str> {
    match name {
        "capture backend" | "capture sources" => {
            Some("Install grim and slurp, and run Overlayr inside a desktop session.")
        }
        _ => None,
    }
}

fn capitalize(text: &str) -> String {
    let mut characters = text.chars();
    match characters.next() {
        Some(first) => first.to_uppercase().chain(characters).collect(),
        None => String::new(),
    }
}

fn language_picker(ui: &mut egui::Ui, id: &str, code: &mut String) {
    ui.horizontal(|ui| {
        let selected = LANGUAGES
            .iter()
            .find(|(value, _)| *value == code.as_str())
            .map_or("Custom", |(_, name)| name);
        egui::ComboBox::from_id_salt(id)
            .width(150.0)
            .selected_text(selected)
            .show_ui(ui, |ui| {
                for (value, name) in LANGUAGES {
                    if ui
                        .selectable_label(code.as_str() == *value, *name)
                        .clicked()
                    {
                        *code = (*value).into();
                    }
                }
            });
        ui.add(
            egui::TextEdit::singleline(code)
                .desired_width(52.0)
                .hint_text("code"),
        )
        .on_hover_text("Language code, for example ja or zh-CN");
    });
}

#[cfg(test)]
mod tests {
    use super::LineSizes;

    #[test]
    fn keys_get_their_hyprland_names() {
        use super::hyprland_key;
        use eframe::egui::Key;
        assert_eq!(hyprland_key(Key::O).as_deref(), Some("O"));
        assert_eq!(hyprland_key(Key::Num7).as_deref(), Some("7"));
        assert_eq!(hyprland_key(Key::F8).as_deref(), Some("F8"));
        assert_eq!(hyprland_key(Key::Space).as_deref(), Some("space"));
        assert_eq!(hyprland_key(Key::Backtick).as_deref(), Some("grave"));
    }

    #[test]
    fn a_list_on_screen_shares_one_size_and_headings_keep_theirs() {
        // List items, two boxed a little taller with their icons, and a
        // heading twice their size.
        let heights = LineSizes::default().line_heights(
            [20.0, 26.0, 19.0, 21.0, 27.0, 44.0]
                .map(|height| (height, 1))
                .into_iter(),
        );
        assert!(heights[..5].iter().all(|height| *height == heights[0]));
        assert_eq!(heights[5], 44.0);
    }

    #[test]
    fn a_few_taller_boxes_do_not_enlarge_a_list() {
        let mut sizes = LineSizes::default();
        // Rows first seen boxed with their icons...
        sizes.line_heights([25.0, 25.0, 25.0].map(|height| (height, 1)).into_iter());
        // ...then mostly read as just their text.
        let heights = sizes.line_heights(
            [18.0, 17.0, 18.0, 25.0, 18.0]
                .map(|height| (height, 1))
                .into_iter(),
        );
        assert!(heights.iter().all(|height| *height == 18.0));
    }

    #[test]
    fn text_of_one_size_keeps_one_font_size() {
        let mut sizes = LineSizes::default();
        // One font, read a few pixels apart.
        let first = sizes.line_height(19.0, 1);
        assert_eq!(sizes.line_height(17.0, 1), first);
        assert_eq!(sizes.line_height(21.0, 1), first);
        // A paragraph of three such lines.
        assert_eq!(sizes.line_height(58.0, 3), first);
        // A title in a bigger font gets its own size.
        assert_eq!(sizes.line_height(30.0, 1), 30.0);
        // Unknown line counts are worked out from known sizes.
        assert_eq!(sizes.line_height(60.0, 0), 30.0);
        assert_eq!(sizes.line_height(19.0, 0), first);
    }
}
