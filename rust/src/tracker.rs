//! Follows recognised text between frames so OCR and translation only run for
//! text that is actually new.
//!
//! Each frame goes through three thresholds:
//! - **Change**: tiles that differ from the previous frame. An unchanged frame
//!   costs one comparison and nothing else.
//! - **Follow**: known text inside changed areas is looked for at its old
//!   position and nearby. If its pixels still match closely enough, its
//!   translation simply moves with it — no OCR, no translation.
//! - **Detect**: only when enough of the frame changed in ways no known text
//!   explains (new text appeared) does OCR run again.
//!
//! After OCR, each recognised text is compared with known text; above the
//! match threshold it reuses the existing translation, which also absorbs OCR
//! noise such as "gour" for "your".

use std::collections::{HashMap, VecDeque};

use crate::pack::TextStyle;
use crate::services::ServiceResult;
use crate::types::{Frame, Rect, TextBlock};

/// Side length of the tiles used for change detection, in image pixels.
const TILE: usize = 16;
/// Mean per-pixel difference above which a tile counts as changed. Small
/// enough for text changes, large enough to ignore compression noise.
const TILE_CHANGE_LEVEL: u32 = 6;
/// Downscale factor for the wide search for moved text.
const COARSE: usize = 4;
/// How long an area that changed without new text is ignored. In time, not
/// frames: capture runs faster while translations are shown.
const ANIMATED_TIME: std::time::Duration = std::time::Duration::from_secs(15);
/// Brightness difference from the background that marks a text stroke.
const INK_LEVEL: u8 = 40;
/// Fewer stroke samples than this falls back to comparing whole patches.
const MIN_INK_SAMPLES: usize = 8;
/// Detections that may miss lost text before its area can count as
/// animation.
const MAX_LOST_MISSES: u8 = 2;
/// Similarity at which text in an animated area still counts as there.
const ANIMATED_KEEP_SIMILARITY: f32 = 0.5;
/// Share of a text's strokes (and of the gaps between them) that must still
/// be in place for restyled text to count as the same text.
const RESTYLED_KEEP_SIMILARITY: f32 = 0.8;
/// How far (in pixels) anchored text may have shifted from where the
/// creator recorded it.
const ANCHOR_SEARCH: i32 = 6;
/// How closely OCR text must match an anchor's text for its box to show
/// where that text sits.
const ANCHOR_TEXT_SIMILARITY: f32 = 0.8;
/// How much lower the whole-patch similarity of moved text may be than the
/// follow threshold (backgrounds shift a little when text scrolls).
const MOVED_PATCH_SLACK: f32 = 0.1;
/// Disappeared texts remembered for instant recall.
const RECALL_CAPACITY: usize = 256;
/// Mean deviation below which a tile counts as plain background.
const BLANK_SPREAD: u32 = 6;
/// Pixel samples per patch comparison; patches are subsampled to this size so
/// large paragraphs cost the same as short words.
const PATCH_SAMPLES: usize = 2_000;
/// Translations remembered after their text leaves the screen, so text that
/// returns later is not translated again.
const MEMORY_CAPACITY: usize = 1_024;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TrackerSettings {
    /// Minimum text similarity (0–1) for recognised text to reuse an existing
    /// translation.
    pub match_threshold: f32,
    /// Minimum pixel similarity (0–1) for known text to count as still
    /// present, at its old position or after moving.
    pub follow_threshold: f32,
    /// Fraction of the frame (0–1) that must change, unexplained by known
    /// text, before OCR runs again.
    pub detect_threshold: f32,
    /// How far moved text is searched for, in image pixels.
    pub search_radius: i32,
    /// Report kept text as moved whenever the picture under it changes,
    /// so it is redrawn over its new background (e.g. a hover highlight).
    pub follow_highlights: bool,
}

impl Default for TrackerSettings {
    fn default() -> Self {
        Self {
            match_threshold: 0.85,
            follow_threshold: 0.8,
            detect_threshold: 0.001,
            search_radius: 96,
            follow_highlights: true,
        }
    }
}

/// A translation for recognised text, optionally styled by a pack.
#[derive(Clone, Debug, PartialEq)]
pub struct Translated {
    pub text: String,
    pub style: Option<TextStyle>,
}

impl Translated {
    pub fn plain(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            style: None,
        }
    }
}

/// A read of the screen handed out by [`Tracker::take_detection`].
#[derive(Clone, Debug, Default)]
pub struct Detection {
    /// The area to read; `None` for the whole frame.
    pub region: Option<Rect>,
    trigger: Vec<Rect>,
}

/// What a new frame requires from the pipeline.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Update {
    /// Nothing visible changed.
    Unchanged,
    /// Known text moved or disappeared; publish [`Tracker::blocks`].
    Moved,
    /// New text may have appeared; run OCR and call [`Tracker::reconcile`].
    Detect,
}

pub struct Tracker {
    settings: TrackerSettings,
    items: Vec<TrackedText>,
    memory: VecDeque<(String, Translated)>,
    previous: Option<GrayImage>,
    previous_bounds: Option<Rect>,
    pending_detection: bool,
    /// When new content last changed on screen, to read it once it has
    /// settled rather than halfway through a fade or blur.
    last_new_content: Option<std::time::Instant>,
    frame_index: u64,
    /// Changed tiles that caused the pending detection.
    trigger: Vec<Rect>,
    /// Tiles that change without new text appearing (animated icons, colour
    /// cycling), mapped to the frame until which they are ignored.
    animated: HashMap<(i32, i32), std::time::Instant>,
    /// Image area the pending detection should read; `None` for the whole
    /// frame.
    region: Option<Rect>,
    /// Text that disappeared, kept so it is restored instantly, translation
    /// and all, if it reappears in the same place (e.g. a reopened menu).
    recall: VecDeque<TrackedText>,
    /// Where tracked text was recently lost, with how many detections since
    /// missed it. These areas aren't mistaken for animation right away: a
    /// lost text often just needs reading again.
    lost_areas: Vec<(Rect, u8)>,
    anchors: Vec<Anchor>,
}

#[derive(Clone)]
struct TrackedText {
    block: TextBlock,
    /// Position in image pixels of the most recent frame.
    pixels: Rect,
    /// How the text looked when it was recognised.
    patch: Patch,
    /// The same, downscaled by [`COARSE`] for the wide search.
    coarse_patch: Patch,
    /// Set for text recognised by a pack anchor: its translation is locked
    /// and OCR results never replace it.
    anchor: Option<usize>,
}

/// A pack text the creator confirmed in the studio: where it was and how it
/// looked, with its translation.
#[derive(Clone, Debug)]
pub struct AnchorTarget {
    /// Capture size the anchor was recorded at.
    pub frame_width: u32,
    pub frame_height: u32,
    /// Area in image pixels.
    pub area: Rect,
    /// Grayscale (brightest-channel) pixels of the area.
    pub width: u32,
    pub height: u32,
    pub pixels: Vec<u8>,
    pub source_text: String,
    pub translation: Translated,
}

struct Anchor {
    frame_size: (usize, usize),
    area: Rect,
    patch: Patch,
    source_text: String,
    translation: Translated,
    active: bool,
    /// Whether it is looked for in frames of `usable_for` size (see
    /// [`select_anchors`]).
    usable: bool,
    usable_for: (usize, usize),
    /// Where OCR found the text within `area`, relative to it: recordings
    /// often include an icon or highlight beside the text, which the
    /// translation must neither cover nor be sized by.
    text_area: Option<Rect>,
}

impl Tracker {
    pub fn new(settings: TrackerSettings) -> Self {
        Self {
            settings,
            items: Vec::new(),
            memory: VecDeque::new(),
            previous: None,
            previous_bounds: None,
            pending_detection: true,
            last_new_content: None,
            frame_index: 0,
            trigger: Vec::new(),
            animated: HashMap::new(),
            region: None,
            recall: VecDeque::new(),
            lost_areas: Vec::new(),
            anchors: Vec::new(),
        }
    }

    /// Pack texts to recognise by their pixels (see [`AnchorTarget`]).
    /// Matching text gets its translation without OCR, locked in place.
    pub fn set_anchors(&mut self, targets: Vec<AnchorTarget>) {
        self.items.retain(|item| item.anchor.is_none());
        self.anchors = targets
            .into_iter()
            .filter(|target| target.pixels.len() == (target.width * target.height) as usize)
            .map(|target| Anchor {
                frame_size: (target.frame_width as usize, target.frame_height as usize),
                area: target.area,
                patch: Patch::new(GrayImage {
                    width: target.width as usize,
                    height: target.height as usize,
                    pixels: target.pixels,
                }),
                source_text: target.source_text,
                translation: target.translation,
                active: false,
                usable: false,
                usable_for: (0, 0),
                text_area: None,
            })
            .collect();
    }

    /// The image area the pending detection should read, in pixels of the
    /// frame passed to [`Tracker::update`]; `None` means the whole frame.
    pub fn detection_region(&self) -> Option<Rect> {
        self.region
    }

    /// How long the screen has gone without new content changing. Reading
    /// text while a menu still fades in misses it, and in a window too small
    /// for the pack's anchors nothing else would translate it.
    pub fn settled_for(&self) -> std::time::Duration {
        self.last_new_content
            .map_or(std::time::Duration::MAX, |at| at.elapsed())
    }

    /// Hands out the pending detection to read in the background. Tracking
    /// goes on meanwhile; pass the results to [`Tracker::reconcile_detection`].
    pub fn take_detection(&mut self) -> Detection {
        self.pending_detection = false;
        Detection {
            region: self.region.take(),
            trigger: std::mem::take(&mut self.trigger),
        }
    }

