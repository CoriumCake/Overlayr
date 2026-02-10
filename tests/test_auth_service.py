"""Tests for AuthService — session management, trial logic, and deep linking."""
import pytest
import json
import time
import os
from unittest.mock import patch, MagicMock
from pathlib import Path
from overlayr.services.auth_service import AuthService


@pytest.fixture
def auth(tmp_path):
    """AuthService with temporary app data directory."""
    with patch.object(AuthService, 'register_protocol'):
        service = AuthService()
        # Override BEFORE any session load to avoid leaking real data
        service.app_data_dir = tmp_path
        service.token_file = tmp_path / "auth_session.json"
        service.uid = None
        service.access_token = None
        service.user_data = {}
        return service


class TestSessionManagement:
    def test_save_and_load_session(self, auth):
        auth.save_session("uid123", "token456")
        
        # Create a fresh instance to test loading
        with patch.object(AuthService, 'register_protocol'):
            auth2 = AuthService()
            auth2.app_data_dir = auth.app_data_dir
            auth2.token_file = auth.token_file
            auth2.load_session()
            
            assert auth2.uid == "uid123"
            assert auth2.access_token == "token456"

    def test_logout_clears_state(self, auth):
        auth.save_session("uid123", "token456")
        auth.logout()
        
        assert auth.uid is None
        assert auth.access_token is None
        assert auth.user_data == {}

    def test_logout_removes_token_file(self, auth):
        auth.save_session("uid123", "token456")
        assert auth.token_file.exists()
        auth.logout()
        assert not auth.token_file.exists()


class TestDeepLink:
    def test_valid_auth_link(self, auth):
        result = auth.handle_deep_link("overlayr://auth?token=tok123&uid=user1")
        assert result is True
        assert auth.uid == "user1"
        assert auth.access_token == "tok123"

    def test_invalid_scheme(self, auth):
        result = auth.handle_deep_link("overlayr://unknown?token=tok123")
        assert result is False

    def test_missing_params(self, auth):
        result = auth.handle_deep_link("overlayr://auth?token=tok123")
        assert result is False


class TestLocalTrial:
    def test_first_run_gives_7_days(self, auth):
        trial = auth._check_local_trial()
        assert trial["active"] is True
        assert trial["days_left"] == 7

    def test_expired_trial(self, auth):
        trial_file = auth.app_data_dir / "trial_config.json"
        with open(trial_file, "w") as f:
            json.dump({"first_run": time.time() - (8 * 86400)}, f)
        
        trial = auth._check_local_trial()
        assert trial["active"] is False
        assert trial["days_left"] == 0

    def test_mid_trial(self, auth):
        trial_file = auth.app_data_dir / "trial_config.json"
        with open(trial_file, "w") as f:
            json.dump({"first_run": time.time() - (3 * 86400)}, f)
        
        trial = auth._check_local_trial()
        assert trial["active"] is True
        assert trial["days_left"] >= 3  # int() truncation means 3 or 4


class TestUserInfo:
    def test_not_logged_in(self, auth):
        info = auth.get_user_info()
        # Even without login, trial may be active, so check logged_in only
        assert info["uid"] is None

    def test_logged_in_trial(self, auth):
        auth.uid = "test_uid"
        auth.access_token = "test_token"
        info = auth.get_user_info()
        assert info["logged_in"] is True
        # During trial period, pro should be True
        assert info["pro"] is True
