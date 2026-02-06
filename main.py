import sys
import os

# Ensure src is in path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from overlayr.ui.main_window import MainWindow
from overlayr.services.auth_service import AuthService

if __name__ == "__main__":
    # Initialize Auth Service (checking for deep links)
    auth_service = AuthService()
    
    # Process arguments (e.g., overlayr://auth?token=...)
    if len(sys.argv) > 1:
        auth_service.process_launch_args(sys.argv)

    app = MainWindow(auth_service)
    app.mainloop()
