"""Google Calendar: what is read from its event lists and what is sent to it (mocked with respx).

The responses in fixtures/google_calendar were written from Google's documented shapes (the
Events and CalendarList resources, the sync guide and the recurring events guide, as read in
October 2026). They are not recordings: no Google credentials existed when this was built, so
none of it has been run against Google itself.
"""
import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.calendars import google
from app.calendars.base import AuthLost, Entry, Gone, Refused, SyncReset, Unavailable
from tests.helpers import london, wall

FIXTURES = Path(__file__).parent / "fixtures" / "google_calendar"
EVENTS = f"{google.API}/calendars/family%40group.calendar.example.test/events"
TOKEN = {"access_token": "access-1", "expires_in": 3599, "token_type": "Bearer",
         "scope": " ".join(reversed(google.SCOPES))}
NOW = london("2026-10-05 12:00")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def _no_tokens_in_hand():
    google._tokens.clear()
    yield
    google._tokens.clear()


@pytest.fixture
def calendar():
    return google.GoogleCalendar("client-id", "client-secret", "refresh-1", "family@group.calendar.example.test",
                                 "Europe/London")


def form(request: httpx.Request) -> dict[str, str]:
    return {name: values[0] for name, values in parse_qs(request.content.decode()).items()}


def entry(summary="Dentist", start="2026-10-09 14:00", end="2026-10-09 15:00", **more) -> Entry:
    return Entry(summary=summary, start=london(start), end=london(end), **more)


# ---------------------------------------------------------------- reading
@respx.mock
async def test_a_first_read_asks_for_what_is_still_to_come_and_reads_every_kind_of_event(calendar):
    respx.post(google.TOKEN_URL).respond(json=TOKEN)
    listed = respx.get(EVENTS).respond(json=fixture("events_first_read"))
    async with calendar:
        found, token = await calendar.changes(None, NOW)

    assert token == "sync-token-after-first-read"
    asked = listed.calls.last.request
    assert asked.headers["authorization"] == "Bearer access-1"
    assert dict(asked.url.params) == {"maxResults": "250", "showDeleted": "true", "singleEvents": "false",
                                      "timeMin": "2026-10-05T11:00:00Z"}
    assert form(respx.calls[0].request) == {"client_id": "client-id", "client_secret": "client-secret",
                                            "refresh_token": "refresh-1", "grant_type": "refresh_token"}

    by_id = {remote.id: remote for remote in found}
    assert [remote.series_id is not None for remote in found] == [False] * 8 + [True] * 2   # a series before its dates

    hair = by_id["7c2m9s4h1p0r8t6u3v5w2x1yzb"]
    assert hair.etag == '"3519400361000000"' and not hair.deleted and hair.owner is None
    assert hair.entry == Entry(summary="Hairdresser", start=london("2026-10-07 16:00"), end=london("2026-10-07 16:45"),
                               location="Cut & Dry, 4 High Street", notes="Ask for Sam")

    inset = by_id["0a9inset8day7tq3c9d2e6hazz"].entry
    assert inset.all_day and (wall(inset.start), wall(inset.end)) == ("Fri 9 Oct 00:00", "Sat 10 Oct 00:00")

    football = by_id["5j1n2k8v0qsl4m7tq3c9d2e6ha"].entry
    assert football.rrule == "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261215T235959Z;WKST=SU"    # its parts in one order
    assert football.exdates == (date(2026, 9, 29),) and wall(football.start) == "Tue 1 Sep 17:00"

    moved = by_id["5j1n2k8v0qsl4m7tq3c9d2e6ha_20261013T160000Z"]
    assert (moved.series_id, moved.original) == ("5j1n2k8v0qsl4m7tq3c9d2e6ha", date(2026, 10, 13))
    assert wall(moved.entry.start) == "Wed 14 Oct 18:00" and moved.entry.rrule is None
    dropped = by_id["5j1n2k8v0qsl4m7tq3c9d2e6ha_20261020T160000Z"]
    assert dropped.deleted and dropped.entry is None and dropped.original == date(2026, 10, 20)

    ours = by_id["3g8ours2made1this0event9abcd"]
    assert ours.owner == "1b671a64-40d5-491e-99b0-da01ff1f3341" and wall(ours.entry.start) == "Wed 7 Oct 10:30"
    assert by_id["9deleted8event7tq3c9d2e6hzz"].deleted
    assert by_id["2birthday0nana7tq3c9d2e6hzz"].skipped == "it is not an ordinary event"
    assert by_id["4odd0dates7tq3c9d2e6hzzyyx"].skipped == "it repeats in a way that cannot be followed here"
    untitled = by_id["6untitled0newyork7tq3c9d2ehz"].entry       # from an email, in another zone, with no end
    assert (untitled.summary, wall(untitled.start), wall(untitled.end)) == (
        "(No title)", "Thu 8 Oct 14:00", "Thu 8 Oct 15:00")


