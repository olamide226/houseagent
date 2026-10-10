"""A CalDAV server for the tests: Radicale, started once for the session on a local port.

It stands in for iCloud, which the tests cannot reach: no Apple account exists. Radicale speaks
the same protocol (RFC 4791 and RFC 6578) and keeps real etags and sync tokens, so discovery,
reading, writing and "what changed" are exercised for real. What iCloud does differently is not
covered here; tests/contract/test_apple_calendar.py replays the differences that are published.

`Phone` is a person's calendar app: it makes, changes and deletes iCalendar files directly.
"""
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx

PASSWORDS = {"ola@example.test": "olas-made-up-pass", "ada@example.test": "adas-made-up-pass"}
# What stands for the one a person makes at Apple when the first has stopped working.
REPLACEMENT = "a-new-one-made-at-apple"
MKCALENDAR = ('<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
              "<d:displayname>{name}</d:displayname><c:supported-calendar-component-set>{holds}"
              "</c:supported-calendar-component-set></d:prop></d:set></c:mkcalendar>")
FILE = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Apple Inc.//iOS 18.0//EN\r\nCALSCALE:GREGORIAN\r\n{events}END:VCALENDAR\r\n"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def vevent(uid: str, *lines: str) -> str:
    """One VEVENT. `lines` are its properties, as a calendar app would write them."""
    return "".join(f"{line}\r\n" for line in ("BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261005T110000Z", *lines,
                                              "END:VEVENT"))


class Server:
    def __init__(self, port: int, folder: Path) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self._port, self._folder = port, folder
        self._passwords: dict[str, str] = {}
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.set_passwords(PASSWORDS)
        (self._folder / "config").write_text(
            f"[server]\nhosts = 127.0.0.1:{self._port}\n[auth]\ntype = htpasswd\n"
            f"htpasswd_filename = {self._folder / 'users'}\nhtpasswd_encryption = plain\nhtpasswd_cache = False\n"
            f"[storage]\nfilesystem_folder = {self._folder / 'data'}\n[logging]\nlevel = warning\n")
        self._process = subprocess.Popen([sys.executable, "-m", "radicale", "--config", str(self._folder / "config")],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                httpx.get(self.url, timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("the CalDAV server for the tests did not start")

    def stop(self) -> None:
        if self._process:
            self._process.terminate()
            self._process.wait(timeout=10)

    def set_passwords(self, passwords: dict[str, str]) -> None:
        """As when someone removes an app-specific password at Apple: the old one stops working."""
        self._passwords = dict(passwords)
        (self._folder / "users").write_text("".join(f"{user}:{password}\n" for user, password in passwords.items()))

    def empty(self) -> None:
        """Remove every calendar, and put the passwords back."""
        self.set_passwords(PASSWORDS)
        for user, password in PASSWORDS.items():
            httpx.request("DELETE", f"{self.url}/{quote(user, safe='')}/", auth=(user, password))

    def phone(self, user: str, calendar: str = "family", name: str = "Family", holds: tuple[str, ...] = ("VEVENT",)
              ) -> "Phone":
        """`user`'s calendar app on one calendar, which is made if it is not there."""
        phone = Phone(self.url, user, self._passwords[user], calendar)
        made = phone.http.request("MKCALENDAR", phone.folder, content=MKCALENDAR.format(
            name=name, holds="".join(f'<c:comp name="{kind}"/>' for kind in holds)))
        assert made.status_code in (201, 409), made.text      # 409: it is there already
        return phone


class Phone:
    def __init__(self, server: str, user: str, password: str, calendar: str) -> None:
        self.user, self.password = user, password
        self.path = f"/{quote(user, safe='')}/{calendar}/"
        self.folder = server + self.path
        self.http = httpx.Client(auth=(user, password), timeout=10)

    def put(self, name: str, *events: str) -> str:
        """Make or replace the file `name`.ics with these VEVENTs. Returns its id as the sync knows it."""
        sent = self.http.put(f"{self.folder}{name}.ics", content=FILE.format(events="".join(events)),
                             headers={"Content-Type": "text/calendar; charset=utf-8"})
        assert sent.status_code in (201, 204), sent.text
        return f"{self.path}{name}.ics"

    def read(self, remote_id: str) -> str:
        """A file as it is kept, with folded lines put back together."""
        got = self.http.get(self.folder.removesuffix(self.path) + remote_id.partition("#")[0])
        assert got.status_code == 200, got.status_code
        return got.text.replace("\r\n", "\n").replace("\n ", "")

    def lines(self, remote_id: str, *names: str) -> list[str]:
        """The lines of a file's VEVENTs that are one of the properties `names`: event by event, and
        within each in the order they are asked for."""
        def name(line: str) -> str:
            return line.split(":")[0].split(";")[0]

        events = self.read(remote_id).split("BEGIN:VEVENT")[1:]
        return [line for event in events
                for line in sorted((line for line in event.splitlines() if name(line) in names),
                                   key=lambda line: names.index(name(line)))]

    def sign_in(self, password: str) -> None:
        """The phone itself stays signed in when an app's password changes."""
        self.http.auth = (self.user, password)

    def delete(self, remote_id: str) -> None:
        assert self.http.delete(self.folder.removesuffix(self.path) + remote_id).status_code in (200, 204)

    def files(self) -> list[str]:
        """The ids of every file on the calendar."""
        listed = self.http.request("PROPFIND", self.folder, headers={"Depth": "1"})
        return sorted(href for href in _hrefs(listed.text) if href.endswith(".ics"))


def _hrefs(multistatus: str) -> list[str]:
    import re

    return re.findall(r"<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>", multistatus)
