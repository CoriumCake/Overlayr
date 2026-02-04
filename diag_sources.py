import sys
import os
import ctypes
from ctypes import wintypes

# Add src to path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from overlayr.services.source_manager import SourceManager
from overlayr.core.capture_engine import CaptureEngine

def test_discovery():
    sm = SourceManager()
    ce = CaptureEngine()
    
    print("Fetching windows...")
    windows = sm.get_windows()
    
    print(f"\nFound {len(windows)} filtered windows:")
    for w in windows:
        print(f"- {w['title']} (Process: {w['process']}, HWND: {w['id']})")
        
        # Test thumbnail
        thumb = ce.get_thumbnail("window", w)
        if thumb:
            print(f"  [OK] Thumbnail captured: {thumb.size}")
        else:
            print(f"  [FAIL] Thumbnail capture failed")

if __name__ == "__main__":
    test_discovery()
