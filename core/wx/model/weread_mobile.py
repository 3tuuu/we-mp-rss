"""Optional WeRead mobile list adapter; Web credentials remain separate.

Protocol reference: x554960766/wechat-mp-tools backend/auth.py and articles.py.
Authentication can renew once; rate limits and verification requests stop the operation.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid
import tempfile
from contextlib import contextmanager

import requests

BASE = "https://i.weread.qq.com"
STATE = Path("data/weread_mobile.json")
LOCK = threading.RLock()
APP_HEADERS = {"User-Agent": "WeRead/7.5.0 (Android; 33; arm64-v8a)", "Content-Type": "application/json; charset=UTF-8"}
AUTH_CODES = {"401", "-2012", "-2013", "-2010"}
REFRESH_INTERVAL = 12 * 3600
REFRESH_BACKOFF = 1800


@contextmanager
def auth_lock():
    """Serialize credential rotation across threads and worker processes."""
    with LOCK:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE.parent / ".weread-auth.lock", "a+b") as stream:
            os.chmod(stream.name, 0o600)
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


# Signature primitive adapted from x554960766/wechat-mp-tools (MIT).
# Only the pure algorithm is retained; upstream token logging is not used.
SBOX = [
52,202,85,64,29,182,147,198,49,48,41,53,50,167,184,17,
194,181,22,250,139,177,36,164,16,144,4,233,8,248,59,138,
156,140,68,249,188,92,105,226,161,218,210,211,117,137,247,30,
45,80,86,215,114,83,191,34,251,32,15,1,46,69,135,110,
102,72,242,224,205,254,103,169,67,244,148,81,206,165,74,238,
19,38,142,204,170,51,20,93,14,57,187,207,145,43,129,77,
234,153,236,26,44,133,197,217,54,116,75,24,225,241,61,157,
65,159,180,23,13,214,76,190,220,175,151,40,119,240,98,255,
113,193,200,39,143,108,104,168,155,230,89,28,27,18,9,152,
78,63,6,55,0,186,31,10,25,47,201,213,208,87,73,111,
253,37,228,97,12,66,203,150,100,95,219,173,96,35,141,154,
109,195,196,94,62,185,146,106,189,91,7,127,118,149,237,79,
171,132,122,128,231,120,199,229,235,115,131,107,252,56,70,125,
71,101,179,82,99,58,5,209,239,163,166,222,158,60,2,174,
178,123,160,246,243,42,192,172,134,3,90,84,11,245,130,212,
126,227,223,176,216,221,33,232,124,136,162,121,88,112,183,21]


def gen_signature(values):
    value = b"".join(sorted([bytes(SBOX[b] for b in str(v).encode()) for v in values] + [b"5a6f1"]))
    for _ in range(2):
        xor = 0
        for byte in value:
            xor ^= byte
        shift = xor % 11
        rotated = bytearray(len(value))
        for index, byte in enumerate(value):
            rotated[(index + shift) % len(value)] = byte
        value = hashlib.sha256(rotated).hexdigest().encode("ascii")
    return value.decode("ascii")


def refresh_mobile(failed_token=None):
    """Renew on schedule or once after a definite auth error; never loop."""
    with auth_lock():
        state = load_state()
        now = int(time.time())
        if not state.get("accessToken"):
            return state
        if failed_token and state["accessToken"] != failed_token:
            return state  # Another worker already renewed this request's token.
        if failed_token and now - state.get("refresh_attempt_at", 0) < 60:
            return state
        if state.get("needs_init") and now >= state.get("refresh_retry_at", 0):
            try:
                initialize_session(state)
                state.update(needs_init=False, initialized_at=now, refresh_error=None, refresh_retry_at=0)
                save_state(state)
            except (MobileError, requests.RequestException) as exc:
                state.update(refresh_retry_at=now + REFRESH_BACKOFF,
                             refresh_error={"code": "sync_failed", "at": now, "message": "续期后同步失败，稍后自动重试"})
                save_state(state)
                raise MobileError("sync_failed", "续期后同步失败") from exc
        if not failed_token and now - state.get("refreshed_at", state.get("authorized_at", 0)) < REFRESH_INTERVAL:
            return state
        if now < state.get("refresh_retry_at", 0):
            return state
        state["refresh_attempt_at"] = now
        try:
            if not state.get("refreshToken") or not state.get("deviceId"):
                raise MobileError("missing_refresh_token", "缺少移动端续期信息，请重新扫码")
            rand = secrets.randbelow(1000)
            stamp = int(time.time() * 1000)
            payload = {"refreshToken": state["refreshToken"], "deviceId": state["deviceId"],
                       "wxToken": 0, "inBackground": 0, "trackId": "", "kickType": 1,
                       "refCgi": "", "timestamp": stamp, "random": rand,
                       "signature": gen_signature(["", state["deviceId"], stamp, rand, state["refreshToken"]]),
                       "virtualChannelId": "", "deviceName": "Android"}
            data = response_json(requests.post(BASE + "/login", json=payload, headers=APP_HEADERS, timeout=(10, 20)))
            if not data.get("accessToken") or (data.get("vid") and str(data["vid"]) != str(state["vid"])):
                raise MobileError("invalid_refresh_response", "续期没有返回同一账号的有效会话")
            state["accessToken"] = data["accessToken"]
            state["skey"] = data.get("skey") or data["accessToken"]
            if data.get("refreshToken"):
                state["refreshToken"] = data["refreshToken"]
            # Persist rotated credentials BEFORE sync: a sync outage must not lose a rotated refreshToken.
            state.update(refreshed_at=now, refresh_error=None, refresh_retry_at=0, needs_reauth=False, needs_init=True)
            save_state(state)
            initialize_session(state)
            state.update(initialized_at=now, needs_init=False)
            save_state(state)
            return state
        except (MobileError, requests.RequestException) as exc:
            code = str(exc.code) if isinstance(exc, MobileError) else "network_error"
            state.update(refresh_retry_at=now + REFRESH_BACKOFF,
                         refresh_error={"code": code, "at": now, "message": "移动端自动续期失败；30 分钟后重试"},
                         needs_reauth=code in AUTH_CODES or code == "missing_refresh_token")
            save_state(state)
            raise MobileError(code, "移动端自动续期失败，请查看授权状态") from exc


class MobileError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def load_state():
    with LOCK:
        if not STATE.exists():
            return {}
        return json.loads(STATE.read_text())


def save_state(data):
    with LOCK:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".weread-mobile-", dir=STATE.parent)
        temporary = Path(temporary_name)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.chmod(temporary, 0o600)
        temporary.replace(STATE)


def response_json(response):
    try:
        data = response.json()
    except ValueError as exc:
        raise MobileError("invalid_json", "移动端接口没有返回 JSON") from exc
    if not isinstance(data, dict):
        raise MobileError("invalid_response", "移动端接口返回结构异常")
    code = data.get("errCode", data.get("errcode", 0))
    if code not in (None, 0, "0") or response.status_code != 200:
        if response.status_code == 401 or str(code) in ("-2012", "-2013", "-2010"):
            message = "移动端授权无效或已过期，请重新扫码；Web 正文授权不受影响"
        elif response.status_code in (403, 429, 499) or str(code) in ("-2041", "-2014"):
            message = "历史接口受限或要求验证，本次停止；请在微信读书按平台提示处理"
        else:
            message = "移动端接口请求失败"
        raise MobileError(code or response.status_code, message)
    return data


def parse_page(payload, book_id, offset, count):
    raw = None
    for key in ("data", "chapters", "list", "reviews"):
        if key in payload:
            raw = payload[key]
            break
    if not isinstance(raw, list):
        raise MobileError("unsupported_schema", "未识别的历史列表结构，不能视为空列表成功")
    items, seen = [], set()
    entries = []
    for row in raw:
        if not isinstance(row, dict):
            raise MobileError("unsupported_schema", "历史列表条目不是对象")
        children = row.get("subReviews")
        if children is not None:
            if not isinstance(children, list):
                raise MobileError("unsupported_schema", "历史文章组结构异常")
            entries.extend(children)
        else:
            entries.append(row)
    for entry in entries:
        if not isinstance(entry, dict):
            raise MobileError("unsupported_schema", "历史文章结构异常")
        row = entry.get("review") or entry
        mp = row.get("mpInfo") or {}
        rid = row.get("reviewId") or entry.get("reviewId") or ""
        title = mp.get("title") or row.get("title") or ""
        timestamp = row.get("createTime") or mp.get("time") or row.get("update_time")
        try:
            timestamp = int(timestamp)
            if timestamp > 10**12:
                timestamp //= 1000
        except (TypeError, ValueError):
            timestamp = 0
        if not rid.startswith(book_id + "_") or not title or timestamp <= 0:
            raise MobileError("unsupported_article", "历史文章缺少有效标识、标题或发布时间，停止导入")
        if rid in seen:
            continue
        seen.add(rid)
        items.append({"reviewId": rid, "title": title, "time": timestamp,
                      "pic": mp.get("pic_url") or mp.get("thumbUrl") or row.get("cover") or "",
                      "digest": mp.get("digest") or ""})
    flag = payload.get("hasMore")
    has_more = str(flag).lower() in ("1", "true") if flag is not None else len(raw) >= count
    next_offset = payload.get("nextOffset", offset + len(raw))
    if not isinstance(next_offset, int) or (has_more and next_offset <= offset):
        raise MobileError("invalid_cursor", "历史接口未提供可前进的分页位置")
    return {"items": items, "offset": offset, "next_offset": next_offset, "has_more": has_more}


class MobileClient:
    def __init__(self):
        self.state = load_state()

    def enabled(self, book_id):
        # Existing WeRSS add/update jobs use this collector automatically.
        # A shared mobile login applies to every normal MP feed; no allowlist.
        normal_feed = book_id.startswith("MP_WXS_") and book_id[len("MP_WXS_"):].isdigit()
        error = self.state.get("feed_errors", {}).get(book_id, {})
        cooldown = error.get("cooldown_until", self.state.get("cooldown_until", 0))
        return bool(normal_feed and self.state.get("accessToken") and self.state.get("vid")
                    and cooldown < time.time())

    def page(self, book_id, offset=0, count=20):
        try:
            self.state = refresh_mobile()
        except MobileError:
            self.state = load_state()  # A scheduled renewal outage need not invalidate a working token.
        try:
            return self._page_once(book_id, offset, count)
        except MobileError as exc:
            if str(exc.code) not in AUTH_CODES:
                raise
            self.state = refresh_mobile(failed_token=self.state.get("accessToken"))
            try:
                return self._page_once(book_id, offset, count)
            except MobileError as retry_error:
                if str(retry_error.code) in AUTH_CODES:
                    with auth_lock():
                        state = load_state()
                        state["needs_reauth"] = True
                        save_state(state)
                raise

    def _page_once(self, book_id, offset=0, count=20):
        if not book_id.startswith("MP_WXS_") or not 1 <= count <= 20 or offset < 0:
            raise MobileError("invalid_request", "公众号或分页参数无效")
        token = self.state.get("accessToken")
        if not token or not self.state.get("vid"):
            raise MobileError("missing_mobile_auth", "请先完成独立的移动端扫码授权")
        headers = {**APP_HEADERS, "vid": self.state["vid"], "accessToken": token,
                   "skey": self.state.get("skey") or token,
                   "Cookie": f"wr_vid={self.state['vid']}; wr_skey={self.state.get('skey') or token}"}
        try:
            response = requests.get(BASE + "/mp/chapters", params={
                "bookId": book_id, "count": count, "offset": offset, "pf": "android", "synckey": 0,
            }, headers=headers, timeout=(10, 20))
            return parse_page(response_json(response), book_id, offset, count)
        except requests.RequestException as exc:
            raise MobileError("network_error", "历史接口网络请求失败，本次停止") from exc


def initialize_session(state):
    """Perform the normal post-login mobile sync before requesting MP lists."""
    token = state["accessToken"]
    headers = {**APP_HEADERS, "vid": str(state["vid"]), "accessToken": token,
               "skey": state.get("skey") or token, "deviceId": state["deviceId"],
               "Cookie": f"wr_vid={state['vid']}; wr_skey={state.get('skey') or token}"}
    fields = ("shelf bookInventory shelfLecture notifications config gift payItem reviewTimeline "
              "follower readingExchange chat chatRemovedSynckey wechatFriend inBackground "
              "refluxSynckey following applyList mcard discoverFeedSynckey friendReviewSynckey "
              "wehearSyncKey rateSynckey browse medal medalUrl regionResource discoverColumnSynckey")
    payload = {key: 0 for key in fields.split()}
    payload.update(balance=0.0, preferTab=1, localBrowseTab="", localCommunityTab="")
    try:
        data = response_json(requests.post(BASE + "/mobileSync", json=payload,
                                           headers=headers, timeout=(10, 20)))
    except requests.RequestException as exc:
        raise MobileError("sync_network_error", "移动端登录后同步失败，请稍后重试") from exc
    if "shelf" not in data:
        raise MobileError("sync_invalid_response", "移动端登录后同步未返回预期数据")


class MobileLogin:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"
        self.message = "尚未开始移动端授权"
        self.image = ""
        self.generation = 0

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "message": self.message, "qrcode": self.image}

    def start(self):
        # One pending login at a time. Do not overwrite the working Web cookies.
        with self.lock:
            if self.status in ("starting", "waiting", "scanned"):
                return self.snapshot_unlocked()
            self.generation += 1
            generation = self.generation
            self.status, self.message = "starting", "正在申请移动端二维码"
        try:
            session = requests.Session()
            ticket = response_json(session.get(BASE + "/wxticket", params={"nonceStr": "weread"}, headers=APP_HEADERS, timeout=15))
            qr = response_json(session.get("https://open.weixin.qq.com/connect/sdk/qrconnect", params={
                "appid": "wxab9b71ad2b90ff34", "noncestr": "weread", "timestamp": ticket["timeStamp"],
                "scope": "snsapi_userinfo,snsapi_timeline,snsapi_friend,snsapi_favorites", "signature": ticket["signature"],
            }, headers=APP_HEADERS, timeout=15))
            qr_uuid = qr.get("uuid")
            if not qr_uuid:
                raise MobileError("missing_qr", "上游未返回移动端登录二维码")
            image = (qr.get("qrcode") or {}).get("qrcodebase64")
            with self.lock:
                self.status, self.message = "waiting", "请用微信扫码并确认微信读书移动端授权"
                self.image = "data:image/jpeg;base64," + image if image else "https://open.weixin.qq.com/connect/qrcode/" + qr_uuid
            threading.Thread(target=self._poll, args=(session, qr_uuid, generation), daemon=True).start()
            return self.snapshot()
        except Exception:
            with self.lock:
                self.status, self.message = "failed", "移动端二维码申请失败，请稍后重试"
            raise MobileError("qr_failed", self.message)

    def snapshot_unlocked(self):
        return {"status": self.status, "message": self.message, "qrcode": self.image}

    def _poll(self, session, qr_uuid, generation):
        started = time.monotonic()
        try:
            while time.monotonic() - started < 300:
                with self.lock:
                    if generation != self.generation:
                        return
                try:
                    result = session.get("https://long.open.weixin.qq.com/connect/l/qrconnect", params={"f": "json", "uuid": qr_uuid}, timeout=28).json()
                except (requests.RequestException, ValueError):
                    time.sleep(2)
                    continue
                code = result.get("wx_errcode", result.get("errcode"))
                if code == 405:
                    state = load_state()
                    device = state.get("deviceId") or uuid.uuid4().hex
                    now, rand = int(time.time() * 1000), secrets.randbelow(1000)
                    payload = {"code": result["wx_code"], "deviceId": device, "timestamp": now, "random": rand,
                               "signature": hashlib.sha256(f"{now}{device}{rand}".encode()).hexdigest(),
                               "wxToken": 1, "appFirstInstall": 0, "installId": state.get("installId") or str(uuid.uuid4()),
                               "isFromQrcode": 1, "isAutoLogout": 0, "deviceName": "Android", "deviceType": 1,
                               "trackId": "", "virtualChannelId": ""}
                    data = response_json(session.post(BASE + "/login", json=payload, headers=APP_HEADERS, timeout=20))
                    if not data.get("vid") or not data.get("accessToken"):
                        raise MobileError("missing_token", "移动端登录未返回完整会话")
                    state.update({k: data.get(k, "") for k in ("vid", "accessToken", "refreshToken", "skey")})
                    state.update(vid=str(data["vid"]), deviceId=device, installId=payload["installId"],
                                 validated_feeds=[], feed_errors={}, cooldown_until=0, authorized_at=int(time.time()))
                    initialize_session(state)
                    state.update(initialized_at=int(time.time()), refreshed_at=int(time.time()),
                                 needs_reauth=False, refresh_error=None, refresh_retry_at=0, needs_init=False)
                    with auth_lock():
                        latest = load_state()
                        latest.update(state)
                        save_state(latest)
                    with self.lock:
                        self.status, self.message, self.image = "success", "移动端授权已保存；新增公众号将自动分页采集", ""
                    return
                if code in (402, 403):
                    break
                if code == 404:
                    with self.lock:
                        self.status, self.message = "scanned", "已扫码，请在微信确认授权"
                time.sleep(2)
            with self.lock:
                self.status, self.message, self.image = "expired", "二维码已过期，请重新申请", ""
        except Exception:
            with self.lock:
                self.status, self.message, self.image = "failed", "移动端登录失败，请重试；没有改动 Web 授权", ""


def refresh_web(failed_cookie=None):
    """Validate Web login independently, renew via wr_rt and save only verified cookies."""
    from core.config import Config, cfg
    with auth_lock():
        state = load_state()
        report = state.get("web_auth", {})
        now = int(time.time())
        lic = Config("./data/wx.lic")
        data = lic.get("weread_data", {})
        if isinstance(data, str):
            data = json.loads(data)
        configured = cfg.get("weread.cookie", "")
        target = data
        accounts = data.get("accounts")
        if not configured and isinstance(accounts, list) and accounts:
            target = accounts[0]
            if failed_cookie:
                def cookie_vid(value):
                    return dict(part.strip().split("=", 1) for part in value.split(";") if "=" in part).get("wr_vid")
                vid = cookie_vid(failed_cookie)
                target = next((account for account in accounts if cookie_vid(account.get("cookie", "")) == vid), None)
                if target is None:
                    return failed_cookie
        cookie = configured or target.get("cookie", "")
        if failed_cookie and cookie != failed_cookie:
            return cookie
        if now < report.get("retry_at", 0):
            return cookie
        if not failed_cookie and not report.get("error") and now - report.get("checked_at", 0) < REFRESH_INTERVAL:
            return cookie
        report["checked_at"] = now
        headers = {"User-Agent": "Mozilla/5.0", "Origin": "https://weread.qq.com",
                   "Referer": "https://weread.qq.com/"}

        def verify(value):
            response = requests.get("https://weread.qq.com/web/shelf/sync",
                                    params={"userVid": "", "synckey": 0},
                                    headers={**headers, "Cookie": value}, timeout=(10, 20))
            result = response_json(response)
            if not any(key in result for key in ("books", "bookCount", "synckey")):
                raise MobileError("web_invalid_response", "Web 授权验证返回异常")

        valid = False
        try:
            if not cookie:
                raise MobileError("missing_web_auth", "请先扫码授权 Web 正文")
            if not failed_cookie:
                try:
                    verify(cookie)
                    valid = True
                except MobileError as exc:
                    if str(exc.code) not in AUTH_CODES:
                        raise
            cookies = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
            if not cookies.get("wr_rt"):
                raise MobileError("missing_web_refresh", "Web Cookie 缺少续期信息")
            with requests.Session() as session:
                session.headers.update(headers)
                for key, value in cookies.items():
                    session.cookies.set(key, value, domain="weread.qq.com", path="/")
                response = session.post("https://weread.qq.com/web/login/renewal",
                                        json={"rq": "%2Fweb%2Fbook%2Fread", "ql": False}, timeout=(10, 20))
                result = response_json(response)
                # Only response cookies count; the pre-existing session jar is not evidence of renewal.
                updates = {c.name: c.value for c in response.cookies if c.value}
                skey = updates.get("wr_skey") or result.get("wr_skey") or result.get("skey")
                if not skey:
                    raise MobileError("web_no_new_cookie", "Web 续期未返回新 Cookie")
                cookies.update(updates, wr_skey=skey)
            renewed = "; ".join(f"{key}={value}" for key, value in cookies.items())
            verify(renewed)
            if configured:
                cfg.set("weread.cookie", renewed)
                cfg.save_config()
            else:
                target["cookie"] = renewed
                lic.set("weread_data", data)
                lic.save_config()
            report.update(valid=True, needs_reauth=False, refreshed_at=now, error=None, retry_at=0)
            state["web_auth"] = report
            save_state(state)
            return renewed
        except (MobileError, requests.RequestException) as exc:
            code = str(exc.code) if isinstance(exc, MobileError) else "network_error"
            report.update(valid=valid, needs_reauth=not valid and (code in AUTH_CODES or code in ("missing_web_auth", "missing_web_refresh")),
                          error={"code": code, "message": "Web 自动续期失败"}, retry_at=now + REFRESH_BACKOFF)
            state["web_auth"] = report
            save_state(state)
            return cookie


MAINTENANCE_STOP = threading.Event()
MAINTENANCE_THREAD = None


def start_maintenance():
    global MAINTENANCE_THREAD
    if MAINTENANCE_THREAD and MAINTENANCE_THREAD.is_alive():
        return
    MAINTENANCE_STOP.clear()

    def run():
        while not MAINTENANCE_STOP.wait(1800):
            for refresh in (refresh_mobile, refresh_web):
                try:
                    refresh()
                except Exception:
                    # Do not log response bodies, credentials or network exception URLs.
                    from core.log import logger
                    logger.warning("微信读书授权维护未完成，请查看历史分页管理页的授权状态")

    MAINTENANCE_THREAD = threading.Thread(target=run, name="weread-auth-maintenance", daemon=True)
    MAINTENANCE_THREAD.start()


login = MobileLogin()
