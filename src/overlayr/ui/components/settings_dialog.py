import customtkinter as ctk

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_close=None):
        super().__init__(parent)
        
        # Anti-flicker: hide window during construction
        self.withdraw()
        
        self.title("Settings")
        self.geometry("400x300")
        self.resizable(False, False)
        
        self.on_close = on_close
        
        # Center on parent
        self.transient(parent)
        
        self._build_ui()
        
        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._close)
        
        # Anti-flicker: show window after fully built
        self.update_idletasks()
        self.after(50, self._show_dialog)
    
    def _show_dialog(self):
        self.deiconify()
        self.attributes("-topmost", True)
        self.grab_set()
        if self.winfo_exists():
            self.focus_set()

    def _build_ui(self):
        # Title
        ctk.CTkLabel(
            self, 
            text="Settings", 
            font=("Segoe UI", 20, "bold")
        ).pack(pady=(20, 10))
        
        # Placeholder content
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        ctk.CTkLabel(
            content_frame,
            text="Settings options coming soon...",
            text_color="gray70"
        ).pack(pady=20)
        
        # Close Button
        ctk.CTkButton(
            self,
            text="Close",
            command=self._close,
            width=100
        ).pack(pady=20)
    
    def _close(self):
        if self.on_close:
            self.on_close()
        self.destroy()