    /// Like [`Tracker::reconcile_region`], for a detection taken earlier with
    /// [`Tracker::take_detection`] and read from `frame`. Changes seen since
    /// stay pending, and text that has vanished from the screen while it was
    /// being read is dropped rather than shown.
    pub fn reconcile_detection(
        &mut self,
        frame: &Frame,
        blocks: Vec<TextBlock>,
        detection: Detection,
        translate: impl FnMut(&[String]) -> ServiceResult<Vec<Option<Translated>>>,
    ) -> ServiceResult<()> {
        let pending = (
            self.pending_detection,
            self.region.replace(detection.region.unwrap_or_default()),
            std::mem::replace(&mut self.trigger, detection.trigger),
        );
        if detection.region.is_none() {
            self.region = None;
        }
        let result = self.reconcile_region(frame, blocks, translate);
        (self.pending_detection, self.region, self.trigger) = pending;
        let threshold = self.settings.follow_threshold;
        if let Some(latest) = self.previous.as_ref().filter(|latest| {
            GrayImage::from_frame(frame).is_some_and(|read| read.same_size(latest))
        }) {
            let mut vanished = Vec::new();
            self.items.retain(|item| {
                let (left, top) = (item.pixels.left, item.pixels.top);
                // Text only restyled meanwhile (a hover highlight fading out
                // as the pointer left) is still there.
                let keep = item.anchor.is_some()
                    || !detection
                        .region
                        .is_none_or(|region| mostly_inside(item.pixels, region))
                    || patch_similarity(latest, &item.patch, left, top) >= threshold
                    || restyled_similarity(latest, &item.patch, left, top)
                        >= RESTYLED_KEEP_SIMILARITY;
                if !keep {
                    vanished.push(item.pixels);
                }
                keep
            });
            // Whatever replaced it may be text too, and the screen may not
            // change again to trigger a read: read the area again.
            if !vanished.is_empty() {
                let text = self
                    .items
                    .iter()
                    .map(|item| item.pixels)
                    .collect::<Vec<_>>();
                let region = detection_region(&vanished, &text, latest.width, latest.height);
                self.region = if self.pending_detection {
                    self.region.zip(region).map(|(old, new)| union(old, new))
                } else {
                    region
                };
                self.pending_detection = true;
                self.lost_areas
                    .extend(vanished.iter().map(|area| (expand(*area, 4), 0)));
            }
        }
        result
    }

    /// Makes the next [`Tracker::update`] request detection regardless of
    /// what changed.
    pub fn request_detection(&mut self) {
        self.pending_detection = true;
        self.region = None;
    }

    pub fn update(&mut self, frame: &Frame) -> Update {
        self.frame_index += 1;
        let Some(current) = GrayImage::from_frame(frame) else {
            self.pending_detection = true;
            self.trigger.clear();
            self.region = None;
            return Update::Detect;
        };
        let bounds_changed = self.previous_bounds.replace(frame.bounds) != Some(frame.bounds);
        let previous = self.previous.replace(current);
        let current = self.previous.as_ref().expect("frame was just stored");
        let Some(previous) = previous.filter(|previous| previous.same_size(current)) else {
            self.pending_detection = true;
            self.trigger.clear();
            self.region = None;
            return Update::Detect;
        };
        // Already waiting for a read: keep following and dropping text all
        // the same, so vanished text disappears at once, and add any new
        // changes to the area to read.
        let was_pending = self.pending_detection;

        let changed = changed_tiles(&previous, current);
        if changed.is_empty() {
            // The window may have moved on screen without its contents changing.
            return if was_pending {
                Update::Detect
            } else if bounds_changed && !self.items.is_empty() {
                Update::Moved
            } else {
                Update::Unchanged
            };
        }

        let mut moved = bounds_changed;
        let mut explained = Vec::new();
        let mut lost = Vec::new();
        let mut offsets = Vec::new();
        let settings = self.settings;
        let coarse = current.downscale(COARSE);
        let now = std::time::Instant::now();
        let animated = &self.animated;
        let is_animated = |tile: &Rect| {
            animated
                .get(&tile_key(*tile))
                .is_some_and(|&until| until >= now)
        };
        self.items.retain_mut(|item| {
            if item.anchor.is_some()
                || !changed
                    .iter()
                    .any(|tile| overlaps(*tile, expand(item.pixels, 4)))
            {
                return true;
            }
            match locate(current, &coarse, item, &offsets, settings) {
                Some(found) => {
                    explained.push(found);
                    if found != item.pixels {
                        offsets.push((found.left - item.pixels.left, found.top - item.pixels.top));
                        item.pixels = found;
                    }
                    // Something changed under the text even if it stayed put
                    // (a hover highlight): redraw it, so its background is
                    // sampled again and matches.
                    if settings.follow_highlights || found != item.pixels {
                        moved = true;
                    }
                    true
                }
                // Text in an animated area (e.g. colour-cycling, or a
                // translucent panel over a moving game world) can fail to
                // match while still being there; keep it in place, but only
                // while it is still recognisable there: when the panel
                // closes, the text is gone.
                None if changed
                    .iter()
                    .any(|tile| overlaps(*tile, item.pixels) && is_animated(tile))
                    && partly_covered_similarity(
                        current,
                        &item.patch,
                        item.pixels.left,
                        item.pixels.top,
                    ) >= ANIMATED_KEEP_SIMILARITY =>
                {
                    explained.push(item.pixels);
                    true
                }
                // The same text restyled where it is, e.g. a menu row
                // highlighted under the pointer: its strokes are where they
                // were, in other colours. Keeping it stops the translation
                // flickering off and on while hovering.
                None if restyled_similarity(
                    current,
                    &item.patch,
                    item.pixels.left,
                    item.pixels.top,
                ) >= RESTYLED_KEEP_SIMILARITY =>
                {
                    explained.push(item.pixels);
                    // Redraw it, so its background follows the new colours.
                    moved |= settings.follow_highlights;
                    true
                }
                None => {
                    moved = true;
                    lost.push(item.clone());
                    false
                }
            }
        });

        // Text that returns to where it was before (a reopened menu or
        // dialog) is recognised by its pixels and restored without OCR.
        let mut recalled = Vec::new();
        self.recall.retain(|item| {
            let returned = changed.iter().any(|tile| overlaps(*tile, item.pixels))
                && patch_similarity(current, &item.patch, item.pixels.left, item.pixels.top)
                    >= settings.follow_threshold;
            if returned {
                recalled.push(item.clone());
            }
            !returned
        });
        for item in recalled {
            if !self
                .items
                .iter()
                .any(|known| overlaps(known.pixels, item.pixels))
            {
                explained.push(item.pixels);
                self.items.push(item);
                moved = true;
            }
        }
        let (anchored, anchors_changed, anchors_lost) = check_anchors(
            &mut self.anchors,
            &mut self.items,
            current,
            Some(&changed),
            settings.follow_threshold,
        );
        lost.extend(anchors_lost);
        self.lost_areas
            .extend(lost.iter().map(|item| (expand(item.pixels, 4), 0)));
        remember(&mut self.recall, lost.iter().cloned());
        explained.extend(anchored);
        moved |= anchors_changed;

        // A changed tile is explained if known text is there now, or if it is
        // now plain background (text moved or vanished from it). Anything
        // else is new content worth reading.
        // When text scrolled, graphics around it usually scrolled the same
        // way; content that matches the previous frame shifted by that offset
        // is not new.
        let scroll = dominant_offset(&offsets);
        // Where tracked text just vanished (e.g. restyled under a hovering
        // cursor) the screen is read again at once, even in an animated area:
        // otherwise the text stays untranslated until the area calms down.
        let lost_here = |tile: &Rect| lost.iter().any(|item| overlaps(*tile, item.pixels));
        let unexplained = changed
            .into_iter()
            .filter(|tile| !explained.iter().any(|rect| overlaps(*tile, *rect)))
            .filter(|tile| lost_here(tile) || !is_animated(tile))
            .filter(|tile| !is_blank(current, *tile))
            .filter(|tile| {
                !scroll.is_some_and(|offset| moved_with(&previous, current, *tile, offset))
            })
            .collect::<Vec<_>>();
        let unexplained_area = unexplained
            .iter()
            .map(|tile| u64::from(tile.width) * u64::from(tile.height))
            .sum::<u64>();
        let frame_area = (current.width * current.height).max(1) as f64;
        if unexplained_area > 0
            && unexplained_area as f64 / frame_area >= f64::from(settings.detect_threshold)
        {
            self.pending_detection = true;
            self.last_new_content = Some(std::time::Instant::now());
            let text = self
                .items
                .iter()
                .map(|item| item.pixels)
                .chain(self.lost_areas.iter().map(|(area, _)| *area))
                .collect::<Vec<_>>();
            let region = detection_region(&unexplained, &text, current.width, current.height);
            if was_pending {
                // `None` reads the whole frame, which covers both.
                self.region = self.region.zip(region).map(|(old, new)| union(old, new));
                self.trigger.extend(unexplained);
            } else {
                self.region = region;
                self.trigger = unexplained;
            }
            Update::Detect
        } else if was_pending {
            Update::Detect
        } else if moved {
            Update::Moved
        } else {
            Update::Unchanged
        }
    }

    /// Replaces the tracked text with fresh OCR results. Text similar enough
    /// to something already known reuses its translation; only new text is
    /// passed to `translate`. On error the tracker keeps its previous state
    /// and requests detection again on the next frame.
    pub fn reconcile(
        &mut self,
        frame: &Frame,
        blocks: Vec<TextBlock>,
        mut translate: impl FnMut(&str) -> ServiceResult<Option<Translated>>,
    ) -> ServiceResult<()> {
        self.region = None;
        self.reconcile_region(frame, blocks, |texts| {
            texts.iter().map(|text| translate(text)).collect()
        })
    }

