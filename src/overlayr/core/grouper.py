"""Turns OCR lines into the blocks that get translated, one rule set per scene.

CLAUDE.md section 1 ranks grouping above character accuracy: a misread letter
can still be guessed from context, but a paragraph merged across a column, or
two speech bubbles fused into one, is translated as a sentence nobody wrote.

Section 7 gives each scene its own rule, and one rule they all share: a region
from the RegionProvider is a **veto**. Two lines in different regions are never
merged, however close together they look. Distance alone cannot tell the gutter
between two columns from the space between two words.

The heuristics here were moved out of ``AsyncEngine._merge_text_blocks`` rather
than rewritten - they already worked - and the veto and reading order were
added around them.

Elements are the dicts the engine passes around: ``bbox`` as [left, top, right,
bottom], ``original`` text, and ``bg_color``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .scene import SceneType

Element = Dict[str, Any]

# Two lines belong to the same paragraph only if their glyphs are about the same
# size; a jump in height means a heading, a speaker name, or a different panel.
FONT_HEIGHT_TOLERANCE = 1.25

# Right-to-left is the reading order of Japanese comics. It follows the source
# language rather than a setting of its own: the user has already said what
# language they are reading, and section 1 asks for one less thing to configure.
RTL_LANGUAGES = {"ja", "zh", "zh-CN", "zh-TW"}


def _bbox_values(element: Element):
    left, top, right, bottom = element["bbox"]
    return left, top, right - left, bottom - top


def _height(element: Element) -> float:
    return max(1.0, element["bbox"][3] - element["bbox"][1])


def merge_cluster(cluster: Sequence[Element]) -> Element:
    """Fuse a group of lines into the single block that gets translated."""
    ordered = sorted(cluster, key=lambda item: item["bbox"][1])

    return {
        "bbox": [
            min(item["bbox"][0] for item in ordered),
            min(item["bbox"][1] for item in ordered),
            max(item["bbox"][2] for item in ordered),
            max(item["bbox"][3] for item in ordered),
        ],
        "original": " ".join(item["original"] for item in ordered),
        # Colours and line height come from the topmost line rather than an
        # average: the block is painted as one patch, and a median across lines
        # of different sizes would fit none of them.
        "bg_color": ordered[0].get("bg_color", "#010101"),
        "text_color": ordered[0].get("text_color", "#ffffff"),
        "line_height": _median_line_height(ordered),
    }


def _median_line_height(ordered: Sequence[Element]) -> float:
    """Typical line height in a cluster, used to size the translation.

    The median ignores the one oversized line a stray box adds, which a mean
    would let drag the whole block's text out of its box.
    """
    heights = sorted(item.get("line_height", item["bbox"][3] - item["bbox"][1])
                     for item in ordered)
    if not heights:
        return 0.0
    return float(heights[len(heights) // 2])


def filter_nested(elements: List[Element]) -> List[Element]:
    """Drop boxes that sit inside a larger box, keeping any text they add.

    The OCR pool often returns both a whole block and the lines inside it. The
    inner ones are redundant, but only usually - so text the outer box does not
    already contain is appended rather than thrown away.
    """
    if len(elements) <= 1:
        return elements

    by_area = sorted(elements, reverse=True,
                     key=lambda e: (e["bbox"][2] - e["bbox"][0]) * (e["bbox"][3] - e["bbox"][1]))
    kept: List[Element] = []

    for item in by_area:
        ix, iy, iw, ih = _bbox_values(item)
        item_area = iw * ih
        nested = False

        for keeper in kept:
            kx, ky, kw, kh = _bbox_values(keeper)
            overlap_w = max(0, min(ix + iw, kx + kw) - max(ix, kx))
            overlap_h = max(0, min(iy + ih, ky + kh) - max(iy, ky))

            if item_area <= 0 or (overlap_w * overlap_h) / item_area <= 0.50:
                continue

            nested = True
            small_text = item["original"].strip()
            big_text = keeper["original"]
            if small_text and small_text not in big_text:
                small_words = set(small_text.split())
                common = small_words & set(big_text.split())
                if not small_words or len(common) / len(small_words) <= 0.8:
                    keeper["original"] += " " + small_text
            break

        if not nested:
            kept.append(item)

    return kept


class Grouper:
    """Base class: the region veto, the reading order, and the shared helpers."""

    # How far outside a region, in line heights, a line may sit and still be
    # counted as belonging to it. Zero switches the snapping off.
    SNAP_MARGIN = 0.75

    def __init__(self, regions: Optional[Sequence] = None, source_lang: str = "en"):
        self.regions = list(regions or [])
        self.source_lang = source_lang
        # Which region each element sits in, keyed by identity. Testing a point
        # against a polygon is not free, and the clustering loops ask the same
        # question about the same element many times over.
        self._region_cache: Dict[int, int] = {}

    # -- region veto ------------------------------------------------------

    def region_of(self, element: Element) -> int:
        """Index of the region holding this element, or -1 when none does.

        The centre of the box decides, so a line whose box spills slightly past
        a bubble outline still counts as being inside it.
        """
        if not self.regions:
            return -1

        key = id(element)
        cached = self._region_cache.get(key)
        if cached is not None:
            return cached

        left, top, right, bottom = element["bbox"]
        cx, cy = (left + right) / 2.0, (top + bottom) / 2.0

        best_index, best_area = -1, None
        for index, region in enumerate(self.regions):
            if not region.contains_point(cx, cy):
                continue
            # The tightest region that holds the point wins, so a line inside a
            # bubble is not claimed by the panel the bubble sits in.
            if best_area is None or region.area < best_area:
                best_index, best_area = index, region.area

        if best_index == -1:
            best_index = self._nearest_region(cx, cy, _height(element))

        self._region_cache[key] = best_index
        return best_index

    def _nearest_region(self, cx: float, cy: float, height: float) -> int:
        """Region just outside of which this point sits, if there is one.

        Detected outlines hug the text closely, so a line can end up a few
        pixels outside the bubble that plainly contains it. Without this the
        line counts as belonging to no region at all, and then nothing stops it
        from being merged with the bubble next door - which is the exact mistake
        the veto exists to prevent.
        """
        if self.SNAP_MARGIN <= 0:
            return -1

        margin = max(8.0, height * self.SNAP_MARGIN)
        best_index, best_distance = -1, None
        for index, region in enumerate(self.regions):
            dx = max(region.left - cx, 0, cx - region.right)
            dy = max(region.top - cy, 0, cy - region.bottom)
            distance = max(dx, dy)
            if distance > margin:
                continue
            if best_distance is None or distance < best_distance:
                best_index, best_distance = index, distance
        return best_index

    def may_merge(self, a: Element, b: Element) -> bool:
        """Whether the region veto allows these two lines to be merged."""
        if not self.regions:
            return True
        return self.region_of(a) == self.region_of(b)

    # -- reading order ----------------------------------------------------

    @property
    def right_to_left(self) -> bool:
        return self.source_lang in RTL_LANGUAGES

    def sort_blocks(self, blocks: List[Element]) -> List[Element]:
        """Order blocks the way a reader would go through them.

        Rows first, then across each row - and across it from the right when
        the source language reads that way.
        """
        if not blocks:
            return blocks

        row_height = sorted(_height(block) for block in blocks)[len(blocks) // 2]
        row_threshold = max(20.0, row_height * 1.5)

        remaining = sorted(blocks, key=lambda b: b["bbox"][1])
        ordered: List[Element] = []
        while remaining:
            first = remaining.pop(0)
            row = [first]
            rest = []
            for block in remaining:
                if abs(block["bbox"][1] - first["bbox"][1]) < row_threshold:
                    row.append(block)
                else:
                    rest.append(block)
            row.sort(key=lambda b: b["bbox"][0], reverse=self.right_to_left)
            ordered.extend(row)
            remaining = rest
        return ordered

    # -- entry point ------------------------------------------------------

    def group(self, elements: List[Element]) -> List[Element]:
        if not elements:
            return []
        clusters = self.build_clusters(filter_nested(list(elements)))
        return self.sort_blocks([merge_cluster(cluster) for cluster in clusters])

    def build_clusters(self, elements: List[Element]) -> List[List[Element]]:
        raise NotImplementedError


class ComicGrouper(Grouper):
    """One bubble, one block - section 7 allows no exception.

    Lines the bubble detector did not claim fall back to proximity, so a caption
    outside any bubble is still read as one piece instead of scattering.
    """

    # How far apart, in line heights, two unclaimed lines may sit and still be
    # joined, and how much of the narrower one must overlap the other. Both are
    # tighter than the paragraph defaults: comic text is set in short centred
    # lines, and a generous rule reaches straight into the next bubble.
    LOOSE_VERTICAL_GAP = 1.0
    LOOSE_MIN_OVERLAP = 0.5

    def build_clusters(self, elements):
        in_regions: Dict[int, List[Element]] = {}
        loose: List[Element] = []

        for element in elements:
            index = self.region_of(element)
            if index == -1:
                loose.append(element)
            else:
                in_regions.setdefault(index, []).append(element)

        clusters = [lines for _, lines in sorted(in_regions.items())]
        clusters.extend(_cluster_by_proximity(loose,
                                              vertical_gap=self.LOOSE_VERTICAL_GAP,
                                              min_overlap=self.LOOSE_MIN_OVERLAP))
        return clusters


class GameUiGrouper(Grouper):
    """Lines join only inside one panel, at one size, with one line between them.

    A HUD counter and a line of dialogue can sit close together on screen and
    have nothing to do with each other, so the panel is what holds a block
    together here, not distance.
    """

    def build_clusters(self, elements):
        clusters: List[List[Element]] = []

        for element in sorted(elements, key=lambda e: e["bbox"][1]):
            left, top, width, height = _bbox_values(element)
            for cluster in clusters:
                if not self.may_merge(cluster[-1], element):
                    continue

                cluster_bottom = max(item["bbox"][3] for item in cluster)
                cluster_left = min(item["bbox"][0] for item in cluster)
                cluster_right = max(item["bbox"][2] for item in cluster)

                if max(0, min(left + width, cluster_right) - max(left, cluster_left)) <= 0:
                    continue

                cluster_height = max(_height(item) for item in cluster)
                ratio = max(cluster_height, height) / min(cluster_height, height)
                if ratio > FONT_HEIGHT_TOLERANCE:
                    continue

                if max(0, top - cluster_bottom) < height * 1.2:
                    cluster.append(element)
                    break
            else:
                clusters.append([element])

        return clusters


class DocumentGrouper(Grouper):
    """Fragments join into lines, lines into paragraphs, paragraphs stay in one column.

    The column bands are the veto here: without them a two-column page merges
    across the gutter and both paragraphs come out as nonsense.
    """

    def build_clusters(self, elements):
        lines = self._form_lines(elements)
        return self._form_paragraphs(lines)

    def _form_lines(self, elements):
        """Join fragments that share a baseline into one line."""
        ordered = sorted(elements, key=lambda e: e["bbox"][1])
        lines: List[List[Element]] = []

        for element in ordered:
            centre_y = (element["bbox"][1] + element["bbox"][3]) / 2.0
            height = _height(element)
            for line in lines:
                if not self.may_merge(line[-1], element):
                    continue
                member = line[-1]
                member_centre = (member["bbox"][1] + member["bbox"][3]) / 2.0
                if abs(member_centre - centre_y) > height * 0.6:
                    continue

                gap = min(abs(member["bbox"][2] - element["bbox"][0]),
                          abs(element["bbox"][2] - member["bbox"][0]))
                overlap = max(0, min(member["bbox"][2], element["bbox"][2])
                              - max(member["bbox"][0], element["bbox"][0]))
                if overlap > 0 or gap < height * 4:
                    line.append(element)
                    break
            else:
                lines.append([element])

        merged = []
        for line in lines:
            line.sort(key=lambda e: e["bbox"][0])
            merged.append(merge_cluster(line))
        return merged

    def _form_paragraphs(self, lines):
        if not lines:
            return []

        lines.sort(key=lambda e: e["bbox"][1])
        paragraphs = [[lines[0]]]

        for current in lines[1:]:
            previous = paragraphs[-1][-1]

            if not self.may_merge(previous, current):
                paragraphs.append([current])
                continue

            gap = current["bbox"][1] - previous["bbox"][3]
            previous_height = _height(previous)
            current_height = _height(current)
            average_height = (previous_height + current_height) / 2.0
            ratio = max(previous_height, current_height) / min(previous_height, current_height)

            overlap_x = max(0, min(previous["bbox"][2], current["bbox"][2])
                            - max(previous["bbox"][0], current["bbox"][0]))
            horizontal_gap = 0 if overlap_x > 0 else max(
                0, current["bbox"][0] - previous["bbox"][2],
                previous["bbox"][0] - current["bbox"][2])

            same_column = overlap_x > 0 or horizontal_gap < 50
            close_enough = gap < average_height * 1.5
            # A different size means a heading, which never belongs to the
            # paragraph beneath it.
            same_size = ratio < 1.3

            if same_column and close_enough and same_size:
                paragraphs[-1].append(current)
            else:
                paragraphs.append([current])

        return paragraphs


def _cluster_by_proximity(elements, vertical_gap: float = 2.0,
                          min_overlap: float = 0.0):
    """Group lines that sit directly above one another - the old paragraph rule.

    ``min_overlap`` is the share of the narrower box that must overlap
    horizontally. Any overlap at all is enough for a paragraph, but not for
    comic text: two bubbles stacked in a panel share a sliver of width, and
    merging them puts one character`s words in another character`s mouth.
    """
    clusters: List[List[Element]] = []
    for element in sorted(elements, key=lambda e: e["bbox"][1]):
        left, top, width, height = _bbox_values(element)
        for cluster in clusters:
            cluster_left = min(item["bbox"][0] for item in cluster)
            cluster_right = max(item["bbox"][2] for item in cluster)
            cluster_bottom = max(item["bbox"][3] for item in cluster)

            overlap = max(0, min(left + width, cluster_right) - max(left, cluster_left))
            narrower = max(1, min(width, cluster_right - cluster_left))
            if overlap <= 0 or overlap / narrower < min_overlap:
                continue
            if max(0, top - cluster_bottom) < height * vertical_gap:
                cluster.append(element)
                break
        else:
            clusters.append([element])
    return clusters


class LegacyGrouper(Grouper):
    """What the pipeline did before scenes existed, kept for an unknown source."""

    def __init__(self, regions=None, source_lang="en", mode="paragraph"):
        super().__init__(regions, source_lang)
        self.mode = mode

    def build_clusters(self, elements):
        if self.mode == "line":
            return DocumentGrouper(self.regions, self.source_lang).build_clusters(elements)
        return _cluster_by_proximity(elements)


GROUPERS = {
    SceneType.COMIC: ComicGrouper,
    SceneType.GAME_UI: GameUiGrouper,
    SceneType.DOCUMENT: DocumentGrouper,
}


def get_grouper(scene, regions=None, source_lang: str = "en",
                merge_mode: str = "paragraph") -> Grouper:
    """Grouper for a scene; an unknown scene keeps the old merge-mode behaviour."""
    grouper_class = GROUPERS.get(scene)
    if grouper_class is None:
        return LegacyGrouper(regions, source_lang, merge_mode)
    return grouper_class(regions, source_lang)
