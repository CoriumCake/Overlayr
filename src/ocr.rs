use anyhow::{Context, Result};
use image::{GenericImageView, DynamicImage, ImageBuffer, Luma, Rgb};
use imageproc::region_labelling::{connected_components, Connectivity};
use ndarray::Array4;
use ort::{inputs, session::Session, value::TensorRef};
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

pub struct RustOCREngine {
    det_session: Session,
    rec_session: Session,
    vocab: Vec<String>,
}

fn load_dict(path: &Path) -> Result<Vec<String>> {
    let file = File::open(path).context("Failed to open dictionary file")?;
    let reader = BufReader::new(file);
    let mut characters = Vec::new();
    
    // In PaddleOCR English model, index 0 is reserved for 'blank'
    characters.push("blank".to_string());
    
    for line in reader.lines() {
        let line = line?;
        let line = line.trim_end_matches('\r').trim_end_matches('\n').to_string();
        characters.push(line);
    }
    
    // Appends space character at index 96
    characters.push(" ".to_string());
    
    Ok(characters)
}

fn preprocess_detect(img: &DynamicImage) -> Array4<f32> {
    let resized = img.resize_exact(640, 640, image::imageops::FilterType::Triangle);
    let mut tensor = Array4::<f32>::zeros((1, 3, 640, 640));
    
    let mean = [0.485, 0.456, 0.406];
    let std = [0.229, 0.224, 0.225];
    
    for y in 0..640 {
        for x in 0..640 {
            let pixel = resized.get_pixel(x, y);
            let r = (pixel[0] as f32 / 255.0 - mean[0]) / std[0];
            let g = (pixel[1] as f32 / 255.0 - mean[1]) / std[1];
            let b = (pixel[2] as f32 / 255.0 - mean[2]) / std[2];
            
            tensor[[0, 0, y as usize, x as usize]] = r;
            tensor[[0, 1, y as usize, x as usize]] = g;
            tensor[[0, 2, y as usize, x as usize]] = b;
        }
    }
    
    tensor
}

fn ctc_decode(indices: &[usize], probs: &[f32], vocab: &[String]) -> (String, f32) {
    let mut char_list = Vec::new();
    let mut conf_list = Vec::new();
    
    for i in 0..indices.len() {
        let idx = indices[i];
        if idx == 0 {
            continue;
        }
        
        // Remove duplicate adjacent tokens in the original raw output sequence
        if i > 0 && indices[i - 1] == idx {
            continue;
        }
        
        if idx < vocab.len() {
            char_list.push(vocab[idx].clone());
            conf_list.push(probs[i]);
        }
    }
    
    let text = char_list.join("");
    let confidence = if conf_list.is_empty() {
        0.0
    } else {
        conf_list.iter().sum::<f32>() / conf_list.len() as f32
    };
    
    (text, confidence)
}

impl RustOCREngine {
    pub fn new(det_path: &Path, rec_path: &Path, dict_path: &Path) -> Result<Self> {
        let det_session = Session::builder()?
            .commit_from_file(det_path)
            .context("Failed to load detection model")?;

        let rec_session = Session::builder()?
            .commit_from_file(rec_path)
            .context("Failed to load recognition model")?;

        let vocab = load_dict(dict_path).context("Failed to load dictionary")?;

        Ok(RustOCREngine {
            det_session,
            rec_session,
            vocab,
        })
    }

