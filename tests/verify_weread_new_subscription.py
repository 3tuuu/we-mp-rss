"""Run standalone: python tests/verify_weread_new_subscription.py.
Uses a new temporary database and fixture credentials, with no production data.
"""
import asyncio
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import Mock, patch


with tempfile.TemporaryDirectory(prefix="werss-auto-add-") as directory:
    clone = Path(directory) / "test.db"
    os.environ["DB"] = "sqlite:///" + str(clone)
    os.environ["GATHER.MODEL"] = "weread_mp"
    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        from core.config import cfg
        assert cfg.get("db") == "sqlite:///" + str(clone), "DB override was not applied"
        from apis.mps import add_mp
        from core.db import DB
        from core.models.article import Article
        from core.wx.model.weread_mp import MpsWereadMP
        from core.wx.model.weread_mobile import MobileClient, load_state

        from core.models.base import Base
        Base.metadata.create_all(DB.get_engine())
        fixture_auth = patch("core.wx.model.weread_mobile.load_state", return_value={
            "accessToken": "fixture-token", "vid": "1", "authorized_at": int(time.time())})
        fixture_auth.start()
        def fixture_web_auth(self):
            self._weread_cookies = "wr_vid=1; wr_skey=fixture"
            self._weread_ticket = ""
            self._weread_vid = "1"
        book_id = "MP_WXS_999900001"
        assert MobileClient().state.get("validated_feeds", []) == []
        assert MobileClient().enabled(book_id), "new feeds must not require manual validation"
        session = DB.get_session()
        assert str(session.bind.url.database) == str(clone), "unsafe test database"
        session.close()

        def item(token, timestamp):
            return {"reviewId": book_id + "_" + token, "title": "Fixture " + token,
                    "time": timestamp, "pic": "", "digest": ""}

        pages = [
            {"items": [item("a", 1790000003), item("b", 1790000002)],
             "offset": 0, "next_offset": 2, "has_more": True},
            {"items": [item("c", 1790000001)],
             "offset": 2, "next_offset": 3, "has_more": False},
        ]
        with patch("core.queue.TaskQueue.add_task") as enqueue, \
             patch("apis.mps.save_avatar_locally", return_value=""), \
             patch.object(MobileClient, "page", side_effect=pages) as page, \
             patch.object(MpsWereadMP, "_load_weread_auth", fixture_web_auth), \
             patch.object(MpsWereadMP, "ensure_mp_on_shelf", return_value=(True, "fixture")), \
             patch.object(MpsWereadMP, "_get_mp_cover", side_effect=AssertionError("fell back to single article")), \
             patch.object(MpsWereadMP, "_backfill_missing_content", return_value=0), \
             patch("time.sleep"):
            result = asyncio.run(add_mp(mp_name="Auto-add integration fixture", mp_id=base64.b64encode(b"999900001").decode(),
                                        mp_cover=None, avatar="", mp_intro="test fixture", current_user={}))
            assert result["code"] == 0, result
            enqueue.assert_called_once()
            task = enqueue.call_args.args[0]
            kwargs = dict(enqueue.call_args.kwargs)
            kwargs.pop("task_name", None)
            task(**kwargs)
            assert page.call_count == 2, "first-add task did not paginate"

        session = DB.get_session()
        rows = session.query(Article).filter(Article.mp_id == book_id).all()
        assert len(rows) == 3, "articles were not saved by the real callback"
        assert sorted(row.publish_time for row in rows) == [1790000001, 1790000002, 1790000003]
        session.close()
    fixture_auth.stop()
    print(json.dumps({"test": "real add_mp -> queued collector -> two pages -> database",
                      "ok": True, "saved_in_clone": 3, "independent_database": True}))
