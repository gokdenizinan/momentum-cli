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
import csv
import fcntl
import getpass
import json
import os
import platform
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://momentumgrids.com"
API_PREFIX = "/api/v1/mobile/"
VERSION = "1.2.1"
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


def urllib_transport(method, url, headers, body, *, timeout=20):
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
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
        self.lock_depth = 0
        self.lock_owner = None

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
    def locked(self, *, blocking=True):
        owner = threading.get_ident()
        if self.lock_depth and self.lock_owner == owner:
            self.lock_depth += 1
            try:
                yield True
            finally:
                self.lock_depth -= 1
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.directory / "session.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            self.lock_depth = 1
            self.lock_owner = owner
            try:
                yield True
            finally:
                self.lock_depth = 0
                self.lock_owner = None
                fcntl.flock(handle, fcntl.LOCK_UN)


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
        self.cache_response(method, path, query, payload)
        return payload

    def cache_response(self, method, path, query, payload):
        # Cache writes must never make an otherwise successful command fail.
        with contextlib.suppress(Exception):
            if method == "GET" and path == "automations/habits/":
                self.habits = payload["habits"]
                update_prompt_cache(self, habits=self.habits)
            elif path == "focus/sessions/" and method == "GET" and (query or {}).get("status") == "active":
                update_prompt_cache(self, timer=payload["sessions"][0] if payload["sessions"] else None)
            elif path.startswith("focus/sessions/") and method == "POST" and isinstance(payload, dict):
                if "status" in payload:
                    update_prompt_cache(self, timer=payload if payload["status"] in ("running", "paused") else None)
                elif path.endswith("/discard/"):
                    update_prompt_cache(self, timer=None)

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
            clear_prompt_cache(self.store)

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
        if saved.get("session_id") != str(payload["session"]["id"]):
            clear_prompt_cache(self.store)
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
                notify_done(session)
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


def wait_for_enter(seconds):
    """Return True as soon as Enter is pressed, or False after `seconds`."""
    started = time.monotonic()
    with contextlib.suppress(OSError, ValueError):
        if select.select([sys.stdin], [], [], seconds)[0] and sys.stdin.readline():
            return True
    # Timed out, or stdin is closed or unselectable: finish the wait unread.
    time.sleep(max(0, seconds - (time.monotonic() - started)))
    return False


def open_page(url):
    with contextlib.suppress(Exception):
        webbrowser.open(url)


def login_in_browser(client, out):
    dialogs = []
    interactive = sys.stdin.isatty()
    page = {}

    def announce(start):
        code, url = start["user_code"], start["verification_uri"]
        page["url"] = url
        print(f"Your code: {code}", file=out)
        print(f"Approve this terminal at {url} (sign in there if asked).", file=out)
        if interactive:
            print("Press Enter to open the page in your browser.", file=out)
        elif sys.platform == "darwin":
            with contextlib.suppress(OSError):
                dialogs.append(show_code_dialog(code))
        print("Waiting for approval… (Ctrl-C to cancel)", file=out)
        out.flush()
        if not interactive:
            open_page(url)

    def wait(seconds):
        # Keep polling while Enter is optional: people often open the link
        # themselves, and their approval must still reach this terminal.
        deadline = time.monotonic() + seconds
        while wait_for_enter(max(0, deadline - time.monotonic())):
            open_page(page["url"])

    try:
        client.login_in_browser(announce, sleep=wait if interactive else None)
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
    if args.json:
        print_json({"tags": shown}, out)
        return
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
    result = client.call("POST", f"focus/sessions/{session['id']}/{action}/")
    if action == "discard":
        with contextlib.suppress(Exception):
            update_prompt_cache(client, timer=None)
    return result


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
    if args.json:
        print_json({"session": session}, out)
    elif session is None:
        print("No timer is running.", file=out)
    elif args.watch:
        watch(client, session, out)
    else:
        print_lines([(timer_line(session), "accent")], args, out)


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
    if args.json:
        print_json({"habits": habits}, out)
        return
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
    if hasattr(client, "habits"):
        with contextlib.suppress(Exception):
            update_prompt_cache(client, habits=client.habits)


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


PALETTES = {
    "dark": {"normal": ("EFE6DA", 223), "secondary": ("A08C78", 137),
             "accent": ("F0913A", 214), "gold": ("E9B949", 179),
             "partial": ("B8692A", 130), "missed": ("7A6552", 95),
             "frozen": ("9CC0D3", 152), "warning": ("E3826A", 173),
             "rest": ("6FC7B4", 79), "key_bg": ("3A2C20", 237)},
    "light": {"normal": ("2A1D12", 235), "secondary": ("6F5A47", 58),
              "accent": ("A84E0C", 130), "gold": ("7F5700", 94),
              "partial": ("E39D52", 179), "missed": ("9C8874", 138),
              "frozen": ("2C6A8A", 24), "warning": ("A63E26", 124),
              "rest": ("17705F", 29), "key_bg": ("D7D7AF", 188)},
}
DAY_STYLES = {"d": "gold", "p": "partial", "m": "missed", "f": "frozen",
              "o": "secondary", "-": "accent"}


def theme_name():
    theme = os.environ.get("MOMENTUM_THEME")
    if theme in PALETTES:
        return theme
    return "light" if os.environ.get("COLORFGBG", "").split(";")[-1] in ("7", "15") else "dark"


def use_ascii(out=None):
    if os.environ.get("MOMENTUM_ASCII") == "1":
        return True
    try:
        "■".encode(getattr(out or sys.stdout, "encoding", None) or "utf-8")
        return False
    except (UnicodeError, LookupError):
        return True


class StyledText(str):
    """A plain string with optional (cell offset, text, style) drawing spans."""

    def __new__(cls, parts):
        parts = [(clip(text, 10000), style) for text, style in parts]
        obj = super().__new__(cls, "".join(text for text, _ in parts))
        obj.spans = []
        offset = 0
        for text, style in parts:
            obj.spans.extend((offset + x, fragment, color) for x, fragment, color in getattr(text, "spans", []))
            if style and text:
                obj.spans.append((offset, text, style))
            offset += display_width(text)
        return obj


def day_glyph(code, *, ascii=False, monochrome=False):
    glyphs = dict(zip("dpmfo-", "#+o*.o")) if ascii else dict(zip("dpmfo-", "■■□❄·□"))
    # The prescribed colour glyph for partial is also a square. Give partial
    # its own mark when colour cannot distinguish it from done.
    if monochrome and not ascii:
        glyphs["p"] = "+"
    return glyphs.get(code, " ")


def history_strip(habit, *, ascii=False, monochrome=False, days=14):
    return StyledText([(day_glyph(code, ascii=ascii, monochrome=monochrome), DAY_STYLES.get(code))
                       for code in (habit.get("recent_days") or "")[-days:]])


def history_counts(habit):
    history = (habit.get("recent_days") or "")[-112:]
    return history.count("d"), sum(code in "dpm" for code in history), history.count("f")


def eligible_habits(habits):
    return [h for h in habits if h["today"]["completed"] or
            (h["today"]["scheduled"] and not h["today"]["frozen"])]


def today_counts(habits):
    eligible = eligible_habits(habits)
    return sum(bool(h["today"]["completed"]) for h in eligible), len(eligible)


def sparkline(habits, *, ascii=False):
    levels = "_.-=#" if ascii else "▁▂▃▄▅▆▇█"
    totals = []
    for offset in range(9, 0, -1):
        codes = [(h.get("recent_days") or "")[-offset:-offset + 1 or None] for h in habits]
        totals.append((codes.count("d"), sum(code in ("d", "p", "m", "-") for code in codes)))
    cells = []
    for done, total in totals:
        if not total:
            cells.append(" ")
            continue
        level = int(done / total * (4 if ascii else 8) + .5) - (0 if ascii else 1)
        cells.append(levels[min(len(levels) - 1, max(0, level))])
    done, total = map(sum, zip(*totals))
    return "".join(cells), int(done / total * 100 + .5) if total else 0


def today_mark(habit, *, ascii=False):
    today = habit["today"]
    if today["completed"]:
        return ("#" if ascii else "✓"), "gold"
    if today["frozen"]:
        return ("*" if ascii else "❄"), "frozen"
    if not today["scheduled"]:
        return ("." if ascii else "·"), "secondary"
    if Decimal(today.get("value") or "0"):
        return "+", "partial"
    return ("o" if ascii else "○"), "accent"


def grid_lines(habit, today, *, ascii=False, monochrome=False):
    """The 112 local dates occupy 17 Monday-aligned week columns."""
    if isinstance(today, datetime):
        today = today.date()
    first = today - timedelta(days=111)
    monday = first - timedelta(days=first.weekday())
    history = (habit.get("recent_days") or "")[-112:].rjust(112)
    done, eligible, frozen = history_counts(habit)
    sep = " | " if ascii else " · "
    heading = sep.join([habit["name"], "last 16 weeks", f"{done} of {eligible} days", f"{frozen} frozen"])
    labels = list(" " * 39)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    for offset in range(112):
        day = first + timedelta(days=offset)
        if day.day == 1:
            column = 5 + (day - monday).days // 7 * 2
            for index, char in enumerate(months[day.month - 1]):
                if column + index < len(labels):
                    labels[column + index] = char
    lines = [(heading, "title"), ("".join(labels), "secondary")]
    for weekday, label in enumerate(("Mon", "", "Wed", "", "Fri", "", "Sun")):
        parts = [(f" {label:3} ", "secondary")]
        for week in range(17):
            day = monday + timedelta(days=week * 7 + weekday)
            code = history[(day - first).days] if first <= day <= today else " "
            parts.append((day_glyph(code, ascii=ascii, monochrome=monochrome) + " ", DAY_STYLES.get(code)))
        lines.append((StyledText(parts), "normal"))
    return lines


def ansi_enabled(out, color="auto"):
    if color == "never" or "NO_COLOR" in os.environ:
        return False
    return color == "always" or (out.isatty() and os.environ.get("TERM") != "dumb")


def ansi_style(style):
    palette = PALETTES[theme_name()]
    if style == "tab_active":
        return "\033[1;4m" + ansi_style("accent")
    hex_color, index = palette.get(style, palette["normal"])
    if style.startswith("group_"):
        indexes = GROUP_LIGHT_INDEXES if theme_name() == "light" else GROUP_INDEXES
        index = indexes.get(style[6:], 103)
        level = index - 16
        components = (level // 36, level // 6 % 6, level % 6)
        hex_color = "".join(f"{(0, 95, 135, 175, 215, 255)[c]:02x}" for c in components)
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        rgb = ";".join(str(int(hex_color[i:i + 2], 16)) for i in (0, 2, 4))
        return f"\033[38;2;{rgb}m"
    return f"\033[38;5;{index}m"


def print_lines(lines, args, out):
    colored = ansi_enabled(out, getattr(args, "color", "auto"))
    for line, style in lines:
        line = clip(line, 10000)  # sanitize control characters from user names
        if use_ascii(out):
            line = ascii_line(line)
        if colored:
            # Spans use display-cell offsets, including wide habit names.
            parts, cursor = [], 0
            for offset, text, span_style in getattr(line, "spans", []):
                parts.append(ansi_style(style) + cell_slice(line, cursor, offset))
                parts.append(ansi_style(span_style) + text)
                cursor = offset + display_width(text)
            parts.append(ansi_style(style) + cell_slice(line, cursor, display_width(line)))
            line = "".join(parts) + "\033[0m"
        print(line, file=out)


def print_json(payload, out):
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=out)


def cmd_today(client, args, out):
    habits = today_habits(client)
    done, total = today_counts(habits)
    if args.json:
        print_json({"done": done, "total": total, "habits": habits}, out)
        return
    state = UIState(habits=eligible_habits(habits), ascii=use_ascii(out),
                    monochrome=not ansi_enabled(out, args.color))
    dots = ("#" * done + "o" * (total - done)) if state.ascii else ("●" * done + "○" * (total - done))
    lines = [(f" {datetime.now().strftime('%a %d %b')} · {done} of {total} done  {dots}", "title")]
    lines += habit_rows(state, 99, len(state.habits), selected_detail=False, selection_marker=False)
    excluded = [h for h in habits if h not in state.habits]
    if excluded:
        lines.append(("   + " + " · ".join(f"{h['name']} ({habit_detail(h)})" for h in excluded), "secondary"))
    print_lines(lines, args, out)


