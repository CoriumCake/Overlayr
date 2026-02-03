import tkinter as tk
from tkinter import ttk
import threading
import time
from PIL import Image, ImageTk, ImageGrab
import sys
import os
import ctypes

# Import our modules
from ocr_engine import OCREngine
from translator import TranslatorWrapper
from image_utils import is_different

# Enable DPI awareness for sharper text on Windows
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

class ResultOverlay:
    def __init__(self, root):
        self.top = tk.Toplevel(root)
        self.top.title("Translation Overlay")
        self.top.overrideredirect(True) # Remove window decorations
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.8) # Slight transparency
        
        # Initial dummy geometry - will update on show
        self.top.geometry("400x100+100+100")
        self.top.configure(background='black')
        
        # Label for text
        self.label = tk.Label(self.top, text="", fg="white", bg="black", 
                              font=("Segoe UI", 16, "bold"), wraplength=400, justify="center")
        self.label.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.top.withdraw() # Hide initially
        
        self.blink_job = None
        self.is_blinking = False

    def start_blinking(self):
        if not self.top.winfo_viewable():
            return
        if self.is_blinking:
            return
        self.is_blinking = True
        self._blink_loop()

    def _blink_loop(self):
        if not self.is_blinking:
            return
        
        current_alpha = self.top.attributes("-alpha")
        # Toggle between 0.8 and 0.4
        new_alpha = 0.4 if current_alpha > 0.6 else 0.8
        self.top.attributes("-alpha", new_alpha)
        
        self.blink_job = self.top.after(500, self._blink_loop)

    def stop_blinking(self):
        self.is_blinking = False
        if self.blink_job:
            self.top.after_cancel(self.blink_job)
            self.blink_job = None
        self.top.attributes("-alpha", 0.8) # Reset to default

    def show(self, text, x=None, y=None):
        self.stop_blinking() # Ensure we stop blinking when showing new result
        self.label.config(text=text)
        self.top.deiconify()
        
        # Auto-resize logic
        # 1. Update idletasks to let tkinter calculate required size for the label
        self.top.update_idletasks()
        
        # 2. Get required width/height from the label
        req_w = self.label.winfo_reqwidth() + 20 # Padding
        req_h = self.label.winfo_reqheight() + 20 # Padding
        
        # 3. Limit width if it's too wide? Optional.
        # For now, let's just use the required size.
        
        if x is not None and y is not None:
            # Position ABOVE selection: y - req_h
            new_y = y - req_h
            # Ensure not off-screen
            new_y = max(0, new_y)
            self.top.geometry(f"{req_w}x{req_h}+{x}+{new_y}")
        else:
             # Just resize current position
             self.top.geometry(f"{req_w}x{req_h}")
            
    def hide(self):
        self.top.withdraw()

class ScreenTranslatorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Translator (JA -> TH)")
        self.root.geometry("600x400")
        self.root.attributes("-topmost", True)
        
        # Icon setup
        try:
            icon_path = self.resource_path("Icon.png")
            icon_img = tk.PhotoImage(file=icon_path)
            self.root.iconphoto(True, icon_img)
        except Exception as e:
            print(f"Failed to load icon: {e}")

        # State
        self.selection_coords = None # (x1, y1, x2, y2)
        self.ocr_engine = None # Lazy load
        self.translator = None # Lazy load
        self.last_image = None
        self.is_monitoring = False
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.overlay = None
        self.clear_timer = None

        # UI Components
        self._create_ui()
        self.overlay = ResultOverlay(self.root)

        # Initialize engines in background
        self.status_var.set("Initializing engines...")
        threading.Thread(target=self._init_engines, daemon=True).start()

    def resource_path(self, relative_path):
        """ Get absolute path to resource, works for dev and for PyInstaller """
        try:
            # PyInstaller creates a temp folder and stores path in _MEIPASS
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.abspath(".")

        return os.path.join(base_path, relative_path)

    def _create_ui(self):
        # Top Frame: Controls
        control_frame = ttk.Frame(self.root, padding=5)
        control_frame.pack(fill=tk.X)



        self.btn_select = ttk.Button(control_frame, text="Select Area", command=self.start_selection)
        self.btn_select.pack(side=tk.LEFT, padx=5)

        self.btn_capture = ttk.Button(control_frame, text="Capture & Translate", command=self.manual_capture)
        self.btn_capture.pack(side=tk.LEFT, padx=5)
        self.btn_capture.state(['disabled'])

        self.var_realtime = tk.BooleanVar()
        self.chk_realtime = ttk.Checkbutton(control_frame, text="Real-time Mode", variable=self.var_realtime, command=self.toggle_realtime)
        self.chk_realtime.pack(side=tk.LEFT, padx=5)
        self.chk_realtime.state(['disabled'])

        self.var_compact = tk.BooleanVar()
        self.chk_compact = ttk.Checkbutton(control_frame, text="Compact Mode", variable=self.var_compact, command=self.toggle_compact)
        self.chk_compact.pack(side=tk.LEFT, padx=5)

        # Bottom Frame: Status Bar
        status_frame = ttk.Frame(self.root, relief=tk.SUNKEN, padding=2)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        
        self.status_var = tk.StringVar(value="Ready")
        self.lbl_status = ttk.Label(status_frame, textvariable=self.status_var)
        self.lbl_status.pack(side=tk.LEFT, padx=5)

        self.progress = ttk.Progressbar(status_frame, orient=tk.HORIZONTAL, length=100, mode='determinate')
        self.progress.pack(side=tk.RIGHT, padx=5)

        # Middle: Split Pane for Original and Translated Text
        paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Original Text
        frame_orig = ttk.LabelFrame(paned, text="Original", padding=5)
        paned.add(frame_orig, weight=1)
        self.txt_original = tk.Text(frame_orig, height=5)
        self.txt_original.pack(fill=tk.BOTH, expand=True)

        # Translated Text
        frame_trans = ttk.LabelFrame(paned, text="Translated (Thai)", padding=5)
        paned.add(frame_trans, weight=1)
        self.txt_translated = tk.Text(frame_trans, height=5, font=("Segoe UI", 12))
        self.txt_translated.pack(fill=tk.BOTH, expand=True)

    def _init_engines(self):
        try:
            self.ocr_engine = OCREngine(languages=['ja','en'])
            self.translator = TranslatorWrapper(source='ja', target='th')
            self.root.after(0, lambda: self.status_var.set("Ready"))
            self.root.after(0, lambda: self.btn_capture.state(['!disabled']))
            self.root.after(0, lambda: self.chk_realtime.state(['!disabled']))
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"Error: {e}"))

    def start_selection(self):
        self.root.iconify() # Minimize main window
        
        # Create full screen overlay
        self.top = tk.Toplevel(self.root)
        self.top.attributes("-fullscreen", True)
        self.top.attributes("-alpha", 0.3)
        self.top.attributes("-topmost", True)
        self.top.configure(background='black')
        self.top.lift()

        self.canvas = tk.Canvas(self.top, cursor="cross", bg="grey11")
        self.canvas.pack(fill="both", expand=True)

        self.start_x = None
        self.start_y = None
        self.rect = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Escape>", lambda e: self._close_overlay())

    def on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline='red', width=2)

    def on_drag(self, event):
        self.cur_x, self.cur_y = (event.x, event.y)
        self.canvas.coords(self.rect, self.start_x, self.start_y, self.cur_x, self.cur_y)

    def on_release(self, event):
        if self.start_x and self.cur_x:
            x1 = min(self.start_x, self.cur_x)
            y1 = min(self.start_y, self.cur_y)
            x2 = max(self.start_x, self.cur_x)
            y2 = max(self.start_y, self.cur_y)
            
            # Ensure minimal size
            if (x2 - x1) > 10 and (y2 - y1) > 10:
                self.selection_coords = (x1, y1, x2, y2)
                self.status_var.set(f"Selected: {self.selection_coords}")
                # Reset last_image so next check triggers immediately if realtime is on
                self.last_image = None 
            
        self._close_overlay()
        self.root.deiconify() # Show main window
        
        # If realtime was on, it continues with new coords.
        # If we want to auto-capture on select:
        # self.manual_capture()

    def _close_overlay(self):
        self.top.destroy()

    def capture_screen(self):
        if not self.selection_coords:
            return None
        try:
            return ImageGrab.grab(bbox=self.selection_coords)
        except Exception as e:
            print(f"Capture failed: {e}")
            return None

    def process_image(self, image):
        if not image:
            return
        
        self.status_var.set("Processing...")
        self.root.after(0, lambda: self.progress.start(10))
        
        # Start blinking overlay if compact mode is on
        if self.var_compact.get():
            self.root.after(0, self.overlay.start_blinking)
        
        # 1. OCR
        text = self.ocr_engine.extract_text(image)
        
        # Update UI thread safe
        self.root.after(0, lambda: self.txt_original.delete(1.0, tk.END))
        self.root.after(0, lambda: self.txt_original.insert(tk.END, text))

        if not text.strip():
            self.root.after(0, lambda: self.status_var.set("No text found."))
            # Start timer to clear results if not already running
            if self.clear_timer is None:
                self.clear_timer = self.root.after(5000, self._clear_results)
            return

        # Text found, cancel clear timer if running
        if self.clear_timer is not None:
             self.root.after_cancel(self.clear_timer)
             self.clear_timer = None

        # 2. Translate
        translated = self.translator.translate(text)
        
        self.root.after(0, lambda: self.txt_translated.delete(1.0, tk.END))
        self.root.after(0, lambda: self.txt_translated.insert(tk.END, translated))
        self.root.after(0, lambda: self.txt_translated.insert(tk.END, translated))
        self.root.after(0, lambda: self.status_var.set("Done."))
        
        # Update Overlay if Compact Mode is ON
        if self.var_compact.get():
             x = self.selection_coords[0] if self.selection_coords else 100
             # Position ABOVE selection: Use y1 (top) - 10px padding
             y = self.selection_coords[1] - 10 if self.selection_coords else 100 
             self.root.after(0, lambda: self.overlay.show(translated, x, y))
             
        self.root.after(0, lambda: self.progress.stop())

    def _clear_results(self):
        self.clear_timer = None
        self.status_var.set("Cleared.")
        self.txt_original.delete(1.0, tk.END)
        self.txt_translated.delete(1.0, tk.END)
        self.overlay.hide()
        self.overlay.stop_blinking()



    def manual_capture(self):
        if not self.selection_coords:
            self.status_var.set("Please select an area first.")
            return

        image = self.capture_screen()
        # Run in thread to not freeze UI
        threading.Thread(target=self.process_image, args=(image,), daemon=True).start()

    def toggle_realtime(self):
        if self.var_realtime.get():
            if not self.selection_coords:
                self.var_realtime.set(False)
                self.status_var.set("Select area before enabling Real-time.")
                return
            
            self.is_monitoring = True
            self.stop_event.clear()
            self.monitor_thread = threading.Thread(target=self.realtime_loop, daemon=True)
            self.monitor_thread.start()
            self.status_var.set("Real-time Mode: ON")
        else:
            self.is_monitoring = False
            self.stop_event.set()
            self.status_var.set("Real-time Mode: OFF")

    def toggle_compact(self):
        if self.var_compact.get():
            # Enable Compact Mode
            self.root.geometry("400x120") # Shrink window
            # Hide text boxes? Or just keep them small. Let's just shrink.
            pass
        else:
            # Disable Compact Mode
            self.root.geometry("600x400") # Restore size
            self.overlay.hide()

    def realtime_loop(self):
        while self.is_monitoring and not self.stop_event.is_set():
            if not self.selection_coords:
                time.sleep(1)
                continue

            current_image = self.capture_screen()
            
            if self.last_image is None or is_different(self.last_image, current_image):
                # Image changed significantly
                print("Change detected, processing...")
                self.process_image(current_image)
                self.last_image = current_image
            
            # Wait 1 second before next check
            time.sleep(1.0)

if __name__ == "__main__":
    root = tk.Tk()
    app = ScreenTranslatorApp(root)
    root.mainloop()
