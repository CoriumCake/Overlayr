import customtkinter as ctk # type: ignore
import tkinter as tk
import threading
import asyncio
import time
from typing import Optional

from ..core.engine import AsyncEngine
from ..core.capture_engine import CaptureEngine
from ..services.source_manager import SourceManager
from ..ui.overlay import ResultOverlay
from ..ui.components.source_card import SourceCard

class ROISelector(ctk.CTkToplevel):
    def __init__(self, parent, on_confirm):
        super().__init__(parent)
        self.title("Scan Area Selector")
        self.attributes("-alpha", 0.7)
        self.attributes("-topmost", True)
        self.on_confirm = on_confirm
        
        # Make it look like a frame
        self.configure(fg_color="#1f538d")
        
        self.label = ctk.CTkLabel(self, text="Move/Resize over Subtitles\nThen click Confirm", font=("Segoe UI", 12, "bold"))
        self.label.pack(expand=True, padx=20, pady=20)
        
        self.btn_confirm = ctk.CTkButton(self, text="Confirm Area", command=self.confirm, fg_color="green")
        self.btn_confirm.pack(side="bottom", pady=10)
        
        # Initial position near center
        self.geometry("600x150+400+600")

    def confirm(self):
        # Return geometry: x, y, w, h
        x = self.winfo_x()
        y = self.winfo_y()
        w = self.winfo_width()
        h = self.winfo_height()
        self.on_confirm(x, y, w, h)
        self.destroy()