def cmd_grid(client, args, out):
    habit = pick(today_habits(client), args.habit, "habit")
    if args.json:
        print_json({"habit": habit}, out)
    else:
        print_lines(grid_lines(habit, datetime.now().date(), ascii=use_ascii(out),
                               monochrome=not ansi_enabled(out, args.color)), args, out)


def fetch_year(client, year):
    """Dashboard pages are explicit reads, never part of the regular poll."""
    result, habits, page = None, [], 1
    while True:
        payload = client.call("GET", "dashboard/", query={"year": year, "page": page, "page_size": 20})
        result = result or dict(payload)
        habits.extend(payload["habits"])
        if not payload["pagination"]["has_next"]:
            break
        page += 1
    result["habits"] = habits
    result["streaks"] = ({str(h["id"]): h.get("streak", 0) for h in today_habits(client)}
                         if year == datetime.now().date().year else {})
    return result


def scheduled_on(habit, day):
    # Habit.weekdays_on starts with daily scheduling before the first rule.
    history = habit.get("schedule_history")
    weekdays = list(range(1, 8)) if history is not None else habit.get("scheduled_weekdays", list(range(1, 8)))
    for rule in history or []:
        if rule["effective_from"] > day.isoformat():
            break
        weekdays = rule["weekdays"]
    return day.isoweekday() in weekdays


def year_states(habit, year, today):
    entries = {e["date"]: e for e in habit["entries"]}
    frozen = set(habit["frozen_dates"])
    states, day = {}, date(year, 1, 1)
    while day.year == year:
        key = day.isoformat()
        entry = entries.get(key) or {}
        if day > today:
            code = " "
        elif entry.get("completed"):
            code = "d"
        elif key < habit["start_date"]:
            code = "o"
        elif Decimal(entry.get("value") or "0"):
            code = "p"
        elif key in frozen:
            code = "f"
        elif not scheduled_on(habit, day):
            code = "o"
        else:
            code = "-" if day == today else "m"
        states[day] = code
        if day == date(year, 12, 31):
            break
        day += timedelta(days=1)
    return states


def year_stats(habit, states):
    best = run = 0
    months = []
    for code in states.values():
        if code == "d":
            run += 1
            best = max(best, run)
        elif code not in ("f", "o", " "):
            run = 0
    for month in range(1, 13):
        dates = [(d, s) for d, s in states.items() if d.month == month]
        done = sum(s == "d" for _, s in dates)
        frozen = set(habit["frozen_dates"])
        eligible = sum(s == "d" or (s != " " and d.isoformat() >= habit["start_date"]
                                   and scheduled_on(habit, d) and d.isoformat() not in frozen) for d, s in dates)
        months.append({"month": month, "done": done, "eligible": eligible,
                       "rate": round(100 * done / eligible) if eligible else 0})
    # The server's year_summary is authoritative for eligibility (including
    # completed off-schedule days and historical schedule changes).
    summary = habit["year_summary"]
    done, eligible = summary["completed_days"], summary["eligible_days"]
    return {"done": done, "eligible": eligible, "rate": round(100 * done / eligible) if eligible else 0,
            "best_run": best, "frozen": list(states.values()).count("f"), "months": months}


def half_year_lines(states, year, half, *, cursor=None, ascii=False, monochrome=False):
    first, last = date(year, half * 6 + 1, 1), date(year, half * 6 + 6, 30 if half == 0 else 31)
    monday = first - timedelta(days=first.weekday())
    weeks = (last - monday).days // 7 + 1
    labels = list(" " * (4 + weeks * 2 + 2))
    for month in range(first.month, last.month + 1):
        start = date(year, month, 1)
        pos = 4 + (start - monday).days // 7 * 2
        labels[pos:pos + 3] = list(start.strftime("%b"))
    rows = [("".join(labels).rstrip(), "secondary")]
    for weekday in range(7):
        parts = [(date(2026, 11, 16 + weekday).strftime("%a") + " ", "secondary")]
        for week in range(weeks):
            day = monday + timedelta(days=week * 7 + weekday)
            code = states.get(day, " ") if first <= day <= last else " "
            glyph = day_glyph(code, ascii=ascii, monochrome=monochrome)
            parts.append((glyph, "cursor" if day == cursor else DAY_STYLES.get(code)))
            if week < weeks - 1:
                parts.append((" ", None))
        rows.append((StyledText(parts), "normal"))
    return rows


def year_stat_lines(stats, streak=None, *, ascii=False):
    lines = [(f" {stats['done']} of {stats['eligible']} eligible days", "title"),
             (f" {stats['rate']}% completion rate", "gold")]
    if streak is not None:
        lines.append((f" Streak {streak}d", "gold"))
    lines += [(f" Best run {stats['best_run']}d", "normal"), (f" Frozen {stats['frozen']}", "frozen"), ("", "normal")]
    for month in stats["months"]:
        if not month["eligible"]:
            continue  # months that haven't started (or had nothing scheduled)
        label = date(2000, month["month"], 1).strftime("%b")
        bar = progress_bar(month["done"], max(1, month["eligible"]), 10, ascii=ascii)
        lines.append((f" {label} {bar} {month['rate']:3}%", "secondary"))
    return lines


def year_lines(habit, payload, today, *, cursor=None, width=100, height=32, ascii=False, monochrome=False):
    year = payload["year"]
    states = year_states(habit, year, today)
    stats = year_stats(habit, states)
    streak = payload.get("streaks", {}).get(str(habit["id"])) if year == today.year else None
    halves = [0, 1] if width >= 60 else [(cursor.month - 1) // 6 if cursor else (today.month - 1) // 6]
    grids = []
    for half in halves:
        if grids:
            grids.append(("", "normal"))
        grids += half_year_lines(states, year, half, cursor=cursor, ascii=ascii, monochrome=monochrome)
    if width >= 96:
        panel = year_stat_lines(stats, streak, ascii=ascii)
        lines = []
        for index in range(max(len(grids), len(panel))):
            left = grids[index][0] if index < len(grids) else ""
            right, style = panel[index] if index < len(panel) else ("", "normal")
            lines.append((StyledText([(left, None), (" " * max(1, 61 - display_width(left)), None), (right, style)]), "normal"))
        return lines
    # A 40-column terminal pans to the cursor's week; one-shot output uses a
    # full-width grid so no part of the year is lost to clipping.
    if cursor and width < 60:
        half = halves[0]
        first = date(year, half * 6 + 1, 1)
        monday = first - timedelta(days=first.weekday())
        visible_weeks = max(1, (width - 5) // 2)
        cursor_week = (cursor - monday).days // 7
        start_week = max(0, cursor_week - visible_weeks + 1)
        panned = []
        for text, style in grids:
            left = cell_slice(text, 0, 4)
            # Preserve day styles and cursor spans through horizontal panning.
            sliced = str.__new__(StyledText, left + cell_slice(text, 4 + start_week * 2, 4 + (start_week + visible_weeks) * 2))
            sliced.spans = [(4 + offset - (4 + start_week * 2), fragment, color)
                            for offset, fragment, color in getattr(text, "spans", [])
                            if 4 + start_week * 2 <= offset < 4 + (start_week + visible_weeks) * 2]
            panned.append((sliced, style))
        grids = panned
    summary = f" {stats['done']}/{stats['eligible']} done · {stats['rate']}%"
    detail = (f" Streak {streak} · " if streak is not None else " ") + f"best {stats['best_run']} · frozen {stats['frozen']}"
    return grids + [(summary, "gold"), (detail, "secondary")]


def set_history_day(client, habit, day, *, value=None):
    entry = next((e for e in habit["entries"] if e["date"] == day.isoformat()), {})
    body = {"completed": not entry.get("completed", False)} if habit["habit_type"] == "binary" else {"value": str(value)}
    return client.call("PUT", f"habits/{habit['id']}/entries/{day.isoformat()}/", body)


def cmd_year(client, args, out):
    year = args.year if args.year is not None else datetime.now().year
    payload = fetch_year(client, year)
    habit = pick(payload["habits"], args.habit, "habit")
    today = datetime.now().date()
    stats = year_stats(habit, year_states(habit, year, today))
    if args.json:
        print_json({"year": year, "habit": habit, "stats": stats,
                    "streak": payload["streaks"].get(str(habit["id"]))}, out)
    else:
        print_lines([(f"{habit['name']} · {year}", "title")] +
                    year_lines(habit, payload, today, ascii=use_ascii(out), monochrome=not ansi_enabled(out, args.color)), args, out)


def focus_history(client, today, days):
    sessions = []
    cutoff = today - timedelta(days=days - 1)
    for page in range(1, 21):
        payload = client.call("GET", "focus/sessions/", query={"status": "completed", "page_size": 100, "page": page})
        older = False
        for session in payload["sessions"]:
            ended = parse_time(session["ended_at"]).astimezone().date()
            if ended < cutoff:
                older = True
                break
            if ended <= today:
                sessions.append(session)
        if older or not payload["pagination"]["has_next"]:
            return sessions, False
    return sessions, True


def focus_summary(sessions, today, days=7, *, timer=None, elapsed=0):
    first = today - timedelta(days=days - 1)
    before = first - timedelta(days=days)
    daily = {first + timedelta(days=i): 0 for i in range(days)}
    tags, previous = {}, 0
    values = [(s, s["duration_seconds"], parse_time(s["ended_at"]).astimezone().date()) for s in sessions]
    if timer:
        values.append((timer, elapsed, today))
    for session, seconds, day in values:
        if before <= day < first:
            previous += seconds
        elif day in daily:
            daily[day] += seconds
            association = session["association"]
            name = association["name"] if association["kind"] != "none" else "No tag"
            tags[name] = tags.get(name, 0) + seconds
    total = sum(daily.values())
    return {"days": days, "total_seconds": total, "previous_seconds": previous, "delta_seconds": total - previous,
            "daily": [{"date": d.isoformat(), "seconds": n} for d, n in daily.items()],
            "tags": [{"name": name, "seconds": seconds, "percent": round(100 * seconds / total) if total else 0}
                     for name, seconds in sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))]}


def short_duration(seconds):
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02}" if hours else f"{minutes}m"


def focus_buckets(summary):
    days = summary["daily"]
    if summary["days"] == 7:
        return [(date.fromisoformat(d["date"]).strftime("%a"), d["seconds"], i == 6) for i, d in enumerate(days)]
    buckets = {}
    for item in days:
        day = date.fromisoformat(item["date"])
        start = day - timedelta(days=day.weekday()) if summary["days"] == 30 else day.replace(day=1)
        buckets[start] = buckets.get(start, 0) + item["seconds"]
    latest = max(buckets)
    return [(d.strftime("%d%b" if summary["days"] == 30 else "%b"), seconds, d == latest) for d, seconds in buckets.items()]