    /// Like [`Tracker::reconcile`], for OCR results of just
    /// [`Tracker::detection_region`]: text outside the region keeps being
    /// tracked; text inside it is replaced by `blocks`.
    pub fn reconcile_region(
        &mut self,
        frame: &Frame,
        blocks: Vec<TextBlock>,
        mut translate: impl FnMut(&[String]) -> ServiceResult<Vec<Option<Translated>>>,
    ) -> ServiceResult<()> {
        let image = GrayImage::from_frame(frame);
        // Anchored text is recognised by its pixels; OCR results over it are
        // ignored so a misread can't replace the creator's translation.
        if let Some(image) = &image {
            let (_, _, lost) = check_anchors(
                &mut self.anchors,
                &mut self.items,
                image,
                None,
                self.settings.follow_threshold,
            );
            remember(&mut self.recall, lost);
        }
        for item in &self.items {
            let Some(anchor) = item.anchor.and_then(|index| self.anchors.get_mut(index)) else {
                continue;
            };
            let reading = blocks.iter().find(|block| {
                overlaps(item.pixels, frame.to_pixels(block.bounds))
                    && text_similarity(
                        &normalize(&block.source_text),
                        &normalize(&anchor.source_text),
                    ) >= ANCHOR_TEXT_SIMILARITY
            });
            if let Some(block) = reading {
                anchor.text_area = text_within(item.pixels, frame.to_pixels(block.bounds));
            }
        }
        let anchored = self
            .items
            .iter()
            .filter(|item| item.anchor.is_some())
            .map(|item| item.pixels)
            .collect::<Vec<_>>();
        let blocks = blocks
            .into_iter()
            .filter(|block| {
                let pixels = frame.to_pixels(block.bounds);
                !anchored.iter().any(|area| overlaps(*area, pixels))
            })
            .collect::<Vec<_>>();
        let blocks = self.rejoin_fragments(frame, blocks);
        // Known text reuses its translation; everything new is translated
        // in one call, so a provider can handle it all at once.
        let known = blocks
            .iter()
            .map(|block| self.known_translation(&block.source_text))
            .collect::<Vec<_>>();
        let new_texts = blocks
            .iter()
            .zip(&known)
            .filter(|(_, known)| known.is_none())
            .map(|(block, _)| block.source_text.clone())
            .collect::<Vec<_>>();
        let mut fresh = if new_texts.is_empty() {
            Vec::new()
        } else {
            translate(&new_texts)?
        }
        .into_iter();
        let mut items = Vec::with_capacity(blocks.len());
        let mut learned = Vec::new();
        for (mut block, known) in blocks.into_iter().zip(known) {
            let translation = match known {
                Some(translation) => Some(translation),
                None => {
                    let translation = fresh.next().flatten();
                    if let Some(translation) = &translation {
                        learned.push((normalize(&block.source_text), translation.clone()));
                    }
                    translation
                }
            };
            // Text without a translation is still tracked, so it isn't read
            // again, but nothing is drawn over it.
            if let Some(translation) = translation {
                block.translated_text = Some(translation.text);
                block.style = translation.style;
            }
            let pixels = frame.to_pixels(block.bounds);
            let patch = image
                .as_ref()
                .map(|image| image.crop(pixels))
                .unwrap_or_default();
            items.push(TrackedText {
                block,
                pixels,
                coarse_patch: Patch::new(patch.downscale(COARSE)),
                patch: Patch::new(patch),
                anchor: None,
            });
        }
        for entry in learned {
            self.memory.push_back(entry);
            if self.memory.len() > MEMORY_CAPACITY {
                self.memory.pop_front();
            }
        }
        // Areas that triggered this detection but hold no new text are
        // animation; ignore them for a while so they don't trigger OCR on
        // every frame.
        let fresh = items
            .iter()
            .filter(|item| !self.was_tracked_nearby(item))
            .map(|item| expand(item.pixels, 4))
            .collect::<Vec<_>>();
        let now = std::time::Instant::now();
        let until = now + ANIMATED_TIME;
        self.animated.retain(|_, expiry| *expiry >= now);
        for tile in std::mem::take(&mut self.trigger) {
            let text_was_lost_here = self
                .lost_areas
                .iter()
                .any(|(area, misses)| *misses < MAX_LOST_MISSES && overlaps(tile, *area));
            if !text_was_lost_here && !fresh.iter().any(|rect| overlaps(tile, *rect)) {
                // Include neighbouring tiles: the edges of an animation vary
                // from frame to frame.
                let (column, row) = tile_key(tile);
                for dy in -1..=1 {
                    for dx in -1..=1 {
                        self.animated.insert((column + dx, row + dy), until);
                    }
                }
            }
        }
        // Lost text that was read again is resolved; otherwise count a miss.
        self.lost_areas.retain_mut(|(area, misses)| {
            *misses += 1;
            *misses <= MAX_LOST_MISSES && !items.iter().any(|item| overlaps(item.pixels, *area))
        });
        // Outside a partial read, existing text stays tracked unless the new
        // results cover it.
        let (kept, replaced): (Vec<_>, Vec<_>) = std::mem::take(&mut self.items)
            .into_iter()
            .partition(|old| {
                old.anchor.is_some()
                    || (self
                        .region
                        .is_some_and(|region| !mostly_inside(old.pixels, region))
                        && !items
                            .iter()
                            .any(|new: &TrackedText| overlaps(new.pixels, old.pixels)))
            });
        let replaced = replaced
            .into_iter()
            .filter(|old| {
                !items.iter().any(|new| {
                    text_similarity(
                        &normalize(&new.block.source_text),
                        &normalize(&old.block.source_text),
                    ) >= self.settings.match_threshold
                })
            })
            .collect::<Vec<_>>();
        remember(&mut self.recall, replaced);
        self.items = kept.into_iter().chain(items).collect();
        self.pending_detection = false;
        self.region = None;
        Ok(())
    }

    /// The tracked text with screen positions for `frame`.
    pub fn blocks(&self, frame: &Frame) -> Vec<TextBlock> {
        self.items
            .iter()
            .map(|item| {
                let pixels = item
                    .anchor
                    .and_then(|index| self.anchors.get(index)?.text_area)
                    .map_or(item.pixels, |text| {
                        Rect::new(
                            item.pixels.left + text.left,
                            item.pixels.top + text.top,
                            text.width,
                            text.height,
                        )
                    });
                TextBlock {
                    bounds: frame.to_screen(pixels.left, pixels.top, pixels.width, pixels.height),
                    ..item.block.clone()
                }
            })
            .collect()
    }

    /// Whether `item` was already tracked with similar text at about the
    /// same place, i.e. it is not newly appeared text.
    fn was_tracked_nearby(&self, item: &TrackedText) -> bool {
        let text = normalize(&item.block.source_text);
        self.items.iter().any(|known| {
            (known.pixels.left - item.pixels.left).abs() <= 8
                && (known.pixels.top - item.pixels.top).abs() <= 8
                && text_similarity(&normalize(&known.block.source_text), &text)
                    >= self.settings.match_threshold
        })
    }

    /// OCR results that are pieces of a translated text that was just at the
    /// same spot (a mouse pointer or a hover effect splits a menu item into
    /// "Generating" and "Resources") are put back together as that text, so
    /// its translation shows at once instead of the pieces being translated
    /// on their own.
    fn rejoin_fragments(&self, frame: &Frame, blocks: Vec<TextBlock>) -> Vec<TextBlock> {
        let known = self
            .items
            .iter()
            .chain(&self.recall)
            .filter(|item| item.anchor.is_none() && item.block.translated_text.is_some())
            .collect::<Vec<_>>();
        let mut blocks = blocks.into_iter().map(Some).collect::<Vec<_>>();
        let mut joined = Vec::new();
        for item in known {
            let whole = normalize(&item.block.source_text);
            // Hover styles can enlarge the font, so allow some room around it.
            let area = expand(item.pixels, 6.max(item.pixels.height as i32 / 2));
            let pieces = blocks
                .iter()
                .enumerate()
                .filter_map(|(index, block)| {
                    let block = block.as_ref()?;
                    let text = normalize(&block.source_text);
                    let inside = mostly_inside(frame.to_pixels(block.bounds), area);
                    (inside && text.chars().count() >= 3 && text != whole && whole.contains(&text))
                        .then_some(index)
                })
                .collect::<Vec<_>>();
            if pieces.is_empty() {
                continue;
            }
            let confidence = pieces
                .iter()
                .filter_map(|&index| blocks[index].as_ref().map(|block| block.confidence))
                .fold(1.0_f32, f32::min);
            for index in pieces {
                blocks[index] = None;
            }
            joined.push(TextBlock {
                bounds: frame.to_screen(
                    item.pixels.left,
                    item.pixels.top,
                    item.pixels.width,
                    item.pixels.height,
                ),
                lines: item.block.lines,
                ..TextBlock::new(Rect::default(), item.block.source_text.clone(), confidence)
            });
        }
        blocks.into_iter().flatten().chain(joined).collect()
    }

    /// Fills in translations that arrived after their text was read (from
    /// a background translator). Returns whether any text changed.
    pub fn apply_translations(&mut self, translations: &[(String, Translated)]) -> bool {
        let mut changed = false;
        for (source, translation) in translations {
            let source = normalize(source);
            for item in &mut self.items {
                if item.anchor.is_none()
                    && item.block.translated_text.is_none()
                    && normalize(&item.block.source_text) == source
                {
                    item.block.translated_text = Some(translation.text.clone());
                    item.block.style = translation.style.clone();
                    changed = true;
                }
            }
            self.memory.push_back((source, translation.clone()));
            if self.memory.len() > MEMORY_CAPACITY {
                self.memory.pop_front();
            }
        }
        changed
    }

