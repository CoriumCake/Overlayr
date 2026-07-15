import sys
import os
import json
import subprocess
import threading
import ctypes
from ctypes import wintypes
import tkinter as tk
import customtkinter as ctinker
from PIL import Image, ImageGrab
import win32gui
import win32api
import win32con
import win32process

from roi_selector import ROISelector
from overlay import OverlayWindow

# Web-aligned Dark Theme Palette
COLORS = {
    "bg_primary": "#090e15",
    "bg_card": "#111821",
    "accent": "#3d98f4",
    "accent_hover": "#5aabf7",
    "primary_accent": "#00e9ff",
    "secondary_accent": "#182230",
    "text_primary": "#ffffff",
    "text_secondary": "#b2bdcb",
    "border": "#1e2a38",
    "success": "#5cb97a",
    "error": "#d4605a",
}

ctinker.set_appearance_mode("dark")

def get_screen_thumbnail(hmonitor, size=(180, 110)):
    try:
        info = win32api.GetMonitorInfo(hmonitor)
        rect = info["Monitor"]
        bbox = (rect[0], rect[1], rect[2], rect[3])
        img = ImageGrab.grab(bbox)
        return img.resize(size, Image.Resampling.LANCZOS)
    except Exception as e:
        print(f"Error capturing screen thumbnail: {e}")
        return None

def get_window_thumbnail(hwnd, size=(180, 110)):
    try:
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        
        if w <= 0 or h <= 0:
            return None
            
        hwnd_dc = ctypes.windll.user32.GetWindowDC(hwnd)
        mfc_dc = ctypes.windll.gdi32.CreateCompatibleDC(hwnd_dc)
        save_bit_map = ctypes.windll.gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        ctypes.windll.gdi32.SelectObject(mfc_dc, save_bit_map)
        
        # PrintWindow PW_RENDERFULLCONTENT = 2
        ctypes.windll.user32.PrintWindow(hwnd, mfc_dc, 2)
        
        bitmap_bits = ctypes.create_string_buffer(w * h * 4)
        ctypes.windll.gdi32.GetBitmapBits(save_bit_map, w * h * 4, bitmap_bits)
        
        img = Image.frombuffer("RGBA", (w, h), bitmap_bits, "raw", "BGRA", 0, 1)
        img = img.convert("RGB")
        
        # Cleanup
        ctypes.windll.gdi32.DeleteObject(save_bit_map)
        ctypes.windll.gdi32.DeleteDC(mfc_dc)
        ctypes.windll.user32.ReleaseDC(hwnd, hwnd_dc)
        
        return img.resize(size, Image.Resampling.LANCZOS)
    except Exception as e:
        print(f"Error capturing window thumbnail: {e}")
        return None

