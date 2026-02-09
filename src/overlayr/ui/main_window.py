import customtkinter as ctk # type: ignore
import tkinter as tk
import os
import threading
import asyncio
import time
from typing import Optional

from ..core.engine import AsyncEngine
from ..core.capture_engine import CaptureEngine
from ..services.source_manager import SourceManager
from ..ui.overlay import ResultOverlay
from ..ui.components.source_card import SourceCard
from ..ui.components.profile_menu import ProfileMenu

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
        # Status Bar

        
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
    def __init__(self, auth_service=None):
        print("Initializing MainWindow with Auth...")
        super().__init__()
        print("Super init done.")

        # Theme
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.title("Overlayr - Select Source")
        self.geometry("800x600")
        
        # Engines
        self.auth_service = auth_service
        self.async_engine: Optional[AsyncEngine] = None
        self.capture_engine = None
        self.source_manager = None
        
        # Bind resize for responsive grid
        self.bind("<Configure>", lambda e: self.refresh_sources(silent=True) if e.widget == self else None)
        
        # State
        self.selected_source = None
        self.is_streaming = False
        self.is_streaming = False
        self.loop = asyncio.new_event_loop()
        self.current_merge_mode = "paragraph" # Default to Manga mode
        
        # UI Cache to prevent flickering
        self.cards_cache = {"screens": {}, "apps": {}}
        
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
            
            # Connect status callback
            self.async_engine.set_status_callback(lambda msg: self.after(0, lambda: self.lbl_status.configure(text=msg)))
            
            # Warmup translator in background to avoid delay later
            self.async_engine.warmup()
            
            # Now fetch sources
            self._fetch_sources_background()
            self.current_future = None
            self.after(0, lambda: self._on_backend_ready())
            
        except Exception as e:
            print(f"Service Init Error: {e}", flush=True)
            self.after(0, lambda err=e: self.lbl_status.configure(text=f"Init Error: {err}"))

    def _on_backend_ready(self):
        self.lbl_status.configure(text="Ready")
        if self.selected_source:
            self.btn_stream.configure(state="normal")
        
        # Start auto-refresh polling once backend is ready
        self._schedule_auto_refresh()

    def _create_ui(self):
        # Layout: Header, Tabs (Screens/Apps), Grid, Footer
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1) 

        # 1. Header
        self.header_frame = ctk.CTkFrame(self)
        self.header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        # Login / Status Button (Right Side)

        
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
        
        # Grouping Mode
        ctk.CTkLabel(self.header_frame, text="Mode", font=("Roboto Medium", 16)).pack(side="left", padx=(20, 5))
        self.var_grouping_mode = ctk.StringVar(value="Group (Manga)")
        self.opt_grouping_mode = ctk.CTkOptionMenu(
            self.header_frame,
            values=["Group (Manga)", "Document (Lines)"],
            variable=self.var_grouping_mode,
            command=self._update_merge_mode,
            width=140
        )
        self.opt_grouping_mode.pack(side="left", padx=5)
        
        
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
        
        self.btn_define_roi = ctk.CTkButton(self.footer_frame, text="Define Area", command=self.define_roi, width=100)
        self.btn_define_roi.pack(side="right", padx=5, pady=5)
        
        self.btn_stream = ctk.CTkButton(self.footer_frame, text="Start", command=self.toggle_stream, state="disabled", fg_color="green", width=100)
        self.btn_stream.pack(side="right", padx=10, pady=5)
        
        # 4. Auth Check
        self._update_auth_ui()

    def login_flow(self):
        """Opens the login page in browser."""
        import webbrowser
        # Using a fixed URL for now, will match the Astro page
        login_url = "https://overlayr.catcode.tech/login?callback=desktop"
        webbrowser.open(login_url)
        self.lbl_status.configure(text="Waiting for login...", text_color="yellow")
        self.start_login_polling()

    def start_login_polling(self):
        self.polling_start_time = time.time()
        self._check_login_status()

    def _check_login_status(self):
        if not self.auth_service: return

        # Reload session from file
        self.auth_service.load_session()
        
        info = self.auth_service.get_user_info()
        if info['logged_in']:
            self._update_auth_ui()
            self.lbl_status.configure(text="Login Successful!", text_color="green")
            # Stop polling
            return

        # Timeout after 5 minutes
        if time.time() - self.polling_start_time > 300:
            self.lbl_status.configure(text="Login timed out.", text_color="red")
            return

        # Continue polling
        self.after(1500, self._check_login_status)

    def _update_auth_ui(self):
        if not self.auth_service: return
        
        # Clear existing button if any
        if hasattr(self, 'btn_profile'):
            self.btn_profile.destroy()
            
        # Get Status
        user_info = self.auth_service.get_user_info()
        print(f"[DEBUG] _update_auth_ui: {user_info}")
        is_logged_in = user_info['logged_in']
        is_pro = user_info['pro']
        is_logged_in = user_info['logged_in']
        is_pro = user_info['pro']
        photo_url = user_info.get('photo_url')
        
        # Load Icon
        self.profile_icon = None
        self.profile_icon_large = None # For menu
        
        # 1. Try Online Image
        if photo_url:
            try:
                import requests
                from PIL import Image, ImageDraw
                from io import BytesIO
                
                # Check cache
                if not hasattr(self, '_user_photo_cache') or getattr(self, '_user_photo_cache_url', None) != photo_url:
                     try:
                        resp = requests.get(photo_url, timeout=2)
                        if resp.status_code == 200:
                            img_data = BytesIO(resp.content)
                            pil_img = Image.open(img_data).convert("RGBA")
                            
                            # Keep high res original
                            # Resize for button (Use 64x64 for 2x pixel density on 32pt button)
                            pil_btn = pil_img.resize((64, 64), Image.Resampling.LANCZOS)
                            
                            # Circular Mask for Button
                            mask = Image.new('L', (64, 64), 0)
                            draw = ImageDraw.Draw(mask)
                            draw.ellipse((0, 0, 64, 64), fill=255)
                            pil_btn.putalpha(mask)
                            
                            # For Menu (Large, 128x128)
                            pil_large = pil_img.resize((128, 128), Image.Resampling.LANCZOS)
                            # Circular Mask for Menu
                            mask_l = Image.new('L', (128, 128), 0)
                            draw_l = ImageDraw.Draw(mask_l)
                            draw_l.ellipse((0, 0, 128, 128), fill=255)
                            pil_large.putalpha(mask_l)
                            
                            self._user_photo_cache = pil_btn
                            self._user_photo_cache_large = pil_large
                            self._user_photo_cache_url = photo_url
                     except Exception as e:
                        print(f"Failed to download profile image: {e}")
                
                if hasattr(self, '_user_photo_cache'):
                    self.profile_icon = ctk.CTkImage(light_image=self._user_photo_cache, dark_image=self._user_photo_cache, size=(32, 32))
                    self.profile_icon_large = ctk.CTkImage(light_image=self._user_photo_cache_large, dark_image=self._user_photo_cache_large, size=(64, 64))

            except Exception as e:
                print(f"Profile Image Error: {e}")

        # 2. Try Local Placeholder
        if not self.profile_icon:
            try:
                from PIL import Image
                icon_path = os.path.join(os.getcwd(), "assets", "user_placeholder.png")
                if os.path.exists(icon_path):
                     pil_img = Image.open(icon_path)
                     self.profile_icon = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(32, 32))
            except: pass
            
        # 3. Last Resort: Generated Icon
        if not self.profile_icon:
            try:
                from PIL import Image, ImageDraw
                # Create a simple colored circle with a letter
                # Color based on pro status
                color = "#FFD700" if is_pro else "#808080" # Gold or Gray
                img = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.ellipse((0, 0, 32, 32), fill=color)
                
                # Add "U" or "P"
                # draw.text((10, 8), "P" if is_pro else "U", fill="white") # Simple text
                
                self.profile_icon = ctk.CTkImage(light_image=img, dark_image=img, size=(32, 32))
            except Exception as e:
                 print(f"Fallback Icon Error: {e}")

        # Create Button (Icon Only)
        self.btn_profile = ctk.CTkButton(
            self.header_frame, 
            text="", 
            image=self.profile_icon, 
            width=40, 
            height=40,
            fg_color="transparent", 
            hover_color=("gray70", "gray30"),
            command=self._on_profile_click
        )
        self.btn_profile.pack(side="right", padx=5)

        # Update Title
        if is_pro:
            self.title("Overlayr - Pro")
        else:
             status, days = self.auth_service.get_subscription_status()
             if status == 'trial':
                 self.title(f"Overlayr - Trial ({days} days left)")
             else:
                 self.title("Overlayr - Free/Expired")

    def _on_settings_click(self):
        print("Settings clicked")

    def _on_profile_click(self):
        # 1. Toggle Check
        # If menu exists and is mapped (visible), hide/destroy it
        if hasattr(self, 'profile_menu') and self.profile_menu.winfo_exists():
             self.profile_menu.destroy()
             del self.profile_menu
             return

        user_info = self.auth_service.get_user_info()
        
        if not user_info['logged_in']:
            # Simple login action if not logged in
            self.login_flow()
            return

        # Show Custom Menu
        # Attach large icon to info
        if self.profile_icon_large:
            user_info['large_icon'] = self.profile_icon_large
        else:
            # Fallback for menu
            user_info['large_icon'] = self.profile_icon 
            
        # Create Menu (Frame inside MainWindow)
        try:
            # We need relative coordinates of the button within MainWindow
            # btn_profile is in header_frame, which is in self
            
            # x = window_x_of_button + width - menu_width
            # But header_frame might have padding
            
            # Easiest way: getting absolute root coordinates and subtracting window root coordinates?
            # No, winfo_x/y is relative to parent.
            
            # button -> header_frame -> MainWindow
            # x = self.header_frame.winfo_x() + self.btn_profile.winfo_x()
            # y = self.header_frame.winfo_y() + self.btn_profile.winfo_y() + button_height
            
            # Ensure geometry is up to date
            self.update_idletasks()
            
            root_x = self.winfo_rootx()
            root_y = self.winfo_rooty()
            
            btn_root_x = self.btn_profile.winfo_rootx()
            btn_root_y = self.btn_profile.winfo_rooty()
            
            btn_x = btn_root_x - root_x
            btn_y = btn_root_y - root_y
            btn_h = self.btn_profile.winfo_height()
            btn_w = self.btn_profile.winfo_width()
            
            menu_w = 220 # matched with ProfileMenu width
            
            # Position: Right-aligned with button
            target_x = btn_x + btn_w - menu_w
            target_y = btn_y + btn_h + 5
            
            # Bounds check (Left)
            if target_x < 10: target_x = 10
            
            # Bounds check (Right - strict clip to window)
            window_w = self.winfo_width()
            if target_x + menu_w > window_w - 10:
                target_x = window_w - menu_w - 10
            
            # Instantiate
            self.profile_menu = ProfileMenu(self, user_info, self.logout_flow, self._on_settings_click)
            
            # Bring to front
            self.profile_menu.lift()
            
            # Show (Static)
            self.profile_menu.show(target_x, target_y)
            
            # Bind global click to dismiss?
            # For now, simplistic approach: bind click on main window to check? 
            # Or just let user click the button again to toggle (handled above).
            
        except Exception as e:
            print(f"Menu Error: {e}")

    def logout_flow(self):
        if self.auth_service:
            self.auth_service.logout()
            self._update_auth_ui()
            self.lbl_status.configure(text="Logged out")


    def _update_merge_mode(self, choice):
        if "Document" in choice:
            self.current_merge_mode = "line"
        else:
            self.current_merge_mode = "paragraph"
        print(f"Merge mode detected: {self.current_merge_mode}")

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

    def refresh_sources(self, silent=False):
        if not self.source_manager: return
        if not silent:
            self.lbl_status.configure(text="Refreshing sources...")
        threading.Thread(target=self._fetch_sources_background, args=(silent,), daemon=True).start()

    def _fetch_sources_background(self, silent=False):
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
            self.after(0, lambda: self._update_source_grids(monitors, windows, silent))
        except Exception as e:
            print(f"Source fetch error: {e}")
            if not silent:
                self.after(0, lambda: self.lbl_status.configure(text="Error fetching sources"))

    def _update_source_grids(self, monitors, windows, silent=False):
        # Synchronize grids instead of rebuilding
        self._sync_grid(self.scroll_screens, monitors, "screens")
        self._sync_grid(self.scroll_apps, windows, "apps")
        
        if not silent:
            self.lbl_status.configure(text="Ready")
        
        # Re-enable start button if selection exists
        if self.selected_source and self.async_engine:
            self.btn_stream.configure(state="normal")

    def _sync_grid(self, parent, new_sources, cache_key):
        """Diff & Sync algorithm to update source cards with responsive layout."""
        cache = self.cards_cache[cache_key]
        new_ids = {s['id'] for s in new_sources}
        
        # 1. Remove old cards
        removed_ids = set(cache.keys()) - new_ids
        for rid in removed_ids:
            cache[rid].destroy()
            del cache[rid]
            
        # 2. Add or Update cards
        for i, source in enumerate(new_sources):
            sid = source['id']
            if sid in cache:
                title = source.get('name') or source.get('title')
                truncated_title = title[:15] + "..." if len(title) > 18 else title
                cache[sid].configure(
                    text=f"{truncated_title}",
                    image=source.get('thumbnail')
                )
            else:
                card = SourceCard(parent, source_data=source, on_click=self.on_source_select)
                cache[sid] = card
        
        # 3. Responsive Positioning
        # Card width is 200, plus padding (10 each side) = 220 total
        # Parent width - scrollbar (~20)
        parent_width = parent.winfo_width()
        if parent_width <= 1: parent_width = 750 # Fallback for initial draw
        
        max_cols = max(1, parent_width // 220)
        
        for i, source in enumerate(new_sources):
            sid = source['id']
            if sid in cache:
                row = i // max_cols
                col = i % max_cols
                cache[sid].grid(row=row, column=col, padx=10, pady=10)
                
        # Configure columns to expand
        for c in range(max_cols):
            parent.grid_columnconfigure(c, weight=1)

    def on_source_select(self, source):
        self.selected_source = source
        friendly_name = source.get('friendly_name') or source.get('name') or source.get('title') or "Unknown Source"
        
        self.lbl_status.configure(text=f"Selected: {friendly_name}")

        
        # If engine is ready, enable button
        if self.async_engine:
             self.btn_stream.configure(state="normal")
        else:
             self.btn_stream.configure(state="disabled") # Keep disabled until ready

    def _on_tab_change(self):
        self.refresh_sources()

    def _schedule_auto_refresh(self):
        """Periodically refresh sources if not streaming."""
        interval_ms = 1000 # 1 second for "instant" feel
        
        if not self.is_streaming and self.source_manager:
            if self.source_manager.has_window_list_changed():
                # Perform a silent refresh to avoid flickering status bar
                self.refresh_sources(silent=True)
            
        self.after(interval_ms, self._schedule_auto_refresh)

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
            # Check if previous processing is active
            if pending_future is not None:
                if not pending_future.done():
                    # Processing is busy (e.g. waiting 5s display). 
                    # Do not capture/hide. Just track window position.
                    if last_bounds:
                         self.after(0, lambda b=last_bounds: self.overlay.set_geometry(
                             f"{b['width']}x{b['height']}+{b['left']}+{b['top']}"
                         ))
                    time.sleep(0.05)
                    continue
                else:
                    pending_future = None

            start_time = time.time()
            try:
                # Optimized Capture:
                # We use SetWindowDisplayAffinity in overlay.py to make it invisible to capture.
                # So we NO LONGER need to hide/restore it manually.
                
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
                        time.sleep(0.1)
                        continue
                    
                    # Start new processing
                    pending_future = asyncio.run_coroutine_threadsafe(
                        self.process_image(image, bounds), 
                        self.loop
                    )


                
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
        
        # Define callback to handle "Slow Motion" steps inside the engine
        async def step_callback(stage, data):
            if stage == "OCR":
                # Step 2: OCR Done (Windows OCR) -> No Delay
                # print("DEBUG: Step 2 - OCR Done. Waiting 2s...", flush=True)
                raw_text = f"Found {len(data)} text blocks"
                self.after(0, lambda: self.overlay.show_debug(f"STEP 2: OCR Done ({raw_text})"))
                # await asyncio.sleep(2)
                
            elif stage == "MERGED":
                # Step 3: Merge Text Blocks (Show Blue Boxes) -> NO DELAY
                # print("DEBUG: Step 3 - Merged. Showing boxes. Waiting 2s...", flush=True)
                
                # Extract boxes for display
                boxes = [item['bbox'] for item in data]
                offset_x = bounds['left']
                offset_y = bounds['top']
                geometry = f"{bounds['width']}x{bounds['height']}+{bounds['left']}+{bounds['top']}"
                
                self.after(0, lambda: self.overlay.set_geometry(geometry))
                # self.after(0, lambda: self.overlay.show_boxes(boxes, offset_x, offset_y, color="#00ffff")) # Cyan/Blue
                # self.after(0, lambda: self.overlay.show_debug("STEP 3: Text Merged (Blue Boxes)"))
                
                # Flow: Merge -> Immediate
                # await asyncio.sleep(2)
                
            elif stage == "TRANSLATED":
                pass # Skip delay here, we move straight to display

        try:
            # Pass the callback to the engine, skip translation enabled (Original Text)
            # Use current_merge_mode (thread-safe string)
            results, raw_text = await self.async_engine.process_image(
                image, 
                step_callback=step_callback, 
                skip_translation=False,
                merge_mode=self.current_merge_mode
            )
            
            # Step 5: Draw Overlay Lines (Display Translated Result) -> Delay
            
            # print("DEBUG: Step 5 - Draw Overlay. Waiting...", flush=True)
            self.after(0, lambda: self.overlay.show_debug("STEP 5: Result (Translated)"))
            
            # Use bounds directly from capture (always current)
            offset_x = bounds['left']
            offset_y = bounds['top']
            geometry = f"{bounds['width']}x{bounds['height']}+{bounds['left']}+{bounds['top']}"

            if self.is_streaming:
                self.after(0, lambda: self._update_overlay_safe(results, raw_text, offset_x, offset_y, geometry))
                
            # Wait 0 seconds (Immediate Yield) for max performance
            await asyncio.sleep(0)
            
            # Request: Blue frame AND Text disappear after 5s delay
            # User Change (1621): Keep overlay until next result replaces it.
            # self.after(0, self.overlay.hide)

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
