"""Connecting an adult's own Google Calendar from the Calendar page (ADR 0037): the trip to Google
and back, picking the one calendar to share, and disconnecting. Google is mocked with respx."""
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import respx

from app.agent.actions import Recorder
from app.calendars import google
from app.calendars.base import RemoteCalendar
from app.config import get_settings, mask_secrets
from app.core.timeutil import day_bounds, local, utcnow
from app.dashboard import routes
from app.db import execute, fetch_all, fetch_one, tx
from app.services import calendar, calendar_sync
from tests.helpers import add_member, ctx_for, seed_home
from tests.unit.test_dashboard import login

CALLBACK = "/dashboard/calendar/google/callback"
TOKEN = {"access_token": "access-1", "expires_in": 3599, "token_type": "Bearer", "refresh_token": "refresh-ola",
         "scope": " ".join(google.SCOPES)}
CALENDARS = {"items": [
    {"id": "ola@example.test", "summary": "ola@example.test", "accessRole": "owner", "primary": True},
    {"id": "family@group.example.test", "summary": "Family", "accessRole": "writer"}]}


async def home_with_ada():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
    return home


async def set_off(client) -> str:
    """Tap Connect Google Calendar. Returns the state Google will hand back."""
    google._tokens.clear()
    gone = await client.get("/dashboard/calendar/connect/google")
    assert gone.status_code == 303
    return parse_qs(urlsplit(gone.headers["location"]).query)["state"][0]


async def connection():
    async with tx() as conn:
        return await fetch_one(conn, "select * from calendar_connections")


@respx.mock
async def test_connect_goes_to_google_and_back_then_one_calendar_is_picked_and_it_is_kept_in_step(client):
    home = await home_with_ada()
    assert (await client.get("/dashboard/calendar/connect/google")).status_code == 401      # not signed in
    csrf = await login(client, home)
    page = (await client.get("/dashboard/calendar")).text
    assert '<a class="btn" href="/dashboard/calendar/connect/google">Connect Google Calendar</a>' in page

    gone = await client.get("/dashboard/calendar/connect/google")
    where = urlsplit(gone.headers["location"])
    asked = {name: values[0] for name, values in parse_qs(where.query).items()}
    assert gone.status_code == 303 and where.netloc == "accounts.google.com"
    assert asked["client_id"] == "test-client.apps.googleusercontent.test"
    assert asked["redirect_uri"] == f"http://testserver{CALLBACK}"
    assert await connection() is None                              # nothing is written by setting off

    exchanged = respx.post(google.TOKEN_URL).respond(json=TOKEN)
    respx.get(f"{google.API}/users/me/calendarList").respond(json=CALENDARS)
    back = await client.get(CALLBACK, params={"code": "one-time-code", "state": asked["state"]})
    assert back.status_code == 303 and back.headers["location"] == "/dashboard/calendar#own-calendar"
    assert parse_qs(exchanged.calls.last.request.content.decode())["code"] == ["one-time-code"]
    stored = await connection()
    assert (stored["status"], stored["account"], stored["member_id"], stored["calendar_id"]) == (
        "choosing", "ola@example.test", home.ola, None)
    assert "refresh-ola" not in stored["secret"]                   # encrypted at rest
    assert calendar_sync.unseal(get_settings(), stored["secret"]) == {"refresh_token": "refresh-ola"}

    page = (await client.get("/dashboard/calendar")).text
    assert "Which calendar do you want to share?" in page and "Everyone in Adebayo will see what is on" in page
    assert 'value="family@group.example.test" required>' in page and "Your main calendar" in page
    assert (await client.post("/dashboard/calendar/own/choose", data={"calendar_id": "family@group.example.test"})
            ).status_code == 403                                   # no CSRF token
    wrong = await client.post("/dashboard/calendar/own/choose", data={"calendar_id": "not-listed"}, headers=csrf)
    assert "pick one of the calendars listed" in wrong.text and (await connection())["status"] == "choosing"

    chosen = await client.post("/dashboard/calendar/own/choose", data={"calendar_id": "family@group.example.test"},
                               headers=csrf)
    stored = await connection()
    assert (stored["status"], stored["calendar_name"], stored["choices"]) == ("active", "Family", [])
    assert stored["next_pull_at"] is not None and stored["last_synced_at"] is None
    assert "Bringing your plans over" in chosen.text and 'hx-trigger="every 4s"' in chosen.text
    async with tx() as conn:
        (logged,) = await fetch_all(conn, "select tool, source, result, inverse, member_id from agent_actions")
    assert logged == {"tool": "calendar.connect", "source": "dashboard", "inverse": [], "member_id": home.ola,
                      "result": "OK: Google Calendar connected: Family"}       # in Activity, with nothing to undo

    # The card asks again while the first copy is made, and reloads the page once the plans are in.
    waiting = await client.get("/dashboard/calendar/own", headers=csrf)
    assert "Bringing your plans over" in waiting.text and "hx-refresh" not in waiting.headers
    async with tx() as conn:
        await execute(conn, "update calendar_connections set last_synced_at = :now", now=utcnow())
    done = await client.get("/dashboard/calendar/own", headers=csrf)
    assert done.headers["hx-refresh"] == "true" and "Plans go both ways" in done.text
    assert '<span class="pill good">Connected</span>' in done.text and "every 4s" not in done.text

    # The other adult sees that Ola shares it, and is offered their own.
    await login(client, home, home.members["Ada"])
    page = (await client.get("/dashboard/calendar")).text
    assert "Connect Google Calendar" in page and '<span class="title">Ola</span>' in page and "Family" in page