class SourceCard(ctinker.CTkButton):
    def __init__(self, master, name, source_type, source_id, on_click, thumbnail=None, **kwargs):
        self.source_id = source_id
        self.source_type = source_type
        self.on_click_callback = on_click
        
        icon = "🖥️" if source_type == "screen" else "🪟"
        
        clean_name = name.replace("Window: ", "").replace("Screen ", "Display ")
        display_name = clean_name if len(clean_name) <= 24 else clean_name[:21] + "..."
        
        super().__init__(
            master,
            text=f"{icon} {display_name}",
            image=thumbnail,
            compound="top",
            font=("Segoe UI", 12, "bold"),
            command=self._on_click,
            height=150,
            width=200,
            corner_radius=12,
            fg_color=COLORS["bg_card"],
            hover_color=COLORS["secondary_accent"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            border_width=1,
            **kwargs
        )
        
    def _on_click(self):
        self.on_click_callback(self.source_id, self.source_type, self)
        
    def set_selected(self, selected: bool):
        if selected:
            self.configure(
                border_width=2,
                border_color=COLORS["primary_accent"],
                fg_color=COLORS["secondary_accent"]
            )
        else:
            self.configure(
                border_width=1,
                border_color=COLORS["border"],
                fg_color=COLORS["bg_card"]
            )

class MainApp(ctinker.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("Overlayr - Select Source")
        self.geometry("850x700")
        self.configure(fg_color=COLORS["bg_primary"])
        self.resizable(True, True)
        
        # State variables
        self.source_type = "window"  # "screen" | "window"
        self.selected_source_id = None
        
        self.cached_screens = []
        self.cached_apps = []
        self.cards_cache = {"screen": {}, "window": {}}
        
        self.roi_abs = None          # (abs_x, abs_y, w, h)
        self.roi_rel = None          # (rel_x, rel_y, w, h)
        
        self.backend_process = None
        self.is_capturing = False
        self.stdout_thread = None
        
        # Initialize transparent overlay window
        self.overlay = OverlayWindow(self)
        
        # Build UI layout
        self.create_widgets()
        
        # Responsive binding
        self.bind("<Configure>", lambda e: self.on_window_resize(e) if e.widget == self else None)
        
        # Select default tab/mode and trigger list load
        self.set_window_mode()
        
        # Start auto-refresh polling loop
        self.auto_refresh_sources()
        
    def create_widgets(self):
        # Configure Grid Layout for Main Window
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=3)  # TabView gets more vertical space
        self.grid_rowconfigure(2, weight=1)  # Debug logs gets some space
        
        # 1. Header Frame (row=0)
        self.header_frame = ctinker.CTkFrame(
            self, fg_color=COLORS["bg_card"], corner_radius=12, border_color=COLORS["border"], border_width=1
        )
        self.header_frame.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 6))
        
        # Translate Label
        self.lbl_translate = ctinker.CTkLabel(
            self.header_frame, text="Translate", font=("Segoe UI", 14, "bold"), text_color=COLORS["text_primary"]
        )
        self.lbl_translate.pack(side="left", padx=(15, 5), pady=10)
        
        # Source Language OptionMenu
        self.combo_src_lang = ctinker.CTkOptionMenu(
            self.header_frame, 
            values=["English", "Japanese", "Korean", "Chinese"],
            width=110,
            height=32,
            corner_radius=8,
            fg_color=COLORS["secondary_accent"],
            button_color=COLORS["accent"],
            button_hover_color=COLORS["accent_hover"],
            text_color=COLORS["text_primary"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["secondary_accent"],
            dropdown_text_color=COLORS["text_primary"]
        )
        self.combo_src_lang.pack(side="left", padx=4, pady=10)
        self.combo_src_lang.set("English")
        
        # Arrow Separator
        self.lbl_arrow = ctinker.CTkLabel(
            self.header_frame, text="→", font=("Segoe UI", 14), text_color=COLORS["text_secondary"]
        )
        self.lbl_arrow.pack(side="left", padx=4, pady=10)
        
        # Target Language OptionMenu
        self.combo_tgt_lang = ctinker.CTkOptionMenu(
            self.header_frame,
            values=["Thai", "English"],
            width=110,
            height=32,
            corner_radius=8,
            fg_color=COLORS["secondary_accent"],
            button_color=COLORS["accent"],
            button_hover_color=COLORS["accent_hover"],
            text_color=COLORS["text_primary"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["secondary_accent"],
            dropdown_text_color=COLORS["text_primary"]
        )
        self.combo_tgt_lang.pack(side="left", padx=4, pady=10)
        self.combo_tgt_lang.set("Thai")
        
        # Grouping Mode Label
        self.lbl_mode = ctinker.CTkLabel(
            self.header_frame, text="Mode", font=("Segoe UI", 13, "bold"), text_color=COLORS["text_primary"]
        )
        self.lbl_mode.pack(side="left", padx=(20, 5), pady=10)
        
        # Grouping Mode OptionMenu
        self.combo_group_mode = ctinker.CTkOptionMenu(
            self.header_frame,
            values=["Group (Manga)", "Document (Lines)"],
            width=150,
            height=32,
            corner_radius=8,
            fg_color=COLORS["secondary_accent"],
            button_color=COLORS["accent"],
            button_hover_color=COLORS["accent_hover"],
            text_color=COLORS["text_primary"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["secondary_accent"],
            dropdown_text_color=COLORS["text_primary"]
        )
        self.combo_group_mode.pack(side="left", padx=4, pady=10)
        self.combo_group_mode.set("Group (Manga)")
        
        # 2. Main Tabs View (row=1)
        self.tab_view = ctinker.CTkTabview(
            self,
            command=self.on_tab_changed,
            fg_color=COLORS["bg_primary"],
            segmented_button_fg_color=COLORS["bg_card"],
            segmented_button_selected_color=COLORS["accent"],
            segmented_button_selected_hover_color=COLORS["accent_hover"],
            segmented_button_unselected_color=COLORS["bg_card"],
            text_color=COLORS["text_primary"]
        )
        self.tab_view.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 6))
        
        self.tab_screens = self.tab_view.add("Screens")
        self.tab_apps = self.tab_view.add("Applications")
        
        # Screens Tab Content
        self.tab_screens.configure(fg_color=COLORS["bg_primary"])
        self.scroll_screens = ctinker.CTkScrollableFrame(
            self.tab_screens, fg_color=COLORS["bg_primary"]
        )
        self.scroll_screens.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Applications Tab Content
        self.tab_apps.configure(fg_color=COLORS["bg_primary"])
        self.scroll_apps = ctinker.CTkScrollableFrame(
            self.tab_apps, fg_color=COLORS["bg_primary"]
        )
        self.scroll_apps.pack(fill="both", expand=True, padx=5, pady=5)
        
        # 3. Live Readings Debug Box (row=2)
        self.debug_frame = ctinker.CTkFrame(
            self, fg_color=COLORS["bg_card"], corner_radius=12, border_color=COLORS["border"], border_width=1
        )
        self.debug_frame.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 10))
        
        self.lbl_debug = ctinker.CTkLabel(
            self.debug_frame, text="📖 Live Readings (Debug Window)", 
            font=ctinker.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COLORS["primary_accent"]
        )
        self.lbl_debug.pack(anchor="w", padx=15, pady=(10, 2))
        
        self.txt_debug = ctinker.CTkTextbox(
            self.debug_frame, state="disabled", fg_color=COLORS["bg_primary"],
            text_color=COLORS["text_primary"], border_color=COLORS["border"], border_width=1
        )
        self.txt_debug.pack(fill="both", expand=True, padx=15, pady=(0, 12))
        
        # 4. Footer Frame (row=3)
        self.footer_frame = ctinker.CTkFrame(
            self, fg_color=COLORS["bg_card"], corner_radius=12, border_color=COLORS["border"], border_width=1
        )
        self.footer_frame.grid(row=3, column=0, sticky="ew", padx=15, pady=(0, 15))
        
        # Status Label (Left Side)
        self.lbl_status = ctinker.CTkLabel(
            self.footer_frame, text="Ready", anchor="w", 
            font=("Segoe UI", 12), text_color=COLORS["text_secondary"]
        )
        self.lbl_status.pack(side="left", padx=15, pady=10)
        
        # Define Area Button (Middle)
        self.btn_roi = ctinker.CTkButton(
            self.footer_frame, text="Define Area", command=self.define_roi, width=110, height=36, corner_radius=8,
            fg_color=COLORS["secondary_accent"], hover_color=COLORS["bg_primary"], text_color=COLORS["text_primary"]
        )
        self.btn_roi.pack(side="left", padx=5, pady=10)
        
        # Clear Area Button (Middle)
        self.btn_clear_roi = ctinker.CTkButton(
            self.footer_frame, text="Clear Area", command=self.clear_roi, width=100, height=36, corner_radius=8,
            fg_color=COLORS["error"], hover_color="#b54f49", text_color="#ffffff"
        )
        self.btn_clear_roi.pack(side="left", padx=5, pady=10)
        
        # Start Capture Button (Right Side)
        self.btn_action = ctinker.CTkButton(
            self.footer_frame, text="Start Capture", font=ctinker.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["success"], hover_color="#4da669", text_color="#ffffff",
            command=self.toggle_capture, width=140, height=36, corner_radius=8
        )
        self.btn_action.pack(side="right", padx=15, pady=10)
        
    def on_tab_changed(self):
        selected_tab = self.tab_view.get()
        if selected_tab == "Screens":
            self.source_type = "screen"
        else:
            self.source_type = "window"
        self.update_source_list()
        
    def set_window_mode(self):
        self.tab_view.set("Applications")
        self.source_type = "window"
        self.update_source_list()
        
    def set_screen_mode(self):
        self.tab_view.set("Screens")
        self.source_type = "screen"
        self.update_source_list()
        
    def update_source_list(self):
        if self.is_capturing:
            return
            
        # Spawn a background worker thread to query systems and grab thumbnails.
        # This keeps the main GUI loop completely unblocked, eliminating clicking/dragging stutter.
        threading.Thread(target=self._fetch_sources_worker, daemon=True).start()
        
    def _fetch_sources_worker(self):
        try:
            # Query self HWNDs to exclude the application's own preview cards
            my_hwnd = self.winfo_id()
            overlay_hwnd = self.overlay.top.winfo_id() if hasattr(self, 'overlay') else 0
            
            # Fetch application windows
            raw_apps = []
            def enum_callback(hwnd, extra):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title and title != self.title:
                        # Exclude self-references and any titles containing 'Overlayr'
                        if hwnd == my_hwnd or hwnd == overlay_hwnd or "Overlayr" in title:
                            return
                        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                        if not (style & win32con.WS_EX_TOOLWINDOW):
                            raw_apps.append((hwnd, f"Window: {title}"))
            win32gui.EnumWindows(enum_callback, None)
            
            # Fetch monitors
            raw_screens = []
            for i, monitor in enumerate(win32api.EnumDisplayMonitors()):
                hmonitor = monitor[0].handle
                info = win32api.GetMonitorInfo(monitor[0])
                name = f"Screen {i+1} ({info['Monitor'][2] - info['Monitor'][0]}x{info['Monitor'][3] - info['Monitor'][1]})"
                raw_screens.append((hmonitor, name))
                
            # Perform heavy thumbnail captures on the background thread
            screens_data = []
            for hmonitor, name in raw_screens:
                pil_img = get_screen_thumbnail(hmonitor)
                screens_data.append((hmonitor, name, pil_img))
                
            apps_data = []
            for hwnd, name in raw_apps:
                pil_img = get_window_thumbnail(hwnd)
                apps_data.append((hwnd, name, pil_img))
                
            # Dispatch UI updates back to the Tkinter main thread
            self.after(0, lambda: self._on_sources_fetched(screens_data, apps_data))
        except Exception as e:
            print(f"Error in background source fetch: {e}")
            
    def _on_sources_fetched(self, screens_data, apps_data):
        if self.is_capturing:
            return
            
        self.cached_screens = [(item_id, item_name) for item_id, item_name, _ in screens_data]
        self.cached_apps = [(item_id, item_name) for item_id, item_name, _ in apps_data]
        
        self.sync_grid(self.scroll_screens, screens_data, "screen")
        self.sync_grid(self.scroll_apps, apps_data, "window")
        
    def sync_grid(self, scroll_frame, items_data, source_type):
        cache = self.cards_cache[source_type]
        new_ids = {item_id for item_id, _, _ in items_data}
        
        # 1. Remove closed sources
        removed_ids = set(cache.keys()) - new_ids
        for rid in removed_ids:
            cache[rid].destroy()
            del cache[rid]
            
        # 2. Add or Update cards
        for i, (item_id, item_name, pil_img) in enumerate(items_data):
            ctk_img = None
            if pil_img:
                ctk_img = ctinker.CTkImage(light_image=pil_img, dark_image=pil_img, size=(180, 110))
                
            clean_name = item_name.replace("Window: ", "").replace("Screen ", "Display ")
            icon = "🖥️" if source_type == "screen" else "🪟"
            display_name = clean_name if len(clean_name) <= 24 else clean_name[:21] + "..."
            
            if item_id in cache:
                card = cache[item_id]
                card.configure(text=f"{icon} {display_name}")
                if ctk_img:
                    card.configure(image=ctk_img)
            else:
                card = SourceCard(
                    scroll_frame,
                    name=item_name,
                    source_type=source_type,
                    source_id=item_id,
                    on_click=self.on_source_card_clicked,
                    thumbnail=ctk_img
                )
                cache[item_id] = card
                
        # 3. Position cards
        self.regrid_sources()

    def on_source_card_clicked(self, source_id, source_type, clicked_card):
        self.selected_source_id = source_id
        self.source_type = source_type
        
        for frame in [self.scroll_screens, self.scroll_apps]:
            for widget in frame.winfo_children():
                if isinstance(widget, SourceCard):
                    widget.set_selected(widget.source_id == source_id and widget.source_type == source_type)
                    
        name_text = clicked_card.cget("text").replace("🖥️ ", "").replace("🪟 ", "")
        self.lbl_status.configure(text=f"Selected Source: {name_text}")
        
    def on_window_resize(self, event):
        if self.cached_screens or self.cached_apps:
            # Grid repositioning (regrid_sources) has zero GDI captures and runs in <1ms, resolving stuttering.
            self.regrid_sources()
            
    def regrid_sources(self):
        for source_type in ["screen", "window"]:
            scroll_frame = self.scroll_screens if source_type == "screen" else self.scroll_apps
            cache = self.cards_cache[source_type]
            
            active_cards = [cache[sid] for sid, _ in (self.cached_screens if source_type == "screen" else self.cached_apps) if sid in cache]
            if not active_cards:
                continue
                
            frame_width = scroll_frame.winfo_width()
            if frame_width <= 1:
                frame_width = 800
                
            card_width = 220
            max_cols = max(1, frame_width // card_width)
            
            for c in range(max_cols):
                scroll_frame.grid_columnconfigure(c, weight=1)
                
            for i, card in enumerate(active_cards):
                row = i // max_cols
                col = i % max_cols
                card.grid(row=row, column=col, padx=10, pady=10)
                card.set_selected(self.selected_source_id == card.source_id and self.source_type == card.source_type)
        
    def auto_refresh_sources(self):
        self.update_source_list()
        
        if not self.is_capturing:
            self.after(2000, self.auto_refresh_sources)
            
    def define_roi(self):
        if not self.selected_source_id:
            self.log_debug("Please select a target source first!")
            return
        
        self.clear_roi()
        ROISelector(self.apply_roi)
        
    def apply_roi(self, abs_x, abs_y, w, h):
        self.roi_abs = (abs_x, abs_y, w, h)
        src_x, src_y, src_w, src_h = self.get_source_bounds()
        
        rel_x = abs_x - src_x
        rel_y = abs_y - src_y
        
        self.roi_rel = (rel_x, rel_y, w, h)
        self.log_debug(f"Area defined: {int(w)}x{int(h)} at offset ({int(rel_x)},{int(rel_y)})")
        self.btn_roi.configure(text="🎯 Area Locked", fg_color="orange")
        
    def clear_roi(self):
        self.roi_abs = None
        self.roi_rel = None
        self.btn_roi.configure(text="Define Area", fg_color=COLORS["secondary_accent"])
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
        self.btn_action.configure(text="Stop Capture", fg_color=COLORS["error"], hover_color="#b54f49")
        self.lbl_status.configure(text="Status: Launching Backend...")
        
        self.txt_debug.configure(state="normal")
        self.txt_debug.delete("1.0", "end")
        self.txt_debug.configure(state="disabled")
        
        try:
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
            
            self.stdout_thread = threading.Thread(target=self.read_backend_output, daemon=True)
            self.stdout_thread.start()
            
            src_lang_code = "en" if "English" in self.combo_src_lang.get() else "ja"
            tgt_lang_code = "th" if "Thai" in self.combo_tgt_lang.get() else "en"
            
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
                    self.after(0, lambda m=msg: self.lbl_status.configure(text=f"Status: {m}", text_color=COLORS["error"]))
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
            
        for item in results:
            text = item.get("text", "")
            trans = item.get("translated", "")
            self.log_debug(f"OCR: {text} → {trans}")
            
        if results:
            src_x, src_y, src_w, src_h = self.get_source_bounds()
            if self.roi_abs:
                ox, oy, ow, oh = self.roi_abs
                self.overlay.set_geometry(ox, oy, ow, oh)
            else:
                self.overlay.set_geometry(src_x, src_y, src_w, src_h)
            self.overlay.show_results(results)
        else:
            self.overlay.hide()
            
    def stop_capture(self):
        self.is_capturing = False
        self.lbl_status.configure(text="Status: Stopping Capture...", text_color="#ffffff")
        
        self.send_backend_command({"action": "stop"})
        
        if self.backend_process:
            self.send_backend_command({"action": "exit"})
            try:
                self.backend_process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.backend_process.kill()
            self.backend_process = None
            
        self.overlay.hide()
        self.btn_action.configure(text="Start Capture", fg_color=COLORS["success"], hover_color="#4da669")
        self.lbl_status.configure(text="Status: Stopped")
        
        # Restart auto-refresh polling loop
        self.after(1000, self.auto_refresh_sources)
        
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
