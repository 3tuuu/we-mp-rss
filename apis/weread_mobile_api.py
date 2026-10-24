"""Authenticated WeRSS endpoints for the optional mobile history adapter."""
import time

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from apis.base import success_response, error_response
from core.auth import get_current_user
from core.wx.model.weread_mobile import MobileClient, MobileError, load_state, save_state, login
from core.wx.model.weread_mobile import start_maintenance, MAINTENANCE_STOP

router = APIRouter(prefix="/mobile", tags=["微信读书历史分页"])


@router.on_event("startup")
def startup():
    start_maintenance()


@router.on_event("shutdown")
def shutdown():
    MAINTENANCE_STOP.set()


class HistoryRequest(BaseModel):
    mp_id: str
    page_size: int = Field(default=5, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=100000)
    max_pages: int = Field(default=2, ge=1, le=10)
    enable: bool = False


@router.get("/status")
def status(current_user=Depends(get_current_user)):
    state = load_state()
    return success_response({"authorized": bool(state.get("accessToken")) and not state.get("needs_reauth", False),
                             "auto_refresh": True,
                             "maintenance_interval_minutes": 30,
                             "refresh_interval_hours": 12,
                             "needs_reauth": state.get("needs_reauth", False),
                             "refreshed_at": state.get("refreshed_at"),
                             "refresh_attempt_at": state.get("refresh_attempt_at"),
                             "refresh_error": state.get("refresh_error"),
                             "refresh_retry_at": state.get("refresh_retry_at", 0),
                             "web_auth": state.get("web_auth", {}),
                             "automatic_for_new_feeds": bool(state.get("accessToken") and state.get("vid")) and not state.get("needs_reauth", False),
                             "authorized_at": state.get("authorized_at"),
                             "validated_feeds": state.get("validated_feeds", []),
                             "last_error": state.get("last_error"),
                             "feed_errors": state.get("feed_errors", {}),
                             "login": login.snapshot()})


@router.post("/qr/code")
def qrcode(current_user=Depends(get_current_user)):
    try:
        return success_response(login.start())
    except MobileError as exc:
        return error_response(400, str(exc), {"code": exc.code})


@router.post("/verify")
def verify(req: HistoryRequest, current_user=Depends(get_current_user)):
    try:
        client = MobileClient()
        first = client.page(req.mp_id, 0, req.page_size)
        if not first["items"] or not first["has_more"]:
            raise MobileError("unproven_pagination", "第一页不足以验证跨页；未开启自动历史采集")
        time.sleep(2)
        second = client.page(req.mp_id, first["next_offset"], req.page_size)
        ids = {x["reviewId"] for x in first["items"]}
        if not any(x["reviewId"] not in ids for x in second["items"]):
            raise MobileError("repeated_page", "第二页没有不同文章；未开启自动历史采集")
        if req.enable:
            state = load_state()
            state["validated_feeds"] = sorted(set(state.get("validated_feeds", [])) | {req.mp_id})
            state.update(last_error=None, cooldown_until=0)
            state.setdefault("feed_errors", {}).pop(req.mp_id, None)
            save_state(state)
        return success_response({"verified": True, "enabled": req.enable,
                                 "first": first, "second": second}, "两页历史列表验证通过")
    except MobileError as exc:
        return error_response(400, str(exc), {"code": exc.code})


@router.post("/collect")
def collect(req: HistoryRequest, current_user=Depends(get_current_user)):
    from core.db import DB
    from core.models.feed import Feed
    from core.wx.model.weread_mp import MpsWereadMP
    from jobs.article import UpdateArticle

    if not MobileClient().enabled(req.mp_id):
        return error_response(400, "移动端尚未授权，或此公众号正在冷却，请查看连接状态")
    session = DB.get_session()
    try:
        feed = session.query(Feed).filter(Feed.id == req.mp_id).first()
        if feed is None:
            return error_response(404, "公众号尚未添加到 WeRSS")
        name = feed.mp_name
    finally:
        session.close()
    try:
        wx = MpsWereadMP()
        wx._load_weread_auth()
        summary = wx._collect_mobile_history(req.mp_id, name, UpdateArticle, req.max_pages,
                                             req.page_size, req.offset, stop_when_known=False)
        return success_response(summary, "历史文章元数据已入库，正文由后台限量补抓")
    except MobileError as exc:
        return error_response(400, str(exc), {"code": exc.code})


@router.get("/panel", include_in_schema=False)
def panel():
    """Keep existing bookmarks working inside the normal WeRSS interface."""
    return RedirectResponse("/weread", status_code=307)