def focus_chart(summary, width, *, rows=8, ascii=False):
    buckets = focus_buckets(summary)
    # Seven five-cell bars still fit at 40 columns by shedding their gaps.
    # Longer windows pan to the latest buckets if even gapless bars won't fit.
    gap = 3 if len(buckets) * 8 - 3 <= width else 1 if len(buckets) * 6 - 1 <= width else 0
    buckets = buckets[-max(1, (width + gap) // (5 + gap)):]
    maximum = max(1, max(seconds for _, seconds, _ in buckets))
    average = sum(item["seconds"] for item in summary["daily"]) / len(focus_buckets(summary))
    avg_row = min(rows - 1, rows - 1 - int(average / maximum * (rows - 1)))
    lines = [(StyledText([(clip(short_duration(n), 5).center(5) + " " * gap, "accent" if current else "secondary")
                         for _, n, current in buckets]), "normal")]
    for row in range(rows):
        parts = []
        for _, seconds, current in buckets:
            eighths = round(seconds / maximum * rows * 8)
            level = max(0, min(8, eighths - (rows - row - 1) * 8))
            glyph = ("#" if ascii else "▁▂▃▄▅▆▇█"[level - 1]) if level else ("-" if row == avg_row else " ")
            # The dashed average line runs through empty space only, never through a bar.
            cell = glyph * 5
            parts.append((cell + ("-" if row == avg_row else " ") * gap, "accent" if current else "secondary"))
        lines.append((StyledText(parts), "normal"))
    lines.append((StyledText([(name.center(5) + " " * gap, "accent" if current else "secondary")
                              for name, _, current in buckets]), "normal"))
    lines.append((f" avg {short_duration(average)} per {'day' if summary['days'] == 7 else 'week' if summary['days'] == 30 else 'month'}", "secondary"))
    return lines


def write_focus_csv(sessions, out):
    writer = csv.writer(out)
    writer.writerow(("start", "end", "seconds", "tag_or_habit", "name", "status"))
    for session in sessions:
        association = session["association"]
        writer.writerow((session["started_at"], session["ended_at"], session["duration_seconds"],
                         association["name"] if association["kind"] != "none" else "", session["name"], session["status"]))


def export_focus(sessions, output, out=None):
    if output is None:
        write_focus_csv(sessions, out or sys.stdout)
        return
    # A new file avoids following symlinks or silently replacing another export.
    try:
        descriptor = os.open(os.path.expanduser(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), 0o600)
            write_focus_csv(sessions, handle)
    except OSError as error:
        raise CLIError(f"Could not export to {output}: {error}") from error


def positive_days(raw):
    value = int(raw)
    if value < 1 or value > 36500:
        raise argparse.ArgumentTypeError("Choose 1 to 36500 days.")
    return value


def cmd_export(client, args, out):
    sessions, capped = focus_history(client, datetime.now().date(), args.days)
    export_focus(sessions, args.output, out)
    if capped:
        print("Export capped at 20 pages (2000 sessions).", file=sys.stderr)


def cmd_week(client, args, out):
    today = datetime.now().date()
    sessions, capped = focus_history(client, today, 14)
    timer = active_timer(client)
    summary = focus_summary(sessions, today, timer=timer, elapsed=timer["elapsed_seconds"] if timer else 0)
    if args.json:
        print_json(dict(summary, capped=capped), out)
    else:
        delta = summary["delta_seconds"]
        lines = [(f"LAST 7 DAYS {short_duration(summary['total_seconds'])} focused · {'▲' if delta >= 0 else '▼'} {short_duration(abs(delta))} vs the 7 days before", "title")]
        lines += focus_chart(summary, 100, ascii=use_ascii(out)) + [("BY TAG", "title")]
        lines += [(f" {t['name']}  {progress_bar(t['seconds'], max(1, summary['total_seconds']), 24, ascii=use_ascii(out))}  {short_duration(t['seconds'])}  {t['percent']}%", "normal") for t in summary["tags"]]
        if capped:
            lines.append(("History capped at 20 pages (2000 sessions).", "warning"))
        print_lines(lines, args, out)


# Nearest xterm colours to the existing Kairos identities in static/css/kairos.css.
# Light green/teal and coral reserve adjacent choices to avoid collisions with
# another group and the Momentum orange (130).
GROUP_INDEXES = {"blue": 111, "purple": 183, "green": 115, "rose": 211, "amber": 180,
                 "teal": 116, "indigo": 147, "coral": 216, "olive": 150, "slate": 146}
GROUP_LIGHT_INDEXES = {"blue": 25, "purple": 97, "green": 29, "rose": 131, "amber": 94,
                       "teal": 23, "indigo": 61, "coral": 95, "olive": 58, "slate": 60}


def task_sections(payload):
    day = date.fromisoformat(payload["day"])
    tomorrow = (day + timedelta(days=1)).isoformat()
    sections = {"TODAY": [], "TOMORROW": [], "LATER": []}
    for task in payload["tasks"]:
        if task["state"] == "done":
            completed = parse_time(task["completed_at"])
            if completed and completed.astimezone().date() == day:
                sections["TODAY"].append(task)
        elif task["state"] == "open":
            planned = task["planned_for"]
            section = "TODAY" if planned and planned <= payload["day"] else "TOMORROW" if planned == tomorrow else "LATER"
            sections[section].append(task)
    for tasks in sections.values():
        tasks.sort(key=lambda t: (t["state"] == "done", t["created_at"], t["id"]))
    return sections


def change_task(client, task, action, **fields):
    return client.call("POST", f"kairos/{task['id']}/", dict(fields, action=action, version=task["version"]))


def add_task(client, title, destination="today", *, identity=None):
    task = client.call("POST", "kairos/", {"id": identity or str(uuid.uuid4()), "title": title})
    # CaptureSerializer creates an unplanned task. Planning is a versioned change.
    return task if destination == "later" else change_task(client, task, destination)


def cmd_tasks(client, args, out):
    payload = client.call("GET", "kairos/")
    if args.json:
        print_json(payload, out)
        return
    groups = {g["id"]: g for g in payload["groups"]}
    lines = []
    for section, tasks in task_sections(payload).items():
        lines.append((section, "title"))
        for task in tasks:
            group = groups.get(task.get("group_id"), {})
            mark = "✓" if task["state"] == "done" else "○"
            reminder = parse_time(task.get("remind_at"))
            detail = (" · " + group["name"]) if group else ""
            detail += (" · " + reminder.astimezone().strftime("%d %b %H:%M")) if reminder else ""
            lines.append((f" {mark} {task['title']}{detail}", "secondary" if task["state"] == "done" else "normal"))
    print_lines(lines, args, out)


def cmd_task(client, args, out):
    if args.task_command == "add":
        try:
            task = add_task(client, args.title, "tomorrow" if args.tomorrow else "later" if args.later else "today")
        except APIError as error:
            if error.status == 409:
                client.call("GET", "kairos/")
            raise
        print(f"Added {task['title']}.", file=out)
    else:
        payload = client.call("GET", "kairos/")
        tasks = [dict(t, name=t["title"]) for t in payload["tasks"] if t["state"] == "open"]
        task = pick(tasks, args.query, "task")
        try:
            change_task(client, task, "done")
        except APIError as error:
            if error.status == 409:
                client.call("GET", "kairos/")
            raise
        print(f"Done {task['title']}.", file=out)


_MISSING = object()
TIMER_CACHE_KEYS = ("id", "name", "status", "association", "elapsed_seconds", "target_duration_seconds")


def clear_prompt_cache(store):
    with contextlib.suppress(OSError):
        (store.directory / "prompt-cache.json").unlink()


def load_prompt_cache(client):
    try:
        saved = client.store.load()
        data = json.loads((client.store.directory / "prompt-cache.json").read_text())
        if (data.get("base_url") == client.base_url and data.get("session_id") == saved.get("session_id")
                and data.get("date") == datetime.now().date().isoformat()):
            return data
    except (OSError, ValueError, CLIError, AttributeError):
        pass
    return {}


def update_prompt_cache(client, *, timer=_MISSING, habits=None):
    store = client.store
    store.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Separate nonblocking cache lock prevents lost updates between commands.
    with open(store.directory / "prompt-cache.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        data = load_prompt_cache(client)
        now = time.time()
        if timer is not _MISSING:
            data["timer"] = {key: timer[key] for key in TIMER_CACHE_KEYS if key in timer} if timer else None
            data["timer_fetched_at"] = now
        if habits is not None:
            data["done"], data["total"] = today_counts(habits)
            data["habits_fetched_at"] = now
        data.update(base_url=client.base_url, session_id=store.load().get("session_id"),
                    date=datetime.now().date().isoformat())
        data["fetched_at"] = min(data.get("timer_fetched_at", 0), data.get("habits_fetched_at", 0))
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=store.directory, prefix=".prompt-", delete=False) as handle:
                path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle)
            os.replace(path, store.directory / "prompt-cache.json")
        finally:
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()


def prompt_payload(cache):
    session = dict(cache["timer"]) if cache.get("timer") else None
    if session and session["status"] == "running":
        session["elapsed_seconds"] += max(0, int(time.time() - cache["timer_fetched_at"]))
        if session.get("target_duration_seconds"):
            session["elapsed_seconds"] = min(session["elapsed_seconds"], session["target_duration_seconds"])
    return {"session": session, "done": cache.get("done", 0), "total": cache.get("total", 0)}


def print_prompt(cache, args, out):
    if not cache:
        return
    payload = prompt_payload(cache)
    if args.json:
        print_json(payload, out)
        return
    session = payload["session"]
    mark = ("*" if use_ascii(out) else "●") if session and session["status"] == "running" else ("||" if use_ascii(out) else "❚❚")
    timer = f"{mark} {timer_title(session)}" if session else ""
    elapsed = clock(session["elapsed_seconds"]) if session else ""
    default = "{timer} {elapsed} · {done}/{total}" if session else "{done}/{total}"
    text = (args.format or default).format(timer=timer, elapsed=elapsed, done=payload["done"], total=payload["total"])
    text = clip(text, 10000)
    if args.tmux:
        style = "accent" if session else "gold"
        tmux_color = args.color != "never" and "NO_COLOR" not in os.environ
        text = ascii_line(text) if use_ascii(out) else text
        text = text.replace("#", "##")
        if tmux_color:
            text = f"#[fg=colour{PALETTES[theme_name()][style][1]}]{text}#[default]"
        print(text, file=out)
    else:
        print_lines([(text, "accent" if session else "gold")], args, out)


def cmd_prompt(client, args, out):
    cache = {}
    try:
        client._require_session(client.store.load())
        cache = load_prompt_cache(client)
        if time.time() - cache.get("fetched_at", 0) > args.max_age:
            # Own the session lock for the entire refresh; nested token refresh
            # uses this same lock. Never wait behind another shell command.
            with client.store.locked(blocking=False) as acquired:
                if acquired:
                    deadline = time.monotonic() + 2
                    original = client.transport

                    def short_transport(method, url, headers, body):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise CLIError("Prompt refresh timed out.")
                        if url.endswith(API_PREFIX + "auth/refresh/"):
                            # The server spends the old refresh token as soon as it
                            # answers; abandoning a slow reply would sign this
                            # terminal out, so token rotation gets the usual timeout.
                            return original(method, url, headers, body)
                        if original is urllib_transport:
                            return urllib_transport(method, url, headers, body, timeout=remaining)
                        return original(method, url, headers, body)

                    client.transport = short_transport
                    try:
                        active_timer(client)
                        today_habits(client)
                    finally:
                        client.transport = original
        client._require_session(client.store.load())
        cache = load_prompt_cache(client) or cache
    except Exception:
        # A locally revoked session must not expose an old account's status.
        try:
            client._require_session(client.store.load())
            cache = load_prompt_cache(client) or cache
        except Exception:
            return
    try:
        print_prompt(cache, args, out)
    except Exception:
        # Invalid custom formats still get the ordinary cached status.
        args.format = None
        with contextlib.suppress(Exception):
            print_prompt(cache, args, out)


def notify_desktop(text, *, enabled=None):
    enabled = os.environ.get("MOMENTUM_NOTIFY") != "0" if enabled is None else enabled
    if not enabled or sys.platform != "darwin":
        return
    script = ('on run argv', 'display notification (item 1 of argv) with title "Momentum"', 'end run')
    command = ["osascript"]
    for line in script:
        command += ["-e", line]
    with contextlib.suppress(Exception):
        subprocess.run([*command, text], capture_output=True, timeout=2, check=False)


def notify_done(session, *, enabled=None):
    text = f"Time's up · {timer_title(session)} · {clock(session['target_duration_seconds'])}"
    notify_desktop(text, enabled=enabled)


def completed_today(client, today):
    sessions, page = [], 1
    while True:
        payload = client.call("GET", "focus/sessions/", query={"status": "completed", "page_size": 100, "page": page})
        older = False
        for session in payload["sessions"]:
            ended = parse_time(session["ended_at"]).astimezone().date()
            if ended < today:
                older = True
                break
            if ended == today:
                sessions.append(session)
        if older or not payload["pagination"]["has_next"]:
            return sessions
        page += 1


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
    discard_return: str | None = None
    ascii: bool = field(default_factory=use_ascii)
    monochrome: bool = field(default_factory=lambda: "NO_COLOR" in os.environ)
    completed_sessions: list = field(default_factory=list)
    completed_date: object = None
    focus_break: dict | None = None
    notify: bool = field(default_factory=lambda: os.environ.get("MOMENTUM_NOTIFY") != "0")
    tab: int = 1
    loaded_tabs: set = field(default_factory=lambda: {1})
    year: int = field(default_factory=lambda: datetime.now().year)
    years: dict = field(default_factory=dict)
    grid_selection: int = 0
    grid_cursor: date = field(default_factory=lambda: datetime.now().date())
    focus_days: int = 7
    focus_windows: dict = field(default_factory=dict)
    tasks_payload: dict | None = None
    task_selection: int = 0
    input_text: str = ""
    input_destination: int = 0
    pending_edit: tuple | None = None
    pending_task: dict | None = None
    capture_id: str | None = None


class UIApp:
    def __init__(self, client, *, color="auto"):
        self.client = client
        self.color = color
        self.state = UIState()
        self.last_refresh = -UI_POLL_SECONDS
        self.quit = False
        self.last_focus_refresh = -UI_POLL_SECONDS
        self.notified = set()

    def refresh_focus(self):
        self.last_focus_refresh = time.monotonic()
        try:
            today = datetime.now().date()
            sessions = completed_today(self.client, today)
            self.state.completed_sessions = sessions
            self.state.completed_date = today
        except CLIError as error:
            self.state.status = str(error)

    def check_done(self):
        state = self.state
        timer = state.timer
        if (timer and timer.get("target_duration_seconds") and
                ui_elapsed(state) >= timer["target_duration_seconds"] and timer["id"] not in self.notified):
            self.notified.add(timer["id"])
            notify_done(timer, enabled=state.notify)
            return True
        rest = state.focus_break
        if rest and not rest["notified"] and break_elapsed(state) >= rest["seconds"]:
            rest["notified"] = True
            if state.notify:
                notify_desktop(f"Break over · round {rest['next_round']} of 4 is next", enabled=True)
                return True
        return False

    def begin_focus_break(self, saved):
        state = self.state
        saved_round = focus_round(state)
        started = time.monotonic()
        state.timer = None
        state.timer_fetched_at = started
        state.focus_break = {
            "started": started, "seconds": break_minutes(saved_round) * 60,
            "saved_round": saved_round, "next_round": saved_round % 4 + 1,
            "association": {key: saved["association"][key] for key in ("kind", "id", "name")},
            "name": saved["name"], "target_seconds": saved.get("target_duration_seconds"),
            "notified": False,
        }
        if state.completed_date == state.date.date():
            state.completed_sessions.append(saved)
        for window in state.focus_windows.values():
            window["sessions"].insert(0, saved)
        state.status = f"Saved {timer_title(saved)}"

    def start_focus_round(self):
        state = self.state
        rest = state.focus_break
        association = rest["association"]
        target = rest["target_seconds"]
        session = start_timer(self.client,
                              tag=association if association["kind"] == "tag" else None,
                              habit=association if association["kind"] == "habit" else None,
                              name=rest["name"] or None, minutes=target / 60 if target else None)
        state.timer, state.timer_fetched_at = session, time.monotonic()
        state.focus_break = None
        state.status = f"Started round {rest['next_round']} · {timer_title(session)}"

    def refresh(self, message=None):
        """Keep the last good data on failure and throttle even failed polls."""
        state = self.state
        errors = []
        try:
            state.timer = active_timer(self.client)
            state.timer_fetched_at = time.monotonic()
            if state.timer:
                state.focus_break = None
        except CLIError as error:
            errors.append(str(error))
        try:
            if state.tab != 1:
                if state.tab == 4:
                    self.refresh_tasks()
                self.last_refresh = state.now = time.monotonic()
                state.date = datetime.now().astimezone()
                if state.overlay == "focus" and state.now - self.last_focus_refresh >= UI_POLL_SECONDS:
                    self.refresh_focus()
                if message is not None:
                    state.status = message
                if errors:
                    state.status = " · ".join(errors)
                return
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
        if state.overlay == "focus" and state.now - self.last_focus_refresh >= UI_POLL_SECONDS:
            self.refresh_focus()
        if errors:
            state.status = " · ".join(dict.fromkeys(([message] if message else []) + errors))
        elif message is not None:
            state.status = message

    def refresh_year(self, *, force=False):
        state = self.state
        if force or state.year not in state.years:
            payload = fetch_year(self.client, state.year)
            old = grid_habit(state)
            state.years[state.year] = payload
            state.grid_selection = next((i for i, h in enumerate(payload["habits"]) if old and h["id"] == old["id"]),
                                        min(state.grid_selection, max(0, len(payload["habits"]) - 1)))

    def refresh_stats(self, *, force=False):
        state = self.state
        if force or state.focus_days not in state.focus_windows:
            sessions, capped = focus_history(self.client, state.date.date(), state.focus_days * 2)
            state.focus_windows[state.focus_days] = {"sessions": sessions, "capped": capped}
            if capped:
                state.status = "History capped at 20 pages (2000 sessions)"

    def refresh_tasks(self):
        state = self.state
        selected = selected_task(state)
        state.tasks_payload = self.client.call("GET", "kairos/")
        tasks = visible_tasks(state)
        state.task_selection = next((i for i, t in enumerate(tasks) if selected and t["id"] == selected["id"]),
                                    min(state.task_selection, max(0, len(tasks) - 1)))

    def open_tab(self, tab):
        state = self.state
        state.tab, state.overlay, state.status = tab, None, ""
        state.date = datetime.now().astimezone()
        if tab == 1 and tab not in state.loaded_tabs:
            self.refresh()
        elif tab == 2:
            self.refresh_year()
        elif tab == 3:
            self.refresh_stats()
        elif tab == 4 and tab not in state.loaded_tabs:
            self.refresh_tasks()
        state.loaded_tabs.add(tab)

def display_width(text):
    """Terminal cells, including wide emoji/CJK and zero-width combining marks."""
    return sum(
        0 if unicodedata.category(char) in ("Mn", "Me", "Cf", "Cc", "Cs")
        else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


def clip(text, width):
    """A safe single line, truncated by cells rather than Python character count."""
    source = text
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cs") else c for c in str(text))
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return source if text == source else text
    result = ""
    used = 0
    for char in text:
        cells = display_width(char)
        if used + cells > width - 1:
            break
        result += char
        used += cells
    result += "…"
    if getattr(source, "spans", None):
        styled = str.__new__(StyledText, result)
        styled.spans = [(offset, cell_slice(fragment, 0, max(0, display_width(result) - 1 - offset)), style)
                        for offset, fragment, style in source.spans if offset < display_width(result) - 1]
        return styled
    return result


def cell_slice(text, start, end):
    result, offset = "", 0
    for char in text:
        size = display_width(char)
        if (size == 0 and result and start < offset <= end) or (size and start <= offset < end and offset + size <= end):
            result += char
        offset += size
    return result


def columns(left, right, width):
    right = clip(right, width)
    left = clip(left, max(0, width - display_width(right) - 2))
    return left + " " * max(0, width - display_width(left + right)) + right


def progress_bar(value, target, width=8, *, ascii=False):
    fraction = min(1, max(0, float(value or 0) / float(target))) if target else 0
    filled = int(fraction * width)
    return ("#" if ascii else "█") * filled + ("." if ascii else "░") * (width - filled)


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
        parts.append("not today")
    elif habit["habit_type"] != "numeric":
        parts.append("done" if habit["today"]["completed"] else "to do")
    return " · ".join(parts)


def habit_layout(habits, width, *, ascii=False):
    labels = [clip(f"{h['emoji']} {h['name']}".strip(), 10000) for h in habits]
    details = [habit_detail(h) for h in habits]
    if ascii:
        labels = [ascii_line(label) for label in labels]
        details = [ascii_line(detail) for detail in details]
    strip_width = 16 if width >= 74 else 0
    bar_width = 10 if width >= 55 else 0
    streak_width = 7 if width >= 48 else 0
    fixed = 4 + strip_width + bar_width + streak_width
    detail_width = min(max(map(display_width, details), default=5), max(0, width - fixed - 8))
    name_width = max(1, min(max(map(display_width, labels), default=8), width - detail_width - fixed - 2))
    if strip_width:
        name_width = max(name_width, min(20, max(1, width - detail_width - fixed - 2)))
    return labels, details, name_width, detail_width, strip_width, bar_width, streak_width


def habit_rows(state, width, count, *, selected_detail=True, selection_marker=True):
    if count <= 0:
        return []
    if not state.habits:
        return [(" No habits yet. Add habits in the app.", "secondary")][:count]
    labels, details, name_width, detail_width, strip_width, bar_width, streak_width = habit_layout(state.habits, width, ascii=state.ascii)
    selection = min(max(0, state.selection), len(state.habits) - 1)
    show_detail = selected_detail and count >= 2
    capacity = max(1, count - int(show_detail))
    start = max(0, selection - capacity + 1)
    lines = []
    for index in range(start, min(len(state.habits), start + capacity)):
        habit = state.habits[index]
        selected = index == selection
        name = clip(labels[index], name_width)
        mark, mark_style = today_mark(habit, ascii=state.ascii)
        label = ((">" if state.ascii else "›") if selected and selection_marker else " ") + mark + " " + name
        if selected and selection_marker:
            parts = [(label, "selected")]
        else:
            parts = [(label[:1], "accent"), (mark + " ", mark_style), (name, None)]
        parts.append((" " * (name_width - display_width(name) + 2), None))
        if strip_width:
            strip = history_strip(habit, ascii=state.ascii, monochrome=state.monochrome)
            parts += [(strip, None), (" " * (16 - display_width(strip)), None)]
        if streak_width:
            streak = habit.get("streak") or 0
            parts.append(((f"{streak}d" if streak else "-" if state.ascii else "–").rjust(5) + "  ", "gold" if streak >= 7 else "secondary"))
        parts.append((clip(details[index], detail_width), mark_style))
        if bar_width and habit["habit_type"] == "numeric":
            parts.append((" " * (detail_width - display_width(clip(details[index], detail_width)) + 2) +
                          progress_bar(habit["today"]["value"], habit["target_value"], ascii=state.ascii), mark_style))
        line = StyledText(parts)
        style = "secondary" if habit["today"]["frozen"] or not habit["today"]["scheduled"] else "normal"
        lines.append((line, style))
        if selected and show_detail:
            done, eligible, _ = history_counts(habit)
            lines.append((f"    {done} of {eligible} days in the last 16 weeks · g opens the grid", "secondary"))
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
    "1 Today · 2 Grid · 3 Focus · 4 Tasks",
    "Esc  Back to Today (cancel a prompt)",
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
    "g  Selected habit's 16-week grid",
    "f  Focus clock or break · Esc/f back",
    "Focus Space/p  Pause or resume · s saves and starts a break",
    "Break Space/p  Start next round · d ends the break",
    "n  Toggle notifications in focus mode",
    "r  Refresh from the server",
    "?  Show help; any key closes it",
    "q/Ctrl-C  Quit; timer keeps running",
]


PIXEL_FONT = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    ":": (".", "#", ".", "#", "."),
}


