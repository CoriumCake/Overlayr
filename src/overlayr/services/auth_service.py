import winreg
import sys
import os
import json
import base64
import time
import requests
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# PLACEHOLDERS - To be replaced with actual Firebase Config
FIREBASE_PROJECT_ID = "overlayr-app" # Replace with your project ID
API_KEY = "YOUR_FIREBASE_API_KEY" # Needed for "Identity Toolkit" check if we want to refresh tokens

class AuthService:
    def __init__(self):
        self.app_data_dir = Path.home() / ".overlayr"
        self.app_data_dir.mkdir(exist_ok=True)
        self.token_file = self.app_data_dir / "auth_session.json"
        
        self.user_data = {}
        self.access_token = None
        self.uid = None
        
        # Load existing session
        self.load_session()

        # Register protocol handler on startup
        self.register_protocol()

    def register_protocol(self):
        """Registers the overlayr:// custom URI scheme in Windows Registry."""
        try:
            exe_path = sys.executable
            # If running as script (dev), point to python.exe + main.py ??
            # For now assume we want to register whatever is running.
            # IN PRODUCTION (Frozen): exe_path is the exe.
            # IN DEV: We might want code to handle this carefully.
            
            # Simplified for now: Register current executable
            # If dev, this might just open python.
            
            # Protocol Key
            key_path = r"Software\Classes\overlayr"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Overlayr Protocol")
                winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
                
            # Icon
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{key_path}\\DefaultIcon") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{exe_path},1")
                
            # Command
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{key_path}\\shell\\open\\command") as key:
                # "%1" receives the full url
                cmd = f'"{exe_path}" "%1"'
                # If running from source, might need adjustments
                if not getattr(sys, 'frozen', False):
                     # Hack for dev mode: assume main.py is in CWD or near
                     pass 
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, cmd)
                
        except Exception as e:
            print(f"[Auth] Failed to register protocol: {e}")

    def process_launch_args(self, args):
        """Checks if app was launched via protocol."""
        for arg in args:
            if arg.startswith("overlayr://"):
                self.handle_deep_link(arg)

    def handle_deep_link(self, url):
        """Parses overlayr://auth?token=...&uid=..."""
        parsed = urlparse(url)
        if parsed.netloc == "auth":
            params = parse_qs(parsed.query)
            token = params.get('token', [None])[0]
            uid = params.get('uid', [None])[0]
            
            if token and uid:
                self.save_session(uid, token)
                print(f"[Auth] Logged in as {uid}")
                return True
        return False

    def save_session(self, uid, token):
        self.uid = uid
        self.access_token = token
        data = {
            "uid": uid, 
            "access_token": token,
            "timestamp": time.time()
        }
        with open(self.token_file, 'w') as f:
            json.dump(data, f)
            
    def load_session(self):
        if self.token_file.exists():
            try:
                with open(self.token_file, 'r') as f:
                    data = json.load(f)
                    self.uid = data.get("uid")
                    self.access_token = data.get("access_token")
            except:
                pass

    def get_subscription_status(self):
        """
        Checks 1. Free Trial 2. Firestore Profile
        Returns: 'pro', 'trial', 'expired', 'none'
        """
        # 1. Check Local Trial (Frictionless)
        trial_status = self._check_local_trial()
        if trial_status['active']:
            return 'trial', trial_status['days_left']

        # 2. Check Firestore if Logged In
        if self.uid and self.access_token:
            profile = self._fetch_firestore_profile()
            if profile and profile.get('isPro'):
                # Check expiry if exists
                expiry = profile.get('expiryDate')
                if expiry:
                    # simplistic check, ideally parsed isoformat
                    if expiry > time.strftime("%Y-%m-%dT%H:%M:%S"):
                        return 'pro', 999
                    else:
                        return 'expired', 0
                return 'pro', 999 # Lifetime or no expiry set

        return 'none', 0

    def _check_local_trial(self):
        """
        Checks local 'first_run' date.
        Gives 7 days from first run.
        """
        trial_file = self.app_data_dir / "trial_config.json"
        now = time.time()
        
        if not trial_file.exists():
            # First run!
            with open(trial_file, 'w') as f:
                json.dump({"first_run": now}, f)
            return {"active": True, "days_left": 7}
            
        try:
            with open(trial_file, 'r') as f:
                data = json.load(f)
                first_run = data.get("first_run", now)
                
            elapsed_days = (now - first_run) / (24 * 3600)
            if elapsed_days < 7:
                 return {"active": True, "days_left": int(7 - elapsed_days)}
            else:
                 return {"active": False, "days_left": 0}
        except:
            return {"active": False, "days_left": 0}

    def _fetch_firestore_profile(self):
        """
        GET https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents/users/{UID}
        """
        if not self.uid: return None
        
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/users/{self.uid}"
        # Attach Bearer Token
        # NOTE: This access_token is the Firebase ID Token.
        headers = {"Authorization": f"Bearer {self.access_token}"}
        
        try:
            r = requests.get(url, headers=headers)
            if r.status_code == 200:
                doc = r.json()
                # Parse Firestore fields format
                # { "fields": { "isPro": { "booleanValue": true } ... } }
                fields = doc.get("fields", {})
                
                # Helper to unpack
                def unpack(f):
                    if 'booleanValue' in f: return f['booleanValue']
                    if 'stringValue' in f: return f['stringValue']
                    if 'timestampValue' in f: return f['timestampValue']
                    return None
                
                return {k: unpack(v) for k, v in fields.items()}
                
        except Exception as e:
            print(f"[Auth] Firestore check failed: {e}")
            
        return None
