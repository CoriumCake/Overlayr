import mss
import mss.tools
from PIL import Image
from typing import Optional, Dict, Any, Tuple
import asyncio
import time
from ..utils.singleton import SingletonMeta
from ..services.source_manager import SourceManager
from .wgc_capture import WGCCapture


WINDOW_FRAME_RETRIES = 3
WINDOW_FRAME_RETRY_DELAY_SECONDS = 0.01


def _is_solid_white(image: Image.Image) -> bool:
    """Return whether a captured frame contains only opaque white pixels.

    WGC can briefly return a valid frame object whose surface has not received
    the window contents yet. Those frames show up as an entirely white image.
    Keep this check deliberately strict so a genuinely mostly-white window is
    still accepted.
    """
    if image.width == 0 or image.height == 0:
        return True

    extrema = image.convert("RGB").getextrema()
    return all(channel_min == 255 and channel_max == 255 for channel_min, channel_max in extrema)

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
        
    def capture(self) -> Tuple[Optional[Image.Image], Optional[Dict[str, int]]]:
        """
        Capture the current target.
        Returns (image, bounds_dict) where bounds_dict has keys: left, top, width, height
        """
        try:
            if self.mode == "screen":
                with mss.mss() as sct:
                    monitor_idx = self.target['id'] + 1
                    
                    if monitor_idx < len(sct.monitors):
                        mon = sct.monitors[monitor_idx]
                        
                        target_bbox = {"left": mon['left'], "top": mon['top'], "width": mon['width'], "height": mon['height']}
                        
                        # Apply ROI if set
                        if self.roi:
                            rx, ry, rw, rh = self.roi
                            target_bbox = {
                                "left": mon['left'] + int(rx * mon['width']),
                                "top": mon['top'] + int(ry * mon['height']),
                                "width": int(rw * mon['width']),
                                "height": int(rh * mon['height'])
                            }
                        
                        img = sct.grab(target_bbox)
                        return Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX"), target_bbox
            
            elif self.mode == "window":
                # Use WGC for Background Capture
                # WGC returns full window image. We might need to crop ROI or window borders if client area matches?
                # For simplicty, WGC captures client area usually (if using CreateForWindow). 
                # Actually standard CreateForWindow captures client area + non-client?
                # Usually entire window including borders.
                
                img = None
                for attempt in range(WINDOW_FRAME_RETRIES):
                    candidate = self.wgc.get_latest_frame()
                    if candidate is None:
                        break
                    if not _is_solid_white(candidate):
                        img = candidate
                        break

                    # A white frame is commonly produced while WGC is
                    # attaching to a window or rebuilding its surface after a
                    # resize. Give the compositor a moment to publish the
                    # next frame before abandoning this capture tick.
                    if attempt + 1 < WINDOW_FRAME_RETRIES:
                        time.sleep(WINDOW_FRAME_RETRY_DELAY_SECONDS)

                if img:
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
                         target_bbox = {"left": w_obj.left, "top": w_obj.top, "width": w_obj.width, "height": w_obj.height}
                         
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
                                 "left": w_obj.left + crop_x,
                                 "top": w_obj.top + crop_y,
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
                    monitor_idx = target['id'] + 1
                    if monitor_idx < len(sct.monitors):
                        img_sct = sct.grab(sct.monitors[monitor_idx])
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