def big_clock(text, width, height, *, ascii=False):
    pixels = [".".join(PIXEL_FONT[char][row] for char in text) for row in range(5)]
    for horizontal, vertical in ((8, 4), (6, 3), (4, 2), (2, 1)):
        if len(pixels[0]) * horizontal <= width and 5 * vertical <= height:
            rows = []
            for row in pixels:
                line = "".join(("#" if ascii else "█") * horizontal if pixel == "#" else " " * horizontal for pixel in row)
                rows.extend([line] * vertical)
            return rows
    return [text]


def focus_round(state):
    rounds_done = len(state.completed_sessions) if state.completed_date == state.date.date() else 0
    return rounds_done % 4 + 1


def break_minutes(round_number):
    return 15 if round_number == 4 else 5


def break_elapsed(state):
    rest = state.focus_break
    return min(rest["seconds"], max(0, int(state.now - rest["started"])))


def focus_totals(state):
    sessions = state.completed_sessions if state.completed_date == state.date.date() else []
    sessions = [(s, s["duration_seconds"]) for s in sessions]
    if state.timer:
        sessions.append((state.timer, ui_elapsed(state)))
    tags = {}
    for session, seconds in sessions:
        association = session["association"]
        if association["kind"] == "tag":
            name = association["name"]
            tags[name] = tags.get(name, 0) + seconds
    return sum(seconds for _, seconds in sessions), len(sessions), sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))[:3]


