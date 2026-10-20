//! macOS integration: the Screen Recording permission, and `overlayr://`
//! login links, which macOS delivers as an Apple Event instead of a launch
//! argument.

use std::ffi::{CStr, c_char};

use objc2::rc::Retained;
use objc2::runtime::{AnyObject, NSObject};
use objc2::{ClassType, class, define_class, msg_send, sel};

#[link(name = "CoreGraphics", kind = "framework")]
unsafe extern "C" {
    fn CGPreflightScreenCaptureAccess() -> bool;
    fn CGRequestScreenCaptureAccess() -> bool;
}

/// System Settings → Privacy & Security → Screen Recording.
const SCREEN_RECORDING_SETTINGS: &str =
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";

/// Whether Overlayr may capture other apps' windows. Without Screen
/// Recording access, captures only show the desktop wallpaper.
pub fn can_record_screen() -> bool {
    // SAFETY: takes no arguments and only reads the permission.
    unsafe { CGPreflightScreenCaptureAccess() }
}

/// Asks for Screen Recording access. macOS shows its own prompt only the
/// first time; this also opens the settings page, where the user switches
/// Overlayr on afterwards. Access applies once Overlayr restarts.
pub fn request_screen_recording() {
    // SAFETY: takes no arguments; shows the system prompt at most once.
    if unsafe { CGRequestScreenCaptureAccess() } {
        return;
    }
    open_screen_recording_settings();
}

pub fn open_screen_recording_settings() {
    if let Err(error) = std::process::Command::new("open")
        .arg(SCREEN_RECORDING_SETTINGS)
        .spawn()
    {
        eprintln!("Could not open the Screen Recording settings: {error}");
    }
}

/// `kInternetEventClass` and `kAEGetURL`: "open this URL".
const GET_URL: u32 = u32::from_be_bytes(*b"GURL");
/// `keyDirectObject`: the event's main parameter, here the URL.
const DIRECT_OBJECT: u32 = u32::from_be_bytes(*b"----");

define_class!(
    // SAFETY: NSObject has no subclassing requirements, and this class
    // doesn't implement Drop.
    #[unsafe(super(NSObject))]
    #[name = "OverlayrLoginLinkHandler"]
    struct LoginLinkHandler;

    impl LoginLinkHandler {
        #[unsafe(method(handleGetURLEvent:withReplyEvent:))]
        fn handle_get_url(&self, event: &AnyObject, _reply: &AnyObject) {
            let Some(url) = event_url(event) else {
                return;
            };
            // Saves the session; the window notices it within seconds.
            if let Err(error) = crate::auth::handle_launch_args([url]) {
                eprintln!("Could not save login session: {error}");
            }
        }
    }
);

/// The URL a "get URL" Apple Event carries.
fn event_url(event: &AnyObject) -> Option<String> {
    // SAFETY: `event` is the NSAppleEventDescriptor AppKit passes to the
    // handler; these methods take and return the types declared here.
    unsafe {
        let descriptor: Option<Retained<AnyObject>> =
            msg_send![event, paramDescriptorForKeyword: DIRECT_OBJECT];
        let descriptor = descriptor?;
        let string: Option<Retained<AnyObject>> = msg_send![&*descriptor, stringValue];
        let string = string?;
        let utf8: *const c_char = msg_send![&*string, UTF8String];
        (!utf8.is_null()).then(|| CStr::from_ptr(utf8).to_string_lossy().into_owned())
    }
}

/// Receives `overlayr://` links. Called before the event loop starts, so a
/// link that launched Overlayr is handled too.
pub fn handle_login_links() {
    // SAFETY: plain NSObject construction and the documented
    // NSAppleEventManager API, with a selector the handler class defines.
    unsafe {
        let handler: Retained<LoginLinkHandler> = msg_send![LoginLinkHandler::class(), new];
        let manager: Retained<AnyObject> =
            msg_send![class!(NSAppleEventManager), sharedAppleEventManager];
        let _: () = msg_send![
            &*manager,
            setEventHandler: &*handler,
            andSelector: sel!(handleGetURLEvent:withReplyEvent:),
            forEventClass: GET_URL,
            andEventID: GET_URL
        ];
        // The manager doesn't retain its handler; keep it for the app's life.
        std::mem::forget(handler);
    }
}
