
import sys
import os
import time

# Current dir to path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from overlayr.services.auth_service import AuthService

def debug_auth_flow():
    print("--- Starting Auth Flow Debug ---")
    
    auth = AuthService()
    
    # 1. Simulate receiving a token
    print("\n[1] Simulating Login...")
    fake_token = "fake_jwt_token_for_debug"
    fake_uid = "debug_user_001"
    
    # Manually save session
    auth.save_session(fake_uid, fake_token)
    print(f"Session saved for {fake_uid}")
    
    # 2. Fetch User Details (Checking API Key validity)
    print("\n[2] Fetching User Details...")
    # We need a REAL token for this to work with Firebase, 
    # but let's see if it at least TRIES and what the error is.
    auth.fetch_user_details()
    
    print(f"\n[3] User Data State: {auth.user_data}")
    
    # 3. Check UI Data
    print("\n[4] UI Data (get_user_info):")
    info = auth.get_user_info()
    print(info)
    
    if info['photo_url']:
        print("SUCCESS: Photo URL is present.")
    else:
        print("FAILURE: Photo URL is missing.")

if __name__ == "__main__":
    debug_auth_flow()
