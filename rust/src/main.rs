fn main() -> eframe::Result {
    let argument = std::env::args().nth(1);
    if matches!(argument.as_deref(), Some("--version" | "-V")) {
        println!("overlayr {}", overlayr_core::diagnostics::version());
        return Ok(());
    }
    if matches!(argument.as_deref(), Some("--help" | "-h")) {
        println!(
            "Overlayr - real-time screen translation overlay\n\nUsage:\n  overlayr           Open the settings window\n  overlayr --diagnose Check desktop, OCR, and configuration readiness\n  overlayr --version  Print the application version"
        );
        return Ok(());
    }
    if matches!(argument.as_deref(), Some("--diagnose")) {
        let config_path = overlayr_core::AppConfig::default_path();
        let config_result =
            overlayr_core::AppConfig::load(&config_path).map_err(|error| error.to_string());
        let report = overlayr_core::diagnostics::collect(&config_path, config_result);
        print!("{report}");
        if !report.is_healthy() {
            std::process::exit(1);
        }
        return Ok(());
    }
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
