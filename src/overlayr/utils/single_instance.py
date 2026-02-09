import win32pipe, win32file, pywintypes
import threading
import time
import sys
import os

PIPE_NAME = r'\\.\pipe\OverlayrSingleInstance'

class SingleInstance:
    def __init__(self, on_args_received=None):
        self.on_args_received = on_args_received
        self.is_running = False
        self.stop_event = threading.Event()

    def start_server(self):
        """Starts the named pipe server in a separate thread."""
        self.is_running = True
        self.thread = threading.Thread(target=self._server_loop, daemon=True)
        self.thread.start()
        
    def _server_loop(self):
        while not self.stop_event.is_set():
            try:
                pipe = win32pipe.CreateNamedPipe(
                    PIPE_NAME,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    1, 65536, 65536,
                    0,
                    None
                )
                
                win32pipe.ConnectNamedPipe(pipe, None)
                
                # Read data
                try:
                    resp = win32file.ReadFile(pipe, 64*1024)
                    data = resp[1].decode('utf-8')
                    if data and self.on_args_received:
                        # Parse args (simple split or similar, but typically we send raw string)
                        # We might receive valid JSON or just a string
                        # For deep links, it's usually just one arg "overlayr://..."
                        self.on_args_received(data.split('|||'))
                except Exception as e:
                    print(f"[SingleInstance] Read Error: {e}")
                finally:
                    win32file.CloseHandle(pipe)
                    
            except Exception as e:
                # print(f"[SingleInstance] Server loop error: {e}")
                time.sleep(1)

    def notify_existing_instance(self, args):
        """Tries to connect to an existing instance. Returns True if successful."""
        try:
            # Try to open the pipe
            handle = win32file.CreateFile(
                PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None
            )
            
            # Send args joined by delimiter
            msg = "|||".join(args)
            win32file.WriteFile(handle, msg.encode('utf-8'))
            win32file.CloseHandle(handle)
            return True
            
        except pywintypes.error as e:
            if e.args[0] == 2: # ERROR_FILE_NOT_FOUND
                return False
            return False
        except Exception:
            return False

    def stop(self):
        self.stop_event.set()
