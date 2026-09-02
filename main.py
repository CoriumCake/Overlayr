import sys
import os

# Ensure src is in path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from overlayr.utils.console import make_output_safe

# Before anything can log a line of the user's language into a cp1252 console.
make_output_safe()

from overlayr.ui.main_window import MainWindow
from overlayr.services.auth_service import AuthService
from overlayr.services.config_service import ConfigService

if __name__ == "__main__":
    # Initialize Services
    config_service = ConfigService()
    auth_service = AuthService()

    # Single Instance Check
    try:
        from overlayr.utils.single_instance import SingleInstance
        
        def on_new_args(args):
            print(f"Received new args: {args}")
            # If we receive new args (like a deep link), process them
            if len(args) > 1:
                # Arg 0 is usually path, Arg 1 is overlayr://...
                # Filter for overlayr://
                links = [a for a in args if "overlayr://" in a]
                if links:
                    auth_service.handle_deep_link(links[0])

        instance = SingleInstance(on_args_received=on_new_args)
        
        # 1. Try to notify existing
        if instance.notify_existing_instance(sys.argv):
            print("Passed arguments to existing instance. Exiting.")
            sys.exit(0)
            
        # 2. If no existing, start server
        instance.start_server()
            
    except Exception as e:
        print(f"SingleInstance Error: {e}")

    
    # Process arguments (e.g., overlayr://auth?token=...)
    if len(sys.argv) > 1:
        auth_service.process_launch_args(sys.argv)

    app = MainWindow(auth_service, config_service)
    app.mainloop()
