fn main() -> eframe::Result {
    limit_malloc_arenas();
    let arguments = std::env::args().collect::<Vec<_>>();
    let argument = arguments.get(1).map(String::as_str);
    if let Err(error) = overlayr_core::auth::handle_launch_args(&arguments[1..]) {
        eprintln!("Could not save login session: {error}");
    }
    let is_command = matches!(
        argument,
        Some(
            "--version"
                | "-V"
                | "--help"
                | "-h"
                | "--diagnose"
                | "--toggle"
                | "--toggle-translations"
        )
    );
    if !is_command && let Err(error) = overlayr_core::auth::register_protocol() {
        eprintln!("Could not register overlayr login protocol: {error}");
    }
    // Shortcut commands go to the running instance and exit.
    if let Some(command) = argument.and_then(overlayr_core::control::Command::from_argument) {
        if let Err(error) = overlayr_core::control::send(command) {
            eprintln!("Overlayr isn't running ({error})");
            std::process::exit(1);
        }
        return Ok(());
    }
    if matches!(argument, Some("--version" | "-V")) {
        println!("overlayr {}", overlayr_core::diagnostics::version());
        return Ok(());
    }
    if matches!(argument, Some("--help" | "-h")) {
        println!(
            "Overlayr - real-time screen translation overlay\n\nUsage:\n  overlayr           Open the settings window\n  overlayr --diagnose Check desktop, OCR, and configuration readiness\n  overlayr --toggle   Start or stop the overlay of the running Overlayr\n  overlayr --toggle-translations\n                      Hide or show the translations\n  overlayr --version  Print the application version"
        );
        return Ok(());
    }
    if matches!(argument, Some("--diagnose")) {
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
            .with_min_inner_size([420.0, 420.0]),
        // On Wayland, a vsynced buffer swap waits for a frame callback that the
        // compositor never sends to a hidden window (e.g. on another
        // workspace). That blocked the whole event loop, freezing the overlay
        // and triggering "not responding". Repaints are scheduled explicitly,
        // so vsync isn't needed.
        vsync: false,
        ..Default::default()
    };
    eframe::run_native(
        "Overlayr",
        native_options,
        Box::new(|_creation_context| Ok(Box::new(overlayr_core::ui::OverlayrApp::new()))),
    )
}

/// glibc gives each thread that allocates its own memory pool, and with
/// capture, OCR and UI threads passing large frames between them the pools
/// fragment: OCR alone held ~190 MB more than with two pools, at the same
/// speed. Must run before any thread starts.
fn limit_malloc_arenas() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    {
        const M_ARENA_MAX: i32 = -8;
        unsafe extern "C" {
            fn mallopt(param: i32, value: i32) -> i32;
        }
        // SAFETY: mallopt only changes allocator tuning; nothing else is
        // running yet.
        unsafe {
            mallopt(M_ARENA_MAX, 2);
        }
    }
}
