
import sys
import os
sys.path.append(os.path.join(os.getcwd(), 'src'))
from overlayr.services.auth_service import AuthService

# Simulate deep link
link = "overlayr://auth?token=debug_token&uid=debug_user_123"

# Find running instance and send args... 
# Actually, the app listens for new args via SingleInstance.
# But SingleInstance uses a tricky mechanism (socket or window message).
# Let's just run a script that tries to open the *same* SingleInstance. 
# If the app is running, `main.py` handles the "notify existing" part.

os.system(f'python main.py "{link}"')
