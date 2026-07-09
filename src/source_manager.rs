use windows::Win32::Foundation::{BOOL, HWND, LPARAM, RECT};
use windows::Win32::Graphics::Gdi::{
    EnumDisplayMonitors, GetMonitorInfoW, HMONITOR, MONITORINFOEXW,
    GetDC, ReleaseDC, GetDIBits, DeleteObject, HGDIOBJ, BITMAPINFO, BITMAPINFOHEADER,
    DIB_RGB_COLORS, BI_RGB
};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetWindowLongW, GetWindowTextLengthW, GetWindowTextW, GetWindowThreadProcessId, IsWindowVisible, GWL_EXSTYLE,
    WS_EX_APPWINDOW, WS_EX_TOOLWINDOW, IsIconic, GetIconInfo, ICONINFO
};
use windows::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, QueryFullProcessImageNameW};
use windows::Win32::Foundation::CloseHandle;

#[derive(Clone, Debug, PartialEq)]
pub struct SourceInfo {
    pub id: isize, // HWND or monitor handle as isize
    pub name: String,
    pub details: String,
    pub source_type: String, // "screen" | "window"
    pub hwnd: Option<HWND>,
    pub hmonitor: Option<HMONITOR>,
    pub icon_rgba: Option<Vec<u8>>,
    pub icon_width: u32,
    pub icon_height: u32,
}

unsafe fn hicon_to_rgba(hicon: windows::Win32::UI::WindowsAndMessaging::HICON) -> Option<(Vec<u8>, u32, u32)> {
    let mut icon_info = ICONINFO::default();
    if GetIconInfo(hicon, &mut icon_info).is_err() {
        return None;
    }
    
    let hdc = GetDC(HWND(0));
    if hdc.is_invalid() {
        let _ = DeleteObject(HGDIOBJ(icon_info.hbmColor.0));
        let _ = DeleteObject(HGDIOBJ(icon_info.hbmMask.0));
        return None;
    }

    let mut bitmap_info = BITMAPINFO::default();
    bitmap_info.bmiHeader.biSize = std::mem::size_of::<BITMAPINFOHEADER>() as u32;
    
    // Call GetDIBits with null pointer first to get dimensions
    if GetDIBits(hdc, icon_info.hbmColor, 0, 0, None, &mut bitmap_info, DIB_RGB_COLORS) == 0 {
        let _ = ReleaseDC(HWND(0), hdc);
        let _ = DeleteObject(HGDIOBJ(icon_info.hbmColor.0));
        let _ = DeleteObject(HGDIOBJ(icon_info.hbmMask.0));
        return None;
    }
    
    let width = bitmap_info.bmiHeader.biWidth;
    let height = bitmap_info.bmiHeader.biHeight.abs();
    
    // Request 32-bit RGBA top-down
    bitmap_info.bmiHeader.biHeight = -height; 
    bitmap_info.bmiHeader.biBitCount = 32;
    bitmap_info.bmiHeader.biCompression = BI_RGB.0;
    
    let buf_size = (width * height * 4) as usize;
    let mut buf = vec![0u8; buf_size];
    
    let lines = GetDIBits(
        hdc,
        icon_info.hbmColor,
        0,
        height as u32,
        Some(buf.as_mut_ptr() as *mut _),
        &mut bitmap_info,
        DIB_RGB_COLORS,
    );
    
    let _ = ReleaseDC(HWND(0), hdc);
    let _ = DeleteObject(HGDIOBJ(icon_info.hbmColor.0));
    let _ = DeleteObject(HGDIOBJ(icon_info.hbmMask.0));
    
    if lines == 0 {
        return None;
    }
    
    // Convert BGRA to RGBA
    for chunk in buf.chunks_exact_mut(4) {
        let b = chunk[0];
        let r = chunk[2];
        chunk[0] = r;
        chunk[2] = b;
    }
    
    Some((buf, width as u32, height as u32))
}