@respx.mock
async def test_a_later_read_sends_only_the_token_follows_the_pages_and_a_forgotten_token_says_start_again(calendar):
    respx.post(google.TOKEN_URL).respond(json=TOKEN)
    page_one = {"items": [{"id": "a", "status": "cancelled"}], "nextPageToken": "page-2"}
    page_two = {"items": [{"id": "b", "status": "cancelled"}], "nextSyncToken": "token-2"}
    listed = respx.get(EVENTS).mock(side_effect=[httpx.Response(200, json=page_one), httpx.Response(200, json=page_two)])
    async with calendar:
        found, token = await calendar.changes("token-1", NOW)
        assert ([remote.id for remote in found], token) == (["a", "b"], "token-2")
        first, second = (dict(call.request.url.params) for call in listed.calls)
        assert first == {"maxResults": "250", "showDeleted": "true", "singleEvents": "false", "syncToken": "token-1"}
        assert second == {**first, "pageToken": "page-2"}
        assert len(respx.calls) == 3                         # one access token served both pages

        respx.get(EVENTS).respond(410, json={"error": {"code": 410, "errors": [{"reason": "fullSyncRequired"}]}})
        with pytest.raises(SyncReset):
            await calendar.changes("token-2", NOW)


# ---------------------------------------------------------------- writing
@respx.mock
async def test_a_new_event_is_sent_whole_marked_as_ours_with_googles_own_reminders_off(calendar):
    respx.post(google.TOKEN_URL).respond(json=TOKEN)
    made = respx.post(EVENTS).respond(json={"id": "new-1", "etag": '"1"'})
    async with calendar:
        assert await calendar.create(entry(location="High Street"), "event-uuid") == ("new-1", '"1"')
        weekly = entry("Chatterbox for Tobi", "2026-10-06 09:00", "2026-10-06 10:00", rrule="FREQ=WEEKLY;BYDAY=TU",
                       exdates=(date(2026, 10, 13), date(2026, 10, 27)))     # the second is after the clocks change
        await calendar.create(weekly, "event-2")
        day = entry("Inset day", "2026-10-09 00:00", "2026-10-10 00:00", all_day=True,
                    rrule="FREQ=YEARLY;UNTIL=20301009T225959Z", exdates=(date(2027, 10, 9),))
        await calendar.create(day, "event-3")

    first, second, third = (json.loads(call.request.content) for call in made.calls)
    assert dict(made.calls[0].request.url.params) == {"sendUpdates": "none"}       # nobody is emailed
    assert first == {
        "summary": "Dentist", "location": "High Street",
        "start": {"dateTime": "2026-10-09T14:00:00+01:00", "timeZone": "Europe/London"},
        "end": {"dateTime": "2026-10-09T15:00:00+01:00", "timeZone": "Europe/London"},
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": {"houseagent": "event-uuid"}}}
    assert second["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=Europe/London:20261013T090000,20261027T090000"]
    assert (third["start"], third["end"]) == ({"date": "2026-10-09"}, {"date": "2026-10-10"})
    assert third["recurrence"] == ["RRULE:FREQ=YEARLY;UNTIL=20301009", "EXDATE;VALUE=DATE:20271009"]


@respx.mock
async def test_a_change_sends_only_what_changed_and_a_null_clears(calendar):
    respx.post(google.TOKEN_URL).respond(json=TOKEN)
    patched = respx.patch(f"{EVENTS}/theirs%2F1").respond(json={"id": "theirs/1", "etag": '"2"'})
    weekly = entry("Chatterbox", "2026-10-06 10:00", "2026-10-06 11:00", rrule="FREQ=WEEKLY;BYDAY=TU",
                   exdates=(date(2026, 10, 13),))
    async with calendar:
        assert await calendar.update("theirs/1", entry(), {"location", "summary"}) == '"2"'
        await calendar.update("theirs/1", entry(), {"start", "end"})
        await calendar.update("theirs/1", entry("Inset day", "2026-10-09 00:00", "2026-10-10 00:00", all_day=True),
                              {"start", "end", "all_day"})
        await calendar.update("theirs/1", weekly, {"exdates"})
        await calendar.update("theirs/1", weekly, {"start", "end"})

    bodies = [json.loads(call.request.content) for call in patched.calls]
    assert dict(patched.calls[0].request.url.params) == {"sendUpdates": "none"}
    assert bodies[0] == {"summary": "Dentist", "location": None}
    assert bodies[1] == {
        "start": {"date": None, "dateTime": "2026-10-09T14:00:00+01:00", "timeZone": "Europe/London"},
        "end": {"date": None, "dateTime": "2026-10-09T15:00:00+01:00", "timeZone": "Europe/London"}}
    assert bodies[2] == {"start": {"date": "2026-10-09", "dateTime": None, "timeZone": None},
                         "end": {"date": "2026-10-10", "dateTime": None, "timeZone": None}, "recurrence": None}
    assert bodies[3] == {"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=Europe/London:20261013T100000"]}
    assert set(bodies[4]) == {"start", "end", "recurrence"}       # the skipped dates move with the series' time


