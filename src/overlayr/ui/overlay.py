import tkinter as tk
import customtkinter as ctk # type: ignore
import platform
import re

from ..utils.display_utils import tk_pixel_font

class ResultOverlay:
    def __init__(self, root: tk.Tk, include_in_capture: bool = False):
        self.root = root
        self.include_in_capture = bool(include_in_capture)
        self._is_visible = False
        self._capture_was_visible = False
        self._capture_affinity_changed = False
        self._hwnd = None
        # Keep references alive for Tk photo images until the next redraw.
        self._photo_images = []
        self.top = tk.Toplevel(root)
        self.top.title("Translation Overlay")
        self.top.overrideredirect(True) # Remove window decorations
        self.top.attributes("-topmost", True)
        # Keep source-background patches opaque so the original glyphs cannot
        # show through after they have been cleared.
        self.top.attributes("-alpha", 1.0)
        
        # Initial dummy geometry
        self.top.geometry("400x100+100+100")
        
        # Background color for transparency trick
        # Windows supports specific color transparency
        if platform.system() == "Windows":
            # Do not use black as the transparent key: black can be a valid
            # sampled manga/game background and must remain paintable.
            self.transparent_key = '#ff00ff'
            self.top.configure(background=self.transparent_key)
            self.top.attributes('-transparentcolor', self.transparent_key)
            self.bg_color = self.transparent_key
        else:
            self.top.configure(background='gray')
            self.top.attributes('-alpha', 0.5) 
            self.bg_color = 'gray'

        # Fullscreen transparent canvas
        self.canvas = tk.Canvas(self.top, bg=self.bg_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        # Debug console (top left)
        self.debug_text_id = self.canvas.create_text(
            10, 10,
            text="",
            anchor="nw",
            fill="cyan",
            font=("Consolas", 14, "bold"),
            width=1200,
            tags="debug"
        )
        
        self.top.withdraw() # Hide initially
        
        # Make the overlay "Invisible to Screen Capture" on Windows
        # This prevents the OCR from reading our own translated text!
        if platform.system() == "Windows":
             self._apply_window_styles()
                
    def _apply_window_styles(self):
        try:
            import ctypes
            from ctypes import wintypes
            
            # Ensure window is partially mapped so it has an HWND
            self.top.update_idletasks()
            
            # Get window handle
            hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id())
            if not hwnd:
                hwnd = self.top.winfo_id()
                
            # 1. Set Window Display Affinity (hidden from screenshots by default)
            self.set_capture_inclusion(self.include_in_capture)

            # 2. Set Extended Window Styles (Click-Through & No-Focus)
            # WS_EX_TRANSPARENT (0x20): Events pass through window
            # WS_EX_LAYERED (0x80000): Required for transparency
            # WS_EX_TOOLWINDOW (0x80): Hides from Alt-Tab
            # WS_EX_NOACTIVATE (0x08000000): Prevents focus stealing
            
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED     = 0x00080000
            WS_EX_TOOLWINDOW  = 0x00000080
            WS_EX_NOACTIVATE  = 0x08000000
            
            # Get current style
            current_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            
            # Add new styles
            new_style = current_style | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            
            # Set new style
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            print(f"Overlay Click-Through: ENABLED (Style: {hex(new_style)})", flush=True)
                
        except Exception as e:
            print(f"Overlay Style Error: {e}", flush=True)

    def set_capture_inclusion(self, include: bool):
        """Control whether Windows screenshots include the translation overlay."""
        self.include_in_capture = bool(include)

        if platform.system() != "Windows":
            return

        try:
            import ctypes
            from ctypes import wintypes

            self.top.update_idletasks()
            hwnd = self._get_hwnd()

            # WDA_NONE allows normal screenshots; WDA_EXCLUDEFROMCAPTURE hides
            # this window from Windows capture APIs.
            affinity = 0x00000000 if self.include_in_capture else 0x00000011
            user32 = ctypes.windll.user32
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

            success = user32.SetWindowDisplayAffinity(hwnd, affinity)
            state = "INCLUDED" if self.include_in_capture else "EXCLUDED"
            if success:
                print(f"Overlay Capture: {state} (HWND: {hwnd})", flush=True)
            else:
                print(f"Overlay Capture: FAILED to set {state} (HWND: {hwnd})", flush=True)
        except Exception as e:
            print(f"Overlay Capture Setting Error: {e}", flush=True)

    def _get_hwnd(self):
        """Return and cache the overlay HWND for inexpensive capture toggles."""
        if self._hwnd:
            return self._hwnd

        import ctypes
        hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id())
        if not hwnd:
            hwnd = self.top.winfo_id()
        self._hwnd = hwnd
        return hwnd

    def _set_capture_affinity(self, include: bool):
        """Change capture affinity without hiding the Tk window."""
        if platform.system() != "Windows":
            return False

        try:
            import ctypes
            from ctypes import wintypes

            hwnd = self._get_hwnd()
            affinity = 0x00000000 if include else 0x00000011
            user32 = ctypes.windll.user32
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
            return bool(user32.SetWindowDisplayAffinity(hwnd, affinity))
        except Exception:
            return False
        
    @staticmethod
    def _inpaint_source_region(source_image, clear_box, ink_box):
        """Return an image patch with the source glyphs removed.

        ``clear_box`` is in overlay/canvas coordinates while ``source_image``
        is the captured ROI whose origin is the overlay origin.  Keeping this
        operation on the captured pixels preserves manga bubbles, gradients,
        grid lines, and other textured backgrounds instead of replacing them
        with one sampled RGB value.
        """
        if source_image is None:
            return None

        try:
            import cv2
            import numpy as np
            from PIL import Image

            image = source_image.convert("RGB")
            width, height = image.size
            x1, y1, x2, y2 = (int(round(value)) for value in clear_box)
            crop_x1 = max(0, x1)
            crop_y1 = max(0, y1)
            crop_x2 = min(width, x2)
            crop_y2 = min(height, y2)
            if crop_x2 <= crop_x1 or crop_y2 <= crop_y1:
                return None

            crop = np.asarray(image)[crop_y1:crop_y2, crop_x1:crop_x2].copy()
            mask = np.zeros(crop.shape[:2], dtype=np.uint8)
            if len(ink_box) == 4 and not isinstance(ink_box[0], (list, tuple, dict)):
                ink_boxes = [ink_box]
            else:
                ink_boxes = ink_box

            has_mask = False
            for region in ink_boxes:
                if isinstance(region, dict):
                    box = region.get("bbox")
                    region_mask = region.get("mask")
                else:
                    box = region
                    region_mask = None
                if not box:
                    continue

                box_x1, box_y1, box_x2, box_y2 = (
                    int(round(value)) for value in box
                )
                padding = 0 if region_mask is not None else 1
                ix1 = max(crop_x1, box_x1 - padding)
                iy1 = max(crop_y1, box_y1 - padding)
                ix2 = min(crop_x2, box_x2 + padding)
                iy2 = min(crop_y2, box_y2 + padding)
                if ix2 <= ix1 or iy2 <= iy1:
                    continue

                target = mask[
                    iy1 - crop_y1:iy2 - crop_y1,
                    ix1 - crop_x1:ix2 - crop_x1,
                ]
                if region_mask is None:
                    target[:] = 255
                    has_mask = True
                    continue

                region_mask = np.asarray(region_mask, dtype=np.uint8)
                mask_x1 = ix1 - box_x1
                mask_y1 = iy1 - box_y1
                mask_x2 = mask_x1 + (ix2 - ix1)
                mask_y2 = mask_y1 + (iy2 - iy1)
                if (
                    mask_x1 < 0
                    or mask_y1 < 0
                    or mask_x2 > region_mask.shape[1]
                    or mask_y2 > region_mask.shape[0]
                ):
                    continue
                source_mask = region_mask[mask_y1:mask_y2, mask_x1:mask_x2]
                np.maximum(target, source_mask, out=target)
                has_mask = has_mask or bool(np.any(source_mask))

            if not has_mask:
                return None
            # A small radius retains nearby line art while reconstructing the
            # background underneath the original glyphs.
            cleaned = cv2.inpaint(crop, mask, 3, cv2.INPAINT_TELEA)
            return Image.fromarray(cleaned)
        except Exception:
            # The solid-colour fallback below keeps older/minimal installs
            # working when Pillow/OpenCV image conversion is unavailable.
            return None

    def _draw_source_patch(self, source_image, clear_box, ink_box, offset_x, offset_y):
        """Draw a cleaned source crop and return whether it was drawn."""
        if source_image is None:
            return False

        try:
            from PIL import ImageTk

            if len(ink_box) == 4 and not isinstance(ink_box[0], (list, tuple, dict)):
                source_ink_boxes = [ink_box]
            else:
                source_ink_boxes = ink_box

            normalized_regions = []
            for region in source_ink_boxes:
                if isinstance(region, dict):
                    box = region.get("bbox")
                    if not box:
                        continue
                    normalized_regions.append({
                        "bbox": (
                            box[0] - offset_x,
                            box[1] - offset_y,
                            box[2] - offset_x,
                            box[3] - offset_y,
                        ),
                        "mask": region.get("mask"),
                    })
                else:
                    normalized_regions.append((
                        region[0] - offset_x,
                        region[1] - offset_y,
                        region[2] - offset_x,
                        region[3] - offset_y,
                    ))

            patch = self._inpaint_source_region(
                source_image,
                (
                    clear_box[0] - offset_x,
                    clear_box[1] - offset_y,
                    clear_box[2] - offset_x,
                    clear_box[3] - offset_y,
                ),
                normalized_regions,
            )
            if patch is None:
                return False

            patch_x = max(0, int(round(clear_box[0])))
            patch_y = max(0, int(round(clear_box[1])))
            photo = ImageTk.PhotoImage(patch)
            self._photo_images.append(photo)
            self.canvas.create_image(
                patch_x,
                patch_y,
                image=photo,
                anchor="nw",
                tags="result",
            )
            return True
        except Exception:
            return False

    def show_results(
        self,
        results: list,
        offset_x: int,
        offset_y: int,
        visible: bool = True,
        source_image=None,
    ):
        """
        Draw results on canvas.
        :param visible: If True, makes the window visible (deiconify). If False, effectively hides (withdraw) but updates content?
                       Actually, if hidden, we can just Draw. But we should Withdraw if switching to hidden.
        """
        if visible:
            self.top.deiconify()
        else:
            self.top.withdraw()
        self._is_visible = visible
        
        # Clear only translation results, keep debug text
        self.canvas.delete("result")
        self._photo_images.clear()
        
        if not results: return
        
        import math
        
        for item in results:
            bbox = item['bbox'] # [left, top, right, bottom]
            text = item['translated']
            if not text: continue

            # Use the original OCR glyph height as the base size. This keeps
            # the translated text visually aligned with the source font.
            line_height = item.get('line_height', 0)
            
            box_w = bbox[2] - bbox[0]
            box_h = bbox[3] - bbox[1]
            
            glyph_height = item.get("glyph_height", 0)
            if glyph_height > 0:
                # Match the actual detected ink height rather than the often
                # padded OCR rectangle.  Tk's font size is close to the
                # visible glyph height for Segoe UI on Windows.
                font_size = int(round(glyph_height * 0.98))
                font_size = max(8, min(80, font_size))
            elif line_height > 0:
                # Leave a small amount of vertical breathing room for the
                # font's ascenders/descenders inside the source OCR box.
                font_size = int(round(line_height * 0.84))
                font_size = max(8, min(80, font_size))
            else:
                # Fallback heuristic
                max_h_size = box_h * 0.78
                char_aspect = 0.6
                max_w_size = box_w / (max(1, len(text)) * char_aspect)
                font_size = int(min(max_h_size, max_w_size * 2))
                font_size = max(8, min(60, font_size))

            # Segoe UI is the closest broadly available Windows UI font and
            # has complete Thai glyph coverage without excessive bold weight.
            font_family = item.get("font_family") or "Segoe UI"
            font_weight = item.get("font_weight") or "normal"
            font = tk_pixel_font(font_family, font_size, font_weight)
            
            # Use central theme colors
            from .theme import get_theme
            colors = get_theme()
            overlay_bg = colors.get("overlay_bg", "#1a1a1a")
            overlay_fg = item.get("text_color") or colors.get("overlay_text", "#ffffff")
            overlay_border = colors.get("overlay_border", "#333333")
            overlay_glow = colors.get("overlay_glow", "#444444")

            # The engine estimates the source background from pixels around
            # the OCR box. Use it to erase the original glyphs before drawing
            # the translation. Fall back to the theme for older result data.
            source_bg = item.get("bg_color")
            clear_fill = source_bg or overlay_bg
            clear_outline = "" if source_bg else overlay_border
            
            # Draw text FIRST to measure it
            # Position: align top-left with original box for now, can center later
            # x1, y1 = bbox[0] + offset_x, bbox[1] + offset_y
            
            # Better strategy: Center text in original box's center? 
            # Or assume text replaces original.
            # Let's align top-left but wrap at original box width?
            # User wants "background gray color according to size of text result"
            
            # Use the OCR source box as the pixel anchor. The OCR box is the
            # actual replacement area; derived ink masks can be noisy on
            # textured game/manga backgrounds.
            source_x = bbox[0] + offset_x
            source_y = bbox[1] + offset_y
            
            # --- AUTO-FIT LOGIC ---
            min_font_size = 8
            
            # Create text initially
            text_id = self.canvas.create_text(
                source_x, source_y,
                text=text,
                anchor="nw",
                fill=overlay_fg,
                font=font,
                width=max(1, box_w - 2), # Wrap at original width
                justify="left",
                tags="result"
            )
            
            # Measure
            t_bbox = self.canvas.bbox(text_id)
            if t_bbox:
                text_h = t_bbox[3] - t_bbox[1]
                
                # If text height exceeds box height, shrink font
                while text_h > max(1, box_h - 2) and font_size > min_font_size:
                    font_size -= 2 # Step down
                    font = tk_pixel_font(font_family, font_size, font_weight)
                    
                    self.canvas.itemconfig(text_id, font=font)
                    t_bbox = self.canvas.bbox(text_id)
                    if t_bbox:
                        text_h = t_bbox[3] - t_bbox[1]
                    else:
                        break # Should not happen

            
            # Get actual bounding box of the drawn text
            t_bbox = self.canvas.bbox(text_id)
            if not t_bbox: continue

            # Correct Tk font-metric padding so the rendered glyph box starts
            # at the same pixel as the original OCR box.
            self.canvas.move(
                text_id,
                source_x - t_bbox[0],
                source_y - t_bbox[1]
            )
            t_bbox = self.canvas.bbox(text_id)
            if not t_bbox: continue
            
            # Cover both the original OCR box and any extra space needed by
            # the translated text. This makes the clearing work even when the
            # translation is longer than the source text.
            source_x1 = bbox[0] + offset_x
            source_y1 = bbox[1] + offset_y
            source_x2 = bbox[2] + offset_x
            source_y2 = bbox[3] + offset_y

            padding_x = 1 if source_bg else 8
            padding_y = 1 if source_bg else 4

            clear_x1 = min(source_x1, t_bbox[0]) - padding_x
            clear_y1 = min(source_y1, t_bbox[1]) - padding_y
            clear_x2 = max(source_x2, t_bbox[2]) + padding_x
            clear_y2 = max(source_y2, t_bbox[3]) + padding_y

            source_regions = item.get("source_regions") or []
            if source_regions:
                ink_boxes = [
                    {
                        "bbox": (
                            region["bbox"][0] + offset_x,
                            region["bbox"][1] + offset_y,
                            region["bbox"][2] + offset_x,
                            region["bbox"][3] + offset_y,
                        ),
                        "mask": region.get("mask"),
                    }
                    for region in source_regions
                ]
            else:
                source_boxes = item.get("source_boxes") or [item.get("ink_bbox") or bbox]
                ink_boxes = [
                    (
                        box[0] + offset_x,
                        box[1] + offset_y,
                        box[2] + offset_x,
                        box[3] + offset_y,
                    )
                    for box in source_boxes
                ]
            has_source_patch = self._draw_source_patch(
                source_image,
                (clear_x1, clear_y1, clear_x2, clear_y2),
                ink_boxes,
                offset_x,
                offset_y,
            )

            if has_source_patch:
                bg_rect_id = self.canvas.find_withtag("result")[-1]
            else:
                bg_rect_id = self.canvas.create_rectangle(
                    clear_x1, clear_y1, clear_x2, clear_y2,
                    fill=clear_fill,
                    outline=clear_outline,
                    width=1 if clear_outline else 0,
                    tags="result"
                )
            
            # Lower background behind text
            self.canvas.tag_lower(bg_rect_id, text_id)
            
            # Glow/Highlight on text box
            if not source_bg and overlay_glow and overlay_glow != "transparent":
                self.canvas.create_line(
                    t_bbox[0] - padding_x, t_bbox[3] + padding_y,
                    t_bbox[2] + padding_x, t_bbox[3] + padding_y,
                    fill=overlay_glow, width=1,
                    tags="result"
                )

    def set_geometry(self, geometry):
        # Tk handles normal placement; SetWindowPos below guarantees that the
        # final bounds remain physical pixels on mixed-DPI monitor layouts.
        self.top.geometry(geometry)
        if platform.system() != "Windows":
            return

        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geometry)
        if not match:
            return

        try:
            import ctypes

            width, height, left, top = (int(value) for value in match.groups())
            hwnd = self._get_hwnd()
            HWND_TOPMOST = -1
            SWP_NOACTIVATE = 0x0010
            SWP_NOOWNERZORDER = 0x0200
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                left,
                top,
                width,
                height,
                SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
        except Exception:
            pass

    def show_boxes(self, boxes: list, offset_x: int, offset_y: int, color="cyan"):
        """Draw debug boxes (e.g. for merged text blocks)"""
        self.top.deiconify()
        # Clear only previous boxes
        self.canvas.delete("debug_box")
        
        for bbox in boxes:
            # bbox is [left, top, right, bottom]
            x1 = bbox[0] + offset_x
            y1 = bbox[1] + offset_y
            x2 = bbox[2] + offset_x
            y2 = bbox[3] + offset_y
            
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=color,
                width=2,
                tags="debug_box"
            )
            # Add label
            self.canvas.create_text(
                x1, y1-10,
                text="Group", 
                fill=color,
                anchor="sw",
                font=("Consolas", 10),
                tags="debug_box"
            )

    
    def clear_boxes(self):
        """Explicitly clear debug boxes"""
        self.canvas.delete("debug_box")

    def hide_for_capture(self):
        """
        Temporarily excludes the overlay from internal OCR capture.
        The window stays visible, avoiding flicker and Tk calls from the
        background capture thread.
        """
        self._capture_was_visible = self._is_visible
        self._capture_affinity_changed = (
            self._capture_was_visible and self.include_in_capture
        )
        if self._capture_affinity_changed:
            self._set_capture_affinity(False)

    def restore_after_capture(self):
        """
        Restores the user's screenshot inclusion setting after OCR capture.
        """
        if self._capture_affinity_changed:
            self._set_capture_affinity(True)
            self._capture_affinity_changed = False
        self._is_visible = self._capture_was_visible
    
    def show_debug(self, raw_text: str):
        # self.canvas.itemconfig(self.debug_text_id, text=f"OCR RAW: {raw_text}")
        # self.canvas.tag_raise(self.debug_text_id)
        pass

    def hide(self) -> None:
        self.canvas.delete("result")
        self.canvas.delete("debug_box")
        self.canvas.itemconfig(self.debug_text_id, text="")
        self.top.withdraw()
        self._is_visible = False

