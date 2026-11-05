//! The Marketplace tab: a grid of translation packs to install and play,
//! and a dialog for creators to publish their own packs.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::mpsc::{Receiver, Sender, TryRecvError, channel};
use std::time::{Duration, Instant};

use eframe::egui;
use egui::{Color32, CornerRadius, RichText};

use crate::marketplace::{self, Listing};
use crate::pack::Pack;
use crate::services::ServiceResult;
use crate::types::Source;
use crate::ui::{ACCENT, DANGER, SUCCESS, primary_button};

const CARD_WIDTH: f32 = 240.0;
const COVER_HEIGHT: f32 = 135.0;
const COVER_WIDTH: u32 = 480;
/// How often the login file is checked, so logging in through the browser
/// shows up without a restart.
const LOGIN_CHECK: Duration = Duration::from_secs(2);
/// Highest price a creator can set, in baht; the lowest above free is ฿10.
const MAX_PRICE_BAHT: u32 = 10_000;
const MIN_PRICE_BAHT: u32 = 10;
/// The creator's share of each sale, as the website takes 35%.
const CREATOR_SHARE: &str = "65%";

/// What the rest of the app should do after a marketplace interaction.
pub enum MarketplaceAction {
    /// Translate with this installed pack.
    Use(String),
    /// The pack was uninstalled; stop selecting it.
    Uninstalled(String),
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Filter {
    All,
    Installed,
    Mine,
}

enum Done {
    /// A page of listings, whether it continues the current list, and the
    /// packs this account bought (when logged in).
    Listed(ServiceResult<marketplace::Page>, bool, Option<Vec<String>>),
    Installed(ServiceResult<Listing>),
    Published(ServiceResult<Listing>),
    Unpublished(String, ServiceResult<()>),
}

struct PublishForm {
    packs: Vec<(PathBuf, Pack)>,
    pack: Option<usize>,
    title: String,
    description: String,
    cover_source: Option<String>,
    /// Whole baht; 0 is free.
    price_baht: u32,
}

pub struct MarketplaceView {
    listings: Vec<Listing>,
    listed_once: bool,
    installed: HashMap<String, Listing>,
    /// Packs this account bought.
    owned: HashSet<String>,
    search: String,
    searched: String,
    filter: Filter,
    next_offset: Option<u32>,
    /// Cover pictures by listing and version; `None` while downloading or
    /// when there is none.
    covers: HashMap<String, Option<egui::TextureHandle>>,
    /// Sends covers to download to a background thread, which answers on
    /// `cover_results`.
    cover_requests: Option<Sender<(String, Listing)>>,
    cover_results: Option<Receiver<(String, Option<egui::ColorImage>)>>,
    /// The background request running, and what it's for.
    job: Option<(String, Receiver<Done>)>,
    error: Option<String>,
    notice: Option<String>,
    selected: Option<String>,
    publish: Option<PublishForm>,
    confirm_unpublish: Option<String>,
    session_uid: Option<String>,
    login_checked: Option<Instant>,
}

impl Default for MarketplaceView {
    fn default() -> Self {
        Self {
            listings: Vec::new(),
            listed_once: false,
            installed: installed_listings(),
            owned: HashSet::new(),
            search: String::new(),
            searched: String::new(),
            filter: Filter::All,
            next_offset: None,
            covers: HashMap::new(),
            cover_requests: None,
            cover_results: None,
            job: None,
            error: None,
            notice: None,
            selected: None,
            publish: None,
            confirm_unpublish: None,
            session_uid: None,
            login_checked: None,
        }
    }
}

fn installed_listings() -> HashMap<String, Listing> {
    marketplace::installed()
        .into_iter()
        .map(|installed| (installed.listing.id.clone(), installed.listing))
        .collect()
}

impl MarketplaceView {
    /// Whether the publish dialog needs window previews for its cover picker.
    pub fn wants_previews(&self) -> bool {
        self.publish.is_some()
    }

    fn busy(&self) -> bool {
        self.job.is_some()
    }