    fn known_translation(&self, text: &str) -> Option<Translated> {
        let text = normalize(text);
        let threshold = self.settings.match_threshold;
        let current = self.items.iter().filter_map(|item| {
            Some((
                normalize(&item.block.source_text),
                Translated {
                    text: item.block.translated_text.clone()?,
                    style: item.block.style.clone(),
                },
            ))
        });
        let remembered = self.memory.iter().cloned();
        current
            .chain(remembered)
            .map(|(source, translation)| (text_similarity(&text, &source), translation))
            .filter(|(similarity, _)| *similarity >= threshold)
            .max_by(|left, right| left.0.total_cmp(&right.0))
            .map(|(_, translation)| translation)
    }
}

/// Finds `item` in `image`: at its old position, at offsets other text just
/// moved by (scrolling moves everything together), then by searching nearby.
fn locate(
    image: &GrayImage,
    coarse: &GrayImage,
    item: &TrackedText,
    offsets: &[(i32, i32)],
    settings: TrackerSettings,
) -> Option<Rect> {
    if item.patch.is_empty() {
        return None;
    }
    let at = |dx: i32, dy: i32| {
        let rect = Rect::new(
            item.pixels.left + dx,
            item.pixels.top + dy,
            item.pixels.width,
            item.pixels.height,
        );
        (
            patch_similarity(image, &item.patch, rect.left, rect.top),
            rect,
        )
    };
    // In place, a pointer or small effect over part of the text mustn't
    // make it count as gone.
    if partly_covered_similarity(image, &item.patch, item.pixels.left, item.pixels.top)
        >= settings.follow_threshold
    {
        return Some(item.pixels);
    }
    let mut best = at(0, 0);
    // Lines of the same font look alike stroke by stroke, so text found at a
    // new position must also match as a whole patch, background included;
    // otherwise a title could "move" onto a line of a paragraph.
    let moved_here = |(similarity, rect): (f32, Rect)| {
        similarity >= settings.follow_threshold
            && plain_similarity(image, &item.patch.image, rect.left, rect.top)
                >= settings.follow_threshold - MOVED_PATCH_SLACK
    };
    for &(dx, dy) in offsets {
        let candidate = at(dx, dy);
        if moved_here(candidate) {
            return Some(candidate.1);
        }
        if candidate.0 > best.0 {
            best = candidate;
        }
    }

    // Wide search on downscaled images: averaging keeps text recognisable
    // even when the true offset falls between coarse steps.
    let scale = COARSE as i32;
    let (origin_x, origin_y) = (item.pixels.left / scale, item.pixels.top / scale);
    let radius = (settings.search_radius / scale).max(1);
    let mut coarse_best = (f32::MIN, (0, 0));
    if !item.coarse_patch.is_empty() {
        for dy in -radius..=radius {
            for dx in -radius..=radius {
                let similarity =
                    patch_similarity(coarse, &item.coarse_patch, origin_x + dx, origin_y + dy);
                if similarity > coarse_best.0 {
                    coarse_best = (similarity, (dx, dy));
                }
            }
        }
    }

    // Refine at full resolution around the coarse match.
    let (center_x, center_y) = (
        (origin_x + coarse_best.1.0) * scale - item.pixels.left,
        (origin_y + coarse_best.1.1) * scale - item.pixels.top,
    );
    for dy in center_y - scale..=center_y + scale {
        for dx in center_x - scale..=center_x + scale {
            let candidate = at(dx, dy);
            if candidate.0 > best.0 {
                best = candidate;
            }
        }
    }
    moved_here(best).then_some(best.1)
}

/// Grayscale image using each pixel's brightest channel, matching what OCR
/// sees, so coloured text on dark backgrounds keeps its contrast.
#[derive(Clone, Default)]
struct GrayImage {
    width: usize,
    height: usize,
    pixels: Vec<u8>,
}

impl GrayImage {
    fn from_frame(frame: &Frame) -> Option<Self> {
        let width = frame.width as usize;
        let height = frame.height as usize;
        if width == 0 || height == 0 || frame.rgba.len() != width * height * 4 {
            return None;
        }
        let pixels = frame
            .rgba
            .as_chunks::<4>()
            .0
            .iter()
            .map(|pixel| pixel[0].max(pixel[1]).max(pixel[2]))
            .collect();
        Some(Self {
            width,
            height,
            pixels,
        })
    }

    fn same_size(&self, other: &Self) -> bool {
        self.width == other.width && self.height == other.height
    }

    fn is_empty(&self) -> bool {
        self.pixels.is_empty()
    }

    fn get(&self, x: usize, y: usize) -> u8 {
        self.pixels[y * self.width + x]
    }

    /// Box-filtered copy, `factor` times smaller in each direction.
    fn downscale(&self, factor: usize) -> Self {
        let width = self.width / factor;
        let height = self.height / factor;
        if width == 0 || height == 0 {
            return Self::default();
        }
        let area = (factor * factor) as u32;
        let pixels = (0..height)
            .flat_map(|y| (0..width).map(move |x| (x, y)))
            .map(|(x, y)| {
                let sum = (0..factor)
                    .flat_map(|dy| (0..factor).map(move |dx| (dx, dy)))
                    .map(|(dx, dy)| u32::from(self.get(x * factor + dx, y * factor + dy)))
                    .sum::<u32>();
                (sum / area) as u8
            })
            .collect();
        Self {
            width,
            height,
            pixels,
        }
    }

    fn crop(&self, rect: Rect) -> Self {
        let left = rect.left.max(0) as usize;
        let top = rect.top.max(0) as usize;
        let right = (rect.right().max(0) as usize).min(self.width);
        let bottom = (rect.bottom().max(0) as usize).min(self.height);
        if left >= right || top >= bottom {
            return Self::default();
        }
        let pixels = (top..bottom)
            .flat_map(|y| self.pixels[y * self.width + left..y * self.width + right].iter())
            .copied()
            .collect();
        Self {
            width: right - left,
            height: bottom - top,
            pixels,
        }
    }
}

/// How a tracked text looked: its pixels, plus sample positions of its
/// strokes ("ink") and of the background between them.
#[derive(Clone, Default)]
struct Patch {
    image: GrayImage,
    ink: Vec<(usize, usize)>,
    background: Vec<(usize, usize)>,
    /// Ink brightness minus background brightness when the text was read.
    contrast: f32,
}

impl Patch {
    fn new(image: GrayImage) -> Self {
        let step = ((image.width * image.height / PATCH_SAMPLES) as f64)
            .sqrt()
            .max(1.0) as usize;
        let samples = (0..image.height)
            .step_by(step)
            .flat_map(|y| (0..image.width).step_by(step).map(move |x| (x, y)))
            .collect::<Vec<_>>();
        // Text covers a minority of its box, so the median is background.
        let mut values = samples
            .iter()
            .map(|&(x, y)| image.get(x, y))
            .collect::<Vec<_>>();
        values.sort_unstable();
        let background_level = values.get(values.len() / 2).copied().unwrap_or_default();
        let (ink, background): (Vec<_>, Vec<_>) = samples
            .into_iter()
            .partition(|&(x, y)| image.get(x, y).abs_diff(background_level) > INK_LEVEL);
        let mean = |points: &[(usize, usize)]| {
            points
                .iter()
                .map(|&(x, y)| f32::from(image.get(x, y)))
                .sum::<f32>()
                / points.len().max(1) as f32
        };
        let contrast = mean(&ink) - mean(&background);
        Self {
            image,
            ink,
            background,
            contrast,
        }
    }

    fn is_empty(&self) -> bool {
        self.image.is_empty()
    }
}

/// Similarity (0–1) of `patch` placed at (`left`, `top`) in `image`, judged
/// on the text's strokes rather than its whole box, so a changing background
/// behind the text (a blurred game world, flickering light) doesn't make it
/// look gone. The strokes must also still stand out from their background
/// as much as before; text that vanished or was replaced scores low.
fn patch_similarity(image: &GrayImage, patch: &Patch, left: i32, top: i32) -> f32 {
    if patch.ink.len() < MIN_INK_SAMPLES || patch.contrast.abs() < f32::from(INK_LEVEL) {
        return plain_similarity(image, &patch.image, left, top);
    }
    if left < 0
        || top < 0
        || left as usize + patch.image.width > image.width
        || top as usize + patch.image.height > image.height
    {
        return 0.0;
    }
    let (left, top) = (left as usize, top as usize);
    let now = |&(x, y): &(usize, usize)| f32::from(image.get(left + x, top + y));
    let stroke_difference = patch
        .ink
        .iter()
        .map(|point| (f32::from(patch.image.get(point.0, point.1)) - now(point)).abs())
        .sum::<f32>()
        / patch.ink.len() as f32;
    let shape = 1.0 - stroke_difference / patch.contrast.abs();
    let ink_now = patch.ink.iter().map(now).sum::<f32>() / patch.ink.len() as f32;
    let background_now = if patch.background.is_empty() {
        ink_now - patch.contrast
    } else {
        patch.background.iter().map(now).sum::<f32>() / patch.background.len() as f32
    };
    let contrast_kept = (ink_now - background_now) / patch.contrast;
    shape.min(contrast_kept * 1.25).clamp(0.0, 1.0)
}

