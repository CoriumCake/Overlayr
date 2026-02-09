import pygetwindow as gw # type: ignore
import screeninfo
from typing import List, Dict, Any
import ctypes
from ctypes import wintypes
from ..utils.singleton import SingletonMeta

# For getting process name from HWND
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Window Styles
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

SYSTEM_PROCESS_BLACKLIST = {
    "explorer.exe", "searchhost.exe", "shellexperiencehost.exe", 
    "applicationframehost.exe", "systemsettings.exe", "lockapp.exe",
    "overlayr.exe", "taskmgr.exe", "python.exe", "cmd.exe", "powershell.exe",
    "yasb.exe", "widgets.exe", "startmenuexperiencehost.exe"
}

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

def is_system_window(hwnd):
    """Check if window is likely a system/background window."""
    try:
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # Tool windows are usually background/utility
        if ex_style & WS_EX_TOOLWINDOW and not (ex_style & WS_EX_APPWINDOW):
            return True
            
        # Check title for common system strings
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0: return True
        
        title = (ctypes.c_wchar * (length + 1))()
        user32.GetWindowTextW(hwnd, title, length + 1)
        title_str = title.value.lower()
        
        system_titles = ["item-index", "settings", "microsoft text input", "program manager"]
        if any(st in title_str for st in system_titles):
            return True
            
    except:
        pass
    return False

class SourceManager(metaclass=SingletonMeta):
    def __init__(self):
        self._last_hwnd_set = set()

    def has_window_list_changed(self) -> bool:
        """Lightweight check to see if windows was opened/closed."""
        try:
            # Get all visible windows with titles (minimum criteria)
            all_wins = gw.getAllWindows()
            current_hwnds = {getattr(w, '_hWnd', 0) for w in all_wins if w.title and w.visible}
            
            if current_hwnds != self._last_hwnd_set:
                return True
        except:
            pass
        return False
    
    def get_monitors(self) -> List[Dict[str, Any]]:

        monitors = []
        try:
            mss_mons = screeninfo.get_monitors()

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

        windows = []
        try:
            all_wins = gw.getAllWindows()

            for w in all_wins:
                if not (w.title and w.visible and w.width > 50 and w.height > 50):
                    continue
                    
                hwnd = getattr(w, '_hWnd', None)
                if not hwnd: continue
                
                process_name = get_process_name(hwnd)
                
                # Filter blacklist
                if process_name in SYSTEM_PROCESS_BLACKLIST:
                    continue
                    
                # Filter hidden system windows
                if is_system_window(hwnd):
                    continue
                
                # Build display name with process info
                display_name = w.title
                icon = "🪟"
                
                if "brave" in process_name:
                    icon = "🦁"
                elif "chrome" in process_name:
                    icon = "🌐"
                elif "firefox" in process_name:
                    icon = "🦊"
                elif "discord" in process_name:
                    icon = "💬"
                elif any(game in process_name for game in ["steam", "valorant", "genshin", "minecraft"]):
                    icon = "🎮"
                
                # Friendly Name Mapping
                friendly_map = {
                    "brave.exe": "Brave",
                    "chrome.exe": "Google Chrome",
                    "msedge.exe": "Microsoft Edge",
                    "firefox.exe": "Firefox",
                    "discord.exe": "Discord",
                    "notepad.exe": "Notepad",
                    "vlc.exe": "VLC Media Player",
                    "spotify.exe": "Spotify",
                    "code.exe": "VS Code",
                    "steam.exe": "Steam"
                }
                friendly_name = friendly_map.get(process_name, process_name.replace(".exe", "").capitalize())

                windows.append({
                    "id": hwnd,
                    "title": f"{icon} {w.title}",
                    "friendly_name": friendly_name,
                    "original_title": w.title,
                    "process": process_name,
                    "type": "window", 
                    "window_obj": w
                })
            
            # Sort: prioritized apps first (browsers/games)
            windows.sort(key=lambda x: ("🎮" in x["title"] or "🦁" in x["title"] or "🌐" in x["title"]), reverse=True)
            
            # Update state for change detection
            self._last_hwnd_set = {w['id'] for w in windows}
            

        except Exception as e:
            print(f"Error listing windows: {e}")
        return windows
