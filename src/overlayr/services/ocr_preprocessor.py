import cv2
import numpy as np
import os

try:
    import onnxruntime as ort
    HAS_ORT = True
except ImportError:
    HAS_ORT = False

_dbnet_session = None


def _preferred_ort_providers():
    """Return only installed accelerators, with a deterministic CPU fallback."""
    if not HAS_ORT:
        return []
    available = set(ort.get_available_providers())
    preferred = [
        provider
        for provider in (
            "DmlExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        )
        if provider in available
    ]
    return preferred or list(available)


def preprocess_image_for_ocr(image_source):
    """
    Reads an image and applies preprocessing to improve OCR accuracy.
    Technique: Grayscale -> Scale 2x -> Sharpen
    """
    if isinstance(image_source, str):
        img = cv2.imread(image_source)
    else:
        img = image_source
        
    if img is None:
        return None

    # 1. Grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 2. Scale 2x (Cubic interpolation)
    scaled = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    # 3. Sharpen
    # Standard sharpening kernel
    kernel = np.array([[0, -1, 0], 
                       [-1, 5,-1], 
                       [0, -1, 0]])
    sharpened = cv2.filter2D(scaled, -1, kernel)
    
    # 4. Shortest Cut Splitting (DISABLED)
    # User Request: Disable because it cuts speech bubbles and damages non-linear text.
    # split_merged_contours(binary, contours, min_area=100, draw_on=sharpened, color=255)

    return sharpened, 2.0

def detect_text_regions(image_source, bubbles=None):
    """
    Detects potential text regions using Morphological Dilation.
    image_source: Path to image (str) or Image object (numpy array).
    bubbles: Optional list of speech bubble contours to constrain detection.
    Returns a list of bounding rectangles (x, y, w, h).
    """
    if isinstance(image_source, str):
        img = cv2.imread(image_source)
        if img is None:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        img = image_source
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img
    
    # 1. Thresholding
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                   cv2.THRESH_BINARY_INV, 11, 2)

    regions = []
    
    if bubbles:
        # Strategy: Process each bubble separately to prevent bridging
        
        # A. Process Text INSIDE Bubbles
        for cnt in bubbles:
            # Create mask for this single bubble
            bubble_mask = np.zeros(binary.shape, dtype=np.uint8)
            cv2.drawContours(bubble_mask, [cnt], -1, 255, -1)
            
            # Mask the binary text
            masked_text = cv2.bitwise_and(binary, binary, mask=bubble_mask)
            
            # Dilate (Kernel (10,5) for horizontal text)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 5))
            dilated = cv2.dilate(masked_text, kernel, iterations=2)
            
            # Find contours
            cnts, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                x, y, w, h = cv2.boundingRect(c)
                if w > 10 and h > 10:
                    regions.append((x, y, w, h))

        # B. Process Text OUTSIDE Bubbles (Captions/SFX)
        all_bubbles_mask = np.zeros(binary.shape, dtype=np.uint8)
        cv2.drawContours(all_bubbles_mask, bubbles, -1, 255, -1)
        outside_mask = cv2.bitwise_not(all_bubbles_mask)
        
        outside_text = cv2.bitwise_and(binary, binary, mask=outside_mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 5))
        dilated_out = cv2.dilate(outside_text, kernel, iterations=2)
        
        cnts_out, _ = cv2.findContours(dilated_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts_out:
            x, y, w, h = cv2.boundingRect(c)
            if w > 10 and h > 10:
                regions.append((x, y, w, h))
                
    else:
        # Standard unconstrained detection
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 5))
        dilated = cv2.dilate(binary, kernel, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w > 10 and h > 10:
                regions.append((x, y, w, h))
            
    return regions

def detect_speech_bubbles(image_source):
    """
    Detects potential speech bubbles (white regions).
    image_source: Path to image (str) or Image object (numpy array).
    Returns a list of contours.
    """
    if isinstance(image_source, str):
        img = cv2.imread(image_source)
        if img is None:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        # Assume numpy array
        img = image_source
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img
    
    # 1. Thresholding for white regions
    # Reverting to static high threshold (215).
    # Adaptive/Otsu merged background with bubbles.
    # 215 is safe for white bubbles vs gray/screentone background.
    _, binary = cv2.threshold(gray, 215, 255, cv2.THRESH_BINARY)
    
    
    # 1. Topological Feature Extraction (Better than Morphological Closing)
    # We want to identify White Bubbles and fill their text holes, 
    # but NOT bridge the black boundary between Bubble and Page.
    # Closing bridges the boundary. Filling Contours does not.
    
    cnts_raw, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    
    filled_mask = np.zeros_like(gray)
    img_area = gray.shape[0] * gray.shape[1]
    
    
    potential_bubbles = []
    
    # Hierarchy: [Next, Prev, First_Child, Parent]
    for i, c in enumerate(cnts_raw):
        area = cv2.contourArea(c)
        if area < 100: continue
        
        # Check if the contour is White (Foreground)
        # Create a small mask to check mean intensity
        # Optimization: Check center point or just assume hierarchy level?
        # Hierarchy Level 0 are usually White (if background is black) or White (if background is White).
        # Safe check: Mean Intensity.
        mask_check = np.zeros_like(gray)
        cv2.drawContours(mask_check, [c], -1, 255, -1)
        mean_val = cv2.mean(binary, mask=mask_check)[0]
        
        # If mainly white (> 128), it's a bubble candidate or page background
        if mean_val > 128:
            # Draw it filled on the final mask (closes text holes)
            cv2.drawContours(filled_mask, [c], -1, 255, -1)
            
    # 2. Find Contours on the Filled Mask
    # This gives us Solid White Blobs
    cnts_filled, _ = cv2.findContours(filled_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    final_bubbles = []
    
    # Helper for recursive splitting
    def recursive_split_bubble(c):
        hull = cv2.convexHull(c, returnPoints=False)
        try:
            defects = cv2.convexityDefects(c, hull)
        except:
             return [c]
             
        if defects is None: return [c]

        # Helper to calculate angle
        def calculate_angle(pt, p_prev, p_next):
            v1 = np.array(p_prev) - np.array(pt)
            v2 = np.array(p_next) - np.array(pt)
            norm1 = np.linalg.norm(v1)
            norm2 = np.linalg.norm(v2)
            if norm1 == 0 or norm2 == 0: return 180.0
            
            cos_angle = np.dot(v1, v2) / (norm1 * norm2)
            angle = np.degrees(np.arccos(np.clip(cos_angle, -1.0, 1.0)))
            return angle

        deep_defects = []
        for i in range(defects.shape[0]):
            s, e, f, d = defects[i, 0]
            depth = d / 256.0
            
            if depth > 5:
                # Calculate Angle at the defect point 'f'
                # Use neighbors +/- 10 points (adaptive)
                k = 10
                p_f = c[f][0]
                p_prev = c[(f - k) % len(c)][0]
                p_next = c[(f + k) % len(c)][0]
                
                angle = calculate_angle(p_f, p_prev, p_next)
                
                # Filter: Sharp Angle (< 150 degrees)
                # User request: "Angle pointing towards each other"
                # Relaxed to 150 to account for smoothing
                if angle < 150:
                    far_pt = tuple(c[f][0])
                    deep_defects.append((far_pt, depth, f))

        if len(deep_defects) < 2:
            return [c]

        best_pair = None
        min_dist = float('inf')
        total_pts = len(c)
        # Increase to 20% to ensure defects are topologically far (opposing sides)
        min_index_dist = max(20, total_pts * 0.20) # At least 20pts or 20% of contour

        for i in range(len(deep_defects)):
            for j in range(i + 1, len(deep_defects)):
                p1, d1, f1 = deep_defects[i]
                p2, d2, f2 = deep_defects[j]
                
                dist = np.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2)
                
                # Topological Distance (Index Check)
                idx_dist = abs(f1 - f2)
                idx_dist = min(idx_dist, total_pts - idx_dist)
                
                if idx_dist < min_index_dist:
                    continue
                
                if dist < min_dist:
                    min_dist = dist
                    best_pair = (p1, p2)
        
        if best_pair:
            p1, p2 = best_pair
            
            # Validation (Ratio > 0.05)
            x,y,w,h = cv2.boundingRect(c)
            temp_mask = np.zeros((h, w), np.uint8)
            c_offset = c - [x, y]
            cv2.drawContours(temp_mask, [c_offset], -1, 255, -1)
            p1_local = (p1[0]-x, p1[1]-y)
            p2_local = (p2[0]-x, p2[1]-y)
            cv2.line(temp_mask, p1_local, p2_local, 0, 30)
            parts, _ = cv2.findContours(temp_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            valid_cut = False
            if len(parts) >= 2:
                 areas = [cv2.contourArea(p) for p in parts]
                 areas.sort() 
                 ratio = areas[0] / areas[-1] if areas[-1] > 0 else 0
                 # Relaxed Ratio > 0.05
                 if ratio > 0.05:
                     valid_cut = True
                     
            if valid_cut:
                bubble_mask = np.zeros_like(gray)
                cv2.drawContours(bubble_mask, [c], -1, 255, -1)
                cv2.line(bubble_mask, p1, p2, 0, 30)
                split_cnts, _ = cv2.findContours(bubble_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                results = []
                for sc in split_cnts:
                    if cv2.contourArea(sc) > 100:
                        results.extend(recursive_split_bubble(sc))
                return results

        return [c]

    for c in cnts_filled:
        area = cv2.contourArea(c)
        if area < 500: continue
        
        # Filter Massive Background (Page)
        if area > img_area * 0.4:
            continue
        
        final_bubbles.extend(recursive_split_bubble(c))
            
    return final_bubbles

def split_merged_contours(image_bin, contours, min_area=50, draw_on=None, color=255, recursion_depth=0):
    """
    Splits merged text contours using a Fast Horizontal Projection Profile approach.
    Replaces the slow Shortest Path method.
    """
    separated_contours = []
    
    if recursion_depth > 3: # Lower recursion limit for safety
        return contours
        
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            separated_contours.append(cnt)
            continue
            
        x, y, w, h = cv2.boundingRect(cnt)
        
        # Heuristic: Skip if too short (likely single line)
        if h < 35:
             separated_contours.append(cnt)
             continue

        roi = image_bin[y:y+h, x:x+w]
        
        # Calculate Horizontal Projection Profile
        # Sum of pixels per row. Text=255, BG=0.
        # We want to find rows with LOW sum (valleys).
        projection = np.sum(roi, axis=1) # Shape (h,)
        
        # Normalize
        max_val = np.max(projection)
        if max_val == 0:
            separated_contours.append(cnt)
            continue
            
        # Find potential split points (Local Minima)
        # We look for a "valley" that is significantly lower than the text lines.
        # Ideally 0, but might have noise or touching pixels.
        
        # A simple approach: 
        # 1. Smooth the profile?
        # 2. Find row where sum < Threshold (e.g., 20% of max width*255)
        # 3. Choose the "widest" valley or the one closest to the middle?
        
        # Gap Detection Logic:
        # We need a gap of at least a few pixels.
        
        split_row = -1
        
        # Threshold: if row has fewer than 12% of width as pixels, it's a candidate
        thresh = w * 255 * 0.12 # Lowered from 0.15 to avoid false splits in noisy text
        
        # Find runs of low-pixel rows
        is_gap = projection < thresh
        
        # Find the "best" gap (closest to center vertical)
        center_y = h // 2
        best_gap_y = -1
        min_dist_to_center = h
        
        # We need to ignore the very top and bottom margins
        # Reduced margin to allow splitting closer to edges
        margin = int(h * 0.15) 
        
        current_gap_start = -1
        
        for r in range(margin, h - margin):
            if is_gap[r]:
                if current_gap_start == -1:
                    current_gap_start = r
            else:
                if current_gap_start != -1:
                    # Gap ended. Process it.
                    gap_middle = (current_gap_start + (r - 1)) // 2
                    dist = abs(gap_middle - center_y)
                    
                    # We prefer gaps that are effectively separating distinct text blobs
                    # Check if there is significant text Mass above and below?
                    # Using center proximity is a good proxy for "splitting into two equal-ish halves"
                    if dist < min_dist_to_center:
                        min_dist_to_center = dist
                        best_gap_y = gap_middle
                        
                    current_gap_start = -1
        
        # Check trailing gap
        if current_gap_start != -1:
             gap_middle = (current_gap_start + (h - margin - 1)) // 2
             dist = abs(gap_middle - center_y)
             if dist < min_dist_to_center:
                 best_gap_y = gap_middle

        if best_gap_y != -1:
            # print(f"DEBUG: Splitting contour at Y={best_gap_y} (local)")
            
            # Draw the cut on the mask
            # We specifically draw a BLACK line on the binary ROI to cut it
            # But wait, 'roi' is a view into 'image_bin' (numpy array slice).
            # Modifying 'roi' modifies 'image_bin'? YES.
            # But we need to use this modified binary to find contours.
            
            # Create a localized mask to find new contours
            # Actually, to support recursion and output, we can just cut the DRAW_ON image
            # and return the new bounding boxes? 
            # The interface returns 'contours'.
            
            # Let's Modify the ROI to simulate the cut
            cv2.line(roi, (0, best_gap_y), (w, best_gap_y), 0, 4) # 4px cut
            
            # If we need visual feedback on the Output Image
            if draw_on is not None:
                # White line on the visualization (Grayscale/BGR)
                pt1 = (x, y + best_gap_y)
                pt2 = (x + w, y + best_gap_y)
                cv2.line(draw_on, pt1, pt2, color, 4)
                
            # Now determine the new contours from this cut ROI
            # Note: We modified 'roi' in place, so 'image_bin' is modified.
            # But we only want to re-find contours in this ROI area, not valid globally.
            
            # Extract the actual split parts
            # We can run findContours on the ROI
            new_cnts, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            valid_parts = []
            for nc in new_cnts:
                 if cv2.contourArea(nc) > min_area / 4:
                     nc_global = nc + np.array([x, y])
                     valid_parts.append(nc_global)
                     
            if len(valid_parts) > 1:
                # Recurse
                recursive_results = split_merged_contours(image_bin, valid_parts, min_area, draw_on, color, recursion_depth + 1)
                separated_contours.extend(recursive_results)
            else:
                 separated_contours.append(cnt) # Failed to split effectively
        else:
             # Try Vertical Split (Side-by-Side, e.g. "SORTED" "YOU'RE NOT")
             # Projection on X-axis (Columns)
             projection_x = np.sum(roi, axis=0) # Shape (w,)
             max_val_x = np.max(projection_x)
             
             if max_val_x == 0:
                 separated_contours.append(cnt)
                 continue
                 
             thresh_x = h * 255 * 0.12 # Threshold relative to Height
             is_gap_x = projection_x < thresh_x
             
             center_x = w // 2
             best_gap_x = -1
             min_dist_to_center_x = w
             margin_x = int(w * 0.15)
             
             current_gap_start = -1
             
             # Find best gap close to center X
             for c in range(margin_x, w - margin_x):
                 if is_gap_x[c]:
                     if current_gap_start == -1:
                         current_gap_start = c
                 else:
                     if current_gap_start != -1:
                         gap_middle = (current_gap_start + (c - 1)) // 2
                         dist = abs(gap_middle - center_x)
                         if dist < min_dist_to_center_x:
                             min_dist_to_center_x = dist
                             best_gap_x = gap_middle
                         current_gap_start = -1
             
             # Check trailing gap
             if current_gap_start != -1:
                 gap_middle = (current_gap_start + (w - margin_x - 1)) // 2
                 dist = abs(gap_middle - center_x)
                 if dist < min_dist_to_center_x:
                     best_gap_x = gap_middle
             
             if best_gap_x != -1:
                 # Vertical Split found
                 cv2.line(roi, (best_gap_x, 0), (best_gap_x, h), 0, 4)
                 
                 if draw_on is not None:
                     pt1 = (x + best_gap_x, y)
                     pt2 = (x + best_gap_x, y + h)
                     cv2.line(draw_on, pt1, pt2, color, 4)
                 
                 new_cnts, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                 valid_parts = []
                 for nc in new_cnts:
                      if cv2.contourArea(nc) > min_area / 4:
                          nc_global = nc + np.array([x, y])
                          valid_parts.append(nc_global)
                 
                 if len(valid_parts) > 1:
                     recursive_results = split_merged_contours(image_bin, valid_parts, min_area, draw_on, color, recursion_depth + 1)
                     separated_contours.extend(recursive_results)
                 else:
                      separated_contours.append(cnt)
             else:
                 separated_contours.append(cnt) # No gap found anywhere
                 
    return separated_contours


def apply_cuts_to_original(image_path):
    """
    Applies Shortest Cut splitting to the original image.
    Returns: Numpy array (BGR) of the original size with cuts applied (White lines).
    """
    img = cv2.imread(image_path)
    if img is None: return None
    
    # Scale 2x for better detection
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    scaled = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    
    # Sharpen for detection
    kernel = np.array([[0, -1, 0], [-1, 5,-1], [0, -1, 0]])
    sharpened = cv2.filter2D(scaled, -1, kernel)
    
    # Binary
    binary = cv2.adaptiveThreshold(sharpened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                   cv2.THRESH_BINARY_INV, 11, 2)
                                   
    # NEW: Mask Bubble Borders (They connect text and cause merging)
    # Detect bubbles on original image
    bubbles_orig = detect_speech_bubbles(img)
    print(f"DEBUG: apply_cuts detected {len(bubbles_orig)} bubbles.")
    
    # Create Scaled Output
    img_scaled_bgr = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    
    if bubbles_orig:
        # Scale contours to 2x matches binary/output
        bubbles_scaled = []
        for cnt in bubbles_orig:
            # Scale points
            cnt_scaled = cnt * 2
            bubbles_scaled.append(cnt_scaled)
            
        # 1. Erase Borders in Binary (Draw Black line on White Border)
        # Note: Binary is Inverted (Text=255, Border=255). We want Gap (0).
        cv2.drawContours(binary, bubbles_scaled, -1, 0, 8)
        
        # 2. Erase Borders in Output Image (Draw White line on Black Border)
        # BGR: White = (255, 255, 255)
        cv2.drawContours(img_scaled_bgr, bubbles_scaled, -1, (255, 255, 255), 8)
        
        # cv2.imwrite("debug_en4_masked.png", img_scaled_bgr)
        
    # Re-Find Contours on the Masked Binary
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    # Apply cuts (White = 255, 255, 255)
    # Lower min_area to 20 to catch small split parts
    split_merged_contours(binary, contours, min_area=20, draw_on=img_scaled_bgr, color=(255, 255, 255))
    
    # Resize back to original
    img_final = cv2.resize(img_scaled_bgr, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_AREA)
    
    return img_final

def get_expanded_polygon(contour, expand_ratio=2.5):
    """Expands bounding polygon for text regions using pyclipper."""
    import pyclipper
    
    area = cv2.contourArea(contour)
    perimeter = cv2.arcLength(contour, True)
    if perimeter == 0:
        return contour
        
    # Calculate expansion distance (DBNet formula)
    distance = area * expand_ratio / perimeter
    
    # Expand polygon
    offset = pyclipper.PyclipperOffset()
    offset.AddPath(contour.reshape(-1, 2).tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    expanded = offset.Execute(distance)
    
    if len(expanded) == 0:
        return contour
        
    expanded = np.array(expanded[0]).reshape(-1, 1, 2)
    
    # Approximate polygon to reduce points
    epsilon = 0.005 * cv2.arcLength(expanded, True)
    approx_polygon = cv2.approxPolyDP(expanded, epsilon, True)
    
    return approx_polygon

def detect_text_regions_dbnet(image_source):
    """
    Detects text regions using the DBNet model.
    Falls back to bubble detection if onnxruntime or the model is missing.
    """
    global _dbnet_session
    if not HAS_ORT:
        print("onnxruntime is not installed. Falling back to bubble detection.")
        return detect_speech_bubbles(image_source)

    if isinstance(image_source, str):
        orig_img = cv2.imread(image_source)
        if orig_img is None: return []
    else:
        orig_img = image_source
        if len(orig_img.shape) == 2:
            orig_img = cv2.cvtColor(orig_img, cv2.COLOR_GRAY2BGR)

    orig_h, orig_w = orig_img.shape[:2]
    target_size = 640
    
    if _dbnet_session is None:
        # Load from the models folder relative to the project root
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        model_path = os.path.join(project_root, "models", "Detec_Manga-English.onnx")
        if not os.path.exists(model_path):
             print(f"DBNet model not found at {model_path}. Falling back to bubble detection.")
             return detect_speech_bubbles(image_source)
        # Load ONNX model with GPU acceleration if available
        _dbnet_session = ort.InferenceSession(
            model_path,
            providers=_preferred_ort_providers(),
        )
        
    img = cv2.cvtColor(orig_img, cv2.COLOR_BGR2RGB)
    img_resized = cv2.resize(img, (target_size, target_size))
    
    # Normalize: (I/255.0 - mean) / std
    img_normalized = img_resized.astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    img_normalized = (img_normalized - mean) / std
    
    # HWC to CHW and add batch dimension
    tensor = np.transpose(img_normalized, (2, 0, 1))
    tensor = np.expand_dims(tensor, axis=0)
    
    input_name = _dbnet_session.get_inputs()[0].name
    outputs = _dbnet_session.run(None, {input_name: tensor})
    
    prob_map = outputs[0]
    if len(prob_map.shape) == 4:
        prob = prob_map[0, 0]
    else:
        prob = prob_map[0]
        
    # Scale back to original resolution
    prob = cv2.resize(prob, (orig_w, orig_h))
    
    # Binarize
    threshold = 0.15
    mask = (prob > threshold).astype(np.uint8) * 255
    
    # Extract contours
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    
    polygons = []
    for contour in contours:
        if cv2.contourArea(contour) < 10:
            continue
        poly = get_expanded_polygon(contour, expand_ratio=2.5)
        polygons.append(poly)
        
    return polygons


def preload_dbnet_model():
    """
    Preloads and warms up the DBNet model so the first inference frame is fast.
    """
    global _dbnet_session
    if not HAS_ORT:
        return
    try:
        if _dbnet_session is None:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            model_path = os.path.join(project_root, "models", "Detec_Manga-English.onnx")
            if os.path.exists(model_path):
                print(f"DBNet: Preloading model from {model_path}...", flush=True)
                _dbnet_session = ort.InferenceSession(
                    model_path,
                    providers=_preferred_ort_providers(),
                )
                # Warmup inference
                dummy = np.zeros((640, 640, 3), dtype=np.uint8)
                detect_text_regions_dbnet(dummy)
                print("DBNet: Model preloaded & warmed up successfully.", flush=True)
    except Exception as e:
        print(f"DBNet Preload Error: {e}", flush=True)