@respx.mock
async def test_deleting_what_is_already_gone_is_done_and_an_event_gone_before_a_change_says_so(calendar):
    respx.post(google.TOKEN_URL).respond(json=TOKEN)
    deleted = respx.delete(f"{EVENTS}/one").mock(side_effect=[httpx.Response(204), httpx.Response(410), httpx.Response(404)])
    respx.patch(f"{EVENTS}/one").respond(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
    async with calendar:
        for _ in range(3):
            await calendar.delete("one")
        with pytest.raises(Gone):
            await calendar.update("one", entry(), {"summary"})
    assert dict(deleted.calls[0].request.url.params) == {"sendUpdates": "none"}


@respx.mock
async def test_refusals_are_sorted_into_wait_give_up_on_this_event_and_sign_in_again(calendar):
    respx.post(google.TOKEN_URL).respond(json=TOKEN)

    def refusal(status: int, reason: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": status, "errors": [{"reason": reason}]}})

    async with calendar:
        for response, expected in [
            (refusal(403, "rateLimitExceeded"), Unavailable), (refusal(403, "quotaExceeded"), Unavailable),
            (refusal(429, "rateLimitExceeded"), Unavailable), (refusal(503, "backendError"), Unavailable),
            (httpx.Response(502, text="<html>bad gateway</html>"), Unavailable),
            (refusal(403, "forbiddenForNonOrganizer"), Refused), (refusal(400, "invalid"), Refused),
            (refusal(404, "notFound"), Gone),
        ]:
            respx.post(EVENTS).mock(return_value=response)
            with pytest.raises(expected):
                await calendar.create(entry(), "event-uuid")
        respx.get(EVENTS).mock(side_effect=httpx.ConnectTimeout("timed out"))
        with pytest.raises(Unavailable):
            await calendar.changes("token", NOW)
        respx.get(EVENTS).respond(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
        with pytest.raises(Gone):                    # the calendar itself was deleted
            await calendar.changes("token", NOW)


@respx.mock
async def test_an_access_token_turned_away_is_replaced_once_before_the_sign_in_is_called_lost(calendar):
    tokens = respx.post(google.TOKEN_URL).mock(side_effect=[
        httpx.Response(200, json=TOKEN), httpx.Response(200, json={**TOKEN, "access_token": "access-2"}),
        httpx.Response(200, json={**TOKEN, "access_token": "access-3"})])
    listed = respx.get(EVENTS).mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json={"items": [], "nextSyncToken": "t"}),
        httpx.Response(401), httpx.Response(401)])
    async with calendar:
        assert await calendar.changes("token", NOW) == ([], "t")
        assert [call.request.headers["authorization"] for call in listed.calls] == ["Bearer access-1", "Bearer access-2"]
        with pytest.raises(AuthLost):
            await calendar.changes("token", NOW)
    assert tokens.call_count == 3