/// Like [`patch_similarity`], ignoring the fifth of the strokes that changed
/// most: text with a mouse pointer or a small effect over part of it still
/// matches, text that changed style or disappeared does not.
fn partly_covered_similarity(image: &GrayImage, patch: &Patch, left: i32, top: i32) -> f32 {
    if patch.ink.len() < MIN_INK_SAMPLES || patch.contrast.abs() < f32::from(INK_LEVEL) {
        return plain_similarity(image, &patch.image, left, top);
    }
    if left < 0
        || top < 0
        || left as usize + patch.image.width > image.width
        || top as usize + patch.image.height > image.height
    {
        return 0.0;
    }
    let (left, top) = (left as usize, top as usize);
    let now = |&(x, y): &(usize, usize)| f32::from(image.get(left + x, top + y));
    let mut strokes = patch
        .ink
        .iter()
        .map(|point| {
            let value = now(point);
            (
                (f32::from(patch.image.get(point.0, point.1)) - value).abs(),
                value,
            )
        })
        .collect::<Vec<_>>();
    strokes.sort_by(|a, b| a.0.total_cmp(&b.0));
    strokes.truncate((strokes.len() * 4 / 5).max(1));
    let count = strokes.len() as f32;
    let shape = 1.0
        - strokes
            .iter()
            .map(|(difference, _)| difference)
            .sum::<f32>()
            / count
            / patch.contrast.abs();
    let ink_now = strokes.iter().map(|(_, value)| value).sum::<f32>() / count;
    let background_now = if patch.background.is_empty() {
        ink_now - patch.contrast
    } else {
        let mut values = patch.background.iter().map(now).collect::<Vec<_>>();
        values.sort_by(f32::total_cmp);
        values[values.len() / 2]
    };
    let contrast_kept = (ink_now - background_now) / patch.contrast;
    shape.min(contrast_kept * 1.25).clamp(0.0, 1.0)
}

/// Whether the text's strokes are still where they were, whatever their
/// colours now (0–1): the share of stroke samples that still stand out from
/// the background, and of background samples that don't, whichever is
/// lower, at the best of the positions within a pixel. Different text in the
/// same place, or the game world after a menu closes, scores low.
fn restyled_similarity(image: &GrayImage, patch: &Patch, left: i32, top: i32) -> f32 {
    if patch.ink.len() < MIN_INK_SAMPLES || patch.background.is_empty() {
        return 0.0;
    }
    let mut best = 0.0_f32;
    for (dx, dy) in (-1..=1).flat_map(|dy| (-1..=1).map(move |dx| (dx, dy))) {
        let (left, top) = (left + dx, top + dy);
        if left < 0
            || top < 0
            || left as usize + patch.image.width > image.width
            || top as usize + patch.image.height > image.height
        {
            continue;
        }
        let (left, top) = (left as usize, top as usize);
        let now = |&(x, y): &(usize, usize)| image.get(left + x, top + y);
        let mut background = patch.background.iter().map(now).collect::<Vec<_>>();
        background.sort_unstable();
        let level = background[background.len() / 2];
        let stands_out = |point: &(usize, usize)| now(point).abs_diff(level) > INK_LEVEL / 2;
        let share = |points: &[(usize, usize)], ink: bool| {
            points
                .iter()
                .filter(|point| stands_out(point) == ink)
                .count() as f32
                / points.len() as f32
        };
        best = best.max(share(&patch.ink, true).min(share(&patch.background, false)));
    }
    best
}

/// Similarity (0–1) of whole patches, for text without clear strokes. The
/// mean difference is scaled by the patch's own contrast, so faint and bold
/// text are judged alike.
fn plain_similarity(image: &GrayImage, patch: &GrayImage, left: i32, top: i32) -> f32 {
    if left < 0
        || top < 0
        || left as usize + patch.width > image.width
        || top as usize + patch.height > image.height
    {
        return 0.0;
    }
    let (left, top) = (left as usize, top as usize);
    let step = ((patch.width * patch.height / PATCH_SAMPLES) as f64)
        .sqrt()
        .max(1.0) as usize;
    let samples = || {
        (0..patch.height)
            .step_by(step)
            .flat_map(move |y| (0..patch.width).step_by(step).map(move |x| (x, y)))
    };
    let count = samples().count().max(1) as f32;
    let mean = samples()
        .map(|(x, y)| f32::from(patch.get(x, y)))
        .sum::<f32>()
        / count;
    let spread = samples()
        .map(|(x, y)| (f32::from(patch.get(x, y)) - mean).abs())
        .sum::<f32>()
        / count;
    let difference = samples()
        .map(|(x, y)| {
            (i16::from(patch.get(x, y)) - i16::from(image.get(left + x, top + y))).unsigned_abs()
                as f32
        })
        .sum::<f32>()
        / count;
    (1.0 - difference / (2.0 * spread.max(8.0))).clamp(0.0, 1.0)
}

/// Tiles whose content differs between two same-sized images.
fn changed_tiles(previous: &GrayImage, current: &GrayImage) -> Vec<Rect> {
    let mut tiles = Vec::new();
    for tile_top in (0..current.height).step_by(TILE) {
        for tile_left in (0..current.width).step_by(TILE) {
            let bottom = (tile_top + TILE).min(current.height);
            let right = (tile_left + TILE).min(current.width);
            let mut difference = 0_u32;
            let mut count = 0_u32;
            for y in (tile_top..bottom).step_by(2) {
                for x in (tile_left..right).step_by(2) {
                    difference += u32::from(previous.get(x, y).abs_diff(current.get(x, y)));
                    count += 1;
                }
            }
            if count > 0 && difference > TILE_CHANGE_LEVEL * count {
                tiles.push(Rect::new(
                    tile_left as i32,
                    tile_top as i32,
                    (right - tile_left) as u32,
                    (bottom - tile_top) as u32,
                ));
            }
        }
    }
    tiles
}

/// Whether a tile is nearly uniform, i.e. holds no text.
fn is_blank(image: &GrayImage, tile: Rect) -> bool {
    let region = image.crop(tile);
    if region.is_empty() {
        return true;
    }
    let count = region.pixels.len() as u32;
    let mean = region
        .pixels
        .iter()
        .map(|&pixel| u32::from(pixel))
        .sum::<u32>()
        / count;
    let spread = region
        .pixels
        .iter()
        .map(|&pixel| u32::from(pixel).abs_diff(mean))
        .sum::<u32>()
        / count;
    spread < BLANK_SPREAD
}

/// The offset most moved text shared, if any.
fn dominant_offset(offsets: &[(i32, i32)]) -> Option<(i32, i32)> {
    offsets
        .iter()
        .max_by_key(|offset| offsets.iter().filter(|other| other == offset).count())
        .copied()
}

/// Whether `tile` in `current` shows what `previous` showed at the position
/// `offset` earlier, i.e. the content only moved.
fn moved_with(previous: &GrayImage, current: &GrayImage, tile: Rect, offset: (i32, i32)) -> bool {
    let (source_left, source_top) = (tile.left - offset.0, tile.top - offset.1);
    if source_left < 0
        || source_top < 0
        || source_left as usize + tile.width as usize > previous.width
        || source_top as usize + tile.height as usize > previous.height
    {
        return false;
    }
    let mut difference = 0_u32;
    let mut count = 0_u32;
    for y in (0..tile.height as usize).step_by(2) {
        for x in (0..tile.width as usize).step_by(2) {
            let now = current.get(tile.left as usize + x, tile.top as usize + y);
            let before = previous.get(source_left as usize + x, source_top as usize + y);
            difference += u32::from(now.abs_diff(before));
            count += 1;
        }
    }
    count > 0 && difference <= TILE_CHANGE_LEVEL * count
}

fn tile_key(tile: Rect) -> (i32, i32) {
    (tile.left / TILE as i32, tile.top / TILE as i32)
}

/// Looks for anchored pack texts where the frame changed (or everywhere,
/// with `changed` unset), adding locked items for those present and removing
/// those gone. Returns the areas anchored text covers, whether anything was
/// added, moved or removed, and the anchored text that went away (unlocked,
/// so OCR pieces of it can still be rejoined while it is restyled).
fn check_anchors(
    anchors: &mut [Anchor],
    items: &mut Vec<TrackedText>,
    image: &GrayImage,
    changed: Option<&[Rect]>,
    threshold: f32,
) -> (Vec<Rect>, bool, Vec<TrackedText>) {
    let mut explained = Vec::new();
    let mut moved = false;
    let mut gone = Vec::new();
    select_anchors(anchors, (image.width, image.height));
    for (index, anchor) in anchors.iter_mut().enumerate() {
        let current = items
            .iter()
            .find(|item| item.anchor == Some(index))
            .map_or(anchor.area, |item| item.pixels);
        let touched = changed.is_none_or(|tiles| {
            tiles
                .iter()
                .any(|tile| overlaps(*tile, expand(current, ANCHOR_SEARCH)))
        });
        if !touched {
            if anchor.active {
                explained.push(current);
            }
            continue;
        }
        let found = anchor
            .usable
            .then(|| {
                let mut best = (0.0_f32, anchor.area);
                for dy in -ANCHOR_SEARCH..=ANCHOR_SEARCH {
                    for dx in -ANCHOR_SEARCH..=ANCHOR_SEARCH {
                        let rect = Rect::new(
                            anchor.area.left + dx,
                            anchor.area.top + dy,
                            anchor.area.width,
                            anchor.area.height,
                        );
                        let similarity =
                            patch_similarity(image, &anchor.patch, rect.left, rect.top);
                        if similarity > best.0 {
                            best = (similarity, rect);
                        }
                    }
                }
                best
            })
            .filter(|(similarity, _)| *similarity >= threshold)
            .map(|(_, rect)| rect);
        match found {
            Some(rect) => {
                explained.push(rect);
                match items.iter_mut().find(|item| item.anchor == Some(index)) {
                    Some(item) if item.pixels != rect => {
                        item.pixels = rect;
                        moved = true;
                    }
                    Some(_) => {}
                    None => {
                        // An anchor replaces any OCR reading of the same text.
                        items.retain(|item| item.anchor.is_some() || !overlaps(item.pixels, rect));
                        items.push(TrackedText {
                            block: TextBlock {
                                translated_text: Some(anchor.translation.text.clone()),
                                style: anchor.translation.style.clone(),
                                ..TextBlock::new(rect, anchor.source_text.clone(), 1.0)
                            },
                            pixels: rect,
                            coarse_patch: Patch::new(anchor.patch.image.downscale(COARSE)),
                            patch: anchor.patch.clone(),
                            anchor: Some(index),
                        });
                        moved = true;
                    }
                }
                anchor.active = true;
            }
            // Restyled where it is (a menu row highlighted under the
            // pointer): still there, redrawn over its new background.
            None if anchor.active && image_matches_restyled(image, items, index) => {
                explained.push(current);
                moved = true;
            }
            None => {
                if anchor.active {
                    let (lost, kept) = std::mem::take(items)
                        .into_iter()
                        .partition(|item| item.anchor == Some(index));
                    *items = kept;
                    gone.extend(lost.into_iter().map(|item: TrackedText| TrackedText {
                        anchor: None,
                        ..item
                    }));
                    moved = true;
                }
                anchor.active = false;
            }
        }
    }
    (explained, moved, gone)
}

