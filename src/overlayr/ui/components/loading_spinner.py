import tkinter as tk
import customtkinter as ctk # type: ignore
import math

class LoadingSpinner(ctk.CTkFrame):
    def __init__(self, master, width=40, height=40, color=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.width = width
        self.height = height
        self.color = color if color else ["#1F6AA5", "#1F6AA5"] # Light/Dark mode colors or simple list
        if isinstance(self.color, str):
            self.color = [self.color, self.color]
            
        # Determine background color for Canvas (tkinter doesn't support 'transparent')
        # We need to manually match the parent's color or standard theme bg
        bg_color = self._fg_color
        if bg_color == "transparent" or bg_color == ["transparent", "transparent"]:
            # Use standard CTk background colors as fallback
            # Light: gray92, Dark: gray14
            bg_color = ["gray92", "gray14"]
        
        self.bg_color = bg_color    
            
        self.canvas = tk.Canvas(
            self, 
            width=self.width, 
            height=self.height, 
            bg=self._apply_appearance_mode(bg_color), 
            highlightthickness=0
        )
        self.canvas.pack(expand=True, fill="both")
        
        self.angle = 0
        self.is_running = False
        self.animation_id = None
        self.arc_id = None
        
        # Draw initial arc
        self._draw_spinner()
        
    def _draw_spinner(self):
        self.canvas.delete("all")
        w = self.width
        h = self.height
        
        # Determine color based on mode
        mode = ctk.get_appearance_mode()
        color = self.color[1] if mode == "Dark" else self.color[0]
        
        # Transparent BG trick if needed, but for now simple canvas bg matching frame
        self.canvas.configure(bg=self._apply_appearance_mode(self.bg_color))
        
        # Draw arc
        # Bounding box for arc
        pad = 4
        self.arc_id = self.canvas.create_arc(
            pad, pad, w-pad, h-pad,
            start=self.angle,
            extent=270, # 3/4 circle
            style="arc",
            width=4,
            outline=color
        )
        
    def start(self):
        if not self.is_running:
            self.is_running = True
            self._animate()
            
    def stop(self):
        self.is_running = False
        if self.animation_id:
            self.after_cancel(self.animation_id)
            self.animation_id = None
            
    def _animate(self):
        if not self.is_running: return
        
        self.angle = (self.angle - 10) % 360 # Counter-clockwise
        
        # Update arc start angle
        if self.arc_id:
            self.canvas.itemconfigure(self.arc_id, start=self.angle)
            
        self.animation_id = self.after(20, self._animate) # 50fps

    def destroy(self):
        self.stop()
        super().destroy()
