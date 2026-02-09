import customtkinter as ctk # type: ignore
from PIL import Image

class ProfileMenu(ctk.CTkFrame):
    def __init__(self, parent, user_info, on_logout, on_settings):
        super().__init__(parent, fg_color="#2b2b2b", corner_radius=10, border_width=1, border_color="gray30")
        self.user_info = user_info
        self.on_logout = on_logout
        self.on_settings = on_settings
        
        # Dimensions
        self.width = 220
        # self.height will be determined by content
        
        self.grid_columnconfigure(0, weight=1)
        
        # --- UI Build ---
        self._build_ui()
        
        # Close on click outside (Parent needs to handle this via global binding or transparent overlay)
        # For now, we will use a "Dismiss" button or rely on parent to kill us.
        # Better: Create a transparent button that covers the whole screen BEHIND this frame?
        # Or just let MainWindow handle clicks to close.

    def _build_ui(self):
        # 1. Header (User Info)
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", padx=10, pady=(15, 10))
        
        # Avatar
        icon = self.user_info.get('large_icon')
        if icon:
             self.avatar_label = ctk.CTkLabel(self.header_frame, text="", image=icon)
             self.avatar_label.pack(pady=(0, 5))
        
        # Name
        name = self.user_info.get("display_name") or "User"
        ctk.CTkLabel(
            self.header_frame, 
            text=name, 
            font=("Segoe UI", 16, "bold"),
            text_color="white"
        ).pack()
        
        # Email
        email = self.user_info.get("email")
        if email and "@" in email:
            if len(email) > 25: email = email[:22] + "..."
            ctk.CTkLabel(
                self.header_frame, 
                text=email, 
                font=("Segoe UI", 11), 
                text_color="gray70"
            ).pack()

        # Pro Badge
        if self.user_info.get("pro"):
            ctk.CTkLabel(
                self.header_frame,
                text="PRO ACCOUNT",
                font=("Segoe UI", 9, "bold"),
                text_color="#FFD700",
                fg_color="#332b00",
                corner_radius=4,
                height=18
            ).pack(pady=(5, 0))

        # 2. Divider
        ctk.CTkFrame(self, height=1, fg_color="gray40").pack(fill="x", padx=15, pady=5)
        
        # 3. Actions
        self._create_btn("Settings", self.on_settings)
        self._create_btn("Sign Out", self.on_logout, color="red")
        
        # Bottom Padding
        ctk.CTkFrame(self, height=10, fg_color="transparent").pack()

    def _create_btn(self, text, command, color=None):
        btn = ctk.CTkButton(
            self, 
            text=text, 
            command=command, # Parent handles destroy/hiding
            fg_color="transparent",
            text_color=color if color == "red" else "white",
            hover_color="gray30",
            anchor="w",
            font=("Segoe UI", 14),
            height=40
        )
        btn.pack(fill="x", padx=5, pady=2)

    def show(self, target_x, target_y):
        # Ensure correct size
        self.update_idletasks()
        req_h = self.winfo_reqheight()
        
        # Reset to normal color and size
        self.configure(fg_color=["#2B2B2B", "#2B2B2B"], height=req_h) 
        # Note: Default CTkFrame color is usually gray transparent-ish in some themes, 
        # but let's stick to a solid dark color for the menu.
        
        # Disable propagation so we can force height if needed, 
        # but for static show, pack_propagate(True) is actually safer to let it fit content.
        # Let's try letting it autosize first.
        self.pack_propagate(True)
        self.grid_propagate(True)
        
        self.place(x=target_x, y=target_y)
        self.lift()


