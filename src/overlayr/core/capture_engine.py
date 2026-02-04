import mss
import mss.tools
from PIL import Image
from typing import Optional, Dict, Any, Tuple
import asyncio
from ..utils.singleton import SingletonMeta
from ..services.source_manager import SourceManager

class CaptureEngine(metaclass=SingletonMeta):
    """
    Handles capturing images from Screens or Windows.
    """
    def __init__(self):
        self.mode = "screen" # screen | window
        self.target = None
        self.target_id = None # monitor_index or window_hwnd/title
        self.current_bbox = None
        self.source_manager = None # Don't init here, circle dep/blocking?
        self.roi = None # (x_ratio, y_ratio, w_ratio, h_ratio) or None

    def set_source(self, mode: str, target: Dict[str, Any]) -> None:
        self.mode = mode
        self.target = target
        self.roi = None # Clear ROI when switching sources

    def set_roi(self, roi: Optional[Tuple[float, float, float, float]]) -> None:
        """Set Region of Interest as ratios (x, y, w, h) of target."""
        self.roi = roi
        
    def capture(self) -> Tuple[Optional[Image.Image], Optional[Dict[str, int]]]:
        """
        Capture the current target.
        Returns (image, bounds_dict) where bounds_dict has keys: left, top, width, height
        """
        try:
            with mss.mss() as sct:
                if self.mode == "screen":
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
                    # Re-fetch window to get fresh position
                    import pygetwindow as gw
                    hwnd = self.target.get('id')
                    
                    # Find window by hwnd
                    w = None
                    try:
                        for win in gw.getAllWindows():
                            if getattr(win, '_hWnd', None) == hwnd:
                                w = win
                                break
                    except:
                        w = self.target.get('window_obj')
                    
                    if w is None:
                        w = self.target.get('window_obj')
                    
                    if w and w.width > 0 and w.height > 0:
                        target_bbox = {"top": w.top, "left": w.left, "width": w.width, "height": w.height}
                        
                        # Apply ROI if set
                        if self.roi:
                            rx, ry, rw, rh = self.roi
                            target_bbox = {
                                "left": w.left + int(rx * w.width),
                                "top": w.top + int(ry * w.height),
                                "width": int(rw * w.width),
                                "height": int(rh * w.height)
                            }
                            
                        img = sct.grab(target_bbox)
                        return Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX"), target_bbox
                    
        except Exception as e:
            print(f"Capture error: {e}")
            return None, None
        return None, None

    def get_thumbnail(self, mode: str, target: Dict[str, Any], size: Tuple[int, int] = (150, 100)) -> Any:
        try:
            with mss.mss() as sct:
                img = None
                
                if mode == "screen":
                    monitor_idx = target['id'] + 1
                    if monitor_idx < len(sct.monitors):
                        img_sct = sct.grab(sct.monitors[monitor_idx])
                        img = Image.frombytes("RGB", img_sct.size, img_sct.bgra, "raw", "BGRX")
                
                elif mode == "window":
                    w = target['window_obj']
                    if w.width > 0 and w.height > 0:
                         bbox = {"top": w.top, "left": w.left, "width": w.width, "height": w.height}
                         img_sct = sct.grab(bbox)
                         img = Image.frombytes("RGB", img_sct.size, img_sct.bgra, "raw", "BGRX")
                
                if img:
                    return img.resize(size, Image.Resampling.LANCZOS)
                
        except Exception as e:
            pass
        return None
