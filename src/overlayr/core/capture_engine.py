import mss
import mss.tools
from PIL import Image
from typing import Optional, Dict, Any, Tuple
import asyncio
from ..utils.singleton import SingletonMeta
from ..services.source_manager import SourceManager
from ..utils.display_utils import resolve_monitor_bounds
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
        
    def capture(self) -> Tuple[Optional[Image.Image], Optional[Dict[str, int]]]:
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
                        if self.roi:
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
                         window_bbox = {"left": w_obj.left, "top": w_obj.top, "width": w_obj.width, "height": w_obj.height}

                         # WGC CreateForWindow normally returns the client
                         # area, not the outer title-bar frame. Use the same
                         # screen-space origin for the overlay.
                         client_bbox = self._get_client_bounds(hwnd)
                         if client_bbox:
                             window_area_error = abs(img.width - window_bbox["width"]) + abs(img.height - window_bbox["height"])
                             client_area_error = abs(img.width - client_bbox["width"]) + abs(img.height - client_bbox["height"])
                             target_bbox = client_bbox if client_area_error <= window_area_error else window_bbox
                         else:
                             target_bbox = window_bbox

                         # The canvas coordinates are image pixels, so make
                         # the overlay geometry match the captured image.
                         target_bbox["width"] = img.width
                         target_bbox["height"] = img.height
                         
                         # If ROI is set, crop the WGC image
                         if self.roi:
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
                
                import numpy as np
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
