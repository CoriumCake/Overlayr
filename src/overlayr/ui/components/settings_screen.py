import customtkinter as ctk
from ..theme import get_theme, DARK_THEME, LIGHT_THEME

class SettingsScreen(ctk.CTkFrame):
    """Settings screen that replaces the main content area"""
    
    def __init__(self, parent, on_back, user_info=None, on_logout=None):
        colors = get_theme()
        # Use tuple color for atomic theme switching
        from ..theme import get_theme_tuple
        super().__init__(parent, fg_color=get_theme_tuple("bg_primary"))
        self.on_back = on_back
        self.user_info = user_info or {}
        self.on_logout = on_logout
        
        # Track pending changes
        self._original_theme = ctk.get_appearance_mode()
        self._pending_theme = self._original_theme
        
        self._build_ui()
    
    def _get_colors(self):
        """Get current theme colors"""
        return get_theme()
    
    
    def _build_ui(self):
        colors = self._get_colors()
        from ..theme import get_theme_tuple
        
        # Header with title and close button
        self.header_frame = ctk.CTkFrame(self, fg_color=get_theme_tuple("bg_primary"))
        self.header_frame.pack(fill="x", padx=15, pady=(15, 10))
        
        # Title (left side)
        self.title_label = ctk.CTkLabel(
            self.header_frame,
            text="Settings",
            font=("Segoe UI", 24, "bold"),
            text_color=colors["text_primary"]
        )
        self.title_label.pack(side="left", padx=5)
        
        # Close button (X on right)
        self.close_btn = ctk.CTkButton(
            self.header_frame,
            text="✕",
            command=self._on_discard,
            width=40,
            height=40,
            fg_color=colors["secondary_accent"],
            hover_color=colors["bg_hover"],
            text_color=colors["text_primary"],
            font=("Segoe UI", 16, "bold"),
            corner_radius=10
        )
        self.close_btn.pack(side="right")
        
        # Content area (scrollable)
        self.content = ctk.CTkScrollableFrame(
            self, 
            fg_color=get_theme_tuple("bg_primary")
        )
        self.content.pack(fill="both", expand=True, padx=15, pady=10)
        
        # === Account Section ===
        self._build_account_section(colors)
        
        # === Appearance Section ===
        self._build_appearance_section(colors)
        
        # === About Section ===
        self._build_about_section(colors)
        
        # === Footer with Save/Discard buttons ===
        self._build_footer(colors)
    
    def _build_account_section(self, colors):
        self.account_section = ctk.CTkFrame(
            self.content, 
            fg_color=colors["bg_card"], 
            corner_radius=12,
            border_width=1,
            border_color=colors["border"]
        )
        self.account_section.pack(fill="x", pady=6)
        
        self.account_content = ctk.CTkFrame(self.account_section, fg_color=colors["bg_card"])
        self.account_content.pack(fill="x", padx=20, pady=20)
        
        # Section title with icon
        self.account_title_row = ctk.CTkFrame(self.account_content, fg_color=colors["bg_card"])
        self.account_title_row.pack(fill="x", pady=(0, 15))
        
        self.account_title_label = ctk.CTkLabel(
            self.account_title_row,
            text="👤  Account",
            font=("Segoe UI", 16, "bold"),
            text_color=colors["text_primary"]
        )
        self.account_title_label.pack(side="left")

        if not self.user_info.get("logged_in"):
            # Guest State
            self.guest_msg_frame = ctk.CTkFrame(self.account_content, fg_color=colors["bg_card"])
            self.guest_msg_frame.pack(fill="x")
            
            self.guest_msg_label = ctk.CTkLabel(
                self.guest_msg_frame,
                text="Sign in to sync your profile and access Pro features.",
                font=("Segoe UI", 13),
                text_color=colors["text_secondary"],
                justify="left"
            )
            self.guest_msg_label.pack(side="left")
            
            self.signin_btn = ctk.CTkButton(
                self.account_content,
                text="Sign In",
                command=self._handle_login,
                width=120,
                height=36,
                fg_color=colors["accent"],
                hover_color=colors["accent_hover"],
                text_color="white",
                font=("Segoe UI", 13, "bold"),
                corner_radius=8
            )
            self.signin_btn.pack(anchor="w", pady=(15, 0))
            return

        # Profile row (avatar + info) - ONLY for logged in users
        self.profile_row = ctk.CTkFrame(self.account_content, fg_color=colors["bg_card"])
        self.profile_row.pack(fill="x")
        
        # Avatar
        self._load_profile_image(self.profile_row, colors)
        
        # Info column (beside avatar)
        self.info_col = ctk.CTkFrame(self.profile_row, fg_color=colors["bg_card"])
        self.info_col.pack(side="left", fill="x", expand=True, padx=(15, 0))
        
        # Email
        email = self.user_info.get("email") or self.user_info.get("display_name") or "Unknown"
        self.email_label = ctk.CTkLabel(
            self.info_col,
            text=email,
            font=("Segoe UI", 14, "bold"),
            text_color=colors["text_primary"]
        )
        self.email_label.pack(anchor="w")
        
        # Plan / Package Name
        is_pro = self.user_info.get("pro", False)
        plan_text = "Pro Plan" if is_pro else "Free Plan"
        plan_color = colors["pro_badge"] if is_pro else colors["text_secondary"]
        
        self.plan_label = ctk.CTkLabel(
            self.info_col,
            text=plan_text,
            font=("Segoe UI", 12, "bold" if is_pro else "normal"),
            text_color=plan_color
        )
        self.plan_label.pack(anchor="w", pady=(2, 0))
        
        # Logout button (below profile row)
        if self.on_logout:
            self.logout_btn = ctk.CTkButton(
                self.account_content,
                text="Sign Out",
                command=self._handle_logout,
                width=110,
                height=36,
                fg_color=colors["secondary_accent"],
                hover_color=colors["bg_hover"],
                text_color=colors["error"],
                font=("Segoe UI", 13),
                corner_radius=8
            )
            self.logout_btn.pack(anchor="w", pady=(15, 0))

    def _handle_login(self):
        """Trigger login flow from parent window"""
        if hasattr(self.master, 'login_flow'):
            self.master.login_flow()
            # Also close settings to show the "Waiting for login" status on main screen
            self.on_back()
    
    def _build_appearance_section(self, colors):
        self.appearance_section = ctk.CTkFrame(
            self.content, 
            fg_color=colors["bg_card"], 
            corner_radius=12,
            border_width=1,
            border_color=colors["border"]
        )
        self.appearance_section.pack(fill="x", pady=6)
        
        self.appearance_content = ctk.CTkFrame(self.appearance_section, fg_color=colors["bg_card"])
        self.appearance_content.pack(fill="x", padx=20, pady=20)
        
        # Header row
        self.appearance_header_row = ctk.CTkFrame(self.appearance_content, fg_color=colors["bg_card"])
        self.appearance_header_row.pack(fill="x")
        
        self.appearance_label = ctk.CTkLabel(
            self.appearance_header_row,
            text="🎨  Appearance",
            font=("Segoe UI", 16, "bold"),
            text_color=colors["text_primary"]
        )
        self.appearance_label.pack(side="left")
        
        # Theme switcher (segmented button)
        self.theme_var = ctk.StringVar(value=ctk.get_appearance_mode())
        self.theme_switch = ctk.CTkSegmentedButton(
            self.appearance_header_row,
            values=["Light", "Dark"],
            variable=self.theme_var,
            command=self._on_theme_preview,
            font=("Segoe UI", 13),
            corner_radius=8
        )
        self.theme_switch.pack(side="right")
    
    def _build_about_section(self, colors):
        self.about_section = ctk.CTkFrame(
            self.content, 
            fg_color=colors["bg_card"], 
            corner_radius=12,
            border_width=1,
            border_color=colors["border"]
        )
        self.about_section.pack(fill="x", pady=6)
        
        self.about_content = ctk.CTkFrame(self.about_section, fg_color=colors["bg_card"])
        self.about_content.pack(fill="x", padx=20, pady=20)
        
        self.about_title_label = ctk.CTkLabel(
            self.about_content,
            text="ℹ️  About",
            font=("Segoe UI", 16, "bold"),
            text_color=colors["text_primary"]
        )
        self.about_title_label.pack(anchor="w")
        
        self.about_version_label = ctk.CTkLabel(
            self.about_content,
            text="Overlayr v1.0.0",
            font=("Segoe UI", 13),
            text_color=colors["text_secondary"]
        )
        self.about_version_label.pack(anchor="w", pady=(10, 0))
        
        self.about_desc_label = ctk.CTkLabel(
            self.about_content,
            text="Real-time screen translation overlay",
            font=("Segoe UI", 12),
            text_color=colors["text_muted"]
        )
        self.about_desc_label.pack(anchor="w", pady=(5, 0))
    
    def _build_footer(self, colors):
        from ..theme import get_theme_tuple
        self.footer = ctk.CTkFrame(self, fg_color=get_theme_tuple("bg_primary"))
        self.footer.pack(fill="x", padx=15, pady=15)
        
        # Cancel button
        self.discard_btn = ctk.CTkButton(
            self.footer,
            text="Cancel",
            command=self._on_discard,
            width=90,
            height=36,
            fg_color=colors["secondary_accent"],
            hover_color=colors["bg_hover"],
            text_color=colors["text_primary"],
            font=("Segoe UI", 13),
            corner_radius=8
        )
        self.discard_btn.pack(side="left", padx=(0, 8))
        
        # Save button
        self.save_btn = ctk.CTkButton(
            self.footer,
            text="Save Changes",
            command=self._on_save,
            width=120,
            height=36,
            fg_color=colors["accent"],
            hover_color=colors["accent_hover"],
            text_color="white",
            font=("Segoe UI", 13, "bold"),
            corner_radius=8
        )
        self.save_btn.pack(side="right")
    
    
    def refresh_theme(self):
        """Update all settings UI components to match the current theme"""
        colors = self._get_colors()
        
        # 1. Main Containers - ALREADY handled by Tuple Colors (Atomic Switch)
        # We don't need to manually configure fg_color for these anymore
        # self.configure(fg_color=colors["bg_primary"])
        # self.header_frame.configure(fg_color=colors["bg_primary"])
        # self.content.configure(fg_color=colors["bg_primary"])
        # self.footer.configure(fg_color=colors["bg_primary"])
        
        # 2. Header Elements
        self.title_label.configure(text_color=colors["text_primary"])
        self.close_btn.configure(
            fg_color=colors["secondary_accent"],
            hover_color=colors["bg_hover"],
            text_color=colors["text_primary"]
        )
        
        # 3. Account Section
        self.account_section.configure(fg_color=colors["bg_card"], border_color=colors["border"])
        self.account_content.configure(fg_color=colors["bg_card"])
        self.account_title_row.configure(fg_color=colors["bg_card"])
        self.account_title_label.configure(text_color=colors["text_primary"])
        
        if hasattr(self, 'guest_msg_frame'):
            self.guest_msg_frame.configure(fg_color=colors["bg_card"])
            self.guest_msg_label.configure(text_color=colors["text_secondary"])
            self.signin_btn.configure(
                fg_color=colors["accent"],
                hover_color=colors["accent_hover"]
            )
        
        if hasattr(self, 'profile_row'):
            self.profile_row.configure(fg_color=colors["bg_card"])
            self.info_col.configure(fg_color=colors["bg_card"])
            self.email_label.configure(text_color=colors["text_primary"])
            
            is_pro = self.user_info.get("pro", False)
            plan_color = colors["pro_badge"] if is_pro else colors["text_secondary"]
            self.plan_label.configure(text_color=plan_color)
            
            if hasattr(self, 'logout_btn'):
                self.logout_btn.configure(
                    fg_color=colors["secondary_accent"],
                    hover_color=colors["bg_hover"],
                    text_color=colors["error"]
                )
        
        # 4. Appearance Section
        self.appearance_section.configure(fg_color=colors["bg_card"], border_color=colors["border"])
        self.appearance_content.configure(fg_color=colors["bg_card"])
        self.appearance_header_row.configure(fg_color=colors["bg_card"])
        self.appearance_label.configure(text_color=colors["text_primary"])
        # theme_switch handles its own colors via segmented button defaults usually,
        # but we can configure it if needed
        # self.theme_switch.configure(...) 
        
        # 5. About Section
        self.about_section.configure(fg_color=colors["bg_card"], border_color=colors["border"])
        self.about_content.configure(fg_color=colors["bg_card"])
        self.about_title_label.configure(text_color=colors["text_primary"])
        self.about_version_label.configure(text_color=colors["text_secondary"])
        self.about_desc_label.configure(text_color=colors["text_muted"])
        
        # 6. Footer Buttons
        self.discard_btn.configure(
            fg_color=colors["secondary_accent"],
            hover_color=colors["bg_hover"],
            text_color=colors["text_primary"]
        )
        self.save_btn.configure(
            fg_color=colors["accent"],
            hover_color=colors["accent_hover"]
        )

    def _on_theme_preview(self, value):
        """Preview theme change (live)"""
        self._pending_theme = value
        
        # CTk handles background colors atomically via tuples now.
        # We only need to trigger text/icon updates.
        ctk.set_appearance_mode(value)
        self.refresh_theme()
        
        # Force master update if available
        if hasattr(self.master, 'refresh_theme'):
             self.master.refresh_theme()
    
    def _on_save(self):
        """Save settings and go back"""
        # Theme is already applied via preview
        # Persist to config file
        if hasattr(self.master, 'config_service') and self.master.config_service:
            self.master.config_service.set("appearance_mode", ctk.get_appearance_mode())
            
        self.on_back()
    
    def _on_discard(self):
        """Discard changes and go back"""
        # Revert theme if changed
        if self._pending_theme != self._original_theme:
            ctk.set_appearance_mode(self._original_theme)
        self.on_back()
    
    def _handle_logout(self):
        """Handle logout button click"""
        if self.on_logout:
            self.on_logout()
        self.on_back()
    
    def _load_profile_image(self, parent, colors):
        """Load and display profile image"""
        import threading
        from PIL import Image, ImageDraw
        import io
        
        photo_url = self.user_info.get("photo_url")
        avatar_size = 64
        
        # Create placeholder first
        self.avatar_label = ctk.CTkLabel(parent, text="", width=avatar_size, height=avatar_size)
        self.avatar_label.pack(side="left")
        
        # Create circular placeholder
        def create_placeholder():
            img = Image.new('RGBA', (avatar_size, avatar_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse((0, 0, avatar_size-1, avatar_size-1), fill=colors["bg_hover"])
            return ctk.CTkImage(light_image=img, dark_image=img, size=(avatar_size, avatar_size))
        
        self.avatar_label.configure(image=create_placeholder())
        
        if photo_url:
            def fetch_image():
                try:
                    import urllib.request
                    with urllib.request.urlopen(photo_url, timeout=5) as response:
                        data = response.read()
                    
                    img = Image.open(io.BytesIO(data))
                    img = img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                    
                    # Make circular
                    mask = Image.new('L', (avatar_size, avatar_size), 0)
                    draw = ImageDraw.Draw(mask)
                    draw.ellipse((0, 0, avatar_size-1, avatar_size-1), fill=255)
                    
                    output = Image.new('RGBA', (avatar_size, avatar_size), (0, 0, 0, 0))
                    output.paste(img, (0, 0))
                    output.putalpha(mask)
                    
                    ctk_img = ctk.CTkImage(light_image=output, dark_image=output, size=(avatar_size, avatar_size))
                    
                    # Update on main thread
                    if self.winfo_exists():
                        self.after(0, lambda: self.avatar_label.configure(image=ctk_img) if self.winfo_exists() else None)
                except Exception as e:
                    print(f"Profile image load error: {e}")
            
            threading.Thread(target=fetch_image, daemon=True).start()