@respx.mock
async def test_a_trip_to_google_that_connects_nothing_says_why_and_stores_nothing(client):
    home = await home_with_ada()
    await login(client, home)
    state = await set_off(client)
    token = respx.post(google.TOKEN_URL).respond(json=TOKEN)
    listed = respx.get(f"{google.API}/users/me/calendarList").respond(json={"items": []})

    async def back(**params) -> str:
        response = await client.get(CALLBACK, params=params)
        assert response.status_code == 303 and response.headers["cache-control"] == "no-store"
        return response.headers["location"]

    forged = routes._connecting().dumps({"m": home.members["Ada"]})    # a real answer, but somebody else's
    for wrong in ("", "not-signed", forged):
        assert await back(code="c", state=wrong) == "/dashboard/calendar?connect=stale#own-calendar"
    assert token.call_count == 0                                       # the code was never sent anywhere
    assert await back(error="access_denied", state=state) == "/dashboard/calendar?connect=denied#own-calendar"
    assert token.call_count == 0
    assert await back(code="c", state=state) == "/dashboard/calendar?connect=empty#own-calendar"
    assert listed.call_count == 1

    respx.post(google.TOKEN_URL).respond(json={**TOKEN, "scope": google.CALENDARS_SCOPE})
    assert await back(code="c", state=state) == "/dashboard/calendar?connect=partial#own-calendar"
    respx.post(google.TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    assert await back(code="c", state=state) == "/dashboard/calendar?connect=failed#own-calendar"
    assert await connection() is None

    page = (await client.get("/dashboard/calendar?connect=partial")).text
    assert "Nothing was connected. Google asks two things, and both boxes have to stay ticked." in page
    assert "Nothing was connected, because Google was not given permission." in (
        await client.get("/dashboard/calendar?connect=denied")).text
    assert 'role="alert"' not in (await client.get("/dashboard/calendar?connect=made-up")).text


async def test_an_answer_that_comes_back_too_late_is_not_taken(client, monkeypatch):
    home = await home_with_ada()
    await login(client, home)
    state = await set_off(client)
    monkeypatch.setattr(routes, "CONNECT_SECONDS", -1)
    back = await client.get(CALLBACK, params={"code": "c", "state": state})
    assert back.headers["location"] == "/dashboard/calendar?connect=stale#own-calendar"


@respx.mock
async def test_disconnect_forgets_the_sign_in_tells_google_and_leaves_every_event_where_it_is(client):
    home = await home_with_ada()
    csrf = await login(client, home)
    async with tx() as conn:
        rec = Recorder(ctx_for(conn, home))
        await calendar.schedule_event(rec, title="Dentist", starts_at=utcnow() + timedelta(days=3))
        await calendar_sync.begin(conn, home.id, home.ola, "google", "ola@example.test",
                                  calendar_sync.seal(get_settings(), refresh_token="refresh-ola"),
                                  [RemoteCalendar("family", "Family")])
        await calendar_sync.choose(rec, "family")
    revoked = respx.post(google.REVOKE_URL).respond(200)
    assert (await client.post("/dashboard/calendar/own/disconnect")).status_code == 403
    page = await client.post("/dashboard/calendar/own/disconnect", headers=csrf)

    assert await connection() is None and "Connect Google Calendar" in page.text and "Dentist" in page.text
    assert parse_qs(revoked.calls.last.request.content.decode()) == {"token": ["refresh-ola"]}
    async with tx() as conn:
        (logged,) = await fetch_all(conn, "select result from agent_actions where tool = 'calendar.disconnect'")
    assert logged["result"] == "OK: Google Calendar disconnected"

    # Backing out before a calendar was picked is not worth a line in Activity.
    async with tx() as conn:
        await calendar_sync.begin(conn, home.id, home.ola, "google", "ola@example.test",
                                  calendar_sync.seal(get_settings(), refresh_token="r"), [RemoteCalendar("f", "F")])
    await client.post("/dashboard/calendar/own/disconnect", headers=csrf)
    async with tx() as conn:
        assert len(await fetch_all(conn, "select 1 from agent_actions where tool = 'calendar.disconnect'")) == 1
    assert await connection() is None


async def test_a_connection_that_stopped_offers_connect_again_and_the_other_adult_sees_it_stopped(client):
    home = await home_with_ada()
    async with tx() as conn:
        await calendar_sync.begin(conn, home.id, home.ola, "google", "ola@example.test",
                                  calendar_sync.seal(get_settings(), refresh_token="r"),
                                  [RemoteCalendar("family", "Family")])
        await calendar_sync.choose(Recorder(ctx_for(conn, home)), "family")
        await execute(conn, "update calendar_connections set status = 'broken', last_error = 'AuthLost: invalid_grant'")
    await login(client, home)
    page = (await client.get("/dashboard/calendar")).text
    assert "Your Google Calendar has stopped being kept in step" in page and "Nothing has been lost." in page
    assert '<a class="btn" href="/dashboard/calendar/connect/google">Connect again</a>' in page
    assert "invalid_grant" not in page and "AuthLost" not in page          # the reason is for the logs
    await login(client, home, home.members["Ada"])
    assert "Family · stopped working" in (await client.get("/dashboard/calendar")).text


async def test_where_google_calendar_is_not_switched_on_the_page_says_so_and_there_is_nowhere_to_go(client, monkeypatch):
    home = await home_with_ada()
    off = get_settings().model_copy(update={"google_client_secret": None})
    monkeypatch.setattr(routes, "get_settings", lambda: off)
    await login(client, home)
    page = " ".join((await client.get("/dashboard/calendar")).text.split())
    assert "Google Calendar is not switched on for this household yet." in page and "connect/google" not in page
    assert "As the person who set this up, you can switch it on" in page
    assert (await client.get("/dashboard/calendar/connect/google")).status_code == 404
    assert (await client.get(CALLBACK, params={"code": "c", "state": "s"})).status_code == 404
    await login(client, home, home.members["Ada"])
    assert "Whoever set this up can switch it on." in " ".join((await client.get("/dashboard/calendar")).text.split())


async def test_an_all_day_event_is_shown_without_a_time_on_the_calendar_and_today_pages(client):
    home = await home_with_ada()
    now = utcnow()
    async with tx() as conn:
        start, end = day_bounds(local(now, "Europe/London").date(), "Europe/London")
        await calendar.schedule_event(Recorder(ctx_for(conn, home)), title="Inset day", starts_at=start, ends_at=end,
                                      all_day=True, allow_started=True)
    await login(client, home)
    for path in ("/dashboard/calendar", "/dashboard"):
        page = (await client.get(path)).text
        assert '<span class="time">All day</span>' in page and "Inset day" in page
        assert '<span class="time">00:00</span>' not in page


def test_the_code_google_sends_back_is_kept_out_of_the_access_log():
    assert mask_secrets(f"{CALLBACK}?state=abc.def&code=4/0Ab_cd-EF&scope=x") == f"{CALLBACK}?state=…&code=…&scope=x"
