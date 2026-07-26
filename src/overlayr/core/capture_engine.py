from typing import Any, Dict, Optional, Tuple

import mss
import mss.tools
from PIL import Image

from ..utils.display_utils import (
    resolve_monitor_bounds,
    select_capture_bounds,
)
from ..utils.singleton import SingletonMeta
from .wgc_capture import WGCCapture


class CaptureEngine(metaclass=SingletonMeta):
    """
    Handles capturing images from Screens or Windows.
    Now supports WGC for Background Application Capture.
    """
    def __init__(self):
        self.mode = "screen" # screen | window
        self.target = None
        self.target_id = None # monitor_index or window_hwnd/title
        self.current_bbox = None
        self.source_manager = None # Don't init here, circle dep/blocking?
        self.roi = None # (x_ratio, y_ratio, w_ratio, h_ratio) or None
        
        # WGC Instance
        self.wgc = WGCCapture()

    def set_source(self, mode: str, target: Dict[str, Any]) -> None:
        self.mode = mode
        self.target = target
        self.roi = None # Clear ROI when switching sources
        
        # Stop WGC if switching away or changing target
        self.wgc.stop()
        
        if mode == "window":
            # Start WGC session
            self.wgc.start(target)



    def set_roi(self, roi: Optional[Tuple[float, float, float, float]]) -> None:
        """Set Region of Interest as ratios (x, y, w, h) of target."""
        self.roi = roi

    def get_source_bounds(
        self,
        mode: Optional[str] = None,
        target: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, int]]:
        """Return full source bounds in physical virtual-screen pixels."""
        source_mode = mode or self.mode
        source_target = target or self.target
        if not source_target:
            return None

        if source_mode == "screen":
            try:
                with mss.mss() as sct:
                    return resolve_monitor_bounds(source_target, sct.monitors)
            except Exception:
                stored = source_target.get("bounds")
                return dict(stored) if stored else None

        if source_mode != "window":
            return None

        hwnd = source_target.get("id")
        if not hwnd:
            return None

        window_obj = source_target.get("window_obj")
        fallback_bounds = None
        if window_obj is not None:
            try:
                fallback_bounds = {
                    "left": int(window_obj.left),
                    "top": int(window_obj.top),
                    "width": int(window_obj.width),
                    "height": int(window_obj.height),
                }
            except Exception:
                fallback_bounds = None

        visible_bounds = self._get_visible_window_bounds(hwnd)
        window_bounds = self._get_window_bounds(hwnd) or fallback_bounds
        client_bounds = self._get_client_bounds(hwnd)

        frame = None
        if self.mode == "window" and self.target:
            if self.target.get("id") == hwnd:
                frame = self.wgc.get_latest_frame()
        if frame is not None:
            matched = select_capture_bounds(
                frame.width,
                frame.height,
                visible_bounds,
                window_bounds,
                client_bounds,
            )
            if matched:
                return matched

        return visible_bounds or window_bounds or client_bounds

    @staticmethod
    def _get_client_bounds(hwnd: int) -> Optional[Dict[str, int]]:
        """Get the window client area's screen-space pixel bounds."""
        try:
            import ctypes
            from ctypes import wintypes

            class POINT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            rect = RECT()
            origin = POINT(0, 0)
            user32 = ctypes.windll.user32
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return None
            if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
                return None

            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return None

            return {
                "left": int(origin.x),
                "top": int(origin.y),
                "width": int(width),
                "height": int(height),
            }
        except Exception:
            return None

    @staticmethod
    def _get_window_bounds(hwnd: int) -> Optional[Dict[str, int]]:
        """Get DPI-aware outer window bounds from Win32."""
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            user32 = ctypes.windll.user32
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None

            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return None

            return {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(width),
                "height": int(height),
            }
        except Exception:
            return None

    @staticmethod
    def _get_visible_window_bounds(hwnd: int) -> Optional[Dict[str, int]]:
        """Get the physical DWM frame captured by Graphics Capture."""
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

            # DWMWA_EXTENDED_FRAME_BOUNDS excludes invisible resize borders
            # and corresponds to the visible frame returned by WGC.
            result = dwmapi.DwmGetWindowAttribute(
                hwnd,
                9,
                ctypes.byref(rect),
                ctypes.sizeof(rect),
            )
            if result != 0:
                return None

            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return None

            return {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(width),
                "height": int(height),
            }
        except Exception:
            return None
        
    def capture(
        self,
        *,
        apply_roi: bool = True,
    ) -> Tuple[Optional[Image.Image], Optional[Dict[str, int]]]:
        """
        Capture the current target.
        Returns (image, bounds_dict) where bounds_dict has keys: left, top, width, height
        """
        try:
            if self.mode == "screen":
                with mss.mss() as sct:
                    monitor_bbox = resolve_monitor_bounds(self.target, sct.monitors)
                    
                    if monitor_bbox:
                        target_bbox = monitor_bbox.copy()
                        
                        # Apply ROI if set
                        if apply_roi and self.roi:
                            rx, ry, rw, rh = self.roi
                            target_bbox = {
                                "left": monitor_bbox['left'] + int(rx * monitor_bbox['width']),
                                "top": monitor_bbox['top'] + int(ry * monitor_bbox['height']),
                                "width": int(rw * monitor_bbox['width']),
                                "height": int(rh * monitor_bbox['height'])
                            }
                        
                        img = sct.grab(target_bbox)
                        return Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX"), target_bbox
            
            elif self.mode == "window":
                # Use WGC for Background Capture
                # WGC returns full window image. We might need to crop ROI or window borders if client area matches?
                # For simplicty, WGC captures client area usually (if using CreateForWindow). 
                # Actually standard CreateForWindow captures client area + non-client?
                # Usually entire window including borders.
                
                img = self.wgc.get_latest_frame()
                if img is not None:
                    # Get Window Position from WGC? No, WGC returns image. 
                    # We still need Window Position to draw the Overlay correctly!
                    # So we still need gw/pygetwindow to track the "Target Location" on screen!
                    
                    import pygetwindow as gw
                    hwnd = self.target.get('id')
                    
                    # Retrieve current bounds
                    w_obj = None
                    try:
                       w_obj = gw.Window(hwnd) # pygetwindow might handle hwnd directly? No, it wraps FindWindow.
                       # Optimization: We already have window object in target?
                       # target['window_obj'] is a stale object? gw objects update?
                       w_obj = self.target.get('window_obj')
                       # Check if valid/visible
                       if not w_obj.isActive and not w_obj.visible: # Is active not needed, just visible
                           pass
                    except: pass
                    
                    if w_obj:
                         fallback_bbox = {
                             "left": w_obj.left,
                             "top": w_obj.top,
                             "width": w_obj.width,
                             "height": w_obj.height,
                         }
                         window_bbox = self._get_window_bounds(hwnd) or fallback_bbox
                         client_bbox = self._get_client_bounds(hwnd)
                         visible_bbox = self._get_visible_window_bounds(hwnd)
                         target_bbox = select_capture_bounds(
                             img.width,
                             img.height,
                             visible_bbox,
                             window_bbox,
                             client_bbox,
                         )
                         if target_bbox is None:
                             return None, None
                         
                         # If ROI is set, crop the WGC image
                         if apply_roi and self.roi:
                             rx, ry, rw, rh = self.roi
                             
                             # Crop coordinates relative to image (0,0 is top-left of image)
                             # Image size check
                             iw, ih = img.size
                             
                             crop_x = int(rx * iw)
                             crop_y = int(ry * ih)
                             crop_w = int(rw * iw)
                             crop_h = int(rh * ih)
                             
                             img = img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
                             
                             # Update target_bbox to match screen position of ROI
                             target_bbox = {
                                 "left": target_bbox["left"] + crop_x,
                                 "top": target_bbox["top"] + crop_y,
                                 "width": crop_w,
                                 "height": crop_h
                             }
                             
                         return img, target_bbox
                    
                return None, None
                    
        except Exception as e:
            print(f"Capture error: {e}")
            return None, None
        return None, None

    def capture_full_source(
        self,
    ) -> Tuple[Optional[Image.Image], Optional[Dict[str, int]]]:
        """Capture one full-source frame without changing the active ROI."""
        return self.capture(apply_roi=False)

    def get_thumbnail(self, mode: str, target: Dict[str, Any], size: Tuple[int, int] = (150, 100)) -> Any:
        try:
            if mode == "screen":
                with mss.mss() as sct:
                    monitor_bbox = resolve_monitor_bounds(target, sct.monitors)
                    if monitor_bbox:
                        img_sct = sct.grab(monitor_bbox)
                        img = Image.frombytes("RGB", img_sct.size, img_sct.bgra, "raw", "BGRX")
                        return img.resize(size, Image.Resampling.LANCZOS)
                
            elif mode == "window":
                import ctypes
                from ctypes import wintypes
                
                hwnd = target.get('id')
                if not hwnd: return None
                
                # Get Window dimensions
                rect = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                
                if w <= 0 or h <= 0: return None
                
                # Create Device Contexts
                hwnd_dc = ctypes.windll.user32.GetWindowDC(hwnd)
                mfc_dc = ctypes.windll.gdi32.CreateCompatibleDC(hwnd_dc)
                save_bit_map = ctypes.windll.gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
                ctypes.windll.gdi32.SelectObject(mfc_dc, save_bit_map)
                
                # PrintWindow captures the window content even if obscured
                # PW_RENDERFULLCONTENT = 0x00000002
                ctypes.windll.user32.PrintWindow(hwnd, mfc_dc, 2)
                
                # Convert to PIL
                bmp_info = bytes(40) # BITMAPINFOHEADER is 40 bytes
                # We'll use a slightly more direct way to get bits if PrintWindow worked
                # For simplicity in this env, we use BitBlt as backup or just direct
                
                # This is a bit complex for raw ctypes, let's use a simpler PIL capture if PrintWindow fails
                # OR use the buffer directly.
                
                bitmap_bits = ctypes.create_string_buffer(w * h * 4)
                ctypes.windll.gdi32.GetBitmapBits(save_bit_map, w * h * 4, bitmap_bits)
                
                img = Image.frombuffer("RGBA", (w, h), bitmap_bits, "raw", "BGRA", 0, 1)
                img = img.convert("RGB")
                
                # Cleanup
                ctypes.windll.gdi32.DeleteObject(save_bit_map)
                ctypes.windll.gdi32.DeleteDC(mfc_dc)
                ctypes.windll.user32.ReleaseDC(hwnd, hwnd_dc)
                
                return img.resize(size, Image.Resampling.LANCZOS)
                
        except Exception as e:
            print(f"Thumbnail error: {e}")
        return None