    fn run(
        &mut self,
        ctx: &egui::Context,
        label: impl Into<String>,
        work: impl FnOnce() -> Done + Send + 'static,
    ) {
        let (sender, receiver) = channel();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let _ = sender.send(work());
            ctx.request_repaint();
        });
        self.job = Some((label.into(), receiver));
    }

    fn refresh(&mut self, ctx: &egui::Context) {
        let query = self.search.clone();
        self.searched = query.clone();
        let logged_in = self.session_uid.is_some();
        self.run(ctx, "Loading packs…", move || {
            let page = marketplace::list(&query, 0);
            // Not knowing what was bought only hides the Owned labels.
            let owned = logged_in.then(|| marketplace::owned().ok()).flatten();
            Done::Listed(page, false, owned)
        });
    }

    fn load_more(&mut self, ctx: &egui::Context) {
        let Some(offset) = self.next_offset else {
            return;
        };
        let query = self.searched.clone();
        self.run(ctx, "Loading more…", move || {
            Done::Listed(marketplace::list(&query, offset), true, None)
        });
    }

    /// Takes covers the background thread has downloaded.
    fn receive_covers(&mut self, ctx: &egui::Context) {
        let Some(results) = &self.cover_results else {
            return;
        };
        while let Ok((key, image)) = results.try_recv() {
            let texture = image.map(|image| {
                ctx.load_texture(format!("cover:{key}"), image, egui::TextureOptions::LINEAR)
            });
            self.covers.insert(key, texture);
        }
    }

    fn check_login(&mut self) {
        if self
            .login_checked
            .is_none_or(|checked| checked.elapsed() >= LOGIN_CHECK)
        {
            self.session_uid = crate::auth::load_session().map(|session| session.uid);
            self.login_checked = Some(Instant::now());
        }
    }

    fn finish_job(&mut self) -> Option<MarketplaceAction> {
        let (_, receiver) = self.job.as_ref()?;
        let done = match receiver.try_recv() {
            Ok(done) => done,
            Err(TryRecvError::Empty) => return None,
            Err(TryRecvError::Disconnected) => {
                self.job = None;
                return None;
            }
        };
        self.job = None;
        match done {
            Done::Listed(Ok(page), more, owned) => {
                if let Some(owned) = owned {
                    self.owned = owned.into_iter().collect();
                }
                if more {
                    let known = self
                        .listings
                        .iter()
                        .map(|listing| listing.id.clone())
                        .collect::<std::collections::HashSet<_>>();
                    self.listings.extend(
                        page.listings
                            .into_iter()
                            .filter(|listing| !known.contains(&listing.id)),
                    );
                } else {
                    self.listings = page.listings;
                }
                self.next_offset = page.next_offset;
                self.listed_once = true;
            }
            Done::Listed(Err(error), _, _) => {
                self.listed_once = true;
                self.error = Some(error);
            }
            Done::Installed(Ok(listing)) => {
                self.notice = Some(format!("Installed \"{}\"", listing.title));
                if listing.price > 0 {
                    self.owned.insert(listing.id.clone());
                }
                self.installed = installed_listings();
            }
            Done::Published(Ok(listing)) => {
                self.notice = Some(format!(
                    "Published \"{}\" (version {})",
                    listing.title, listing.version
                ));
                self.publish = None;
                self.listings.retain(|existing| existing.id != listing.id);
                self.listings.insert(0, listing);
            }
            Done::Unpublished(id, Ok(())) => {
                self.notice = Some("Removed the pack from the marketplace".into());
                self.listings.retain(|listing| listing.id != id);
                self.selected = None;
            }
            Done::Installed(Err(error))
            | Done::Published(Err(error))
            | Done::Unpublished(_, Err(error)) => self.error = Some(error),
        }
        None
    }

    pub fn show(
        &mut self,
        ctx: &egui::Context,
        sources: &[Source],
        thumbnails: &HashMap<String, egui::TextureHandle>,
        thumbnail_images: &HashMap<String, egui::ColorImage>,
    ) -> Option<MarketplaceAction> {
        self.check_login();
        self.finish_job();
        self.receive_covers(ctx);
        if !self.listed_once && !self.busy() {
            self.refresh(ctx);
        }
        let mut action = None;
        egui::CentralPanel::default()
            .frame(egui::Frame::central_panel(&ctx.style()).inner_margin(egui::Margin::same(16)))
            .show(ctx, |ui| {
                self.toolbar(ui, ctx);
                if let Some(error) = self.error.clone()
                    && message(ui, &error, DANGER)
                {
                    self.error = None;
                }
                if let Some(notice) = self.notice.clone()
                    && message(ui, &notice, SUCCESS)
                {
                    self.notice = None;
                }
                egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .show(ui, |ui| self.grid(ui));
            });
        if self.selected.is_some() {
            action = self.detail_modal(ctx);
        }
        if self.publish.is_some() {
            self.publish_modal(ctx, sources, thumbnails, thumbnail_images);
        }
        action
    }

    fn toolbar(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        ui.horizontal(|ui| {
            ui.heading("Marketplace");
            ui.add_space(8.0);
            for (filter, label) in [
                (Filter::All, "All packs"),
                (Filter::Installed, "Installed"),
                (Filter::Mine, "My packs"),
            ] {
                ui.selectable_value(&mut self.filter, filter, label);
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if self.session_uid.is_some() {
                    if ui
                        .add_enabled(!self.busy(), primary_button("＋ Add", ACCENT))
                        .on_hover_text("Publish one of your packs to the marketplace")
                        .clicked()
                    {
                        self.open_publish();
                    }
                    if ui
                        .small_button("Log out")
                        .on_hover_text("Log out of your Overlayr account")
                        .clicked()
                    {
                        if let Err(error) = crate::auth::logout(&marketplace::api_base()) {
                            self.error = Some(format!("Could not log out: {error}"));
                        }
                        self.login_checked = None;
                    }
                } else if ui
                    .add(primary_button("Log in", ACCENT))
                    .on_hover_text("Log in on the Overlayr website to install and publish packs")
                    .clicked()
                {
                    open_in_browser(&marketplace::login_url());
                }
                if ui
                    .add_enabled(!self.busy(), egui::Button::new("⟳"))
                    .on_hover_text("Reload")
                    .clicked()
                {
                    self.refresh(ctx);
                }
                let search = ui.add(
                    egui::TextEdit::singleline(&mut self.search)
                        .hint_text("Search games and packs")
                        .desired_width(220.0),
                );
                if search.lost_focus()
                    && ui.input(|input| input.key_pressed(egui::Key::Enter))
                    && self.search != self.searched
                    && !self.busy()
                {
                    self.refresh(ctx);
                }
                if let Some((label, _)) = &self.job {
                    ui.spinner();
                    ui.label(RichText::new(label).weak());
                }
            });
        });
        ui.add_space(10.0);
    }

    fn visible(&self) -> Vec<Listing> {
        match self.filter {
            Filter::All => self.listings.clone(),
            Filter::Mine => self
                .listings
                .iter()
                .filter(|listing| Some(&listing.owner_uid) == self.session_uid.as_ref())
                .cloned()
                .collect(),
            // Installed packs show even when offline or no longer listed.
            Filter::Installed => {
                let mut installed = self.installed.values().cloned().collect::<Vec<_>>();
                installed.sort_by_key(|listing| listing.title.to_lowercase());
                installed
            }
        }
    }

    fn grid(&mut self, ui: &mut egui::Ui) {
        let listings = self.visible();
        if listings.is_empty() {
            ui.add_space(40.0);
            ui.vertical_centered(|ui| {
                let text = match self.filter {
                    _ if !self.listed_once => "Loading…",
                    Filter::All if self.searched.is_empty() => {
                        "No packs yet. Make one in the Studio and share it with ＋ Add."
                    }
                    Filter::All => "No packs match your search.",
                    Filter::Installed => "Packs you install show up here.",
                    Filter::Mine => "Packs you publish show up here.",
                };
                ui.label(RichText::new(text).weak());
            });
            return;
        }
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = egui::vec2(14.0, 14.0);
            for listing in &listings {
                if self.pack_card(ui, listing).clicked() {
                    self.selected = Some(listing.id.clone());
                }
            }
        });
        if self.filter == Filter::All && self.next_offset.is_some() {
            ui.add_space(14.0);
            ui.vertical_centered(|ui| {
                if ui
                    .add_enabled(!self.busy(), egui::Button::new("Load more"))
                    .clicked()
                {
                    let ctx = ui.ctx().clone();
                    self.load_more(&ctx);
                }
            });
            ui.add_space(14.0);
        }
    }

    fn cover(&mut self, ctx: &egui::Context, listing: &Listing) -> Option<egui::TextureHandle> {
        if listing.cover_url.is_empty() {
            return None;
        }
        let key = format!("{}:{}", listing.id, listing.version);
        if let Some(cover) = self.covers.get(&key) {
            return cover.clone();
        }
        // Not asked for yet: queue it, and show the placeholder meanwhile.
        self.covers.insert(key.clone(), None);
        let requests = self.cover_requests.get_or_insert_with(|| {
            let (requests, queue) = channel::<(String, Listing)>();
            let (results, received) = channel();
            let ctx = ctx.clone();
            std::thread::spawn(move || {
                while let Ok((key, listing)) = queue.recv() {
                    let image = marketplace::cover(&listing).ok().and_then(|png| {
                        let image =
                            image::load_from_memory_with_format(&png, image::ImageFormat::Png)
                                .ok()?
                                .to_rgba8();
                        Some(egui::ColorImage::from_rgba_unmultiplied(
                            [image.width() as usize, image.height() as usize],
                            image.as_raw(),
                        ))
                    });
                    if results.send((key, image)).is_err() {
                        return;
                    }
                    ctx.request_repaint();
                }
            });
            self.cover_results = Some(received);
            requests
        });
        let _ = requests.send((key, listing.clone()));
        None
    }

    fn pack_card(&mut self, ui: &mut egui::Ui, listing: &Listing) -> egui::Response {
        let width = CARD_WIDTH.min(ui.max_rect().width()).max(160.0);
        let cover_height = COVER_HEIGHT * width / CARD_WIDTH;
        let (rect, response) =
            ui.allocate_exact_size(egui::vec2(width, cover_height + 78.0), egui::Sense::click());
        if !ui.is_rect_visible(rect) {
            return response;
        }
        let cover = self.cover(ui.ctx(), listing);
        let visuals = ui.visuals().clone();
        let painter = ui.painter();
        let (fill, stroke) = if response.hovered() {
            (
                visuals.widgets.hovered.weak_bg_fill,
                egui::Stroke::new(1.5_f32, ACCENT),
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
        let cover_rect = egui::Rect::from_min_size(rect.min, egui::vec2(width, cover_height));
        paint_cover(painter, cover_rect, cover.as_ref(), listing, &visuals);

        let installed = self.installed.contains_key(&listing.id);
        let mine = Some(&listing.owner_uid) == self.session_uid.as_ref();
        let badge = if mine {
            Some(("Yours", ACCENT))
        } else if installed {
            Some(("Installed", SUCCESS))
        } else {
            None
        };
        if let Some((text, color)) = badge {
            let galley = painter.layout_no_wrap(
                text.into(),
                egui::FontId::proportional(11.0),
                Color32::WHITE,
            );
            let badge_rect = egui::Rect::from_min_size(
                cover_rect.right_top() + egui::vec2(-galley.size().x - 18.0, 8.0),
                galley.size() + egui::vec2(10.0, 4.0),
            );
            painter.rect_filled(badge_rect, CornerRadius::same(6), color);
            painter.galley(
                badge_rect.min + egui::vec2(5.0, 2.0),
                galley,
                Color32::WHITE,
            );
        }

        let text_left = rect.left() + 10.0;
        let max_width = width - 20.0;
        let line = |text: String, top: f32, font: egui::FontId, color: Color32| {
            let galley = painter.layout(text, font, color, max_width);
            let row = galley.rows.first().map_or(0.0, |row| row.height());
            painter
                .with_clip_rect(egui::Rect::from_min_size(
                    egui::pos2(text_left, top),
                    egui::vec2(max_width, row),
                ))
                .galley(egui::pos2(text_left, top), galley, color);
        };
        let top = cover_rect.bottom() + 8.0;
        line(
            listing.title.clone(),
            top,
            egui::FontId::proportional(15.0),
            visuals.strong_text_color(),
        );
        line(
            format!(
                "{} · {} → {}",
                if listing.application.is_empty() {
                    "Any application"
                } else {
                    &listing.application
                },
                listing.source_lang,
                listing.target_lang
            ),
            top + 22.0,
            egui::FontId::proportional(12.0),
            visuals.text_color(),
        );
        let price = if listing.price > 0 && !mine && self.owned.contains(&listing.id) {
            "Owned".into()
        } else {
            listing.price_label()
        };
        line(
            format!(
                "{price}   by {}   ⬇ {}   {} lines",
                listing.author, listing.downloads, listing.entries
            ),
            top + 42.0,
            egui::FontId::proportional(11.5),
            visuals.weak_text_color(),
        );
        response.on_hover_cursor(egui::CursorIcon::PointingHand)
    }

    fn detail_modal(&mut self, ctx: &egui::Context) -> Option<MarketplaceAction> {
        let id = self.selected.clone()?;
        let listing = self
            .listings
            .iter()
            .find(|listing| listing.id == id)
            .or_else(|| self.installed.get(&id))
            .cloned()?;
        let installed = self.installed.get(&id).cloned();
        let mine = Some(&listing.owner_uid) == self.session_uid.as_ref();
        let logged_in = self.session_uid.is_some();
        let must_buy = listing.price > 0 && !mine && !self.owned.contains(&id);
        let cover = self.cover(ctx, &listing);
        let mut action = None;
        let mut close = false;
        let width = (ctx.content_rect().width() - 48.0).clamp(300.0, 560.0);
        let modal = egui::Modal::new(egui::Id::new("marketplace-detail")).show(ctx, |ui| {
            ui.set_width(width);
            let (rect, _) =
                ui.allocate_exact_size(egui::vec2(width, width * 9.0 / 16.0), egui::Sense::hover());
            let visuals = ui.visuals().clone();
            paint_cover(ui.painter(), rect, cover.as_ref(), &listing, &visuals);
            ui.add_space(8.0);
            ui.heading(&listing.title);
            ui.label(
                RichText::new(format!(
                    "{} · {} → {} · by {}",
                    if listing.application.is_empty() {
                        "Any application"
                    } else {
                        &listing.application
                    },
                    listing.source_lang,
                    listing.target_lang,
                    listing.author
                ))
                .weak(),
            );
            ui.label(
                RichText::new(format!(
                    "{} · {} translated lines · {} installs · version {}",
                    listing.price_label(),
                    listing.entries,
                    listing.downloads,
                    listing.version
                ))
                .small()
                .weak(),
            );
            if !listing.description.is_empty() {
                ui.add_space(6.0);
                egui::ScrollArea::vertical()
                    .max_height(160.0)
                    .show(ui, |ui| ui.label(&listing.description));
            }
            ui.add_space(12.0);
            ui.horizontal(|ui| {
                let busy = self.busy();
                match &installed {
                    Some(_) => {
                        if ui
                            .add_enabled(!busy, primary_button("▶ Use this pack", ACCENT))
                            .on_hover_text("Translate with this pack (Translate tab)")
                            .clicked()
                        {
                            action = Some(MarketplaceAction::Use(id.clone()));
                            close = true;
                        }
                        if ui
                            .add_enabled(!busy, egui::Button::new("Uninstall"))
                            .clicked()
                        {
                            match marketplace::uninstall(&id) {
                                Ok(()) => {
                                    self.installed.remove(&id);
                                    action = Some(MarketplaceAction::Uninstalled(id.clone()));
                                }
                                Err(error) => {
                                    self.error = Some(format!("Could not uninstall: {error}"));
                                }
                            }
                        }
                    }
                    None if logged_in && must_buy => {
                        if ui
                            .add_enabled(
                                !busy,
                                primary_button(&format!("Buy · {}", listing.price_label()), ACCENT),
                            )
                            .on_hover_text(
                                "Pay on the Overlayr website, then come back and install",
                            )
                            .clicked()
                        {
                            open_in_browser(&marketplace::buy_url(&id));
                            self.notice = Some(
                                "Finish paying in your browser, then press Install here".into(),
                            );
                        }
                        if ui
                            .add_enabled(!busy, egui::Button::new("Install"))
                            .on_hover_text("After paying")
                            .clicked()
                        {
                            let pack_id = id.clone();
                            self.run(ctx, "Installing…", move || {
                                Done::Installed(marketplace::install(&pack_id))
                            });
                        }
                    }
                    None if logged_in => {
                        if ui
                            .add_enabled(!busy, primary_button("⬇ Install", ACCENT))
                            .clicked()
                        {
                            let pack_id = id.clone();
                            self.run(ctx, "Installing…", move || {
                                Done::Installed(marketplace::install(&pack_id))
                            });
                        }
                    }
                    None => {
                        let label = if must_buy {
                            "Log in to buy"
                        } else {
                            "Log in to install"
                        };
                        if ui.add(primary_button(label, ACCENT)).clicked() {
                            open_in_browser(&marketplace::login_url());
                        }
                    }
                }
                if let Some(installed) = &installed
                    && installed.version < listing.version
                    && logged_in
                    && ui
                        .add_enabled(!busy, egui::Button::new("Update"))
                        .on_hover_text("Updates also install when you start the pack")
                        .clicked()
                {
                    let pack_id = id.clone();
                    self.run(ctx, "Updating…", move || {
                        Done::Installed(marketplace::install(&pack_id))
                    });
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("Close").clicked() {
                        close = true;
                    }
                    if mine {
                        if self.confirm_unpublish.as_deref() == Some(id.as_str()) {
                            if ui
                                .add_enabled(!busy, primary_button("Really remove?", DANGER))
                                .clicked()
                            {
                                self.confirm_unpublish = None;
                                let pack_id = id.clone();
                                self.run(ctx, "Removing…", move || {
                                    let result = marketplace::unpublish(&pack_id);
                                    Done::Unpublished(pack_id, result)
                                });
                            }
                        } else if ui
                            .add_enabled(!busy, egui::Button::new("Remove from marketplace"))
                            .on_hover_text(if listing.price > 0 {
                                "It stops being listed; people who bought it keep it"
                            } else {
                                "Players who installed it can no longer use it"
                            })
                            .clicked()
                        {
                            self.confirm_unpublish = Some(id.clone());
                        }
                    }
                });
            });
        });
        if close || modal.should_close() {
            self.selected = None;
            self.confirm_unpublish = None;
        }
        action
    }

    fn open_publish(&mut self) {
        let packs = crate::pack::library()
            .into_iter()
            .filter(|(_, pack)| {
                pack.entries
                    .iter()
                    .any(|entry| !entry.translation.trim().is_empty())
            })
            .collect::<Vec<_>>();
        let mut form = PublishForm {
            packs,
            pack: None,
            title: String::new(),
            description: String::new(),
            cover_source: None,
            price_baht: 0,
        };
        if form.packs.len() == 1 {
            select_pack(&mut form, 0, &self.listings);
        }
        self.publish = Some(form);
    }

    fn publish_modal(
        &mut self,
        ctx: &egui::Context,
        sources: &[Source],
        thumbnails: &HashMap<String, egui::TextureHandle>,
        thumbnail_images: &HashMap<String, egui::ColorImage>,
    ) {
        let busy = self.busy();
        let listings = self.listings.clone();
        let Some(form) = &mut self.publish else {
            return;
        };
        let mut close = false;
        let mut submit = None;
        let width = (ctx.content_rect().width() - 48.0).clamp(300.0, 640.0);
        let modal = egui::Modal::new(egui::Id::new("marketplace-publish")).show(ctx, |ui| {
            ui.set_width(width);
            ui.heading("Publish a pack");
            ui.label(
                RichText::new(
                    "Players download it encrypted and can only use it in Overlayr while \
                     logged in; they can't open or export it. Publishing it again later \
                     updates it for everyone.",
                )
                .small()
                .weak(),
            );
            ui.add_space(10.0);
            if form.packs.is_empty() {
                ui.label(
                    "You don't have a pack with translations yet. Make one in the Studio first.",
                );
            } else {
                egui::Grid::new("publish-form")
                    .num_columns(2)
                    .spacing([12.0, 10.0])
                    .show(ui, |ui| {
                        ui.label("Pack");
                        let selected = form
                            .pack
                            .and_then(|index| form.packs.get(index))
                            .map_or("Choose a pack".to_string(), |(_, pack)| pack.name.clone());
                        let mut chosen = None;
                        egui::ComboBox::from_id_salt("publish-pack")
                            .width(ui.available_width())
                            .selected_text(selected)
                            .show_ui(ui, |ui| {
                                for (index, (_, pack)) in form.packs.iter().enumerate() {
                                    let label =
                                        format!("{} ({} entries)", pack.name, pack.entries.len());
                                    if ui
                                        .selectable_label(form.pack == Some(index), label)
                                        .clicked()
                                    {
                                        chosen = Some(index);
                                    }
                                }
                            });
                        if let Some(index) = chosen {
                            select_pack(form, index, &listings);
                        }
                        ui.end_row();
                        ui.label("Title");
                        ui.add(
                            egui::TextEdit::singleline(&mut form.title)
                                .char_limit(80)
                                .desired_width(f32::INFINITY),
                        );
                        ui.end_row();
                        ui.label("Description");
                        ui.add(
                            egui::TextEdit::multiline(&mut form.description)
                                .char_limit(2000)
                                .desired_rows(4)
                                .hint_text("What it covers, how complete it is, credits…")
                                .desired_width(f32::INFINITY),
                        );
                        ui.end_row();
                        ui.label("Price");
                        ui.horizontal(|ui| {
                            ui.add(
                                egui::DragValue::new(&mut form.price_baht)
                                    .range(0..=MAX_PRICE_BAHT)
                                    .prefix("฿")
                                    .speed(1.0),
                            );
                            let hint = if form.price_baht == 0 {
                                "Free".to_string()
                            } else {
                                format!("You receive {CREATOR_SHARE} of each sale")
                            };
                            ui.label(RichText::new(hint).small().weak());
                        });
                        ui.end_row();
                    });
                if form.price_baht > 0 {
                    ui.horizontal_wrapped(|ui| {
                        ui.spacing_mut().item_spacing.x = 4.0;
                        ui.label(
                            RichText::new(
                                "Selling needs payouts set up on your Overlayr profile \
                                 (creators in Thailand).",
                            )
                            .small()
                            .weak(),
                        );
                        if ui.small_button("Set up payouts ↗").clicked() {
                            open_in_browser(&marketplace::payouts_url());
                        }
                    });
                }
                if form.price_baht > 0 && form.price_baht < MIN_PRICE_BAHT {
                    ui.label(
                        RichText::new(format!("The lowest price is ฿{MIN_PRICE_BAHT}."))
                            .small()
                            .color(DANGER),
                    );
                }
                ui.add_space(8.0);
                ui.label(RichText::new("Cover").strong());
                ui.label(
                    RichText::new("Pick an open window, ideally the game, as the cover picture.")
                        .small()
                        .weak(),
                );
                egui::ScrollArea::horizontal()
                    .max_height(110.0)
                    .show(ui, |ui| {
                        ui.horizontal(|ui| {
                            if ui
                                .selectable_label(form.cover_source.is_none(), "No picture")
                                .clicked()
                            {
                                form.cover_source = None;
                            }
                            for source in sources {
                                let Some(texture) = thumbnails.get(&source.id) else {
                                    continue;
                                };
                                let selected = form.cover_source.as_ref() == Some(&source.id);
                                let image = egui::Image::new(texture)
                                    .fit_to_exact_size(egui::vec2(160.0, 90.0))
                                    .corner_radius(4);
                                let response = ui
                                    .add(egui::Button::image(image).selected(selected))
                                    .on_hover_text(&source.name);
                                if response.clicked() {
                                    form.cover_source = Some(source.id.clone());
                                }
                            }
                        });
                    });
            }
            ui.add_space(12.0);
            ui.horizontal(|ui| {
                let priced = form.price_baht == 0 || form.price_baht >= MIN_PRICE_BAHT;
                let ready = form.pack.is_some() && !form.title.trim().is_empty() && priced && !busy;
                if ui
                    .add_enabled(ready, primary_button("Publish", ACCENT))
                    .clicked()
                    && let Some((_, pack)) = form.pack.and_then(|index| form.packs.get(index))
                {
                    let cover = form
                        .cover_source
                        .as_ref()
                        .and_then(|id| thumbnail_images.get(id))
                        .and_then(cover_png);
                    submit = Some((
                        pack.clone(),
                        form.title.trim().to_owned(),
                        form.description.trim().to_owned(),
                        cover,
                        form.price_baht * 100,
                    ));
                }
                if busy {
                    ui.spinner();
                }
                if ui.button("Cancel").clicked() {
                    close = true;
                }
            });
        });
        if let Some((pack, title, description, cover, price)) = submit {
            self.run(ctx, "Publishing…", move || {
                Done::Published(marketplace::publish(
                    &pack,
                    &title,
                    &description,
                    cover.as_deref(),
                    price,
                ))
            });
        }
        if (close || modal.should_close()) && !busy {
            self.publish = None;
        }
    }
}

