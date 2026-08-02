import customtkinter as ctk # type: ignore
import tkinter as tk
import os
import threading
import asyncio
import time
import queue
from typing import Optional

from ..core.engine import AsyncEngine
from ..core.capture_engine import CaptureEngine
from ..services.source_manager import SourceManager
from ..ui.overlay import ResultOverlay
from ..ui.components.source_card import SourceCard
from ..ui.components.loading_spinner import LoadingSpinner
# from ..ui.components.profile_menu import ProfileMenu
from .theme import get_theme, apply_theme_to_window
from ..utils.image_utils import calculate_similarity

class ROISelector(ctk.CTkToplevel):
    def __init__(self, parent, on_confirm):
        super().__init__(parent)
        self.title("Scan Area Selector")
        self.attributes("-alpha", 0.7)
        self.attributes("-topmost", True)
        self.on_confirm = on_confirm
        
        # Make it look like a frame
        colors = get_theme()
        self.configure(fg_color=colors["secondary_accent"])
        
        self.label = ctk.CTkLabel(
            self, 
            text="Move/Resize over Subtitles\nThen click Confirm", 
            font=("Segoe UI", 12, "bold"),
            text_color=colors["text_primary"]
        )
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
    def __init__(self, auth_service=None, config_service=None):
        print("Initializing MainWindow with Auth...")
        # Use tuple color for atomic theme switching
        from .theme import get_theme, get_theme_tuple
        super().__init__(fg_color=get_theme_tuple("bg_primary"))
        print("Super init done.")

        self.auth_service = auth_service
        self.config_service = config_service

        # Theme
        initial_mode = self.config_service.get("appearance_mode", "Dark") if self.config_service else "Dark"
        ctk.set_appearance_mode(initial_mode)
        ctk.set_default_color_theme("blue")
        apply_theme_to_window(self)

        self.title("Overlayr - Select Source")
        self.geometry("800x600")
        self.resizable(True, True)
        
        def force_startup_size():
            # More aggressive resize
            
            if self.state() == 'zoomed' or self.winfo_width() > 1000:
                self.state('normal')
                self.withdraw() # Flicker protection / force refresh
                self.geometry("800x600")
                self.update_idletasks()
                self.deiconify()
            else:
                self.geometry("800x600")

        # Ensure not maximized on startup - increase delay to 100ms to override any auto-restore
        self.after(200, force_startup_size)
        
        # auth_service and config_service are already set above
        self.async_engine: Optional[AsyncEngine] = None
        self.capture_engine = None
        self.source_manager = None
        
        # Bind resize for responsive grid
        self.bind("<Configure>", lambda e: self._on_resize(e) if e.widget == self else None)
        
        # State
        self.cached_monitors = []
        self.cached_windows = []
        self.selected_source = None
        self.is_streaming = False
        self.is_streaming = False
        self.loop = asyncio.new_event_loop()
        self.current_merge_mode = "paragraph" # Default to Manga mode
        
        # UI Cache to prevent flickering
        self.cards_cache = {"screens": {}, "apps": {}}
        
        # Thread-safe status message queue
        self._status_queue = queue.Queue()
        self._poll_status_messages()
        
        # UI Setup
        self._load_assets()
        self._create_ui()
        self.overlay = ResultOverlay(self)

        # Background Threads
        threading.Thread(target=self._run_async_loop, daemon=True).start()
        
        # Initialize backend (chain: services -> sources)
        self.lbl_status.configure(text="Initializing services...")
        threading.Thread(target=self._init_services, daemon=True).start()

    def _load_assets(self):
        """Load static assets like icons with proper light/dark variants"""
        try:
            from PIL import Image, ImageOps
            asset_path = os.path.join(os.getcwd(), "assets", "settings.png")
            if os.path.exists(asset_path):
                pil_img = Image.open(asset_path).convert("RGBA")
                
                # Create inverted version for dark mode
                r, g, b, a = pil_img.split()
                rgb_img = Image.merge('RGB', (r, g, b))
                inverted_rgb = ImageOps.invert(rgb_img)
                r2, g2, b2 = inverted_rgb.split()
                inverted_img = Image.merge('RGBA', (r2, g2, b2, a))
                
                # Fix #4: Provide separate images for light/dark modes
                # light_image = original dark gear (visible on light bg)
                # dark_image = inverted white gear (visible on dark bg)
                self.settings_icon = ctk.CTkImage(
                    light_image=pil_img,
                    dark_image=inverted_img,
                    size=(24, 24)
                )
            else:
                self.settings_icon = None
        except Exception as e:
            print(f"Error loading settings icon: {e}")
            self.settings_icon = None

    def _init_services(self):
        try:
            # print("Initializing services in background...", flush=True)
            self.capture_engine = CaptureEngine()
            self.source_manager = SourceManager()
            self.async_engine = AsyncEngine()
            
            # Pass our async loop to the engine so it can schedule background tasks safely
            if hasattr(self, 'loop') and self.loop:
                self.async_engine.set_loop(self.loop)
            
            # Connect status callback (thread-safe via queue)
            self.async_engine.set_status_callback(self._thread_safe_status)
            
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
            self.btn_stream.configure(state="normal", fg_color="green")
        
        # Start auto-refresh polling once backend is ready
        self._schedule_auto_refresh()
    
    def _thread_safe_status(self, msg):
        """Thread-safe: push status message to queue (can be called from any thread)"""
        try:
            self._status_queue.put_nowait(msg)
        except:
            pass
    
    def _poll_status_messages(self):
        """Main thread: poll queue and update UI"""
        try:
            while True:
                msg = self._status_queue.get_nowait()
                self.lbl_status.configure(text=msg)
        except queue.Empty:
            pass
        except:
            pass
        # Schedule next poll
        self.after(100, self._poll_status_messages)
    
    def _hide_scrollbar(self, scrollable_frame):
        """Hide the scrollbar of a CTkScrollableFrame"""
        try:
            # Access the internal scrollbar widget
            if hasattr(scrollable_frame, '_scrollbar'):
                scrollable_frame._scrollbar.grid_remove()
        except:
            pass
    
    def _show_scrollbar(self, scrollable_frame):
        """Show the scrollbar of a CTkScrollableFrame"""
        try:
            if hasattr(scrollable_frame, '_scrollbar'):
                scrollable_frame._scrollbar.grid()
        except:
            pass
    
    def _update_scrollbar_visibility(self, scrollable_frame):
        """Show scrollbar only if content overflows (using yview fraction)"""
        try:
            scrollable_frame.update_idletasks()
            
            # Use yview() to check if the view covers the entire range
            # yview returns (0.0, 1.0) if all content is visible
            y_fraction = scrollable_frame._parent_canvas.yview()
            
            # If the fraction is (0.0, 1.0), it means no scroll is needed
            if y_fraction[0] <= 0.0 and y_fraction[1] >= 1.0:
                self._hide_scrollbar(scrollable_frame)
            else:
                self._show_scrollbar(scrollable_frame)
        except Exception as e:
            # Fallback to height check if canvas check fails
            try:
                content_height = scrollable_frame._parent_frame.winfo_reqheight()
                visible_height = scrollable_frame.winfo_height()
                if content_height > visible_height:
                    self._show_scrollbar(scrollable_frame)
                else:
                    self._hide_scrollbar(scrollable_frame)
            except:
                pass

    def refresh_theme(self):
        """Update all UI components to match the current theme"""
        colors = get_theme()
        apply_theme_to_window(self)
        
        # Update Header (elevated surface)
        self.header_frame.configure(fg_color=colors["bg_card"])
        for child in self.header_frame.winfo_children():
            if isinstance(child, ctk.CTkLabel):
                child.configure(text_color=colors["text_primary"])
            elif isinstance(child, ctk.CTkOptionMenu):
                child.configure(
                    fg_color=colors["secondary_accent"],
                    button_color=colors["accent"],
                    button_hover_color=colors["accent_hover"],
                    text_color=colors["text_primary"],
                    dropdown_fg_color=colors["bg_card"],
                    dropdown_hover_color=colors["bg_card_hover"],
                    dropdown_text_color=colors["text_primary"]
                )
            elif isinstance(child, ctk.CTkButton):
                child.configure(
                    fg_color="transparent",
                    hover_color=colors["bg_hover"],
                    text_color=colors["text_primary"]
                )
        
        # Update Tabs
        self.tab_view.configure(
            fg_color=colors["bg_primary"],
            segmented_button_fg_color=colors["bg_secondary"],
            segmented_button_selected_color=colors["accent"],
            segmented_button_selected_hover_color=colors["accent_hover"],
            segmented_button_unselected_color=colors["bg_secondary"],
            segmented_button_unselected_hover_color=colors["bg_hover"],
            text_color=colors["text_primary"]
        )
        # Update tab content frame backgrounds
        # Note: Tabview doesn't support tuple colors well yet, so we keep manual update here for now
        self.tab_screens.configure(fg_color=colors["bg_primary"])
        self.tab_apps.configure(fg_color=colors["bg_primary"])
        # Update scrollable frames and main content wrapper
        # Main content uses tuple now, but we can re-confirm
        # self.main_content.configure(fg_color=colors["bg_primary"]) 
        self.scroll_screens.configure(fg_color=colors["bg_primary"])
        self.scroll_apps.configure(fg_color=colors["bg_primary"])
        
        # Update Footer (elevated surface)
        self.footer_frame.configure(fg_color=colors["bg_card"])
        self.lbl_status.configure(text_color=colors["text_secondary"])
        self.btn_define_roi.configure(
            fg_color=colors["secondary_accent"],
            hover_color=colors["bg_hover"],
            text_color=colors["text_primary"]
        )
        # Only update start button if not streaming
        if not self.is_streaming:
            self.btn_stream.configure(
                fg_color=colors["success"] if self.selected_source else "gray",
                text_color="white"
            )
            
        # Update cached cards if any exist
        def update_cards(grid_frame):
            for card in grid_frame.winfo_children():
                if isinstance(card, SourceCard):
                    card.refresh_colors(colors) # Update text/hover/etc
                    card.set_selected(self.selected_source == card.source_data)
        
        update_cards(self.scroll_screens)
        update_cards(self.scroll_apps)
        
        # Reload icons if needed (gear icon might need different inversion depending on light/dark)
        self._load_assets()
        if hasattr(self, 'btn_settings'):
            self.btn_settings.configure(image=getattr(self, 'settings_icon', None))

        self.update_idletasks()

    def _create_ui(self):
        # Layout: Header, Tabs (Screens/Apps), Grid, Footer
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1) 

        # 1. Header — elevated surface
        colors = get_theme()
        self.header_frame = ctk.CTkFrame(self, fg_color=colors["bg_card"], corner_radius=12)
        self.header_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        # Login / Status Button (Right Side)

        
        # Language Selection UI
        ctk.CTkLabel(self.header_frame, text="Translate", font=("Segoe UI", 14, "bold"), text_color=colors["text_primary"]).pack(side="left", padx=(12, 5))
        
        self.languages = {
            "Japanese": "ja",
            "English": "en", 
            "Korean": "ko",
            "Thai": "th",
            "Chinese": "zh-CN"
        }
        
        # Source
        self.var_source_lang = ctk.StringVar(value=self.config_service.get("source_lang", "English") if self.config_service else "English")
        self.opt_source_lang = ctk.CTkOptionMenu(
            self.header_frame, 
            values=list(self.languages.keys()),
            command=self._on_language_change,
            variable=self.var_source_lang,
            width=100,
            height=32,
            corner_radius=8,
            fg_color=colors["secondary_accent"],
            button_color=colors["accent"],
            button_hover_color=colors["accent_hover"],
            text_color=colors["text_primary"],
            dropdown_fg_color=colors["bg_card"],
            dropdown_hover_color=colors["bg_card_hover"],
            dropdown_text_color=colors["text_primary"]
        )
        self.opt_source_lang.pack(side="left", padx=4)
        
        ctk.CTkLabel(self.header_frame, text="→", font=("Segoe UI", 14), text_color=colors["text_secondary"]).pack(side="left", padx=4)
        
        # Target
        self.var_target_lang = ctk.StringVar(value=self.config_service.get("target_lang", "Thai") if self.config_service else "Thai")
        self.opt_target_lang = ctk.CTkOptionMenu(
            self.header_frame, 
            values=list(self.languages.keys()),
            command=self._on_language_change,
            variable=self.var_target_lang,
            width=100,
            height=32,
            corner_radius=8,
            fg_color=colors["secondary_accent"],
            button_color=colors["accent"],
            button_hover_color=colors["accent_hover"],
            text_color=colors["text_primary"],
            dropdown_fg_color=colors["bg_card"],
            dropdown_hover_color=colors["bg_card_hover"],
            dropdown_text_color=colors["text_primary"]
        )
        self.opt_target_lang.pack(side="left", padx=4)
        
        # Grouping Mode
        ctk.CTkLabel(self.header_frame, text="Mode", font=("Segoe UI", 13, "bold"), text_color=colors["text_primary"]).pack(side="left", padx=(20, 5))
        
        # Load from config, Default to Manga if missing
        initial_group_mode = self.config_service.get("grouping_mode", "Group (Manga)") if self.config_service else "Group (Manga)"
        self.var_grouping_mode = ctk.StringVar(value=initial_group_mode)

        self.opt_grouping_mode = ctk.CTkOptionMenu(
            self.header_frame,
            values=["Group (Manga)", "Document (Lines)"],
            variable=self.var_grouping_mode,
            command=self._update_merge_mode,
            width=150,
            height=32,
            corner_radius=8,
            fg_color=colors["secondary_accent"],
            button_color=colors["accent"],
            button_hover_color=colors["accent_hover"],
            text_color=colors["text_primary"],
            dropdown_fg_color=colors["bg_card"],
            dropdown_hover_color=colors["bg_card_hover"],
            dropdown_text_color=colors["text_primary"]
        )
        self.opt_grouping_mode.pack(side="left", padx=4)
        
        
        
        # Main content wrapper (for swapping with settings screen)
        # Use tuple for atomic background switch
        from .theme import get_theme_tuple
        self.main_content = ctk.CTkFrame(self, fg_color=get_theme_tuple("bg_primary"))
        self.main_content.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self.main_content.grid_columnconfigure(0, weight=1)
        self.main_content.grid_rowconfigure(0, weight=1)
        
        # 2. Tabs (inside main_content)
        self.tab_view = ctk.CTkTabview(
            self.main_content, 
            command=self._on_tab_change,
            fg_color=colors["bg_primary"],
            segmented_button_fg_color=colors["bg_secondary"],
            segmented_button_selected_color=colors["accent"],
            segmented_button_selected_hover_color=colors["accent_hover"],
            segmented_button_unselected_color=colors["bg_secondary"],
            text_color=colors["text_primary"]
        )
        self.tab_view.grid(row=0, column=0, sticky="nsew", padx=12, pady=(0, 6))
        self.tab_screens = self.tab_view.add("Screens")
        self.tab_apps = self.tab_view.add("Applications")
        
        # Restore Capture Mode (Tab Selection)
        if self.config_service:
            last_capture_mode = self.config_service.get("capture_mode", "Screens")
            if last_capture_mode in ["Screens", "Applications"]:
                 self.tab_view.set(last_capture_mode)

        
        # Set tab content backgrounds to match primary surface
        self.tab_screens.configure(fg_color=colors["bg_primary"])
        self.tab_apps.configure(fg_color=colors["bg_primary"])
        
        # Scrollable Frames (Grids) for each tab
        self.scroll_screens = ctk.CTkScrollableFrame(
            self.tab_screens, 
            fg_color=colors["bg_primary"]
        )
        self.scroll_screens.pack(fill="both", expand=True)
        # Hide scrollbar initially (will show only when content overflows)
        self._hide_scrollbar(self.scroll_screens)
        
        self.scroll_apps = ctk.CTkScrollableFrame(
            self.tab_apps, 
            fg_color=colors["bg_primary"]
        )
        self.scroll_apps.pack(fill="both", expand=True)
        # Hide scrollbar initially
        self._hide_scrollbar(self.scroll_apps)

        # Loading Spinners (Initial State)
        self.scroll_screens.grid_columnconfigure(0, weight=1)
        self.spinner_screens = LoadingSpinner(self.scroll_screens, width=50, height=50)
        self.spinner_screens.grid(row=0, column=0, pady=50) # No side padding, centered by weight
        self.spinner_screens.start()
        
        self.scroll_apps.grid_columnconfigure(0, weight=1)
        self.spinner_apps = LoadingSpinner(self.scroll_apps, width=50, height=50)
        self.spinner_apps.grid(row=0, column=0, pady=50)
        self.spinner_apps.start()

        # 3. Footer — elevated surface
        self.footer_frame = ctk.CTkFrame(self.main_content, fg_color=colors["bg_card"], corner_radius=12)
        self.footer_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))

        self.lbl_status = ctk.CTkLabel(self.footer_frame, text="Ready", anchor="w", text_color=colors["text_secondary"], font=("Segoe UI", 12))
        self.lbl_status.pack(side="left", padx=12, pady=8)
        
        self.btn_define_roi = ctk.CTkButton(
            self.footer_frame, 
            text="Define Area", 
            command=self.define_roi, 
            width=110,
            height=36,
            corner_radius=8,
            fg_color=colors["secondary_accent"],
            hover_color=colors["bg_hover"],
            text_color=colors["text_primary"],
            font=("Segoe UI", 13)
        )
        self.btn_define_roi.pack(side="right", padx=8, pady=8)
        
        self.btn_stream = ctk.CTkButton(
            self.footer_frame, 
            text="Start Capture", 
            command=self.toggle_stream, 
            state="disabled",
            width=130,
            height=36,
            corner_radius=8,
            font=("Segoe UI", 13, "bold")
        )
        self.btn_stream.pack(side="right", padx=8, pady=8)
        
        # 4. Auth Check
        self._update_auth_ui()
        
        # Apply final theme sync
        self.refresh_theme()

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
        if hasattr(self, 'btn_settings'):
            self.btn_settings.destroy()
            
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

        # Create Settings Button (Right Side)
        colors = get_theme()
        
        self.btn_settings = ctk.CTkButton(
            self.header_frame, 
            text="",
            image=getattr(self, 'settings_icon', None),
            width=40,
            height=40,
            fg_color="transparent",
            hover_color=colors["bg_hover"],
            corner_radius=20,
            command=self._on_settings_click
        )
        self.btn_settings.pack(side="right", padx=5)

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
        """Show settings screen directly"""
        self._show_settings_screen()

    def logout_flow(self):
        """Reset auth and update UI"""
        if self.auth_service:
            self.auth_service.logout()
        self._update_auth_ui()
        self.lbl_status.configure(text="Signed Out", text_color="white")
        
        # If we are in settings screen, go back
        if hasattr(self, 'settings_screen') and self.settings_screen:
            self._hide_settings_screen()
    
    def _show_settings_screen(self):
        """Hide main content and header, show settings screen (flicker-free)"""
        from ..ui.components.settings_screen import SettingsScreen
        
        # Get user info first
        user_info = self.auth_service.get_user_info() if self.auth_service else {}
        
        # Create settings screen (but don't show yet)
        self.settings_screen = SettingsScreen(
            self, 
            on_back=self._hide_settings_screen,
            user_info=user_info,
            on_logout=self.logout_flow
        )
        
        # Build the widget tree before showing
        self.settings_screen.update_idletasks()
        
        # Now swap views atomically
        self.header_frame.grid_forget()
        self.main_content.grid_forget()
        self.settings_screen.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=0, pady=0)
        
        # Force redraw
        self.update_idletasks()
    
    def _hide_settings_screen(self):
        """Hide settings screen and restore header + main content (flicker-free)"""
        # Prepare main content before destroying settings
        self.header_frame.update_idletasks()
        self.main_content.update_idletasks()
        
        # Swap views atomically
        if hasattr(self, 'settings_screen') and self.settings_screen:
            self.settings_screen.grid_forget()
        
        self.header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        self.main_content.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        
        # Force redraw then destroy
        self.update_idletasks()
        
        if hasattr(self, 'settings_screen') and self.settings_screen:
            self.settings_screen.destroy()
            self.settings_screen = None
        
        # Re-apply theme after returning to main view
        self.after(50, self.refresh_theme)






    def _update_merge_mode(self, choice):
        if self.config_service:
            self.config_service.set("grouping_mode", choice)
            
        if "Document" in choice:
            self.current_merge_mode = "line"
        else:
            self.current_merge_mode = "paragraph"
        print(f"Merge mode detected: {self.current_merge_mode}")

    def _on_language_change(self, choice):
        if self.config_service:
            self.config_service.set("source_lang", self.var_source_lang.get())
            self.config_service.set("target_lang", self.var_target_lang.get())
            
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
        # Update cache
        self.cached_monitors = monitors
        self.cached_windows = windows
        
        # Synchronize grids instead of rebuilding
        self._sync_grid(self.scroll_screens, monitors, "screens")
        self._sync_grid(self.scroll_apps, windows, "apps")
        
        if not silent:
            self.lbl_status.configure(text="Ready")
            
        # Only remove spinners if we actually found something, OR if this was a manual refresh (silent=False)
        # If silent (auto-refresh) and list is empty, maybe keep spinner? 
        # Actually, if list is empty, we show empty state.
        # Let's remove spinners always when data returns, to avoid infinite spinner on empty list.
        
        # Remove Spinners if present
        if hasattr(self, 'spinner_screens') and self.spinner_screens:
            self.spinner_screens.destroy()
            self.spinner_screens = None
            
        if hasattr(self, 'spinner_apps') and self.spinner_apps:
            self.spinner_apps.destroy()
            self.spinner_apps = None
        
        # Update scrollbar visibility after content changes
        self.after(100, lambda: self._update_scrollbar_visibility(self.scroll_screens))
        self.after(100, lambda: self._update_scrollbar_visibility(self.scroll_apps))

    def _on_resize(self, event):
        """Update grid layout on resize without fetching source data."""
        if not self.cached_monitors and not self.cached_windows: return
        self._update_source_grids(self.cached_monitors, self.cached_windows, silent=True)
        
        # Re-enable start button if selection exists
        if self.selected_source and self.async_engine and not self.is_streaming:
            self.btn_stream.configure(state="normal", fg_color="green")

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
        if self.async_engine and not self.is_streaming:
             self.btn_stream.configure(state="normal", fg_color="green")
        elif not self.async_engine:
             self.btn_stream.configure(state="disabled", fg_color="gray") # Keep disabled until ready

    def _on_tab_change(self):
        if self.config_service:
            self.config_service.set("capture_mode", self.tab_view.get())
            
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
            
            # Start Visibility Loop (Decoupled for instant toggle)
            self._check_visibility()
        else:
            # Stop
            self.is_streaming = False
            self.btn_stream.configure(text="Start", fg_color="green")
            self.overlay.hide()

    def _check_visibility(self):
        """
        Check active window frequently (10Hz) to specific overlay visibility instantly.
        Separate from OCR loop to avoid lag.
        """
        if not self.is_streaming: return
        
        # User requested: always show translation overlay, don't hide when switching windows
        is_active = True
        
        # Check for state change
        prev_state = getattr(self, 'is_target_active', True)
        
        # Update state
        self.is_target_active = is_active
        
        # Apply instantly if changed
        if is_active != prev_state:
             # print(f"Visibility Changed: {prev_state} -> {is_active}", flush=True)
             if not is_active:
                 self.overlay.show_results([], 0, 0, visible=False)
             else:
                 # NEW: Instant Show using Cached Data
                 last_data = getattr(self, 'last_overlay_data', None)
                 if last_data:
                     results, x, y, geometry = last_data
                     if geometry:
                        self.overlay.set_geometry(geometry)
                     self.overlay.show_results(results, x, y, visible=True)

        # Schedule next check (100ms)
        self.after(100, self._check_visibility)



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
                    
                    # NEW: Image similarity check (>85% -> skip and delay 0.2s)
                    last_img = getattr(self, 'last_translated_image', None)
                    if last_img:
                        sim = calculate_similarity(image, last_img)
                        if sim > 85.0:
                            # print(f"Image similar ({sim:.1f}%), skipping frame.")
                            time.sleep(0.2)
                            continue
                            
                    self.last_translated_image = image
                    
                    # Focus check moved to _check_visibility loop
                    is_active = getattr(self, 'is_target_active', True)
                    
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
                # Extract boxes for display
                boxes = [item['bbox'] for item in data]
                # Overlay window IS ALREADY at bounds['left'], bounds['top']
                # So we draw relative to 0,0 of the overlay.
                offset_x = 0 
                offset_y = 0
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
            # Use bounds directly from capture (always current)
            # Fix: Overlay window is moved to bounds['left'], bounds['top'] via geometry.
            # So the content should be drawn at offset 0,0 relative to the window.
            offset_x = 0
            offset_y = 0
            geometry = f"{bounds['width']}x{bounds['height']}+{bounds['left']}+{bounds['top']}"

            if self.is_streaming:
                # Use stored visibility state
                visible = getattr(self, 'is_target_active', True)
                self.after(0, lambda: self._update_overlay_safe(results, raw_text, offset_x, offset_y, geometry, visible))
                
            # Wait 0 seconds (Immediate Yield) for max performance
            await asyncio.sleep(0)
            
            # Request: Blue frame AND Text disappear after 5s delay
            # User Change (1621): Keep overlay until next result replaces it.
            # self.after(0, self.overlay.hide)

        except Exception as e:
            print(f"Proc Error: {e}")

    def _update_overlay_safe(self, results, raw_text, x, y, geometry, visible=True):
        if not self.is_streaming:
            self.overlay.hide()
            return
            
        if geometry:
            self.overlay.set_geometry(geometry)
        self.overlay.show_results(results, x, y, visible=visible)
        
        # Cache for Instant Restore
        self.last_overlay_data = (results, x, y, geometry)
        
        # self.overlay.show_debug(f"Raw: {raw_text[:50]}...")
            

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
