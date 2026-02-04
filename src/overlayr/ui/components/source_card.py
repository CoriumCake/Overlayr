import customtkinter as ctk # type: ignore
from typing import Callable, Any

class SourceCard(ctk.CTkButton):
    def __init__(self, master, source_data: dict, on_click: Callable[[dict], None], **kwargs):
        self.source_data = source_data
        self.on_click_callback = on_click
        
        # Determine icon/text
        is_screen = source_data.get('type') == 'screen'
    def _truncate(self, text: str, length: int) -> str:
        if len(text) > length:
            return text[:length-3] + "..."
        return text

    def __init__(self, master, source_data: dict, on_click: Callable[[dict], None], **kwargs):
        self.source_data = source_data
        self.on_click_callback = on_click
        
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
            fg_color="#2B2B2B",
            hover_color="#3A3A3A",
            border_color="#1F6AA5",  # Highlight color
            border_width=0, # Default no border
            **kwargs
        )
        
    def _on_click(self):
        if self.on_click_callback:
            self.on_click_callback(self.source_data)

    def set_selected(self, selected: bool):
        if selected:
            self.configure(border_width=2, fg_color="#1F6AA5")
        else:
            self.configure(border_width=0, fg_color="#2B2B2B")
