import asyncio
import ctypes
import threading
import time
from ctypes import POINTER, Structure, byref, c_int, c_uint, c_void_p, cast
from ctypes import wintypes
from typing import Any, Dict, Optional

# Constants
HRESULT = ctypes.c_long
S_OK = 0
DXGI_FORMAT_B8G8R8A8_UNORM = 87
D3D11_BIND_RENDER_TARGET = 0x20

# GUIDs (IIDs)
class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8)
    ]
    
    def __init__(self, guid_str):
        # Allow init from string like '{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}'
        import uuid
        u = uuid.UUID(guid_str)
        self.Data1 = u.time_low
        self.Data2 = u.time_mid
        self.Data3 = u.time_hi_version
        self.Data4 = (wintypes.BYTE * 8)(*u.bytes[8:])
        
    def __str__(self):
        return f"{{{self.Data1:08x}-{self.Data2:04x}-{self.Data3:04x}-{self.Data4[0]:02x}{self.Data4[1]:02x}...}}"

# COM Interfaces Definitions
class IUnknown(Structure):
    _fields_ = [("lpVtbl", POINTER(POINTER(c_void_p)))]

class IInspectable(IUnknown):
    pass # Inherits IUnknown layout

# IGraphicsCaptureItemInterop
# IID: 3628E819-3F9E-4161-8FC1-9F20D44922B0
IID_IGraphicsCaptureItemInterop = GUID('{3628E819-3F9E-4161-8FC1-9F20D44922B0}')

class IGraphicsCaptureItemInterop(IUnknown):
    _iid_ = IID_IGraphicsCaptureItemInterop
    # VTable: QueryInterface, AddRef, Release, CreateForWindow, CreateForMonitor
    # Index 3: CreateForWindow
    pass

# IDirect3DDevice
# IID: A37624AB-8D5F-4650-9D3E-9EAE3D9BC670
IID_IDirect3DDevice = GUID('{A37624AB-8D5F-4650-9D3E-9EAE3D9BC670}')

# IGraphicsCaptureItem (WinRT Interface)
# We treat it as IInspectable mostly, but valid for passing around.
IID_IGraphicsCaptureItem = GUID('{79C3F95B-31F7-4EC2-A464-632EF5D30760}')

# IDirect3D11CaptureFramePoolStatics
# IID: 7784056A-67AA-4D53-AE54-1088D5A8CA21
IID_IDirect3D11CaptureFramePoolStatics = GUID('{7784056A-67AA-4D53-AE54-1088D5A8CA21}')

# IDirect3D11CaptureFramePool
# IID: 24EB6D22-1975-422E-82E7-780DBD8DDF24
IID_IDirect3D11CaptureFramePool = GUID('{24EB6D22-1975-422E-82E7-780DBD8DDF24}')

# IGraphicsCaptureSession
# IID: 814E42A9-F70F-4AD7-939B-FDDCC6EB880D
IID_IGraphicsCaptureSession = GUID('{814E42A9-F70F-4AD7-939B-FDDCC6EB880D}')

# Helper Functions
def check_hresult(hr, msg="COM Error"):
    if hr != 0:
        raise OSError(f"{msg}: {hex(hr)}")

def ro_get_activation_factory(class_id_str, iid):
    print(f"WGC (Pure): Getting Factory for {class_id_str}...", flush=True)
    
    
    # DLL Loading Logic
    roapi = None
    combase = None
    
    import sys
    is_64bits = sys.maxsize > 2**32
    print(f"WGC (Pure): Python Arch: {'64-bit' if is_64bits else '32-bit'}", flush=True)
    
    try:
        # Load kernel32 explicitly first (sanity check)
        try:
             kernel32 = ctypes.WinDLL("kernel32.dll")
             print(f"WGC (Pure): Loaded kernel32: {kernel32}", flush=True)
        except OSError as e:
             print(f"WGC (Pure): Failed to load kernel32! {e}", flush=True)
             
        # Load ole32 explicitly (COM fundamental)
        try:
             ole32 = ctypes.WinDLL("ole32.dll")
             print(f"WGC (Pure): Loaded ole32: {ole32}", flush=True)
        except OSError as e:
             print(f"WGC (Pure): Failed to load ole32! {e}", flush=True)
             
        # 1. Try combase first (dependency)
        combase_path = r"C:\Windows\System32\combase.dll"
        try:
             combase = ctypes.WinDLL(combase_path)
             print(f"WGC (Pure): Loaded combase: {combase}", flush=True)
        except OSError as e:
             print(f"WGC (Pure): Failed to load combase.dll ({combase_path}): {e}", flush=True)
             try:
                 combase = ctypes.WinDLL("combase.dll")
                 print(f"WGC (Pure): Loaded combase (simple): {combase}", flush=True)
             except OSError as e2:
                 print(f"WGC (Pure): Failed to load combase (simple): {e2}", flush=True)

        # 2. Try roapi
        roapi_path = r"C:\Windows\System32\roapi.dll"
        import os
        if os.path.exists(roapi_path):
             print(f"WGC (Pure): Found roapi.dll at {roapi_path}", flush=True)
        else:
             print(f"WGC (Pure): NOT FOUND roapi.dll at {roapi_path}", flush=True)
             
        try:
             roapi = ctypes.WinDLL(roapi_path)
             print(f"WGC (Pure): Loaded roapi: {roapi}", flush=True)
        except OSError as e:
             print(f"WGC (Pure): Failed to load roapi.dll ({roapi_path}): {e}", flush=True)
             try:
                 roapi = ctypes.WinDLL("roapi.dll")
                 print(f"WGC (Pure): Loaded roapi (simple): {roapi}", flush=True)
             except OSError as e2:
                 print(f"WGC (Pure): Failed to load roapi (simple): {e2}", flush=True)
                 
        # 3. Fallback: Kernel32 LoadLibrary
        if not roapi or not combase:
             print("WGC (Pure): Standard ctypes.WinDLL failed. Trying kernel32.LoadLibrary...", flush=True)
             if not combase:
                 h_combase = kernel32.LoadLibraryW(combase_path)
                 if h_combase:
                     combase = ctypes.WinDLL(combase_path, handle=h_combase)
                     print(f"WGC (Pure): Loaded combase.dll via LoadLibrary: {hex(h_combase)}", flush=True)
                 else:
                     print(f"WGC (Pure): LoadLibrary combase failed: {kernel32.GetLastError()}", flush=True)

             if not roapi:
                 # Check if roapi functions are in combase!
                 print("WGC (Pure): Checking if RoGetActivationFactory is in combase...", flush=True)
                 if combase:
                     try:
                         # Try to access function from combase
                         func = combase.RoGetActivationFactory
                         print("WGC (Pure): Found RoGetActivationFactory in combase.dll!", flush=True)
                         roapi = combase # Use combase as roapi provider
                     except AttributeError:
                         print("WGC (Pure): RoGetActivationFactory NOT found in combase.dll.", flush=True)

                 if not roapi:
                     h_roapi = kernel32.LoadLibraryW(roapi_path)
                     if h_roapi:
                         roapi = ctypes.WinDLL(roapi_path, handle=h_roapi)
                         print(f"WGC (Pure): Loaded roapi.dll via LoadLibrary: {hex(h_roapi)}", flush=True)
                     else:
                         print(f"WGC (Pure): LoadLibrary roapi failed: {kernel32.GetLastError()}", flush=True)

        if not roapi and not combase:
             raise OSError("Failed to load roapi.dll or combase.dll")
        
        # If roapi still None but combase exists, try using combase as last resort
        if not roapi and combase:
             print("WGC (Pure): roapi missing, attempting to use combase as fallback...", flush=True)
             roapi = combase

    except Exception as e:
        print(f"WGC (Pure): DLL Load Critical Failure: {e}", flush=True)
        # Last ditch: windll (already loaded by python?)
        try:
             roapi = ctypes.windll.roapi
             combase = ctypes.windll.combase
             print("WGC (Pure): Fallback to windll.roapi/combase successful.", flush=True)
        except Exception as e2:
             print(f"WGC (Pure): Fallback failed: {e2}", flush=True)
             raise e2
    
    class_id_hstring = c_void_p()
    # WindowsCreateString signature: (sourceString, length, hstring)
    # Return HRESULT
    try:
         WindowsCreateString = combase.WindowsCreateString
         WindowsCreateString.argtypes = [wintypes.LPCWSTR, wintypes.UINT, POINTER(c_void_p)]
         WindowsCreateString.restype = HRESULT
         
         hr = WindowsCreateString(class_id_str, len(class_id_str), byref(class_id_hstring))
         check_hresult(hr, f"WindowsCreateString {class_id_str}")
    except AttributeError:
         # Some systems it's in a different DLL? No, should be combase.
         print("WGC (Pure): WindowsCreateString not found in combase.dll?", flush=True)
         raise OSError("WindowsCreateString not found")
    
    factory_ptr = c_void_p()
    
    try:
         RoGetActivationFactory = roapi.RoGetActivationFactory
         RoGetActivationFactory.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]
         RoGetActivationFactory.restype = HRESULT
         
         hr = RoGetActivationFactory(class_id_hstring, byref(iid), byref(factory_ptr))
         
         # Always delete string
         WindowsDeleteString = combase.WindowsDeleteString
         WindowsDeleteString.argtypes = [c_void_p]
         WindowsDeleteString.restype = HRESULT
         WindowsDeleteString(class_id_hstring)
         
         check_hresult(hr, f"RoGetActivationFactory {class_id_str}")
    except OSError as e:
         print(f"WGC (Pure): Factory activation failed: {e}", flush=True)
         raise e
    
    print(f"WGC (Pure): Got Factory Ptr: {factory_ptr}", flush=True)
    return factory_ptr

