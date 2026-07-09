use windows::core::{Result, Error, ComInterface};
use windows::Graphics::SizeInt32;
use windows::Graphics::Capture::{Direct3D11CaptureFramePool, GraphicsCaptureItem, GraphicsCaptureSession};
use windows::Graphics::DirectX::Direct3D11::IDirect3DDevice;
use windows::Win32::Foundation::{HWND, E_FAIL};
use windows::Win32::Graphics::Direct3D::{D3D_DRIVER_TYPE_HARDWARE, D3D_FEATURE_LEVEL_11_0};
use windows::Win32::Graphics::Direct3D11::{
    D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D, D3D11_CPU_ACCESS_READ, D3D11_MAPPED_SUBRESOURCE,
    D3D11_MAP_READ, D3D11_TEXTURE2D_DESC, D3D11_USAGE_STAGING,
};
use windows::Win32::Graphics::Dxgi::IDXGIDevice;
use windows::Win32::System::WinRT::Direct3D11::CreateDirect3D11DeviceFromDXGIDevice;
use windows::Win32::System::WinRT::Direct3D11::IDirect3DDxgiInterfaceAccess;
use windows::Win32::System::WinRT::Graphics::Capture::IGraphicsCaptureItemInterop;
use windows::Win32::UI::WindowsAndMessaging::GetClientRect;
use windows::Win32::Graphics::Gdi::{GetMonitorInfoW, HMONITOR, MONITORINFOEXW};
use windows::Win32::Foundation::RECT;

pub struct WgcCapture {
    d3d_device: ID3D11Device,
    d3d_context: ID3D11DeviceContext,
    winrt_device: IDirect3DDevice,
    item: Option<GraphicsCaptureItem>,
    pool: Option<Direct3D11CaptureFramePool>,
    session: Option<GraphicsCaptureSession>,
    staging_texture: Option<ID3D11Texture2D>,
    width: i32,
    height: i32,
}

impl WgcCapture {
    pub fn new() -> Result<Self> {
        let mut d3d_device = None;
        let mut d3d_context = None;
        let feature_levels = [D3D_FEATURE_LEVEL_11_0];
        unsafe {
            D3D11CreateDevice(
                None,
                D3D_DRIVER_TYPE_HARDWARE,
                None,
                windows::Win32::Graphics::Direct3D11::D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                Some(&feature_levels),
                windows::Win32::Graphics::Direct3D11::D3D11_SDK_VERSION,
                Some(&mut d3d_device),
                None,
                Some(&mut d3d_context),
            )?;
        }
        let d3d_device = d3d_device.unwrap();
        let d3d_context = d3d_context.unwrap();

        let dxgi_device: IDXGIDevice = d3d_device.cast()?;
        let inspectable = unsafe { CreateDirect3D11DeviceFromDXGIDevice(&dxgi_device)? };
        let winrt_device: IDirect3DDevice = inspectable.cast()?;

        Ok(Self {
            d3d_device,
            d3d_context,
            winrt_device,
            item: None,
            pool: None,
            session: None,
            staging_texture: None,
            width: 0,
            height: 0,
        })
    }

    pub fn start_window(&mut self, hwnd: HWND) -> Result<()> {
        self.stop();

        // Get window size
        let mut rect = RECT::default();
        unsafe {
            GetClientRect(hwnd, &mut rect)?;
        }
        let width = rect.right - rect.left;
        let height = rect.bottom - rect.top;
        if width <= 0 || height <= 0 {
            return Err(Error::new(E_FAIL, "Invalid window dimensions".into()));
        }
        self.width = width;
        self.height = height;

        // Create Capture Item
        let interop = windows::core::factory::<GraphicsCaptureItem, IGraphicsCaptureItemInterop>()?;
        let item: GraphicsCaptureItem = unsafe {
            interop.CreateForWindow(hwnd)?
        };

        self.init_capture(item, width, height)
    }

    pub fn start_monitor(&mut self, hmonitor: HMONITOR) -> Result<()> {
        self.stop();

        // Get monitor dimensions
        let mut info = MONITORINFOEXW::default();
        info.monitorInfo.cbSize = std::mem::size_of::<MONITORINFOEXW>() as u32;
        let width;
        let height;
        unsafe {
            if GetMonitorInfoW(hmonitor, &mut info as *mut _ as *mut _).as_bool() {
                let rect = info.monitorInfo.rcMonitor;
                width = rect.right - rect.left;
                height = rect.bottom - rect.top;
            } else {
                return Err(Error::new(E_FAIL, "Failed to get monitor info".into()));
            }
        }
        self.width = width;
        self.height = height;

        // Create Capture Item
        let interop = windows::core::factory::<GraphicsCaptureItem, IGraphicsCaptureItemInterop>()?;
        let item: GraphicsCaptureItem = unsafe {
            interop.CreateForMonitor(hmonitor)?
        };

        self.init_capture(item, width, height)
    }