def focus_today_lines(state, width, current_tag):
    total, count, tags = focus_totals(state)
    stats = [(StyledText([(" TODAY  ", "secondary_bold"), (short_duration(total), "normal_bold"),
                         (f" focused · {count} session{'' if count == 1 else 's'}", "secondary")]), "normal")]
    if not tags:
        return stats
    longest = max(1, tags[0][1])
    duration_width = max(5, max(len(short_duration(seconds)) for _, seconds in tags))
    name_width = min(12, max(0, width - duration_width - 7))
    bar_width = min(30, max(0, width - name_width - duration_width - 7))
    for tag, seconds in sorted(tags, key=lambda item: item[0] != current_tag):
        name = clip(ascii_line(tag) if state.ascii else tag, name_width)
        bar = ("#" if state.ascii else "█") * round(seconds / longest * bar_width)
        duration = short_duration(seconds).rjust(duration_width)
        parts = [("   " + name + " " * (name_width - display_width(name) + 2), "normal"),
                 (bar, "accent" if tag == current_tag else "partial"),
                 (" " * (bar_width - len(bar) + 2) + duration, "normal")]
        stats.append((StyledText(parts), "normal"))
    return stats


def focus_footer(state, width):
    space = "Space" if state.ascii else "␣"
    notify = f"notify: {'on' if state.notify else 'off'}"
    quit_label = "quit"
    if state.focus_break:
        hints = [(space, f"start round {state.focus_break['next_round']}"), ("d", "end break"), ("n", notify)]
    elif state.timer:
        action = "pause" if state.timer["status"] == "running" else "resume"
        hints = [(space, action), ("s", "save"), ("d", "discard"), ("n", notify)]
        if width >= len(" q  quit, timer keeps running"):
            quit_label = "quit, timer keeps running"
    else:
        hints = [("n", notify)]
    hints += [("esc", "back"), ("q", quit_label)]
    rows, parts, used = [], [], 0
    for key, label in hints:
        keycap, label = f" {key} ", f" {label}"
        pair_width = display_width(keycap + label)
        if pair_width > width:
            continue
        if used and used + pair_width + 2 > width:
            rows.append((StyledText(parts), "normal"))
            parts, used = [], 0
        if used:
            parts.append(("  ", None))
            used += 2
        parts += [(keycap, "key"), (label, "secondary")]
        used += pair_width
    if parts:
        rows.append((StyledText(parts), "normal"))
    return rows