# Direct3D11 Device Creation (Pure Ctypes)
def create_d3d11_device_ptr():
    """Creates a D3D11 Device and returns an IDirect3DDevice (WinRT) pointer."""
    print("WGC (Pure): Creating Native D3D11 Device...", flush=True)
    d3d11 = ctypes.windll.d3d11
    
    device_ptr = c_void_p()
    D3D_DRIVER_TYPE_HARDWARE = 1
    D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
    D3D11_SDK_VERSION = 7
    
    d3d11.D3D11CreateDevice.argtypes = [
        c_void_p, c_int, c_void_p, c_int, c_void_p, c_int, c_int, 
        POINTER(c_void_p), c_void_p, c_void_p
    ]
    
    hr = d3d11.D3D11CreateDevice(
        None, D3D_DRIVER_TYPE_HARDWARE, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        None, 0, D3D11_SDK_VERSION, byref(device_ptr), None, None
    )
    check_hresult(hr, "D3D11CreateDevice")
    
    # Query IDXGIDevice
    p_unknown = cast(device_ptr, POINTER(POINTER(c_void_p)))
    vtbl = p_unknown.contents
    
    print(f"WGC (Pure): VTable Ptr: {vtbl}", flush=True)
    try:
         query_interface_addr = vtbl[0]
         print(f"WGC (Pure): QueryInterface Addr: {hex(query_interface_addr) if query_interface_addr else 'None'}", flush=True)
    except Exception as e:
         print(f"WGC (Pure): Failed to read VTable[0]: {e}", flush=True)
         
    QueryInterface_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
    QueryInterface = QueryInterface_proto(vtbl[0])
    
    IID_IDXGIDevice = GUID('{54ec77fa-1377-44e6-8c32-884572211a0b}')
    dxgi_device_ptr = c_void_p()
    hr = QueryInterface(device_ptr, byref(IID_IDXGIDevice), byref(dxgi_device_ptr))
    
    if hr != 0:
        print(f"WGC (Pure): QueryInterface IDXGIDevice failed ({hex(hr)}). Trying IDXGIDevice1...", flush=True)
        # Verify device_ptr integrity
        temp_ptr = c_void_p()
        IID_IUnknown = GUID('{00000000-0000-0000-C000-000000000046}')
        hr_sanity = QueryInterface(device_ptr, byref(IID_IUnknown), byref(temp_ptr))
        print(f"WGC (Pure): Sanity Check (IUnknown): {hex(hr_sanity)}", flush=True)
        if temp_ptr:
             # Release without checking protocol or error
             try:
                 Release_proto_simple = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
                 Release_simple = Release_proto_simple(vtbl[2])
                 Release_simple(temp_ptr)
             except: pass
        
        if hr_sanity != 0:
             print("WGC (Pure): Device Ptr corrupted!", flush=True)
             raise OSError("Device Pointer Invalid")
        
        # Try IDXGIDevice1, 2, 3
        # Ensure fresh pointer for result
        try_iids = [
             ('IDXGIDevice1', '{77db970f-6276-48ba-ba28-070143b4392c}'),
             ('IDXGIDevice2', '{050d4780-00de-4f60-8c96-50ca9d7f767a}'),
             ('IDXGIDevice3', '{2633066b-4514-4c7a-8fd8-12ea98059d18}')
        ]
        
        success = False
        for name, iid_str in try_iids:
            try:
                print(f"WGC (Pure): Trying QueryInterface {name}...", flush=True)
                dxgi_device_ptr = c_void_p()
                iid = GUID(iid_str)
                hr = QueryInterface(device_ptr, byref(iid), byref(dxgi_device_ptr))
                if hr == 0 and dxgi_device_ptr:
                    print(f"WGC (Pure): {name} Found!", flush=True)
                    success = True
                    break
                else:
                    print(f"WGC (Pure): {name} failed: {hex(hr)}", flush=True)
            except Exception as e:
                print(f"WGC (Pure): {name} Access Violation: {e}", flush=True)
                
        if not success:
             print("WGC (Pure): All IDXGIDevice queries failed.", flush=True)
             # Last resort: Try creating from device_ptr directly? (Risky)
             pass
        else:
             # Just set hr=0 to pass check
             hr = 0
             
    check_hresult(hr, "QueryInterface IDXGIDevice(Variant)")
    
    # CreateDirect3D11DeviceFromDXGIDevice
    try:
        func = d3d11.CreateDirect3D11DeviceFromDXGIDevice
    except:
        # Fallback for very old systems? Unlikely.
        raise OSError("CreateDirect3D11DeviceFromDXGIDevice not exported from d3d11.dll")
        
    func.argtypes = [c_void_p, POINTER(c_void_p)]
    func.restype = HRESULT
    
    inspectable_ptr = c_void_p()
    
    print("WGC (Pure): Calling CreateDirect3D11DeviceFromDXGIDevice...", flush=True)
    try:
         hr = func(dxgi_device_ptr, byref(inspectable_ptr))
         print(f"WGC (Pure): CreateDirect3D11DeviceFromDXGIDevice returned {hex(hr)}", flush=True)
         check_hresult(hr, "CreateDirect3D11DeviceFromDXGIDevice")
    except OSError as e:
         print(f"WGC (Pure): CreateDirect3D11DeviceFromDXGIDevice failed (OSError): {e}", flush=True)
         raise e
    
    # Cleanup native pointers (CreateDirect3D11.. AddRefs)
    Release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
    
    if dxgi_device_ptr:
        try:
            print("WGC (Pure): Releasing dxgi_device_ptr...", flush=True)
            p_dxgi = cast(dxgi_device_ptr, POINTER(POINTER(c_void_p)))
            vtbl_dxgi = p_dxgi.contents
            Release_dxgi = Release_proto(vtbl_dxgi[2])
            Release_dxgi(dxgi_device_ptr)
            print("WGC (Pure): Released dxgi_device_ptr.", flush=True)
        except Exception as e:
            print(f"WGC (Pure): Release dxgi_device_ptr failed: {e}", flush=True)
            
    if device_ptr:
        try:
             print("WGC (Pure): Releasing device_ptr...", flush=True)
             Release = Release_proto(vtbl[2])
             Release(device_ptr)
             print("WGC (Pure): Released device_ptr.", flush=True)
        except Exception as e:
             print(f"WGC (Pure): Release device_ptr failed: {e}", flush=True)
    
    print(f"WGC (Pure): Created WinRT Device Ptr: {inspectable_ptr}", flush=True)
    return inspectable_ptr

