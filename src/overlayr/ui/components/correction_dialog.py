"""Small dialog for teaching a game profile a preferred translation."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

import customtkinter as ctk  # type: ignore

from ..theme import get_theme


def feedback_items_from_results(
    results: Iterable[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    """Return unique, editable source/translation pairs in display order."""
    items: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        source = str(result.get("original") or "").strip()
        translated = str(result.get("translated") or "").strip()
        pair = (source, translated)
        if not source or not translated or pair in seen:
            continue
        seen.add(pair)
        items.append(pair)
    return items


class CorrectionDialog(ctk.CTkToplevel):
    """Let a player store a preferred translation for the active game."""

    def __init__(
        self,
        master,
        *,
        profile_name: str,
        items: list[tuple[str, str]],
        on_save: Callable[[str, str], None],
    ) -> None:
        super().__init__(master)
        if not items:
            raise ValueError("At least one translation is required")

        self._items = dict(items)
        self._on_save_callback = on_save
        colors = get_theme()

        self.title(f"Teach Overlayr — {profile_name}")
        self.geometry("540x360")
        self.resizable(False, False)
        self.configure(fg_color=colors["bg_primary"])
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(
            self,
            text=f"Preferred translation for {profile_name}",
            font=("Segoe UI", 18, "bold"),
            text_color=colors["text_primary"],
        ).pack(anchor="w", padx=24, pady=(24, 6))
        ctk.CTkLabel(
            self,
            text=(
                "This correction is saved only in this game's local profile "
                "and takes effect immediately."
            ),
            wraplength=490,
            justify="left",
            text_color=colors["text_secondary"],
        ).pack(anchor="w", padx=24, pady=(0, 16))

        self.source_var = ctk.StringVar(value=items[0][0])
        self.source_menu = ctk.CTkOptionMenu(
            self,
            values=list(self._items),
            variable=self.source_var,
            command=self._select_source,
            width=492,
        )
        self.source_menu.pack(fill="x", padx=24, pady=(0, 12))

        self.translation_box = ctk.CTkTextbox(
            self,
            height=120,
            wrap="word",
            fg_color=colors["bg_card"],
            text_color=colors["text_primary"],
        )
        self.translation_box.pack(fill="x", padx=24, pady=(0, 16))
        self.translation_box.insert("1.0", items[0][1])

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(0, 20))
        ctk.CTkButton(
            actions,
            text="Cancel",
            command=self.destroy,
            fg_color=colors["secondary_accent"],
            text_color=colors["text_primary"],
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            actions,
            text="Save Correction",
            command=self._save,
            fg_color=colors["accent"],
        ).pack(side="right")

        self.after(50, self.translation_box.focus_set)

    def _select_source(self, source: str) -> None:
        self.translation_box.delete("1.0", "end")
        self.translation_box.insert("1.0", self._items[source])

    def _save(self) -> None:
        source = self.source_var.get()
        translation = self.translation_box.get("1.0", "end").strip()
        if not source or not translation:
            return
        self._on_save_callback(source, translation)
        self.destroy()


__all__ = ["CorrectionDialog", "feedback_items_from_results"]