/// Fills the form from the chosen pack, or from its published listing.
fn select_pack(form: &mut PublishForm, index: usize, listings: &[Listing]) {
    form.pack = Some(index);
    let Some((_, pack)) = form.packs.get(index) else {
        return;
    };
    match listings.iter().find(|listing| listing.id == pack.id) {
        Some(listing) => {
            form.title = listing.title.clone();
            form.description = listing.description.clone();
            form.price_baht = listing.price / 100;
        }
        None => {
            form.title = pack.name.clone();
            form.description.clear();
            form.price_baht = 0;
        }
    }
}

/// A window preview as a PNG small enough for the marketplace.
fn cover_png(image: &egui::ColorImage) -> Option<Vec<u8>> {
    let [width, height] = image.size;
    let rgba = image
        .pixels
        .iter()
        .flat_map(|pixel| pixel.to_srgba_unmultiplied())
        .collect::<Vec<_>>();
    let mut picture = image::RgbaImage::from_raw(width as u32, height as u32, rgba)?;
    if picture.width() > COVER_WIDTH {
        let height = (u64::from(picture.height()) * u64::from(COVER_WIDTH)
            / u64::from(picture.width()))
        .max(1) as u32;
        picture = image::imageops::thumbnail(&picture, COVER_WIDTH, height);
    }
    let mut png = Vec::new();
    image::DynamicImage::ImageRgba8(picture)
        .to_rgb8()
        .write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png)
        .ok()?;
    Some(png)
}

