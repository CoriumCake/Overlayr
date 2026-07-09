#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")] // Hide console window in release builds

mod capture;
mod ocr;
mod translation;
mod source_manager;
mod gui;

fn load_window_icon() -> egui::IconData {
    let icon_bytes = include_bytes!("../Icon.ico");
    if let Ok(img) = image::load_from_memory_with_format(icon_bytes, image::ImageFormat::Ico) {
        use image::GenericImageView;
        let (width, height) = img.dimensions();
        let rgba = img.into_rgba8().into_raw();
        egui::IconData {
            rgba,
            width,
            height,
        }
    } else {
        egui::IconData::default()
    }
}

fn main() -> Result<(), eframe::Error> {

    let mut options = eframe::NativeOptions::default();
    options.viewport = egui::ViewportBuilder::default()
        .with_title("Overlayr")
        .with_inner_size([550.0, 500.0])
        .with_resizable(false)
        .with_maximize_button(false)
        .with_icon(load_window_icon());

    eframe::run_native(
        "overlayr_main",
        options,
        Box::new(|cc| Box::new(crate::gui::MainWindow::new(cc))),
    )
}
