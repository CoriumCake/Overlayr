use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use numpy::PyReadonlyArray3;
use std::sync::Mutex;
use std::path::PathBuf;
use rapidocr_core::{
    config::PipelineConfig,
    model::{model_set_by_name, ModelCache, ModelDownloadMode, DEFAULT_MODEL_SET_NAME},
    RapidOcr,
};
use image::{RgbImage, Rgb};

lazy_static::lazy_static! {
    static ref OCR_ENGINE: Mutex<Option<RapidOcr>> = Mutex::new(None);
}

#[pyfunction]
fn initialize_ocr(model_dir_str: &str) -> PyResult<()> {
    let mut engine = OCR_ENGINE.lock().unwrap();
    if engine.is_none() {
        let model_dir = PathBuf::from(model_dir_str);
        let model_set_name = DEFAULT_MODEL_SET_NAME.to_string();
        
        let model_set = model_set_by_name(&model_set_name)
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("Unknown model set"))?;
            
        let pipeline = PipelineConfig::full();
        let cache = ModelCache::new(model_dir);
        
        cache.ensure_model_set_for_pipeline(model_set, pipeline, ModelDownloadMode::Missing)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;

        let cfg = cache.config_for(model_set).with_pipeline(pipeline);
        let ocr = RapidOcr::from_config(cfg)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
            
        *engine = Some(ocr);
    }
    Ok(())
}

#[pyfunction]
fn extract_text<'py>(py: Python<'py>, image: PyReadonlyArray3<u8>) -> PyResult<Bound<'py, PyList>> {
    let array = image.as_array();
    let shape = array.shape();
    
    // Expecting RGB format [H, W, 3]
    if shape.len() != 3 || shape[2] != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err("Image must be an RGB numpy array [H, W, 3]"));
    }
    
    let (h, w) = (shape[0] as u32, shape[1] as u32);
    
    let rgb_image = RgbImage::from_fn(w, h, |x, y| {
        Rgb([
            array[[y as usize, x as usize, 0]],
            array[[y as usize, x as usize, 1]],
            array[[y as usize, x as usize, 2]],
        ])
    });

    let mut lock = OCR_ENGINE.lock().unwrap();
    let ocr = lock.as_mut().ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("OCR Engine not initialized"))?;
    
    let output = ocr.run_image(&rgb_image)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        
    let result_list = PyList::empty(py);
    
    for line in output.lines {
        // rapidocr-core lines usually provide `bbox` or `box` (maybe quad), `text`, `score`.
        // We will just put an empty bbox if we aren't sure, let's look at the structure next.
        // Wait, for now just returning text and score to avoid compilation errors if structure is unknown.
        // Let's assume line has `text`, `score`, `bbox`.
        
        let text = line.text;
        let conf = line.score;
        
        // We will extract bounding box points if available
        let bbox = PyList::empty(py);
        for point in line.bbox.points {
             let pt = PyTuple::new(py, &[point[0] as i32, point[1] as i32]).unwrap();
             bbox.append(pt).unwrap();
        }
        
        let item = PyTuple::new(py, &[bbox.into_any(), text.into_pyobject(py).unwrap().into_any(), conf.into_pyobject(py).unwrap().into_any()]).unwrap();
        result_list.append(item).unwrap();
    }

    Ok(result_list)
}

#[pymodule]
fn rust_ocr(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(initialize_ocr, m)?)?;
    m.add_function(wrap_pyfunction!(extract_text, m)?)?;
    Ok(())
}
