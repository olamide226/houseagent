"""Dashboard access: first-run setup, magic-link login, signed session cookie, CSRF (spec section 13)."""
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import available_timezones

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_settings
from app.core.timeutil import utcnow
from app.db import tx
from app.services import households, members

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

COOKIE = "ha_session"
SESSION_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class Session:
    member_id: str
    household_id: str
    name: str
    is_admin: bool
    household: str
    timezone: str
    csrf: str


class LoginRequired(Exception):
    """No valid session; rendered as a page that says how to get a login link."""


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="dashboard-session")


def _csrf_for(cookie: str) -> str:
    """CSRF token derived from SESSION_SECRET and the session."""
    return hmac.new(get_settings().session_secret.encode(), f"csrf:{cookie}".encode(), hashlib.sha256).hexdigest()


async def current_session(request: Request) -> Session:
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        raise LoginRequired
    try:
        data = _serializer().loads(cookie, max_age=SESSION_SECONDS)
    except BadSignature:
        raise LoginRequired from None
    async with tx() as conn:
        member = await members.session_member(conn, data["m"], data["v"])
    if member is None:
        raise LoginRequired   # logged out everywhere, or the member is gone
    return Session(member["id"], member["household_id"], member["name"], member["is_admin"],
                   member["household"], member["timezone"], _csrf_for(cookie))


async def require_csrf(request: Request, session: Session = Depends(current_session)) -> Session:
    """Every POST carries the token, in the htmx header or a form field."""
    given = request.headers.get("X-CSRF-Token") or str((await request.form()).get("csrf", ""))
    if not hmac.compare_digest(given.encode(), session.csrf.encode()):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
    return session


def _setup_allowed(token: str) -> bool:
    expected = get_settings().setup_token or ""
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, token: str = "") -> Response:
    async with tx() as conn:
        if await households.household_exists(conn) or not _setup_allowed(token):
            raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "setup.html", {
        "token": token, "timezone": get_settings().default_timezone, "error": None})


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, token: str = Form(""), household: str = Form(...),
                       timezone: str = Form(...), admin: str = Form(...)) -> Response:
    settings = get_settings()
    async with tx() as conn:
        # Serialise first-run submissions so two cannot both create a household.
        await conn.exec_driver_sql("select pg_advisory_xact_lock(hashtext('setup'))")
        if await households.household_exists(conn) or not _setup_allowed(token):
            raise HTTPException(status_code=404)
        if timezone not in available_timezones() or not household.strip() or not admin.strip():
            return templates.TemplateResponse(request, "setup.html", {
                "token": token, "timezone": timezone, "error": "Check the names and the time zone."},
                status_code=422)
        _, member_id = await households.create_household(conn, household.strip(), timezone, admin.strip())
        code = await members.create_invite(conn, member_id, utcnow())
    link = f"https://t.me/{settings.tg_bot_username}?start={code}" if settings.tg_bot_username else None
    qr = None
    if link:
        qr = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).to_string(encoding="unicode")
    return templates.TemplateResponse(request, "setup_done.html", {
        "admin": admin.strip(), "code": code, "link": link, "qr": qr, "agent": settings.agent_name})


@router.get("/login/{token}")
async def login(token: str) -> Response:
    settings = get_settings()
    async with tx() as conn:
        member = await members.consume_login_token(conn, token, utcnow())
    if member is None:
        raise LoginRequired
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        COOKIE, _serializer().dumps({"m": member["id"], "v": member["session_version"]}),
        max_age=SESSION_SECONDS, httponly=True, samesite="lax",
        secure=settings.public_base_url.startswith("https://"),
    )
    return response


@router.post("/logout")
async def logout(everywhere: int = 0, session: Session = Depends(require_csrf)) -> Response:
    if everywhere:
        async with tx() as conn:
            await members.log_out_everywhere(conn, session.member_id)
    response = RedirectResponse("/dashboard", status_code=303)
    response.delete_cookie(COOKIE)
    return response
