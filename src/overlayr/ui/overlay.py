import tkinter as tk
import customtkinter as ctk # type: ignore
import platform

from ..core.overlay_render import (DEFAULT_OVERLAY_STYLE, MIN_FONT_SIZE,
                                   PADDING_X, PADDING_Y, STYLE_REPLACE,
                                   corner_radius, fit_font_size, wrap_width_for)

FONT_FAMILY = "Segoe UI Semibold"

# Replace mode has to hide the words underneath it, so the window is fully
# opaque. Card mode is a panel floating over the source and reads better with a
# little of the original showing through.
STYLE_ALPHA = {STYLE_REPLACE: 1.0}
DEFAULT_ALPHA = 0.8


class ResultOverlay:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.top = tk.Toplevel(root)
        self.top.title("Translation Overlay")
        self.top.overrideredirect(True) # Remove window decorations
        self.top.attributes("-topmost", True)
        self.style = DEFAULT_OVERLAY_STYLE
        self.top.attributes("-alpha", STYLE_ALPHA.get(self.style, DEFAULT_ALPHA))
        
        # Initial dummy geometry
        self.top.geometry("400x100+100+100")
        
        # Background color for transparency trick
        # Windows supports specific color transparency
        if platform.system() == "Windows":
            self.top.configure(background='black')
            self.top.attributes('-transparentcolor', 'black')
            self.bg_color = 'black'
        else:
            self.top.configure(background='gray')
            self.top.attributes('-alpha', 0.5) 
            self.bg_color = 'gray'

        # Fullscreen transparent canvas
        self.canvas = tk.Canvas(self.top, bg=self.bg_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        # Debug console (top left)
        self.debug_text_id = self.canvas.create_text(
            10, 10,
            text="",
            anchor="nw",
            fill="cyan",
            font=("Consolas", 14, "bold"),
            width=1200,
            tags="debug"
        )
        
        self.top.withdraw() # Hide initially
        
        # Make the overlay "Invisible to Screen Capture" on Windows
        # This prevents the OCR from reading our own translated text!
        if platform.system() == "Windows":
             self._apply_window_styles()
                
    def _apply_window_styles(self):
        try:
            import ctypes
            from ctypes import wintypes
            
            # Ensure window is partially mapped so it has an HWND
            self.top.update_idletasks()
            
            # Get window handle
            hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id())
            if not hwnd:
                hwnd = self.top.winfo_id()
                
            # WDA_EXCLUDEFROMCAPTURE = 0x00000011 (Windows 10 2004+)
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            
            user32 = ctypes.windll.user32
            
            # 1. Set Window Display Affinity (Invisible to Capture)
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
            
            success = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if success:
                print(f"Overlay Invisibility: ENABLED (HWND: {hwnd})", flush=True)
            else:
                err = ctypes.GetLastError()
                print(f"Overlay Invisibility: FAILED (HWND: {hwnd}, Error: {err})", flush=True)

            # 2. Set Extended Window Styles (Click-Through & No-Focus)
            # WS_EX_TRANSPARENT (0x20): Events pass through window
            # WS_EX_LAYERED (0x80000): Required for transparency
            # WS_EX_TOOLWINDOW (0x80): Hides from Alt-Tab
            # WS_EX_NOACTIVATE (0x08000000): Prevents focus stealing
            
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED     = 0x00080000
            WS_EX_TOOLWINDOW  = 0x00000080
            WS_EX_NOACTIVATE  = 0x08000000
            
            # Get current style
            current_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            
            # Add new styles
            new_style = current_style | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            
            # Set new style
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            print(f"Overlay Click-Through: ENABLED (Style: {hex(new_style)})", flush=True)
                
        except Exception as e:
            print(f"Overlay Style Error: {e}", flush=True)
        
    def set_style(self, style: str) -> None:
        """Choose how translations are presented (CLAUDE.md 9.24)."""
        self.style = style
        self.top.attributes("-alpha", STYLE_ALPHA.get(style, DEFAULT_ALPHA))

    def show_results(self, results: list, offset_x: int, offset_y: int, visible: bool = True):
        """Draw one frame's translated blocks.

        Replace mode paints each block over the box the original text came from
        so the source words disappear; Card mode draws a panel sized to the
        translation, which is what the overlay did before the style existed.
        """
        if visible:
            self.top.deiconify()
        else:
            self.top.withdraw()

        # Clear only translation results, keep debug text
        self.canvas.delete("result")

        if not results:
            return

        for item in results:
            text = item.get("translated")
            if not text:
                continue
            if self.style == STYLE_REPLACE:
                self._draw_replace(item, text, offset_x, offset_y)
            else:
                self._draw_card(item, text, offset_x, offset_y)

    def _fit(self, text_id, text, box_w, box_h, line_height):
        """Shrink the drawn text until it sits inside the original box."""
        def measure(candidate, size, wrap):
            self.canvas.itemconfig(text_id, text=candidate,
                                   font=(FONT_FAMILY, size), width=wrap)
            bounds = self.canvas.bbox(text_id)
            if not bounds:
                return (0, 0)
            return (bounds[2] - bounds[0], bounds[3] - bounds[1])

        size = fit_font_size(text, box_w, box_h, measure, line_height=line_height)
        self.canvas.itemconfig(text_id, text=text, font=(FONT_FAMILY, size),
                               width=wrap_width_for(box_w))
        return size

    def _draw_replace(self, item, text, offset_x, offset_y):
        """Cover the source text with its own paper colour and write on top."""
        left, top, right, bottom = item["bbox"]
        x1, y1 = left + offset_x, top + offset_y
        x2, y2 = right + offset_x, bottom + offset_y
        box_w, box_h = max(1, x2 - x1), max(1, y2 - y1)

        # The fill is the whole point of this mode: it has to be the original
        # box, not the size of the translation, or the source text shows around
        # the edges of a translation that came out shorter.
        self._fill_patch(x1 - PADDING_X, y1 - PADDING_Y,
                         x2 + PADDING_X, y2 + PADDING_Y,
                         item.get("bg_color") or "#010101")

        text_id = self.canvas.create_text(
            x1 + box_w / 2, y1 + box_h / 2,
            text=text,
            anchor="center",
            fill=item.get("text_color") or "#ffffff",
            font=(FONT_FAMILY, MIN_FONT_SIZE),
            width=wrap_width_for(box_w),
            tags="result",
        )
        self._fit(text_id, text, box_w, box_h, item.get("line_height", 0))

    def _fill_patch(self, x1, y1, x2, y2, colour):
        """Paint a rounded patch over the source text.

        Tk has no rounded rectangle, but a polygon through the corner points
        with ``smooth`` on is one, and it costs the same to draw.
        """
        radius = corner_radius(x2 - x1, y2 - y1)
        if radius < 2:
            return self.canvas.create_rectangle(
                x1, y1, x2, y2, fill=colour, outline="", tags="result")

        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1,
            x2, y1 + radius, x2, y2 - radius, x2, y2,
            x2 - radius, y2, x1 + radius, y2, x1, y2,
            x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return self.canvas.create_polygon(
            points, fill=colour, outline=colour, smooth=True, tags="result")

    def _draw_card(self, item, text, offset_x, offset_y):
        """Float a themed panel above the source text."""
        from .theme import get_theme
        colors = get_theme()

        left, top, right, bottom = item["bbox"]
        box_w = max(1, right - left)
        box_h = max(1, bottom - top)
        cx = left + offset_x + box_w / 2
        cy = top + offset_y + box_h / 2

        text_id = self.canvas.create_text(
            cx, cy,
            text=text,
            anchor="center",
            fill=colors.get("overlay_text", "#ffffff"),
            font=(FONT_FAMILY, MIN_FONT_SIZE),
            width=wrap_width_for(box_w),
            tags="result",
        )
        self._fit(text_id, text, box_w, box_h, item.get("line_height", 0))

        bounds = self.canvas.bbox(text_id)
        if not bounds:
            return

        card_padding_x, card_padding_y = 8, 4
        background = self.canvas.create_rectangle(
            bounds[0] - card_padding_x, bounds[1] - card_padding_y,
            bounds[2] + card_padding_x, bounds[3] + card_padding_y,
            fill=colors.get("overlay_bg", "#1a1a1a"),
            outline=colors.get("overlay_border", "#333333"),
            width=1,
            tags="result",
        )
        self.canvas.tag_lower(background, text_id)

        glow = colors.get("overlay_glow", "#444444")
        if glow and glow != "transparent":
            self.canvas.create_line(
                bounds[0] - card_padding_x, bounds[3] + card_padding_y,
                bounds[2] + card_padding_x, bounds[3] + card_padding_y,
                fill=glow, width=1, tags="result",
            )

    def set_geometry(self, geometry):
        # Prevent jumpy movement by only updating if changed significantly or just setting directly
        self.top.geometry(geometry)

    def show_boxes(self, boxes: list, offset_x: int, offset_y: int, color="cyan"):
        """Draw debug boxes (e.g. for merged text blocks)"""
        self.top.deiconify()
        # Clear only previous boxes
        self.canvas.delete("debug_box")
        
        for bbox in boxes:
            # bbox is [left, top, right, bottom]
            x1 = bbox[0] + offset_x
            y1 = bbox[1] + offset_y
            x2 = bbox[2] + offset_x
            y2 = bbox[3] + offset_y
            
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=color,
                width=2,
                tags="debug_box"
            )
            # Add label
            self.canvas.create_text(
                x1, y1-10,
                text="Group", 
                fill=color,
                anchor="sw",
                font=("Consolas", 10),
                tags="debug_box"
            )

    
    def clear_boxes(self):
        """Explicitly clear debug boxes"""
        self.canvas.delete("debug_box")

    def hide_for_capture(self):
        """
        Temporarily hides the window using Win32 API directly.
        Safe to call from background capture thread on Windows.
        """
        # Clear debug boxes on new capture cycle
        self.canvas.delete("debug_box")
        
        if platform.system() == "Windows":
            try:
                import ctypes
                hwnd = self.top.winfo_id()
                # SW_HIDE = 0
                ctypes.windll.user32.ShowWindow(hwnd, 0)
            except:
                pass

    def restore_after_capture(self):
        """
        Restores window visibility.
        """
        if platform.system() == "Windows":
            try:
                import ctypes
                hwnd = self.top.winfo_id()
                # SW_SHOWNOACTIVATE = 4 (Shows window without stealing focus)
                ctypes.windll.user32.ShowWindow(hwnd, 4)
            except:
                pass
    
    def show_debug(self, raw_text: str):
        # self.canvas.itemconfig(self.debug_text_id, text=f"OCR RAW: {raw_text}")
        # self.canvas.tag_raise(self.debug_text_id)
        pass

    def hide(self) -> None:
        self.canvas.delete("result")
        self.canvas.delete("debug_box")
        self.canvas.itemconfig(self.debug_text_id, text="")
        self.top.withdraw()

