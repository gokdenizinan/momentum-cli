#!/usr/bin/env python3
"""Momentum Grids from the terminal: focus timers and today's habit logs.

The commands call the same /api/v1/mobile/ endpoints as the iOS app, so a timer
started here appears in the app and on the web (one active timer per account).
Standard library only (Python 3.9+). Install with
`brew install gokdenizinan/tap/momentum`, or link this file onto PATH as
`momentum`. Then `momentum login` and `momentum`; see `momentum --help`.
Tokens live in ~/.config/momentum-grids/session.json, readable only by you.
MOMENTUM_BASE_URL points the CLI at another server (local, Preview).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import platform
import socket
import subprocess
import sys
import time
import unicodedata
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://momentumgrids.com"
API_PREFIX = "/api/v1/mobile/"
VERSION = "1.0.0"
CLIENT_VERSION = f"cli-{VERSION}"
# Refresh early so a request never races the 15-minute access-token expiry.
REFRESH_MARGIN = timedelta(seconds=60)
WATCH_POLL_SECONDS = 30
# The interactive view ticks locally; a slower server poll keeps hosting costs down.
UI_POLL_SECONDS = 60
GET_STARTED = '''Get started:
  momentum login            sign in once (approve in your browser)
  momentum                  open the interactive view
  momentum start "FIN 401"  start a timer for a tag
  momentum log water +2     add 2 to a numeric habit'''


class CLIError(Exception):
    """A message for the user; main() prints it and exits with status 1."""


class APIError(CLIError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


def urllib_transport(method, url, headers, body):
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except URLError as error:
        raise CLIError(f"Could not reach {url}: {error.reason}") from error


def default_config_dir():
    override = os.environ.get("MOMENTUM_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "momentum-grids"


class SessionStore:
    """The token file, plus a lock around refreshes.

    The server revokes the whole device session when a used refresh token is
    presented again, so two commands must never refresh with the same token.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "session.json"

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise CLIError(f"Could not read {self.path}: {error}") from error

    def save(self, data):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(temporary, self.path)

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.directory / "session.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class Client:
    def __init__(self, store, *, base_url=None, transport=urllib_transport):
        self.store = store
        self.transport = transport
        self.base_url = (
            base_url
            or os.environ.get("MOMENTUM_BASE_URL")
            or store.load().get("base_url")
            or DEFAULT_BASE_URL
        ).rstrip("/")

    def call(self, method, path, body=None, *, query=None):
        """Send an authenticated request, refreshing the access token as needed."""
        token = self._access_token()
        status, payload = self._send(method, path, body, query=query, token=token)
        if status == 401:
            # Revoked or expired early; one refresh decides whether to sign in again.
            token = self._access_token(rejected=token)
            status, payload = self._send(method, path, body, query=query, token=token)
        if status >= 400:
            raise self._error(status, payload)
        return payload

    def _device(self):
        # A stable device identifier makes the server reuse this terminal's
        # session row on every sign-in instead of adding a new one.
        return {
            "device_identifier": self.store.load().get("device_identifier") or str(uuid.uuid4()),
            "device_name": device_name(),
            "app_version": CLIENT_VERSION,
            "system_version": platform.platform(terse=True)[:32],
        }

    def login(self, account, password):
        device = self._device()
        body = {"password": password, **device}
        body["email" if "@" in account else "username"] = account
        status, payload = self._send("POST", "auth/login/", body)
        if status >= 400:
            error = self._error(status, payload)
            if error.code == "invalid_credentials":
                error.args = (
                    f"{error} Signed up with Apple or Google? Run `momentum login` "
                    "without --password and approve in your browser.",
                )
            raise error
        with self.store.locked():
            self._remember(payload, {"account": account, "device_identifier": device["device_identifier"]})
        return payload

    def login_in_browser(self, announce, *, sleep=None):
        """Approve this terminal on the website (works for Apple and Google accounts).

        `announce` receives the start payload so the caller can show the code.
        """
        device = self._device()
        status, start = self._send("POST", "auth/terminal/", device)
        if status == 404:
            raise CLIError(
                f"{self.base_url} doesn't support browser sign-in yet. Use: momentum login --password"
            )
        if status >= 400:
            raise self._error(status, start)
        announce(start)
        sleep = sleep or time.sleep
        interval = start.get("interval", 5)
        deadline = time.monotonic() + start.get("expires_in", 600)
        while time.monotonic() < deadline:
            sleep(interval)
            status, payload = self._send(
                "POST", "auth/terminal/token/", {"device_code": start["device_code"]}
            )
            if status < 400:
                with self.store.locked():
                    self._remember(payload, {"device_identifier": device["device_identifier"]})
                return payload
            error = self._error(status, payload)
            if error.code == "slow_down":
                interval += 5
            elif error.code == "access_denied":
                raise CLIError("Sign-in was denied in the browser.")
            elif error.code != "authorization_pending":
                break
        raise CLIError("The sign-in code expired. Run momentum login again.")

    def logout(self):
        saved = self.store.load()
        if saved.get("session_id") and saved.get("base_url") == self.base_url:
            # Revoke only this device session. auth/logout/ would also discard
            # the account's running timer, including one started on the phone.
            with contextlib.suppress(CLIError):
                self.call("DELETE", f"sessions/{saved['session_id']}/")
        with self.store.locked():
            self.store.save(self._signed_out(self.store.load()))

    def _access_token(self, *, rejected=None):
        saved = self.store.load()
        self._require_session(saved)
        if saved.get("access_token") != rejected and not self._expiring(saved):
            return saved["access_token"]
        with self.store.locked():
            saved = self.store.load()  # another command may have refreshed meanwhile
            self._require_session(saved)
            if saved.get("access_token") != rejected and not self._expiring(saved):
                return saved["access_token"]
            status, payload = self._send(
                "POST", "auth/refresh/", {"refresh_token": saved["refresh_token"]}
            )
            if status >= 400:
                error = self._error(status, payload)
                if status in (400, 401):
                    self.store.save(self._signed_out(saved))
                    raise CLIError(
                        "Your terminal sign-in expired or was revoked. Run: momentum login"
                    ) from error
                raise error
            self._remember(payload)
            return payload["access_token"]

    def _require_session(self, saved):
        if not saved.get("refresh_token") or saved.get("base_url") != self.base_url:
            raise CLIError(f"Not signed in to {self.base_url}. Run: momentum login")

    @staticmethod
    def _expiring(saved):
        expires = parse_time(saved.get("access_token_expires_at"))
        return expires is None or expires - REFRESH_MARGIN <= datetime.now(timezone.utc)

    @staticmethod
    def _signed_out(saved):
        keep = ("base_url", "device_identifier", "account")
        return {key: saved[key] for key in keep if key in saved}

    def _remember(self, payload, saved=None):
        saved = self.store.load() if saved is None else saved
        saved.update(
            base_url=self.base_url,
            access_token=payload["access_token"],
            access_token_expires_at=payload["access_token_expires_at"],
            refresh_token=payload["refresh_token"],
            refresh_token_expires_at=payload["refresh_token_expires_at"],
            session_id=str(payload["session"]["id"]),
        )
        self.store.save(saved)

    def _send(self, method, path, body=None, *, query=None, token=None):
        url = self.base_url + API_PREFIX + path
        if query:
            url += "?" + urlencode(query)
        headers = {
            "Accept": "application/json",
            "User-Agent": f"MomentumGridsCLI/{CLIENT_VERSION}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self.transport(method, url, headers, data)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload

    @staticmethod
    def _error(status, payload):
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or "Request failed."
            details = error.get("details")
            if message == "Request validation failed." and isinstance(details, dict):
                message = "; ".join(
                    f"{field}: {' '.join(map(str, problems)) if isinstance(problems, list) else problems}"
                    for field, problems in details.items()
                )
            return APIError(status, error.get("code") or "error", message)
        return APIError(status, "http_error", f"The server answered with HTTP {status}.")


def device_name():
    host = socket.gethostname().split(".")[0] or "this Mac"
    return f"Terminal ({host})"[:100]


def fold(text):
    """Case- and accent-insensitive key, so "kairos" finds "Kaïros" and "ışık" finds "Işık"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ı", "i"))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).strip()


def pick(items, query, kind):
    """Exact name, then unique prefix, then unique substring; a habit also matches its id."""
    if kind == "habit" and query.isdigit():
        for item in items:
            if str(item["id"]) == query:
                return item
    wanted = fold(query)
    for matches_name in (
        lambda name: name == wanted,
        lambda name: name.startswith(wanted),
        lambda name: wanted in name,
    ):
        found = [item for item in items if matches_name(fold(item["name"]))]
        if len(found) == 1:
            return found[0]
        if found:
            names = ", ".join(item["name"] for item in found)
            raise CLIError(f'"{query}" matches several {kind}s: {names}. Be more specific.')
    available = ", ".join(item["name"] for item in items) or "none yet"
    raise CLIError(f'No {kind} matches "{query}". Available: {available}')


def clock(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def number(value):
    """API decimals ("8.00") without trailing zeros, for display."""
    return format(Decimal(value or "0").normalize(), "f")


def timer_title(session):
    association = session["association"]
    parts = [session["name"]] if session["name"] else []
    if association["kind"] != "none":
        parts.append(association["name"])
    return " · ".join(parts) or "Untitled timer"


def timer_line(session, elapsed=None):
    elapsed = session["elapsed_seconds"] if elapsed is None else elapsed
    target = session.get("target_duration_seconds")
    state = "Running" if session["status"] == "running" else "Paused"
    timing = clock(elapsed)
    if target:
        timing += f" of {clock(target)} ({clock(target - elapsed)} left)"
    return f"{state}: {timer_title(session)}  {timing}"


def active_timer(client):
    sessions = client.call(
        "GET", "focus/sessions/", query={"status": "active", "page_size": 1}
    )["sessions"]
    return sessions[0] if sessions else None


def require_active_timer(client):
    session = active_timer(client)
    if session is None:
        raise CLIError("No timer is running. Start one with: momentum start <tag>")
    return session


def today_habits(client):
    return client.call("GET", "automations/habits/")["habits"]


def focus_tags(client, *, include_archived=False):
    tags = client.call("GET", "focus/tags/")["tags"]
    return [tag for tag in tags if include_archived or not tag["archived"]]


def watch(client, session, out):
    """Redraw the timer every second until Ctrl-C; the timer itself keeps running."""
    fetched = time.monotonic()
    try:
        while True:
            if time.monotonic() - fetched >= WATCH_POLL_SECONDS:
                session = active_timer(client)
                fetched = time.monotonic()
                if session is None:
                    print("\nThe timer was finished or discarded elsewhere.", file=out)
                    return
            elapsed = session["elapsed_seconds"]
            if session["status"] == "running":
                elapsed += int(time.monotonic() - fetched)
            target = session.get("target_duration_seconds")
            if target:
                elapsed = min(elapsed, target)
            out.write("\r\033[K" + timer_line(session, elapsed))
            out.flush()
            if target and elapsed >= target:
                print("\a\nTime's up. Save it with: momentum stop", file=out)
                return
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped watching; the timer keeps running.", file=out)


def ask(prompt, *, secret=False, default=""):
    """Prompt in the terminal, or in a macOS dialog when there is no terminal.

    Claude Code's `!` commands have no stdin, and the dialog keeps the password
    out of the transcript.
    """
    if sys.stdin.isatty():
        if secret:
            return getpass.getpass(f"{prompt}: ")
        return input(f"{prompt}{f' [{default}]' if default else ''}: ").strip() or default
    if sys.platform != "darwin":
        raise CLIError(
            "momentum login needs an interactive terminal, or --email with --password-stdin."
        )
    hidden = " with hidden answer" if secret else ""
    script = (
        "on run argv",
        "set reply to display dialog (item 1 of argv) default answer (item 2 of argv)"
        f' with title "Momentum Grids"{hidden}',
        "return text returned of reply",
        "end run",
    )
    command = ["osascript"]
    for line in script:
        command += ["-e", line]
    result = subprocess.run(
        [*command, f"{prompt}:", default], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        if "-128" in result.stderr:
            raise CLIError("Sign-in canceled.")
        raise CLIError(f"Could not show the sign-in dialog: {result.stderr.strip()}")
    answer = result.stdout.rstrip("\n")
    return answer if secret else answer.strip()


def show_code_dialog(code):
    """Show the code while polling; Claude Code's `!` may print output only at the end."""
    script = (
        "on run argv",
        'display dialog ("Enter this code on the page that just opened in your browser:"'
        ' & return & return & item 1 of argv) with title "Momentum Grids" buttons {"OK"} default button 1',
        "end run",
    )
    command = ["osascript"]
    for line in script:
        command += ["-e", line]
    return subprocess.Popen(
        [*command, code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def login_in_browser(client, out):
    dialogs = []

    def announce(start):
        code, url = start["user_code"], start["verification_uri"]
        print(f"Your code: {code}", file=out)
        print(f"Approve this terminal at {url} (sign in there if asked).", file=out)
        if sys.stdin.isatty():
            with contextlib.suppress(EOFError):
                input("Press Enter to open the page in your browser… ")
        elif sys.platform == "darwin":
            with contextlib.suppress(OSError):
                dialogs.append(show_code_dialog(code))
        print("Waiting for approval… (Ctrl-C to cancel)", file=out)
        out.flush()
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    try:
        client.login_in_browser(announce)
    finally:
        for dialog in dialogs:
            dialog.terminate()
    account = client.call("GET", "account/")
    name = account.get("verified_email") or account.get("username") or "your account"
    with client.store.locked():
        saved = client.store.load()
        saved["account"] = name
        client.store.save(saved)
    return name


def cmd_login(client, args, out):
    if not (args.password or args.email or args.password_stdin):
        name = login_in_browser(client, out)
        print(f"Signed in to {client.base_url} as {name} ({device_name()}).", file=out)
        print("Next: run `momentum` to open the interactive view.", file=out)
        return
    account = args.email or ask("Email", default=client.store.load().get("account", ""))
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = ask("Password", secret=True)
    if not account or not password:
        raise CLIError("Enter your email and password.")
    client.login(account, password)
    print(f"Signed in to {client.base_url} as {account} ({device_name()}).", file=out)


def cmd_logout(client, args, out):
    client.logout()
    print("Signed out of this terminal. Running timers were left untouched.", file=out)


def cmd_tags(client, args, out):
    shown = focus_tags(client, include_archived=args.all)
    if not shown:
        print("No focus tags yet. Create one with: momentum start <name> --new-tag", file=out)
    for tag in shown:
        print(tag["name"] + ("  (archived)" if tag["archived"] else ""), file=out)


def start_timer(client, *, tag=None, habit=None, name=None, minutes=None):
    """Shared timer creation for the command and interactive picker."""
    body = {"client_session_id": str(uuid.uuid4())}
    if tag:
        body.update(association_kind="tag", tag_id=tag["id"])
    elif habit:
        body.update(association_kind="habit", habit_id=habit["id"])
    if name:
        body["name"] = name
    if minutes:
        body["target_duration_seconds"] = max(1, round(minutes * 60))
    try:
        return client.call("POST", "focus/sessions/", body)
    except APIError as error:
        running = active_timer(client) if error.code == "active_session_exists" else None
        if running is None:
            raise
        raise CLIError(
            f"Another timer is active. {timer_line(running)}\n"
            "Finish it with `momentum stop` or drop it with `momentum discard` first."
        ) from error


def change_timer(client, session, action):
    return client.call("POST", f"focus/sessions/{session['id']}/{action}/")


def cmd_start(client, args, out):
    if args.tag and args.habit:
        raise CLIError("Choose a tag or a habit, not both.")
    tag = habit = None
    if args.tag:
        tags = focus_tags(client)
        same_name = [tag for tag in tags if fold(tag["name"]) == fold(args.tag)]
        if args.new_tag and not same_name:
            tag = client.call("POST", "focus/tags/", {"name": args.tag})
        else:
            tag = pick(tags, args.tag, "tag")
    elif args.habit:
        habit = pick(today_habits(client), args.habit, "habit")
    session = start_timer(client, tag=tag, habit=habit, name=args.name, minutes=args.minutes)
    print(f"Started {timer_title(session)}.", file=out)
    if args.watch:
        watch(client, session, out)


def cmd_status(client, args, out):
    session = active_timer(client)
    if session is None:
        print("No timer is running.", file=out)
    elif args.watch:
        watch(client, session, out)
    else:
        print(timer_line(session), file=out)


def transition(action, verb):
    def command(client, args, out):
        session = require_active_timer(client)
        session = change_timer(client, session, action)
        if action == "finish":
            print(f"Saved {clock(session['duration_seconds'])} for {timer_title(session)}.", file=out)
        else:
            print(f"{verb} {timer_title(session)} at {clock(session['elapsed_seconds'])}.", file=out)
    return command


def cmd_discard(client, args, out):
    session = require_active_timer(client)
    if not args.yes:
        if not sys.stdin.isatty():
            raise CLIError("Discarding deletes the timer. Re-run with --yes to confirm.")
        answer = input(f"Delete {timer_title(session)} ({clock(session['elapsed_seconds'])}) without saving? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Kept the timer.", file=out)
            return
    change_timer(client, session, "discard")
    print(f"Discarded {timer_title(session)}.", file=out)


def habit_mark(habit):
    today = habit["today"]
    if today["frozen"]:
        return "❄"
    if today["completed"]:
        return "✓"
    return "·" if today["scheduled"] else " "


def habit_line(habit):
    today = habit["today"]
    mark = habit_mark(habit)
    label = f"{habit['emoji']} {habit['name']}".strip()
    if habit["habit_type"] == "numeric":
        unit = f" {habit['unit']}" if habit["unit"] else ""
        label += f"  {number(today['value'])} / {number(habit['target_value'])}{unit}"
    if not today["scheduled"]:
        label += "  (not scheduled today)"
    return f"{mark} {label}"


def cmd_habits(client, args, out):
    habits = today_habits(client)
    if not habits:
        print("No active habits.", file=out)
    for habit in habits:
        print(habit_line(habit), file=out)


def set_today(client, habit, *, completed=None, value=None):
    """Write today's absolute state (retries never double-count) and mirror it locally."""
    if habit["habit_type"] == "binary":
        payload = client.call("PUT", f"habits/{habit['id']}/today/", {"completed": completed})
    else:
        payload = client.call("PUT", f"habits/{habit['id']}/today/numeric/", {"value": str(value)})
        habit["today"]["value"] = payload["value"]
    habit["today"]["completed"] = payload["completed"]


def numeric_value(habit, raw):
    current = Decimal(habit["today"]["value"] or "0")
    try:
        if raw is None:
            new = current + Decimal(habit.get("quick_step") or "1")
        elif raw[0] in "+-":
            new = current + Decimal(raw)
        else:
            new = Decimal(raw)
    except InvalidOperation as error:
        raise CLIError(f'"{raw}" is not a number. Use 5, +2 or -1.') from error
    return max(Decimal("0"), new)


def cmd_log(client, args, out):
    habit = pick(today_habits(client), args.habit, "habit")
    if habit["habit_type"] == "binary":
        if args.value is not None:
            raise CLIError(f"{habit['name']} is a done/not-done habit; leave out the value.")
        set_today(client, habit, completed=True)
    else:
        set_today(client, habit, value=numeric_value(habit, args.value))
    print(habit_line(habit), file=out)


def cmd_undo(client, args, out):
    habit = pick(today_habits(client), args.habit, "habit")
    set_today(client, habit, completed=False, value=Decimal("0"))
    print(f"Cleared today's log for {habit['name']}.", file=out)


@dataclass
class UIState:
    """Plain presentation data; monotonic timestamps keep the local clock steady."""

    habits: list = field(default_factory=list)
    timer: dict | None = None
    selection: int = 0
    status: str = ""
    now: float = 0
    timer_fetched_at: float = 0
    date: datetime = field(default_factory=lambda: datetime.now().astimezone())
    overlay: str | None = None
    tags: list = field(default_factory=list)
    filter_text: str = ""
    picker_selection: int = 0
    pending_tag: dict | None = None
    minutes: str = ""
    pending_discard: dict | None = None
    discard_prompt: str = ""


class UIApp:
    def __init__(self, client):
        self.client = client
        self.state = UIState()
        self.last_refresh = -UI_POLL_SECONDS
        self.quit = False

    def refresh(self, message=None):
        """Keep the last good data on failure and throttle even failed polls."""
        state = self.state
        errors = []
        try:
            state.timer = active_timer(self.client)
            state.timer_fetched_at = time.monotonic()
        except CLIError as error:
            errors.append(str(error))
        try:
            selected_id = (
                state.habits[state.selection]["id"]
                if 0 <= state.selection < len(state.habits) else None
            )
            state.habits = today_habits(self.client)
            state.selection = next(
                (i for i, habit in enumerate(state.habits) if habit["id"] == selected_id),
                min(state.selection, max(0, len(state.habits) - 1)),
            )
        except CLIError as error:
            errors.append(str(error))
        self.last_refresh = state.now = time.monotonic()
        state.date = datetime.now().astimezone()
        if errors:
            state.status = " · ".join(dict.fromkeys(([message] if message else []) + errors))
        elif message is not None:
            state.status = message


def display_width(text):
    """Terminal cells, including wide emoji/CJK and zero-width combining marks."""
    return sum(
        0 if unicodedata.category(char) in ("Mn", "Me", "Cf", "Cc", "Cs")
        else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


def clip(text, width):
    """A safe single line, truncated by cells rather than Python character count."""
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cs") else c for c in str(text))
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    result = ""
    used = 0
    for char in text:
        cells = display_width(char)
        if used + cells > width - 1:
            break
        result += char
        used += cells
    return result + "…"


def columns(left, right, width):
    right = clip(right, width)
    left = clip(left, max(0, width - display_width(right) - 2))
    return left + " " * max(0, width - display_width(left + right)) + right


def progress_bar(value, target, width=8):
    fraction = min(1, max(0, float(value or 0) / float(target))) if target else 0
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


def ui_elapsed(state):
    if not state.timer:
        return 0
    elapsed = state.timer["elapsed_seconds"]
    if state.timer["status"] == "running":
        elapsed += max(0, int(state.now - state.timer_fetched_at))
    target = state.timer.get("target_duration_seconds")
    return min(elapsed, target) if target else elapsed


def tag_choices(state):
    choices = [None] + state.tags
    query = fold(state.filter_text)
    return [tag for tag in choices if query in fold(tag["name"] if tag else "No tag")]


def habit_detail(habit):
    parts = []
    if habit["habit_type"] == "numeric":
        unit = f" {habit['unit']}" if habit["unit"] else ""
        parts.append(f"{number(habit['today']['value'])} / {number(habit['target_value'])}{unit}")
    if habit["today"]["frozen"]:
        parts.append("frozen today")
    elif not habit["today"]["scheduled"]:
        parts.append("not scheduled today")
    return " · ".join(parts)


def habit_rows(state, width, count):
    if not state.habits:
        return [(" No habits yet. Add habits in the app.", "secondary")][:count]
    labels = [f"{h['emoji']} {h['name']}".strip() for h in state.habits]
    details = [habit_detail(h) for h in state.habits]
    # Reserve the detail column, but keep at least eight cells for names.
    detail_width = min(max(map(display_width, details)), max(0, width - 13))
    name_width = max(1, min(max(map(display_width, labels)), width - detail_width - 5))
    selection = min(max(0, state.selection), len(state.habits) - 1)
    start = max(0, selection - count + 1)
    lines = []
    for index in range(start, min(len(state.habits), start + count)):
        habit = state.habits[index]
        selected = index == selection
        name = clip(labels[index], name_width)
        line = ("›" if selected else " ") + habit_mark(habit) + " " + name
        if details[index]:
            line += " " * (name_width - display_width(name) + 2)
            line += clip(details[index], max(0, width - display_width(line)))
        if habit["habit_type"] == "numeric" and display_width(line) + 10 <= width:
            line += "  " + progress_bar(habit["today"]["value"], habit["target_value"])
        style = "selected" if selected else "secondary" if habit["today"]["frozen"] or not habit["today"]["scheduled"] else "normal"
        lines.append((line, style))
    return lines


def wrap_hints(hints, width, separator="  "):
    lines = []
    current = ""
    for hint in hints:
        candidate = current + (separator if current else "") + hint
        if current and display_width(candidate) > width:
            lines.append(current)
            current = hint
        else:
            current = candidate
    return lines + ([current] if current else [])


HELP_LINES = [
    "↑/↓ or k/j  Move between habits",
    "Enter/Space  Toggle done or add a step",
    "+/=  Mark done or add a quick step",
    "-/_  Clear or subtract a quick step",
    "t  Choose a tag and optional countdown",
    "Picker: type to filter, ↑/↓ to move,",
    "Enter to choose, Esc to cancel.",
    "Countdown: digits, Backspace, Enter.",
    "p  Pause or resume the timer",
    "s  Save the timer",
    "d  Discard: y confirms, N keeps it",
    "r  Refresh from the server",
    "?  Show help; any key closes it",
    "q/Esc/Ctrl-C  Quit; timer keeps running",
]


def render(state, width, height):
    """Build (text, style) rows without curses, I/O, or changes to state.

    Leave the last terminal column unused, including the bottom-right cell.
    Small screens shed decoration and bars before the selected habit.
    """
    width, height = max(0, width - 1), max(0, height)
    if not width or not height:
        return []
    if state.overlay == "help":
        lines = [(" Momentum Grids · Help", "title"), ("", "normal")]
        lines += [(line, "normal") for line in HELP_LINES]
        lines += [("", "normal"), (" Any key closes help", "secondary")]
    elif state.overlay == "tags":
        top = [(" Start a timer · Choose a tag", "title"),
               (" Filter: " + state.filter_text, "accent")]
        bottom = [(state.status, "accent"), ("↑↓ move  Enter choose  Esc cancel", "secondary")]
        choices = tag_choices(state)
        count = max(0, height - len(top) - len(bottom))
        start = max(0, state.picker_selection - count + 1)
        rows = [(("› " if i == state.picker_selection else "  ") + (tag["name"] if tag else "No tag"),
                 "selected" if i == state.picker_selection else "normal")
                for i, tag in enumerate(choices) if start <= i < start + count]
        if not choices:
            rows = [(" No matching tags", "secondary")][:count]
        lines = top + rows + [("", "normal")] * max(0, count - len(rows)) + bottom
    elif state.overlay in ("minutes", "discard"):
        prompt = (" Countdown minutes (Enter for none): " + state.minutes
                  if state.overlay == "minutes" else state.discard_prompt)
        # Wrapping keeps the complete destructive confirmation readable at 40 columns.
        lines = [(" Start a timer" if state.overlay == "minutes" else " Discard timer", "title")]
        lines += [(line, "accent") for line in wrap_hints(prompt.split(), width, " ")]
        lines += [(state.status, "accent"),
                  ("Digits · Backspace · Enter · Esc cancel" if state.overlay == "minutes"
                   else "y discard  N/Esc keep", "secondary")]
    else:
        timer = state.timer
        top = [(columns(" Momentum Grids", state.date.strftime("%a %d %b"), width), "title")]
        if height >= 12:
            top.append(("─" * width, "secondary"))
        if timer:
            elapsed = ui_elapsed(state)
            label = "● " if timer["status"] == "running" else "❚❚ Paused · "
            top.append((columns(" " + label + timer_title(timer), clock(elapsed), width), "accent"))
            target = timer.get("target_duration_seconds")
            if target:
                if elapsed >= target:
                    top.append((" Time's up · press s to save", "accent"))
                else:
                    left = f"{clock(target - elapsed)} left"
                    bar_width = min(20, max(0, width - display_width(left) - 5))
                    top.append(("   " + progress_bar(elapsed, target, bar_width) + "  " + left, "secondary"))
        else:
            top.append((" No timer running · press t to start one", "secondary"))
        if height >= 12:
            top.append(("", "normal"))
        eligible = [h for h in state.habits if h["today"]["completed"] or
                    (h["today"]["scheduled"] and not h["today"]["frozen"])]
        done = sum(bool(h["today"]["completed"]) for h in eligible)
        top.append((f" Today  {done} of {len(eligible)} done", "title"))
        hints = ["↑↓ move", "⏎ log", "+ − adjust", "t timer"]
        if timer:
            hints += ["p pause" if timer["status"] == "running" else "p resume", "s save", "d discard"]
        hints += ["r refresh", "? help", "q quit"]
        foot = wrap_hints(hints, width) if height >= 10 else [
            "↑↓ ⏎ ± t " + ("p s d " if timer else "") + "r ? q"
        ]
        bottom = ([("─" * width, "secondary")] if height >= 12 else [])
        bottom += [(state.status, "accent")] + [(line, "secondary") for line in foot]
        # In unusually short terminals, always retain a row for the selection.
        bottom = bottom[-max(0, height - 1):] if height > 1 else []
        top = top[:max(0, height - len(bottom) - 1)]
        count = max(0, height - len(top) - len(bottom))
        rows = habit_rows(state, width, count)
        lines = top + rows + [("", "normal")] * max(0, count - len(rows)) + bottom
    return [(clip(text, width), style) for text, style in lines[:height]]


def handle_key(app, key):
    """Handle terminal-independent keys; all API errors become visible status text."""
    state = app.state
    key = {"\n": "ENTER", "\r": "ENTER", "\x1b": "ESC", "\x7f": "BACKSPACE", "\b": "BACKSPACE"}.get(key, key)
    if key == "\x03":
        app.quit = True
        return
    if key in ("RESIZE", -1):
        return
    if state.overlay == "help":
        state.overlay = None
        return
    try:
        if state.overlay == "discard":
            session = state.pending_discard
            state.overlay = None
            if key in ("y", "Y"):
                change_timer(app.client, session, "discard")
                state.timer = None
                app.refresh(f"Discarded {timer_title(session)}")
            else:
                state.status = "Kept the timer"
            return
        if state.overlay == "tags":
            choices = tag_choices(state)
            if key == "ESC":
                state.overlay = None
            elif key in ("UP", "DOWN"):
                delta = 1 if key == "DOWN" else -1
                state.picker_selection = min(max(0, state.picker_selection + delta), max(0, len(choices) - 1))
            elif key == "ENTER":
                if choices:
                    state.pending_tag = choices[state.picker_selection]
                    state.minutes = ""
                    state.overlay = "minutes"
            elif key == "BACKSPACE" or (isinstance(key, str) and len(key) == 1 and key.isprintable()):
                state.filter_text = state.filter_text[:-1] if key == "BACKSPACE" else state.filter_text + key
                state.picker_selection = 0
            return
        if state.overlay == "minutes":
            if key == "ESC":
                state.overlay = None
            elif key == "BACKSPACE":
                state.minutes = state.minutes[:-1]
            elif key == "ENTER":
                session = start_timer(app.client, tag=state.pending_tag, minutes=int(state.minutes or "0"))
                state.timer = session
                state.timer_fetched_at = time.monotonic()
                state.overlay = None
                app.refresh(f"Started {timer_title(session)}")
            elif isinstance(key, str) and key in "0123456789" and len(key) == 1:
                state.minutes = (state.minutes + key)[:6]
            return
        if key in ("q", "ESC"):
            app.quit = True
        elif key == "?":
            state.overlay = "help"
        elif key in ("UP", "k", "DOWN", "j"):
            delta = 1 if key in ("DOWN", "j") else -1
            state.selection = min(max(0, state.selection + delta), max(0, len(state.habits) - 1))
        elif key == "r":
            app.refresh("Refreshed")
        elif key == "t":
            app.refresh()
            if state.timer:
                state.status = "Save (s) or discard (d) the current timer first"
            else:
                state.tags = focus_tags(app.client)
                state.filter_text, state.picker_selection = "", 0
                state.overlay = "tags"
        elif key in ("p", "s", "d"):
            if not state.timer:
                state.status = "No timer running · press t to start one"
                return
            session = state.timer
            if key == "d":
                state.pending_discard = session
                state.discard_prompt = f"Discard {timer_title(session)} ({clock(ui_elapsed(state))}) without saving? y/N"
                state.overlay = "discard"
                return
            action = "finish" if key == "s" else "pause" if session["status"] == "running" else "resume"
            updated = change_timer(app.client, session, action)
            state.timer = None if action == "finish" else updated
            state.timer_fetched_at = time.monotonic()
            verb = {"finish": "Saved", "pause": "Paused", "resume": "Resumed"}[action]
            app.refresh(f"{verb} {timer_title(session)}")
        elif key in ("ENTER", " ", "+", "=", "-", "_") and state.habits:
            habit = state.habits[state.selection]
            if habit["habit_type"] == "binary":
                completed = key in ("+", "=") or (key in ("ENTER", " ") and not habit["today"]["completed"])
                set_today(app.client, habit, completed=completed)
                message = ("Logged " if completed else "Cleared ") + habit["name"]
            else:
                raw = str(-Decimal(habit.get("quick_step") or "1")) if key in ("-", "_") else None
                set_today(app.client, habit, value=numeric_value(habit, raw))
                message = f"Logged {habit['name']}: {habit_detail(habit)}"
            app.refresh(message)
    except CLIError as error:
        app.refresh(str(error))


def curses_ui(screen, app):
    """The thin terminal adapter: styles, safe drawing, and key translation."""
    import curses

    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.keypad(True)
    screen.timeout(1000)
    styles = {"normal": 0, "title": curses.A_BOLD, "accent": curses.A_BOLD,
              "secondary": curses.A_DIM, "selected": curses.A_REVERSE | curses.A_BOLD}
    # wrapper() initializes colors. Keep pair zero on the terminal's background
    # even when NO_COLOR disables all accent pairs.
    with contextlib.suppress(curses.error):
        curses.use_default_colors()
    if "NO_COLOR" not in os.environ and curses.has_colors():
        with contextlib.suppress(curses.error):
            curses.init_pair(1, 214 if curses.COLORS >= 256 else curses.COLOR_YELLOW, -1)
            curses.init_pair(2, 137 if curses.COLORS >= 256 else curses.COLOR_YELLOW, -1)
            for style in ("title", "accent", "selected"):
                styles[style] |= curses.color_pair(1)
            styles["secondary"] |= curses.color_pair(2)
    keys = {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_ENTER: "ENTER",
            curses.KEY_BACKSPACE: "BACKSPACE", curses.KEY_RESIZE: "RESIZE"}
    app.refresh()
    while not app.quit:
        app.state.now = time.monotonic()
        app.state.date = datetime.now().astimezone()
        if app.state.now - app.last_refresh >= UI_POLL_SECONDS:
            app.refresh()
        height, width = screen.getmaxyx()
        screen.erase()
        for row, (line, style) in enumerate(render(app.state, width, height)):
            with contextlib.suppress(curses.error):
                screen.addstr(row, 0, line, styles[style])
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:  # timeout: redraw the local timer
            continue
        handle_key(app, keys.get(key, key))


def cmd_ui(client, args, out):
    if not sys.stdin.isatty() or not out.isatty():
        raise CLIError("The interactive view needs a terminal. Run: momentum --help")
    saved = client.store.load()
    if not saved.get("refresh_token") or saved.get("base_url") != client.base_url:
        print("Welcome to Momentum Grids in your terminal. Sign in once to get started.", file=out)
        cmd_login(client, argparse.Namespace(email=None, password_stdin=False, password=False), out)
    import curses

    out.flush()
    os.environ.setdefault("ESCDELAY", "25")  # Esc closes overlays without curses' 1 s wait
    try:
        curses.wrapper(curses_ui, UIApp(client))
    except KeyboardInterrupt:
        pass
    except curses.error as error:
        raise CLIError(f"Could not open the interactive view: {error}") from error


def build_parser():
    parser = argparse.ArgumentParser(
        prog="momentum",
        description="Momentum Grids focus timers and habit logs from the terminal.",
        epilog=GET_STARTED,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"momentum {VERSION}")
    parser.set_defaults(handler=cmd_ui)
    commands = parser.add_subparsers(dest="command", metavar="command")

    commands.add_parser("ui", help="open the interactive view").set_defaults(handler=cmd_ui)

    login = commands.add_parser("login", help="sign in once (approve in your browser)")
    login.add_argument("--password", action="store_true", help="use email and password instead of the browser")
    login.add_argument("--email", help="account email for password sign-in (prompted if left out)")
    login.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    login.set_defaults(handler=cmd_login)

    logout = commands.add_parser("logout", help="sign this terminal out; timers keep running")
    logout.set_defaults(handler=cmd_logout)

    tags = commands.add_parser("tags", help="list focus tags")
    tags.add_argument("--all", action="store_true", help="include archived tags")
    tags.set_defaults(handler=cmd_tags)

    start = commands.add_parser("start", help="start a timer")
    start.add_argument("tag", nargs="?", help="tag name; a unique prefix or part is enough")
    start.add_argument("--habit", help="track the timer against a habit instead of a tag")
    start.add_argument("--new-tag", action="store_true", help="create the tag first")
    start.add_argument("-m", "--minutes", type=float, help="countdown length in minutes")
    start.add_argument("-n", "--name", help="what you are working on")
    start.add_argument("-w", "--watch", action="store_true", help="show a live clock (Ctrl-C to leave)")
    start.set_defaults(handler=cmd_start)

    status = commands.add_parser("status", help="show the active timer")
    status.add_argument("-w", "--watch", action="store_true", help="show a live clock (Ctrl-C to leave)")
    status.set_defaults(handler=cmd_status)

    for name, action, verb, help_text in (
        ("pause", "pause", "Paused", "pause the active timer"),
        ("resume", "resume", "Resumed", "resume the paused timer"),
        ("stop", "finish", "Saved", "finish and save the active timer"),
    ):
        commands.add_parser(name, help=help_text).set_defaults(handler=transition(action, verb))

    discard = commands.add_parser("discard", help="delete the active timer without saving")
    discard.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    discard.set_defaults(handler=cmd_discard)

    habits = commands.add_parser("habits", help="list today's habits")
    habits.set_defaults(handler=cmd_habits)

    log = commands.add_parser("log", help="log a habit for today")
    log.add_argument("habit", help="habit name (a unique prefix or part) or id")
    log.add_argument("value", nargs="?", help="numeric habits: +2 / -1 to adjust, 5 to set; default adds one step")
    log.set_defaults(handler=cmd_log)

    undo = commands.add_parser("undo", help="clear today's log for a habit")
    undo.add_argument("habit", help="habit name (a unique prefix or part) or id")
    undo.set_defaults(handler=cmd_undo)
    return parser


def main(argv=None, *, transport=urllib_transport, config_dir=None, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    if args.command is None and (not sys.stdin.isatty() or not out.isatty()):
        print(GET_STARTED, file=out)
        return 0
    client = Client(SessionStore(config_dir or default_config_dir()), transport=transport)
    try:
        args.handler(client, args, out)
    except CLIError as error:
        print(f"momentum: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