def create_capture_item(hwnd):
    """Creates a GraphicsCaptureItem from HWND using interop."""
    print(f"WGC (Pure): Creating Capture Item for HWND {hwnd}...", flush=True)
    
    
    # 1. Try requesting IGraphicsCaptureItemInterop directly
    # Note: On some Windows builds (Insider/Canary), the IID might differ.
    # Standard: 3628E819-3F9E-4161-8FC1-9F20D44922B0
    # Found on User: 3B92ACC9-E584-5862-BF5C-9C316C6D2DBB (Using this one)
    # Let's try attempting current known working IID first
    IID_IGraphicsCaptureItemInterop = GUID('{3B92ACC9-E584-5862-BF5C-9C316C6D2DBB}')
    
    try:
        factory_ptr = ro_get_activation_factory(
            "Windows.Graphics.Capture.GraphicsCaptureItem", 
            IID_IGraphicsCaptureItemInterop
        )
    except OSError as e:
        print(f"WGC (Pure): Direct factory request failed: {e}. Trying IUnknown/IActivationFactory...", flush=True)
        # 2. Try requesting IActivationFactory (00000035-0000-0000-C000-000000000046)
        IID_IActivationFactory = GUID('{00000035-0000-0000-C000-000000000046}')
        try:
             factory_base = ro_get_activation_factory(
                 "Windows.Graphics.Capture.GraphicsCaptureItem",
                 IID_IActivationFactory
             )
             print(f"WGC (Pure): Got IActivationFactory: {factory_base}", flush=True)
             
             # QI for Interop
             p_factory = cast(factory_base, POINTER(POINTER(c_void_p)))
             vtbl = p_factory.contents
             
             # Remove excessive debug logs here
             
             QueryInterface_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
             QueryInterface = QueryInterface_proto(vtbl[0])
             
             factory_ptr = c_void_p()
             hr = QueryInterface(factory_base, byref(IID_IGraphicsCaptureItemInterop), byref(factory_ptr))
             
             # Release base
             Release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
             Release = Release_proto(vtbl[2])
             Release(factory_base)
             
             check_hresult(hr, "QueryInterface IGraphicsCaptureItemInterop")
             print(f"WGC (Pure): StartFactory QI Success: {factory_ptr}", flush=True)
             
        except Exception as e2:
             print(f"WGC (Pure): Fallback Factory QI failed: {e2}", flush=True)
             
             # Last ditch: Try standard IID if the above failed
             try:
                 print("WGC (Pure): Trying Standard IID 3628E819...", flush=True)
                 IID_Standard = GUID('{3628E819-3F9E-4161-8FC1-9F20D44922B0}')
                 factory_ptr = ro_get_activation_factory("Windows.Graphics.Capture.GraphicsCaptureItem", IID_Standard)
             except:
                 raise e2
    
    # IGraphicsCaptureItemInterop VTable
    # [0] QI, [1] AddRef, [2] Release, [3] CreateForWindow

    
    # IGraphicsCaptureItemInterop VTable
    # [0] QI, [1] AddRef, [2] Release, [3] GetIids, [4] GetRuntimeClassName, [5] GetTrustLevel, [6] CreateForWindow?
    # Since 3b92acc9 was listed by GetIids, it likely inherits from IInspectable.
    # So CreateForWindow would be at index 6.
    p_factory = cast(factory_ptr, POINTER(POINTER(c_void_p)))
    vtbl = p_factory.contents
    
    # Debug: Check Runtime Class Name
    # [4] GetRuntimeClassName
    try:
        GetRuntimeClassName_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
        GetRuntimeClassName = GetRuntimeClassName_proto(vtbl[4])
        
        hstring_ptr = c_void_p()
        hr_name = GetRuntimeClassName(factory_ptr, byref(hstring_ptr))
        
        if hr_name == 0 and hstring_ptr:
            # We need WindowsDeleteString to free it, and WindowsGetStringRawBuffer to read it.
            # Lazy import just for debug
            try:
                combase = ctypes.WinDLL("combase.dll")
                buffer = combase.WindowsGetStringRawBuffer(hstring_ptr, None)
                if buffer:
                    name = ctypes.wstring_at(buffer)
                    print(f"WGC (Pure): Factory Runtime Class: {name}", flush=True)
                combase.WindowsDeleteString(hstring_ptr)
            except: pass
        else:
            print(f"WGC (Pure): GetRuntimeClassName failed: {hex(hr_name)}", flush=True)
    except Exception as e_debug:
        print(f"WGC (Pure): Debug Runtime Class failed: {e_debug}", flush=True)

    
    # Using HWND as c_void_p (might need check)
    # HWND is usually int/long in python ctypes, but passed as c_void_p works.
    # Wintypes.HWND ensures correct ABI.
    CreateForWindow_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, wintypes.HWND, POINTER(GUID), POINTER(c_void_p))
    
    # Try index 6 (CreateForWindow on IInspectable-derived interface)
    print(f"WGC (Pure): Calling CreateForWindow(VTBL[6]) on {factory_ptr} for HWND {hwnd}...", flush=True)
    
    # Hypothesis: CreateForWindow might take only 2 args: HWND, void**
    # (If using the specific interface, IID might be implicit)
    print(f"WGC (Pure): Attempting CreateForWindow(VTBL[6]) with 2 args (HWND, Result*)...", flush=True)
    CreateForWindow_proto_2 = ctypes.WINFUNCTYPE(HRESULT, c_void_p, wintypes.HWND, POINTER(c_void_p))
    item_ptr = c_void_p()
    
    hr = CreateForWindow_proto_2(vtbl[6])(factory_ptr, wintypes.HWND(hwnd), byref(item_ptr))
    print(f"WGC (Pure): CreateForWindow(2-arg) returned hr={hex(hr)} item_ptr={item_ptr}", flush=True)
    
    if not item_ptr:
        # Try CreateForMonitor logic (Index 7?)
        # HMONITOR MonitorFromWindow(HWND hwnd, DWORD flags);
        try:
            user32 = ctypes.windll.user32
            hmonitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2) # MONITOR_DEFAULTTONEAREST = 2
            print(f"WGC (Pure): MonitorFromWindow({hwnd}) -> {hmonitor}", flush=True)
            
            if hmonitor:
                 # CreateForMonitor is likely at Index 7 (if VTable[6] is CreateForWindow)
                 # HRESULT CreateForMonitor(HMONITOR, REFIID, void**)
                 print(f"WGC (Pure): Attempting CreateForMonitor({hmonitor})...", flush=True)
                 CreateForMonitor_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, wintypes.HMONITOR, POINTER(GUID), POINTER(c_void_p))
                 
                 # Try index 7
                 CreateForMonitor = CreateForMonitor_proto(vtbl[7])
                 
                 hr_mon = CreateForMonitor(factory_ptr, wintypes.HMONITOR(hmonitor), byref(IID_IGraphicsCaptureItem), byref(item_ptr))
                 print(f"WGC (Pure): CreateForMonitor returned hr={hex(hr_mon)} item_ptr={item_ptr}", flush=True)
                 
                 if item_ptr:
                     print("WGC (Pure): SUCCESS! Captured Monitor instead of Window.", flush=True)
                 else:
                     print("WGC (Pure): CreateForMonitor also returned NULL.", flush=True)
        except Exception as e_mon:
            print(f"WGC (Pure): CreateForMonitor failed: {e_mon}", flush=True)
    
    # Release factory
    Release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
    Release = Release_proto(vtbl[2])
    Release(factory_ptr)
    
    check_hresult(hr, "CreateForWindow")
    return item_ptr

