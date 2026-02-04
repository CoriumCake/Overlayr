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
            try:
                import ctypes
                from ctypes import wintypes
                
                # Get window handle
                hwnd = self.top.winfo_id()
                
                # WDA_EXCLUDEFROMCAPTURE = 0x00000011 (Windows 10 2004+)
                # WDA_MONITOR = 0x00000001 (Prevents capture, shows black)
                # We use 0x11 to just EXCLUDE it completely so computer sees through it
                WDA_EXCLUDEFROMCAPTURE = 0x00000011
                
                user32 = ctypes.windll.user32
                user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
                user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
                
                success = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
                if success:
                    print("Overlay Invisibility: ENABLED (Computer sees through translation)")
                else:
                    print("Overlay Invisibility: FAILED to apply affinity.")
            except Exception as e:
                print(f"Overlay Invisibility: Error setting affinity: {e}")
        
    def show_results(self, results: list, offset_x: int, offset_y: int):
        self.top.deiconify()
        
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
            glass_bg = "#1a1a1a"
            fg_color = "#ffffff"
            
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
            
            text_id = self.canvas.create_text(
                cx, cy,
                text=text,
                anchor="center",
                fill=fg_color,
                font=font,
                width=box_w, # Wrap at original width
                tags="result"
            )
            
            # Get actual bounding box of the drawn text
            t_bbox = self.canvas.bbox(text_id)
            if not t_bbox: continue
            
            padding_x = 8
            padding_y = 4
            
            # Draw background behind text using text's actual bbox
            bg_rect_id = self.canvas.create_rectangle(
                t_bbox[0] - padding_x, t_bbox[1] - padding_y,
                t_bbox[2] + padding_x, t_bbox[3] + padding_y,
                fill=glass_bg,
                outline="#333333",
                width=1,
                tags="result"
            )
            
            # Lower background behind text
            self.canvas.tag_lower(bg_rect_id, text_id)
            
            # Glow/Highlight on text box
            self.canvas.create_line(
                t_bbox[0] - padding_x, t_bbox[3] + padding_y,
                t_bbox[2] + padding_x, t_bbox[3] + padding_y,
                fill="#444444", width=1,
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

