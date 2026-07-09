use egui::{Color32, FontData, FontDefinitions, FontFamily, Style, Visuals};

pub fn apply_theme(ctx: &egui::Context) {
    // 1. Setup Visuals (Colors)
    let mut visuals = Visuals::dark();
    
    // Sleek dark palette
    visuals.widgets.noninteractive.bg_fill = Color32::from_rgb(18, 18, 22);
    visuals.widgets.noninteractive.fg_stroke.color = Color32::from_rgb(220, 220, 225);
    
    visuals.widgets.inactive.bg_fill = Color32::from_rgb(28, 28, 35);
    visuals.widgets.inactive.fg_stroke.color = Color32::from_rgb(180, 180, 190);
    
    visuals.widgets.hovered.bg_fill = Color32::from_rgb(45, 45, 55);
    visuals.widgets.hovered.fg_stroke.color = Color32::from_rgb(255, 255, 255);
    visuals.widgets.hovered.expansion = 1.0;
    
    visuals.widgets.active.bg_fill = Color32::from_rgb(60, 60, 75);
    visuals.widgets.active.fg_stroke.color = Color32::from_rgb(255, 255, 255);
    
    // Vibrant Neon Accent (Neon Cyan / Electric Blue)
    let accent_color = Color32::from_rgb(0, 210, 255);
    visuals.selection.bg_fill = accent_color;
    visuals.selection.stroke.color = Color32::WHITE;
    
    visuals.hyperlink_color = accent_color;
    visuals.window_fill = Color32::from_rgb(18, 18, 22);
    visuals.window_stroke.color = Color32::from_rgb(40, 40, 50);
    
    ctx.set_visuals(visuals);

    // 2. Setup Fonts with fallbacks for Thai and Emojis
    let mut fonts = FontDefinitions::default();
    let mut proportional = vec![];
    let mut monospace = vec![];

    let mut add_font = |name: &str, path: &str| {
        if let Ok(font_bytes) = std::fs::read(path) {
            fonts.font_data.insert(
                name.to_owned(),
                FontData::from_owned(font_bytes),
            );
            proportional.push(name.to_owned());
            monospace.push(name.to_owned());
        }
    };

    add_font("segoe_ui", "C:\\Windows\\Fonts\\segoeui.ttf");
    add_font("leelawadee", "C:\\Windows\\Fonts\\leelawad.ttf");
    add_font("tahoma", "C:\\Windows\\Fonts\\tahoma.ttf");
    add_font("segoe_emoji", "C:\\Windows\\Fonts\\seguiemj.ttf");

    if let Some(family) = fonts.families.get_mut(&FontFamily::Proportional) {
        for name in proportional.iter().rev() {
            family.insert(0, name.clone());
        }
    }
    if let Some(family) = fonts.families.get_mut(&FontFamily::Monospace) {
        for name in monospace.iter().rev() {
            family.insert(0, name.clone());
        }
    }

    ctx.set_fonts(fonts);
}

pub fn get_accent_color() -> Color32 {
    Color32::from_rgb(0, 210, 255)
}

pub fn get_bg_color() -> Color32 {
    Color32::from_rgb(18, 18, 22)
}

pub fn get_panel_bg_color() -> Color32 {
    Color32::from_rgb(28, 28, 35)
}
