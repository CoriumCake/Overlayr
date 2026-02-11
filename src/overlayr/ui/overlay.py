import tkinter as tk
import customtkinter as ctk # type: ignore
import platform

class ResultOverlay:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.top = tk.Toplevel(root)
        self.top.title("Translation Overlay")
        self.top.overrideredirect(True) # Remove window decorations
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.8) # Slight transparency
        
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
        
    def show_results(self, results: list, offset_x: int, offset_y: int, visible: bool = True):
        """
        Draw results on canvas.
        :param visible: If True, makes the window visible (deiconify). If False, effectively hides (withdraw) but updates content?
                       Actually, if hidden, we can just Draw. But we should Withdraw if switching to hidden.
        """
        if visible:
            self.top.deiconify()
        else:
            self.top.withdraw()
        
        # Clear only translation results, keep debug text
        self.canvas.delete("result")
        
        if not results: return
        
        import math
        
        for item in results:
            bbox = item['bbox'] # [left, top, right, bottom]
            text = item['translated']
            if not text: continue
            
            # Use average line height from OCR as base font size
            # Fallback to heuristic if not available
            line_height = item.get('line_height', 0)
            
            box_w = bbox[2] - bbox[0]
            box_h = bbox[3] - bbox[1]
            
            if line_height > 0:
                # User requested: Font size equal to size of letters to be translated
                # Set font size slightly smaller than line height for padding
                font_size = int(line_height * 0.9)
                font_size = max(10, min(80, font_size)) # Relaxed limits
            else:
                # Fallback heuristic
                max_h_size = box_h * 0.8
                char_aspect = 0.6
                max_w_size = box_w / (max(1, len(text)) * char_aspect)
                font_size = int(min(max_h_size, max_w_size * 2))
                font_size = max(10, min(60, font_size))

            font = ("Segoe UI Semibold", font_size)
            
            # Use central theme colors
            from .theme import get_theme
            colors = get_theme()
            overlay_bg = colors.get("overlay_bg", "#1a1a1a")
            overlay_fg = colors.get("overlay_text", "#ffffff")
            overlay_border = colors.get("overlay_border", "#333333")
            overlay_glow = colors.get("overlay_glow", "#444444")
            
            # Draw text FIRST to measure it
            # Position: align top-left with original box for now, can center later
            # x1, y1 = bbox[0] + offset_x, bbox[1] + offset_y
            
            # Better strategy: Center text in original box's center? 
            # Or assume text replaces original.
            # Let's align top-left but wrap at original box width?
            # User wants "background gray color according to size of text result"
            
            cx = bbox[0] + offset_x + box_w/2
            cy = bbox[1] + offset_y + box_h/2
            
            # Draw text centered
            # Width constraint: Let's set width to box_w * 1.5 to allow some expansion but wrap eventually
            # Or just set width=box_w to keep layout clean? 
            # User said "Result text overflow", so maybe allow wider?
            # Let's try width=box_w first.
            
            # --- AUTO-FIT LOGIC ---
            min_font_size = 10
            
            # Create text initially
            text_id = self.canvas.create_text(
                cx, cy,
                text=text,
                anchor="center",
                fill=overlay_fg,
                font=font,
                width=box_w, # Wrap at original width
                tags="result"
            )
            
            # Measure
            t_bbox = self.canvas.bbox(text_id)
            if t_bbox:
                text_h = t_bbox[3] - t_bbox[1]
                
                # If text height exceeds box height, shrink font
                while text_h > box_h and font_size > min_font_size:
                    font_size -= 2 # Step down
                    font = ("Segoe UI Semibold", font_size)
                    
                    self.canvas.itemconfig(text_id, font=font)
                    t_bbox = self.canvas.bbox(text_id)
                    if t_bbox:
                        text_h = t_bbox[3] - t_bbox[1]
                    else:
                        break # Should not happen

            
            # Get actual bounding box of the drawn text
            t_bbox = self.canvas.bbox(text_id)
            if not t_bbox: continue
            
            padding_x = 8
            padding_y = 4
            
            # Draw background behind text using text's actual bbox
            bg_rect_id = self.canvas.create_rectangle(
                t_bbox[0] - padding_x, t_bbox[1] - padding_y,
                t_bbox[2] + padding_x, t_bbox[3] + padding_y,
                fill=overlay_bg,
                outline=overlay_border,
                width=1,
                tags="result"
            )
            
            # Lower background behind text
            self.canvas.tag_lower(bg_rect_id, text_id)
            
            # Glow/Highlight on text box
            if overlay_glow and overlay_glow != "transparent":
                self.canvas.create_line(
                    t_bbox[0] - padding_x, t_bbox[3] + padding_y,
                    t_bbox[2] + padding_x, t_bbox[3] + padding_y,
                    fill=overlay_glow, width=1,
                    tags="result"
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

