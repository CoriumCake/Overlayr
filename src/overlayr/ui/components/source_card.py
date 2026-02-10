import customtkinter as ctk # type: ignore
from typing import Callable, Any
from ..theme import get_theme

class SourceCard(ctk.CTkButton):
    def _truncate(self, text: str, length: int) -> str:
        if len(text) > length:
            return text[:length-3] + "..."
        return text

    def __init__(self, master, source_data: dict, on_click: Callable[[dict], None], **kwargs):
        self.source_data = source_data
        self.on_click_callback = on_click
        
        # Get theme colors
        self.colors = get_theme()
        
        # Determine icon/text
        is_screen = source_data.get('type') == 'screen'
        raw_title = source_data.get('name') if is_screen else source_data.get('title')
        title = self._truncate(raw_title, 18)
        
        details = self._truncate(source_data.get('details', 'Window'), 20)
        
        text = title
        
        super().__init__(
            master, 
            text=text,
            image=source_data.get('thumbnail'),
            compound="top",
            command=self._on_click, 
            height=150, 
            width=200,
            corner_radius=10,
            fg_color=self.colors["bg_card"],
            hover_color=self.colors["bg_card_hover"],
            border_color=self.colors["border"],
            text_color=self.colors["text_primary"],
            border_width=1,  # Fix #6: always visible border
            **kwargs
        )
        
    def _on_click(self):
        if self.on_click_callback:
            self.on_click_callback(self.source_data)

    def refresh_colors(self, new_colors: dict):
        """Update colors for theme switch"""
        self.colors = new_colors
        self.configure(
            text_color=self.colors["text_primary"],
            hover_color=self.colors["bg_card_hover"]
        )
        # Re-apply selection state to update bg/border
        # We need to know if we are selected. Ideally passed in or stored.
        # But set_selected uses self.colors, so we can just re-call it if we track state.
        # For now, let main_window handle selection state update since it knows.
        pass
    
    def set_selected(self, selected: bool):
        if selected:
            # Fix #3: subtle tint + accent border instead of solid fill
            self.configure(
                border_width=2,
                border_color=self.colors["accent"],
                fg_color=self.colors["bg_card_selected"]
            )
        else:
            self.configure(
                border_width=1,
                border_color=self.colors["border"],
                fg_color=self.colors["bg_card"]
            )