    pub fn extract_text(&mut self, img: &DynamicImage) -> Result<Vec<(Vec<Vec<i32>>, String, f32)>> {
        let orig_w = img.width();
        let orig_h = img.height();

        // 3. Preprocess for detection
        let det_input = preprocess_detect(img);

        // 4. Run Text Detection
        let det_outputs = self.det_session.run(inputs![TensorRef::from_array_view(&det_input)?])?;
        
        let prob_output = det_outputs[0].try_extract_array::<f32>()?;
        let prob_view = prob_output.into_dimensionality::<ndarray::Ix4>()?;
        
        let threshold = 0.3;
        let prob_shape = prob_view.shape();
        let h_prob = prob_shape[2] as u32;
        let w_prob = prob_shape[3] as u32;
        
        let mut mask: ImageBuffer<Luma<u8>, Vec<u8>> = ImageBuffer::new(w_prob, h_prob);
        for y in 0..h_prob {
            for x in 0..w_prob {
                let val = prob_view[[0, 0, y as usize, x as usize]];
                if val > threshold {
                    mask.put_pixel(x, y, Luma([255]));
                } else {
                    mask.put_pixel(x, y, Luma([0]));
                }
            }
        }

        // 5. Connected Component Analysis (CCA) to find bounding boxes
        let conn = connected_components(&mask, Connectivity::Four, Luma([0u8]));
        let mut boxes: HashMap<u32, (u32, u32, u32, u32)> = HashMap::new(); // label -> (min_x, min_y, max_x, max_y)
        
        let (mask_w, mask_h) = conn.dimensions();
        for y in 0..mask_h {
            for x in 0..mask_w {
                let label = conn.get_pixel(x, y).0[0];
                if label == 0 {
                    continue; // background
                }
                let entry = boxes.entry(label).or_insert((x, y, x, y));
                entry.0 = entry.0.min(x);
                entry.1 = entry.1.min(y);
                entry.2 = entry.2.max(x);
                entry.3 = entry.3.max(y);
            }
        }

        // Filter boxes and map back to original coordinates
        let scale_x = orig_w as f32 / w_prob as f32;
        let scale_y = orig_h as f32 / h_prob as f32;
        
        let expand_w = 2.3;
        let expand_h = 1.8;

        let mut text_regions = Vec::new();
        for (_label, &(min_x, min_y, max_x, max_y)) in &boxes {
            let w = max_x - min_x + 1;
            let h = max_y - min_y + 1;
            let area = w * h;
            if area < 15 {
                continue; // Filter small noise
            }
            
            let cx = (min_x + max_x) as f32 / 2.0;
            let cy = (min_y + max_y) as f32 / 2.0;
            
            // Map to original dimensions with expansion
            let cx_orig = cx * scale_x;
            let cy_orig = cy * scale_y;
            let w_orig = w as f32 * scale_x * expand_w;
            let h_orig = h as f32 * scale_y * expand_h;
            
            let min_x_orig = ((cx_orig - w_orig / 2.0).max(0.0) as u32).min(orig_w - 1);
            let min_y_orig = ((cy_orig - h_orig / 2.0).max(0.0) as u32).min(orig_h - 1);
            let max_x_orig = ((cx_orig + w_orig / 2.0).max(0.0) as u32).min(orig_w - 1);
            let max_y_orig = ((cy_orig + h_orig / 2.0).max(0.0) as u32).min(orig_h - 1);
            
            let box_w = max_x_orig - min_x_orig + 1;
            let box_h = max_y_orig - min_y_orig + 1;
            if box_w < 5 || box_h < 5 {
                continue;
            }
            
            text_regions.push((min_x_orig, min_y_orig, max_x_orig, max_y_orig));
        }

        // Sort regions top-to-bottom, left-to-right to maintain reading order
        text_regions.sort_by(|a, b| {
            if (a.1 as i32 - b.1 as i32).abs() < 15 {
                a.0.cmp(&b.0)
            } else {
                a.1.cmp(&b.1)
            }
        });

        let mut output_results = Vec::new();

        // 6. Run Recognition on each crop
        for &(x1, y1, x2, y2) in &text_regions {
            let box_w = x2 - x1 + 1;
            let box_h = y2 - y1 + 1;
            
            // Crop region from original image
            let crop = img.crop_imm(x1, y1, box_w, box_h);
            
            // Resize height to 48 while preserving aspect ratio
            let new_h = 48;
            let new_w = ((box_w as f32) * (48.0 / (box_h as f32))) as u32;
            let new_w = new_w.max(8); // Ensure width is not zero
            
            let resized_crop = image::imageops::resize(&crop, new_w, new_h, image::imageops::FilterType::CatmullRom);
            
            // Pad width to a multiple of 32 for model alignment
            let padded_w = ((new_w + 31) / 32) * 32;
            println!("[OCR DEBUG] Crop box: {}x{} | new_w: {} | padded_w: {}", box_w, box_h, new_w, padded_w);
            
            // Initialize padded white background image
            let mut padded_img = ImageBuffer::from_pixel(padded_w, new_h, Rgb([255, 255, 255]));
            for y in 0..new_h {
                for x in 0..new_w {
                    let rgba = resized_crop.get_pixel(x, y);
                    padded_img.put_pixel(x, y, Rgb([rgba[0], rgba[1], rgba[2]]));
                }
            }
            
            // Build tensor: shape [1, 3, 48, padded_w], normalized to [-1.0, 1.0]
            let mut rec_tensor = Array4::<f32>::zeros((1, 3, 48, padded_w as usize));
            for y in 0..new_h {
                for x in 0..padded_w {
                    let pixel = padded_img.get_pixel(x, y);
                    rec_tensor[[0, 0, y as usize, x as usize]] = (pixel[0] as f32 / 255.0 - 0.5) / 0.5;
                    rec_tensor[[0, 1, y as usize, x as usize]] = (pixel[1] as f32 / 255.0 - 0.5) / 0.5;
                    rec_tensor[[0, 2, y as usize, x as usize]] = (pixel[2] as f32 / 255.0 - 0.5) / 0.5;
                }
            }

            // Run recognition inference
            let rec_outputs = self.rec_session.run(inputs![TensorRef::from_array_view(&rec_tensor)?])?;
            let output_tensor = rec_outputs[0].try_extract_array::<f32>()?;
            let output_view = output_tensor.into_dimensionality::<ndarray::Ix3>()?;
            
            let shape = output_view.shape();
            let seq_len = shape[1];
            let num_classes = shape[2];
            
            let mut pred_indices = Vec::with_capacity(seq_len);
            let mut pred_probs = Vec::with_capacity(seq_len);
            
            for t in 0..seq_len {
                let mut max_idx = 0;
                let mut max_prob = f32::NEG_INFINITY;
                for c in 0..num_classes {
                    let prob = output_view[[0, t, c]];
                    if prob > max_prob {
                        max_prob = prob;
                        max_idx = c;
                    }
                }
                pred_indices.push(max_idx);
                pred_probs.push(max_prob);
            }
            
            let (text, confidence) = ctc_decode(&pred_indices, &pred_probs, &self.vocab);

            let bbox = vec![
                vec![x1 as i32, y1 as i32],
                vec![x2 as i32, y1 as i32],
                vec![x2 as i32, y2 as i32],
                vec![x1 as i32, y2 as i32],
            ];

            output_results.push((bbox, text, confidence));
        }

        Ok(output_results)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn test_ocr() {
        let det_path = Path::new("assets/models/Detec_Manga-English.onnx");
        let rec_path = Path::new("assets/models/PaddleOCR.onnx");
        let dict_path = Path::new("assets/models/en_dict.txt");
        
        let mut engine = RustOCREngine::new(det_path, rec_path, dict_path).unwrap();
        
        // Load a debug image
        let img_path = Path::new("debug_images/proc_grouped_1770912594847.png");
        assert!(img_path.exists(), "Debug image not found!");
        let img = image::open(img_path).unwrap();
        
        println!("Input image size: {}x{}", img.width(), img.height());
        let results = engine.extract_text(&img).unwrap();
        println!("OCR results count: {}", results.len());
        for (i, (bbox, text, conf)) in results.iter().enumerate() {
            println!("  [{}] conf={:.3} text='{}' bbox={:?}", i, conf, text, bbox);
        }
        assert!(!results.is_empty(), "No text detected!");
    }
}