# ---------------------------------------------------------------- signing in
def test_the_person_is_sent_to_google_to_allow_the_two_things_needed_and_nothing_more():
    url = urlsplit(google.consent_url("client-id", "https://home.test/dashboard/calendar/google/callback", "signed"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert {name: values[0] for name, values in parse_qs(url.query).items()} == {
        "client_id": "client-id", "redirect_uri": "https://home.test/dashboard/calendar/google/callback",
        "response_type": "code", "access_type": "offline", "prompt": "consent", "state": "signed",
        "scope": "https://www.googleapis.com/auth/calendar.calendarlist.readonly "
                 "https://www.googleapis.com/auth/calendar.events"}


@respx.mock
async def test_the_code_is_exchanged_for_a_refresh_token_only_when_everything_asked_for_was_allowed():
    exchanged = respx.post(google.TOKEN_URL).respond(json={**TOKEN, "refresh_token": "refresh-9"})
    assert await google.exchange("client-id", "client-secret", "https://home.test/cb", "the-code") == "refresh-9"
    assert form(exchanged.calls.last.request) == {
        "code": "the-code", "client_id": "client-id", "client_secret": "client-secret",
        "redirect_uri": "https://home.test/cb", "grant_type": "authorization_code"}
    assert await google.access_token("client-id", "client-secret", "refresh-9") == "access-1"
    assert exchanged.call_count == 1                 # the access token that came with it is used, not fetched again

    respx.post(google.TOKEN_URL).respond(json={**TOKEN, "refresh_token": "r", "scope": google.CALENDARS_SCOPE})
    with pytest.raises(Refused):                     # the box for events was unticked
        await google.exchange("client-id", "client-secret", "https://home.test/cb", "the-code")
    respx.post(google.TOKEN_URL).respond(json=TOKEN)
    with pytest.raises(Refused):                     # no refresh token came back
        await google.exchange("client-id", "client-secret", "https://home.test/cb", "the-code")


@respx.mock
async def test_a_refused_refresh_is_a_lost_sign_in_a_wrong_client_is_ours_and_a_fault_is_tried_later():
    for response, expected in [
        (httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."}),
         AuthLost),
        (httpx.Response(401, json={"error": "invalid_client"}), Refused),
        (httpx.Response(503, text="unavailable"), Unavailable),
    ]:
        respx.post(google.TOKEN_URL).mock(return_value=response)
        with pytest.raises(expected):
            await google.access_token("client-id", "client-secret", "refresh-1")
    respx.post(google.TOKEN_URL).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(Unavailable):
        await google.access_token("client-id", "client-secret", "refresh-1")


@respx.mock
async def test_the_calendars_offered_are_the_ones_that_can_be_written_to_with_the_main_one_first():
    listed = respx.get(f"{google.API}/users/me/calendarList").respond(json=fixture("calendar_list"))
    account, found = await google.calendars("access-1")
    assert dict(listed.calls.last.request.url.params) == {"minAccessRole": "writer", "maxResults": "250"}
    assert account == "ada@example.test"
    assert [(choice.id, choice.name) for choice in found] == [
        ("ada@example.test", "ada@example.test"), ("family@group.calendar.example.test", "Family"),
        ("school@group.calendar.example.test", "School")]


@respx.mock
async def test_disconnecting_tells_google_the_sign_in_is_finished_with_and_survives_google_being_away():
    revoked = respx.post(google.REVOKE_URL).respond(200)
    await google.revoke("refresh-1")
    assert form(revoked.calls.last.request) == {"token": "refresh-1"}
    respx.post(google.REVOKE_URL).mock(side_effect=httpx.ConnectError("no route"))
    await google.revoke("refresh-1")
