import tkinter as tk
import ctypes

class OverlayWindow:
    def __init__(self, parent):
        self.top = tk.Toplevel(parent)
        self.top.withdraw()  # Start hidden
        
        # Configure window
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-transparentcolor", "black")  # Key color for GDI transparency
        self.top.configure(bg="black")
        
        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.enable_click_through()
        
    def enable_click_through(self):
        try:
            # Ensure window has been mapped
            self.top.update_idletasks()
            
            user32 = ctypes.windll.user32
            # On Windows, Toplevel HWND corresponds to the child frame; 
            # We must target the actual OS container window parent handle.
            hwnd = user32.GetParent(self.top.winfo_id())
            if not hwnd:
                hwnd = self.top.winfo_id()
            
            # Hide from capture (WDA_EXCLUDEFROMCAPTURE = 0x00000011) to avoid feedback loops
            user32.SetWindowDisplayAffinity(hwnd, 0x00000011)
            
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED     = 0x00080000
            WS_EX_TOOLWINDOW  = 0x00000080
            WS_EX_NOACTIVATE  = 0x08000000
            
            current_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_style = current_style | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        except Exception as e:
            print(f"Error configuring overlay click-through: {e}")
            
    def set_geometry(self, x, y, w, h):
        self.top.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
        
    def show_results(self, results):
        self.canvas.delete("all")
        self.top.deiconify()
        
        for item in results:
            bbox = item["bbox"]
            x1 = bbox[0][0]
            y1 = bbox[0][1]
            x2 = bbox[2][0]
            y2 = bbox[2][1]
            
            # Draw outline rectangle
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline="#00ffff",  # Cyan highlight border
                width=2
            )
            
    def hide(self):
        self.canvas.delete("all")
        self.top.withdraw()