def focus_lines(state, width, height):
    header = tab_top(state, width, active_tab=3)
    rule = ("─" * width, "secondary")
    footer = focus_footer(state, width)
    timer, rest = state.timer, state.focus_break

    def centered(line, style):
        line = clip(ascii_line(line) if state.ascii else line, width)
        pad = " " * ((width - display_width(line)) // 2)
        return (StyledText([(pad, None), (line, None)]) if getattr(line, "spans", None) else pad + line, style)

    message = [centered(state.status, "accent")] if state.status else []
    if not timer and not rest:
        return (header + [(" No timer running · press t to start one", "secondary")] + message + [rule] + footer)[:height]

    if rest:
        elapsed, target = break_elapsed(state), rest["seconds"]
        tone = "rest"
        badge = "● BREAK OVER" if elapsed >= target else "● BREAK"
        association, name = rest["association"], rest["name"]
        round_label = f"BREAK · next: round {rest['next_round']} of 4"
        round_note, done_rounds, current_round = "", rest["saved_round"], None
    else:
        elapsed, target = ui_elapsed(state), timer.get("target_duration_seconds")
        association, name = timer["association"], timer["name"]
        if target and elapsed >= target:
            badge, tone = "● TIME'S UP", "gold"
        elif timer["status"] == "paused":
            badge, tone = "❚❚ PAUSED", "accent"
        else:
            badge, tone = "● RUNNING", "accent"
        current_round = focus_round(state)
        round_label = f"ROUND {current_round} OF 4"
        round_note = f"then a {break_minutes(current_round)}-minute break"
        done_rounds = current_round - 1

    status_labels = [(badge, tone)]
    if association["name"]:
        status_labels.append((("next: " if rest else "") + association["name"], "normal_bold"))
    if name:
        status_labels.append((("next: " if rest and not association["name"] else "") + name, "normal"))
    status_width = sum(display_width(ascii_line(text) if state.ascii else text) for text, _ in status_labels)
    separator = "  ·  " if status_width + 5 * (len(status_labels) - 1) <= width else " · "
    status_parts = [status_labels[0]]
    for part in status_labels[1:]:
        status_parts += [(separator, "secondary"), part]
    status = centered(StyledText(status_parts), "normal")

    gap = "   " if width >= 60 else " "
    round_parts = [(round_label, "secondary_bold"), (gap, None)]
    for number in range(1, 5):
        if number > 1 and width >= 60:
            round_parts.append((" ", None))
        if number <= done_rounds:
            round_parts.append(("●", "gold"))
        elif number == current_round:
            round_parts.append(("◉", "accent"))
        else:
            round_parts.append(("○", "secondary"))
    rounds = StyledText(round_parts)
    round_rows = []
    if round_note and display_width(rounds + gap + round_note) <= width:
        rounds = StyledText([(rounds, None), (gap + round_note, "secondary")])
    elif round_note:
        round_rows.append(centered(round_note, "secondary"))
    round_rows.insert(0, centered(rounds, "normal"))

    timing = []
    if target:
        percent = min(100, int(elapsed / target * 100))
        bar = progress_bar(elapsed, target, min(56, max(0, width - 8)), ascii=state.ascii)
        remaining = f"{clock(target - elapsed)} left of {clock(target)}"
        if elapsed >= target:
            remaining = f"Press space to start round {rest['next_round']}" if rest else "Time's up · press s to save"
        timing = [centered(StyledText([(bar, tone), (f"  {percent}%", "normal_bold")]), "normal"),
                  centered(remaining, tone if elapsed >= target else "secondary")]

    current_tag = association["name"] if association["kind"] == "tag" and not rest else None
    stats = focus_today_lines(state, width, current_tag)
    details = timing + round_rows + message
    available = height - len(header) - 1 - len(details) - len(stats) - len(footer) - 2
    if available < 1:
        # Retain the clock, status and whole key pairs on short terminals.
        compact_header = header[:1] if height < len(header) + 2 else header
        primary = compact_header + [status, centered(clock(elapsed), tone)] + details
        remaining_rows = max(0, height - len(primary))
        footer = footer[:remaining_rows]
        return (primary + stats[:remaining_rows - len(footer)] + footer)[:height]

    clock_rows = [centered(line, tone) for line in big_clock(clock(elapsed), width, available, ascii=state.ascii)]
    blank = ("", "normal")
    spacing = [blank] if height >= 24 and available - len(clock_rows) >= 6 else []
    lines = header + spacing * 2 + [status] + spacing + clock_rows
    if timing:
        lines += spacing + timing
    lines += spacing + round_rows + message
    bottom = [rule] + stats + spacing + [rule] + footer
    return lines + [blank] * (height - len(lines) - len(bottom)) + bottom


def ascii_line(line):
    table = str.maketrans({"·": ".", "─": "-", "↑": "^", "↓": "v", "⏎": "Enter",
                           "−": "-", "±": "+/-", "…": "~", "●": "*", "❚": "|",
                           "○": "o", "◉": "@", "✓": "x", "⇥": "Tab", "←": "<", "→": ">",
                           "▲": "^", "▼": "v", "›": ">"})
    convert = lambda text: str(text).translate(table).encode("ascii", "replace").decode("ascii")
    if not getattr(line, "spans", None):
        return convert(line)
    parts, cursor = [], 0
    for offset, fragment, style in line.spans:
        parts.append((convert(cell_slice(line, cursor, offset)), None))
        parts.append((convert(fragment), style))
        cursor = offset + display_width(fragment)
    parts.append((convert(cell_slice(line, cursor, display_width(line))), None))
    return StyledText(parts)


def tab_header(state, width, active_tab):
    parts = [(" Momentum Grids   ", "title")]
    for number, name in enumerate(("Today", "Grid", "Focus", "Tasks"), 1):
        parts += [(f"{number} {name}", "tab_active" if active_tab == number else "secondary"), ("  ", None)]
    full = StyledText(parts)
    if display_width(full) + 13 <= width:
        return StyledText([(full, None), (" " * (width - display_width(full) - 10) + state.date.strftime("%a %d %b"), "secondary")])
    return full


def tab_top(state, width, *, active_tab=None):
    active_tab = state.tab if active_tab is None else active_tab
    if width < 60:
        parts = []
        for n, name in enumerate(("Today", "Grid", "Focus", "Tasks"), 1):
            parts += [(f"{n} {name}", "tab_active" if n == active_tab else "secondary"), (" ", None)]
        return [(columns(" Momentum Grids", state.date.strftime("%a %d %b"), width), "title"), (StyledText(parts), "normal")]
    return [(tab_header(state, width, active_tab), "normal"), ("─" * width, "secondary")]


def grid_habit(state):
    habits = state.years.get(state.year, {}).get("habits", [])
    return habits[min(state.grid_selection, len(habits) - 1)] if habits else None


def grid_status(state):
    habit = grid_habit(state)
    if not habit:
        return "No habits in this year"
    day = state.grid_cursor
    code = year_states(habit, state.year, state.date.date()).get(day, " ")
    label = {"d": "done", "p": "partial", "m": "missed", "f": "frozen", "o": "not scheduled", "-": "open", " ": "future"}[code]
    action = "Enter sets value" if habit["habit_type"] == "numeric" else "Enter clears" if code == "d" else "Enter marks done"
    return f"{day.strftime('%a %-d %b')} · {label} · " + ("future dates cannot be edited" if day > state.date.date() else action)


def grid_tab_lines(state, width, height, *, footer=None):
    lines = tab_top(state, width)
    habit = grid_habit(state)
    payload = state.years.get(state.year, {})
    switcher = f" {state.year}  " + (habit["name"] if habit else "No habits")
    lines += [(switcher, "title"), (" ⇥/shift-⇥ habit · ←→ at an edge", "secondary")]
    if habit:
        # Render thresholds refer to physical terminal columns, before the
        # reserved final column is removed.
        lines += year_lines(habit, payload, state.date.date(), cursor=state.grid_cursor,
                            width=width + 1, height=height, ascii=state.ascii, monochrome=state.monochrome)
    if footer is None:
        footer = [(state.status or grid_status(state), "accent"),
                  ("arrows day · Enter edit", "secondary"),
                  ("[ previous year · " + ("] next year · " if payload.get("can_go_to_next_year") else "") + "r refresh · Esc back · q quit", "secondary")]
        if height < 20:
            footer = footer[:1] + [("Tab · arrows · Enter · [ ] · r · Esc/q", "secondary")]
    return lines[:max(0, height - len(footer))] + [("", "normal")] * max(0, height - len(lines) - len(footer)) + footer


def focus_tab_lines(state, width, height):
    history = state.focus_windows.get(state.focus_days, {})
    sessions = history.get("sessions", [])
    summary = focus_summary(sessions, state.date.date(), state.focus_days, timer=state.timer, elapsed=ui_elapsed(state))
    delta = summary["delta_seconds"]
    heading = f" LAST {state.focus_days} DAYS {short_duration(summary['total_seconds'])} focused"
    comparison = f" {'▲' if delta >= 0 else '▼'} {short_duration(abs(delta))} vs the {state.focus_days} days before"
    lines = tab_top(state, width) + [(heading + (comparison if width >= 75 else ""), "title")]
    if width < 75:
        lines.append((comparison, "secondary"))
    chart_rows = 8 if height >= 28 else 2
    lines += focus_chart(summary, width, rows=chart_rows, ascii=state.ascii)
    lines.append((" BY TAG", "title"))
    max_tags = max(1, height - len(lines) - 8) if height >= 28 else 1
    for tag in summary["tags"][:max_tags]:
        right = f"{short_duration(tag['seconds'])} {tag['percent']}%"
        name_width = min(20, max(4, width - len(right) - 5))
        name = clip(ascii_line(tag["name"]) if state.ascii else tag["name"], name_width)
        parts = [(" " + name + " " * (name_width - display_width(name)), "normal")]
        bar_width = min(30, max(0, width - name_width - len(right) - 5))
        parts += [(" " + progress_bar(tag["seconds"], max(1, summary["total_seconds"]), bar_width, ascii=state.ascii), "secondary"),
                  (" " + right, "normal")]
        lines.append((StyledText(parts), "normal"))
    if not summary["tags"]:
        lines.append((" No completed focus yet", "secondary"))
    lines.append((" RECENT", "title"))
    recent = ([state.timer] if state.timer else []) + sessions
    for session in recent[:4 if height >= 28 else 1]:
        started = parse_time(session["started_at"]).astimezone()
        live = session is state.timer
        seconds = ui_elapsed(state) if live else session["duration_seconds"]
        status = session["status"] if live else "saved"
        label = started.strftime("%a %d  %H:%M")
        right = f"{status} {clock(seconds)}"
        title_width = max(0, width - display_width(label + right) - 5)
        text = f" {label}  {clip(timer_title(session), title_width)}"
        lines.append((columns(text, right, width), "accent" if live else "secondary"))
    hints = "w week · m month · y year · x CSV · f focus · r refresh · Esc back · q quit" if width >= 75 else "w/m/y range · x CSV · f · r · Esc/q"
    footer = [(state.status or ("History capped at 20 pages (2000 sessions)" if history.get("capped") else ""), "warning" if history.get("capped") else "accent"),
              (hints, "secondary")]
    return lines[:max(0, height - len(footer))] + [("", "normal")] * max(0, height - len(lines) - len(footer)) + footer


def visible_tasks(state):
    return [t for tasks in task_sections(state.tasks_payload).values() for t in tasks] if state.tasks_payload else []


def selected_task(state):
    tasks = visible_tasks(state)
    return tasks[min(state.task_selection, len(tasks) - 1)] if tasks else None


def tasks_tab_lines(state, width, height, *, footer=None):
    payload = state.tasks_payload or {"day": state.date.date().isoformat(), "tasks": [], "groups": [], "limits": {}}
    groups = {g["id"]: g for g in payload["groups"]}
    sections = task_sections(payload)
    today = sections["TODAY"]
    top = tab_top(state, width) + [(f" KAIROS  {sum(t['state'] == 'open' for t in today)} open today · {sum(t['state'] == 'done' for t in today)} done", "title")]
    limits = payload.get("limits", {})
    limit_text = []
    if limits.get("group_limit") is not None:
        limit_text.append(f"groups {len(groups)}/{limits['group_limit']}")
    if limits.get("step_reminders") is False:
        limit_text.append("reminders require an upgrade")
    if limit_text:
        top.append((" " + " · ".join(limit_text), "secondary"))
    selected = selected_task(state)
    rows, selected_row = [], 0
    for section, tasks in sections.items():
        rows.append((" " + section, "title"))
        if not tasks:
            rows.append(("   No tasks", "secondary"))
        for task in tasks:
            is_selected = selected and task["id"] == selected["id"]
            done = task["state"] == "done"
            mark = ("x" if done else "o") if state.ascii else ("✓" if done else "○")
            group = groups.get(task.get("group_id"))
            reminder = parse_time(task.get("remind_at"))
            detail = (" ● " + group["name"]) if group else ""
            detail += (" " + reminder.astimezone().strftime("%H:%M")) if reminder else ""
            if state.ascii:
                detail = ascii_line(detail)
            title_width = max(4, width - display_width(detail) - 5)
            if display_width(detail) > width // 2:
                detail = clip(detail, width // 2)
                title_width = max(4, width - display_width(detail) - 5)
            title = clip(ascii_line(task["title"]) if state.ascii else task["title"], title_width)
            parts = [(('›' if is_selected else ' ') + f" {mark} {title}",
                      "selected" if is_selected else "secondary" if done else "normal")]
            parts.append((" " * max(1, width - display_width(parts[0][0]) - display_width(detail)), None))
            parts.append((detail,
                          "group_" + group["color"] if group else "secondary"))
            if is_selected:
                selected_row = len(rows)
            rows.append((StyledText(parts), "normal"))
            if is_selected:
                if task.get("first_step"):
                    rows += [("     first step  " + task["first_step"], "secondary")]
                if reminder:
                    rows += [("     reminder    " + reminder.astimezone().strftime("%a %d %b at %H:%M"), "secondary")]
    if footer is None:
        footer = [(state.status, "accent"), ("Space done/undo · → tomorrow · l later · T today", "secondary"),
                  ("a add · e edit · r remind · x dismiss · t timer", "secondary"), ("R refresh · Esc back · q quit", "secondary")]
        if height < 20:
            footer = footer[:1] + [("Space · a e r x t · → l T · R · Esc/q", "secondary")]
    count = max(0, height - len(top) - len(footer))
    start = max(0, selected_row - count + 3)
    shown = rows[start:start + count]
    return top + shown + [("", "normal")] * max(0, count - len(shown)) + footer


def input_lines(state, width):
    labels = {"year_value": "Value", "task_add": "New task ›", "task_edit": "Title ›",
              "task_remind": "Reminder HH:MM ›", "focus_export": "CSV file ›"}
    if state.overlay == "year_confirm":
        habit, day = state.pending_edit
        prompt = f"Change {habit['name']} on {day.isoformat()} (older than 7 days)? y/N"
    elif state.overlay == "task_dismiss":
        prompt = f"Dismiss {state.pending_task['title']}? y/N"
    else:
        prompt = labels[state.overlay] + " " + state.input_text + "_"
    lines = tab_top(state, width) + [(line, "accent") for line in wrap_hints(prompt.split(), width, " ")]
    if state.overlay in ("task_add", "task_remind"):
        destinations = ("Today", "Tomorrow", "Later") if state.overlay == "task_add" else ("Today", "Tomorrow")
        lines += [(" " + destinations[state.input_destination] + " · ⇥ switch", "title")]
    lines += [(state.status, "warning"), ("y confirms · Esc cancels" if state.overlay in ("year_confirm", "task_dismiss") else "Backspace · Enter saves · Esc cancels", "secondary")]
    return lines


def render(state, width, height):
    """Build (text, style) rows without curses, I/O, or changes to state.

    Leave the last terminal column unused, including the bottom-right cell.
    Small screens shed decoration and bars before the selected habit.
    """
    width, height = max(0, width - 1), max(0, height)
    if not width or not height:
        return []
    if state.overlay in ("year_value", "task_add", "task_edit", "task_remind"):
        # Inline prompts keep the relevant grid/task selection on screen.
        footer = [("─" * width, "secondary")] + input_lines(state, width)[len(tab_top(state, width)):]
        lines = (grid_tab_lines(state, width, height, footer=footer) if state.overlay == "year_value"
                 else tasks_tab_lines(state, width, height, footer=footer))
    elif state.overlay in ("year_confirm", "task_dismiss", "focus_export"):
        lines = input_lines(state, width)
    elif state.overlay == "focus":
        lines = focus_lines(state, width, height)
    elif state.overlay == "grid":
        lines = grid_lines(state.habits[state.selection], state.date, ascii=state.ascii, monochrome=state.monochrome) if state.habits else []
        lines += [("", "normal"), (" Any key closes the grid", "secondary")]
    elif state.overlay == "help":
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
        rows = [(StyledText([(("› " if i == state.picker_selection else "  ") + (tag["name"] if tag else "No tag"),
                             "selected" if i == state.picker_selection else None)]), "normal")
                for i, tag in enumerate(choices) if start <= i < start + count]
        if not choices:
            rows = [(" No matching tags", "secondary")][:count]
        lines = top + rows + [("", "normal")] * max(0, count - len(rows)) + bottom
    elif state.overlay in ("minutes", "discard"):
        prompt = (" Countdown minutes (Enter for none): " + state.minutes
                  if state.overlay == "minutes" else state.discard_prompt)
        # Wrapping keeps the complete destructive confirmation readable at 40 columns.
        lines = [(" Start a timer" if state.overlay == "minutes" else " Discard timer", "title")]
        lines += [(line, "accent" if state.overlay == "minutes" else "warning") for line in wrap_hints(prompt.split(), width, " ")]
        lines += [(state.status, "accent"),
                  ("Digits · Backspace · Enter · Esc cancel" if state.overlay == "minutes"
                   else "y discard  N/Esc keep", "secondary")]
    elif state.tab == 2:
        lines = grid_tab_lines(state, width, height)
    elif state.tab == 3:
        lines = focus_tab_lines(state, width, height)
    elif state.tab == 4:
        lines = tasks_tab_lines(state, width, height)
    else:
        timer = state.timer
        top = tab_top(state, width)
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
                    top.append(("   " + progress_bar(elapsed, target, bar_width, ascii=state.ascii) + "  " + left, "secondary"))
        else:
            top.append((" No timer running · press t to start one", "secondary"))
        if height >= 12:
            top.append(("", "normal"))
        done, total = today_counts(state.habits)
        dots = ("#" * done + "o" * (total - done)) if state.ascii else ("●" * done + "○" * (total - done))
        summary = f" TODAY  {done} of {total} done  " + clip(dots, max(0, width - 55 if width >= 79 else width - 24))
        if width >= 79:
            spark, rate = sparkline(state.habits, ascii=state.ascii)
            summary = columns(summary, f"LAST 9 DAYS {spark} {rate}%", width)
        top.append((summary, "title"))
        if height >= 12:
            _, _, name_width, _, strip_width, _, streak_width = habit_layout(state.habits, width, ascii=state.ascii)
            heading = "   " + clip("HABIT", name_width).ljust(name_width + 2)
            heading += "LAST 14 DAYS    " if strip_width else ""
            heading += "STREAK " if streak_width else ""
            top.append((heading + "TODAY", "secondary"))
        hints = ["↑↓ move", "⏎ log", "+ − adjust", "t timer"]
        if timer:
            hints += ["p pause" if timer["status"] == "running" else "p resume", "s save", "d discard"]
        hints += ["g grid", "f focus", "r refresh", "? help", "q quit"]
        foot = wrap_hints(hints, width) if height >= 10 else [
            "↑↓ ⏎ ± t " + ("p s d " if timer else "") + "r ? q"
        ]
        bottom = [(state.status, "accent")]
        bottom += ([("─" * width, "secondary")] if height >= 12 else [])
        bottom += [(line, "secondary") for line in foot]
        if height >= 20:
            legend = "    " + "  ".join(day_glyph(code, ascii=state.ascii, monochrome=state.monochrome) + " " + label
                                        for code, label in zip("dpmfo-", ("done", "partial", "missed", "frozen", "off", "today open")))
            bottom.insert(0, (legend, "secondary"))
        # In unusually short terminals, always retain a row for the selection.
        bottom = bottom[-max(0, height - 1):] if height > 1 else []
        top = top[:max(0, height - len(bottom) - 1)]
        count = max(0, height - len(top) - len(bottom))
        rows = habit_rows(state, width, count)
        lines = top + rows + [("", "normal")] * max(0, count - len(rows)) + bottom
    return [(ascii_line(clip(ascii_line(text), width)) if state.ascii else clip(text, width), style)
            for text, style in lines[:height]]


def begin_grid_value(app):
    state = app.state
    habit, day = state.pending_edit
    if habit["habit_type"] == "numeric":
        entry = next((e for e in habit["entries"] if e["date"] == day.isoformat()), {})
        state.input_text = number(entry.get("value") or "0")
        state.overlay = "year_value"
        state.status = ""
    else:
        save_grid_value(app)


def save_grid_value(app, value=None):
    state = app.state
    habit, day = state.pending_edit
    # No local mutation on a refused write and no retry of the request.
    set_history_day(app.client, habit, day, value=value)
    state.overlay = None
    state.loaded_tabs.discard(1)
    state.years = {state.year: state.years[state.year]}
    state.status = f"Saved {habit['name']} · {day.isoformat()}"
    app.refresh_year(force=True)


def handle_tab_input(app, key):
    state = app.state
    overlay = state.overlay
    inputs = ("year_value", "year_confirm", "task_add", "task_edit", "task_remind", "task_dismiss", "focus_export")
    if overlay not in inputs:
        return False
    if key == "ESC":
        state.overlay = None
        state.status = "Cancelled"
        return True
    if overlay == "year_confirm":
        state.overlay = None
        if key in ("y", "Y"):
            begin_grid_value(app)
        else:
            state.status = "Kept the day unchanged"
        return True
    if overlay == "task_dismiss":
        state.overlay = None
        if key in ("y", "Y"):
            change_task(app.client, state.pending_task, "dismiss")
            app.refresh_tasks()
            state.status = "Dismissed " + state.pending_task["title"]
        else:
            state.status = "Kept the task"
        return True
    if key in ("TAB", "\t") and overlay in ("task_add", "task_remind"):
        state.input_destination = (state.input_destination + 1) % (3 if overlay == "task_add" else 2)
    elif key == "BACKSPACE":
        state.input_text = state.input_text[:-1]
    elif key == "ENTER":
        if overlay == "year_value":
            try:
                value = Decimal(state.input_text)
            except InvalidOperation as error:
                raise CLIError("Enter a non-negative number.") from error
            if not value.is_finite() or value < 0:
                raise CLIError("Enter a non-negative number.")
            save_grid_value(app, value)
        elif overlay == "task_add":
            add_task(app.client, state.input_text, ("today", "tomorrow", "later")[state.input_destination], identity=state.capture_id)
            state.overlay = None
            app.refresh_tasks()
            state.status = "Added " + state.input_text
        elif overlay == "task_edit":
            change_task(app.client, state.pending_task, "edit", title=state.input_text)
            state.overlay = None
            app.refresh_tasks()
            state.status = "Updated task title"
        elif overlay == "task_remind":
            try:
                reminder_time = datetime.strptime(state.input_text, "%H:%M").time()
            except ValueError as error:
                raise CLIError("Use HH:MM; Tab chooses today or tomorrow.") from error
            base = date.fromisoformat(state.tasks_payload["day"]) + timedelta(days=state.input_destination)
            # astimezone() resolves the local offset for the selected date,
            # including a DST boundary between today and tomorrow.
            reminder = datetime.combine(base, reminder_time).astimezone()
            change_task(app.client, state.pending_task, "remind", remind_at=reminder.isoformat())
            state.overlay = None
            app.refresh_tasks()
            state.status = "Reminder saved"
        else:
            if not state.input_text.strip():
                raise CLIError("Enter a filename, or Esc to cancel.")
            sessions, capped = focus_history(app.client, state.date.date(), state.focus_days)
            export_focus(sessions, state.input_text)
            state.overlay = None
            state.status = "Exported " + state.input_text + (" · capped at 20 pages" if capped else "")
    elif isinstance(key, str) and len(key) == 1 and key.isprintable():
        allowed = "0123456789." if overlay == "year_value" else "0123456789:" if overlay == "task_remind" else None
        if allowed is None or key in allowed:
            limit = 200 if overlay in ("task_add", "task_edit") else 1024 if overlay == "focus_export" else 20
            state.input_text = (state.input_text + key)[:limit]
    return True


def handle_tab_key(app, key):
    state = app.state
    if state.tab == 2:
        payload = state.years.get(state.year, {})
        habit = grid_habit(state)
        if key == "r":
            app.refresh_year(force=True)
            state.status = "Refreshed"
        elif key in ("[", "]"):
            if key == "]" and not payload.get("can_go_to_next_year"):
                state.status = "Already at the latest year"
                return True
            new_year = state.year + (1 if key == "]" else -1)
            if new_year < 1900:
                state.status = "The earliest supported year is 1900"
                return True
            state.year = new_year
            try:
                state.grid_cursor = state.grid_cursor.replace(year=new_year)
            except ValueError:  # Feb 29 in a non-leap year
                state.grid_cursor = date(new_year, 2, 28)
            app.refresh_year()
            state.status = ""
        elif key in ("TAB", "\t", "BTAB"):
            habits = payload.get("habits", [])
            if habits:
                state.grid_selection = (state.grid_selection + (-1 if key == "BTAB" else 1)) % len(habits)
                state.status = ""
        elif key in ("UP", "DOWN", "LEFT", "RIGHT") and habit:
            delta = {"UP": -1, "DOWN": 1, "LEFT": -7, "RIGHT": 7}[key]
            candidate = state.grid_cursor + timedelta(days=delta)
            if candidate.year == state.year:
                state.grid_cursor = candidate
            elif key in ("LEFT", "RIGHT"):
                state.grid_selection = (state.grid_selection + (1 if key == "RIGHT" else -1)) % len(payload["habits"])
            state.status = ""
        elif key == "ENTER" and habit:
            if state.grid_cursor > state.date.date():
                state.status = "Future dates cannot be edited."
            elif habit.get("can_log") is False:
                state.status = (habit.get("logging_block") or {}).get("message", "This habit cannot be logged.")
            else:
                state.pending_edit = (habit, state.grid_cursor)
                if (state.date.date() - state.grid_cursor).days > 7:
                    state.overlay = "year_confirm"
                else:
                    begin_grid_value(app)
        return True
    if state.tab == 3:
        if key in ("w", "m", "y"):
            state.focus_days = {"w": 7, "m": 30, "y": 365}[key]
            state.status = ""
            app.refresh_stats()
        elif key == "r":
            state.status = ""
            app.refresh_stats(force=True)
        elif key == "x":
            state.overlay, state.input_text = "focus_export", ""
        elif key in ("t", "p", "s", "d"):
            return False
        return True
    if state.tab == 4:
        task = selected_task(state)
        if key in ("UP", "DOWN", "j", "k"):
            state.task_selection = min(max(0, state.task_selection + (1 if key in ("DOWN", "j") else -1)), max(0, len(visible_tasks(state)) - 1))
        elif key == "R" or (key == "r" and task is None):
            app.refresh_tasks()
            state.status = "Refreshed"
        elif key == "a":
            state.overlay, state.input_text, state.input_destination = "task_add", "", 0
            state.capture_id = str(uuid.uuid4())
            state.status = ""
        elif task:
            if key in (" ", "RIGHT", "l", "T"):
                action = ("undo" if task["state"] == "done" else "done") if key == " " else {"RIGHT": "tomorrow", "l": "later", "T": "today"}[key]
                change_task(app.client, task, action)
                app.refresh_tasks()
                state.status = f"{task['title']} · {action}"
            elif key in ("e", "r", "x"):
                state.pending_task = dict(task)
                state.overlay = {"e": "task_edit", "r": "task_remind", "x": "task_dismiss"}[key]
                state.input_text = task["title"] if key == "e" else ""
                state.input_destination = 0
                state.status = ""
            elif key == "t":
                if state.timer:
                    state.status = "Save (s) or discard (d) the current timer first"
                else:
                    session = start_timer(app.client, name=task["title"])
                    state.timer, state.timer_fetched_at = session, time.monotonic()
                    state.focus_break = None
                    state.status = "Started " + task["title"]
        return True
    return False


def handle_key(app, key):
    """Translate terminal keys and handle input; API errors become status text."""
    state = app.state
    if isinstance(key, int):
        import curses

        key = {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_ENTER: "ENTER",
               curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT", curses.KEY_BTAB: "BTAB",
               curses.KEY_BACKSPACE: "BACKSPACE", curses.KEY_RESIZE: "RESIZE"}.get(key, key)
    key = {"\n": "ENTER", "\r": "ENTER", "\x1b": "ESC", "\x7f": "BACKSPACE", "\b": "BACKSPACE"}.get(key, key)
    if key in ("RESIZE", -1):
        return
    editing = state.overlay in ("task_add", "task_edit", "focus_export", "tags")
    if key == "\x03" or (key == "q" and not editing):
        state.focus_break = None
        app.quit = True
        return
    if state.overlay in ("help", "grid") and key not in ("1", "2", "3", "4"):
        state.overlay = None
        return
    try:
        if handle_tab_input(app, key):
            return
        if state.overlay in (None, "focus", "help", "grid") and key in ("1", "2", "3", "4"):
            app.open_tab(int(key))
            return
        if state.overlay is None:
            if key == "f":
                if state.timer or state.focus_break:
                    state.overlay = "focus"
                    app.refresh_focus()
                else:
                    state.status = "No timer running · press t to start one"
                return
            if key == "ESC" and state.tab != 1:
                app.open_tab(1)
                return
            if state.tab != 1 and handle_tab_key(app, key):
                return
        if state.overlay == "discard":
            session = state.pending_discard
            state.overlay = state.discard_return
            state.discard_return = None
            if key in ("y", "Y"):
                change_timer(app.client, session, "discard")
                state.timer = None
                state.overlay = None
                app.refresh(f"Discarded {timer_title(session)}")
            else:
                state.status = "Kept the timer"
            return
        if state.overlay == "focus":
            if key in ("ESC", "f"):
                state.overlay = None
                return
            if key == "n":
                state.notify = not state.notify
                if state.focus_break:
                    state.status = f"Notifications {'on' if state.notify else 'off'}"
                return
            if state.focus_break:
                if key in (" ", "p"):
                    app.start_focus_round()
                elif key == "s":
                    state.status = "Breaks aren't saved"
                elif key == "d":
                    state.focus_break = state.overlay = None
                    state.status = "Ended the break"
                return
            if key == " ":
                key = "p"
            if key not in ("p", "s", "d"):
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
                state.focus_break = None
                state.overlay = None
                app.refresh(f"Started {timer_title(session)}")
            elif isinstance(key, str) and key in "0123456789" and len(key) == 1:
                state.minutes = (state.minutes + key)[:6]
            return
        if key == "ESC":
            state.overlay = None
        elif key == "?":
            state.overlay = "help"
        elif key == "g" and state.habits:
            state.overlay = "grid"
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
                state.discard_return = state.overlay
                state.pending_discard = session
                state.discard_prompt = f"Discard {timer_title(session)} ({clock(ui_elapsed(state))}) without saving? y/N"
                state.overlay = "discard"
                return
            action = "finish" if key == "s" else "pause" if session["status"] == "running" else "resume"
            updated = change_timer(app.client, session, action)
            if action == "finish" and state.overlay == "focus":
                app.begin_focus_break(updated)
                return
            state.timer = None if action == "finish" else updated
            if action == "finish":
                state.overlay = None
                state.focus_windows.clear()
                if state.tab == 3:
                    app.refresh_stats()
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
            state.years.clear()
    except CLIError as error:
        if state.overlay == "focus" and state.focus_break:
            state.status = str(error)
        elif state.tab == 1 and state.overlay not in ("year_value", "year_confirm", "task_add", "task_edit", "task_remind", "task_dismiss", "focus_export"):
            app.refresh(str(error))
        else:
            state.status = str(error)
            if isinstance(error, APIError) and error.status == 409 and state.tab == 4:
                state.overlay = None
                try:
                    app.refresh_tasks()
                except CLIError as refresh_error:
                    state.status += " · " + str(refresh_error)


def curses_ui(screen, app):
    """The thin terminal adapter: styles, safe drawing, and input."""
    import curses

    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.keypad(True)
    screen.timeout(1000)
    styles = {"normal": 0, "title": curses.A_BOLD, "accent": curses.A_BOLD,
              "secondary": curses.A_DIM, "selected": curses.A_REVERSE | curses.A_BOLD,
              "gold": curses.A_BOLD, "partial": 0, "missed": curses.A_DIM,
              "frozen": 0, "warning": curses.A_BOLD,
              "rest": curses.A_BOLD, "key": curses.A_REVERSE,
              "tab_active": curses.A_BOLD | curses.A_UNDERLINE | curses.A_REVERSE,
              "cursor": curses.A_REVERSE | curses.A_BOLD}
    styles.update({"group_" + name: 0 for name in GROUP_INDEXES})
    # wrapper() initializes colors. Keep pair zero on the terminal's background
    # even when NO_COLOR disables all accent pairs.
    with contextlib.suppress(curses.error):
        curses.use_default_colors()
    colors = app.color != "never" and "NO_COLOR" not in os.environ and curses.has_colors()
    app.state.monochrome = not colors
    if colors:
        with contextlib.suppress(curses.error):
            theme = theme_name()
            palette = PALETTES[theme]
            for pair, style in enumerate(("accent", "secondary", "gold", "partial", "missed", "frozen", "warning", "normal"), 1):
                basic = curses.COLOR_CYAN if style == "frozen" else curses.COLOR_RED if style == "warning" else curses.COLOR_YELLOW
                curses.init_pair(pair, palette[style][1] if curses.COLORS >= 256 else basic, -1)
                styles[style] |= curses.color_pair(pair)
            for style in ("title", "accent", "selected"):
                styles[style] |= curses.color_pair(1)
            styles["tab_active"] = curses.A_BOLD | curses.A_UNDERLINE | curses.color_pair(1)
            styles["cursor"] |= curses.color_pair(1)
            indexes = GROUP_LIGHT_INDEXES if theme == "light" else GROUP_INDEXES
            basics = (curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_GREEN, curses.COLOR_MAGENTA,
                      curses.COLOR_YELLOW, curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_RED,
                      curses.COLOR_GREEN, curses.COLOR_CYAN)
            for pair, ((name, index), basic) in enumerate(zip(indexes.items(), basics), 9):
                curses.init_pair(pair, index if curses.COLORS >= 256 else basic, -1)
                styles["group_" + name] = curses.color_pair(pair)
            curses.init_pair(19, palette["rest"][1] if curses.COLORS >= 256 else curses.COLOR_CYAN, -1)
            styles["rest"] |= curses.color_pair(19)
            if curses.COLORS >= 256:
                foreground, background = palette["normal"][1], palette["key_bg"][1]
            elif theme == "light":
                foreground, background = curses.COLOR_BLACK, curses.COLOR_WHITE
            else:
                foreground, background = curses.COLOR_WHITE, curses.COLOR_BLACK
            curses.init_pair(20, foreground, background)
            styles["key"] = curses.color_pair(20)
    styles["normal_bold"] = styles["normal"] | curses.A_BOLD
    styles["secondary_bold"] = styles["secondary"] | curses.A_BOLD
    app.refresh()
    while not app.quit:
        app.state.now = time.monotonic()
        app.state.date = datetime.now().astimezone()
        if app.state.now - app.last_refresh >= UI_POLL_SECONDS:
            app.refresh()
        if app.check_done():
            with contextlib.suppress(curses.error):
                curses.beep()
        height, width = screen.getmaxyx()
        screen.erase()
        for row, (line, style) in enumerate(render(app.state, width, height)):
            with contextlib.suppress(curses.error):
                screen.addstr(row, 0, line, styles[style])
                for column, fragment, span_style in getattr(line, "spans", []):
                    if fragment:
                        screen.addstr(row, column, fragment, styles[span_style])
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:  # timeout: redraw the local timer
            continue
        handle_key(app, key)


def curses_term_override(term, has_rep, ncurses_version, fallback_available):
    """Old ncurses repeats only a wide glyph's low byte when terminfo has rep."""
    if (term and term != "xterm-256color" and has_rep and fallback_available
            and (ncurses_version is None or tuple(ncurses_version[:2]) < (6, 1))):
        return "xterm-256color"
    return None


def prepare_curses_terminal(fd=-1):
    """Choose a safe process-local TERM before wrapper/initscr reads it."""
    import curses

    term = os.environ.get("TERM")
    version = getattr(curses, "ncurses_version", None)
    if os.environ.get("MOMENTUM_KEEP_TERM") == "1" or not curses_term_override(term, True, version, True):
        return
    try:
        curses.setupterm(term, fd)
        has_rep = bool(curses.tigetstr("rep"))
        if not has_rep:
            return
        # Python caches its first setupterm(), so inspect the fallback in a
        # fresh interpreter. initscr() will re-read our final TERM in this one.
        probe = subprocess.run(
            [sys.executable, "-c", "import curses, sys; curses.setupterm('xterm-256color'); "
             "sys.exit(bool(curses.tigetstr('rep')))"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2,
        )
        override = curses_term_override(term, has_rep, version, probe.returncode == 0)
        if override:
            os.environ["TERM"] = override
    except (curses.error, OSError, subprocess.SubprocessError):
        pass  # Compatibility detection must never prevent opening the UI.


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
    prepare_curses_terminal()
    try:
        curses.wrapper(curses_ui, UIApp(client, color=args.color))
    except KeyboardInterrupt:
        pass
    except curses.error as error:
        raise CLIError(f"Could not open the interactive view: {error}") from error


def cmd_completion(client, args, out):
    parser = build_parser()
    commands = next(action.choices for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    global_options = [option for action in parser._actions for option in action.option_strings]
    options = {name: sorted({option for action in command._actions for option in action.option_strings})
               for name, command in commands.items()}
    task_commands = next(action.choices for action in commands["task"]._actions if isinstance(action, argparse._SubParsersAction))
    options["task"] = sorted(set(options["task"] + list(task_commands) +
                                 [option for command in task_commands.values() for action in command._actions for option in action.option_strings]))
    names = " ".join(commands)
    if args.shell == "bash":
        lines = ["_momentum() {", '  local cur="${COMP_WORDS[COMP_CWORD]}" cmd="" word choices',
                 '  for word in "${COMP_WORDS[@]:1:COMP_CWORD-1}"; do',
                 f'    case " {names} " in *" $word "*) cmd="$word"; break;; esac', '  done',
                 '  case "${COMP_WORDS[COMP_CWORD-1]}" in',
                 '    --color) COMPREPLY=( $(compgen -W "auto always never" -- "$cur") ); return;;',
                 '    completion) COMPREPLY=( $(compgen -W "zsh bash fish" -- "$cur") ); return;;',
                 '  esac', '  case "$cmd" in']
        lines += [f'    {name}) choices="{" ".join(flags)}";;' for name, flags in options.items()]
        lines += [f'    *) choices="{names} {" ".join(global_options)}";;', '  esac',
                  '  COMPREPLY=( $(compgen -W "$choices" -- "$cur") )', '}', 'complete -F _momentum momentum']
    elif args.shell == "zsh":
        lines = ['#compdef momentum', '_momentum() {', '  local command="" word',
                 '  for word in ${words[2,-1]}; do', f'    case " {names} " in *" $word "*) command="$word"; break;; esac',
                 '  done', '  if [[ ${words[CURRENT-1]} == --color ]]; then',
                 "    _values 'color' auto always never; return", '  fi',
                 '  if [[ $command == completion && ${words[CURRENT]} != -* ]]; then',
                 "    _values 'shell' zsh bash fish; return", '  fi', '  case $command in']
        lines += [f"    {name}) _values 'option' {' '.join(chr(39) + flag + chr(39) for flag in flags)};;" for name, flags in options.items()]
        lines += [f"    *) _values 'command' {names} {' '.join(chr(39) + flag + chr(39) for flag in global_options)};;", '  esac', '}', 'compdef _momentum momentum']
    else:
        lines = ['complete -c momentum -f', f"complete -c momentum -n '__fish_use_subcommand' -a '{names}'"]
        for name, flags in options.items():
            for flag in flags:
                if not flag.startswith("-"):
                    lines.append(f"complete -c momentum -n '__fish_seen_subcommand_from {name}' -a '{flag}'")
                    continue
                flag_option = ("-l " + flag[2:]) if flag.startswith("--") else ("-s " + flag[1:])
                values = " -x -a 'auto always never'" if flag == "--color" else ""
                lines.append(f"complete -c momentum -n '__fish_seen_subcommand_from {name}' {flag_option}{values}")
        lines += ["complete -c momentum -l color -x -a 'auto always never'",
                  "complete -c momentum -l version",
                  "complete -c momentum -n '__fish_seen_subcommand_from completion' -a 'zsh bash fish'"]
    print("\n".join(lines), file=out)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="momentum",
        description="Momentum Grids focus timers and habit logs from the terminal.",
        epilog=GET_STARTED,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"momentum {VERSION}")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="terminal colour (default: auto)")
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

    today = commands.add_parser("today", help="today's habits, history and completion summary")
    today.set_defaults(handler=cmd_today)
    grid = commands.add_parser("grid", help="show a habit's last 16 weeks")
    grid.add_argument("habit", help="habit name (a unique prefix or part) or id")
    grid.set_defaults(handler=cmd_grid)
    year = commands.add_parser("year", help="show a habit's year grid and stats")
    year.add_argument("habit", help="habit name or id")
    year.add_argument("--year", type=int, help="calendar year (default: current)")
    year.set_defaults(handler=cmd_year)
    week = commands.add_parser("week", help="last seven days of focus, bars and tags")
    week.set_defaults(handler=cmd_week)
    export = commands.add_parser("export", help="export completed focus sessions as CSV")
    export.add_argument("--days", type=positive_days, default=7)
    export.add_argument("--output", help="create a private CSV file (default: stdout)")
    export.set_defaults(handler=cmd_export)
    tasks = commands.add_parser("tasks", help="list Kairos tasks by planned day")
    tasks.set_defaults(handler=cmd_tasks)
    task = commands.add_parser("task", help="add or complete a Kairos task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    add = task_commands.add_parser("add", help="capture and plan a task")
    add.add_argument("title")
    destination = add.add_mutually_exclusive_group()
    destination.add_argument("--tomorrow", action="store_true")
    destination.add_argument("--later", action="store_true")
    add.set_defaults(handler=cmd_task)
    done = task_commands.add_parser("done", help="complete a task by title")
    done.add_argument("query")
    done.set_defaults(handler=cmd_task)
    prompt = commands.add_parser("prompt", help="cached one-line status for shells and tmux")
    prompt.add_argument("--max-age", type=float, default=60, help="refresh cache after this many seconds (default: 60)")
    prompt.add_argument("--format", help="template: {timer}, {elapsed}, {done}, {total}")
    prompt.add_argument("--tmux", action="store_true", help="emit tmux styles instead of ANSI")
    prompt.set_defaults(handler=cmd_prompt)
    completion = commands.add_parser("completion", help="print a shell completion script")
    completion.add_argument("shell", choices=("zsh", "bash", "fish"))
    completion.set_defaults(handler=cmd_completion)
    for command in commands.choices.values():
        command.add_argument("--color", choices=("auto", "always", "never"), default=argparse.SUPPRESS)
    for command in task_commands.choices.values():
        command.add_argument("--color", choices=("auto", "always", "never"), default=argparse.SUPPRESS)
    for command in (status, tags, habits, today, grid, prompt, year, week, tasks):
        command.add_argument("--json", action="store_true", help="print stable JSON without presentation styles")
    return parser


def main(argv=None, *, transport=urllib_transport, config_dir=None, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    if args.command is None and (not sys.stdin.isatty() or not out.isatty()):
        print(GET_STARTED, file=out)
        return 0
    try:
        if args.command == "completion":
            args.handler(None, args, out)
            return 0
        client = Client(SessionStore(config_dir or default_config_dir()), transport=transport)
        args.handler(client, args, out)
    except CLIError as error:
        if args.command == "prompt":
            return 0
        print(f"momentum: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.command == "prompt":
            return 0
        return 130
    except Exception:
        if args.command == "prompt":
            return 0
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