unsafe extern "system" fn enum_windows_callback(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let list = &mut *(lparam.0 as *mut Vec<SourceInfo>);
    
    // Check visibility & iconic/minimized status
    let visible = IsWindowVisible(hwnd).as_bool();
    let minimized = IsIconic(hwnd).as_bool();
    if !visible && !minimized {
        return true.into();
    }
    
    // Get window title
    let title_len = GetWindowTextLengthW(hwnd);
    if title_len == 0 {
        return true.into();
    }
    
    let mut title = vec![0u16; (title_len + 1) as usize];
    let copied = GetWindowTextW(hwnd, &mut title);
    if copied == 0 {
        return true.into();
    }
    let title_str = String::from_utf16_lossy(&title[0..copied as usize]);
    
    // Filter tool windows that are not app windows
    let ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE) as u32;
    if (ex_style & WS_EX_TOOLWINDOW.0) != 0 && (ex_style & WS_EX_APPWINDOW.0) == 0 {
        return true.into();
    }
    
    // Filter common system processes
    let mut pid = 0u32;
    GetWindowThreadProcessId(hwnd, Some(&mut pid));
    let mut process_name = String::new();
    if let Ok(handle) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
        let mut path_buf = vec![0u16; 260];
        let mut size = 260;
        let pwstr = windows::core::PWSTR(path_buf.as_mut_ptr());
        if QueryFullProcessImageNameW(handle, windows::Win32::System::Threading::PROCESS_NAME_FORMAT(0), pwstr, &mut size).is_ok() {
            let path_str = String::from_utf16_lossy(&path_buf[0..size as usize]);
            if let Some(filename) = path_str.split('\\').last() {
                process_name = filename.to_lowercase();
            }
        }
        let _ = CloseHandle(handle);
    }
    
    let blacklist = [
        "explorer.exe", "searchhost.exe", "shellexperiencehost.exe", 
        "applicationframehost.exe", "systemsettings.exe", "lockapp.exe",
        "overlayr.exe", "taskmgr.exe", "cmd.exe", "powershell.exe",
    ];
    if blacklist.contains(&process_name.as_str()) {
        return true.into();
    }
    
    let details = if minimized {
        format!("{} [Minimized]", process_name)
    } else {
        process_name
    };
    
    let mut icon_rgba = None;
    let mut icon_width = 0;
    let mut icon_height = 0;
    
    // Extract icon
    {
        use windows::Win32::UI::WindowsAndMessaging::{
            SendMessageW, WM_GETICON, GetClassLongPtrW, GCLP_HICONSM, GCLP_HICON, HICON
        };
        use windows::Win32::Foundation::{WPARAM, LPARAM};
        
        let mut hicon_ptr = SendMessageW(hwnd, WM_GETICON, WPARAM(2), LPARAM(0)).0;
        if hicon_ptr == 0 {
            hicon_ptr = SendMessageW(hwnd, WM_GETICON, WPARAM(0), LPARAM(0)).0;
        }
        if hicon_ptr == 0 {
            hicon_ptr = SendMessageW(hwnd, WM_GETICON, WPARAM(1), LPARAM(0)).0;
        }
        if hicon_ptr == 0 {
            hicon_ptr = GetClassLongPtrW(hwnd, GCLP_HICONSM) as isize;
        }
        if hicon_ptr == 0 {
            hicon_ptr = GetClassLongPtrW(hwnd, GCLP_HICON) as isize;
        }
        
        if hicon_ptr != 0 {
            let hicon = HICON(hicon_ptr);
            if let Some((rgba, w, h)) = hicon_to_rgba(hicon) {
                icon_rgba = Some(rgba);
                icon_width = w;
                icon_height = h;
            }
        }
    }
    
    list.push(SourceInfo {
        id: hwnd.0 as isize,
        name: title_str,
        details,
        source_type: "window".to_string(),
        hwnd: Some(hwnd),
        hmonitor: None,
        icon_rgba,
        icon_width,
        icon_height,
    });
    
    true.into()
}

unsafe extern "system" fn enum_monitors_callback(
    hmonitor: HMONITOR,
    _: windows::Win32::Graphics::Gdi::HDC,
    _: *mut RECT,
    lparam: LPARAM,
) -> BOOL {
    let list = &mut *(lparam.0 as *mut Vec<SourceInfo>);
    
    let mut info = MONITORINFOEXW::default();
    info.monitorInfo.cbSize = std::mem::size_of::<MONITORINFOEXW>() as u32;
    if GetMonitorInfoW(hmonitor, &mut info as *mut _ as *mut _).as_bool() {
        let name_len = info.szDevice.iter().position(|&x| x == 0).unwrap_or(0);
        let name = String::from_utf16_lossy(&info.szDevice[0..name_len]);
        let rect = info.monitorInfo.rcMonitor;
        let details = format!("{}x{} at ({},{})", rect.right - rect.left, rect.bottom - rect.top, rect.left, rect.top);
        
        list.push(SourceInfo {
            id: hmonitor.0 as isize,
            name: format!("Display {}", list.len() + 1),
            details,
            source_type: "screen".to_string(),
            hwnd: None,
            hmonitor: Some(hmonitor),
            icon_rgba: None,
            icon_width: 0,
            icon_height: 0,
        });
    }
    true.into()
}

pub fn list_windows() -> Vec<SourceInfo> {
    let mut list = Vec::new();
    unsafe {
        let _ = EnumWindows(Some(enum_windows_callback), LPARAM(&mut list as *mut _ as isize));
    }
    list
}

pub fn list_monitors() -> Vec<SourceInfo> {
    let mut list = Vec::new();
    unsafe {
        let _ = EnumDisplayMonitors(
            None,
            None,
            Some(enum_monitors_callback),
            LPARAM(&mut list as *mut _ as isize),
        );
    }
    list
}
