import pygetwindow as gw # type: ignore
import screeninfo
from typing import List, Dict, Any
import ctypes
from ctypes import wintypes
from ..utils.singleton import SingletonMeta

# For getting process name from HWND
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

def get_process_name(hwnd):
    """Get process name from window handle."""
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                exe_path = (ctypes.c_wchar * 260)()
                size = wintypes.DWORD(260)
                if kernel32.QueryFullProcessImageNameW(handle, 0, exe_path, ctypes.byref(size)):
                    return exe_path.value.split('\\')[-1].lower()
            finally:
                kernel32.CloseHandle(handle)
    except:
        pass
    return ""

class SourceManager(metaclass=SingletonMeta):
    
    def get_monitors(self) -> List[Dict[str, Any]]:
        print("Getting monitors...")
        monitors = []
        try:
            mss_mons = screeninfo.get_monitors()
            print(f"Found {len(mss_mons)} monitors")
            for i, m in enumerate(mss_mons):
                monitors.append({
                    "id": i,
                    "name": f"Display {i+1}",
                    "details": f"{m.width}x{m.height} at ({m.x},{m.y})",
                    "type": "screen",
                    "monitor_obj": m
                })
        except Exception as e:
            print(f"Error listing monitors: {e}")
        return monitors
    
    def get_windows(self) -> List[Dict[str, Any]]:
        print("Getting windows...")
        windows = []
        try:
            all_wins = gw.getAllWindows()
            print(f"Found {len(all_wins)} total windows (raw)")
            for w in all_wins:
                if w.title and w.visible and w.width > 0 and w.height > 0:
                    hwnd = getattr(w, '_hWnd', None)
                    process_name = get_process_name(hwnd) if hwnd else ""
                    
                    # Build display name with process info
                    display_name = w.title
                    if "brave" in process_name:
                        display_name = f"🦁 {w.title}"  # Brave browser icon
                    elif "chrome" in process_name:
                        display_name = f"🌐 {w.title}"
                    elif "firefox" in process_name:
                        display_name = f"🦊 {w.title}"
                    
                    windows.append({
                        "id": hwnd or id(w),
                        "title": display_name,
                        "original_title": w.title,
                        "process": process_name,
                        "type": "window", 
                        "window_obj": w
                    })
            print(f"Filtered to {len(windows)} visible windows")
        except Exception as e:
            print(f"Error listing windows: {e}")
        return windows