    fn init_capture(&mut self, item: GraphicsCaptureItem, width: i32, height: i32) -> Result<()> {
        // Create Frame Pool
        let size = SizeInt32 { Width: width, Height: height };
        let pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            &self.winrt_device,
            windows::Graphics::DirectX::DirectXPixelFormat(87),
            2,
            size,
        )?;

        // Create Session
        let session = pool.CreateCaptureSession(&item)?;
        session.StartCapture()?;

        self.item = Some(item);
        self.pool = Some(pool);
        self.session = Some(session);

        Ok(())
    }

    pub fn get_latest_frame(&mut self) -> Result<Option<image::RgbaImage>> {
        let pool = match &self.pool {
            Some(p) => p,
            None => return Ok(None),
        };

        let frame = pool.TryGetNextFrame()?;
        let surface = match frame.Surface() {
            Ok(s) => s,
            Err(_) => return Ok(None),
        };

        let access: IDirect3DDxgiInterfaceAccess = surface.cast()?;
        let texture: ID3D11Texture2D = unsafe { access.GetInterface()? };

        let mut desc = D3D11_TEXTURE2D_DESC::default();
        unsafe {
            texture.GetDesc(&mut desc as *mut _);
        }

        // Create/resize staging texture if needed
        let recreate_staging = match &self.staging_texture {
            Some(staging) => {
                let mut staging_desc = D3D11_TEXTURE2D_DESC::default();
                unsafe {
                    staging.GetDesc(&mut staging_desc as *mut _);
                }
                staging_desc.Width != desc.Width || staging_desc.Height != desc.Height
            }
            None => true,
        };

        if recreate_staging {
            let mut staging_desc = D3D11_TEXTURE2D_DESC::default();
            staging_desc.Width = desc.Width;
            staging_desc.Height = desc.Height;
            staging_desc.MipLevels = 1;
            staging_desc.ArraySize = 1;
            staging_desc.Format = desc.Format;
            staging_desc.SampleDesc.Count = 1;
            staging_desc.SampleDesc.Quality = 0;
            staging_desc.Usage = D3D11_USAGE_STAGING;
            staging_desc.BindFlags = 0;
            staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ.0 as u32;
            staging_desc.MiscFlags = 0;

            let mut staging_texture = None;
            unsafe {
                self.d3d_device.CreateTexture2D(&staging_desc as *const _, None, Some(&mut staging_texture as *mut _))?;
            }
            self.staging_texture = staging_texture;
        }

        let staging = self.staging_texture.as_ref().unwrap();

        // Copy resource to staging
        unsafe {
            self.d3d_context.CopyResource(staging, &texture);
        }

        // Map and read
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        unsafe {
            self.d3d_context.Map(staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped))?;
        }

        let width = desc.Width as usize;
        let height = desc.Height as usize;
        let row_pitch = mapped.RowPitch as usize;

        let mut pixels = Vec::with_capacity(width * height * 4);
        unsafe {
            let src = mapped.pData as *const u8;
            for y in 0..height {
                let row_start = src.add(y * row_pitch);
                for x in 0..width {
                    let pixel_ptr = row_start.add(x * 4);
                    let b = *pixel_ptr;
                    let g = *pixel_ptr.add(1);
                    let r = *pixel_ptr.add(2);
                    let a = *pixel_ptr.add(3);
                    pixels.push(r);
                    pixels.push(g);
                    pixels.push(b);
                    pixels.push(a);
                }
            }
            self.d3d_context.Unmap(staging, 0);
        }

        let img = image::RgbaImage::from_raw(width as u32, height as u32, pixels)
            .ok_or_else(|| Error::new(E_FAIL, "Failed to construct RgbaImage from raw pixels".into()))?;

        Ok(Some(img))
    }

    pub fn stop(&mut self) {
        if let Some(session) = self.session.take() {
            let _ = session.Close();
        }
        if let Some(pool) = self.pool.take() {
            let _ = pool.Close();
        }
        self.item = None;
        self.staging_texture = None;
        self.width = 0;
        self.height = 0;
    }
}

impl Drop for WgcCapture {
    fn drop(&mut self) {
        self.stop();
    }
}