def create_frame_pool(device_ptr, pixel_format, num_buffers, size_width, size_height):
    """Creates a Direct3D11CaptureFramePool."""
    print("WGC (Pure): Creating Frame Pool...", flush=True)
    factory_ptr = ro_get_activation_factory(
        "Windows.Graphics.Capture.Direct3D11CaptureFramePool",
        IID_IDirect3D11CaptureFramePoolStatics
    )
    
    # IDirect3D11CaptureFramePoolStatics VTable
    # IInspectable: 0-5
    # [6] CreateFreeThreaded
    
    p_factory = cast(factory_ptr, POINTER(POINTER(c_void_p)))
    vtbl = p_factory.contents
    
    # struct Size { int32 Width; int32 Height; }
    class Size(Structure):
        _fields_ = [("Width", c_int), ("Height", c_int)]
        
    size_struct = Size(size_width, size_height)
    
    CreateFreeThreaded_proto = ctypes.WINFUNCTYPE(
        HRESULT, 
        c_void_p,           # this
        c_void_p,           # device (IDirect3DDevice*)
        c_int,              # pixelFormat (int)
        c_int,              # numberOfBuffers (int)
        Size,               # size (struct by value)
        POINTER(c_void_p)   # result (IDirect3D11CaptureFramePool**)
    )
    
    CreateFreeThreaded = CreateFreeThreaded_proto(vtbl[6])
    
    pool_ptr = c_void_p()
    hr = CreateFreeThreaded(factory_ptr, device_ptr, pixel_format, num_buffers, size_struct, byref(pool_ptr))
    
    # Release factory
    Release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
    Release = Release_proto(vtbl[2])
    Release(factory_ptr)
    
    check_hresult(hr, "CreateFreeThreaded")
    print(f"WGC (Pure): Frame Pool Ptr: {pool_ptr}", flush=True)
    return pool_ptr

