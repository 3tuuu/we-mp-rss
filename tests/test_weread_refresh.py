"""Credential rotation regressions. All credentials and state are isolated fixtures."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_weread_mobile import m, collector_module


def response(payload, status=200):
    item = Mock(status_code=status)
    item.json.return_value = payload
    item.cookies = []
    return item


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(m, "STATE", Path(self.directory.name) / "state.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.state = {"accessToken": "old", "refreshToken": "refresh-old", "vid": "1", "deviceId": "device"}
        m.save_state(self.state)

    @patch.object(m, "initialize_session")
    @patch.object(m.requests, "post")
    def test_rotation_persists_refresh_token_and_identity(self, post, sync):
        post.return_value = response({"accessToken": "new", "refreshToken": "refresh-new"})
        state = m.refresh_mobile()
        self.assertEqual(state["refreshToken"], "refresh-new")
        self.assertEqual(m.load_state()["accessToken"], "new")
        self.assertEqual(state["deviceId"], "device")
        sync.assert_called_once()
        m.refresh_mobile()
        self.assertEqual(post.call_count, 1)

    @patch.object(m, "initialize_session")
    @patch.object(m.requests, "post")
    def test_sync_outage_does_not_lose_rotated_token(self, post, sync):
        post.return_value = response({"accessToken": "new", "refreshToken": "refresh-new"})
        sync.side_effect = m.MobileError("network_error", "failed")
        with self.assertRaises(m.MobileError):
            m.refresh_mobile()
        self.assertEqual(m.load_state()["refreshToken"], "refresh-new")
        self.assertTrue(m.load_state()["needs_init"])
        sync.side_effect = None
        state = m.load_state()
        state["refresh_retry_at"] = 0
        m.save_state(state)
        self.assertFalse(m.refresh_mobile()["needs_init"])
        self.assertEqual(post.call_count, 1)

    @patch.object(m.requests, "post")
    def test_expired_refresh_requires_scan_with_backoff(self, post):
        post.return_value = response({"errCode": -2013})
        with self.assertRaises(m.MobileError):
            m.refresh_mobile()
        state = m.load_state()
        self.assertTrue(state["needs_reauth"])
        self.assertGreater(state["refresh_retry_at"], time.time())
        m.refresh_mobile()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(state["accessToken"], "old")

    @patch.object(m.requests, "post")
    def test_verification_failure_is_not_treated_as_expiry(self, post):
        post.return_value = response({"errCode": -2041}, 499)
        with self.assertRaises(m.MobileError):
            m.refresh_mobile()
        self.assertFalse(m.load_state()["needs_reauth"])

    @patch.object(m.requests, "post")
    def test_concurrent_client_reuses_already_rotated_token(self, post):
        state = {**self.state, "accessToken": "new"}
        m.save_state(state)
        self.assertEqual(m.refresh_mobile(failed_token="old")["accessToken"], "new")
        post.assert_not_called()

    @patch.object(m, "refresh_mobile")
    def test_auth_retry_is_bounded(self, refresh):
        refresh.return_value = self.state
        client = m.MobileClient()
        client._page_once = Mock(side_effect=m.MobileError(-2012, "expired"))
        with self.assertRaises(m.MobileError):
            client.page("MP_WXS_1")
        self.assertEqual(client._page_once.call_count, 2)
        self.assertTrue(m.load_state()["needs_reauth"])

    @patch.object(m, "refresh_mobile")
    def test_rate_limit_does_not_trigger_auth_refresh(self, refresh):
        refresh.return_value = self.state
        client = m.MobileClient()
        client._page_once = Mock(side_effect=m.MobileError(-2041, "limited"))
        with self.assertRaises(m.MobileError):
            client.page("MP_WXS_1")
        self.assertEqual(refresh.call_count, 1)  # Scheduled check only.
        self.assertEqual(client._page_once.call_count, 1)

    def web_config(self):
        lic = Mock()
        lic.get.return_value = {"cookie": "wr_vid=1; wr_skey=old; wr_rt=refresh"}
        cfg = Mock()
        cfg.get.return_value = ""
        return lic, cfg

    @patch.object(m.requests, "Session")
    @patch.object(m.requests, "get")
    def test_web_only_saves_verified_new_response_cookie(self, get, session):
        lic, cfg = self.web_config()
        get.return_value = response({"books": []})
        renewed = response({})
        renewed.cookies = [Mock(name="cookie")]
        renewed.cookies[0].name = "wr_skey"
        renewed.cookies[0].value = "new"
        session.return_value.__enter__.return_value.post.return_value = renewed
        with patch("core.config.Config", return_value=lic), patch("core.config.cfg", cfg):
            self.assertIn("wr_skey=new", m.refresh_web())
        lic.save_config.assert_called_once()
        self.assertTrue(m.load_state()["web_auth"]["valid"])

    @patch.object(m.requests, "Session")
    @patch.object(m.requests, "get")
    def test_existing_cookie_jar_is_not_fake_renewal_success(self, get, session):
        lic, cfg = self.web_config()
        get.return_value = response({"books": []})
        session.return_value.__enter__.return_value.post.return_value = response({})
        with patch("core.config.Config", return_value=lic), patch("core.config.cfg", cfg):
            self.assertIn("wr_skey=old", m.refresh_web())
        lic.save_config.assert_not_called()
        report = m.load_state()["web_auth"]
        self.assertFalse(report["needs_reauth"])
        self.assertEqual(report["error"]["code"], "web_no_new_cookie")

    @patch.object(m.requests, "get")
    @patch.object(m.requests, "Session")
    def test_web_expired_renewal_marks_reauth(self, session, get):
        lic, cfg = self.web_config()
        session.return_value.__enter__.return_value.post.return_value = response({"errCode": -2013})
        with patch("core.config.Config", return_value=lic), patch("core.config.cfg", cfg):
            m.refresh_web(failed_cookie="wr_vid=1; wr_skey=old; wr_rt=refresh")
        self.assertTrue(m.load_state()["web_auth"]["needs_reauth"])
        lic.save_config.assert_not_called()
        get.assert_not_called()

    def test_body_auth_retries_with_renewed_web_cookie_once(self):
        wx = object.__new__(collector_module.MpsWereadMP)
        wx._weread_cookies = "old"
        wx._get_mp_content_once = Mock(side_effect=[collector_module.WereadMPAPIError(-2012, "expired"), "body"])
        with patch.object(m, "refresh_web", return_value="new"):
            self.assertEqual(wx._get_mp_content("review"), "body")
        self.assertEqual(wx._weread_cookies, "new")
        self.assertEqual(wx._get_mp_content_once.call_count, 2)

    @patch.object(m.requests, "Session")
    @patch.object(m.requests, "get")
    def test_web_failed_renewal_retries_after_backoff_before_twelve_hours(self, get, session):
        lic, cfg = self.web_config()
        state = m.load_state()
        state["web_auth"] = {"checked_at": int(time.time()) - 1900,
                             "retry_at": int(time.time()) - 100, "error": {"code": "network_error"}}
        m.save_state(state)
        get.return_value = response({"books": []})
        session.return_value.__enter__.return_value.post.return_value = response({})
        with patch("core.config.Config", return_value=lic), patch("core.config.cfg", cfg):
            m.refresh_web()
        session.return_value.__enter__.return_value.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
