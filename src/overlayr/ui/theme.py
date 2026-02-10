"""
Theme configuration for Overlayr
Synced with web (global.css) — cyan/turquoise primary + royal blue accent.
Dark: deep blue-gray ramp.  Light: cool off-white ramp.
"""

# Dark Theme — Synced with web: hsl(215,40%,6%) base
# Cyan primary (#00e9ff), Royal blue accent (#3d98f4)
DARK_THEME = {
    "bg_primary": "#090e15",       # Base surface — web --color-bg
    "bg_secondary": "#090e15",     # Unified background
    "bg_card": "#111821",          # Elevated surface — web --color-surface
    "bg_card_selected": "#1f2937", # Selected card (Gray)
    "bg_card_hover": "#1a222e",    # Card hover
    "bg_hover": "#182230",         # General hover
    "accent": "#3d98f4",           # Royal blue — web --color-accent
    "accent_hover": "#5aabf7",     # Lighter on hover
    "primary_accent": "#00e9ff",   # Cyan — web --color-primary
    "secondary_accent": "#182230", # Muted surface button
    "text_primary": "#ffffff",     # White — web --color-text-main
    "text_secondary": "#b2bdcb",   # Muted — web --color-text-muted
    "text_muted": "#6b7a8d",       # Dim gray
    "border": "#1e2a38",           # Subtle border
    "success": "#5cb97a",          # Desaturated green
    "warning": "#d4a94e",          # Warm amber
    "error": "#d4605a",            # Muted coral-red
    "pro_badge": "#d4a94e",        # Matches warning tone
    "pro_badge_bg": "#1e1a0e",     # Subtle dark warm tint
    "overlay_bg": "#111821",       # Matching surface
    "overlay_text": "#ffffff",     # White
    "overlay_border": "#00e9ff",   # Cyan border
    "overlay_glow": "transparent",
}

# Light Theme — Synced with web: hsl(215,20%,95%) base
# Darker cyan primary (#0bc8da), Royal blue accent (#248cf3)
LIGHT_THEME = {
    "bg_primary": "#f0f2f5",       # Base surface — slightly softer off-white
    "bg_secondary": "#f0f2f5",     # Unified background
    "bg_card": "#ffffff",          # Keep cards white for contrast against gray bg
    "bg_card_selected": "#e5e7eb", # Selected card (Gray)
    "bg_card_hover": "#f3f4f6",    # Hover — light gray
    "bg_hover": "#e5e7eb",         # General hover
    "accent": "#248cf3",           # Royal blue — web --color-accent
    "accent_hover": "#4da3f7",     # Lighter on hover
    "primary_accent": "#0bc8da",   # Darker cyan — web --color-primary
    "secondary_accent": "#eef0f3", # Muted surface button
    "text_primary": "#000000",     # Pure black
    "text_secondary": "#333333",   # Dark gray
    "text_muted": "#a0aec0",       # Light gray
    "border": "#e2e8f0",           # Subtle border
    "success": "#3d8f5a",          # Forest green
    "warning": "#b38b30",          # Warm amber
    "error": "#c04840",            # Muted red
    "pro_badge": "#b38b30",        # Matches warning tone
    "pro_badge_bg": "#fefcbf",     # Soft warm background
    "overlay_bg": "#ffffff",       # White card
    "overlay_text": "#1a202c",     # Softened black
    "overlay_border": "#0bc8da",   # Cyan border
    "overlay_glow": "transparent",
}

def get_theme():
    """Get the current theme colors based on appearance mode"""
    import customtkinter as ctk
    mode = ctk.get_appearance_mode()
    return DARK_THEME if mode == "Dark" else LIGHT_THEME

def apply_theme_to_window(window):
    """Apply theme colors to the main window"""
    import customtkinter as ctk
    theme = get_theme()
    
    try:
        window.configure(fg_color=theme["bg_primary"])
    except:
        pass

def get_theme_tuple(key):
    """
    Get a (light_color, dark_color) tuple for a specific theme key.
    Useful for initialization to allow CTk to handle theme switching atomically.
    """
    return (LIGHT_THEME[key], DARK_THEME[key])