class MainWindow(ctk.CTk):
    def __init__(self):
        print("Initializing MainWindow...")
        super().__init__()
        print("Super init done.")

        # Theme
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.title("Overlayr - Select Source")
        self.geometry("800x600")
        
        # Engines
        self.async_engine: Optional[AsyncEngine] = None
        self.capture_engine = None
        self.source_manager = None
        
        # State
        self.selected_source = None
        self.is_streaming = False
        self.loop = asyncio.new_event_loop()
        
        # UI Setup
        self._create_ui()
        self.overlay = ResultOverlay(self)

        # Background Threads
        threading.Thread(target=self._run_async_loop, daemon=True).start()
        
        # Initialize backend (chain: services -> sources)
        self.lbl_status.configure(text="Initializing services...")
        threading.Thread(target=self._init_services, daemon=True).start()

    def _init_services(self):
        try:
            # print("Initializing services in background...", flush=True)
            self.capture_engine = CaptureEngine()
            self.source_manager = SourceManager()
            self.async_engine = AsyncEngine()
            
            # Pass our async loop to the engine so it can schedule background tasks safely
            if hasattr(self, 'loop') and self.loop:
                self.async_engine.set_loop(self.loop)
            
            # Warmup translator in background to avoid delay later
            self.async_engine.warmup()
            
            # Now fetch sources
            self._fetch_sources_background()
            self.current_future = None
            self.after(0, lambda: self._on_backend_ready())
            
        except Exception as e:
            print(f"Service Init Error: {e}", flush=True)
            self.after(0, lambda: self.lbl_status.configure(text=f"Init Error: {e}"))

    def _on_backend_ready(self):
        self.lbl_status.configure(text="Ready")
        if self.selected_source:
            self.btn_stream.configure(state="normal")

    def _create_ui(self):
        # Layout: Header, Tabs (Screens/Apps), Grid, Footer
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1) 

        # 1. Header
        self.header_frame = ctk.CTkFrame(self)
        self.header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        
        # Language Selection UI
        ctk.CTkLabel(self.header_frame, text="Translate", font=("Roboto Medium", 16)).pack(side="left", padx=5)
        
        self.languages = {
            "Japanese": "ja",
            "English": "en", 
            "Korean": "ko",
            "Thai": "th",
            "Chinese": "zh-CN"
        }
        
        # Source
        self.var_source_lang = ctk.StringVar(value="English")
        self.opt_source_lang = ctk.CTkOptionMenu(
            self.header_frame, 
            values=list(self.languages.keys()),
            command=self._on_language_change,
            variable=self.var_source_lang,
            width=100
        )
        self.opt_source_lang.pack(side="left", padx=5)
        
        ctk.CTkLabel(self.header_frame, text="➜").pack(side="left", padx=5)
        
        # Target
        self.var_target_lang = ctk.StringVar(value="Thai")
        self.opt_target_lang = ctk.CTkOptionMenu(
            self.header_frame, 
            values=list(self.languages.keys()),
            command=self._on_language_change,
            variable=self.var_target_lang,
            width=100
        )
        self.opt_target_lang.pack(side="left", padx=5)
        
        self.switch_compact = ctk.CTkSwitch(self.header_frame, text="Compact", command=self.toggle_compact)
        self.switch_compact.pack(side="right", padx=10)
        
        # 2. Tabs
        self.tab_view = ctk.CTkTabview(self, command=self._on_tab_change)
        self.tab_view.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.tab_screens = self.tab_view.add("Screens")
        self.tab_apps = self.tab_view.add("Applications")
        
        # Scrollable Frames (Grids) for each tab
        self.scroll_screens = ctk.CTkScrollableFrame(self.tab_screens, fg_color="transparent")
        self.scroll_screens.pack(fill="both", expand=True)
        
        self.scroll_apps = ctk.CTkScrollableFrame(self.tab_apps, fg_color="transparent")
        self.scroll_apps.pack(fill="both", expand=True)

        # 3. Footer
        self.footer_frame = ctk.CTkFrame(self)
        self.footer_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.lbl_status = ctk.CTkLabel(self.footer_frame, text="Ready", anchor="w")
        self.lbl_status.pack(side="left", padx=10, pady=5)
        
        self.lbl_current_target = ctk.CTkLabel(self.footer_frame, text="Target: None", font=("Segoe UI", 10, "italic"), text_color="gray")
        self.lbl_current_target.pack(side="left", padx=10, pady=5)
        
        self.btn_define_roi = ctk.CTkButton(self.footer_frame, text="Define Area", command=self.define_roi, width=100)
        self.btn_define_roi.pack(side="right", padx=5, pady=5)
        
        self.btn_stream = ctk.CTkButton(self.footer_frame, text="Start", command=self.toggle_stream, state="disabled", fg_color="green", width=100)
        self.btn_stream.pack(side="right", padx=10, pady=5)
        


    def _on_language_change(self, choice):
        if not self.async_engine: return
        
        src_name = self.var_source_lang.get()
        tgt_name = self.var_target_lang.get()
        
        src_code = self.languages.get(src_name, 'ja')
        tgt_code = self.languages.get(tgt_name, 'en')
        
        print(f"Language changed: {src_name} -> {tgt_name}")
        self.lbl_status.configure(text=f"Switching: {src_name} -> {tgt_name}...")
        
        # Correctly schedule coroutine on the event loop
        # Note: update_languages inside AsyncEngine uses create_task, which fails if called from thread without loop.
        # But here we are on MainThread (Tkinter callback).
        # We should invoke the AsyncEngine method which spawns the task.
        # Wait, if we are on MainThread, create_task works IF the loop is running on MainThread.
        # But AsyncEngine loop is running in a separate thread (self._run_async_loop).
        
        # We need to reach into that thread's loop.
        # We stored self.loop in init.
        
        asyncio.run_coroutine_threadsafe(
            self._update_langs_async(src_code, tgt_code), 
            self.loop
        )

    async def _update_langs_async(self, src, tgt):
        # This runs inside the async loop thread
        await self.async_engine.translator_service.load_model(src, tgt)
        # Update OCR too
        # But OCR update is sync/thread-locked... ideally run in executor or just call it?
        # self.async_engine.update_languages helper does both.
        # Let's fix AsyncEngine.update_languages to be async or just call logic manually here?
        
        # Let's call the logic manually to be safe about contexts
        await self.async_engine.translator_service.load_model(src, tgt)
        
        # OCR update (sync, safe to call from here? OCREngine has locks)
        ocr_code = src
        if src == 'ja': ocr_code = 'ja'
        elif src == 'ko': ocr_code = 'ko'
        elif src.startswith('zh'): ocr_code = 'ch_sim'
        elif src == 'th': ocr_code = 'th'
        elif src == 'en': ocr_code = 'en'
        
        langs = [ocr_code]
        if 'en' not in langs: langs.append('en')
        self.async_engine.ocr_service.set_languages(langs)
        
        # Callback to UI
        self.after(0, lambda: self.lbl_status.configure(text=f"Languages Updated: {src}->{tgt}"))

    def refresh_sources(self):
        if not self.source_manager: return
        self.lbl_status.configure(text="Refreshing sources...")
        threading.Thread(target=self._fetch_sources_background, daemon=True).start()

    def _fetch_sources_background(self):
        if not self.source_manager: return
        try:
            # Heavy lifting in thread
            monitors = self.source_manager.get_monitors()
            windows = self.source_manager.get_windows()
            
            # Generate Thumbnails (Experimental)
            if self.capture_engine:
                 for m in monitors:
                     try:
                        pil_img = self.capture_engine.get_thumbnail("screen", m)
                        if pil_img:
                             m['thumbnail'] = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(150, 100))
                     except: pass
                     
                 for w in windows:
                     try:
                        pil_img = self.capture_engine.get_thumbnail("window", w)
                        if pil_img:
                             w['thumbnail'] = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(150, 100))
                     except: pass
            
            # Update UI in main loop
            self.after(0, lambda: self._update_source_grids(monitors, windows))
        except Exception as e:
            print(f"Source fetch error: {e}")
            self.after(0, lambda: self.lbl_status.configure(text="Error fetching sources"))

    def _update_source_grids(self, monitors, windows):
        # Clear existing
        for widget in self.scroll_screens.winfo_children(): widget.destroy()
        for widget in self.scroll_apps.winfo_children(): widget.destroy()
        
        self._populate_grid(self.scroll_screens, monitors)
        self._populate_grid(self.scroll_apps, windows)
        self.lbl_status.configure(text="Ready")
        
        # Re-enable start button if selection exists
        if self.selected_source and self.async_engine:
            self.btn_stream.configure(state="normal")

    def _populate_grid(self, parent, sources):
        row = 0
        col = 0
        max_cols = 3
        
        for source in sources:
            card = SourceCard(parent, source_data=source, on_click=self.on_source_select)
            card.grid(row=row, column=col, padx=10, pady=10)
            
            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    def on_source_select(self, source):
        self.selected_source = source
        name = source.get('name') or source.get('title') or "Unknown Source"
        self.lbl_status.configure(text=f"Selected: {name}")
        self.lbl_current_target.configure(text=f"Targeting: {name}", text_color="white")

        
        # If engine is ready, enable button
        if self.async_engine:
             self.btn_stream.configure(state="normal")
        else:
             self.btn_stream.configure(state="disabled") # Keep disabled until ready

    def _on_tab_change(self):
        self.refresh_sources()

    def _init_backend(self):
        try:
            self.async_engine = AsyncEngine()
            self.after(0, lambda: self.lbl_status.configure(text="Engine Ready"))
            if self.selected_source:
                 self.after(0, lambda: self.btn_stream.configure(state="normal"))
        except Exception as e:
            print(f"Init Error: {e}")

    def _run_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def toggle_stream(self):
        if not self.is_streaming:
            # Start
            if not self.selected_source: return
            
            self.is_streaming = True
            self.btn_stream.configure(text="Stop", fg_color="red")
            
            # Configure capture engine
            mode = self.selected_source.get('type')
            self.capture_engine.set_source(mode, self.selected_source)
            
            # Start loop
            threading.Thread(target=self.stream_loop, daemon=True).start()
        else:
            # Stop
            self.is_streaming = False
            self.btn_stream.configure(text="Start", fg_color="green")
            self.overlay.hide()



    def stream_loop(self):
        print("Stream loop started.", flush=True)
        pending_future = None
        last_bounds = None
        
        while self.is_streaming:
            start_time = time.time()
            try:
                # Capture - now returns (image, bounds)
                result = self.capture_engine.capture()
                image, bounds = result if result else (None, None)
                
                if image and bounds:
                    last_bounds = bounds
                    
                    # Focus Check: Only show overlay if target is active
                    is_active = True
                    try:
                        import pygetwindow as gw
                        active_win = gw.getActiveWindow()
                        if self.selected_source.get('type') == 'window':
                            target_hwnd = self.selected_source.get('id')
                            if active_win is None or getattr(active_win, '_hWnd', None) != target_hwnd:
                                is_active = False
                    except: pass

                    if not is_active:
                        self.after(0, self.overlay.hide)
                        # We still loop but skip heavy processing
                        time.sleep(0.1)
                        continue
                    
                    # Check if previous processing is done
                    if pending_future is not None:
                        if pending_future.done():
                            pending_future = None
                    
                    # Only start new processing if not already processing
                    if pending_future is None:
                        pending_future = asyncio.run_coroutine_threadsafe(
                            self.process_image(image, bounds), 
                            self.loop
                        )
                    else:
                        # Just update overlay geometry to track window
                        self.after(0, lambda b=bounds: self.overlay.set_geometry(
                            f"{b['width']}x{b['height']}+{b['left']}+{b['top']}"
                        ))
                
            except Exception as e:
                print(f"Stream error: {e}")
            
            # Target ~30fps (33ms per frame)
            elapsed = time.time() - start_time
            sleep_time = max(0, 0.033 - elapsed)
            time.sleep(sleep_time)
        
        # Ensure cleanup on exit
        self.after(0, self.overlay.hide)
        print("Stream loop stopped.", flush=True)


    async def process_image(self, image, bounds):
        if not self.async_engine: return
        try:
            results, raw_text = await self.async_engine.process_image(image)
            
            # Use bounds directly from capture (always current)
            offset_x = bounds['left']
            offset_y = bounds['top']
            geometry = f"{bounds['width']}x{bounds['height']}+{bounds['left']}+{bounds['top']}"

            if self.is_streaming:
                self.after(0, lambda: self._update_overlay_safe(results, raw_text, offset_x, offset_y, geometry))

        except Exception as e:
            print(f"Proc Error: {e}")

    def _update_overlay_safe(self, results, raw_text, x, y, geometry):
        if not self.is_streaming:
            self.overlay.hide()
            return
            
        if geometry:
            self.overlay.set_geometry(geometry)
        self.overlay.show_results(results, x, y)
        self.overlay.show_debug(raw_text)
            
    def toggle_compact(self):
        if self.switch_compact.get():
             self.geometry("400x100")
             self.tab_view.grid_remove()
             self.header_frame.grid_remove()
        else:
             self.geometry("800x600")
             self.tab_view.grid()
             self.header_frame.grid()

    def define_roi(self):
        if not self.selected_source: 
            self.lbl_status.configure(text="Select source first!", text_color="red")
            return
            
        # If already has ROI, clear it on first click? 
        # Or just open selector
        selector = ROISelector(self, self.apply_roi)
        
    def apply_roi(self, x, y, w, h):
        # Calculate ratios relative to target screen/window
        if not self.selected_source: return
        
        target = self.selected_source
        if target['type'] == 'screen':
            # Relative to monitor
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[target['id'] + 1]
                rx = (x - mon['left']) / mon['width']
                ry = (y - mon['top']) / mon['height']
                rw = w / mon['width']
                rh = h / mon['height']
        else:
            # Relative to window
            win = target['window_obj']
            # Re-fetch for accuracy
            rx = (x - win.left) / win.width
            ry = (y - win.top) / win.height
            rw = w / win.width
            rh = h / win.height
            
        # Clamp ratios
        rx = max(0, min(1, rx))
        ry = max(0, min(1, ry))
        rw = max(0, min(1-rx, rw))
        rh = max(0, min(1-ry, rh))
        
        self.capture_engine.set_roi((rx, ry, rw, rh))
        self.lbl_status.configure(text="Area Locked", text_color="cyan")
        self.btn_define_roi.configure(text="Clear Area", command=self.clear_roi, fg_color="orange")

    def clear_roi(self):
        self.capture_engine.set_roi(None)
        self.lbl_status.configure(text="Area Cleared", text_color="white")
        self.btn_define_roi.configure(text="Define Area", command=self.define_roi, fg_color=["#3B8ED0", "#1F6AA5"])

if __name__ == "__main__":
    app = MainWindow()
    app.mainloop()
