fn main() -> eframe::Result {
    let instance = match overlayr_core::single_instance::SingleInstance::acquire() {
        Ok(Some(instance)) => instance,
        Ok(None) => {
            eprintln!("Overlayr is already running");
            return Ok(());
        }
        Err(error) => {
            eprintln!("Could not acquire Overlayr single-instance lock: {error}");
            return Err(eframe::Error::AppCreation(Box::new(error)));
        }
    };
    let _instance = instance;
    let native_options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_title("Overlayr")
            .with_inner_size([1100.0, 720.0])
            .with_min_inner_size([820.0, 540.0]),
        vsync: true,
        ..Default::default()
    };
    eframe::run_native(
        "Overlayr",
        native_options,
        Box::new(|_creation_context| Ok(Box::new(overlayr_core::ui::OverlayrApp::new()))),
    )
}