/// Whether the item locked to anchor `index` still has its strokes in
/// place, in whatever colours.
fn image_matches_restyled(image: &GrayImage, items: &[TrackedText], index: usize) -> bool {
    items
        .iter()
        .find(|item| item.anchor == Some(index))
        .is_some_and(|item| {
            restyled_similarity(image, &item.patch, item.pixels.left, item.pixels.top)
                >= RESTYLED_KEEP_SIMILARITY
        })
}

/// Picks the anchors to look for in frames of `size`: those recorded at that
/// size, or for texts without one, the recording at the nearest size. Game
/// interfaces are usually laid out from a corner in fixed pixels, so text
/// recorded in a slightly different window is still found; where it isn't,
/// its pixels don't match and OCR reads it as before.
fn select_anchors(anchors: &mut [Anchor], size: (usize, usize)) {
    if anchors
        .first()
        .is_none_or(|anchor| anchor.usable_for == size)
    {
        return;
    }
    let distance = |anchor: &Anchor| {
        anchor.frame_size.0.abs_diff(size.0) + anchor.frame_size.1.abs_diff(size.1)
    };
    let mut nearest = HashMap::<&str, usize>::new();
    for anchor in anchors.iter() {
        let best = nearest.entry(&anchor.source_text).or_insert(usize::MAX);
        *best = (*best).min(distance(anchor));
    }
    let nearest = anchors
        .iter()
        .map(|anchor| nearest[anchor.source_text.as_str()] == distance(anchor))
        .collect::<Vec<_>>();
    for (anchor, usable) in anchors.iter_mut().zip(nearest) {
        anchor.usable = usable;
        anchor.usable_for = size;
    }
}

/// Keeps translated text that disappeared for instant recall.
fn remember(recall: &mut VecDeque<TrackedText>, items: impl IntoIterator<Item = TrackedText>) {
    for item in items {
        if item.anchor.is_none() && item.block.translated_text.is_some() && !item.patch.is_empty() {
            recall.push_back(item);
        }
    }
    let excess = recall.len().saturating_sub(RECALL_CAPACITY);
    recall.drain(..excess);
}

/// Bounding box of `tiles` plus a margin, so text at the edge of the change
/// is read whole. `None` (read everything) when it covers most of the frame.
///
/// The region is a band across the full width: text runs horizontally, so a
/// narrower crop would cut lines in half (e.g. when only a colour-cycling
/// word in a sentence changed) and OCR would read fragments. The band grows
/// to cover any known or just-lost text it touches, so paragraphs are read
/// whole.
fn detection_region(tiles: &[Rect], text: &[Rect], width: usize, height: usize) -> Option<Rect> {
    const MARGIN: i32 = 32;
    let mut top = (tiles.iter().map(|tile| tile.top).min()? - MARGIN).max(0);
    let mut bottom = (tiles.iter().map(|tile| tile.bottom()).max()? + MARGIN).min(height as i32);
    loop {
        let mut grown = false;
        for area in text {
            if area.top < bottom && area.bottom() > top {
                // Clamp before comparing: text at the frame's edge would
                // otherwise "grow" the band forever.
                let area_top = (area.top - 4).max(0);
                let area_bottom = (area.bottom() + 4).min(height as i32);
                if area_top < top {
                    top = area_top;
                    grown = true;
                }
                if area_bottom > bottom {
                    bottom = area_bottom;
                    grown = true;
                }
            }
        }
        if !grown {
            break;
        }
    }
    let band = i64::from(bottom - top) * width as i64;
    (band * 5 < (width * height) as i64 * 3)
        .then(|| Rect::new(0, top, width as u32, (bottom - top) as u32))
}

fn union(first: Rect, second: Rect) -> Rect {
    let left = first.left.min(second.left);
    let top = first.top.min(second.top);
    let right = first.right().max(second.right());
    let bottom = first.bottom().max(second.bottom());
    Rect::new(left, top, (right - left) as u32, (bottom - top) as u32)
}

/// The part of `text` inside `area`, relative to `area` (`None` if they
/// don't overlap).
fn text_within(area: Rect, text: Rect) -> Option<Rect> {
    let left = text.left.max(area.left);
    let top = text.top.max(area.top);
    let right = text.right().min(area.right());
    let bottom = text.bottom().min(area.bottom());
    (right > left && bottom > top).then(|| {
        Rect::new(
            left - area.left,
            top - area.top,
            (right - left) as u32,
            (bottom - top) as u32,
        )
    })
}

fn mostly_inside(inner: Rect, outer: Rect) -> bool {
    let width = (inner.right().min(outer.right()) - inner.left.max(outer.left)).max(0);
    let height = (inner.bottom().min(outer.bottom()) - inner.top.max(outer.top)).max(0);
    let area = i64::from(inner.width) * i64::from(inner.height);
    area > 0 && i64::from(width) * i64::from(height) * 2 > area
}

fn overlaps(first: Rect, second: Rect) -> bool {
    first.left < second.right()
        && second.left < first.right()
        && first.top < second.bottom()
        && second.top < first.bottom()
}

fn expand(rect: Rect, by: i32) -> Rect {
    Rect::new(
        rect.left - by,
        rect.top - by,
        rect.width + 2 * by as u32,
        rect.height + 2 * by as u32,
    )
}

