from pathlib import Path
import importlib.util
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("candidate_weread_mp", str(Path(__file__).resolve().parents[1] / "core/wx/model/weread_mp.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
content_spec = importlib.util.spec_from_file_location("candidate_article_content", str(Path(__file__).resolve().parents[1] / "core/article_content.py"))
content_module = importlib.util.module_from_spec(content_spec)
content_spec.loader.exec_module(content_module)


class HardeningTest(unittest.TestCase):
    def collector(self):
        wx = object.__new__(module.MpsWereadMP)
        wx._weread_cookies = "wr_vid=test; wr_skey=test"
        wx._weread_ticket = ""
        wx.user_agent = "test"
        wx.proxy_enabled = False
        wx.http_proxy_url = ""
        return wx

    def response(self, status=200, text='<div id="js_content"><p>Body</p></div>', payload=None):
        response = Mock(status_code=status, text=text)
        response.json.return_value = payload
        return response

    @patch("requests.get")
    def test_auth_error_is_preserved_without_retry(self, get):
        get.return_value = self.response(text='{"errCode":-2012}', payload={"errCode": -2012, "errMsg": "登录超时"})
        with patch("core.wx.model.weread_mobile.refresh_web", return_value=""), self.assertRaises(module.WereadMPAPIError) as error:
            self.collector()._get_mp_content("review")
        self.assertEqual(error.exception.code, -2012)
        self.assertFalse(error.exception.retriable)
        self.assertEqual(get.call_count, 1)

    @patch.object(module.time, "sleep")
    @patch("requests.get")
    def test_transient_server_error_retries_once(self, get, sleep):
        get.side_effect = [self.response(status=503), self.response()]
        self.assertEqual(self.collector()._get_mp_content("review"), "<p>Body</p>")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(3)

    @patch.object(module.time, "sleep")
    @patch("requests.get")
    def test_retry_is_bounded(self, get, sleep):
        get.return_value = self.response(status=503)
        with self.assertRaises(module.WereadMPAPIError):
            self.collector()._get_mp_content("review")
        self.assertEqual(get.call_count, 2)

    @patch("requests.get")
    def test_valid_body_still_parses(self, get):
        get.return_value = self.response()
        self.assertEqual(self.collector()._get_mp_content("review"), "<p>Body</p>")

    @patch("requests.get")
    def test_json_without_body_is_rejected(self, get):
        get.return_value = self.response(text='{}', payload={})
        with self.assertRaises(module.WereadMPAPIError) as error:
            self.collector()._get_mp_content("review")
        self.assertEqual(error.exception.code, "invalid_content")

    @patch("core.article_content.mark_article_fetch_failed")
    @patch("core.db.DB.get_session")
    @patch.object(module.time, "sleep")
    def test_shell_article_does_not_starve_later_body(self, sleep, get_session, mark_failed):
        select, failed, saved = Mock(), Mock(), Mock()
        select.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [("1-1_bad",), ("1-1_good",)]
        saved.query.return_value.filter.return_value.update.return_value = 1
        get_session.side_effect = [select, failed, saved]
        wx = self.collector()
        wx._get_content_interval = Mock(return_value=0)
        wx._get_mp_content = Mock(side_effect=[module.WereadMPAPIError("invalid_content", "shell"), "<p>Body</p>"])
        self.assertEqual(wx._backfill_missing_content("MP_WXS_1"), 1)
        self.assertEqual(wx._get_mp_content.call_count, 2)
        self.assertEqual(len(wx.backfill_errors), 1)
        mark_failed.assert_called_once()
        saved.commit.assert_called_once()

    @patch("core.wx.model.weread_mp.MpsWereadMP")
    @patch.object(content_module, "fetch_article_content")
    @patch.object(content_module.cfg, "get", return_value="weread_mp")
    def test_background_repair_uses_weread_instead_of_web(self, cfg, fetch, collector):
        article = Mock(id="1-1_token", mp_id="MP_WXS_1", content="", url="https://example.test/article", description="existing")
        article.fix_fail_count = 0
        collector.return_value._get_mp_content.return_value = "<p>Recovered body</p>"
        updated, mode = content_module.sync_article_content(Mock(), article)
        self.assertTrue(updated)
        self.assertEqual(mode, "weread_mp")
        self.assertEqual(article.has_content, 1)
        self.assertIsNone(article.fetch_started_at)
        collector.return_value._get_mp_content.assert_called_once_with("MP_WXS_1_token")
        fetch.assert_not_called()


class UpstreamContentCompatibilityTest(unittest.TestCase):
    def test_new_wechat_plain_text_body_is_preserved(self):
        html = "<script>var share = {content_noencode: 'Line one\\x0aLine two'};</script>"
        self.assertEqual(module.extract_mp_content(html), "<p>Line one</p><p>Line two</p>")

if __name__ == "__main__":
    unittest.main()