fn paint_cover(
    painter: &egui::Painter,
    rect: egui::Rect,
    cover: Option<&egui::TextureHandle>,
    listing: &Listing,
    visuals: &egui::Visuals,
) {
    let rounding = CornerRadius {
        nw: 10,
        ne: 10,
        sw: 0,
        se: 0,
    };
    match cover {
        Some(texture) => {
            // Fill the area, cropping the picture's overflow.
            let size = texture.size_vec2();
            let scale = (rect.width() / size.x).max(rect.height() / size.y);
            let shown = egui::vec2(
                rect.width() / (size.x * scale),
                rect.height() / (size.y * scale),
            );
            let uv = egui::Rect::from_center_size(egui::pos2(0.5, 0.5), shown);
            painter.add(
                egui::epaint::RectShape::filled(rect, rounding, Color32::WHITE)
                    .with_texture(texture.id(), uv),
            );
        }
        None => {
            painter.rect_filled(rect, rounding, ACCENT.gamma_multiply(0.25));
            let initial = listing
                .application
                .chars()
                .chain(listing.title.chars())
                .find(|character| character.is_alphanumeric())
                .map_or_else(
                    || "?".into(),
                    |character| character.to_uppercase().to_string(),
                );
            painter.text(
                rect.center(),
                egui::Align2::CENTER_CENTER,
                initial,
                egui::FontId::proportional(rect.height() * 0.4),
                visuals.strong_text_color(),
            );
        }
    }
}

/// A dismissible message line. Returns true when dismissed.
fn message(ui: &mut egui::Ui, text: &str, color: Color32) -> bool {
    let mut dismissed = false;
    egui::Frame::new()
        .fill(color.gamma_multiply(0.15))
        .corner_radius(CornerRadius::same(8))
        .inner_margin(egui::Margin::symmetric(12, 6))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                if ui.small_button("✖").clicked() {
                    dismissed = true;
                }
                ui.add(egui::Label::new(RichText::new(text).color(color)).wrap());
            });
        });
    ui.add_space(8.0);
    dismissed
}

/// Opens `url` in the default browser.
pub fn open_in_browser(url: &str) {
    #[cfg(target_os = "windows")]
    let command = std::process::Command::new("cmd")
        .args(["/C", "start", ""])
        .arg(url)
        .spawn();
    #[cfg(target_os = "macos")]
    let command = std::process::Command::new("open").arg(url).spawn();
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let command = std::process::Command::new("xdg-open").arg(url).spawn();
    if let Err(error) = command {
        eprintln!("Could not open {url}: {error}");
    }
}
