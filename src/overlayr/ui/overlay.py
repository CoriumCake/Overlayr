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
        
        for item in results:
            bbox = item['bbox'] # [left, top, right, bottom]
            text = item['translated']
            bg = item['bg_color'] # Original background color
            
            # Use a slightly more transparent and "glassy" version of the detected background
            # Or use a sleek dark glass theme if background is erratic
            glass_bg = "#1a1a1a" # Dark slate
            
            # Map coordinates
            x1 = bbox[0]
            y1 = bbox[1]
            x2 = bbox[2]
            y2 = bbox[3]
            
            # Calculate font size from box height (aiming for 1:1 match)
            box_height = y2 - y1
            # 0.95 or 1.0 multiplier for true 1:1 feel on Segoe UI
            font_size = max(12, int(box_height * 0.95))
            font = ("Segoe UI Semibold", font_size)
            
            # Foreground color (white with better contrast)
            fg_color = "#ffffff"
            
            # Draw semi-transparent background with rounded effect
            padding_x = 10
            padding_y = 6
            
            # Create text centered vertically in the box
            text_id = self.canvas.create_text(
                x1 + padding_x, y1 + (box_height / 2),
                text=text,
                anchor="w", 
                fill=fg_color,
                font=font,
                width=x2 - x1 + 300,
                tags="result"
            )
            
            t_bbox = self.canvas.bbox(text_id)
            if not t_bbox: continue
            
            # Draw "Glass" background behind text
            bg_rect_id = self.canvas.create_rectangle(
                t_bbox[0] - padding_x/2, t_bbox[1] - padding_y/2,
                t_bbox[2] + padding_x/2, t_bbox[3] + padding_y/2,
                fill=glass_bg,
                outline="#333333",
                width=1,
                tags="result"
            )
            
            # Glow effect (stacking a blurred version or just offset shadows)
            # For simplicity and performance, we'll just use a subtle highlight line
            self.canvas.create_line(
                t_bbox[0] - padding_x, t_bbox[1] - 2,
                t_bbox[2] + padding_x, t_bbox[1] - 2,
                fill="#444444", width=1,
                tags="result"
            )
            
            self.canvas.tag_lower(bg_rect_id, text_id)

    def set_geometry(self, geometry):
        # Prevent jumpy movement by only updating if changed significantly or just setting directly
        self.top.geometry(geometry)

    def show_debug(self, raw_text: str):
        self.canvas.itemconfig(self.debug_text_id, text=f"OCR RAW: {raw_text}")
        self.canvas.tag_raise(self.debug_text_id)

    def hide(self) -> None:
        self.canvas.delete("result")
        self.canvas.itemconfig(self.debug_text_id, text="")
        self.top.withdraw()