def create_session(item_ptr, pool_ptr):
    """Creates a GraphicsCaptureSession from Pool and Item."""
    print("WGC (Pure): Creating Capture Session...", flush=True)
    
    # IDirect3D11CaptureFramePool::CreateCaptureSession(item, result)
    # VTable definition needed for IDirect3D11CaptureFramePool
    
    # IDirect3D11CaptureFramePool inherits from IInspectable
    # [6] Recreate
    # [7] TryGetNextFrame
    # [8] add_FrameArrived
    # [9] remove_FrameArrived
    # [10] CreateCaptureSession
    # [11] DispatcherQueue
    
    p_pool = cast(pool_ptr, POINTER(POINTER(c_void_p)))
    vtbl = p_pool.contents
    
    CreateCaptureSession_proto = ctypes.WINFUNCTYPE(
        HRESULT,
        c_void_p,           # this
        c_void_p,           # item (IGraphicsCaptureItem*)
        POINTER(c_void_p)   # result (IGraphicsCaptureSession**)
    )
    
    CreateCaptureSession = CreateCaptureSession_proto(vtbl[10]) # Index 10
    
    session_ptr = c_void_p()
    hr = CreateCaptureSession(pool_ptr, item_ptr, byref(session_ptr))
    check_hresult(hr, "CreateCaptureSession")
    
    print(f"WGC (Pure): Session Ptr: {session_ptr}", flush=True)
    return session_ptr

def start_session(session_ptr):
    """Starts the capture session."""
    print("WGC (Pure): Starting Session...", flush=True)
    p_session = cast(session_ptr, POINTER(POINTER(c_void_p)))
    vtbl = p_session.contents
    
    # IGraphicsCaptureSession
    # IInspectable: 0-5
    # [6] StartCapture
    
    StartCapture_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p)
    StartCapture = StartCapture_proto(vtbl[6])
    
    hr = StartCapture(session_ptr)
    check_hresult(hr, "StartCapture")
    print("WGC (Pure): Session Started.", flush=True)