/// Lowercases and collapses whitespace, for comparing recognised text.
pub fn normalize(text: &str) -> String {
    text.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

/// Character-level similarity (0–1) from Levenshtein distance.
pub fn text_similarity(first: &str, second: &str) -> f32 {
    let first = first.chars().collect::<Vec<_>>();
    let second = second.chars().collect::<Vec<_>>();
    let longest = first.len().max(second.len());
    if longest == 0 {
        return 1.0;
    }
    // Edit distance is at least the length difference, so skip pairs that
    // cannot reach any useful similarity.
    if (first.len().abs_diff(second.len()) as f32 / longest as f32) > 0.5 {
        return 0.0;
    }
    let mut previous = (0..=second.len()).collect::<Vec<_>>();
    let mut current = vec![0; second.len() + 1];
    for (i, first_char) in first.iter().enumerate() {
        current[0] = i + 1;
        for (j, second_char) in second.iter().enumerate() {
            let substitution = previous[j] + usize::from(first_char != second_char);
            current[j + 1] = substitution.min(previous[j + 1] + 1).min(current[j] + 1);
        }
        std::mem::swap(&mut previous, &mut current);
    }
    1.0 - previous[second.len()] as f32 / longest as f32
}

#[cfg(test)]
mod tests {
    use super::*;

    const WIDTH: u32 = 200;
    const HEIGHT: u32 = 240;

    /// A dark frame with bright "text" stamped at the given positions. Each
    /// stamp has its own pattern, like distinct words.
    fn frame(stamps: &[(i32, i32, u8)]) -> Frame {
        let mut rgba = vec![20_u8; (WIDTH * HEIGHT * 4) as usize];
        for &(left, top, seed) in stamps {
            for y in 0..12 {
                for x in 0..40 {
                    // Pseudo-random glyph pixels, unique per seed, like real text.
                    let hash = (x as u32).wrapping_mul(73_856_093)
                        ^ (y as u32).wrapping_mul(19_349_663)
                        ^ u32::from(seed).wrapping_mul(83_492_791);
                    let on = hash.wrapping_mul(2_654_435_761) >> 30 == 0;
                    let (px, py) = (left + x, top + y);
                    if on && px >= 0 && py >= 0 && (px as u32) < WIDTH && (py as u32) < HEIGHT {
                        let offset = ((py as u32 * WIDTH + px as u32) * 4) as usize;
                        rgba[offset..offset + 3].copy_from_slice(&[230, 230, 230]);
                    }
                }
            }
        }
        Frame::new(1, Rect::new(0, 0, WIDTH, HEIGHT), WIDTH, HEIGHT, rgba)
    }

    fn block(left: i32, top: i32, text: &str) -> TextBlock {
        TextBlock::new(Rect::new(left, top, 40, 12), text, 0.9)
    }

    fn tracking(stamps: &[(i32, i32, u8)], blocks: Vec<TextBlock>) -> Tracker {
        let mut tracker = Tracker::new(TrackerSettings::default());
        let first = frame(stamps);
        assert_eq!(tracker.update(&first), Update::Detect);
        tracker
            .reconcile(&first, blocks, |text| {
                Ok(Some(Translated::plain(format!("T:{text}"))))
            })
            .expect("translation succeeds");
        tracker
    }

    #[test]
    fn unchanged_frames_need_no_work() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        assert_eq!(tracker.update(&frame(&[(20, 20, 1)])), Update::Unchanged);
    }

    #[test]
    fn moved_text_is_followed_without_detection() {
        let mut tracker = tracking(
            &[(20, 20, 1), (20, 60, 2)],
            vec![block(20, 20, "Tasks"), block(20, 60, "Rewards")],
        );
        // Scroll everything down by 17 pixels.
        let scrolled = frame(&[(20, 37, 1), (20, 77, 2)]);
        assert_eq!(tracker.update(&scrolled), Update::Moved);
        let blocks = tracker.blocks(&scrolled);
        assert_eq!(blocks[0].bounds, Rect::new(20, 37, 40, 12));
        assert_eq!(blocks[1].bounds, Rect::new(20, 77, 40, 12));
        assert_eq!(blocks[0].translated_text.as_deref(), Some("T:Tasks"));
    }

    #[test]
    fn new_text_triggers_detection() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        assert_eq!(
            tracker.update(&frame(&[(20, 20, 1), (120, 80, 3)])),
            Update::Detect
        );
    }

    #[test]
    fn replaced_text_is_dropped_and_redetected() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        assert_eq!(tracker.update(&frame(&[(20, 20, 4)])), Update::Detect);
    }

    #[test]
    fn highlighted_text_keeps_its_translation() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        // Hovering lightens the row behind the text and brightens the text,
        // too much for the usual stroke match to recognise it.
        let mut highlighted = frame(&[(20, 20, 1)]);
        let width = highlighted.width as usize;
        for y in 16..36 {
            for x in 16..64 {
                let pixel = &mut highlighted.rgba[(y * width + x) * 4..][..3];
                let value = if pixel[0] > 100 { 255 } else { 170 };
                pixel.copy_from_slice(&[value, value, value]);
            }
        }
        tracker.update(&highlighted);
        let blocks = tracker.blocks(&highlighted);
        assert_eq!(blocks.len(), 1, "the text is still there");
        assert_eq!(blocks[0].translated_text.as_deref(), Some("T:Tasks"));
        // Other text in its place still replaces it.
        assert_eq!(tracker.update(&frame(&[(20, 20, 5)])), Update::Detect);
    }

    #[test]
    fn vanished_text_needs_no_detection() {
        let mut tracker = tracking(
            &[(20, 20, 1), (20, 60, 2)],
            vec![block(20, 20, "Tasks"), block(20, 60, "Rewards")],
        );
        assert_eq!(tracker.update(&frame(&[(20, 60, 2)])), Update::Moved);
        assert_eq!(tracker.blocks(&frame(&[])).len(), 1);
    }

    #[test]
    fn new_text_where_moved_text_was_is_detected() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        assert_eq!(
            tracker.update(&frame(&[(20, 50, 1), (20, 20, 5)])),
            Update::Detect
        );
    }

    #[test]
    fn animated_areas_stop_triggering_detection() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        // An animated icon (no text) changes at the bottom right.
        let frames = [
            frame(&[(20, 20, 1), (140, 90, 6)]),
            frame(&[(20, 20, 1), (140, 90, 7)]),
        ];
        assert_eq!(tracker.update(&frames[0]), Update::Detect);
        tracker
            .reconcile(&frames[0], vec![block(20, 20, "Tasks")], |_| {
                panic!("nothing new to translate")
            })
            .expect("reconcile");
        assert_eq!(tracker.update(&frames[1]), Update::Unchanged);
        assert_eq!(tracker.update(&frames[0]), Update::Unchanged);
    }

    #[test]
    fn text_over_an_animated_background_disappears_when_closed() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        // Something animates right next to the text, like the game world
        // behind a translucent quest panel.
        for seed in [6, 7, 8] {
            let animated = frame(&[(20, 20, 1), (62, 20, seed)]);
            if tracker.update(&animated) == Update::Detect {
                tracker
                    .reconcile_region(&animated, vec![block(20, 20, "Tasks")], |_| {
                        panic!("nothing new to translate")
                    })
                    .expect("reconcile");
            }
            assert!(
                tracker
                    .blocks(&animated)
                    .iter()
                    .any(|b| b.source_text == "Tasks"),
                "the text is still there"
            );
        }
        // The panel closes: the text is gone, the animation goes on.
        let closed = frame(&[(62, 20, 9)]);
        tracker.update(&closed);
        assert!(
            !tracker
                .blocks(&closed)
                .iter()
                .any(|b| b.source_text == "Tasks"),
            "the translation must not stay on screen"
        );
    }

    #[test]
    fn a_pointer_over_part_of_the_text_keeps_it_tracked() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        let mut hovered = frame(&[(20, 20, 1)]);
        // A mouse pointer over the end of the word.
        hovered.fill(Rect::new(52, 18, 8, 14), [0, 0, 0, 255]);
        assert_ne!(tracker.update(&hovered), Update::Detect);
        assert!(
            tracker
                .blocks(&hovered)
                .iter()
                .any(|block| block.translated_text.as_deref() == Some("T:Tasks"))
        );
    }

    #[test]
    fn restyled_text_in_an_animated_area_is_read_again_at_once() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        // The game world moves on both sides of the word.
        for seed in [6, 7, 8] {
            let animated = frame(&[(20, 20, 1), (-30, 20, seed), (62, 20, seed + 10)]);
            if tracker.update(&animated) == Update::Detect {
                tracker
                    .reconcile_region(&animated, vec![block(20, 20, "Tasks")], |_| {
                        panic!("nothing new to translate")
                    })
                    .expect("reconcile");
            }
        }
        // Hovering redraws the word in another style.
        let hovered = frame(&[(20, 20, 2), (-30, 20, 9), (62, 20, 19)]);
        assert_eq!(tracker.update(&hovered), Update::Detect);
    }

    #[test]
    fn a_menu_item_split_by_the_pointer_keeps_its_translation() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Generating Resources")]);
        // Hovered: the item is drawn differently and the pointer splits it.
        let hovered = frame(&[(20, 20, 2)]);
        assert_eq!(tracker.update(&hovered), Update::Detect);
        tracker
            .reconcile_region(
                &hovered,
                vec![
                    TextBlock::new(Rect::new(20, 20, 18, 12), "Generating", 0.9),
                    TextBlock::new(Rect::new(42, 20, 18, 12), "Resources", 0.9),
                ],
                |texts| panic!("the pieces must not be translated: {texts:?}"),
            )
            .expect("reconcile");
        let blocks = tracker.blocks(&hovered);
        assert_eq!(blocks.len(), 1, "{blocks:?}");
        assert_eq!(blocks[0].source_text, "Generating Resources");
        assert_eq!(
            blocks[0].translated_text.as_deref(),
            Some("T:Generating Resources")
        );
    }

    #[test]
    fn new_text_is_read_in_its_own_region_and_other_text_is_kept() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Tasks")]);
        let next = frame(&[(20, 20, 1), (140, 90, 3)]);
        assert_eq!(tracker.update(&next), Update::Detect);
        let region = tracker.detection_region().expect("a partial read");
        assert!(region.top <= 90 && region.bottom() >= 102, "{region:?}");
        assert_eq!((region.left, region.width), (0, WIDTH), "full-width band");
        assert!(
            region.top > 32,
            "the known text is outside the band: {region:?}"
        );
        tracker
            .reconcile_region(&next, vec![block(140, 90, "Rewards")], |texts| {
                Ok(texts
                    .iter()
                    .map(|text| Some(Translated::plain(format!("T:{text}"))))
                    .collect())
            })
            .expect("reconcile");
        let texts = tracker
            .blocks(&next)
            .into_iter()
            .map(|block| block.source_text)
            .collect::<Vec<_>>();
        assert_eq!(texts, ["Tasks", "Rewards"]);
    }

    #[test]
    fn text_is_kept_while_the_background_behind_it_changes() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Game Menu")]);
        // A blurred, moving game world behind the text: every background
        // pixel in the text's box changes, the white strokes don't.
        let mut moving = frame(&[(20, 20, 1)]);
        for y in 20..32 {
            for x in 20..60 {
                let offset = ((y * WIDTH + x) * 4) as usize;
                if moving.rgba[offset] != 230 {
                    let shade = 40 + ((x * 13 + y * 7) % 60) as u8;
                    moving.rgba[offset..offset + 3].copy_from_slice(&[shade, shade / 2, shade / 3]);
                }
            }
        }
        assert_ne!(tracker.update(&moving), Update::Detect);
        assert_eq!(
            tracker.blocks(&moving).len(),
            1,
            "the text is still tracked"
        );
    }

    #[test]
    fn regions_grow_to_cover_whole_paragraphs() {
        // A changed word in the middle of a known three-line paragraph.
        let paragraph = Rect::new(10, 100, 300, 60);
        let changed = [Rect::new(160, 128, 16, 16)];
        let region = detection_region(&changed, &[paragraph], 400, 1000).expect("band");
        assert!(region.top <= 100 && region.bottom() >= 160, "{region:?}");
    }

    fn anchor_for(stamp: (i32, i32, u8), text: &str, translation: &str) -> AnchorTarget {
        let source = frame(&[stamp]);
        let (left, top, _) = stamp;
        let pixels = (top..top + 12)
            .flat_map(|y| (left..left + 40).map(move |x| (x, y)))
            .map(|(x, y)| {
                let offset = ((y as u32 * WIDTH + x as u32) * 4) as usize;
                source.rgba[offset..offset + 3]
                    .iter()
                    .copied()
                    .max()
                    .unwrap_or(0)
            })
            .collect();
        AnchorTarget {
            frame_width: WIDTH,
            frame_height: HEIGHT,
            area: Rect::new(left, top, 40, 12),
            width: 40,
            height: 12,
            pixels,
            source_text: text.into(),
            translation: Translated::plain(translation),
        }
    }

    #[test]
    fn detection_band_stops_at_the_frame_edges() {
        // Text touching the top and bottom edges used to grow the band
        // forever.
        let changed = [Rect::new(10, 400, 16, 16)];
        let text = [Rect::new(0, 2, 100, 20), Rect::new(0, 380, 100, 18)];
        let region = detection_region(&changed, &text, 2000, 400).expect("band");
        assert_eq!(region.bottom(), 400);
        let region = detection_region(&[Rect::new(10, 0, 16, 16)], &text, 2000, 400);
        assert_eq!(region.map(|band| band.top), Some(0));
    }

    #[test]
    fn anchored_text_is_locked_to_the_creators_translation() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        tracker.set_anchors(vec![anchor_for((20, 20, 1), "Game Menu", "เมนูเกม")]);
        let menu = frame(&[(20, 20, 1)]);
        assert_eq!(tracker.update(&menu), Update::Detect);
        // OCR misreads the anchored text; the anchor wins.
        tracker
            .reconcile(&menu, vec![block(20, 20, "Eaméflenu")], |_| {
                panic!("anchored text needs no translation lookup")
            })
            .expect("reconcile");
        let blocks = tracker.blocks(&menu);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].translated_text.as_deref(), Some("เมนูเกม"));
        assert_eq!(tracker.update(&menu), Update::Unchanged);

        // The menu closes: the locked translation goes with it.
        assert_eq!(tracker.update(&frame(&[])), Update::Moved);
        assert!(tracker.blocks(&frame(&[])).is_empty());
        // It reopens: recognised again from its pixels, without OCR.
        assert_eq!(tracker.update(&menu), Update::Moved);
        assert_eq!(
            tracker.blocks(&menu)[0].translated_text.as_deref(),
            Some("เมนูเกม")
        );
    }

    #[test]
    fn text_restyled_while_being_read_stays_translated() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        // Read while highlighted under the pointer...
        let hovered = frame(&[(20, 20, 1)]);
        assert_eq!(tracker.update(&hovered), Update::Detect);
        let detection = tracker.take_detection();
        // ...and the pointer leaves before the read finishes: same strokes,
        // dimmer.
        let mut normal = hovered.clone();
        for pixel in normal.rgba.as_chunks_mut::<4>().0 {
            if pixel[0] == 230 {
                pixel[..3].copy_from_slice(&[120, 160, 60]);
            }
        }
        tracker.update(&normal);
        tracker
            .reconcile_detection(&hovered, vec![block(20, 20, "Menu")], detection, |texts| {
                Ok(texts
                    .iter()
                    .map(|text| Some(Translated::plain(format!("T:{text}"))))
                    .collect())
            })
            .expect("reconcile");
        let blocks = tracker.blocks(&normal);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].translated_text.as_deref(), Some("T:Menu"));
    }

    #[test]
    fn text_gone_while_being_read_is_read_again() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        let first = frame(&[(20, 20, 1)]);
        tracker.update(&first);
        let detection = tracker.take_detection();
        // Other text replaces it during the read, then the screen stays still.
        let replaced = frame(&[(20, 20, 2)]);
        tracker.update(&replaced);
        tracker
            .reconcile_detection(&first, vec![block(20, 20, "Menu")], detection, |texts| {
                Ok(vec![None; texts.len()])
            })
            .expect("reconcile");
        assert!(tracker.blocks(&replaced).is_empty());
        assert_eq!(tracker.update(&replaced), Update::Detect);
    }

    #[test]
    fn anchors_recorded_in_another_window_size_are_still_found() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        let mut other_size = anchor_for((20, 20, 1), "Game Menu", "เมนูเกม");
        other_size.frame_width += 300;
        tracker.set_anchors(vec![other_size]);
        let menu = frame(&[(20, 20, 1)]);
        tracker.update(&menu);
        tracker
            .reconcile(&menu, vec![], |_| panic!("nothing to translate"))
            .expect("reconcile");
        let blocks = tracker.blocks(&menu);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].translated_text.as_deref(), Some("เมนูเกม"));
    }

    #[test]
    fn anchored_text_is_drawn_where_ocr_finds_it() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        tracker.set_anchors(vec![anchor_for((20, 20, 1), "Game Menu", "เมนูเกม")]);
        let menu = frame(&[(20, 20, 1)]);
        tracker.update(&menu);
        // The recording includes an icon left of the text; OCR boxes just
        // the text.
        let text = Rect::new(30, 22, 28, 8);
        tracker
            .reconcile(&menu, vec![TextBlock::new(text, "Game Menu", 0.9)], |_| {
                panic!("nothing to translate")
            })
            .expect("reconcile");
        let blocks = tracker.blocks(&menu);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].bounds, text);
        assert_eq!(blocks[0].translated_text.as_deref(), Some("เมนูเกม"));
    }

    #[test]
    fn highlighted_anchored_text_keeps_its_translation() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        tracker.set_anchors(vec![anchor_for((20, 20, 1), "Game Menu", "เมนูเกม")]);
        let menu = frame(&[(20, 20, 1)]);
        tracker.update(&menu);
        tracker
            .reconcile(&menu, vec![], |_| panic!("nothing to translate"))
            .expect("reconcile");
        // The pointer highlights the row: same strokes, other colours.
        let mut hovered = menu.clone();
        for pixel in hovered.rgba.as_chunks_mut::<4>().0 {
            let colour = if pixel[0] == 230 { 150 } else { 90 };
            pixel[..3].copy_from_slice(&[colour; 3]);
        }
        assert_eq!(tracker.update(&hovered), Update::Moved);
        let blocks = tracker.blocks(&hovered);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].translated_text.as_deref(), Some("เมนูเกม"));
    }

    #[test]
    fn restyled_anchored_text_read_in_part_keeps_its_translation() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        tracker.set_anchors(vec![anchor_for((20, 20, 1), "Game Menu", "เมนูเกม")]);
        let menu = frame(&[(20, 20, 1)]);
        tracker.update(&menu);
        tracker
            .reconcile(&menu, vec![], |_| panic!("nothing to translate"))
            .expect("reconcile");
        // Hovering redraws the row in another font, and the pointer hides
        // part of it, so OCR reads only "Game".
        let hovered = frame(&[(20, 20, 7)]);
        assert_eq!(tracker.update(&hovered), Update::Detect);
        let mut asked = Vec::new();
        tracker
            .reconcile_region(&hovered, vec![block(22, 21, "Game")], |texts| {
                asked.extend_from_slice(texts);
                Ok(texts
                    .iter()
                    .map(|text| (text == "Game Menu").then(|| Translated::plain("เมนูเกม")))
                    .collect())
            })
            .expect("reconcile");
        assert_eq!(asked, ["Game Menu"]);
        let blocks = tracker.blocks(&hovered);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].translated_text.as_deref(), Some("เมนูเกม"));
    }

    #[test]
    fn text_does_not_jump_onto_similar_looking_text_elsewhere() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "Title")]);
        // The title disappears and different text in the same font appears
        // below it, on a different background.
        let mut next = frame(&[(20, 70, 2)]);
        next.fill(Rect::new(0, 60, WIDTH, 40), [70, 70, 90, 255]);
        let mut paragraph = frame(&[(20, 70, 2)]);
        for y in 60..100 {
            for x in 0..WIDTH {
                let offset = ((y * WIDTH + x) * 4) as usize;
                if paragraph.rgba[offset] != 230 {
                    paragraph.rgba[offset..offset + 3].copy_from_slice(&[70, 70, 90]);
                }
            }
        }
        tracker.update(&paragraph);
        assert!(
            tracker
                .blocks(&paragraph)
                .iter()
                .all(|block| block.bounds.top != 70),
            "the title must not be relocated onto the other text"
        );
    }

    #[test]
    fn reappearing_text_is_restored_without_reading_it_again() {
        let mut tracker = tracking(
            &[(20, 20, 1), (20, 60, 2)],
            vec![block(20, 20, "Tasks"), block(20, 60, "Rewards")],
        );
        // The menu closes, then opens again.
        assert_eq!(tracker.update(&frame(&[(20, 20, 1)])), Update::Moved);
        assert_eq!(tracker.blocks(&frame(&[])).len(), 1);
        let reopened = frame(&[(20, 20, 1), (20, 60, 2)]);
        assert_eq!(tracker.update(&reopened), Update::Moved);
        let blocks = tracker.blocks(&reopened);
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[1].translated_text.as_deref(), Some("T:Rewards"));
    }

    #[test]
    fn similar_text_reuses_translation_without_translating() {
        let mut tracker = tracking(&[(20, 20, 1)], vec![block(20, 20, "your enchanting rolls")]);
        tracker.request_detection();
        let next = frame(&[(20, 20, 1)]);
        assert_eq!(tracker.update(&next), Update::Detect);
        tracker
            .reconcile(&next, vec![block(20, 20, "gour enchanting rolls")], |_| {
                panic!("known text must not be translated again")
            })
            .expect("reuses translation");
        assert_eq!(
            tracker.blocks(&next)[0].translated_text.as_deref(),
            Some("T:your enchanting rolls")
        );
    }

    #[test]
    fn failed_reconcile_retries_detection() {
        let mut tracker = Tracker::new(TrackerSettings::default());
        let first = frame(&[(20, 20, 1)]);
        assert_eq!(tracker.update(&first), Update::Detect);
        assert!(
            tracker
                .reconcile(&first, vec![block(20, 20, "Tasks")], |_| Err(
                    "offline".into()
                ))
                .is_err()
        );
        assert_eq!(tracker.update(&first), Update::Detect);
    }

    #[test]
    fn text_similarity_tolerates_ocr_noise_but_not_different_words() {
        assert!(text_similarity("gour enchanting rolls", "your enchanting rolls") >= 0.85);
        assert!(text_similarity("tasks", "masks") < 0.85);
        assert_eq!(text_similarity("same", "same"), 1.0);
    }
}
