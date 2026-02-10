import winreg
import sys
import os
import json
import base64
import time
import requests
import concurrent.futures
from urllib.parse import urlparse, parse_qs
from pathlib import Path

_auth_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# PLACEHOLDERS - To be replaced with actual Firebase Config
FIREBASE_PROJECT_ID = "overlayr-486323" # Replace with your project ID
API_KEY = "AIzaSyATZXidpVtX-hQpcdQDERaY1IYyrOOdpMQ" # Needed for "Identity Toolkit" check if we want to refresh tokens

class AuthService:
    def __init__(self):
        self.app_data_dir = Path.home() / ".overlayr"
        self.app_data_dir.mkdir(exist_ok=True)
        self.token_file = self.app_data_dir / "auth_session.json"
        
        self.user_data = {}
        self.access_token = None
        self.uid = None
        self.active_features: list[str] = []  # Synced from Firestore (written by webhook)
        
        # Load existing session
        self.load_session()

        # Register protocol handler on startup
        self.register_protocol()

    def register_protocol(self):
        """Registers the overlayr:// custom URI scheme in Windows Registry."""
        try:
            # Determine command based on run mode
            if getattr(sys, 'frozen', False):
                # Production (PyInstaller exe)
                exe_path = sys.executable
                cmd = f'"{exe_path}" "%1"'
            else:
                # Development (Python script)
                # Use pythonw.exe to avoid console window
                py_dir = os.path.dirname(sys.executable)
                pythonw = os.path.join(py_dir, "pythonw.exe")
                if not os.path.exists(pythonw):
                    pythonw = sys.executable 
                
                exe_path = pythonw
                script_path = os.path.abspath("main.py")
                cmd = f'"{pythonw}" "{script_path}" "%1"'
            
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
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, cmd)
                
            print(f"[Auth] Protocol registered: {cmd}")
                
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
                # Fetch more details immediately
                self.fetch_user_details()
                print(f"[Auth] Logged in as {uid}")
                return True
        return False

    def save_session(self, uid, token):
        self.uid = uid
        self.access_token = token
        data = {
            "uid": uid, 
            "access_token": token,
            "user_data": self.user_data,
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
                    self.user_data = data.get("user_data", {})
            except Exception:
                pass

    def has_feature(self, feature_key: str) -> bool:
        """Check if the user has access to a specific feature."""
        return feature_key in self.active_features

    def _sync_features_from_profile(self, profile: dict):
        """Update local feature cache from Firestore profile."""
        if profile:
            # 'features' is a list of lookup_keys written by the Stripe webhook
            raw = profile.get('features', [])
            if isinstance(raw, list):
                self.active_features = raw
            elif isinstance(raw, str):
                # Firestore REST API returns arrays as repeated stringValue
                self.active_features = [raw] if raw else []
            else:
                self.active_features = []
            print(f"[Auth] Active features: {self.active_features}")
        else:
            self.active_features = []

    def get_subscription_status(self):
        """
        Checks 1. Free Trial 2. Firestore Profile
        Returns: 'pro', 'trial', 'expired', 'none'
        Also syncs active_features from Firestore.
        """
        # 1. Check Local Trial (Frictionless)
        trial_status = self._check_local_trial()
        if trial_status['active']:
            return 'trial', trial_status['days_left']

        # 2. Check Firestore if Logged In
        if self.uid and self.access_token:
            profile = self._fetch_firestore_profile()
            
            # Sync features regardless of isPro status
            self._sync_features_from_profile(profile)
            
            if profile and profile.get('isPro'):
                # Check expiry if exists
                expiry = profile.get('expiryDate')
                if expiry:
                    # simplistic check, ideally parsed isoformat
                    if expiry > time.strftime("%Y-%m-%dT%H:%M:%S"):
                        return 'pro', 999
                    else:
                        self.active_features = []  # Expired — revoke features
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
        except Exception:
            return {"active": False, "days_left": 0}

    def _fetch_firestore_profile_sync(self):
        """Synchronous Firestore fetch (runs in thread pool)."""
        if not self.uid: return None
        
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/users/{self.uid}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                doc = r.json()
                fields = doc.get("fields", {})
                
                def unpack(f):
                    if 'booleanValue' in f: return f['booleanValue']
                    if 'stringValue' in f: return f['stringValue']
                    if 'timestampValue' in f: return f['timestampValue']
                    return None
                
                return {k: unpack(v) for k, v in fields.items()}
                
        except Exception as e:
            print(f"[Auth] Firestore check failed: {e}")
            
        return None

    def _fetch_firestore_profile(self):
        """Non-blocking wrapper — runs sync fetch in thread pool."""
        try:
            future = _auth_executor.submit(self._fetch_firestore_profile_sync)
            return future.result(timeout=15)
        except Exception as e:
            print(f"[Auth] Firestore fetch timed out or failed: {e}")
            return None

    def logout(self):
        """Clears the session and resets state."""
        self.uid = None
        self.access_token = None
        self.user_data = {}
        self.active_features = []
        
        if self.token_file.exists():
            try:
                os.remove(self.token_file)
            except Exception as e:
                print(f"[Auth] Failed to delete session: {e}")

    def _fetch_user_details_sync(self):
        """Synchronous user details fetch (runs in thread pool)."""
        if not self.access_token: return
        
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={API_KEY}"
        headers = {"Content-Type": "application/json"}
        data = {"idToken": self.access_token}
        
        try:
            r = requests.post(url, headers=headers, json=data, timeout=10)
            if r.status_code == 200:
                body = r.json()
                users = body.get('users', [])
                if users:
                    user = users[0]
                    self.user_data['photoUrl'] = user.get('photoUrl')
                    self.user_data['displayName'] = user.get('displayName')
                    self.user_data['email'] = user.get('email')
                    self.save_session(self.uid, self.access_token)
            else:
                print(f"[Auth] Failed to fetch user details: {r.status_code} {r.text}")
        except Exception as e:
            print(f"[Auth] Error fetching user details: {e}")

    def fetch_user_details(self):
        """Non-blocking wrapper — runs sync fetch in thread pool."""
        if not self.access_token: return
        _auth_executor.submit(self._fetch_user_details_sync)

    def get_user_info(self):
        """Returns a dict with user info or partial info."""
        status, _ = self.get_subscription_status()
        return {
            "uid": self.uid,
            "logged_in": bool(self.uid and self.access_token),
            "pro": status in ["pro", "trial"],
            "photo_url": self.user_data.get('photoUrl'),
            "display_name": self.user_data.get('displayName'),
            "email": self.user_data.get('email')
        }

