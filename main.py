import sys
import os
import json
import subprocess
import threading
import tkinter as tk
import customtkinter as ctinker
import win32gui
import win32api
import win32con
import win32process

from roi_selector import ROISelector
from overlay import OverlayWindow

ctinker.set_appearance_mode("dark")
ctinker.set_default_color_theme("blue")

class MainApp(ctinker.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("Overlayr - Pro Translator (Hybrid)")
        self.geometry("560x650")
        self.resizable(False, False)
        
        # State variables
        self.source_type = "window"  # "screen" | "window"
        self.source_list = []        # List of (id, display_name)
        self.selected_source_id = None
        
        self.roi_abs = None          # (abs_x, abs_y, w, h)
        self.roi_rel = None          # (rel_x, rel_y, w, h)
        
        self.backend_process = None
        self.is_capturing = False
        self.stdout_thread = None
        
        # Initialize transparent overlay window
        self.overlay = OverlayWindow(self)
        
        # Build UI layout
        self.create_widgets()
        self.update_source_list()
        
    def create_widgets(self):
        # Title Label
        self.lbl_title = ctinker.CTkLabel(
            self, text="Overlayr - Pro Translator", 
            font=ctinker.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color="#00ffff"
        )
        self.lbl_title.pack(pady=15)
        
        # Source Selection Tabs/Buttons
        self.source_frame = ctinker.CTkFrame(self)
        self.source_frame.pack(fill="x", padx=20, pady=5)
        
        self.btn_window_mode = ctinker.CTkButton(
            self.source_frame, text="Application Windows", 
            command=self.set_window_mode, fg_color="#1f6aa5"
        )
        self.btn_window_mode.pack(side="left", expand=True, padx=5, pady=5)
        
        self.btn_screen_mode = ctinker.CTkButton(
            self.source_frame, text="Monitors / Screens", 
            command=self.set_screen_mode, fg_color="#2b2b2b"
        )
        self.btn_screen_mode.pack(side="left", expand=True, padx=5, pady=5)
        
        # Source Dropdown Selector
        self.selector_frame = ctinker.CTkFrame(self)
        self.selector_frame.pack(fill="x", padx=20, pady=5)
        
        self.lbl_select = ctinker.CTkLabel(self.selector_frame, text="Select Target Source:")
        self.lbl_select.pack(anchor="w", padx=10, pady=2)
        
        self.combo_sources = ctinker.CTkOptionMenu(
            self.selector_frame, values=[], command=self.on_source_selected
        )
        self.combo_sources.pack(fill="x", padx=10, pady=5)
        
        # Refresh and ROI controls
        self.ctrl_frame = ctinker.CTkFrame(self)
        self.ctrl_frame.pack(fill="x", padx=20, pady=5)
        
        self.btn_refresh = ctinker.CTkButton(
            self.ctrl_frame, text="🔄 Refresh Sources", command=self.update_source_list, width=120
        )
        self.btn_refresh.pack(side="left", padx=10, pady=10)
        
        self.btn_roi = ctinker.CTkButton(
            self.ctrl_frame, text="🎯 Define Area", command=self.define_roi, width=120
        )
        self.btn_roi.pack(side="left", padx=10, pady=10)
        
        self.btn_clear_roi = ctinker.CTkButton(
            self.ctrl_frame, text="🧹 Clear Area", command=self.clear_roi, width=100, fg_color="#a51f1f"
        )
        self.btn_clear_roi.pack(side="left", padx=10, pady=10)
        
        # Settings: Source & Target Languages
        self.settings_frame = ctinker.CTkFrame(self)
        self.settings_frame.pack(fill="x", padx=20, pady=5)
        
        self.lbl_src_lang = ctinker.CTkLabel(self.settings_frame, text="Source Language:")
        self.lbl_src_lang.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.combo_src_lang = ctinker.CTkOptionMenu(
            self.settings_frame, values=["English", "Japanese", "Korean", "Chinese"], width=120
        )
        self.combo_src_lang.grid(row=0, column=1, padx=10, pady=5)
        self.combo_src_lang.set("English")
        
        self.lbl_tgt_lang = ctinker.CTkLabel(self.settings_frame, text="Target Language:")
        self.lbl_tgt_lang.grid(row=0, column=2, padx=10, pady=5, sticky="w")
        self.combo_tgt_lang = ctinker.CTkOptionMenu(
            self.settings_frame, values=["Thai", "English"], width=120
        )
        self.combo_tgt_lang.grid(row=0, column=3, padx=10, pady=5)
        self.combo_tgt_lang.set("Thai")
        
        # Start / Stop Button
        self.btn_action = ctinker.CTkButton(
            self, text="Start Capture", font=ctinker.CTkFont(size=16, weight="bold"),
            fg_color="#1fa55a", command=self.toggle_capture, height=45
        )
        self.btn_action.pack(fill="x", padx=20, pady=15)
        
        # Live Readings Box (Debug logs)
        self.debug_frame = ctinker.CTkFrame(self)
        self.debug_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.lbl_debug = ctinker.CTkLabel(
            self.debug_frame, text="📖 Live Readings (Debug Window):", 
            font=ctinker.CTkFont(weight="bold")
        )
        self.lbl_debug.pack(anchor="w", padx=10, pady=5)
        
        self.txt_debug = ctinker.CTkTextbox(self.debug_frame, state="disabled")
        self.txt_debug.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Status Bar
        self.lbl_status = ctinker.CTkLabel(
            self, text="Status: Ready", anchor="w", 
            font=ctinker.CTkFont(size=11)
        )
        self.lbl_status.pack(fill="x", side="bottom", padx=20, pady=5)
        
    def set_window_mode(self):
        self.source_type = "window"
        self.btn_window_mode.configure(fg_color="#1f6aa5")
        self.btn_screen_mode.configure(fg_color="#2b2b2b")
        self.update_source_list()
        
    def set_screen_mode(self):
        self.source_type = "screen"
        self.btn_screen_mode.configure(fg_color="#1f6aa5")
        self.btn_window_mode.configure(fg_color="#2b2b2b")
        self.update_source_list()
        
    def update_source_list(self):
        self.combo_sources.configure(values=[])
        self.source_list.clear()
        
        if self.source_type == "window":
            def enum_callback(hwnd, extra):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    # Ignore tool windows, empty titles, or main app
                    if title and title != self.title:
                        # Exclude program manager and shell
                        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                        if not (style & win32con.WS_EX_TOOLWINDOW):
                            self.source_list.append((hwnd, f"Window: {title}"))
            win32gui.EnumWindows(enum_callback, None)
        else:
            for i, monitor in enumerate(win32api.EnumDisplayMonitors()):
                hmonitor = monitor[0].handle
                info = win32api.GetMonitorInfo(monitor[0])
                name = f"Screen {i+1} ({info['Monitor'][2] - info['Monitor'][0]}x{info['Monitor'][3] - info['Monitor'][1]})"
                self.source_list.append((hmonitor, name))
                
        # Populate Combobox
        display_names = [name for _, name in self.source_list]
        self.combo_sources.configure(values=display_names)
        if display_names:
            self.combo_sources.set(display_names[0])
            self.selected_source_id = self.source_list[0][0]
        else:
            self.combo_sources.set("No sources found")
            self.selected_source_id = None
            
    def on_source_selected(self, chosen_name):
        for s_id, name in self.source_list:
            if name == chosen_name:
                self.selected_source_id = s_id
                break
                
    def define_roi(self):
        if not self.selected_source_id:
            self.log_debug("Please select a target source first!")
            return
        
        # Clear previous ROI
        self.clear_roi()
        
        # Start Snipping overlay
        ROISelector(self.apply_roi)
        
    def apply_roi(self, abs_x, abs_y, w, h):
        self.roi_abs = (abs_x, abs_y, w, h)
        
        # Calculate ROI relative to target source coordinates
        src_x, src_y, src_w, src_h = self.get_source_bounds()
        
        rel_x = abs_x - src_x
        rel_y = abs_y - src_y
        
        self.roi_rel = (rel_x, rel_y, w, h)
        self.log_debug(f"Area defined: {int(w)}x{int(h)} at offset ({int(rel_x)},{int(rel_y)})")
        self.btn_roi.configure(text="🎯 Area Locked", fg_color="orange")
        
    def clear_roi(self):
        self.roi_abs = None
        self.roi_rel = None
        self.btn_roi.configure(text="🎯 Define Area", fg_color=["#3B8ED0", "#1F6AA5"])
        self.log_debug("Area Cleared")
        
    def get_source_bounds(self):
        if not self.selected_source_id:
            return 0, 0, 800, 600
            
        if self.source_type == "screen":
            try:
                info = win32api.GetMonitorInfo(self.selected_source_id)
                rect = info["Monitor"]
                return rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
            except Exception as e:
                print(f"Error getting monitor bounds: {e}")
                return 0, 0, 800, 600
        else:
            try:
                rect = win32gui.GetWindowRect(self.selected_source_id)
                return rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
            except Exception as e:
                print(f"Error getting window bounds: {e}")
                return 0, 0, 800, 600

    def log_debug(self, message):
        self.txt_debug.configure(state="normal")
        self.txt_debug.insert("end", message + "\n")
        self.txt_debug.see("end")
        self.txt_debug.configure(state="disabled")
        
    def toggle_capture(self):
        if self.is_capturing:
            self.stop_capture()
        else:
            self.start_capture()
            
    def start_capture(self):
        if not self.selected_source_id:
            self.log_debug("Error: No source selected.")
            return
            
        self.is_capturing = True
        self.btn_action.configure(text="Stop Capture", fg_color="#a51f1f")
        self.lbl_status.configure(text="Status: Launching Backend...")
        
        # Clear debug box
        self.txt_debug.configure(state="normal")
        self.txt_debug.delete("1.0", "end")
        self.txt_debug.configure(state="disabled")
        
        # Start Backend Subprocess
        try:
            # Creation flags to completely hide console window on Windows
            creation_flags = subprocess.CREATE_NO_WINDOW
            
            self.backend_process = subprocess.Popen(
                ["Overlayr.exe", "--backend"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=creation_flags
            )
            
            # Start background reading thread
            self.stdout_thread = threading.Thread(target=self.read_backend_output, daemon=True)
            self.stdout_thread.start()
            
            # Send start command
            src_lang_code = "en" if self.combo_src_lang.get() == "English" else "ja"
            tgt_lang_code = "th" if self.combo_tgt_lang.get() == "Thai" else "en"
            
            # Configure relative crop if ROI is set
            roi_json = None
            if self.roi_rel:
                roi_json = {
                    "x": float(self.roi_rel[0]),
                    "y": float(self.roi_rel[1]),
                    "w": float(self.roi_rel[2]),
                    "h": float(self.roi_rel[3])
                }
                
            cmd = {
                "action": "start",
                "source_type": self.source_type,
                "source_id": self.selected_source_id,
                "roi": roi_json,
                "src_lang": src_lang_code,
                "tgt_lang": tgt_lang_code
            }
            
            self.send_backend_command(cmd)
            
        except Exception as e:
            self.log_debug(f"Failed to start backend: {e}")
            self.stop_capture()

    def send_backend_command(self, cmd_dict):
        if self.backend_process and self.backend_process.stdin:
            try:
                cmd_line = json.dumps(cmd_dict) + "\n"
                self.backend_process.stdin.write(cmd_line)
                self.backend_process.stdin.flush()
            except Exception as e:
                print(f"Error sending command to backend: {e}")
                
    def read_backend_output(self):
        while self.is_capturing and self.backend_process:
            line = self.backend_process.stdout.readline()
            if not line:
                break
            
            try:
                data = json.loads(line.strip())
                status = data.get("status")
                
                if status == "ready":
                    self.after(0, lambda m=data.get("message", "Ready"): self.lbl_status.configure(text=f"Status: {m}"))
                elif status == "info":
                    msg = data.get("message", "")
                    self.after(0, lambda m=msg: self.lbl_status.configure(text=f"Status: {m}"))
                elif status == "error":
                    msg = data.get("message", "")
                    self.after(0, lambda m=msg: self.lbl_status.configure(text=f"Status: {m}", text_color="red"))
                    self.after(0, lambda m=msg: self.log_debug(f"Backend Error: {m}"))
                elif status == "results":
                    results = data.get("results", [])
                    self.after(0, lambda r=results: self.handle_translation_results(r))
            except Exception as e:
                print(f"Error parsing backend line: {e}")
                
        self.after(0, self.on_backend_stopped)
        
    def handle_translation_results(self, results):
        if not self.is_capturing:
            return
            
        # Log to debug textbox
        for item in results:
            text = item.get("text", "")
            trans = item.get("translated", "")
            self.log_debug(f"OCR: {text} → {trans}")
            
        # Draw on overlay window
        if results:
            src_x, src_y, src_w, src_h = self.get_source_bounds()
            
            # Position overlay window at target screen location
            if self.roi_abs:
                # ROI mode: place overlay exactly over Snapped ROI absolute location
                ox, oy, ow, oh = self.roi_abs
                self.overlay.set_geometry(ox, oy, ow, oh)
            else:
                # Full source mode: place overlay exactly over the window/screen absolute location
                self.overlay.set_geometry(src_x, src_y, src_w, src_h)
                
            self.overlay.show_results(results)
        else:
            self.overlay.hide()
            
    def stop_capture(self):
        self.is_capturing = False
        self.lbl_status.configure(text="Status: Stopping Capture...", text_color="white")
        
        # Send stop command to backend
        self.send_backend_command({"action": "stop"})
        
        # Wait a moment, then terminate backend if it's still alive
        if self.backend_process:
            self.send_backend_command({"action": "exit"})
            try:
                self.backend_process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.backend_process.kill()
            self.backend_process = None
            
        self.overlay.hide()
        self.btn_action.configure(text="Start Capture", fg_color="#1fa55a")
        self.lbl_status.configure(text="Status: Stopped")
        
    def on_backend_stopped(self):
        if self.is_capturing:
            self.log_debug("Backend process terminated unexpectedly.")
            self.stop_capture()

    def destroy(self):
        self.stop_capture()
        super().destroy()

if __name__ == "__main__":
    app = MainApp()
    app.mainloop()
