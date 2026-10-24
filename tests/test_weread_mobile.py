import importlib.util
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("mobile_candidate", str(Path(__file__).resolve().parents[1] / "core/wx/model/weread_mobile.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
sys.modules["core.wx.model.weread_mobile"] = m
collector_spec = importlib.util.spec_from_file_location("mp_history_candidate", str(Path(__file__).resolve().parents[1] / "core/wx/model/weread_mp.py"))
collector_module = importlib.util.module_from_spec(collector_spec)
collector_spec.loader.exec_module(collector_module)


def row(token, title="Article", timestamp=1790000000):
    return {"reviewId": "MP_WXS_1_" + token, "createTime": timestamp, "mpInfo": {"title": title}}


class MobileTest(unittest.TestCase):
    @patch.object(m.requests, "post")
    def test_login_initializes_mobile_session(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"shelf": {}}
        post.return_value = response
        m.initialize_session({"accessToken": "test-token", "vid": "1", "deviceId": "device"})
        self.assertTrue(post.call_args.args[0].endswith("/mobileSync"))
        self.assertEqual(post.call_args.kwargs["headers"]["accessToken"], "test-token")

    @patch.object(m.requests, "post")
    def test_mobile_initialization_error_is_not_ignored(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"errCode": -2041}
        post.return_value = response
        with self.assertRaises(m.MobileError):
            m.initialize_session({"accessToken": "test-token", "vid": "1", "deviceId": "device"})

    def collector(self):
        wx = object.__new__(collector_module.MpsWereadMP)
        wx.articles = []
        wx._get_feed_update_time = Mock(return_value=0)
        wx._is_article_gathered = Mock(return_value=False)
        wx.update_mps = Mock()
        wx._get_page_interval = Mock(return_value=0)
        return wx

    @patch.object(collector_module.time, "sleep")
    @patch.object(m, "MobileClient")
    def test_two_pages_keep_same_title_and_real_times(self, client, sleep):
        first = m.parse_page({"chapters": [row("a")], "hasMore": True}, "MP_WXS_1", 0, 1)
        second = m.parse_page({"chapters": [row("b")], "hasMore": False}, "MP_WXS_1", 1, 1)
        client.return_value.page.side_effect = [first, second]
        wx = self.collector()
        saved = []
        def save(article):
            saved.append(article)
            return True
        result = wx._collect_mobile_history("MP_WXS_1", "test", save, max_pages=2, page_size=1)
        self.assertEqual(result["collected"], 2)
        self.assertEqual([x["publish_time"] for x in saved], [1790000000, 1790000000])
        self.assertEqual(result["stop_reason"], "end")
        self.assertNotEqual(saved[0]["id"], saved[1]["id"])

    @patch.object(collector_module.time, "sleep")
    @patch.object(m, "MobileClient")
    def test_repeated_page_does_not_loop(self, client, sleep):
        page = m.parse_page({"chapters": [row("a")], "hasMore": True}, "MP_WXS_1", 0, 1)
        client.return_value.page.side_effect = [page, page]
        wx = self.collector()
        with self.assertRaises(m.MobileError) as error:
            wx._collect_mobile_history("MP_WXS_1", "test", Mock(return_value=True), max_pages=3, page_size=1)
        self.assertEqual(error.exception.code, "repeated_page")
        self.assertEqual(client.return_value.page.call_count, 2)

    @patch.object(m, "MobileClient")
    def test_daily_scan_stops_at_fully_known_page(self, client):
        client.return_value.page.return_value = m.parse_page({"chapters": [row("a")], "hasMore": True}, "MP_WXS_1", 0, 1)
        wx = self.collector()
        wx._is_article_gathered.return_value = True
        callback = Mock()
        result = wx._collect_mobile_history("MP_WXS_1", "test", callback)
        self.assertEqual(result["stop_reason"], "known_page")
        callback.assert_not_called()
        self.assertEqual(client.return_value.page.call_count, 1)

    def test_offset_tracks_raw_rows_after_dedup(self):
        page = m.parse_page({"chapters": [row("a"), row("a")], "hasMore": True}, "MP_WXS_1", 20, 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["next_offset"], 22)

    def test_unknown_schema_is_not_empty_success(self):
        with self.assertRaises(m.MobileError):
            m.parse_page({"unexpected": []}, "MP_WXS_1", 0, 5)

    def test_missing_timestamp_rejected_instead_of_using_now(self):
        with self.assertRaises(m.MobileError):
            m.parse_page({"chapters": [row("a", timestamp=None)]}, "MP_WXS_1", 0, 5)

    def test_foreign_feed_rejected(self):
        with self.assertRaises(m.MobileError):
            m.parse_page({"chapters": [row("a")]}, "MP_WXS_2", 0, 5)

    def test_empty_page_with_more_is_invalid_cursor(self):
        with self.assertRaises(m.MobileError):
            m.parse_page({"chapters": [], "hasMore": True}, "MP_WXS_1", 0, 5)

    def test_end_page_is_explicit(self):
        page = m.parse_page({"chapters": [row("a")], "hasMore": False}, "MP_WXS_1", 0, 5)
        self.assertFalse(page["has_more"])

    def test_http200_auth_error_preserved(self):
        response = Mock(status_code=200)
        response.json.return_value = {"errcode": -2012, "errmsg": "expired"}
        with self.assertRaises(m.MobileError) as error:
            m.response_json(response)
        self.assertEqual(error.exception.code, -2012)

    def test_no_mobile_auth_does_not_send_request(self):
        with patch.object(m, "load_state", return_value={}), patch.object(m.requests, "get") as get:
            with self.assertRaises(m.MobileError):
                m.MobileClient().page("MP_WXS_1")
            get.assert_not_called()

    def test_credentials_separate_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "STATE", Path(directory) / "mobile.json"):
            m.save_state({"accessToken": "test", "validated_feeds": []})
            self.assertEqual(m.load_state()["accessToken"], "test")
            self.assertEqual(list(Path(directory).glob(".weread-mobile-*")), [])

    def test_authorized_new_feed_needs_no_allowlist(self):
        with patch.object(m, "load_state", return_value={"accessToken": "test", "vid": "1", "validated_feeds": []}):
            self.assertTrue(m.MobileClient().enabled("MP_WXS_999900001"))

    def test_feed_cooldown_does_not_block_other_feeds(self):
        with patch.object(m, "load_state", return_value={"accessToken": "test", "vid": "1", "feed_errors": {"MP_WXS_1": {"cooldown_until": 10**12}}}):
            self.assertFalse(m.MobileClient().enabled("MP_WXS_1"))
            self.assertTrue(m.MobileClient().enabled("MP_WXS_2"))

    def test_featured_feed_is_not_a_mobile_subscription(self):
        with patch.object(m, "load_state", return_value={"accessToken": "test", "vid": "1"}):
            self.assertFalse(m.MobileClient().enabled("MP_WXS_FEATURED_ARTICLES"))

    def test_feed_respects_global_auth_cooldown(self):
        with patch.object(m, "load_state", return_value={"accessToken": "test", "vid": "1", "cooldown_until": 10**12}):
            self.assertFalse(m.MobileClient().enabled("MP_WXS_1"))


if __name__ == "__main__":
    unittest.main()