class WGCCapture:
    def __init__(self):
        self.device_ptr = None
        self.item_ptr = None
        self.pool_ptr = None
        self.session_ptr = None
        self.running = False
        self.lock = threading.RLock()
        
    def initialize(self):
        # COM Initialization for WinRT
        # RO_INIT_MULTITHREADED = 1
        print("WGC (Pure): Initializing COM (RoInitialize)...", flush=True)
        try:
            hr = -1
            # Try loading roapi via kernel32 first as it seems most reliable
            
            # 1. Try roapi (kernel32)
            try:
                kernel32 = ctypes.windll.kernel32
                h_roapi = kernel32.LoadLibraryW(r"C:\Windows\System32\roapi.dll")
                if h_roapi:
                    print(f"WGC (Pure): Loaded roapi.dll for Init via LoadLibrary: {hex(h_roapi)}", flush=True)
                    roapi = ctypes.WinDLL(r"C:\Windows\System32\roapi.dll", handle=h_roapi)
                    hr = roapi.RoInitialize(0)
                else:
                    print(f"WGC (Pure): LoadLibrary roapi failed for Init: {kernel32.GetLastError()}", flush=True)
                    # Fallback
                    hr = ctypes.windll.roapi.RoInitialize(0)
            except Exception as ex:
                print(f"WGC (Pure): RoInitialize(roapi) logic failure: {ex}", flush=True)

            if hr == -1:
                 # Check if RoInitialize is in combase directly
                 try:
                     print("WGC (Pure): Checking for RoInitialize in combase.dll...", flush=True)
                     combase = ctypes.WinDLL(r"C:\Windows\System32\combase.dll")
                     if hasattr(combase, 'RoInitialize'):
                         print("WGC (Pure): Found RoInitialize in combase.dll!", flush=True)
                         
                         try:
                             print("WGC (Pure): Attempting to load extra dependencies (dcomp, dwmapi)...", flush=True)
                             try: ctypes.WinDLL("dcomp.dll")
                             except: pass
                             try: ctypes.WinDLL("dwmapi.dll")
                             except: pass
                             
                             print("WGC (Pure): Attempting to load Windows.UI.Core.dll and Windows.UI.Xaml.dll (for WGC proxies)...", flush=True)
                             # Windows.Graphics.Capture.dll is often merged into these or requires them
                             try: ctypes.WinDLL(r"C:\Windows\System32\Windows.UI.Core.dll")
                             except: pass
                             try: ctypes.WinDLL(r"C:\Windows\System32\Windows.UI.Xaml.dll")
                             except: pass
                             
                             # Still try Windows.Graphics.Capture.dll just in case, but don't fail hard
                             try:
                                 kernel32.LoadLibraryW(r"C:\Windows\System32\Windows.Graphics.Capture.dll")
                             except: pass

                         except Exception as e: 
                             print(f"WGC (Pure): Failed to load proxy DLLs: {e}", flush=True)
    
                         try:
                             ctypes.WinDLL(r'C:\Windows\System32\CoreMessaging.dll')
                             print("WGC (Pure): Loaded CoreMessaging.dll (for proxies)", flush=True)
                         except: pass
                         
                         hr = combase.RoInitialize(0)
                     else:
                         print("WGC (Pure): RoInitialize NOT found in combase.dll.", flush=True)
                 except OSError as e:
                     print(f"WGC (Pure): combase load failed during RoInit check: {e}", flush=True)

         # 3. Fallback: windll.roapi (if loaded)
            if hr == -1:
                 try:
                     print("WGC (Pure): Fallback to windll.roapi for RoInitialize...", flush=True)
                     hr = ctypes.windll.roapi.RoInitialize(0)
                 except: pass
    
            
            # S_OK (0) or S_FALSE (1, already init) or RPC_E_CHANGED_MODE (0x80010106)
            if hr == 0 or hr == 1:
                print(f"WGC (Pure): RoInitialize(0) OK ({hex(hr)}).", flush=True)
            elif hr == 0x80010106: # RPC_E_CHANGED_MODE
                print(f"WGC (Pure): RoInitialize(0) RPC_E_CHANGED_MODE (Thread already init different mode?). Ignoring.", flush=True)
            elif hr == -1:
                print("WGC (Pure): RoInitialize(0) failed to execute or return.", flush=True)
            else:
                print(f"WGC (Pure): RoInitialize(0) returned {hex(hr)}. Trying CoInitializeEx...", flush=True)
                ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception as e:
            print(f"WGC (Pure): RoInitialize Critical Failure: {e}. Trying CoInitializeEx...", flush=True)
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 0) # COINIT_MULTITHREADED
            except: pass

        try:
            self.device_ptr = create_d3d11_device_ptr()
        except Exception as e:
            print(f"WGC Init Fail: {e}")
            self.device_ptr = None
            
    def start(self, target: Dict[str, Any]):
        with self.lock:
            if self.running: return
            
            hwnd = target.get('hwnd') or target.get('id')
            
            print(f"WGC (Pure): Start requested for target: {target}", flush=True)
            
            if not hwnd: 
                print("WGC (Pure): No HWND in target! Aborting.", flush=True)
                return
            
            try:
                if not self.device_ptr:
                    self.initialize()
                    
                if not self.device_ptr:
                    print("WGC: No Device")
                    return

                self.item_ptr = create_capture_item(hwnd)
                
                # Get item size? 
                # IGraphicsCaptureItem::Size is prop get at index 7 (0-5 Ins, 6 Name, 7 Size)
                # struct Size { width, height }
                # The garbage size (-23370384) confirms we were calling Name (6) and getting a pointer.
                class Size(Structure):
                     _fields_ = [("Width", c_int), ("Height", c_int)]
                     
                p_item = cast(self.item_ptr, POINTER(POINTER(c_void_p)))
                vtbl_item = p_item.contents
                
                GetSize_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(Size))
                GetSize = GetSize_proto(vtbl_item[7]) # Index 7
                
                size = Size()
                hr = GetSize(self.item_ptr, byref(size))
                
                # Handling Minimized Windows (Size 0x0) OR Tiny Placeholder
                # Chrome minimized returns ~160x28. Treat anything < 200x100 as "minimized" state to be safe?
                # Actually, 50x50 is safe enough for "not real content".
                if hr == S_OK and (size.Width <= 50 or size.Height <= 50):
                    print(f"WGC (Pure): Target is minimized/tiny ({size.Width}x{size.Height}). Deferring Pool creation.", flush=True)
                    self.running = True # Pretend we started, will try to init pool in get_latest_frame
                    return

                check_hresult(hr, "GetSize")
                
                print(f"WGC (Pure): Item Size: {size.Width}x{size.Height}", flush=True)
                
                self.current_size = (size.Width, size.Height)
                self.pool_ptr = create_frame_pool(
                    self.device_ptr, 
                    DXGI_FORMAT_B8G8R8A8_UNORM, 
                    2, 
                    size.Width, 
                    size.Height
                )
                
                self.session_ptr = create_session(self.item_ptr, self.pool_ptr)
                start_session(self.session_ptr)
                
                self.running = True
                
            except Exception as e:
                print(f"WGC Start Error: {e}")
                self.stop()

    def get_latest_frame(self) -> Optional[Any]:
        # Polling implementation:
        if not self.running: return None

        # Lazy Initialization for Minimized Windows
        if not self.pool_ptr and self.item_ptr and self.device_ptr:
             try:
                # 1. Check size again
                class Size(Structure):
                     _fields_ = [("Width", c_int), ("Height", c_int)]
                     
                p_item = cast(self.item_ptr, POINTER(POINTER(c_void_p)))
                vtbl_item = p_item.contents
                
                GetSize_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(Size))
                GetSize = GetSize_proto(vtbl_item[7]) # Index 7
                
                size = Size()
                hr = GetSize(self.item_ptr, byref(size))
                
                if hr == S_OK and size.Width > 50 and size.Height > 50:
                     print(f"WGC (Pure): Window restored! Size: {size.Width}x{size.Height}. Creating Pool...", flush=True)
                     self.current_size = (size.Width, size.Height)
                     self.pool_ptr = create_frame_pool(
                        self.device_ptr, 
                        DXGI_FORMAT_B8G8R8A8_UNORM, 
                        2, 
                        size.Width, 
                        size.Height
                    )
                     self.session_ptr = create_session(self.item_ptr, self.pool_ptr)
                     start_session(self.session_ptr)
                else:
                     # Still minimized
                     return None
             except Exception as e:
                 print(f"WGC Lazy Init Failed: {e}")
                 return None

        # TryGetNextFrame on the pool
        if not self.pool_ptr: return None
        
        try:
            p_pool = cast(self.pool_ptr, POINTER(POINTER(c_void_p)))
            vtbl = p_pool.contents
            
            # [7] TryGetNextFrame(result)
            TryGetNextFrame_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
            TryGetNextFrame = TryGetNextFrame_proto(vtbl[7])
            
            # Drain frame pool to get ONLY the latest available frame
            latest_frame_ptr = None
            while True:
                curr_ptr = c_void_p()
                hr = TryGetNextFrame(self.pool_ptr, byref(curr_ptr))
                if hr != 0 or not curr_ptr:
                    break
                if latest_frame_ptr:
                    # Release previous frame
                    try:
                        p_old = cast(latest_frame_ptr, POINTER(POINTER(c_void_p)))
                        Release_old = ctypes.WINFUNCTYPE(c_ulong, c_void_p)(p_old.contents[2])
                        Release_old(latest_frame_ptr)
                    except:
                        pass
                latest_frame_ptr = curr_ptr
                
            if not latest_frame_ptr:
                return None

            frame_ptr = latest_frame_ptr
            
            try:
                # We got a frame!
                # 0. Check ContentSize and Recreate Pool if needed
                # Direct3D11CaptureFrame::ContentSize [8]
                p_frame = cast(frame_ptr, POINTER(POINTER(c_void_p)))
                vtbl_frame = p_frame.contents
                
                class SizeInt32(Structure):
                    _fields_ = [("Width", c_int), ("Height", c_int)]

                GetContentSize_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(SizeInt32))
                GetContentSize = GetContentSize_proto(vtbl_frame[8])
                
                content_size = SizeInt32()
                hr = GetContentSize(frame_ptr, byref(content_size))
                
                if hr == 0:
                     current_w, current_h = getattr(self, 'current_size', (0,0))
                     if (content_size.Width != current_w or content_size.Height != current_h) and content_size.Width > 50:
                         print(f"WGC: Size Changed {current_w}x{current_h} -> {content_size.Width}x{content_size.Height}. Recreating Pool.", flush=True)
                         
                         # Recreate Pool
                         # We can call Recreate() on the pool, or just create a new one.
                         # IDirect3D11CaptureFramePool::Recreate(device, pixelFormat, numberOfBuffers, size) [8]
                         # Let's try Recreate first.
                         p_pool_recreate = cast(self.pool_ptr, POINTER(POINTER(c_void_p)))
                         vtbl_pool = p_pool_recreate.contents
                         Recreate_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_int, c_int, SizeInt32)
                         # Wait, SizeInt32 is struct by value? No, in C API usually passed by value or ref?
                         # WinRT uses struct SizeInt32 directly.
                         # Function sig: Recreate(device, pixelFormat, numberOfBuffers, size)
                         
                         # We need to find index of Recreate.
                         # IInspectable: 6 methods.
                         # Create: Static.
                         # Recreate: Method on instance?
                         # Looking at IDirect3D11CaptureFramePool:
                         # 6: Recreate(device, pixelFormat, numberOfBuffers, size)
                         
                         Recreate = Recreate_proto(vtbl_pool[6])
                         # SizeInt32 passed by value in WinRT ABI? Usually passed as struct.
                         # ctypes handles struct by value if specified in argtypes.
                         # Recreate_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_int, c_int, SizeInt32)
                         
                         # hr = Recreate(self.pool_ptr, self.device_ptr, DXGI_FORMAT_B8G8R8A8_UNORM, 2, content_size)
                         # If Recreate fails or too complex, just release and create new?
                         # Let's use CreateFreeThreaded (Static) again? No, that makes a NEW pool.
                         # We have a valid pool instance.
                         # Let's try Recreate.
                         
                         # If this is risky, we can just update self.current_size and let next frame handle it?
                         # No, we need a pool that matches the size often.
                         # Actually WGC allows mismatch, but it might crop/scale.
                         # Better to recreate.
                         
                         # SIMPLER: specific Recreate VTable index is tricky without metadata.
                         # Safe fallback: Treat "Resize" as "Stop/Start" internally?
                         # Or just Update self.current_size and ignore? 
                         # If we don't recreate, we get the old size's staging texture later.
                         
                         # Let's just update self.current_size for Staging creation!
                         # Because the Frame content IS the new size.
                         # The Pool size just determines buffer size. 
                         # If Frame is bigger than Pool, does it crash?
                         
                         # Let's update current_size and see if Staging Texture creation handles it.
                         self.current_size = (content_size.Width, content_size.Height)
                         
                # 1. Get Surface (IDirect3DSurface) from Frame
                # Direct3D11CaptureFrame inherits from IInspectable
                # [6] get_Surface(IDirect3DSurface**)
                p_frame = cast(frame_ptr, POINTER(POINTER(c_void_p)))
                vtbl_frame = p_frame.contents
                
                GetSurface_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
                GetSurface = GetSurface_proto(vtbl_frame[6])
                
                surface_ptr = c_void_p()
                hr = GetSurface(frame_ptr, byref(surface_ptr))
                check_hresult(hr, "GetSurface")
                
                # 2. Get IDirect3D11Texture2D from surface (via IDirect3DDxgiInterfaceAccess or similar)
                # Actually, IDirect3DSurface is a WinRT wrapper. We need to QI for IDirect3DDxgiInterfaceAccess.
                # IID_IDirect3DDxgiInterfaceAccess = A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1
                IID_IDirect3DDxgiInterfaceAccess = GUID('{A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1}')
                
                p_surface = cast(surface_ptr, POINTER(POINTER(c_void_p)))
                vtbl_surface = p_surface.contents
                QI_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
                QI = QI_proto(vtbl_surface[0])
                
                access_ptr = c_void_p()
                hr = QI(surface_ptr, byref(IID_IDirect3DDxgiInterfaceAccess), byref(access_ptr))
                check_hresult(hr, "QI IDirect3DDxgiInterfaceAccess")
                
                # 3. GetInterface (Get Texture ptr)
                # IDirect3DDxgiInterfaceAccess::GetInterface(IID, void**) [3]
                p_access = cast(access_ptr, POINTER(POINTER(c_void_p)))
                vtbl_access = p_access.contents
                GetInterface_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
                GetInterface = GetInterface_proto(vtbl_access[3])
                
                texture_ptr = c_void_p()
                IID_ID3D11Texture2D = GUID('{6f15aaf2-d208-4e89-9ab4-489535d34f9c}')
                hr = GetInterface(access_ptr, byref(IID_ID3D11Texture2D), byref(texture_ptr))
                check_hresult(hr, "GetInterface ID3D11Texture2D")
                
                # 4. Copy to Staging Texture
                # We need a staging texture on the device to read from CPU.
                # If we don't have one, create it.
                # Assuming frame size matches pool size (which matches CreateFramePool)
                # Ideally check texture desc first.
                
                # Context->CopyResource(staging, source)
                # Get Device Context
                # ID3D11Device::GetImmediateContext(ID3D11DeviceContext**) [40] (approx? check vtable)
                # Actually we can get it from self.device_ptr (which is ID3D11Device)
                # Wait, self.device_ptr is IDirect3DDevice (WinRT). We need native ID3D11Device.
                # We released the native one early in initialize! 
                # We need to Keep native D3D11 pointer or QI for it from WinRT wrapper or re-create context.
                # Better: In initialize, save d3d_device_ptr (native).
                
                # CRITICAL: We need the native ID3D11DeviceContext to copy resources.
                # Since we don't have it saved, let's QI the texture for its device?
                # ID3D11DeviceChild::GetDevice [3]
                p_texture = cast(texture_ptr, POINTER(POINTER(c_void_p)))
                vtbl_texture = p_texture.contents
                GetDevice_proto = ctypes.WINFUNCTYPE(None, c_void_p, POINTER(c_void_p))
                GetDevice = GetDevice_proto(vtbl_texture[3])
                
                native_device_ptr = c_void_p()
                GetDevice(texture_ptr, byref(native_device_ptr))
                
                # Now get context
                # ID3D11Device::GetImmediateContext [40]
                p_native_device = cast(native_device_ptr, POINTER(POINTER(c_void_p)))
                vtbl_native_device = p_native_device.contents
                GetImmediateContext_proto = ctypes.WINFUNCTYPE(None, c_void_p, POINTER(c_void_p))
                GetImmediateContext = GetImmediateContext_proto(vtbl_native_device[40])
                
                context_ptr = c_void_p()
                GetImmediateContext(native_device_ptr, byref(context_ptr))
                
                # Create Staging Texture (if needed)
                # We need texture desc to know size
                # ID3D11Texture2D::GetDesc [10]
                class D3D11_TEXTURE2D_DESC(Structure):
                    _fields_ = [
                        ("Width", c_uint),
                        ("Height", c_uint),
                        ("MipLevels", c_uint),
                        ("ArraySize", c_uint),
                        ("Format", c_int),
                        ("SampleDesc_Count", c_uint),
                        ("SampleDesc_Quality", c_uint),
                        ("Usage", c_int),
                        ("BindFlags", c_uint),
                        ("CPUAccessFlags", c_uint),
                        ("MiscFlags", c_uint),
                    ]
                
                GetDesc_proto = ctypes.WINFUNCTYPE(None, c_void_p, POINTER(D3D11_TEXTURE2D_DESC))
                GetDesc = GetDesc_proto(vtbl_texture[10])
                desc = D3D11_TEXTURE2D_DESC()
                GetDesc(texture_ptr, byref(desc))
                
                # Create staging if not exists or size changed
                if not hasattr(self, 'staging_texture_ptr') or self.staging_desc.Width != desc.Width or self.staging_desc.Height != desc.Height:
                    # D3D11_USAGE_STAGING = 3
                    # D3D11_CPU_ACCESS_READ = 0x20000
                    staging_desc = D3D11_TEXTURE2D_DESC()
                    staging_desc.Width = desc.Width
                    staging_desc.Height = desc.Height
                    staging_desc.MipLevels = 1
                    staging_desc.ArraySize = 1
                    staging_desc.Format = desc.Format
                    staging_desc.SampleDesc_Count = 1
                    staging_desc.SampleDesc_Quality = 0
                    staging_desc.Usage = 3 # STAGING
                    staging_desc.BindFlags = 0
                    staging_desc.CPUAccessFlags = 0x20000 # READ
                    staging_desc.MiscFlags = 0
                    
                    # CreateTexture2D [5]
                    CreateTexture2D_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(D3D11_TEXTURE2D_DESC), c_void_p, POINTER(c_void_p))
                    CreateTexture2D = CreateTexture2D_proto(vtbl_native_device[5])
                    
                    new_staging_ptr = c_void_p()
                    hr_create = CreateTexture2D(native_device_ptr, byref(staging_desc), None, byref(new_staging_ptr))
                    check_hresult(hr_create, "CreateStagingTexture")
                    
                    if hasattr(self, 'staging_texture_ptr') and self.staging_texture_ptr:
                         # Release old?
                         pass 
                         
                    self.staging_texture_ptr = new_staging_ptr
                    self.staging_desc = staging_desc
                
                # Copy properties
                # ID3D11DeviceContext::CopyResource [47]
                p_context = cast(context_ptr, POINTER(POINTER(c_void_p)))
                vtbl_context = p_context.contents
                CopyResource_proto = ctypes.WINFUNCTYPE(None, c_void_p, c_void_p, c_void_p)
                CopyResource = CopyResource_proto(vtbl_context[47])
                
                CopyResource(context_ptr, self.staging_texture_ptr, texture_ptr)
                
                # Map
                # ID3D11DeviceContext::Map [14]
                class D3D11_MAPPED_SUBRESOURCE(Structure):
                    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]
                
                Map_proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_uint, c_int, c_uint, POINTER(D3D11_MAPPED_SUBRESOURCE))
                Map = Map_proto(vtbl_context[14])
                
                mapped_resource = D3D11_MAPPED_SUBRESOURCE()
                # Map(resource, subresource=0, mapType=1 (READ), flags=0, &mapped)
                hr_map = Map(context_ptr, self.staging_texture_ptr, 0, 1, 0, byref(mapped_resource))
                check_hresult(hr_map, "Map")
                
                # Copy data to buffer
                width = self.staging_desc.Width
                height = self.staging_desc.Height
                row_pitch = mapped_resource.RowPitch
                
                # Create array/buffer
                # Assuming simple copy for now
                # BGRA = 4 bytes per pixel
                buffer_size = height * row_pitch 
                # Read from pData
                if mapped_resource.pData:
                    # Use string_at to get bytes? No, may contain nulls.
                    # ctypes.memmove?
                    # Create a python bytes object
                    # Optimize: Just duplicate logic from capture_engine
                    import numpy as np
                    
                    # Basic validity check
                    if buffer_size > 0:
                         # Create a ctypes array from pointer
                         # data_type = c_char * buffer_size
                         # c_data = data_type.from_address(mapped_resource.pData)
                         # raw_bytes = bytes(c_data)
                         
                         # More efficient: ctypes.string_at(mapped_resource.pData, buffer_size)
                         raw_bytes = ctypes.string_at(mapped_resource.pData, buffer_size)
                         
                         # Convert to Image
                         from PIL import Image
                         # Stride issues?
                         # If RowPitch == Width * 4, straightforward.
                         # If not, need trimming.
                         
                         if row_pitch == width * 4:
                             image = Image.frombytes('RGBA', (width, height), raw_bytes, 'raw', 'BGRA')
                         else:
                             # Use only valid bytes per row
                             # This is slow in pure python.
                             # For now, just grab it.
                             image = Image.frombytes('RGBA', (width, height), raw_bytes, 'raw', 'BGRA', row_pitch, 1)

                         # Unmap
                         # ID3D11DeviceContext::Unmap [15]
                         Unmap_proto = ctypes.WINFUNCTYPE(None, c_void_p, c_void_p, c_uint)
                         Unmap = Unmap_proto(vtbl_context[15])
                         Unmap(context_ptr, self.staging_texture_ptr, 0)
                         
                         # Release temp objects
                         # Just native_device, context, access, texture, surface
                         # To be comprehensive, release them.
                         # For speed, skip explicit release of small transient objects? No, leaks.
                         # Need release logic.
                         
                         return image

                # If failed or didn't return above
                Unmap_proto = ctypes.WINFUNCTYPE(None, c_void_p, c_void_p, c_uint)
                Unmap = Unmap_proto(vtbl_context[15])
                Unmap(context_ptr, self.staging_texture_ptr, 0)
                
            except Exception as e_frame:
                print(f"WGC Frame Process Error: {e_frame}")
                
            finally:
                # Release frame
                p_frame = cast(frame_ptr, POINTER(POINTER(c_void_p)))
                vtbl_frame = p_frame.contents
                Release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
                Release = Release_proto(vtbl_frame[2])
                Release(frame_ptr)

        except Exception as e:
            # print(f"WGC Frame Error: {e}")
            pass
            
        return None
            


    def stop(self):
        with self.lock:
            self.running = False
            
            def safe_release(ptr):
                if ptr:
                    try:
                        p = cast(ptr, POINTER(POINTER(c_void_p)))
                        vtbl = p.contents
                        Release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
                        Release = Release_proto(vtbl[2])
                        Release(ptr)
                        pass
                    except: pass
            
            # Close/Dispose objects?
            # IClosable interface: Close() [6] Usually.
            
            if self.session_ptr:
                # Close session
                try:
                    p = cast(self.session_ptr, POINTER(POINTER(c_void_p)))
                    vtbl = p.contents
                    # IGraphicsCaptureSession::Close logic? 
                    # For now just release
                    pass
                except: pass
                safe_release(self.session_ptr)
                self.session_ptr = None
                
            if self.pool_ptr:
                # Pool is IClosable
                safe_release(self.pool_ptr)
                self.pool_ptr = None
                
            if self.item_ptr:
                safe_release(self.item_ptr)
                self.item_ptr = None

capture = WGCCapture()
