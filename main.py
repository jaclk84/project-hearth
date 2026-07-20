# =============================================================================
#  PROJECT HEARTH  —  GUPPI (TELEGRAM EDITION)
# =============================================================================
#
#  WHY TELEGRAM
#  ------------
#  The SMS version was blocked by A2P 10DLC carrier registration (rejected twice:
#  once as "vague opt-in", once as "looks like P2P"). Telegram has no carrier
#  gatekeeper, no per-message cost, no 160-char limit, and supports rich formatting,
#  images, and group chats. Everything else about Guppi is unchanged.
#
#  WHAT CARRIED OVER (all of it)
#  -----------------------------
#  People + roles + permissions (enforced in CODE), memory with strict rules,
#  reminders (incl. recurring), shared lists + templates, Google Calendar,
#  per-person Gmail, web search, the scheduler (6am briefing, weekly digest,
#  urgent-email poll), daily caps, quiet hours, and the kill switch.
#
#  WHAT CHANGED (the transport layer only)
#  ---------------------------------------
#    * Identity is a Telegram CHAT ID, not a phone number.
#    * Adults bind themselves ONCE with a secret: /start <SETUP_SECRET>.
#      The secret lives in a Railway env var. Nobody can become an adult without it.
#    * Inbound: a Telegram webhook (JSON) instead of Twilio's form POST.
#    * Outbound: Telegram sendMessage instead of the Twilio REST API.
#    * Images: Telegram's getFile + download, instead of Twilio media URLs.
#    * Formatting: Markdown and longer messages are fine now (SMS's plain-text,
#      300-char discipline is no longer needed).
#
#  GROUP CHAT SUPPORT (and the privacy rule that comes with it)
#  ------------------------------------------------------------
#  Guppi works in the family group AND in private 1:1 chats, but they are NOT the
#  same. In a group, every reply is visible to everyone in the room — so anything
#  private must never be answered there.
#
#    PRIVATE chat  -> full access for that person's role, including EMAIL.
#    GROUP chat    -> family-safe only: calendar, shared lists, reminders, general
#                     questions. NO email. NO memory recall. Guppi offers to DM instead.
#    GROUP, not addressed -> Guppi stays silent (it only replies when @mentioned,
#                     replied-to, or sent a /command). Otherwise it would butt into
#                     every family conversation.
#
#  Proactive messages (6am briefing, weekly digest, reminders, urgent email) are
#  sent to individuals PRIVATELY, never to the group.
#
#  THE PERMISSION MODEL (unchanged, enforced in code)
#    adult     (Jason, Kim)          calendar read+write, email yes, full memory
#    caregiver (Breanna)             calendar read+write, email NO,  logistics memory
#    child     (Lillian, Charlotte)  calendar READ ONLY,  email NO,  names/logistics only
#    unknown   (anyone unbound)      no family data at all; web search only
#
#  Unknown FAILS SAFE: an unbound chat gets the cautious path, never the permissive
#  one. Claiming "I'm Jason" grants nothing — only the setup secret binds an adult.
#
# =============================================================================

import os
import re
import time
import json
import sqlite3
import datetime
from zoneinfo import ZoneInfo
import base64
import hmac
import html as html_lib
import imaplib
import smtplib
import email as emaillib
from email.mime.text import MIMEText
from email.header import decode_header
import urllib.request
import urllib.parse
import urllib.error
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Response, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from anthropic import Anthropic

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# =============================================================================
#  CONFIGURATION
# =============================================================================
app = FastAPI()
claude = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-haiku-4-5"

DB_PATH = "/app/data/guppi.db"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
BASE_URL = "https://web-production-5fa1fd.up.railway.app"
REDIRECT_URI = f"{BASE_URL}/oauth/callback"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# The family's local timezone. Railway servers run on UTC, so without this Guppi
# thinks it is already "tomorrow" during your evening — every reminder, every
# "tomorrow", and the morning briefing would be off by a day. Set TIMEZONE in
# Railway (e.g. America/New_York) to override.
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))

# ---- Telegram ---------------------------------------------------------------
# TELEGRAM_BOT_TOKEN: from @BotFather when you create the bot.
# TELEGRAM_SETUP_SECRET: a private phrase YOU choose. An adult binds their chat by
#   sending "/start <secret>" once. This is the security boundary that replaces the
#   phone-number env vars from the SMS version. Keep it out of code and GitHub.
# TELEGRAM_WEBHOOK_SECRET: optional; Telegram echoes it in a header so we can verify
#   an incoming webhook really came from Telegram and not a stranger POSTing to us.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_SETUP_SECRET = os.environ.get("TELEGRAM_SETUP_SECRET", "")

def _secret_ok(supplied):
    """Timing-safe check of a supplied secret against TELEGRAM_SETUP_SECRET. Uses
    hmac.compare_digest so a wrong guess can't be narrowed by response-time measurement."""
    if not TELEGRAM_SETUP_SECRET or not supplied:
        return False
    return hmac.compare_digest(str(supplied), TELEGRAM_SETUP_SECRET)
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# The bot's @username, used to detect being addressed in a group (e.g. "@GuppiBot").
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@").lower()

# ---- Microsoft (live.com / outlook) email via OAuth2 over IMAP ---------------
# Personal Microsoft accounts no longer allow password/app-password IMAP (basic auth
# was retired Sept 2024). The only supported path is OAuth2: the person signs in on
# Microsoft's page, we get a refresh token, and we authenticate IMAP with an access
# token via SASL XOAUTH2. Requires an Entra app registration (multitenant + personal
# accounts) with delegated scope IMAP.AccessAsUser.All + offline_access.
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_REDIRECT_URI = f"{BASE_URL}/oauth/microsoft/callback"
MS_AUTHORITY = "https://login.microsoftonline.com/common"
# offline_access -> refresh token; the IMAP resource scope -> mail access.
MS_SCOPES = ("offline_access openid email profile "
             "https://outlook.office.com/IMAP.AccessAsUser.All "
             "https://outlook.office.com/SMTP.Send")
MS_IMAP_HOST = "outlook.office365.com"
MS_SMTP_HOST = "smtp.office365.com"
MS_SMTP_PORT = 587
MS_IMAP_PORT = 993

# Where to get weather for the briefing (open-meteo needs no API key). Defaults to
# the Philadelphia area; override with LATITUDE / LONGITUDE env vars.
WEATHER_LAT = os.environ.get("LATITUDE", "39.95")
WEATHER_LON = os.environ.get("LONGITUDE", "-75.16")
# The town name shown alongside a forecast. Purely cosmetic, but it makes a WRONG location
# VISIBLE instead of silent - the whole reason the Philadelphia-vs-Swarthmore gap went
# unnoticed was that nothing ever said which place it was reporting on.
WEATHER_PLACE = os.environ.get("WEATHER_PLACE", "")

# AeroDataBox via RapidAPI — flight lookup by number+date. Free tier is enough for a
# few trips a month. Set FLIGHT_API_KEY in Railway (the RapidAPI key). Without it, the
# flight tool falls back to asking the user for times.
FLIGHT_API_KEY = os.environ.get("FLIGHT_API_KEY", "")
FLIGHT_API_HOST = os.environ.get("FLIGHT_API_HOST", "aerodatabox.p.rapidapi.com")

# Google Calendar color for travel events, so trips stand out. "2" is Sage (muted green)
# in Google's fixed 11-color event palette. Override via env if you prefer another.
TRAVEL_COLOR_ID = os.environ.get("TRAVEL_COLOR_ID", "2")


# ---- A3: transient API failures must not throw a user's request away -----------
# A 529 "overloaded" dropped one of Kim's messages entirely and she had to ask four times.
# open-meteo already retried this exact class of failure; the model call - which every
# single message depends on - had no retry at all.
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_RETRYABLE_TEXT = ("overloaded", "rate limit", "rate_limit", "timeout", "timed out",
                   "connection", "temporarily unavailable", "service unavailable")


def claude_create(**kwargs):
    """claude.messages.create with backoff on transient failures.

    Retries 429/5xx/529 and network blips up to 4 attempts (~1.5s, 3s, 6s). A genuine
    error - bad request, auth, context length - raises immediately rather than being
    retried pointlessly."""
    for attempt in range(4):
        try:
            return claude.messages.create(**kwargs)
        except Exception as e:
            status = getattr(e, "status_code", None)
            text = str(e).lower()
            transient = (status in _RETRYABLE_STATUS
                         or any(t in text for t in _RETRYABLE_TEXT))
            if not transient or attempt == 3:
                if transient:
                    print(f"[claude] giving up after {attempt + 1} attempts: {e}")
                raise
            wait = 1.5 * (2 ** attempt)
            print(f"[claude] transient error "
                  f"({status or type(e).__name__}) attempt {attempt + 1}/4; "
                  f"retrying in {wait:.1f}s")
            time.sleep(wait)


def now_local():
    """Current time in the family's timezone — always use this, never datetime.now()."""
    return datetime.datetime.now(TIMEZONE)


# Which calendar Guppi reads and writes. "primary" is a person's OWN calendar —
# wrong for a family assistant. Set FAMILY_CALENDAR_ID in Railway to the shared
# family calendar's id (looks like ...@group.calendar.google.com). Use the
# list_calendars tool to find it.
FAMILY_CALENDAR_ID = os.environ.get("FAMILY_CALENDAR_ID", "primary")

# Family roster: names seeded here. Chat IDs are bound later:
#   - adults bind themselves with  /start <TELEGRAM_SETUP_SECRET>
#   - everyone else is linked BY A PARENT ("link Breanna", then Breanna sends /start)
SEEDED_PEOPLE = [
    ("Jason",     "adult"),
    ("Kim",       "adult"),
    ("Breanna",   "caregiver"),
    ("Lillian",   "child"),
    ("Charlotte", "child"),
]

# What each role may do. Enforced in code, never left to the model's judgment.
PERMISSIONS = {
    "adult":     {"calendar_read": True, "calendar_write": True,  "email": True},
    "caregiver": {"calendar_read": True, "calendar_write": True,  "email": False},
    "child":     {"calendar_read": True, "calendar_write": False, "email": False},
    "unknown":   {"calendar_read": False, "calendar_write": False, "email": False},
}


# =============================================================================
#  DATABASE
# =============================================================================
def init_db():
    os.makedirs("/app/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # WAL lets readers and a writer proceed concurrently instead of blocking each other —
    # important because the scheduler threads and the webhook hit the DB at the same time
    # (M3). Set once; it persists on the database file.
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception as e:
        print(f"[db] could not set WAL: {e}")
    # One Google token PER PERSON (keyed by their name), not one global token.
    # This is what lets Jason and Kim each connect their own account, and keeps
    # each adult's inbox private to them.
    conn.execute("""CREATE TABLE IF NOT EXISTS google_tokens (
        person TEXT PRIMARY KEY,
        token_json TEXT NOT NULL)""")
    # IMAP email accounts (live.com/outlook, and any IMAP provider). Stores the
    # address + an APP PASSWORD (never the real account password). This is a stored
    # credential, so it lives only in the DB on the private Railway volume — never in
    # code or GitHub. A person may have this AND a Google token; both get searched.
    # Microsoft OAuth tokens, one per person (live.com/outlook via IMAP-XOAUTH2).
    conn.execute("""CREATE TABLE IF NOT EXISTS ms_tokens (
        person TEXT PRIMARY KEY,
        email_addr TEXT,
        refresh_token TEXT NOT NULL,
        access_token TEXT,
        expires_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS imap_accounts (
        person TEXT PRIMARY KEY,
        email_addr TEXT NOT NULL,
        app_password TEXT NOT NULL,
        imap_host TEXT NOT NULL,
        imap_port INTEGER NOT NULL DEFAULT 993)""")
    # Simple key/value settings, adjustable by text (e.g. the daily caps).
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL,
        chat_id TEXT UNIQUE)""")
    # A parent "invites" a non-adult by name; that person then sends /start and is
    # bound to the next open invite. Keeps kids/caregiver from needing the secret.
    conn.execute("""CREATE TABLE IF NOT EXISTS pending_links (
        name TEXT PRIMARY KEY,
        created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL,
        about TEXT,
        added_by TEXT,
        created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        due_at TEXT NOT NULL,
        for_chat TEXT,
        created_by TEXT,
        fired INTEGER NOT NULL DEFAULT 0,
        repeat TEXT DEFAULT 'none')""")
    # Migration: older DBs created before Phase 4 Batch 2 lack the 'repeat' column.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(reminders)").fetchall()]
    if "repeat" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat TEXT DEFAULT 'none'")

    # -------------------------------------------------------------------------
    # MIGRATION: SMS -> Telegram.
    #
    # A database that already exists is NOT reshaped by "CREATE TABLE IF NOT
    # EXISTS" — that statement is skipped entirely, so an old table keeps its old
    # columns. The SMS-era DB has people.phone and reminders.for_phone; the
    # Telegram code needs people.chat_id and reminders.for_chat.
    #
    # Rename the columns in place. The old phone values are meaningless now (a
    # phone number is not a Telegram chat id), so we clear them: everyone simply
    # re-binds with /start. Names, roles, reminders, lists, and memories all
    # survive untouched.
    # -------------------------------------------------------------------------
    pcols = [r[1] for r in conn.execute("PRAGMA table_info(people)").fetchall()]
    if "phone" in pcols and "chat_id" not in pcols:
        print("[migration] people.phone -> people.chat_id (SMS -> Telegram)")
        conn.execute("ALTER TABLE people RENAME COLUMN phone TO chat_id")
        # Old phone numbers can't identify a Telegram chat. Clear them so nobody is
        # left half-bound; everyone re-registers with /start.
        conn.execute("UPDATE people SET chat_id = NULL")

    rcols = [r[1] for r in conn.execute("PRAGMA table_info(reminders)").fetchall()]
    if "for_phone" in rcols and "for_chat" not in rcols:
        print("[migration] reminders.for_phone -> reminders.for_chat (SMS -> Telegram)")
        conn.execute("ALTER TABLE reminders RENAME COLUMN for_phone TO for_chat")
        # Any reminder targeted at a phone number can no longer be delivered. Clear
        # the target so it falls back to a parent rather than failing forever.
        conn.execute("UPDATE reminders SET for_chat = NULL")
    conn.execute("""CREATE TABLE IF NOT EXISTS list_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_name TEXT NOT NULL,
        item TEXT NOT NULL,
        added_by TEXT,
        created_at TEXT NOT NULL)""")
    # Saved list templates, e.g. a reusable "travel" packing list.
    conn.execute("""CREATE TABLE IF NOT EXISTS list_templates (
        name TEXT PRIMARY KEY,
        items_json TEXT NOT NULL)""")
    # Shared household commitments — who agreed to do what, by when. This is family
    # logistics (not personal memory), so it's usable in the GROUP chat. Closes the loop:
    # "I'll grab Charlotte" -> recorded, and "who's got what?" -> answered.
    conn.execute("""CREATE TABLE IF NOT EXISTS commitments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task TEXT NOT NULL,
        who TEXT,
        when_text TEXT,
        created_by TEXT,
        done INTEGER DEFAULT 0,
        created_at TEXT NOT NULL)""")
    # Special occasions with escalating reminders (birthdays, anniversaries, vacations,
    # renewals). A daily job computes days-until and fires 90/30/7/1-day nudges with a
    # help offer (gift ideas / packing list / "take action"). month/day drive annual
    # recurrence; `year` is set only for one-offs (a specific vacation).
    conn.execute("""CREATE TABLE IF NOT EXISTS occasions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,          -- birthday | anniversary | holiday | vacation | renewal | other
        month INTEGER NOT NULL,
        day INTEGER NOT NULL,
        year INTEGER,                -- NULL = recurs every year
        for_chat TEXT,               -- whose briefing gets the nudges (NULL = all parents)
        notes TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL)""")
    conn.commit()
    for name, role in SEEDED_PEOPLE:
        conn.execute("INSERT OR IGNORE INTO people (name, role) VALUES (?, ?)", (name, role))
    conn.commit()
    conn.close()


def db():
    # timeout raises the busy-wait from the 5s default so overlapping writes from the
    # scheduler threads and webhook requests wait rather than erroring "database is locked"
    # (M3). WAL mode is set once at startup (init_db) for better read/write concurrency.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
#  BACKUP — protect the one SQLite file (reminders, lists, memory, AND OAuth
#  tokens) that lives on the Railway volume. If that file is lost, everyone has
#  to reconnect every account. We push a safe copy to the primary parent's
#  Telegram on a schedule (and on demand), so a backup lands somewhere the family
#  controls without any new service, library, or key.
# =============================================================================
# ---- Short-lived signed links for /backup and /restore -----------------------
# The setup secret used to ride in the query string (/backup?secret=...), which wrote it
# into Railway's request logs, browser history, and any proxy in between. That same
# secret is the HMAC key that signs the OAuth state, so leaking it was worse than losing
# one download. Instead: ask Guppi for a link in Telegram and it mints a token that is
#   * signed      - HMAC over the payload, so it cannot be forged;
#   * expiring    - 15 minutes by default;
#   * single-use  - the nonce is burned on first use;
#   * purpose-bound - a download token is rejected by /restore, and vice versa.
# NOTE: /set-webhook deliberately still uses the raw secret. It has to: it is what makes
# the bot reachable in the first place, so you cannot ask Guppi for a link until after
# it has been run. Bootstrapping beats purity for a one-time endpoint.
BACKUP_LINK_MINUTES = 15


def _make_access_token(purpose, minutes=BACKUP_LINK_MINUTES):
    """Mint a signed, expiring, single-use token for one specific endpoint."""
    import secrets as _secrets
    if not TELEGRAM_SETUP_SECRET:
        return None
    exp = int(time.time()) + minutes * 60
    # token_hex, NOT token_urlsafe: urlsafe emits "_" and "-", and an underscore inside a
    # link makes Telegram's Markdown parser reject the whole message (HTTP 400). The
    # plain-text retry rescued it every time, but it logged an error on every send and
    # would have silently degraded formatting. Hex is 0-9a-f - nothing Markdown reacts to.
    nonce = _secrets.token_hex(12)
    payload = f"{purpose}:{exp}:{nonce}"
    sig = hmac.new(TELEGRAM_SETUP_SECRET.encode(), payload.encode(),
                   "sha256").hexdigest()[:24]
    return f"{payload}:{sig}"


def _used_token_key(nonce):
    """Burned-nonce marker. The embedded date is deliberate: _prune_old_settings sweeps
    dated keys older than 10 days, so used tokens clean themselves up instead of growing
    the settings table forever (the same trap as M7/Trap 47)."""
    return f"usedtok_{now_local().strftime('%Y-%m-%d')}_{nonce}"


def _verify_access_token(token, purpose, single_use=True):
    """True only for a token we signed, for THIS purpose, unexpired and not yet used.
    Checks run cheapest-first, and the nonce is burned ONLY after the signature passes -
    otherwise a stranger could burn valid nonces by guessing.

    single_use=False for the CONNECT links. Single-use cannot survive that flow: the first
    request only redirects to Google/Microsoft, and anything that pre-fetches a URL - a
    link-preview crawler, a browser, an antivirus scanner, or the person simply hitting
    back and trying again - consumes the token before the real sign-in happens. Those
    links stay signed, person-bound and 30-minute, which is what actually protects them;
    burning them on first touch only guaranteed they never worked.
    The backup/restore links stay single-use: there the first request IS the download."""
    if not TELEGRAM_SETUP_SECRET or not token:
        return False
    try:
        got_purpose, exp_s, nonce, sig = token.split(":", 3)
    except (ValueError, AttributeError):
        return False
    payload = f"{got_purpose}:{exp_s}:{nonce}"
    expect = hmac.new(TELEGRAM_SETUP_SECRET.encode(), payload.encode(),
                      "sha256").hexdigest()[:24]
    if not hmac.compare_digest(sig, expect):
        print("[link] rejected: bad signature")
        return False
    if not hmac.compare_digest(got_purpose, purpose):
        print(f"[link] rejected: wrong purpose ({got_purpose} used on {purpose})")
        return False
    try:
        if int(exp_s) < int(time.time()):
            print("[link] rejected: expired")
            return False
    except ValueError:
        return False
    if single_use:
        if get_setting(_used_token_key(nonce)):
            print("[link] rejected: already used")
            return False
        set_setting(_used_token_key(nonce), "1")
    return True


def _connect_purpose(person):
    """Token purpose for a connect link, bound to ONE person so a link minted for Kim can
    never be replayed to attach somebody else's account under a different name."""
    safe = "".join(ch for ch in (person or "").lower() if ch.isalnum())
    return f"connect-{safe}"


def tool_connect_link(person, kind="google"):
    """A private, working link for one person to connect or reconnect an account.

    A1: the H2 security fix made the setup secret mandatory on /connect and
    /connect-microsoft, but every place that HANDED OUT those links kept emitting the old
    secret-less URL. Kim clicked one and got 403 Forbidden, which is why she still can't
    reconnect. Pasting the raw secret instead would be worse - it must never land in a
    group chat - so the link carries a signed, single-use, person-bound token, exactly
    like the backup link.

    30 minutes rather than 15: signing in to Google or Microsoft takes longer than
    clicking a download."""
    if not person:
        return "Tell me WHO is connecting (a first name) and I'll make them a link."
    token = _make_access_token(_connect_purpose(person), minutes=30)
    if not token:
        return ("I can't make a secure link - the setup secret isn't configured on the "
                "server, and that's what signs the link.")
    k = (kind or "google").strip().lower()
    if k in ("microsoft", "outlook", "live", "hotmail", "ms"):
        url = f"{BASE_URL}/connect-microsoft?person={urllib.parse.quote(person)}&token={token}"
        which = "Outlook/live.com email"
    else:
        url = f"{BASE_URL}/connect?person={urllib.parse.quote(person)}&token={token}"
        which = "Google (calendar + Gmail)"
    return (f"Here's {person}'s link to connect {which}. It works once and expires in 30 "
            f"minutes:\n\n{url}\n\nOpen it in a browser, sign in, and approve access. "
            f"Send this in a PRIVATE chat only - never in the family group.")


def tool_backup_link(kind="download"):
    """Give a parent a one-shot link to download a backup, or the command to restore one."""
    purpose = "restore" if kind == "restore" else "backup"
    token = _make_access_token(purpose)
    if not token:
        return ("I can't make a secure link - the setup secret isn't configured on the "
                "server, and that's what signs the link.")
    if purpose == "backup":
        return (f"Here's your backup download link. It works ONCE and expires in "
                f"{BACKUP_LINK_MINUTES} minutes:\n\n{BASE_URL}/backup?token={token}\n\n"
                f"It saves the database file straight to whatever device you tap it on. "
                f"Keep it somewhere safe - it contains the credentials for every "
                f"connected account.")
    return (f"Restore link - single use, expires in {BACKUP_LINK_MINUTES} minutes. This "
            f"OVERWRITES everything currently stored, so only use it if the live data is "
            f"already lost.\n\nRun this in PowerShell with your backup file's path:\n\n"
            f'curl.exe -F "file=@C:\\path\\to\\guppi-backup.db" '
            f'"{BASE_URL}/restore?token={token}"')


def make_db_snapshot():
    """Make a CONSISTENT copy of the live DB. You cannot just copy the file — a copy
    taken mid-write can be corrupt. SQLite's backup API takes a proper point-in-time
    snapshot even while the app is using the DB. Returns the snapshot path or None."""
    import tempfile
    try:
        stamp = now_local().strftime("%Y%m%d-%H%M")
        snap_path = os.path.join(tempfile.gettempdir(), f"guppi-backup-{stamp}.db")
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(snap_path)
        with dst:
            src.backup(dst)          # atomic, consistent, safe during writes
        src.close(); dst.close()
        return snap_path
    except Exception as e:
        print(f"[backup] snapshot failed: {e}")
        return None


def _tg_send_document(chat_id, file_path, caption=""):
    """Send a file to a Telegram chat via multipart/form-data (telegram_api only does
    JSON, so document upload needs its own multipart request).

    Returns the sent message_id, or None on failure. The id is what lets run_backup
    delete the PREVIOUS backup once this one has landed."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        filename = os.path.basename(file_path)
        boundary = "----GuppiBackup" + now_local().strftime("%Y%m%d%H%M%S")
        parts = []
        # chat_id field
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="chat_id"\r\n\r\n{chat_id}\r\n')
        if caption:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="caption"\r\n\r\n{caption}\r\n')
        pre = "".join(parts).encode()
        file_header = (f"--{boundary}\r\nContent-Disposition: form-data; "
                       f'name="document"; filename="{filename}"\r\n'
                       f"Content-Type: application/octet-stream\r\n\r\n").encode()
        closing = f"\r\n--{boundary}--\r\n".encode()
        payload = pre + file_header + file_bytes + closing
        req = urllib.request.Request(
            f"{TELEGRAM_API}/sendDocument", data=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read())
        if not body.get("ok"):
            print(f"[backup] sendDocument failed: {body}")
            return None
        return (body.get("result") or {}).get("message_id")
    except Exception as e:
        print(f"[backup] sendDocument error: {e}")
        return None


def _retire_previous_backup(chat_id, new_msg_id):
    """Delete the previously-sent backup message, then record the new one as current.

    Best-effort by design: if the old message is already gone, or is past Telegram's
    48-hour deletion window, we log it and move on. A failed cleanup must never cost us
    the new backup we just successfully sent."""
    try:
        prev = get_setting("last_backup_msg_id")
        prev_chat = get_setting("last_backup_chat") or chat_id
        if prev and str(prev) != str(new_msg_id):
            res = telegram_api("deleteMessage",
                               {"chat_id": prev_chat, "message_id": int(prev)})
            if res is None:
                print(f"[backup] could not delete previous backup msg {prev} "
                      f"(past the 48h window, or already gone) - leaving it")
            else:
                print(f"[backup] removed previous backup msg {prev}")
    except Exception as e:
        print(f"[backup] retire-previous failed (non-fatal): {e}")
    try:
        set_setting("last_backup_msg_id", str(new_msg_id))
        set_setting("last_backup_chat", str(chat_id))
    except Exception as e:
        print(f"[backup] could not record current backup id: {e}")


def run_backup(reason="scheduled"):
    """Snapshot the DB and push it to the primary parent's Telegram, then remove the
    PREVIOUS one, so the chat holds a single current backup instead of a growing archive
    of credential dumps (every one of those files contains every connected account's
    tokens). Returns a status string. Used by the daily job and 'back up now'.

    Order matters: the old file is deleted only AFTER the new one is confirmed sent, so
    there is never a moment when the chat holds zero backups.

    Telegram only lets a bot delete its own messages for 48 hours, so one-in-one-out is
    the most pruning the API allows - a 'delete anything older than a week' sweep is not
    buildable. Backups you want to KEEP should be pulled to your own machine with
    "send me a backup link", which never leaves a copy sitting in Telegram at all."""
    target = _first_parent_chat()
    if not target:
        return "No parent is connected yet, so there's nowhere safe to send a backup."
    snap = make_db_snapshot()
    if not snap:
        return "The backup couldn't be created just now. I'll try again on the next cycle."
    size_kb = max(1, os.path.getsize(snap) // 1024)
    when = now_local().strftime("%A %b %-d, %-I:%M %p")
    caption = (f"Guppi backup ({reason}) — {when}. {size_kb} KB. "
               f"Keep this file safe; it can restore everything (accounts, reminders, "
               f"lists, memory) if the server data is ever lost. This replaces the "
               f"previous backup — ask me for a backup link if you want one to keep.")
    msg_id = _tg_send_document(target, snap, caption)
    try:
        os.remove(snap)
    except Exception:
        pass
    if msg_id:
        _retire_previous_backup(target, msg_id)
        print(f"[backup] sent ({reason}, {size_kb}KB, msg {msg_id})")
        return f"Backup done — I sent the database file to your chat ({size_kb} KB)."
    return "The backup was created but couldn't be sent. I'll retry next cycle."


def _prune_old_settings(days=10):
    """M7: dated counter/dedupe keys (count_*, cap_warned_*, occasion_fired_*,
    flagged_deadlines_* etc.) accumulate forever otherwise. Delete ones whose embedded
    date is older than `days`. Conservative — only touches keys with a recognizable date."""
    import re as _re
    cutoff = (now_local().date() - datetime.timedelta(days=days))
    conn = db()
    try:
        rows = conn.execute("SELECT key FROM settings").fetchall()
        gone = 0
        for row in rows:
            k = row["key"]
            m = _re.search(r"(\d{4}-\d{2}-\d{2})", k)
            if not m:
                continue
            try:
                d = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if d < cutoff:
                conn.execute("DELETE FROM settings WHERE key = ?", (k,))
                gone += 1
        conn.commit()
        if gone:
            print(f"[cleanup] pruned {gone} old dated settings keys")
    except Exception as e:
        print(f"[cleanup] settings prune failed: {e}")
    finally:
        conn.close()


def job_daily_backup():
    """Once a day: push a DB snapshot to the primary parent, and prune stale settings.
    Wrapped so a failure is logged and never destabilizes the scheduler."""
    try:
        run_backup(reason="daily automatic")
    except Exception as e:
        print(f"[backup] daily job error: {e}")
    try:
        _prune_old_settings()
    except Exception as e:
        print(f"[cleanup] daily prune error: {e}")


# =============================================================================
#  WHO IS TEXTING?  (identity + permissions)
# =============================================================================
def identify_sender(chat_id):
    """Return (name, role) for this Telegram chat id.

    Security: a chat id is only an adult if it was BOUND with the setup secret
    (see /start handling). Nobody can talk their way into an adult role. Anyone
    unbound is 'unknown' and gets no access to family data at all.
    """
    if not chat_id:
        return None, "unknown"
    conn = db()
    row = conn.execute("SELECT name, role FROM people WHERE chat_id = ?",
                       (str(chat_id),)).fetchone()
    conn.close()
    if row:
        return row["name"], row["role"]
    return None, "unknown"


def bind_adult(chat_id, supplied_secret):
    """Bind a chat to an adult slot using the setup secret. This REPLACES the
    phone-number env vars of the SMS version as the security boundary.

    Returns a message to send back. The secret must match exactly, and there must be
    a free adult slot (an adult in the roster with no chat bound yet)."""
    if not TELEGRAM_SETUP_SECRET:
        return "Setup isn't configured yet. A parent needs to set the setup secret."
    if not _secret_ok(supplied_secret):
        print(f"[security] bad setup secret from chat {chat_id}")
        return "That setup code isn't right."
    conn = db()
    # Already bound?
    row = conn.execute("SELECT name FROM people WHERE chat_id = ?", (str(chat_id),)).fetchone()
    if row:
        conn.close()
        return f"You're already set up, {row['name']}."
    free = conn.execute(
        "SELECT name FROM people WHERE role = 'adult' AND chat_id IS NULL "
        "ORDER BY id LIMIT 1").fetchone()
    if not free:
        conn.close()
        return "Both parent slots are already taken."
    conn.execute("UPDATE people SET chat_id = ? WHERE name = ?",
                 (str(chat_id), free["name"]))
    conn.commit()
    conn.close()
    print(f"[setup] bound adult {free['name']} to chat {chat_id}")
    return welcome_message(free["name"], "adult")


def welcome_message(name, role):
    """A warm, role-appropriate first message so a newly-connected person knows what
    they can do right away — rather than a bare 'you're all set'. This is onboarding:
    the first impression, and the thing that makes the feature discoverable."""
    intro = f"You're all set, {name}! I'm Guppi, the family assistant. "
    if role == "adult":
        body = ("Here's what I can do for you:\n"
                "• Calendar — check, add, move, or cancel events\n"
                "• Reminders — one-time or recurring, for you or others\n"
                "• Occasions — tell me a birthday/anniversary/trip and I'll remind you "
                "ahead of time with gift or packing help\n"
                "• Lists — shared grocery/to-do lists and reusable templates\n"
                "• Email — connect your inbox and I'll search it, flag deadlines, and even "
                "draft & send replies (I always show you first)\n"
                "• Travel — give me a flight number and I'll build the trip on your calendar\n"
                "• Photos & PDFs — send a school flyer and I'll pull out the events\n\n"
                "I'll also send a morning briefing.")
    elif role == "caregiver":
        body = ("Here's what I can help with:\n"
                "• Calendar — \"what's on the kids' schedule today?\", \"add gymnastics "
                "Tuesday at 4\"\n"
                "• Reminders — \"remind me to pack Lillian's cleats Friday morning\"\n"
                "• Lists — \"add snacks to the shopping list\"\n"
                "• Photos — send me a flyer and I'll offer to add it to the calendar")
    else:  # child
        body = ("I can help you with the family calendar and reminders!\n"
                "• \"What's on the calendar this weekend?\"\n"
                "• \"Remind me about my science project Thursday\"\n"
                "• You can ask me questions too.")
    # Every new person learns on day one that a complete guide exists. This is the single
    # highest-value line in the whole onboarding: the barrier was never the features, it
    # was knowing what to SAY to trigger them.
    tail = ("\n\nSay *guide* any time and I'll show you everything I can do, topic by "
            "topic, with the exact words to use. You don't have to phrase things exactly "
            "right - just ask me in your own words and I'll work it out.")
    return intro + body + tail


def welcome_for(name):
    conn = db()
    row = conn.execute("SELECT role FROM people WHERE name = ?", (name,)).fetchone()
    conn.close()
    return welcome_message(name, row["role"] if row else "child")


def claim_pending(chat_id):
    """A non-adult (child/caregiver) sends /start after a parent invited them by name.
    Binds them to the oldest open invite. No secret needed — a parent already vouched."""
    conn = db()
    row = conn.execute("SELECT name FROM people WHERE chat_id = ?", (str(chat_id),)).fetchone()
    if row:
        conn.close()
        return f"You're already set up, {row['name']}."
    pend = conn.execute(
        "SELECT name FROM pending_links ORDER BY created_at LIMIT 1").fetchone()
    if not pend:
        conn.close()
        return None  # nothing pending; caller decides what to say
    name = pend["name"]
    conn.execute("UPDATE people SET chat_id = ? WHERE name = ?", (str(chat_id), name))
    conn.execute("DELETE FROM pending_links WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    print(f"[setup] bound {name} to chat {chat_id} via pending invite")
    return welcome_for(name)


def link_person(name, requester_role):
    """A parent invites a family member by NAME. That person then sends /start to the
    bot and gets bound. Parents can't be invited this way — they use the secret."""
    if requester_role != "adult":
        return "Only a parent can set up who's who."
    conn = db()
    row = conn.execute("SELECT role, chat_id FROM people WHERE LOWER(name) = ?",
                       (name.strip().lower(),)).fetchone()
    if not row:
        conn.close()
        return f"I don't have anyone named {name} on the family list."
    if row["role"] == "adult":
        conn.close()
        return f"{name} is a parent - they set themselves up with the setup code."
    if row["chat_id"]:
        conn.close()
        return f"{name} is already set up."
    conn.execute("INSERT OR REPLACE INTO pending_links (name, created_at) VALUES (?, ?)",
                 (name.strip().title(), now_local().isoformat()))
    conn.commit()
    conn.close()
    return (f"Okay - have {name.title()} open a chat with me and send /start. "
            f"I'll link them automatically.")


def save_google_token(person, creds):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO google_tokens (person, token_json) VALUES (?, ?)",
                 (person, creds.to_json()))
    conn.commit()
    conn.close()


def load_google_token(person):
    """Load one person's credentials, refreshing if expired. None if not connected.

    If Google has permanently revoked the token (invalid_grant — happens when the OAuth
    app is unpublished and Google expires tokens after 7 days, or the user revoked
    access), we record that so Guppi can say 'reconnect' instead of the misleading
    'you never connected'. A transient network error is NOT treated as revocation."""
    if not person:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT token_json FROM google_tokens WHERE person = ?",
                       (person,)).fetchone()
    conn.close()
    if not row:
        print(f"[gtoken] {person}: no row in google_tokens")
        return None
    try:
        info = json.loads(row[0])
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    except Exception as e:
        print(f"[gtoken] {person}: could not build creds: {e}")
        return None
    # Don't hammer a token already known to be dead - same short-circuit Microsoft has.
    if google_needs_reconnect(person):
        return None
    if creds and not creds.valid and creds.refresh_token:
        # Refresh whenever the creds aren't currently valid (covers 'expired' AND other
        # not-valid states), not only when flagged expired.
        try:
            creds.refresh(GoogleRequest())
            save_google_token(person, creds)
            set_setting(f"google_dead_{person}", "")   # healthy again
        except Exception as e:
            # A2: only invalid_grant used to count as dead, so Kim's token - which fails
            # with invalid_scope because SCOPES gained gmail.send after she connected -
            # retried on every scheduler tick forever (~15 times in one evening) and
            # google_needs_reconnect() kept reporting she was fine.
            txt = str(e).lower()
            dead = any(k in txt for k in (
                "invalid_grant", "invalid_scope", "invalid_client",
                "unauthorized_client", "expired or revoked", "token has been revoked"))
            if dead:
                set_setting(f"google_dead_{person}", "1")
                print(f"[gtoken] {person}: token is DEAD ({e}) - needs a reconnect; "
                      f"staying quiet until it is fixed")
            else:
                print(f"Token refresh failed for {person} (transient?): {e}")
            return None
    return creds


def google_needs_reconnect(person):
    return get_setting(f"google_dead_{person}") == "1"


def any_connected_adult():
    """Name of any adult with a working Google connection. Used ONLY for the shared
    family calendar — never for email."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT person FROM google_tokens").fetchall()
    conn.close()
    for (person,) in rows:
        if load_google_token(person):
            return person
    return None


def make_flow():
    client_config = {"web": {
        "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI]}}
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)


def _make_oauth_state(person):
    """A signed, unguessable state value: person + random nonce + HMAC over both, so the
    callback can verify it originated from us (CSRF) and wasn't crafted by a stranger."""
    import secrets as _secrets
    nonce = _secrets.token_urlsafe(16)
    payload = f"{person}:{nonce}"
    sig = hmac.new(TELEGRAM_SETUP_SECRET.encode(), payload.encode(),
                   "sha256").hexdigest()[:16]
    return f"{payload}:{sig}"


def _verify_oauth_state(state):
    """Return the person name if the state is a valid signed token we issued, else None."""
    try:
        person, nonce, sig = state.split(":", 2)
    except (ValueError, AttributeError):
        return None
    payload = f"{person}:{nonce}"
    expect = hmac.new(TELEGRAM_SETUP_SECRET.encode(), payload.encode(),
                      "sha256").hexdigest()[:16]
    return person if hmac.compare_digest(sig, expect) else None


@app.get("/connect")
def connect(person: str = "", secret: str = "", token: str = ""):
    """Connect a Google account. Visit /connect?person=Jason&secret=<SETUP_SECRET>.

    Requires the setup secret so a stranger who finds the URL can't bind their OWN account
    under a family member's name (which would make Guppi read the attacker's inbox). The
    name rides in a SIGNED 'state' value, verified at the callback (CSRF protection)."""
    # Accept EITHER the raw setup secret (bootstrapping, and older bookmarks) or a signed
    # per-person token minted by the connect_link tool (A1).
    if not (_secret_ok(secret) or
            (person and _verify_access_token(token, _connect_purpose(person), single_use=False))):
        return HTMLResponse(
            "<h2>This sign-in link is invalid or expired</h2><p>Ask Guppi for a new "
            "connect link in a private chat - they expire after 30 minutes and work "
            "once.</p>", status_code=403)
    if not person:
        return HTMLResponse(
            "<h2>Who is connecting?</h2>"
            "<p>Add your name, e.g. <code>/connect?person=Jason&secret=YOUR_CODE</code></p>")
    flow = make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true",
        state=_make_oauth_state(person))
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
def oauth_callback(request: Request, state: str = ""):
    # H2: only accept a state value we signed ourselves — rejects CSRF and stranger-crafted
    # callbacks that would bind an attacker's account under a family name.
    person = _verify_oauth_state(state)
    if not person:
        return HTMLResponse("<h2>This sign-in link is invalid or expired.</h2>",
                            status_code=403)
    flow = make_flow()
    # Railway serves HTTPS at its edge but forwards internally as HTTP; the OAuth
    # library refuses non-HTTPS. Rebuild as https (it IS secure end to end).
    callback_url = str(request.url).replace("http://", "https://", 1)
    flow.fetch_token(authorization_response=callback_url)
    save_google_token(person, flow.credentials)
    print(f"[oauth] saved Google token for {person}")
    safe = html_lib.escape(person)
    return HTMLResponse(f"<h2>Guppi is connected to {safe}'s Google account.</h2>"
                        "<p>You can close this window.</p>")


def get_calendar_service(person=None):
    """Shared family calendar: prefer the requester's own token, else any adult's."""
    creds = load_google_token(person) if person else None
    if not creds:
        fallback = any_connected_adult()
        creds = load_google_token(fallback) if fallback else None
    if not creds:
        print(f"[cal] no usable Google credentials at all")
        return None
    try:
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(f"[cal] build() failed: {e}")
        return None


def get_gmail_service(person):
    """Email ALWAYS uses the requesting person's own token. Never falls back to
    someone else's account — one adult must never read another's inbox."""
    creds = load_google_token(person)
    return build("gmail", "v1", credentials=creds) if creds else None




# ---- Settings (adjustable by text, parents only) ----------------------------
DEFAULT_SETTINGS = {
    "daily_claude_calls_cap": "35",   # proactive Claude calls per day
    "daily_messages_cap": "10",       # proactive messages per day (noise, not cost)
    "poll_minutes": "30",             # how often to check for urgent email
    "quiet_start_hour": "22",         # 10pm — stop proactive activity
    "quiet_end_hour": "6",            # 6am  — resume (briefing goes out at 6am)
    "proactive_enabled": "true",      # master kill switch
}


def get_setting(key):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


def set_setting(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                 (key, str(value)))
    conn.commit()
    conn.close()
    return f"Set {key} to {value}."


def tool_show_settings():
    lines = []
    for k in DEFAULT_SETTINGS:
        lines.append(f"{k}: {get_setting(k)}")
    return "\n".join(lines)


def tool_update_setting(key, value, requester_role):
    """Parents only. Guards against typos and nonsense values."""
    if requester_role != "adult":
        return "Only a parent can change Guppi's settings."
    if key not in DEFAULT_SETTINGS:
        return f"I don't have a setting called '{key}'. Known: {', '.join(DEFAULT_SETTINGS)}"
    # Numeric settings must be sane positive numbers; caps have hard ceilings so a
    # typo (or a persuasive request) can't remove the safety net entirely.
    ceilings = {"daily_claude_calls_cap": 500, "daily_messages_cap": 50,
                "poll_minutes": 1440, "quiet_start_hour": 23, "quiet_end_hour": 23}
    if key in ceilings:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return f"{key} needs to be a whole number."
        if n < 0 or n > ceilings[key]:
            return f"{key} must be between 0 and {ceilings[key]}."
        value = n
    elif key == "proactive_enabled":
        if str(value).lower() not in ("true", "false"):
            return "proactive_enabled must be true or false."
        value = str(value).lower()
    return set_setting(key, value)


# =============================================================================
#  TOOL IMPLEMENTATIONS
# =============================================================================
def tool_list_calendars(person=None):
    """List the calendars this account can see, with their IDs. Used once during
    setup to find the shared family calendar's id."""
    service = get_calendar_service(person)
    if not service:
        return "No Google account is connected yet."
    result = service.calendarList().list().execute()
    items = result.get("items", [])
    if not items:
        return "No calendars found."
    lines = []
    for c in items:
        access = c.get("accessRole", "?")
        primary = " (your primary)" if c.get("primary") else ""
        lines.append(f"{c.get('summary','(untitled)')}{primary} — id: {c['id']} — access: {access}")
    return "\n".join(lines)


def tool_check_calendar(days_ahead=7, person=None):
    service = get_calendar_service(person)
    if not service:
        if person and google_needs_reconnect(person):
            return (f"{person}'s Google access expired and needs to be reconnected. A "
                    f"parent should reopen the connect link and sign in again.")
        return "The Google account isn't connected yet."
    now = now_local()
    later = now + datetime.timedelta(days=days_ahead)
    # Look slightly BACK as well, so an event that started earlier and is still ongoing
    # (e.g. an overnight sleepover ending this morning) is included and can be recognized
    # as in-progress — the action there is pickup/completion, not preparation.
    window_start = now - datetime.timedelta(hours=18)
    try:
        result = service.events().list(
            calendarId=FAMILY_CALENDAR_ID, timeMin=window_start.isoformat(),
            timeMax=later.isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=20).execute()
    except Exception as e:
        print(f"[cal] events.list failed: {e}")
        return (f"I reached your calendar but couldn't read it: {e}. This can mean the "
                f"calendar ID is wrong or access wasn't granted for calendar.")
    events = result.get("items", [])
    if not events:
        return f"No events in the next {days_ahead} days."

    lines = []
    for e in events:
        start_raw = e["start"].get("dateTime", e["start"].get("date"))
        end_raw = e.get("end", {}).get("dateTime", e.get("end", {}).get("date"))
        summary = e.get("summary", "(no title)")
        status = ""
        # Work out whether the event is happening RIGHT NOW, already ended, or upcoming,
        # so the briefing can act sensibly (pick up vs. pack for vs. head to).
        try:
            sdt = datetime.datetime.fromisoformat(start_raw)
            edt = datetime.datetime.fromisoformat(end_raw) if end_raw else None
            if sdt.tzinfo is None:
                sdt = sdt.replace(tzinfo=TIMEZONE)
            if edt and edt.tzinfo is None:
                edt = edt.replace(tzinfo=TIMEZONE)
            if edt and sdt <= now < edt:
                status = " [IN PROGRESS NOW — started earlier, ends " + \
                         edt.strftime("%a %-I:%M %p") + "; likely action is pickup/finish, " \
                         "not prep]"
            elif sdt < now and (not edt or edt <= now):
                status = " [already ended]"
        except (ValueError, TypeError):
            pass
        when = start_raw + (f" to {end_raw}" if end_raw else "")
        lines.append(f"{when}: {summary}{status}")
    return "\n".join(lines)


def _cal_guard(service, person):
    """Shared not-connected check for calendar write ops. Returns an error string or None."""
    if service:
        return None
    if person and google_needs_reconnect(person):
        return (f"{person}'s Google access expired and needs reconnecting. A parent "
                f"should sign in again via the connect link.")
    return "The Google account isn't connected yet."


def tool_find_events(query=None, days_ahead=30, person=None):
    """Find upcoming events, returning each with its ID so it can be edited or deleted.
    Optional `query` matches text in the title. This is how Guppi locates the specific
    event before changing it — it never guesses an ID."""
    service = get_calendar_service(person)
    err = _cal_guard(service, person)
    if err:
        return err
    now = now_local()
    later = now + datetime.timedelta(days=days_ahead)
    try:
        result = service.events().list(
            calendarId=FAMILY_CALENDAR_ID, timeMin=now.isoformat(),
            timeMax=later.isoformat(), singleEvents=True, orderBy="startTime",
            maxResults=50, q=query if query else None).execute()
    except Exception as e:
        print(f"[cal] find_events failed: {e}")
        return f"Couldn't search the calendar: {e}"
    events = result.get("items", [])
    if not events:
        return "No matching events found."
    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        loc = f" @ {e['location']}" if e.get("location") else ""
        # An instance of a repeating event has its own id: deleting it removes ONLY that
        # day. Flag it so Guppi can ask "just this one, or the whole series?" rather than
        # silently removing one of four camp days and reporting the job done.
        series = " | REPEATING SERIES (this id is one occurrence)" if e.get(
            "recurringEventId") else ""
        lines.append(
            f"id={e['id']} | {start} | {e.get('summary','(no title)')}{loc}{series}")
    return ("Matching events (use the id to edit or delete). If an event is marked "
            "REPEATING SERIES, ask whether they mean just that day or all of them - "
            "pass whole_series=true to delete_calendar_event for all of them.\n"
            + "\n".join(lines))


def tool_edit_calendar_event(event_id, person=None, summary=None, start_iso=None,
                             end_iso=None, location=None, details=None):
    """Change fields on an existing event. Only the fields provided are changed; the
    rest are left as they are. event_id comes from find_events."""
    service = get_calendar_service(person)
    err = _cal_guard(service, person)
    if err:
        return err
    tzname = str(TIMEZONE)
    try:
        ev = service.events().get(calendarId=FAMILY_CALENDAR_ID,
                                  eventId=event_id).execute()
    except Exception as e:
        print(f"[cal] edit get failed: {e}")
        return "I couldn't find that event - it may have been deleted. Try finding it again."
    if summary is not None:
        ev["summary"] = summary
    if start_iso is not None:
        ev["start"] = {"dateTime": start_iso, "timeZone": tzname}
    if end_iso is not None:
        ev["end"] = {"dateTime": end_iso, "timeZone": tzname}
    if location is not None:
        ev["location"] = location
    if details is not None:
        # Preserve the Guppi marker if present, replace the detail portion.
        stamp = now_local().strftime("%b %d, %Y at %I:%M %p")
        who = person or "a family member"
        marker = f"— Edited by Guppi (requested by {who} on {stamp})."
        ev["description"] = f"{details}\n\n{marker}"
    try:
        updated = service.events().update(
            calendarId=FAMILY_CALENDAR_ID, eventId=event_id, body=ev).execute()
    except Exception as e:
        print(f"[cal] edit update failed: {e}")
        return f"Couldn't update the event: {e}"
    when = updated["start"].get("dateTime", updated["start"].get("date"))
    return f"Updated '{updated.get('summary','(no title)')}' — now {when}."


def tool_delete_calendar_event(event_id, person=None, whole_series=False):
    """Delete an event by id (from find_events).

    whole_series matters for repeating events: find_events returns one id PER OCCURRENCE,
    so deleting that id removes a single day. Deleting the series means deleting the
    parent event, which Google exposes as `recurringEventId` on each instance."""
    service = get_calendar_service(person)
    err = _cal_guard(service, person)
    if err:
        return err
    # Fetch the title first so the confirmation is meaningful - and, for a repeating
    # event, find the parent id before we delete the instance that points at it.
    title = "the event"
    parent_id = None
    try:
        ev = service.events().get(calendarId=FAMILY_CALENDAR_ID,
                                  eventId=event_id).execute()
        title = f"'{ev.get('summary','(no title)')}'"
        parent_id = ev.get("recurringEventId")
    except Exception:
        pass

    target, scope = event_id, "that event"
    if whole_series:
        if parent_id:
            target, scope = parent_id, "every occurrence of that repeating event"
        else:
            scope = "that event (it wasn't part of a repeating series)"

    try:
        service.events().delete(calendarId=FAMILY_CALENDAR_ID, eventId=target).execute()
    except Exception as e:
        print(f"[cal] delete failed (target={target}, series={whole_series}): {e}")
        return "I couldn't delete that - it may already be gone. Try finding it again."
    if whole_series and parent_id:
        return f"Deleted {title} - the whole repeating series, not just one day."
    if parent_id:
        return (f"Deleted one occurrence of {title}. It repeats, so the other dates are "
                f"still there - say so if you want the whole series gone.")
    return f"Deleted {title} from the calendar."


def _lookup_flight(flight_number, date_iso, _retry=False):
    """Look up one flight by number + date via AeroDataBox. Returns a dict with
    departure/arrival airport codes and scheduled local ISO times, or None. Fails soft:
    any error (no key, not found, network) returns None so the caller can ask the user."""
    if not FLIGHT_API_KEY:
        return None
    fn = flight_number.replace(" ", "").upper()
    url = f"https://{FLIGHT_API_HOST}/flights/number/{urllib.parse.quote(fn)}/{date_iso}"
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": FLIGHT_API_KEY,
        "X-RapidAPI-Host": FLIGHT_API_HOST,
        # Cloudflare (in front of the API) returns 403 error 1010 for requests without a
        # normal browser User-Agent. urllib's default ("Python-urllib/x") gets flagged as
        # a bot, so we present a standard browser UA to pass the integrity check.
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            body = ""
        # A 429 is a transient rate-limit (Basic plan is ~1 req/sec), not a real miss.
        # Wait and retry once before giving up, so a throttle doesn't drop the flight.
        if e.code == 429 and not _retry:
            print(f"[flight] rate-limited on {fn}; retrying once")
            time.sleep(1.5)
            return _lookup_flight(flight_number, date_iso, _retry=True)
        print(f"[flight] lookup failed for {fn} {date_iso}: HTTP {e.code} - {body}")
        return None
    except Exception as e:
        print(f"[flight] lookup failed for {fn} {date_iso}: {e}")
        return None
    # The endpoint returns a list (a number can have multiple legs/segments a day).
    flights = data if isinstance(data, list) else data.get("flights") or []
    if not flights:
        print(f"[flight] no results for {fn} {date_iso}")
        return None
    f = flights[0]
    dep = f.get("departure", {}) or {}
    arr = f.get("arrival", {}) or {}

    def _airport(side):
        ap = side.get("airport", {}) or {}
        return ap.get("iata") or ap.get("icao") or ap.get("name") or "?"

    def _sched_time(side):
        # AeroDataBox gives scheduledTime: {local: "2026-07-22 08:00-04:00", utc: "..."}.
        # Google Calendar needs full ISO 8601 WITH seconds ("2026-07-22T08:00:00-04:00");
        # a missing :SS is a 400 Bad Request. Parse it robustly and re-emit clean ISO.
        st = side.get("scheduledTime") or side.get("revisedTime") or {}
        local = st.get("local") if isinstance(st, dict) else None
        if not local:
            return None
        s = local.strip().replace(" ", "T", 1)
        # Split off the timezone offset (+HH:MM / -HH:MM) so we can normalize the time part.
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)\s*([+-]\d{2}:?\d{2}|Z)?$", s)
        if not m:
            return s  # last resort: hand back what we have
        stamp, offset = m.group(1), m.group(2) or ""
        if stamp.count(":") == 1:      # HH:MM -> HH:MM:SS
            stamp += ":00"
        if offset and offset != "Z" and ":" not in offset:  # +0400 -> +04:00
            offset = offset[:3] + ":" + offset[3:]
        return stamp + offset

    result = {
        "number": fn,
        "dep_airport": _airport(dep), "arr_airport": _airport(arr),
        "dep_time": _sched_time(dep), "arr_time": _sched_time(arr),
        "airline": (f.get("airline", {}) or {}).get("name", ""),
    }
    # A5: the ONLY flight logging was on failure, so when AA5121 came back with the
    # wrong time there was no way to tell whether the API was wrong, our timezone handling
    # was wrong, or the model invented it. Log what we actually received.
    print(f"[flight] {fn} {date_iso} -> dep {result.get('dep_airport')} "
          f"{result.get('dep_time')} / arr {result.get('arr_airport')} "
          f"{result.get('arr_time')}")
    return result


def tool_add_flight(outbound_number, outbound_date, person=None,
                    return_number=None, return_date=None):
    """Add a work trip to the calendar from flight numbers. Looks up each flight's real
    airports and times, then creates: (1) a trip-block event spanning from 1 hour before
    the outbound departure (airport buffer) to the return arrival, and (2) an individual
    event for each flight. Falls back to asking for details if lookup isn't available."""
    service = get_calendar_service(person)
    err = _cal_guard(service, person)
    if err:
        return err

    out = _lookup_flight(outbound_number, outbound_date)
    if not out or not out["dep_time"] or not out["arr_time"]:
        return (f"I couldn't look up flight {outbound_number} on {outbound_date} "
                f"automatically. Tell me the departure and arrival airports and times "
                f"from your confirmation and I'll add it, or forward me the airline email.")

    ret = None
    if return_number and return_date:
        time.sleep(1.2)   # Basic plan allows ~1 request/sec; space the two lookups out
        ret = _lookup_flight(return_number, return_date)
        if not ret or not ret["dep_time"] or not ret["arr_time"]:
            ret = None  # we'll still add the outbound; note the return couldn't be found

    who = person or "Trip"
    tzname = str(TIMEZONE)
    created = []

    # (2) Individual outbound flight event (actual gate-to-gate times).
    _cal_insert_event(
        service,
        summary=f"{who} \u2708 {out['number']} {out['dep_airport']}\u2192{out['arr_airport']}",
        start_iso=out["dep_time"], end_iso=out["arr_time"], tzname=tzname,
        location=f"{out['dep_airport']} \u2192 {out['arr_airport']}",
        details=f"{out['airline']} flight {out['number']}. Auto-added by Guppi.",
        color_id=TRAVEL_COLOR_ID)
    created.append(f"outbound {out['number']} ({out['dep_airport']}\u2192{out['arr_airport']})")

    # Trip-block bounds start at outbound departure minus 1 hour (airport buffer).
    try:
        dep_dt = datetime.datetime.fromisoformat(out["dep_time"])
        block_start = (dep_dt - datetime.timedelta(hours=1)).isoformat()
    except ValueError:
        block_start = out["dep_time"]

    if ret:
        # (2) Individual return flight event.
        _cal_insert_event(
            service,
            summary=f"{who} \u2708 {ret['number']} {ret['dep_airport']}\u2192{ret['arr_airport']}",
            start_iso=ret["dep_time"], end_iso=ret["arr_time"], tzname=tzname,
            location=f"{ret['dep_airport']} \u2192 {ret['arr_airport']}",
            details=f"{ret['airline']} flight {ret['number']}. Auto-added by Guppi.",
            color_id=TRAVEL_COLOR_ID)
        created.append(f"return {ret['number']} ({ret['dep_airport']}\u2192{ret['arr_airport']})")

        # (1) Trip block: outbound depart -1h  ->  return arrival.
        _cal_insert_event(
            service,
            summary=f"{who} \u2708 Trip: {out['dep_airport']}\u2194{out['arr_airport']}",
            start_iso=block_start, end_iso=ret["arr_time"], tzname=tzname,
            location=f"{out['arr_airport']}",
            details=(f"Outbound {out['number']} {out['dep_airport']}\u2192{out['arr_airport']}; "
                     f"return {ret['number']} {ret['dep_airport']}\u2192{ret['arr_airport']}. "
                     f"Block starts 1h before departure for airport travel. Auto-added by Guppi."),
            color_id=TRAVEL_COLOR_ID)
        created.append("trip block")
        summary_line = (f"Added your trip: block from 1h before {out['number']} departs "
                        f"({out['dep_airport']}) through {ret['number']} arrival "
                        f"({ret['arr_airport']}), plus both flight events.")
    else:
        note = ""
        if return_number:
            note = (f" I couldn't look up the return ({return_number}) - tell me its details "
                    f"or forward the confirmation and I'll add it.")
        summary_line = (f"Added your outbound flight {out['number']} "
                        f"({out['dep_airport']}\u2192{out['arr_airport']}).{note}")

    return summary_line


# How a plain-English repeat maps to an iCalendar rule. Kept small on purpose - these
# cover school/sport/camp patterns, which is all a family calendar needs.
_REPEAT_RULES = {
    "daily":    "FREQ=DAILY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly":   "FREQ=WEEKLY",
    "monthly":  "FREQ=MONTHLY",
}
# If the model asks for a repeat but gives no end, cap it rather than writing an event that
# recurs forever on the family calendar. Generous enough to be useful, bounded enough that
# a mistake is a nuisance instead of a cleanup project.
_REPEAT_DEFAULT_COUNT = {"daily": 14, "weekdays": 20, "weekly": 52, "monthly": 12}


def _build_recurrence(repeat, count=None, until=None):
    """Turn a plain repeat description into a Google Calendar RRULE list, or None.

    One recurring event beats N near-identical ones. A 4-day camp used to be four separate
    add_calendar_event calls, each re-emitting the whole details block - which is what
    pushed a single turn past the model's output ceiling (Trap 65). It also makes a
    tidier calendar: one series instead of four unrelated-looking entries."""
    if not repeat or str(repeat).strip().lower() in ("", "none"):
        return None
    key = str(repeat).strip().lower()
    base = _REPEAT_RULES.get(key)
    if not base:
        print(f"[calendar] unknown repeat {repeat!r}; adding a single event instead")
        return None
    rule = f"RRULE:{base}"

    if until:
        try:
            d = datetime.date.fromisoformat(str(until)[:10])
            # UNTIL is UTC. Use end-of-day LOCAL so the final day is actually included -
            # a bare date would cut the last occurrence off in an eastern timezone.
            end_local = datetime.datetime(d.year, d.month, d.day, 23, 59, 59,
                                          tzinfo=TIMEZONE)
            return [rule + ";UNTIL=" + end_local.astimezone(
                datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")]
        except (ValueError, TypeError):
            print(f"[calendar] bad repeat_until {until!r}; falling back to a count")

    try:
        n = int(count) if count is not None else 0
    except (ValueError, TypeError):
        n = 0
    if n <= 0:
        n = _REPEAT_DEFAULT_COUNT.get(key, 12)
        print(f"[calendar] repeat with no end given; capping at {n} occurrences")
    return [rule + f";COUNT={min(n, 365)}"]


def _cal_insert_event(service, summary, start_iso, end_iso, tzname,
                      location=None, details=None, color_id=None):
    """Low-level: insert one event. Shared by the flight tool (and reusable elsewhere).
    color_id is a Google Calendar color number (e.g. "2" = Sage) — used to visually
    distinguish certain events like travel."""
    body = {
        "summary": summary,
        "description": details or "",
        "extendedProperties": {"private": {"created_by": "guppi"}},
        "start": {"dateTime": start_iso, "timeZone": tzname},
        "end": {"dateTime": end_iso, "timeZone": tzname},
    }
    if location:
        body["location"] = location
    if color_id:
        body["colorId"] = str(color_id)
    service.events().insert(calendarId=FAMILY_CALENDAR_ID, body=body).execute()


def tool_add_calendar_event(summary, start_iso, end_iso, person=None,
                            location=None, details=None, personal=False,
                            repeat=None, repeat_count=None, repeat_until=None):
    service = get_calendar_service(person)
    if not service:
        if person and google_needs_reconnect(person):
            return (f"{person}'s Google access expired and needs reconnecting before I "
                    f"can add events. A parent should sign in again via the connect link.")
        return "The Google account isn't connected yet."
    tzname = str(TIMEZONE)
    # Mark every event Guppi creates so it's easy to spot which events came from the
    # assistant vs. ones added by hand. The marker goes at the END of the description,
    # after any real event details, tagged with who requested it and when.
    stamp = now_local().strftime("%b %d, %Y at %I:%M %p")
    who = person or "a family member"
    marker = f"— Added by Guppi (requested by {who} on {stamp})."
    description = f"{details}\n\n{marker}" if details else marker

    body = {
        "summary": summary,
        "description": description,
        "extendedProperties": {"private": {"created_by": "guppi"}},
        "start": {"dateTime": start_iso, "timeZone": tzname},
        "end": {"dateTime": end_iso, "timeZone": tzname},
    }
    if location:
        body["location"] = location   # Google makes this tappable -> maps/directions
    # Personal / just-for-me events (usually a work obligation) get the sage color, the
    # same way travel does, so they're easy to tell apart from family events at a glance.
    if personal:
        body["colorId"] = TRAVEL_COLOR_ID

    rrule = _build_recurrence(repeat, repeat_count, repeat_until)
    if rrule:
        body["recurrence"] = rrule

    try:
        service.events().insert(calendarId=FAMILY_CALENDAR_ID, body=body).execute()
    except Exception as e:
        print(f"[cal] insert failed: {e}")
        if rrule:
            # The usual cause: an event longer than its own repeat interval (a multi-day
            # span set to repeat daily). Say which knob is wrong instead of "it failed".
            return ("I couldn't add that repeating event. If it repeats daily, each "
                    "occurrence has to start and end on the SAME day - set the start and "
                    "end to one day's times and let the repeat cover the rest.")
        return f"I couldn't add that to the calendar: {e}"

    extra = f" at {location}" if location else ""
    if rrule:
        how = str(repeat).lower()
        when = (f"until {repeat_until}" if repeat_until
                else f"{rrule[0].split('COUNT=')[-1]} times"
                if "COUNT=" in rrule[0] else "")
        return (f"Added '{summary}' starting {start_iso}{extra}, repeating {how} {when}. "
                f"It's one repeating series, so the whole thing can be changed at once.")
    return f"Added '{summary}' on {start_iso}{extra}."


# =============================================================================
#  IMAP EMAIL  (live.com / outlook, and any IMAP provider)
# =============================================================================
#  Chosen over Microsoft Graph because a personal live.com account can no longer
#  register an Azure app without a paid directory. IMAP + an app password needs no
#  Azure, no OAuth, no credit card. Works the same for live.com, Gmail, AOL, etc.
#
#  Each person's credentials are their OWN. Email only ever searches the requesting
#  person's mailbox — Kim's search never reaches Jason's inbox.
# =============================================================================

# Known IMAP hosts, so the person only has to give an email + app password.
IMAP_PRESETS = {
    "outlook.com": ("outlook.office365.com", 993),
    "hotmail.com": ("outlook.office365.com", 993),
    "live.com":    ("outlook.office365.com", 993),
    "msn.com":     ("outlook.office365.com", 993),
    "gmail.com":   ("imap.gmail.com", 993),
    "googlemail.com": ("imap.gmail.com", 993),
    "yahoo.com":   ("imap.mail.yahoo.com", 993),
    "aol.com":     ("imap.aol.com", 993),
    "icloud.com":  ("imap.mail.me.com", 993),
    "me.com":      ("imap.mail.me.com", 993),
}


def imap_host_for(email_addr):
    """Pick the IMAP host/port from the address domain. Returns (host, port) or None."""
    domain = email_addr.split("@")[-1].strip().lower()
    return IMAP_PRESETS.get(domain)


def save_imap_account(person, email_addr, app_password):
    """Store a person's IMAP creds. Verifies the login works before saving, so we never
    store a credential that silently fails later. Returns a status message."""
    hostinfo = imap_host_for(email_addr)
    if not hostinfo:
        domain = email_addr.split("@")[-1]
        return (f"I don't know the IMAP settings for {domain}. This works with "
                f"outlook/live/hotmail, gmail, yahoo, aol, and icloud.")
    host, port = hostinfo
    # Verify before saving.
    try:
        M = imaplib.IMAP4_SSL(host, port, timeout=20)
        M.login(email_addr, app_password)
        M.logout()
    except Exception as e:
        print(f"[imap] login test failed for {person}: {e}")
        return ("That didn't log in. Make sure you used an APP PASSWORD (not your normal "
                "password), and that IMAP is enabled on the account. For Outlook/live.com "
                "you generate an app password in your Microsoft account security settings.")
    conn = db()
    conn.execute("INSERT OR REPLACE INTO imap_accounts "
                 "(person, email_addr, app_password, imap_host, imap_port) "
                 "VALUES (?,?,?,?,?)", (person, email_addr, app_password, host, port))
    conn.commit()
    conn.close()
    print(f"[imap] saved account for {person}: {email_addr}")
    return f"Your email ({email_addr}) is connected. I can check it for you now."


def get_imap_account(person):
    conn = db()
    row = conn.execute("SELECT email_addr, app_password, imap_host, imap_port "
                       "FROM imap_accounts WHERE person = ?", (person,)).fetchone()
    conn.close()
    return row


def _decode(s):
    """Decode an email header (handles =?utf-8?...?= encoded words)."""
    if not s:
        return ""
    out = []
    for text, enc in decode_header(s):
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


# =============================================================================
#  MICROSOFT OAUTH  (live.com / outlook — the only path that still works)
# =============================================================================
def _ms_token_request(fields):
    try:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            f"{MS_AUTHORITY}/oauth2/v2.0/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
        print(f"[ms] token request failed: {e} {body}")
        return None


def save_ms_token(person, tok, email_addr=None):
    conn = db()
    old = conn.execute("SELECT refresh_token, email_addr FROM ms_tokens WHERE person = ?",
                       (person,)).fetchone()
    refresh = tok.get("refresh_token") or (old["refresh_token"] if old else None)
    if not refresh:
        conn.close()
        print(f"[ms] no refresh token for {person}; not saving")
        return
    email_addr = email_addr or (old["email_addr"] if old else None)
    expires_at = (now_local() + datetime.timedelta(
        seconds=int(tok.get("expires_in", 3600)) - 120)).isoformat()
    conn.execute("INSERT OR REPLACE INTO ms_tokens "
                 "(person, email_addr, refresh_token, access_token, expires_at) "
                 "VALUES (?,?,?,?,?)",
                 (person, email_addr, refresh, tok.get("access_token"), expires_at))
    conn.commit()
    conn.close()
    # A successful save (fresh connect OR a working refresh) means the token is alive again
    # — clear any dead flag so the short-circuit in get_ms_access_token stops blocking it.
    set_setting(f"ms_dead_{person}", "")


def get_ms_access_token(person):
    """A valid access token for this person, refreshing if needed. None if not connected."""
    if not person or not MS_CLIENT_ID:
        return None
    # If this token is already known-dead (a prior refresh failed and it needs a manual
    # reconnect), don't keep hammering Microsoft on every scheduler job — that just spams
    # the logs and the endpoint. Stay quiet until the reconnect clears the flag.
    if get_setting(f"ms_dead_{person}") == "1":
        return None
    conn = db()
    row = conn.execute("SELECT refresh_token, access_token, expires_at FROM ms_tokens "
                       "WHERE person = ?", (person,)).fetchone()
    conn.close()
    if not row:
        print(f"[ms] {person}: no ms_tokens row")
        return None
    try:
        if row["access_token"] and row["expires_at"] and \
                datetime.datetime.fromisoformat(row["expires_at"]) > now_local():
            return row["access_token"]
    except ValueError:
        pass
    tok = _ms_token_request({
        "client_id": MS_CLIENT_ID, "client_secret": MS_CLIENT_SECRET,
        "grant_type": "refresh_token", "refresh_token": row["refresh_token"],
        "scope": MS_SCOPES, "redirect_uri": MS_REDIRECT_URI})
    if not tok or "access_token" not in tok:
        print(f"[ms] {person}: refresh failed; needs reconnect")
        set_setting(f"ms_dead_{person}", "1")
        return None
    save_ms_token(person, tok)
    set_setting(f"ms_dead_{person}", "")
    return tok["access_token"]


def ms_needs_reconnect(person):
    return get_setting(f"ms_dead_{person}") == "1"


@app.get("/connect-microsoft")
def connect_microsoft(person: str = "", secret: str = "", token: str = ""):
    """Connect a live.com/outlook account:
    /connect-microsoft?person=Jason&secret=<SETUP_SECRET>. Requires the setup secret (H2)
    so a stranger can't bind their own account under a family name."""
    if not MS_CLIENT_ID:
        return HTMLResponse("<h2>Microsoft isn't configured yet.</h2>"
                            "<p>MS_CLIENT_ID and MS_CLIENT_SECRET need to be set in Railway.</p>")
    if not (_secret_ok(secret) or
            (person and _verify_access_token(token, _connect_purpose(person), single_use=False))):
        return HTMLResponse(
            "<h2>This sign-in link is invalid or expired</h2><p>Ask Guppi for a new "
            "connect link in a private chat - they expire after 30 minutes and work "
            "once.</p>", status_code=403)
    if not person:
        return HTMLResponse("<h2>Who is connecting?</h2>"
                            "<p>e.g. <code>/connect-microsoft?person=Jason&secret=YOUR_CODE</code></p>")
    params = {"client_id": MS_CLIENT_ID, "response_type": "code",
              "redirect_uri": MS_REDIRECT_URI, "response_mode": "query",
              "scope": MS_SCOPES, "state": _make_oauth_state(person),
              "prompt": "select_account"}
    return RedirectResponse(
        f"{MS_AUTHORITY}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params))


@app.get("/oauth/microsoft/callback")
def ms_callback(code: str = "", state: str = "", error: str = "",
                error_description: str = ""):
    if error:
        return HTMLResponse("<h2>Microsoft sign-in failed</h2><p>"
                            + html_lib.escape(f"{error}: {error_description}") + "</p>")
    if not code:
        return HTMLResponse("<h2>No authorization code came back.</h2>")
    # H2: verify the signed state we issued.
    person = _verify_oauth_state(state)
    if not person:
        return HTMLResponse("<h2>This sign-in link is invalid or expired.</h2>",
                            status_code=403)
    tok = _ms_token_request({
        "client_id": MS_CLIENT_ID, "client_secret": MS_CLIENT_SECRET,
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": MS_REDIRECT_URI, "scope": MS_SCOPES})
    if not tok or "access_token" not in tok:
        return HTMLResponse("<h2>Couldn't complete the Microsoft sign-in.</h2>"
                            "<p>Try again from /connect-microsoft?person=YourName</p>")
    # Pull the account's email address from the id_token claims if present.
    email_addr = None
    idt = tok.get("id_token")
    if idt:
        try:
            payload = idt.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            email_addr = claims.get("email") or claims.get("preferred_username")
        except Exception:
            pass
    # Fallback: ask the OpenID userinfo endpoint directly. Some personal accounts don't
    # put the address in the id_token, but userinfo returns it.
    if not email_addr and tok.get("access_token"):
        try:
            req = urllib.request.Request(
                "https://graph.microsoft.com/oidc/userinfo",
                headers={"Authorization": f"Bearer {tok['access_token']}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                info = json.loads(r.read())
            email_addr = info.get("email") or info.get("preferred_username")
        except Exception as e:
            print(f"[ms] userinfo lookup failed: {e}")
    if not email_addr:
        # Last resort: we truly couldn't determine the address, so email won't work.
        # Tell the user rather than silently saving a token we can't use.
        print(f"[ms] WARNING: connected {person} but no email address resolved")
        save_ms_token(person, tok, None)
        return HTMLResponse(
            "<h2>Almost there — but I couldn't read your email address.</h2>"
            "<p>Your sign-in worked, but Microsoft didn't return your address, so I "
            "can't check your mail yet. Please try connecting once more from "
            f"/connect-microsoft?person={person}. If it keeps happening, tell Guppi.</p>")
    save_ms_token(person, tok, email_addr)
    print(f"[ms] connected {person} ({email_addr})")
    return HTMLResponse(f"<h2>Guppi is connected to {person}'s Microsoft email"
                        f"{f' ({email_addr})' if email_addr else ''}.</h2>"
                        "<p>You can close this window.</p>")


def _xoauth2_string(user, token):
    """Build the SASL XOAUTH2 auth string Microsoft's IMAP expects."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


def _gmail_send(person, to_addr, subject, body, reply_headers=None):
    """Send via the Gmail API using the person's OAuth token (needs gmail.send scope)."""
    service = get_gmail_service(person)
    if not service:
        return False, "Gmail isn't connected (or needs reconnecting for send access)."
    try:
        msg = MIMEText(body)
        msg["To"] = to_addr
        msg["Subject"] = subject
        if reply_headers:
            # Threading a reply: reference the original so it lands in the same thread.
            if reply_headers.get("message_id"):
                msg["In-Reply-To"] = reply_headers["message_id"]
                msg["References"] = reply_headers["message_id"]
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        send_body = {"raw": raw}
        if reply_headers and reply_headers.get("thread_id"):
            send_body["threadId"] = reply_headers["thread_id"]
        service.users().messages().send(userId="me", body=send_body).execute()
        return True, None
    except Exception as e:
        print(f"[send:gmail] failed for {person}: {e}")
        return False, str(e)


def _ms_smtp_send(person, to_addr, subject, body, reply_headers=None):
    """Send via Outlook SMTP using the person's OAuth token (SASL XOAUTH2, SMTP.Send)."""
    token = get_ms_access_token(person)
    if not token:
        return False, "Microsoft account isn't connected (or needs reconnecting)."
    conn = db()
    row = conn.execute("SELECT email_addr FROM ms_tokens WHERE person = ?",
                       (person,)).fetchone()
    conn.close()
    from_addr = row["email_addr"] if row else None
    if not from_addr:
        return False, "No Microsoft email address on file."
    try:
        msg = MIMEText(body)
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        if reply_headers and reply_headers.get("message_id"):
            msg["In-Reply-To"] = reply_headers["message_id"]
            msg["References"] = reply_headers["message_id"]
        auth = _xoauth2_string(from_addr, token)
        import base64 as _b64
        smtp = smtplib.SMTP(MS_SMTP_HOST, MS_SMTP_PORT, timeout=25)
        smtp.ehlo(); smtp.starttls(); smtp.ehlo()
        # SMTP AUTH XOAUTH2: the auth string is the same XOAUTH2 blob, base64'd.
        code, resp = smtp.docmd("AUTH", "XOAUTH2 " +
                                _b64.b64encode(auth.encode()).decode())
        if code not in (235, 334):
            smtp.quit()
            return False, f"SMTP auth rejected ({code})."
        smtp.sendmail(from_addr, [to_addr], msg.as_string())
        smtp.quit()
        return True, None
    except Exception as e:
        print(f"[send:ms] failed for {person}: {e}")
        return False, str(e)


def send_email_for(person, to_addr, subject, body, reply_headers=None):
    """Provider-agnostic send: use whichever email account this person has connected.
    Prefers Gmail (API) then Microsoft (SMTP). Returns (ok, error)."""
    providers = connected_providers(person)
    if "google" in providers:
        ok, err = _gmail_send(person, to_addr, subject, body, reply_headers)
        if ok:
            return True, "google"
    if "imap" in providers:  # imap here means the MS/live.com account
        ok, err = _ms_smtp_send(person, to_addr, subject, body, reply_headers)
        if ok:
            return True, "microsoft"
        return False, err
    return False, "No email account is connected to send from."


# ---- Draft-then-confirm email sending --------------------------------------
# Guppi NEVER sends without explicit confirmation. draft_email prepares a draft and
# stashes it here (per person); the user reviews and can edit; send_pending_email only
# fires after they say send. In-memory, so a pending draft clears on redeploy (fine —
# an unsent draft that survives a restart would be a surprise, not a feature).
_PENDING_EMAIL = {}   # person -> {to, subject, body, reply_headers, turn_id}
_CURRENT_TURN = {}    # person -> id of the message turn currently being processed
_LAST_USER_TEXT = {}  # person -> their most recent raw message (for send-confirmation check)

def tool_draft_email(person, to_addr, subject, body, reply_headers=None):
    """Prepare an email draft and show it to the user for approval. Does NOT send. The
    user then says 'send it' (-> send_pending_email) or asks for changes."""
    if not to_addr or "@" not in to_addr:
        return ("I need a valid email address to send to. Who should this go to?")
    _PENDING_EMAIL[person] = {
        "to": to_addr, "subject": subject or "(no subject)",
        "body": body, "reply_headers": reply_headers,
        "turn_id": _CURRENT_TURN.get(person)}   # H4: remember which turn drafted it
    preview = (f"Here's the draft — say \"send it\" to send, or tell me what to change:\n\n"
               f"To: {to_addr}\n"
               f"Subject: {subject or '(no subject)'}\n\n"
               f"{body}")
    return preview


def _looks_like_send_confirmation(text):
    """Does the human's own message actually approve sending? Code-level gate (H4) so a
    model can't draft-and-send in one turn on its own — a real human 'send it' is required."""
    if not text:
        return False
    t = text.strip().lower()
    # Must be a short, affirmative send instruction — not a long new request.
    confirmations = ("send it", "send that", "send the email", "send", "yes send",
                     "go ahead and send", "send now", "yes, send", "ok send", "please send",
                     "confirm", "yes", "yep", "yes please", "go ahead", "do it", "sounds good",
                     "looks good", "perfect send")
    return any(t == c or t.startswith(c) for c in confirmations) and len(t) <= 40


def tool_send_pending_email(person, confirming_text=None):
    """Send the draft the user approved. Two code-level gates (not just prompt): a pending
    draft must exist AND have been shown in a PRIOR turn, and the human's current message
    must actually read as a send-confirmation. This makes 'never send without confirmation'
    a guarantee, not a suggestion."""
    pending = _PENDING_EMAIL.get(person)
    if not pending:
        return ("I don't have a draft ready to send. Tell me what you'd like to say and "
                "who it's for, and I'll draft it first.")
    # H4 gate 1: the draft must have been shown in an EARLIER turn, not this same one.
    if pending.get("turn_id") == _CURRENT_TURN.get(person):
        return ("Here's the draft above - tell me to \"send it\" and I will. (I won't send "
                "in the same breath as drafting.)")
    # H4 gate 2: the human's actual message must approve sending.
    if not _looks_like_send_confirmation(confirming_text):
        return ("Just to confirm before I send - reply \"send it\" and it'll go out, or "
                "tell me what to change.")
    ok, info = send_email_for(person, pending["to"], pending["subject"],
                              pending["body"], pending.get("reply_headers"))
    if ok:
        _PENDING_EMAIL.pop(person, None)
        print(f"[send] {person} -> {pending['to']} via {info}")
        return f"Sent to {pending['to']}. ✓"
    _PENDING_EMAIL.pop(person, None)
    return (f"I couldn't send it: {info}. This may mean the account needs reconnecting "
            f"with send access (the email connection was set up before sending was "
            f"enabled — reconnect to grant it).")


def tool_discard_draft(person):
    """Throw away a pending email draft."""
    if _PENDING_EMAIL.pop(person, None):
        return "Okay, I've discarded that draft."
    return "There was no draft to discard."


def _ms_imap_connect(person):
    """Connect to Outlook IMAP using this person's OAuth access token (XOAUTH2)."""
    token = get_ms_access_token(person)
    if not token:
        return None
    conn = db()
    row = conn.execute("SELECT email_addr FROM ms_tokens WHERE person = ?",
                       (person,)).fetchone()
    conn.close()
    email_addr = row["email_addr"] if row else None
    if not email_addr:
        print(f"[ms] no email address on file for {person}; can't IMAP")
        return None
    try:
        M = imaplib.IMAP4_SSL(MS_IMAP_HOST, MS_IMAP_PORT, timeout=20)
        M.authenticate("XOAUTH2",
                       lambda _=None: _xoauth2_string(email_addr, token).encode())
        return M
    except Exception as e:
        print(f"[ms] IMAP XOAUTH2 connect failed for {person} ({email_addr}): {e}")
        return None


def _imap_connect(person):
    """Open an IMAP connection for this person. Prefers Microsoft OAuth (live.com); falls
    back to a stored app-password account (Gmail and other providers that still allow it)."""
    conn = db()
    has_ms = conn.execute("SELECT 1 FROM ms_tokens WHERE person = ?", (person,)).fetchone()
    conn.close()
    if has_ms:
        M = _ms_imap_connect(person)
        if M:
            return M
    row = get_imap_account(person)
    if not row:
        return None
    try:
        M = imaplib.IMAP4_SSL(row["imap_host"], row["imap_port"], timeout=20)
        M.login(row["email_addr"], row["app_password"])
        return M
    except Exception as e:
        print(f"[imap] connect failed for {person}: {e}")
        return None


def _html_to_text(html):
    """Turn an HTML email body into readable plain text.

    The old one-liner - re.sub(r"<[^>]+>", " ", html) - removed TAGS but left the CONTENTS
    of <style>/<script> sitting in the output as text, never unescaped entities (&nbsp;,
    &amp;, &lt; survived literally), and flattened every line break into one run-on blob.
    On an HTML-only email that burns the length budget on stylesheet text and punctuation
    noise, which is exactly how a camp email's DATES got truncated away before the model
    ever saw them (Trap 58). Structure matters too: a list of pickup times is unreadable
    as a single paragraph."""
    if not html:
        return ""
    # Drop the CONTENTS of non-visible elements, not merely their tags.
    text = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", html)
    # Preserve the document's line structure before flattening what's left.
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table|ul|ol)\s*>", "\n", text)
    text = re.sub(r"(?i)</t[dh]\s*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---- Email attachments the model can actually look at ------------------------
# A school/camp email routinely puts the important part IN a picture (a parking map, a
# schedule graphic, a flyer). Reading only the text misses it entirely - the camp email
# literally said "please consult the attached map".
_READABLE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MIN_ATTACHMENT_BYTES = 20_000      # below this it's a signature logo or tracking pixel
_MAX_ATTACHMENT_BYTES = 3_500_000   # keep each image well inside the API's limit
_MAX_ATTACHMENTS = 3                # cap cost: each image is ~1.5k tokens


def _collect_attachments(msg):
    """Pull readable IMAGE attachments from an email message. Returns (images, skipped)
    where images are ready-to-send base64 blocks and `skipped` names what was left out, so
    Guppi can TELL the user something exists it couldn't read rather than silently
    ignoring it."""
    images, skipped = [], []
    try:
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            fname = (part.get_filename() or "")
            try:
                fname = _decode(fname)
            except Exception:
                pass
            fname = fname.replace("\x00", "").strip()
            is_image = ctype in _READABLE_IMAGE_TYPES
            if not is_image and not fname:
                continue                       # an inline body part, not an attachment
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if not is_image:
                skipped.append(f"{fname} ({ctype or 'unknown type'})")
                continue
            if len(payload) < _MIN_ATTACHMENT_BYTES:
                continue                       # logo / tracking pixel, not a document
            if len(payload) > _MAX_ATTACHMENT_BYTES:
                skipped.append(f"{fname or 'an image'} (too large to read)")
                continue
            if len(images) >= _MAX_ATTACHMENTS:
                skipped.append(fname or "an image")
                continue
            images.append({"filename": fname or "image",
                           "media_type": ctype,
                           "data": base64.b64encode(payload).decode()})
    except Exception as e:
        print(f"[email] attachment scan failed: {e}")
    return images, skipped


def _normalize_email_query(query):
    """Trap 48 keeps coming back. The model writes `from:Kimberly Clark Garnet Basketball
    Camp` - a person's NAME plus a subject phrase after an operator that only accepts an
    address or a domain. The tool description forbids it explicitly and the model did it
    anyway, which is the whole "prompts are suggestions, code is a guarantee" principle
    proving itself. So fix it in CODE: if what follows from: doesn't look like an address
    or domain, demote it to an ordinary search word."""
    def fix(m):
        val = m.group(1)
        return m.group(0) if ("@" in val or "." in val) else val
    return re.sub(r"\bfrom:(\S+)", fix, query or "")


def _imap_snippet(msg, limit=250):
    """Best-effort short preview of the plain-text body."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(errors="replace").strip().replace("\n", " ")[:limit]
            return ""
        payload = msg.get_payload(decode=True)
        return payload.decode(errors="replace").strip().replace("\n", " ")[:limit] if payload else ""
    except Exception as e:
        print(f"[email] snippet extraction failed: {e}")
        return ""


def _imap_full_body(msg, limit=20000):
    """Full-ish plain-text body of an IMAP message (for reading scheduling details).
    Prefers text/plain; falls back to a crude HTML strip. Capped so it stays sane."""
    try:
        if msg.is_multipart():
            plain = ""
            html = ""
            for part in msg.walk():
                ctype = part.get_content_type()
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode(errors="replace")
                if ctype == "text/plain":
                    plain += text
                elif ctype == "text/html":
                    html += text
            # Choosing between the two parts is where this used to throw the whole message
            # away. A great deal of HTML mail ships a STUB text/plain part - a single "\n",
            # or "Please enable HTML to view this message". The old test was `plain or
            # html`, and a one-character stub is truthy, so the HTML was never even
            # collected and the body came back effectively empty. Prefer plain text only
            # when it actually carries the content.
            plain_s = plain.strip()
            html_s = _html_to_text(html)
            if len(plain_s) >= max(40, len(html_s) // 2):
                body = plain_s
            else:
                if html_s and len(plain_s) < 40:
                    print(f"[email] text/plain was a {len(plain_s)}-char stub; "
                          f"using the HTML part ({len(html_s)} chars)")
                body = html_s or plain_s
        else:
            payload = msg.get_payload(decode=True)
            raw = payload.decode(errors="replace") if payload else ""
            body = (_html_to_text(raw)
                    if (msg.get_content_type() or "").lower() == "text/html" else raw)
        return body.strip()[:limit]
    except Exception as e:
        # This used to swallow the error and return "", which is indistinguishable from an
        # empty email - the failure mode that makes a body problem invisible in the logs.
        print(f"[email] body extraction failed: {e}")
        return ""


def _imap_fetch_message(M, mid):
    """Fetch ONE COMPLETE message (headers + body) and parse it. Returns a Message or None.

    This replaces a fetch of BODY.PEEK[TEXT], which was wrong in two independent ways and
    is why every email came back with a zero-length body:

      1. BODY[TEXT] is the body WITHOUT headers. Content-Type lives in the headers, so the
         parser never learns the message is multipart or what its boundary is - it treats
         the entire MIME source as one flat text/plain blob, and every attachment becomes
         invisible. is_multipart() returns False on a message that plainly is multipart.
      2. The result was read at a fixed position (md[1][1]). IMAP does not guarantee the
         order or shape of items in a FETCH response, so when the server answered in a
         different shape the parse silently produced None and the body came back empty.

    Fetching BODY.PEEK[] gives the whole RFC822 message, and scanning the response for the
    payload instead of indexing into it removes the positional assumption. PEEK still means
    the message is not marked as read."""
    try:
        typ, md = M.fetch(mid, "(BODY.PEEK[])")
        if typ != "OK" or not md:
            print(f"[imap] fetch of {mid!r} returned {typ}")
            return None
        for item in md:
            if (isinstance(item, tuple) and len(item) > 1
                    and isinstance(item[1], (bytes, bytearray)) and len(item[1]) > 0):
                return emaillib.message_from_bytes(item[1])
        print(f"[imap] no payload found in fetch response for {mid!r}")
        return None
    except Exception as e:
        print(f"[imap] fetch failed for {mid!r}: {e}")
        return None


def imap_search(person, keywords, max_results=5, with_attachments=False):
    """Search a person's IMAP inbox. `keywords` is plain words (Gmail-style operators are
    stripped upstream). Returns normalized dicts, newest first.

    with_attachments is opt-in because attachment bytes are large: only read_email wants
    them, and carrying them through every routine search would waste memory for nothing."""
    M = _imap_connect(person)
    if not M:
        return []
    out = []
    try:
        M.select("INBOX")
        # Build an IMAP search. Plain words -> TEXT search (matches body/headers).
        kw = keywords.strip()
        if kw:
            # IMAP TEXT search, one term (multiple words -> AND them).
            terms = kw.split()
            crit = []
            for t in terms:
                crit += ["TEXT", t]
            typ, data = M.search(None, *crit) if crit else M.search(None, "ALL")
        else:
            typ, data = M.search(None, "ALL")
        if typ != "OK":
            return []
        ids = data[0].split()
        for mid in reversed(ids[-max_results:]):  # newest first
            msg = _imap_fetch_message(M, mid)
            if not msg:
                continue
            # Headers now come from the SAME parsed message as the body - no second fetch,
            # and no chance of the two disagreeing about which message we're looking at.
            out.append({
                "from": _decode(msg.get("From", "?")),
                "subject": _decode(msg.get("Subject", "(no subject)")),
                "date": msg.get("Date", ""),
                "snippet": _imap_snippet(msg),
                "full_body": _imap_full_body(msg),
                "id": mid.decode(), "provider": "imap"})
            if not out[-1]["full_body"]:
                print(f"[imap] WARNING empty body for {out[-1]['subject'][:50]!r} "
                      f"(multipart={msg.is_multipart()} type={msg.get_content_type()})")
            if with_attachments:
                imgs, skipped = _collect_attachments(msg)
                out[-1]["images"] = imgs
                out[-1]["skipped_attachments"] = skipped
    except Exception as e:
        print(f"[imap] search failed for {person}: {e}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


def imap_unread(person, max_results=5):
    """Newest UNSEEN messages, normalized. Used by the urgent-email poll."""
    M = _imap_connect(person)
    if not M:
        return []
    out = []
    try:
        M.select("INBOX")
        # A7: UNSEEN alone matches mail from any date - anything never opened stays
        # "new" indefinitely. SINCE bounds it to genuinely recent mail.
        since = (now_local() - datetime.timedelta(days=3)).strftime("%d-%b-%Y")
        typ, data = M.search(None, "UNSEEN", "SINCE", since)
        if typ != "OK":
            print(f"[imap] UNSEEN SINCE {since} search returned {typ}; falling back")
            typ, data = M.search(None, "UNSEEN")
        if typ != "OK":
            return []
        ids = data[0].split()
        for mid in reversed(ids[-max_results:]):
            # PEEK so we don't mark the mail as read just by checking it.
            typ, md = M.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK":
                continue
            hmsg = emaillib.message_from_bytes(md[0][1])
            out.append({
                "id": f"i:{mid.decode()}",
                "from": _decode(hmsg.get("From", "?")),
                "subject": _decode(hmsg.get("Subject", "(no subject)")),
                "snippet": ""})
    except Exception as e:
        print(f"[imap] unread failed for {person}: {e}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


# =============================================================================
#  PROVIDER-AGNOSTIC EMAIL  (Gmail token AND/OR IMAP, per person)
# =============================================================================
def tool_connection_health(person):
    """A plain-language status of THIS person's connections, live-tested. Answers
    'are my accounts working?' without anyone reading server logs. Actually attempts
    each connection so it reports reality, not just whether a row exists."""
    lines = []

    # --- Google (calendar + Gmail share one token) ---
    conn = db()
    has_google = conn.execute("SELECT 1 FROM google_tokens WHERE person = ?",
                              (person,)).fetchone()
    conn.close()
    if has_google:
        creds = load_google_token(person)
        if creds:
            lines.append("Google (calendar + Gmail): connected and working.")
        elif google_needs_reconnect(person):
            lines.append("Google (calendar + Gmail): EXPIRED - needs reconnecting. Use "
                         "the connect_link tool to give them a working link.")
        else:
            lines.append("Google (calendar + Gmail): connected but not responding right "
                         "now.")
    else:
        lines.append("Google (calendar + Gmail): not connected. Use the connect_link "
                     "tool to give them a working link.")

    # --- Microsoft / live.com ---
    conn = db()
    has_ms = conn.execute("SELECT email_addr FROM ms_tokens WHERE person = ?",
                          (person,)).fetchone()
    conn.close()
    if has_ms:
        M = _ms_imap_connect(person)
        if M:
            try:
                M.logout()
            except Exception:
                pass
            addr = has_ms["email_addr"] or "your account"
            lines.append(f"Microsoft ({addr}): connected and working.")
        elif ms_needs_reconnect(person):
            lines.append("Microsoft (live.com): EXPIRED - needs reconnecting. Use the "
                         "connect_link tool with kind='microsoft'.")
        else:
            lines.append("Microsoft (live.com): connected but not responding right now.")

    # --- App-password IMAP (Gmail-over-IMAP etc.), if any ---
    conn = db()
    imap_row = conn.execute("SELECT email_addr FROM imap_accounts WHERE person = ?",
                            (person,)).fetchone()
    conn.close()
    if imap_row:
        lines.append(f"IMAP ({imap_row['email_addr']}): configured.")

    if not lines:
        lines.append("No accounts connected yet.")
    return "Connection status:\n- " + "\n- ".join(lines)


def connected_providers(person):
    """Which email sources this person has. e.g. ['google', 'imap']"""
    if not person:
        return []
    out = []
    conn = db()
    if conn.execute("SELECT 1 FROM google_tokens WHERE person = ?", (person,)).fetchone():
        out.append("google")
    has_imap = conn.execute(
        "SELECT 1 FROM imap_accounts WHERE person = ?", (person,)).fetchone()
    has_ms = conn.execute(
        "SELECT 1 FROM ms_tokens WHERE person = ?", (person,)).fetchone()
    if has_imap or has_ms:
        out.append("imap")   # both are served by _imap_connect() (app-pw or MS-OAuth)
    conn.close()
    return out


def _gmail_full_body(service, msg_id):
    """Return the full plain-text body of a Gmail message (walks MIME parts)."""
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full").execute()
    except Exception as e:
        print(f"[email:google] full fetch failed: {e}")
        return ""

    def walk(part):
        # Prefer text/plain; fall back to stripping text/html.
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime == "text/plain":
            return base64.urlsafe_b64decode(data).decode(errors="replace")
        if data and mime == "text/html":
            html = base64.urlsafe_b64decode(data).decode(errors="replace")
            return _html_to_text(html)
        text = ""
        for p in part.get("parts", []) or []:
            text += walk(p)
        return text

    return walk(msg.get("payload", {})).strip()


def tool_read_email(query, person, max_results=3):
    """Find the email(s) matching `query` and return their FULL text (not a snippet), so
    Guppi can read the whole thing and pull out scheduling details — who/what/when/where.
    Use this (not search_email) when the user is asking about a specific email's contents,
    or wants an event or reminder created from an email."""
    providers = connected_providers(person)
    if not providers:
        return (f"{person} hasn't connected an email account yet. Use the connect_link "
                f"tool to give them a working link (a bare /connect URL will be refused - "
                f"it needs the signed token that tool provides).")

    # Fix a malformed from: in CODE rather than trusting the description (Trap 48).
    query = _normalize_email_query(query)
    print(f"[read_email] {person} ({'+'.join(providers)}) query={query!r}")

    # Clean query for IMAP (drop Gmail operators); keep raw for Gmail.
    imap_q = re.sub(r"\b(is|in|category|label|has|filename):\S+", "", query)
    imap_q = re.sub(r"\b(newer_than|older_than|after|before):\S+", "", imap_q)
    imap_q = re.sub(r"\bfrom:(\S+)", r"\1", imap_q)
    imap_q = re.sub(r"\s+", " ", imap_q).strip()

    results = []

    if "google" in providers:
        service = get_gmail_service(person)
        if service:
            try:
                res = service.users().messages().list(
                    userId="me", q=query, maxResults=max_results).execute()
                for m in res.get("messages", []):
                    md = service.users().messages().get(
                        userId="me", id=m["id"], format="metadata",
                        metadataHeaders=["From", "Subject", "Date"]).execute()
                    h = {x["name"]: x["value"] for x in md["payload"]["headers"]}
                    body = _gmail_full_body(service, m["id"])
                    results.append({"from": h.get("From", "?"),
                                    "subject": h.get("Subject", "(no subject)"),
                                    "date": h.get("Date", ""), "body": body,
                                    "images": [], "skipped": [], "provider": "google"})
            except Exception as e:
                print(f"[read_email:google] failed: {e}")

    if "imap" in providers:
        # imap_search fetches BODY.PEEK[TEXT], which already carries the attachment bytes,
        # so asking for attachments here costs no extra round-trip.
        for m in imap_search(person, imap_q, max_results, with_attachments=True):
            results.append({"from": m["from"], "subject": m["subject"],
                            "date": m.get("date", ""),
                            "body": m.get("full_body") or m.get("snippet", ""),
                            "images": m.get("images", []),
                            "skipped": m.get("skipped_attachments", []),
                            "provider": "imap"})

    if not results:
        print(f"[read_email] no match for {query!r}")
        return ("I couldn't find an email matching that search. Say that you didn't find "
                "anything with THAT SEARCH - do not tell the user their inbox is empty, "
                "and offer to try different words.")

    # Cap each body. The old cap was 2000 chars, which silently cut a camp email off
    # BEFORE the section containing every date and time - so the model was asked to build
    # calendar events from a message whose schedule it had never been shown (Trap 58).
    # 10000 chars is ~2500 tokens, roughly 0.25c on Haiku: far cheaper than missing a date.
    PER_EMAIL_CAP = 10000
    out, images = [], []
    for r in results[:max_results]:
        body = (r["body"] or "").strip()
        if len(body) > PER_EMAIL_CAP:
            body = (body[:PER_EMAIL_CAP] +
                    "\n\n[TRUNCATED - this email is longer than shown. TELL THE USER you "
                    "only read the first part, and do not assume the rest is unimportant.]")
        hdr = f"From: {r['from']}\nSubject: {r['subject']}"
        if r.get("date"):
            hdr += f"\nDate: {r['date']}"
        note = ""
        if r.get("images"):
            names = ", ".join(i["filename"] for i in r["images"])
            note += (f"\n\n[This email has {len(r['images'])} image attachment(s) ({names}), "
                     f"shown to you after this text. READ THEM - a camp or school email "
                     f"often puts the map, schedule, or key details in the picture.]")
            images.extend(r["images"])
        if r.get("skipped"):
            note += (f"\n\n[This email also has attachment(s) you CANNOT read: "
                     f"{', '.join(r['skipped'])}. Mention them so the user knows to look.]")
        print(f"[read_email] -> [{r.get('provider', '?')}] {r['subject'][:50]!r} "
              f"body={len(body)}c images={len(r.get('images') or [])} "
              f"skipped={len(r.get('skipped') or [])}")
        out.append(f"{hdr}\n\n{body}{note}")

    text = "\n\n----- next email -----\n\n".join(out)
    # A plain string when there's nothing to look at keeps every other caller unchanged;
    # only the attachment case needs the richer shape.
    return {"text": text, "images": images[:_MAX_ATTACHMENTS]} if images else text


def _gmail_search(person, query, max_results):
    service = get_gmail_service(person)
    if not service:
        return []
    try:
        res = service.users().messages().list(
            userId="me", q=query, maxResults=max_results).execute()
    except Exception as e:
        print(f"[email:google] search failed for {person}: {e}")
        return []
    out = []
    for m in res.get("messages", []):
        try:
            msg = service.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
            h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
            out.append({"from": h.get("From", "?"),
                        "subject": h.get("Subject", "(no subject)"),
                        "snippet": msg.get("snippet", "")[:250],
                        "id": m["id"], "provider": "google"})
        except Exception as e:
            print(f"[email:google] fetch failed: {e}")
    return out


def tool_search_email(query, person, max_results=5):
    """Searches THIS PERSON'S own inbox(es) — Gmail, IMAP (live.com), or both.

    Auto-widens the query (Trap 17): a too-narrow search returning nothing, reported as
    'you have no emails', is worse than useless. Enforced in CODE, not the prompt."""
    providers = connected_providers(person)
    if not providers:
        return (f"{person} hasn't connected an email account yet. Use the connect_link "
                f"tool to give them a working link - one for 'google' (calendar + Gmail) "
                f"and/or one for 'microsoft' (live.com/outlook). A bare /connect URL will "
                f"be refused; it needs the signed token that tool provides.")

    query = _normalize_email_query(query)

    # Build queries suited to each provider. Gmail understands its own operators
    # (is:important, from:, after:); IMAP does not — to IMAP those are just literal
    # words that would wrongly match message bodies. So we keep the raw query for Gmail
    # and hand IMAP a cleaned, plain-words version.
    gmail_attempts = [query]
    no_date = re.sub(r"\b(newer_than|older_than|after|before):\S+", "", query).strip()
    if no_date and no_date != query:
        gmail_attempts.append(no_date)

    # For IMAP: drop ALL Gmail operators (is:x, in:x, category:x, label:x) entirely —
    # they're filters, not words — and unwrap from:addr to just the name/word.
    imap_q = re.sub(r"\b(is|in|category|label|has|filename):\S+", "", query)
    imap_q = re.sub(r"\b(newer_than|older_than|after|before):\S+", "", imap_q)
    imap_q = re.sub(r"\bfrom:(\S+)", r"\1", imap_q)
    imap_q = re.sub(r"\s+", " ", imap_q).strip()

    seen = set()
    for attempt in gmail_attempts:
        if attempt in seen:
            continue
        seen.add(attempt)
        print(f"[search_email] {person} ({'+'.join(providers)}) trying: gmail={attempt!r} imap={imap_q!r}")
        found = []
        if "google" in providers:
            found += _gmail_search(person, attempt, max_results)
        if "imap" in providers:
            found += imap_search(person, imap_q, max_results)
        if found:
            print(f"[search_email] found {len(found)}")
            lines = []
            for m in found[:max_results * len(providers)]:
                tag = "" if len(providers) < 2 else f"[{m['provider']}] "
                snip = f": {m['snippet']}" if m['snippet'] else ""
                lines.append(f"{tag}From {m['from']} | {m['subject']}{snip}")
            return "\n".join(lines)
    return "No matching emails found (searched broadly)."


# ---- Memory -----------------------------------------------------------------
def tool_remember(fact, about, added_by):
    conn = db()
    conn.execute("INSERT INTO memories (fact, about, added_by, created_at) VALUES (?,?,?,?)",
                 (fact, about, added_by, now_local().isoformat()))
    conn.commit()
    conn.close()
    return f"Saved: {fact}"


def tool_recall(about=None):
    conn = db()
    if about:
        rows = conn.execute(
            "SELECT id, fact, about FROM memories WHERE about = ? ORDER BY id",
            (about,)).fetchall()
    else:
        rows = conn.execute("SELECT id, fact, about FROM memories ORDER BY id").fetchall()
    conn.close()
    if not rows:
        return "I don't have anything saved yet."
    return "\n".join(f"[{r['id']}] {r['fact']}" +
                     (f" (about {r['about']})" if r["about"] else "") for r in rows)


def tool_forget(memory_id):
    conn = db()
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return "Forgotten." if deleted else "I couldn't find that one."


# ---- Reminders (stored now; the Phase 4 scheduler will fire them) -----------
# ---- Resolve a nudge target (a name, or a group like "the girls") -----------
GROUP_ALIASES = {
    "the girls": ["Lillian", "Charlotte"],
    "the kids": ["Lillian", "Charlotte"],
    "the children": ["Lillian", "Charlotte"],
    "the parents": ["Jason", "Kim"],
}

def resolve_targets(target):
    """Return a list of (name, chat_id) for a nudge target. Accepts a person's name or
    a group alias. Skips anyone who hasn't set themselves up yet."""
    if not target:
        return []
    key = target.strip().lower()
    names = GROUP_ALIASES.get(key)
    conn = db()
    out = []
    if names:
        for n in names:
            row = conn.execute(
                "SELECT name, chat_id FROM people WHERE name = ? AND chat_id IS NOT NULL",
                (n,)).fetchone()
            if row:
                out.append((row["name"], row["chat_id"]))
    else:
        # match a single person by name (case-insensitive)
        row = conn.execute(
            "SELECT name, chat_id FROM people WHERE LOWER(name) = ? AND chat_id IS NOT NULL",
            (key,)).fetchone()
        if row:
            out.append((row["name"], row["chat_id"]))
    conn.close()
    return out


def _norm_reminder_text(t):
    """Lowercase, strip punctuation and filler so two phrasings of the same errand match."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower())
    drop = {"the", "a", "an", "to", "at", "on", "for", "and", "please", "pls",
            "today", "tonight", "tomorrow", "remind", "reminder", "me", "us"}
    return " ".join(w for w in t.split() if w not in drop)


def _find_duplicate_reminder(conn, text, due_iso, chat, window_minutes=30):
    """An unfired reminder for the same person, at ~the same time, about the same thing.

    A4: Kim had to ask four times to get one pharmacy reminder set (a 529 ate the first
    attempt), and Guppi cheerfully created it twice - same person, same 5pm, same errand.
    Nothing checked. Matching is fuzzy because the two texts differed by one word
    ("Stop at pharmacy..." vs "Stop at the pharmacy...")."""
    try:
        due = datetime.datetime.fromisoformat(due_iso)
        if due.tzinfo is None:
            due = due.replace(tzinfo=TIMEZONE)
    except (ValueError, TypeError):
        return None
    want = _norm_reminder_text(text)
    if not want:
        return None
    want_set = set(want.split())
    rows = conn.execute(
        "SELECT id, text, due_at FROM reminders WHERE fired = 0 AND "
        "(for_chat = ? OR (for_chat IS NULL AND ? IS NULL))",
        (str(chat) if chat else None, str(chat) if chat else None)).fetchall()
    for r in rows:
        try:
            other = datetime.datetime.fromisoformat(r["due_at"])
            if other.tzinfo is None:
                other = other.replace(tzinfo=TIMEZONE)
        except (ValueError, TypeError):
            continue
        if abs((other - due).total_seconds()) > window_minutes * 60:
            continue
        have = _norm_reminder_text(r["text"])
        have_set = set(have.split())
        if not have_set:
            continue
        overlap = len(want_set & have_set) / max(1, min(len(want_set), len(have_set)))
        if want == have or want in have or have in want or overlap >= 0.7:
            return r
    return None


def tool_add_reminder(text, due_iso, for_chat, created_by, repeat="none"):
    # Guard: a one-time reminder set in the past fires instantly, which looks broken.
    # It almost always means the model mis-computed the time. Reject it rather than
    # silently misbehave, and tell the model what "now" actually is so it can retry.
    # (Prompts are suggestions; code is a guarantee — same principle as Trap 17.)
    if (repeat or "none").lower() == "none":
        try:
            due_dt = datetime.datetime.fromisoformat(due_iso)
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=TIMEZONE)
            if due_dt <= now_local():
                return (f"That time ({due_iso}) is in the past. Right now it is "
                        f"{now_local().isoformat(timespec='seconds')}. Recompute the "
                        f"time from that and try again.")
        except ValueError:
            return f"I couldn't read '{due_iso}' as a date and time. Use ISO 8601."

    if (repeat or "none").lower() == "none":
        _c = db()
        try:
            dup = _find_duplicate_reminder(_c, text, due_iso, for_chat)
        finally:
            _c.close()
        if dup:
            print(f"[reminder] duplicate suppressed (matches id={dup['id']})")
            return (f"There's already a reminder for that at the same time: "
                    f"'{dup['text']}' (id {dup['id']}). I did NOT add a second one - tell "
                    f"the user it was already set rather than claiming you made a new one.")

    repeat = (repeat or "none").lower()
    valid = {"none", "daily", "weekly", "monthly",
             "weekly:mon","weekly:tue","weekly:wed","weekly:thu",
             "weekly:fri","weekly:sat","weekly:sun"}
    if repeat not in valid:
        repeat = "none"
    conn = db()
    conn.execute(
        "INSERT INTO reminders (text, due_at, for_chat, created_by, repeat) VALUES (?,?,?,?,?)",
        (text, due_iso, for_chat, created_by, repeat))
    conn.commit()
    conn.close()
    tail = "" if repeat == "none" else f" (repeats {repeat.replace(':',' ')})"
    return f"Reminder set: '{text}' for {due_iso}{tail}."


def tool_nudge(target, text, due_iso, created_by, sender_role, repeat="none"):
    """Set a reminder FOR someone else (or a group). Parents only for others; anyone
    can effectively remind themselves via the normal add_reminder path."""
    if sender_role != "adult":
        return "Only a parent can set a reminder for someone else."
    people = resolve_targets(target)
    if not people:
        return (f"{target} isn't set up with me yet. A parent can invite them, then they "
                f"send me /start.")
    repeat = (repeat or "none").lower()
    conn = db()
    # A4: the duplicate pharmacy reminder came through THIS path, not add_reminder.
    if repeat == "none":
        already = []
        for name, chat in people:
            if _find_duplicate_reminder(conn, text, due_iso, chat):
                already.append(name)
        if already and len(already) == len(people):
            conn.close()
            print(f"[nudge] duplicate suppressed for {', '.join(already)}")
            return (f"{' and '.join(already)} already has a reminder for that at the same "
                    f"time - I did NOT add a second one. Say it was already set rather "
                    f"than claiming you just made it.")
    for name, chat in people:
        conn.execute(
            "INSERT INTO reminders (text, due_at, for_chat, created_by, repeat) "
            "VALUES (?,?,?,?,?)", (text, due_iso, chat, created_by, repeat))
    conn.commit(); conn.close()
    who = ", ".join(n for n, _ in people)
    tail = "" if repeat == "none" else f" (repeats {repeat.replace(':',' ')})"
    return f"Reminder set for {who}: '{text}' at {due_iso}{tail}."


def tool_list_reminders(for_chat=None):
    """List upcoming reminders. If for_chat is given, only that person's (plus family-wide
    ones with no specific owner); otherwise all. IDs are shown so they can be deleted."""
    conn = db()
    if for_chat:
        rows = conn.execute(
            "SELECT id, text, due_at, repeat, for_chat FROM reminders "
            "WHERE fired = 0 AND (for_chat = ? OR for_chat IS NULL) ORDER BY due_at",
            (str(for_chat),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text, due_at, repeat, for_chat FROM reminders "
            "WHERE fired = 0 ORDER BY due_at").fetchall()
    conn.close()
    if not rows:
        return "No upcoming reminders."
    out = []
    for r in rows:
        rep = "" if (r["repeat"] or "none") == "none" else f" (repeats {r['repeat'].replace(':',' ')})"
        out.append(f"[{r['id']}] {r['due_at']}: {r['text']}{rep}")
    return "\n".join(out)


def tool_delete_reminder(reminder_id, requester_chat, requester_role):
    """Delete a reminder by id (ids come from list_reminders). A person can delete their
    own reminders (or family-wide ones); a parent can delete any."""
    conn = db()
    row = conn.execute("SELECT text, for_chat FROM reminders WHERE id = ?",
                       (reminder_id,)).fetchone()
    if not row:
        conn.close()
        return "I couldn't find that reminder - it may already be gone. Try listing them again."
    # Permission: parents can delete anything; others only their own or family-wide.
    owns = (row["for_chat"] is None) or (str(row["for_chat"]) == str(requester_chat))
    if requester_role != "adult" and not owns:
        conn.close()
        return "That reminder belongs to someone else, so I can't remove it for you."
    conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()
    return f"Deleted the reminder: {row['text']}."


# ---- Shared lists -----------------------------------------------------------
def tool_add_occasion(title, kind, month, day, year=None, scope="family",
                      creator_chat=None, notes=None, created_by=None):
    """Register a special occasion that should get escalating reminders (a birthday,
    anniversary, holiday, vacation, or renewal). Annual by default; set year for a
    one-off like a specific vacation.

    scope: 'family' (default) -> both parents get the nudges; 'just_me' -> only the
    person who added it. A family birthday should be 'family' so both parents are
    reminded; something only one parent handles (their own work travel, a renewal they
    own) can be 'just_me'."""
    kind = (kind or "other").lower().strip()
    valid = {"birthday", "anniversary", "holiday", "vacation", "renewal", "other"}
    if kind not in valid:
        kind = "other"
    try:
        month = int(month); day = int(day)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return "That date doesn't look right - give me a month (1-12) and day (1-31)."
    except (ValueError, TypeError):
        return "I need a numeric month and day for the occasion."
    # for_chat NULL => all parents get it (family); a specific chat => only that person.
    for_chat = str(creator_chat) if (scope == "just_me" and creator_chat) else None
    conn = db()
    conn.execute(
        "INSERT INTO occasions (title, kind, month, day, year, for_chat, notes, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (title, kind, month, day, year, for_chat, notes, created_by,
         now_local().isoformat()))
    conn.commit(); conn.close()
    when = datetime.date(2000, month, day).strftime("%B %-d")
    yr = f", {year}" if year else " (every year)"
    leads = _OCCASION_LEADS.get(kind, _DEFAULT_LEADS)
    who = "just you" if for_chat else "you and your partner"

    # If it was added INSIDE the normal lead window, only the milestones still ahead will
    # fire THIS year — say so, so there's no silent surprise (a birthday added 4 days out
    # only gets the 1-day nudge this cycle; next year it gets them all).
    today = now_local().date()
    nxt = _occasion_next_date(month, day, year, today)
    remaining = [d for d in leads if nxt and (nxt - today).days <= d and (nxt - today).days >= 0]
    all_ahead = [d for d in leads if nxt and (nxt - today).days <= d]
    lead_str = "/".join(str(d) for d in leads)

    base = (f"Got it - tracking {title} ({kind}) on {when}{yr}. "
            f"I'll remind {who} {lead_str} days before, with help when it's close.")
    if nxt:
        days_away = (nxt - today).days
        first_missed = [d for d in leads if d > days_away]
        if first_missed and days_away >= 0 and not year:
            got = [d for d in leads if d <= days_away]
            got_str = (", ".join(str(d) for d in got) + "-day") if got else "no"
            base += (f" Heads up: it's only {days_away} days away, so this year you'll just "
                     f"get the {got_str} reminder(s) - you'll get the full set next year.")
        elif first_missed and days_away >= 0 and year:
            base += (f" Heads up: it's only {days_away} days away, so some of the earlier "
                     f"reminders have already passed.")
    return base


def tool_list_occasions():
    """List the special occasions Guppi is tracking."""
    conn = db()
    rows = conn.execute(
        "SELECT id, title, kind, month, day, year FROM occasions "
        "ORDER BY month, day").fetchall()
    conn.close()
    if not rows:
        return "I'm not tracking any special occasions yet."
    out = []
    for r in rows:
        when = datetime.date(2000, r["month"], r["day"]).strftime("%b %-d")
        yr = f" {r['year']}" if r["year"] else ""
        out.append(f"[{r['id']}] {when}{yr} - {r['title']} ({r['kind']})")
    return "Special occasions I'm tracking:\n" + "\n".join(out)


def tool_delete_occasion(occasion_id):
    """Stop tracking a special occasion."""
    conn = db()
    row = conn.execute("SELECT title FROM occasions WHERE id = ?",
                       (occasion_id,)).fetchone()
    if not row:
        conn.close()
        return "I couldn't find that occasion - try listing them again."
    conn.execute("DELETE FROM occasions WHERE id = ?", (occasion_id,))
    conn.commit(); conn.close()
    return f"Stopped tracking {row['title']}."


# Which lead-time milestones each kind gets, and how the nudge is framed.
_OCCASION_LEADS = {
    "vacation": [90, 30, 7, 1],
}
_DEFAULT_LEADS = [30, 7, 1]

def _occasion_next_date(month, day, year, today):
    """The next occurrence of an occasion as a date. For annual (year=None) it's this
    year or next; for a one-off it's the fixed date."""
    if year:
        try:
            return datetime.date(int(year), month, day)
        except ValueError:
            return None
    # Annual: this year if not yet passed, else next year. Handle Feb 29 gracefully.
    for yr in (today.year, today.year + 1):
        try:
            d = datetime.date(yr, month, day)
        except ValueError:
            d = datetime.date(yr, month, 28) if month == 2 else None
        if d and d >= today:
            return d
    return None


def _occasion_message(occ, days_out):
    """Build the escalating nudge + help offer for an occasion at a given milestone."""
    title, kind = occ["title"], occ["kind"]
    gift_kinds = {"birthday", "anniversary", "holiday"}
    if days_out == 1:
        base = f"Tomorrow: {title}."
    elif days_out == 7:
        base = f"One week until {title}."
    elif days_out == 30:
        base = f"About a month until {title}."
    elif days_out == 90:
        base = f"About 3 months until {title}."
    else:
        base = f"{days_out} days until {title}."

    if kind in gift_kinds:
        if days_out >= 30:
            offer = " Want some gift ideas, or should I set a reminder to shop?"
        elif days_out == 7:
            offer = " Time to get the gift sorted - want ideas or a reminder to order?"
        else:
            offer = " Last chance for anything you still need."
    elif kind == "vacation":
        if days_out >= 30:
            offer = " Want me to start a packing list or a prep to-do list?"
        elif days_out == 7:
            offer = " Want to review the packing list and trip details?"
        else:
            offer = " Final check - bags packed, plans confirmed?"
    elif kind == "renewal":
        offer = " Want me to set a reminder to take care of it?"
    else:
        offer = " Want me to set a reminder or help you prepare?"

    note = f" ({occ['notes']})" if occ["notes"] else ""
    return base + note + offer


def job_occasion_reminders():
    """Once a day: for each tracked occasion, if today is exactly a milestone (90/30/7/1
    days out, per kind), send the escalating nudge + help offer to the right parent(s).
    De-duped per occasion+year+milestone so each fires once."""
    if not proactive_on() or in_quiet_hours():
        return
    today = now_local().date()
    conn = db()
    occ_rows = conn.execute("SELECT * FROM occasions").fetchall()
    conn.close()
    if not occ_rows:
        return

    parents = _adults_with_chats()
    for occ in occ_rows:
        nxt = _occasion_next_date(occ["month"], occ["day"], occ["year"], today)
        if not nxt:
            continue
        days_out = (nxt - today).days
        leads = _OCCASION_LEADS.get(occ["kind"], _DEFAULT_LEADS)
        if days_out not in leads:
            continue
        # Dedupe: fire each milestone once per occurrence.
        sig = f"occasion_fired_{occ['id']}_{nxt.isoformat()}_{days_out}"
        if get_setting(sig):
            continue
        msg = _occasion_message(occ, days_out)
        targets = ([(None, occ["for_chat"])] if occ["for_chat"]
                   else parents)
        for _n, chat in targets:
            if chat:
                send_message(chat, msg, proactive=True)
        set_setting(sig, "1")


def tool_add_commitment(task, who=None, when_text=None, created_by=None):
    """Record who agreed to do a household task ('Jason is picking up Charlotte at 3').
    Shared logistics, so it works in the group. Keeps the loop closed on verbal plans."""
    conn = db()
    conn.execute("INSERT INTO commitments (task, who, when_text, created_by, created_at) "
                 "VALUES (?,?,?,?,?)",
                 (task, who, when_text, created_by, now_local().isoformat()))
    conn.commit()
    conn.close()
    whopart = f"{who} " if who else ""
    whenpart = f" ({when_text})" if when_text else ""
    return f"Noted: {whopart}on '{task}'{whenpart}."


def tool_list_commitments(include_done=False):
    """Show open household commitments — who's doing what. Answers 'what's on our plate?'
    and 'who's got what today?'."""
    conn = db()
    if include_done:
        rows = conn.execute("SELECT id, task, who, when_text, done FROM commitments "
                            "ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT id, task, who, when_text, done FROM commitments "
                            "WHERE done = 0 ORDER BY id").fetchall()
    conn.close()
    if not rows:
        return "Nothing on the shared list right now."
    out = []
    for r in rows:
        who = r["who"] or "someone"
        when = f" — {r['when_text']}" if r["when_text"] else ""
        mark = " ✓" if r["done"] else ""
        out.append(f"[{r['id']}] {who}: {r['task']}{when}{mark}")
    return "\n".join(out)


def tool_complete_commitment(commitment_id):
    """Mark a household commitment done ('Charlotte's picked up')."""
    conn = db()
    row = conn.execute("SELECT task, who FROM commitments WHERE id = ?",
                       (commitment_id,)).fetchone()
    if not row:
        conn.close()
        return "I couldn't find that one - try listing what's on the plate again."
    conn.execute("UPDATE commitments SET done = 1 WHERE id = ?", (commitment_id,))
    conn.commit()
    conn.close()
    return f"Marked done: {row['task']}."


def tool_add_to_list(list_name, item, added_by):
    conn = db()
    conn.execute("INSERT INTO list_items (list_name, item, added_by, created_at) VALUES (?,?,?,?)",
                 (list_name.lower(), item, added_by, now_local().isoformat()))
    conn.commit()
    conn.close()
    return f"Added '{item}' to the {list_name} list."


def tool_add_items_to_list(list_name, items, added_by):
    """Add several items to a list in one operation. `items` is a list of strings.
    Used for 'build me a grocery list' or 'here's my packing list: a, b, c'."""
    if not items:
        return "No items to add."
    conn = db()
    now = now_local().isoformat()
    for it in items:
        it = str(it).strip()
        if it:
            conn.execute(
                "INSERT INTO list_items (list_name, item, added_by, created_at) VALUES (?,?,?,?)",
                (list_name.lower(), it, added_by, now))
    conn.commit()
    conn.close()
    n = len([i for i in items if str(i).strip()])
    return f"Added {n} items to the {list_name} list."


def tool_show_all_lists():
    """Show the names of every list that has items, with how many items each holds.
    Answers 'what lists do I have?' — previously Guppi had no way to do this."""
    conn = db()
    rows = conn.execute(
        "SELECT list_name, COUNT(*) AS n FROM list_items GROUP BY list_name "
        "ORDER BY list_name").fetchall()
    conn.close()
    if not rows:
        return "There are no lists yet. Add something like 'add milk to the grocery list'."
    lines = [f"{r['list_name']} ({r['n']} item{'s' if r['n'] != 1 else ''})" for r in rows]
    return "Your lists:\n- " + "\n- ".join(lines)


def tool_show_list(list_name):
    conn = db()
    rows = conn.execute(
        "SELECT id, item, added_by FROM list_items WHERE list_name = ? ORDER BY id",
        (list_name.lower(),)).fetchall()
    conn.close()
    if not rows:
        return f"The {list_name} list is empty."
    return "\n".join(f"[{r['id']}] {r['item']}" +
                     (f" (added by {r['added_by']})" if r["added_by"] else "") for r in rows)


def tool_clear_list(list_name):
    """Empty an entire list (e.g. after the grocery run)."""
    conn = db()
    cur = conn.execute("DELETE FROM list_items WHERE list_name = ?", (list_name.lower(),))
    conn.commit(); n = cur.rowcount; conn.close()
    return f"Cleared the {list_name} list ({n} items removed)." if n else f"The {list_name} list was already empty."


def tool_check_off_item(list_name, item_text):
    """Remove an item from a list by matching its text (case-insensitive, partial).
    Lets someone say 'check off milk' without needing the item's id number."""
    conn = db()
    rows = conn.execute(
        "SELECT id, item FROM list_items WHERE list_name = ?", (list_name.lower(),)).fetchall()
    match = None
    for r in rows:
        if item_text.strip().lower() in r["item"].lower():
            match = r; break
    if not match:
        conn.close()
        return f"I couldn't find '{item_text}' on the {list_name} list."
    conn.execute("DELETE FROM list_items WHERE id = ?", (match["id"],))
    conn.commit(); conn.close()
    return f"Checked off {match['item']}."


def tool_remove_from_list(item_id):
    conn = db()
    cur = conn.execute("DELETE FROM list_items WHERE id = ?", (item_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return "Removed." if deleted else "I couldn't find that item."




def tool_save_template(template_name, from_list=None, items=None):
    """Save a reusable template. Either from an existing list (from_list) or from an
    explicit set of items."""
    if from_list:
        conn = db()
        rows = conn.execute("SELECT item FROM list_items WHERE list_name = ?",
                            (from_list.lower(),)).fetchall()
        conn.close()
        items = [r["item"] for r in rows]
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return "There's nothing to save as a template."
    conn = db()
    conn.execute("INSERT OR REPLACE INTO list_templates (name, items_json) VALUES (?, ?)",
                 (template_name.lower(), json.dumps(items)))
    conn.commit(); conn.close()
    return f"Saved '{template_name}' template with {len(items)} items."


def tool_start_from_template(template_name, list_name, added_by):
    """Populate a list from a saved template."""
    conn = db()
    row = conn.execute("SELECT items_json FROM list_templates WHERE name = ?",
                       (template_name.lower(),)).fetchone()
    if not row:
        conn.close()
        return f"I don't have a template called '{template_name}'."
    items = json.loads(row["items_json"])
    now = now_local().isoformat()
    for it in items:
        conn.execute("INSERT INTO list_items (list_name, item, added_by, created_at) VALUES (?,?,?,?)",
                     (list_name.lower(), it, added_by, now))
    conn.commit(); conn.close()
    return f"Started the {list_name} list from '{template_name}' ({len(items)} items)."


def tool_list_templates():
    conn = db()
    rows = conn.execute("SELECT name FROM list_templates ORDER BY name").fetchall()
    conn.close()
    if not rows:
        return "No saved templates yet."
    return "Templates: " + ", ".join(r["name"] for r in rows)


# =============================================================================
#  TOOL DEFINITIONS  (filtered per role before Claude ever sees them)
# =============================================================================
def tools_for_role(role, is_group=False):
    """Return only the tools this person may use. Claude never even SEES a tool the
    sender isn't permitted to call. Permissions live in code, not in the prompt.

    is_group: in the family GROUP chat, every reply is visible to everyone in the room
    (including the children and the caregiver). So private capabilities — EMAIL, memory
    recall, and settings — are withheld there entirely. Not by asking the model nicely:
    by not handing it the tools. Prompts are suggestions; code is a guarantee.
    """
    perms = dict(PERMISSIONS.get(role, PERMISSIONS["unknown"]))

    # An unrecognized chat gets NO family data at all. Only harmless web search.
    if role == "unknown":
        return [{"type": "web_search_20250305", "name": "web_search"}]

    # GROUP PRIVACY: no email in a room the whole family can read.
    if is_group:
        perms["email"] = False

    tools = []

    # Weather is family-safe - no private data, useful to everyone, fine in the group - so
    # every known role gets it, children included.
    #
    # It exists as a TOOL, not just the briefing's internal call, because of Trap 53: with
    # no tool behind it, "what's the weather tomorrow" had no mechanism at all, and the
    # model answered from imagination with a confident, entirely invented forecast.
    tools.append({
        "name": "weather",
        "description": ("Get the REAL forecast for the family's location. You have no "
                        "other source for weather - never state a temperature, condition, "
                        "or chance of rain without calling this first, and never answer "
                        "from memory. Use for 'what's the weather', 'will it rain "
                        "tomorrow', 'do the girls need coats', or whenever a plan depends "
                        "on conditions."),
        "input_schema": {"type": "object", "properties": {
            "when": {"type": "string", "enum": ["today", "tomorrow", "next_3_days"],
                     "description": "Which day(s) to report. Defaults to today."}}}})

    if perms["calendar_read"]:
        tools.append({
            "name": "check_calendar",
            "description": "Check upcoming events on the family's Google Calendar.",
            "input_schema": {"type": "object", "properties": {
                "days_ahead": {"type": "integer", "description": "Days ahead (default 7)."}}}})

    if perms["calendar_write"]:
        tools.append({
            "name": "add_calendar_event",
            "description": ("Add an event to the family's Google Calendar. Put the place "
                            "in `location` (Google makes it tappable for directions), and "
                            "the practical logistics in `details`: what to bring, drop-off "
                            "and pick-up instructions, cost, who to contact, arrival "
                            "notes. Aim for what a parent needs when they open the event "
                            "on their phone in the car - roughly a short paragraph. Do NOT "
                            "paste an entire email in; summarise the parts that matter at "
                            "the event and leave out policies, refund rules and marketing. "
                            "IF THE EVENT REPEATS (a camp running several days, a weekly "
                            "practice), do NOT call this tool once per day - make ONE event "
                            "and set `repeat`, with `repeat_count` or `repeat_until`. Each "
                            "occurrence must start and end on the SAME day; the repeat "
                            "covers the rest. Times are ISO 8601 with timezone offset, "
                            "e.g. 2026-07-12T10:00:00-04:00."),
            "input_schema": {"type": "object", "properties": {
                "summary": {"type": "string", "description": "Short event title."},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "location": {"type": "string",
                             "description": "Address or place name, if known."},
                "details": {"type": "string",
                            "description": ("All other useful info: attendees, what to "
                                            "bring, cost, contacts, instructions, notes.")},
                "personal": {"type": "boolean",
                             "description": ("True when the event is just for the person "
                                             "asking - a work obligation or personal "
                                             "commitment they describe as 'for me' / 'just "
                                             "me' / 'my [work] thing', rather than a shared "
                                             "family event. Personal events are colored "
                                             "sage on the calendar.")},
                "repeat": {"type": "string",
                           "enum": ["none", "daily", "weekdays", "weekly", "monthly"],
                           "description": ("Use for anything happening on more than one "
                                           "day. A Mon-Thu camp is 'daily' with "
                                           "repeat_count 4.")},
                "repeat_count": {"type": "integer",
                                 "description": "How many occurrences in total."},
                "repeat_until": {"type": "string",
                                 "description": ("Last date, YYYY-MM-DD. Use this OR "
                                                 "repeat_count, not both.")}},
                "required": ["summary", "start_iso", "end_iso"]}})
        tools.append({
            "name": "find_events",
            "description": ("Find upcoming events, each returned WITH its id. Use this "
                            "FIRST whenever the user wants to change or cancel an event, "
                            "so you know which event to act on. Optional `query` filters "
                            "by title text (e.g. 'dentist', 'game')."),
            "input_schema": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Text to match in event titles."},
                "days_ahead": {"type": "integer", "description": "How far ahead (default 30)."}}}})
        tools.append({
            "name": "edit_calendar_event",
            "description": ("Change an existing event. Provide event_id (from find_events) "
                            "and ONLY the fields to change — omit anything staying the same. "
                            "Use for 'move the dentist to 3pm', 'rename X', 'add a location'."),
            "input_schema": {"type": "object", "properties": {
                "event_id": {"type": "string"},
                "summary": {"type": "string"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "location": {"type": "string"},
                "details": {"type": "string"}},
                "required": ["event_id"]}})
        tools.append({
            "name": "delete_calendar_event",
            "description": ("Delete/cancel an event by event_id (from find_events). "
                            "Confirm with the user which event before deleting if there's "
                            "any ambiguity. If find_events marked the event as a REPEATING "
                            "SERIES, the id is ONE occurrence - ask whether they mean that "
                            "single day or the whole series, and set whole_series "
                            "accordingly. Never delete a whole series without asking."),
            "input_schema": {"type": "object", "properties": {
                "event_id": {"type": "string"},
                "whole_series": {"type": "boolean",
                                 "description": ("True to remove every occurrence of a "
                                                 "repeating event, not just this one.")}},
                "required": ["event_id"]}})
        tools.append({
            "name": "add_flight",
            "description": ("Add a work/travel trip to the calendar from flight NUMBERS. "
                            "Use when someone gives a flight number and date ('I'm on "
                            "AA1234 July 22, back on AA1235 the 26th'). It looks up the "
                            "real airports and times and creates a trip-block event (from "
                            "1 hour before outbound departure through the return arrival) "
                            "PLUS an event for each flight. Dates must be YYYY-MM-DD; infer "
                            "the year from context if not stated. Include the return flight "
                            "whenever it's given."),
            "input_schema": {"type": "object", "properties": {
                "outbound_number": {"type": "string", "description": "e.g. 'AA1234'"},
                "outbound_date": {"type": "string", "description": "YYYY-MM-DD"},
                "return_number": {"type": "string"},
                "return_date": {"type": "string", "description": "YYYY-MM-DD"}},
                "required": ["outbound_number", "outbound_date"]}})

    if perms["email"]:
        tools.append({
            "name": "search_email",
            "description": "Search this person's own Gmail. Uses Gmail search syntax.",
            "input_schema": {"type": "object", "properties": {
                "query": {"type": "string"}}, "required": ["query"]}})
        tools.append({
            "name": "read_email",
            "description": ("Read the FULL text of a specific email (not just a snippet). "
                            "Use this - not search_email - whenever someone asks what an "
                            "email says, or wants an event or reminder created from an "
                            "email. It returns the whole message so you can pull out the "
                            "who / what / when / where. `query` finds the email: use plain "
                            "topic words (e.g. 'field trip permission', 'Azie appointment') "
                            "or from:<address-or-domain> (e.g. 'from:swarthmore') - never "
                            "put a subject phrase after from:."),
            "input_schema": {"type": "object", "properties": {
                "query": {"type": "string"}}, "required": ["query"]}})
        tools.append({
            "name": "draft_email",
            "description": ("Prepare an email draft and show it to the user for approval. "
                            "This does NOT send - it drafts and previews. Use when the "
                            "user wants to send or reply to an email ('email the coach we'll "
                            "be late', 'reply to the PTA that I'll volunteer'). Write a "
                            "clear, appropriately-toned message in the user's voice. If "
                            "replying to an email you found via read_email, pass its "
                            "reply_headers so it threads correctly. After drafting, the "
                            "user will say 'send it' or ask for changes - never send "
                            "without that confirmation."),
            "input_schema": {"type": "object", "properties": {
                "to_addr": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "The full message text."},
                "reply_headers": {"type": "object", "description": (
                    "For replies only: {message_id, thread_id} from the email being "
                    "replied to, so it threads."), "properties": {
                        "message_id": {"type": "string"},
                        "thread_id": {"type": "string"}}}},
                "required": ["to_addr", "body"]}})
        tools.append({
            "name": "send_pending_email",
            "description": ("Send the draft the user just approved. Only call this after "
                            "draft_email showed a draft AND the user clearly confirmed "
                            "('send it', 'yes send', 'go ahead'). Never call it on your own."),
            "input_schema": {"type": "object", "properties": {}}})
        tools.append({
            "name": "discard_draft",
            "description": "Throw away the pending email draft if the user changes their mind.",
            "input_schema": {"type": "object", "properties": {}}})

    # ---- Always available (shared, family-safe) ----
    tools += [
        {"name": "add_reminder",
         "description": ("Store a reminder for the person asking. due_iso is ISO 8601 with "
                         "timezone offset. For a recurring reminder set repeat to one of: "
                         "daily, weekly, monthly, or weekly:mon/tue/wed/thu/fri/sat/sun "
                         "(e.g. 'every Sunday' -> weekly:sun)."),
         "input_schema": {"type": "object", "properties": {
             "text": {"type": "string"}, "due_iso": {"type": "string"},
             "repeat": {"type": "string",
                        "description": "none|daily|weekly|monthly|weekly:<day>"}},
             "required": ["text", "due_iso"]}},
        {"name": "list_reminders",
         "description": "Show upcoming reminders, each with an [id] you can use to delete it.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "delete_reminder",
         "description": ("Delete/cancel a reminder by its id (ids come from "
                         "list_reminders). To remove a reminder the user named, you MUST "
                         "call list_reminders to get its id, then ACTUALLY call this tool "
                         "with that id in the SAME turn. Never tell the user a reminder is "
                         "deleted unless this tool has returned a success message - listing "
                         "it is not deleting it."),
         "input_schema": {"type": "object", "properties": {
             "reminder_id": {"type": "integer"}}, "required": ["reminder_id"]}},
        {"name": "add_to_list",
         "description": "Add an item to a shared list, e.g. 'grocery' or 'todo'.",
         "input_schema": {"type": "object", "properties": {
             "list_name": {"type": "string"}, "item": {"type": "string"}},
             "required": ["list_name", "item"]}},
        {"name": "add_items_to_list",
         "description": ("Add MANY items to a list at once. Use for 'build me a grocery "
                         "list for tacos' (generate the items, then save them all) or "
                         "'here's my packing list: a, b, c'. Prefer this over calling "
                         "add_to_list repeatedly."),
         "input_schema": {"type": "object", "properties": {
             "list_name": {"type": "string"},
             "items": {"type": "array", "items": {"type": "string"}}},
             "required": ["list_name", "items"]}},
        {"name": "show_list",
         "description": "Show the items in ONE shared list by name.",
         "input_schema": {"type": "object", "properties": {
             "list_name": {"type": "string"}}, "required": ["list_name"]}},
        {"name": "show_all_lists",
         "description": ("Show the NAMES of all lists that exist, with item counts. Use "
                         "when asked 'what lists do I have?' or 'what lists exist?'."),
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "clear_list",
         "description": "Empty an entire list, e.g. after the grocery run.",
         "input_schema": {"type": "object", "properties": {
             "list_name": {"type": "string"}}, "required": ["list_name"]}},
        {"name": "check_off_item",
         "description": "Remove one item from a list by name (e.g. 'check off milk').",
         "input_schema": {"type": "object", "properties": {
             "list_name": {"type": "string"}, "item_text": {"type": "string"}},
             "required": ["list_name", "item_text"]}},
        {"name": "remove_from_list",
         "description": "Remove an item from a list by its id (ids come from show_list).",
         "input_schema": {"type": "object", "properties": {
             "item_id": {"type": "integer"}}, "required": ["item_id"]}},
        {"name": "save_template",
         "description": ("Save a reusable list template, from an existing list (from_list) "
                         "or from explicit items. E.g. 'save this as my travel list'."),
         "input_schema": {"type": "object", "properties": {
             "template_name": {"type": "string"},
             "from_list": {"type": "string"},
             "items": {"type": "array", "items": {"type": "string"}}},
             "required": ["template_name"]}},
        {"name": "start_from_template",
         "description": "Populate a list from a saved template.",
         "input_schema": {"type": "object", "properties": {
             "template_name": {"type": "string"}, "list_name": {"type": "string"}},
             "required": ["template_name", "list_name"]}},
        {"name": "list_templates",
         "description": "Show the names of saved list templates.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "add_commitment",
         "description": ("Record who agreed to do a household task, so it's not forgotten. "
                         "Use when someone commits in conversation ('I'll pick up "
                         "Charlotte at 3', 'I've got the dentist run'). who = the person "
                         "responsible; when_text = a plain-language time like 'today at 3' "
                         "if given. This is shared family logistics and works in the group."),
         "input_schema": {"type": "object", "properties": {
             "task": {"type": "string"},
             "who": {"type": "string", "description": "Who is responsible."},
             "when_text": {"type": "string", "description": "When, in plain words."}},
             "required": ["task"]}},
        {"name": "list_commitments",
         "description": ("Show open household commitments (who's doing what). Use for "
                         "'what's on our plate?', 'who's got what?', 'what did we agree "
                         "on?'."),
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "complete_commitment",
         "description": "Mark a household commitment done, by its id (from list_commitments).",
         "input_schema": {"type": "object", "properties": {
             "commitment_id": {"type": "integer"}}, "required": ["commitment_id"]}},
        {"name": "add_occasion",
         "description": ("Track a special occasion that should get escalating reminders: "
                         "a birthday, anniversary, holiday, vacation, or annual renewal "
                         "(registration, checkup, taxes). Guppi will nudge 30/7/1 days "
                         "before (90/30/7/1 for vacations) and offer help - gift ideas for "
                         "birthdays/anniversaries/holidays, a packing list for vacations, "
                         "or an action reminder for renewals. Use for 'remember Mom's "
                         "birthday is March 5', 'our anniversary is June 12', 'we're going "
                         "to Disney July 20-27', 'car registration renews in October'. "
                         "kind must be one of: birthday, anniversary, holiday, vacation, "
                         "renewal, other. Give month and day as numbers; add year only for "
                         "a one-off (a specific vacation). scope: use 'family' (the "
                         "default) for anything both parents should be reminded of - "
                         "birthdays, anniversaries, family trips, shared renewals; use "
                         "'just_me' only when it's something one parent alone handles. "
                         "When unsure, prefer 'family'."),
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"},
             "kind": {"type": "string",
                      "enum": ["birthday", "anniversary", "holiday", "vacation",
                               "renewal", "other"]},
             "month": {"type": "integer"},
             "day": {"type": "integer"},
             "year": {"type": "integer", "description": "Only for one-off events."},
             "scope": {"type": "string", "enum": ["family", "just_me"],
                       "description": "family = both parents reminded (default); "
                                      "just_me = only the person adding it."},
             "notes": {"type": "string"}},
             "required": ["title", "kind", "month", "day"]}},
        {"name": "list_occasions",
         "description": "List the special occasions Guppi is tracking (birthdays, etc.).",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "delete_occasion",
         "description": "Stop tracking a special occasion, by its id (from list_occasions).",
         "input_schema": {"type": "object", "properties": {
             "occasion_id": {"type": "integer"}}, "required": ["occasion_id"]}},
        {"type": "web_search_20250305", "name": "web_search"},
    ]

    # ---- Private-chat only: memory is personal, so never in the group ----
    if not is_group:
        tools += [
            {"name": "remember",
             "description": ("Save a durable fact about the family (a preference, an "
                             "allergy, a standing detail). Follow the memory rules "
                             "strictly. IMPORTANT: do NOT use this for birthdays, "
                             "anniversaries, holidays, vacations, or annual renewals - "
                             "even if the user says 'remember'. Those are recurring dates "
                             "that need escalating reminders, so use add_occasion instead. "
                             "'Remember Lillian's birthday is April 13' -> add_occasion, "
                             "not remember."),
             "input_schema": {"type": "object", "properties": {
                 "fact": {"type": "string"},
                 "about": {"type": "string", "description": "Who the fact concerns, if anyone."}},
                 "required": ["fact"]}},
            {"name": "recall",
             "description": "Show what Guppi remembers. Optionally filtered to one person.",
             "input_schema": {"type": "object", "properties": {"about": {"type": "string"}}}},
            {"name": "forget",
             "description": "Delete a saved memory by its id (ids come from recall).",
             "input_schema": {"type": "object", "properties": {
                 "memory_id": {"type": "integer"}}, "required": ["memory_id"]}},
        ]

    # ---- Parents only ----
    if role == "adult":
        tools.append({
            "name": "nudge",
            "description": ("Set a reminder FOR someone else or a group (parents only). "
                            "target is a name (e.g. 'Lillian') or a group: 'the girls', "
                            "'the kids', 'the parents'. Use when a parent says 'remind the "
                            "girls...'. For a reminder for the SENDER, use add_reminder."),
            "input_schema": {"type": "object", "properties": {
                "target": {"type": "string"},
                "text": {"type": "string"},
                "due_iso": {"type": "string"},
                "repeat": {"type": "string"}},
                "required": ["target", "text", "due_iso"]}})
        tools.append({
            "name": "invite_person",
            "description": ("Invite a family member (child or caregiver) to use Guppi. "
                            "Give their name; they then send /start to the bot and get "
                            "linked automatically. Parents set themselves up with the "
                            "setup code instead."),
            "input_schema": {"type": "object", "properties": {
                "name": {"type": "string"}}, "required": ["name"]}})
        if not is_group:
            tools.append({
                "name": "manage_deadline_ignores",
                "description": ("Control which email senders Guppi ignores when flagging "
                                "deadlines/invoices. Use when a parent says things like "
                                "'ignore deadline emails from Todoist', 'stop flagging "
                                "Amazon', or 'what senders are you ignoring?'. action is "
                                "'add', 'remove', or 'list'; sender is a name or address "
                                "fragment like 'todoist'."),
                "input_schema": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["add", "remove", "list"]},
                    "sender": {"type": "string"}}, "required": ["action"]}})
            tools.append({
                "name": "manage_email_priorities",
                "description": ("Control which email senders are PRIORITY (always flagged, "
                                "even without a deadline keyword) or IGNORED (never "
                                "flagged) for this person, and view learned patterns. Use "
                                "when a parent says 'always flag emails from the school', "
                                "'the coach is important', 'never flag newsletters', or "
                                "'what are my email priorities?'. This is per-person. "
                                "action: 'prioritize', 'ignore', 'unprioritize', "
                                "'unignore', or 'list'. sender is a name/address fragment."),
                "input_schema": {"type": "object", "properties": {
                    "action": {"type": "string",
                               "enum": ["prioritize", "ignore", "unprioritize",
                                        "unignore", "list"]},
                    "sender": {"type": "string"}}, "required": ["action"]}})
            tools.append({
                "name": "backup_now",
                "description": ("Immediately back up Guppi's database and send the file to "
                                "this parent's Telegram. Use when a parent says 'back up "
                                "now', 'save a backup', or 'export the data'. This "
                                "replaces the previous backup in the chat."),
                "input_schema": {"type": "object", "properties": {}}})
            tools.append({
                "name": "connect_link",
                "description": ("Create a working sign-in link so someone can CONNECT or "
                                "RECONNECT an account. Use whenever anyone needs to link "
                                "or re-link Google (calendar + Gmail) or Microsoft/"
                                "live.com/Outlook email, or when a connection has expired. "
                                "You CANNOT write these links yourself - a hand-typed "
                                "/connect URL is refused by the server. person is whose "
                                "account it is; kind is 'google' or 'microsoft'. If they "
                                "need both, call it twice."),
                "input_schema": {"type": "object", "properties": {
                    "person": {"type": "string",
                               "description": "Whose account, e.g. 'Kim'."},
                    "kind": {"type": "string", "enum": ["google", "microsoft"]}},
                    "required": ["person"]}})
            tools.append({
                "name": "backup_link",
                "description": ("Give this parent a private, single-use, 15-minute link to "
                                "DOWNLOAD the backup file to their own device (kind="
                                "'download'), or the command to RESTORE the database from "
                                "a file they already have (kind='restore'). Use for 'send "
                                "me a backup link', 'let me download the database', 'I "
                                "want to save a copy', 'how do I restore'. Prefer this "
                                "over backup_now when they want a copy to KEEP, since it "
                                "goes to their device instead of leaving a credential "
                                "file sitting in the chat. kind='restore' is destructive "
                                "- only offer it if they clearly want to overwrite."),
                "input_schema": {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["download", "restore"]}}}})
            tools.append({
                "name": "connection_health",
                "description": ("Check whether this person's accounts (Google calendar/"
                                "Gmail, Microsoft/live.com email) are connected and "
                                "working right now. Use for 'are my accounts connected?', "
                                "'check my connections', 'is my email working?'."),
                "input_schema": {"type": "object", "properties": {}}})
            tools.append({
                "name": "list_calendars",
                "description": "List the Google calendars this account can see, with IDs.",
                "input_schema": {"type": "object", "properties": {}}})
            tools.append({
                "name": "show_settings",
                "description": "Show Guppi's current settings (caps, polling, quiet hours).",
                "input_schema": {"type": "object", "properties": {}}})
            tools.append({
                "name": "update_setting",
                "description": ("Change a setting. Parents only, private chat only. Keys: "
                                "daily_claude_calls_cap, daily_messages_cap, poll_minutes, "
                                "quiet_start_hour, quiet_end_hour, proactive_enabled."),
                "input_schema": {"type": "object", "properties": {
                    "key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"]}})
    return tools


def run_tool(name, tool_input, sender_name, sender_role, sender_chat, is_group=False):
    print(f"[tool] {sender_name or 'unknown'} ({sender_role}"
          f"{', GROUP' if is_group else ''}) called '{name}': {tool_input}")
    perms = dict(PERMISSIONS.get(sender_role, PERMISSIONS["unknown"]))

    # HARD GUARD 1: an unrecognized chat can never touch anything belonging to the
    # family, no matter what tool the model somehow tried to call. Fails closed.
    if sender_role == "unknown" and name != "web_search":
        print(f"[security] BLOCKED unknown sender {sender_chat} attempting '{name}'")
        return "I only help members of this household, and I don't recognize you."

    # HARD GUARD 2: private things never happen in the group chat, where everyone can
    # read the answer. Belt-and-braces on top of not offering the tool at all.
    GROUP_FORBIDDEN = {"search_email", "read_email", "draft_email", "send_pending_email",
                       "discard_draft", "recall", "remember", "forget",
                       "show_settings", "update_setting", "list_calendars",
                       "connection_health", "manage_deadline_ignores",
                       "manage_email_priorities", "backup_now", "backup_link",
                       "connect_link"}
    if is_group and name in GROUP_FORBIDDEN:
        print(f"[security] BLOCKED '{name}' in group chat")
        return ("That's private - I won't answer it here where everyone can see. "
                "Message me directly and I'll help.")

    if is_group:
        perms["email"] = False

    if name == "check_calendar":
        if not perms["calendar_read"]:
            return "You don't have calendar access."
        return tool_check_calendar(tool_input.get("days_ahead", 7), sender_name)

    if name == "add_calendar_event":
        if not perms["calendar_write"]:
            return "Only a parent or caregiver can add calendar events."
        return tool_add_calendar_event(
            tool_input["summary"], tool_input["start_iso"], tool_input["end_iso"],
            sender_name, tool_input.get("location"), tool_input.get("details"),
            tool_input.get("personal", False), tool_input.get("repeat"),
            tool_input.get("repeat_count"), tool_input.get("repeat_until"))

    if name == "find_events":
        if not perms["calendar_read"]:
            return "You don't have calendar access."
        return tool_find_events(tool_input.get("query"),
                                tool_input.get("days_ahead", 30), sender_name)

    if name == "edit_calendar_event":
        if not perms["calendar_write"]:
            return "Only a parent or caregiver can change calendar events."
        return tool_edit_calendar_event(
            tool_input["event_id"], sender_name, tool_input.get("summary"),
            tool_input.get("start_iso"), tool_input.get("end_iso"),
            tool_input.get("location"), tool_input.get("details"))

    if name == "delete_calendar_event":
        if not perms["calendar_write"]:
            return "Only a parent or caregiver can delete calendar events."
        return tool_delete_calendar_event(tool_input["event_id"], sender_name,
                                          tool_input.get("whole_series", False))

    if name == "add_flight":
        if not perms["calendar_write"]:
            return "Only a parent or caregiver can add calendar events."
        return tool_add_flight(
            tool_input["outbound_number"], tool_input["outbound_date"], sender_name,
            tool_input.get("return_number"), tool_input.get("return_date"))

    if name == "search_email":
        if not perms["email"]:
            return "You don't have email access."
        return tool_search_email(tool_input["query"], sender_name)

    if name == "read_email":
        if not perms["email"]:
            return "You don't have email access."
        return tool_read_email(tool_input["query"], sender_name)

    if name == "draft_email":
        if not perms["email"]:
            return "You don't have email access."
        return tool_draft_email(
            sender_name, tool_input["to_addr"], tool_input.get("subject"),
            tool_input["body"], tool_input.get("reply_headers"))
    if name == "send_pending_email":
        if not perms["email"]:
            return "You don't have email access."
        return tool_send_pending_email(sender_name, _LAST_USER_TEXT.get(sender_name))
    if name == "discard_draft":
        return tool_discard_draft(sender_name)

    if name == "list_calendars":
        if not perms["calendar_read"]:
            return "You don't have calendar access."
        return tool_list_calendars(sender_name)

    if name == "weather":
        return tool_weather(tool_input.get("when", "today"))
    if name == "backup_now":
        return run_backup(reason="you asked")
    if name == "backup_link":
        return tool_backup_link(tool_input.get("kind", "download"))
    if name == "connect_link":
        return tool_connect_link(tool_input.get("person") or sender_name,
                                 tool_input.get("kind", "google"))
    if name == "connection_health":
        return tool_connection_health(sender_name)
    if name == "manage_deadline_ignores":
        return tool_manage_deadline_ignores(tool_input["action"], tool_input.get("sender"))
    if name == "manage_email_priorities":
        return tool_manage_email_priorities(
            tool_input["action"], tool_input.get("sender"), sender_name)
    if name == "show_settings":
        return tool_show_settings()
    if name == "update_setting":
        return tool_update_setting(tool_input["key"], tool_input["value"], sender_role)

    if name == "remember":
        return tool_remember(tool_input["fact"], tool_input.get("about"), sender_name)
    if name == "recall":
        return tool_recall(tool_input.get("about"))
    if name == "forget":
        return tool_forget(tool_input["memory_id"])

    if name == "add_reminder":
        return tool_add_reminder(tool_input["text"], tool_input["due_iso"],
                                 sender_chat, sender_name,
                                 tool_input.get("repeat", "none"))
    if name == "list_reminders":
        return tool_list_reminders(sender_chat)
    if name == "delete_reminder":
        return tool_delete_reminder(tool_input["reminder_id"], sender_chat, sender_role)
    if name == "nudge":
        return tool_nudge(tool_input["target"], tool_input["text"], tool_input["due_iso"],
                          sender_name, sender_role, tool_input.get("repeat", "none"))

    if name == "add_to_list":
        return tool_add_to_list(tool_input["list_name"], tool_input["item"], sender_name)
    if name == "add_items_to_list":
        return tool_add_items_to_list(tool_input["list_name"], tool_input["items"],
                                      sender_name)
    if name == "show_list":
        return tool_show_list(tool_input["list_name"])
    if name == "show_all_lists":
        return tool_show_all_lists()
    if name == "clear_list":
        return tool_clear_list(tool_input["list_name"])
    if name == "check_off_item":
        return tool_check_off_item(tool_input["list_name"], tool_input["item_text"])
    if name == "remove_from_list":
        return tool_remove_from_list(tool_input["item_id"])
    if name == "save_template":
        return tool_save_template(tool_input["template_name"],
                                  tool_input.get("from_list"), tool_input.get("items"))
    if name == "start_from_template":
        return tool_start_from_template(tool_input["template_name"],
                                        tool_input["list_name"], sender_name)
    if name == "list_templates":
        return tool_list_templates()

    if name == "add_commitment":
        return tool_add_commitment(tool_input["task"], tool_input.get("who"),
                                   tool_input.get("when_text"), sender_name)
    if name == "list_commitments":
        return tool_list_commitments()
    if name == "complete_commitment":
        return tool_complete_commitment(tool_input["commitment_id"])

    if name == "add_occasion":
        return tool_add_occasion(
            tool_input["title"], tool_input["kind"], tool_input["month"],
            tool_input["day"], tool_input.get("year"),
            tool_input.get("scope", "family"), sender_chat,
            tool_input.get("notes"), sender_name)
    if name == "list_occasions":
        return tool_list_occasions()
    if name == "delete_occasion":
        return tool_delete_occasion(tool_input["occasion_id"])

    if name == "invite_person":
        return link_person(tool_input["name"], sender_role)

    return "Unknown tool."


# =============================================================================
#  THE USER GUIDE  —  one source of truth for "what can you do and how"
# =============================================================================
#  Written because the honest answer to "does a new person understand this?" was no.
#  Three things were wrong, and only one of them was a missing feature list:
#
#   1. There were TWO help systems that could disagree - /help sent welcome_message()
#      while "what can you do?" was answered by the MODEL from capabilities_for_role().
#   2. capabilities_for_role is a briefing that literally instructs "keep it to the
#      highlights", so a complete answer was impossible by construction.
#   3. The list had drifted eight features behind the tools, and nothing detected it.
#
#  So: ONE structure, delivered MODEL-FREE (the model cannot summarise away what it never
#  touches), organised by topic, and PHRASE-FIRST - the exact words to say. Knowing a
#  feature exists is useless if you can't guess the trigger, which was the real barrier.
#
#  `tools` on each topic is not decoration: _audit_guide_coverage() checks at startup that
#  every registered tool is documented somewhere here, and complains loudly otherwise. The
#  old list drifted because nothing was watching.
# =============================================================================

GUIDE_TOPICS = [
    {
        "key": "calendar", "title": "Calendar",
        "roles": ("adult", "caregiver", "child"), "private_only": False,
        "tools": ["check_calendar", "add_calendar_event", "edit_calendar_event",
                  "delete_calendar_event", "find_events", "list_calendars"],
        "summary": "See, add, change and delete events on the family calendar.",
        "body": [
            "SEE IT: \"what's on the calendar this week?\", \"what's on today?\", "
            "\"when is Charlotte's camp?\"",
            "ADD: \"add Reese's game Saturday 10am at the high school\". Tell me anything "
            "useful - what to bring, who's going, cost - and I'll put it in the event so "
            "it's there when you open it on your phone.",
            "JUST FOR YOU: say \"my work thing\" or \"just for me\" and I'll colour it "
            "sage so your own commitments stand out from family ones.",
            "CHANGE OR CANCEL: \"move the dentist to 3pm\", \"cancel Saturday's game\".",
            "SOMETHING ON SEVERAL DAYS: \"add camp 9am to 4pm Monday through Thursday\" - "
            "I make ONE repeating event, not four. To remove it, say whether you mean just "
            "that day or the whole thing; I'll ask if you don't.",
            "(Needs a Google Calendar connected - a parent does this once.)",
        ],
    },
    {
        "key": "reminders", "title": "Reminders and nudges",
        "roles": ("adult", "caregiver", "child"), "private_only": False,
        "tools": ["add_reminder", "list_reminders", "delete_reminder", "nudge"],
        "summary": "Get pinged about something - yourself, or someone else in the family.",
        "body": [
            "FOR YOURSELF: \"remind me to call the dentist Thursday at 10am\", "
            "\"remind me in an hour\", \"remind me tonight\".",
            "REPEATING: \"every Sunday at 7pm remind me to take out the recycling\".",
            "FOR SOMEONE ELSE (parents only): \"remind the girls about permission slips "
            "tomorrow at 7:30am\", \"remind Kim at 5 to collect the prescription\".",
            "SEE AND REMOVE: \"what reminders do I have?\", \"delete the recycling one\".",
            "If you ask twice for the same thing at the same time I won't create a second "
            "one - I'll tell you it's already set.",
        ],
    },
    {
        "key": "lists", "title": "Shared lists",
        "roles": ("adult", "caregiver", "child"), "private_only": False,
        "tools": ["add_to_list", "add_items_to_list", "show_all_lists", "show_list",
                  "check_off_item", "clear_list", "remove_from_list", "save_template",
                  "start_from_template", "list_templates"],
        "summary": "Grocery, packing, to-do - shared with everyone.",
        "body": [
            "\"add milk to the grocery list\", \"add eggs, bread and butter\"",
            "\"what's on the grocery list?\", \"what lists do I have?\"",
            "\"check off the milk\", \"take eggs off the list\", \"clear the grocery list\"",
            "REUSABLE: \"save this as my travel list\", then later \"start my travel "
            "list\" to bring it all back.",
        ],
    },
    {
        "key": "email", "title": "Email - reading and sending",
        "roles": ("adult",), "private_only": True,
        "tools": ["search_email", "read_email", "draft_email", "send_pending_email",
                  "discard_draft"],
        "summary": "Search and read your inbox, and send replies (always shown first).",
        "body": [
            "FIND AND READ: \"any important emails today?\", \"find the invoice from "
            "Mark\", \"read the email about the basketball camp\".",
            "I read the WHOLE email, including pictures attached to it - a parking map or a "
            "schedule graphic - so you can ask \"what does the map show?\"",
            "SEND: \"reply to the coach that we'll be late\", \"email Kim the grocery "
            "list\". I ALWAYS show you the draft first and send NOTHING until you say "
            "\"send it\". You can say \"change the wording\" or \"discard that\".",
            "This is your own inbox only, and only in a private chat - never in the group.",
            "(Needs your email connected. Say \"connect my email\" and I'll send a link. "
            "If you connected before sending existed, reconnect once to allow it.)",
        ],
    },
    {
        "key": "flags", "title": "What email I flag you about",
        "roles": ("adult",), "private_only": True,
        "tools": ["manage_email_priorities", "manage_deadline_ignores"],
        "summary": "Control which senders I chase you about, and which I leave alone.",
        "body": [
            "I watch new mail and flag deadlines, invoices and RSVPs, offering to set a "
            "reminder or add it to the calendar. I skip marketing and newsletters.",
            "ALWAYS TELL ME: \"always flag emails from the school\", \"the coach is "
            "important\".",
            "NEVER TELL ME: \"never flag newsletters\", \"ignore Robinhood\".",
            "STOP CHASING DEADLINES FROM ONE SENDER: \"stop flagging deadlines from "
            "Todoist\" - different from ignoring them entirely.",
            "SEE THE RULES: \"what are my email priorities?\"",
            "I also quietly notice what you act on versus ignore, and will OFFER to adjust. "
            "I only ever suggest - your rules always win.",
        ],
    },
    {
        "key": "occasions", "title": "Birthdays, anniversaries and renewals",
        "roles": ("adult",), "private_only": False,
        "tools": ["add_occasion", "list_occasions", "delete_occasion"],
        "summary": "Dates I warn you about well in advance, not on the day.",
        "body": [
            "\"remember Mom's birthday is March 5\", \"our anniversary is June 12\", "
            "\"we're going to Disney July 20-27\", \"the car registration renews in "
            "October\".",
            "I nudge you 30, 7 and 1 days ahead (90/30/7/1 for trips) and offer gift ideas "
            "or a packing list - early enough to actually do something.",
            "\"what occasions are you tracking?\", \"stop tracking the registration\".",
            "Say \"just me\" to keep one private; otherwise both parents get the warnings.",
        ],
    },
    {
        "key": "memory", "title": "What I remember",
        "roles": ("adult", "caregiver", "child"), "private_only": True,
        "tools": ["remember", "recall", "forget"],
        "summary": "Facts I keep about the family - and how to change or delete them.",
        "body": [
            "\"remember that Charlotte is allergic to peanuts\", \"remember Lillian's "
            "teacher is Mrs Bell\".",
            "\"what do you remember?\" - the full list, any time.",
            "\"forget that\", \"forget the bit about the teacher\".",
            "CORRECT ME: \"it's WSSD, not WADS\" - I replace the old fact, I don't keep both.",
            "I save durable things: names, relationships, allergies, standing arrangements. "
            "I deliberately DON'T save anything sensitive - health details, arguments, money, "
            "or how someone is doing. Dates go to occasions instead, so they get warnings.",
        ],
    },
    {
        "key": "group", "title": "Talking to me in the family group",
        "roles": ("adult", "caregiver", "child"), "private_only": False,
        "tools": ["add_commitment", "list_commitments", "complete_commitment"],
        "summary": "How to get my attention there, and what I keep out of it.",
        "body": [
            "In the group I stay quiet unless you're talking TO me - otherwise I'd interrupt "
            "every conversation. Say my name anywhere in the message (\"Guppi, add...\", "
            "\"can you check the calendar, Guppi?\"), or just reply to one of my messages.",
            "WHO'S DOING WHAT: say \"I'll grab Charlotte at 3\" and I'll note it. Ask "
            "\"what's on our plate?\" and I'll list who has what. \"I've got Charlotte\" "
            "marks it done.",
            "If I overhear something schedulable I'll OFFER once - I never add it unless you "
            "say yes.",
            "NEVER in the group: your email, saved memories, settings, backups. Ask me those "
            "privately and I'll answer properly.",
        ],
    },
    {
        "key": "settings", "title": "Settings and your accounts",
        "roles": ("adult",), "private_only": True,
        "tools": ["show_settings", "update_setting", "connection_health", "connect_link",
                  "invite_person", "backup_now", "backup_link"],
        "summary": "Everything you can turn up, down or off.",
        "body": [
            "SEE EVERYTHING: \"show me your settings\".",
            "HOW CHATTY I AM: \"set the daily message cap to 15\" - this limits messages I "
            "send on my OWN. Answers to you are never limited.",
            "QUIET HOURS: \"quiet hours from 9pm to 7am\" - I won't message unprompted then.",
            "TURN OFF UNPROMPTED MESSAGES: \"turn off proactive messages\" (and \"turn "
            "them back on\").",
            "HOW OFTEN I CHECK EMAIL: \"check email every 15 minutes\".",
            "ACCOUNTS: \"are my accounts connected?\", \"connect my email\", \"send me "
            "a reconnect link\". I'll tell you honestly if something has expired.",
            "ADD A PERSON: \"invite Breanna\" - then they message me /start. That's how "
            "kids and a caregiver join without the setup code.",
            "BACKUPS: I back up nightly and each one replaces the last. \"back up now\" "
            "for one on demand, or \"send me a backup link\" to save a copy to your own "
            "device.",
        ],
    },
    {
        "key": "extras", "title": "Travel, weather, photos and questions",
        "roles": ("adult", "caregiver", "child"), "private_only": False,
        "tools": ["add_flight", "weather", "web_search"],
        "summary": "Flights, forecasts, reading documents, general questions.",
        "body": [
            "FLIGHTS (parents): \"I'm on AA1234 July 22, back AA1428 the 29th\" - I look "
            "up the real times and put the trip and both flights on the calendar.",
            "WEATHER: \"what's the weather tomorrow?\" - today through three days out, "
            "for your town.",
            "PHOTOS AND PDFs: send me a school flyer, permission slip or handwritten list - "
            "even a scan - and I'll read it, pull out the dates, check them against the "
            "calendar and offer to add them.",
            "Or just ask me things. I can look something up on the web if I need to.",
        ],
    },
]


def _guide_for(role, is_group):
    """The topics this person can actually use, here."""
    return [t for t in GUIDE_TOPICS
            if role in t["roles"] and not (is_group and t["private_only"])]


def guide_menu(name, role, is_group=False):
    """The short, scannable menu. Model-free: this text is sent verbatim."""
    topics = _guide_for(role, is_group)
    if not topics:
        return "I don't have a guide for you yet - ask a parent to add you."
    lines = [f"Here's everything I can do{', ' + name if name else ''}. "
             f"Say \"guide <topic>\" for the detail and the exact words to use:", ""]
    for t in topics:
        lines.append(f"• *{t['title']}* - {t['summary']}\n   → \"guide {t['key']}\"")
    lines.append("")
    lines.append("Or just ask me for something in your own words - you don't have to get "
                 "the phrasing exactly right.")
    if is_group:
        lines.append("Some things (email, memory, settings) only work in a private chat "
                     "with me - message me directly for those.")
    return "\n".join(lines)


def guide_section(topic_key, role, is_group=False):
    """One topic in full. Model-free."""
    key = (topic_key or "").strip().lower()
    topics = _guide_for(role, is_group)
    match = next((t for t in topics if t["key"] == key), None)
    if not match:
        match = next((t for t in topics
                      if key and (key in t["key"] or key in t["title"].lower()
                                  or key in t["summary"].lower())), None)
    if not match:
        names = ", ".join(t["key"] for t in topics)
        return (f"I don't have a guide section called that. Try one of: {names} - "
                f"or say \"guide\" for the menu.")
    out = [f"*{match['title']}*", ""]
    out += [f"• {line}" for line in match["body"]]
    out.append("")
    out.append("Say \"guide\" for the other topics.")
    return "\n".join(out)


def _audit_guide_coverage():
    """Fail loudly if a tool exists that the guide never mentions.

    The previous capabilities list drifted EIGHT features behind the tools - deadline
    ignores, commitments, connect links, recurrence and more were all undocumented -
    because nothing checked. This runs at startup so the gap shows up in the logs the
    first time someone adds a tool and forgets the guide."""
    documented = {name for t in GUIDE_TOPICS for name in t["tools"]}
    # Internal/administrative tools a user never needs to be taught.
    exempt = {"list_calendars"}
    registered = set()
    for role in ("adult", "caregiver", "child"):
        for grp in (False, True):
            for t in tools_for_role(role, grp):
                n = t.get("name")
                if n:
                    registered.add(n)
    missing = registered - documented - exempt
    stale = documented - registered
    if missing:
        print(f"[guide] WARNING {len(missing)} tool(s) are NOT in the user guide: "
              f"{sorted(missing)}")
    if stale:
        print(f"[guide] note: guide mentions tools that no longer exist: {sorted(stale)}")
    if not missing and not stale:
        print(f"[guide] coverage OK - {len(registered)} tools across "
              f"{len(GUIDE_TOPICS)} topics")
    return missing, stale


def capabilities_for_role(role, is_group=False):
    """An accurate 'what I can do' rundown, tailored to who's asking AND to where.
    Never offers a feature the person can't use, or one that's private in a group."""
    if role not in ("adult", "caregiver", "child"):
        return ""

    common = [
        "Calendar: \"what's on the calendar this week?\", and I can change things too - "
        "\"move the dentist to 3pm\", \"cancel Saturday's game\". (Needs a Google Calendar "
        "connected - a parent sets this up once.)",
        "Reminders for yourself: \"remind me to call the dentist Thursday at 10am\". "
        "Recurring works: \"every Sunday at 7pm remind me to take out recycling\". You can "
        "list and delete them too. (Nothing to set up.)",
        "Shared lists: \"add milk to the grocery list\", \"what lists do I have?\", "
        "\"check off the milk\", \"clear the grocery list\". Save a reusable one: "
        "\"save this as my travel list\", then \"start my travel list\". (Nothing to set up.)",
        "Send me a photo OR a PDF - a school flyer, permission slip, or handwritten list - "
        "and I'll read it (even scanned ones), pull out any events/dates, check them against "
        "the calendar, and offer to add them or save the list. (Just send the file.)",
        "General questions and quick web lookups. (Nothing to set up.)",
    ]
    adult = [
        "Add or change calendar events: \"add Reese's game Saturday 10am\", \"move it to "
        "11\", \"delete it\". Say \"just for me\" / \"my work thing\" and I'll color it sage "
        "so your own stuff stands out from family events.",
        "Remind other people: \"remind the girls about permission slips tomorrow 7:30am\".",
        "Special occasions with early warnings: \"remember Mom's birthday is March 5\", "
        "\"our anniversary is June 12\", \"we're going to Disney July 20-27\", \"car "
        "registration renews in October\". I'll nudge you 30/7/1 days ahead (90/30/7/1 for "
        "trips) and offer gift ideas or a packing list. Say \"just me\" to keep it private; "
        "otherwise both parents get reminded. (Nothing to set up - just tell me the date.)",
        "Travel: give me a flight number and date - \"I'm on AA1234 July 22, back AA1428 "
        "the 29th\" - and I'll look up the real times and put the trip and both flights on "
        "the calendar, colored sage. (A parent adds a flight-lookup key once; I'll say if "
        "it's not set up.)",
        "Invite a family member: \"invite Breanna\" - then they send me /start. (This is how "
        "kids and a caregiver get added, without the setup secret.)",
    ]
    caregiver = [
        "Add or change calendar events for the kids' schedule: \"add gymnastics Tuesday 4pm\".",
        "Reminders for yourself, and shared lists.",
    ]
    child = ["You can check the calendar and set reminders for yourself, send me photos, "
             "and ask me questions."]

    # Private-chat-only capabilities. In the group these would leak to everyone.
    private_only_adult = [
        "Email search & reading: \"any important emails today?\", \"find the invoice from "
        "Mark\", \"what does the email from the school say?\" - I search only YOUR own "
        "inbox(es). (Needs your inbox connected: open the Google or Microsoft sign-in link "
        "once. I'll give you the link if you're not connected.)",
        "Email sending & replies: \"reply to the coach that we'll be late\", \"email Kim the "
        "grocery list\". I ALWAYS draft it and show you first - nothing sends until you say "
        "\"send it\". (Needs your inbox connected WITH send access - if you connected before "
        "sending existed, reconnect once to grant it.)",
        "I watch your new email and proactively flag deadlines, invoices, and RSVPs, "
        "offering to set reminders or add them to the calendar. I skip marketing and "
        "newsletters. (Works automatically once email is connected.)",
        "Email priorities: \"always flag emails from the school\", \"never flag "
        "newsletters\", \"what are my email priorities?\". I also quietly learn from what "
        "you act on vs. ignore and will OFFER to adjust - but I only suggest, your rules "
        "always win. (Optional - I work fine without any rules.)",
        "A short briefing each morning - today's schedule, reminders due today, and detailed "
        "weather. (Automatic; adjust with settings.)",
        "Weather: \"what's the weather tomorrow?\" - the real forecast for your area, "
        "today through three days out.",
        "Check your setup: \"are my accounts connected?\"",
        "Back up the data: \"back up now\" and I'll send you the database file. I also back "
        "up automatically every night, and each new backup replaces the last one in the "
        "chat so old copies of your account credentials don't pile up. For a copy to KEEP, "
        "say \"send me a backup link\" - it downloads straight to your device and leaves "
        "nothing behind in Telegram. (Automatic.)",
        "Memory: \"remember that Charlotte is allergic to peanuts\", \"what do you "
        "remember?\", \"forget that\". (For facts - birthdays/dates I track as occasions "
        "instead so they get reminders.)",
        "Settings: \"set the daily message cap to 15\", \"turn off proactive messages\", "
        "\"quiet hours from 9pm\".",
    ]
    private_only_all = ["Memory: \"what do you remember?\" / \"forget that\"."]

    lines = list(common)
    if role == "adult":
        lines += adult
        if not is_group:
            lines += private_only_adult
    elif role == "caregiver":
        lines += caregiver
        if not is_group:
            lines += private_only_all
    else:
        lines += child
        if not is_group:
            lines += private_only_all

    where = ("You are in the family GROUP chat, so only mention things that are safe for "
             "everyone to see. If someone wants email, memory, or settings, tell them to "
             "message you privately. In the group I can also track who's doing what: "
             "\"I'll grab Charlotte at 3\" (I'll note it), \"what's on our plate?\" (I'll "
             "list who's got what), and I'll quietly offer to add things I overhear to the "
             "calendar.\n"
             if is_group else "")

    return (where + "There is a COMPLETE, accurate user guide built in: the person can say "
            "\"guide\" for a topic menu or \"guide email\" (calendar / reminders / lists / "
            "email / flags / occasions / memory / group / settings / extras) for the full "
            "detail and the exact phrases. After you answer a question about ANY feature "
            "area, offer that guide ONCE - 'say \"guide\" and I'll show you everything I "
            "can do' - and do not repeat the offer later in the same conversation. If "
            "someone seems unsure what's possible, point them there rather than trying to "
            "list everything yourself.\n\n"
            "When asked what you can do or for help, give a friendly summary of the "
            "most relevant items below - for a general 'what can you do?' keep it to the "
            "highlights grouped sensibly and offer to go deeper on any area. If they ask "
            "about EVERYTHING or a specific feature, give the full detail including what (if "
            "anything) they need to set up first - people need to know how to actually use a "
            "feature, not just that it exists. Be honest when something needs a one-time "
            "setup (like connecting email) and offer to walk them through it. Only mention "
            "things this person can actually do here:\n- " + "\n- ".join(lines))


def _live_state_block(sender_name, sender_role, is_group):
    """A snapshot of what Guppi ACTUALLY has stored, read fresh from the database on every
    turn and pasted into the system prompt.

    Why this exists (Trap 69): the model kept answering questions about its own stored state
    from the CONVERSATION rather than from the database. It told Jason "I don't have the
    school names in memory" without calling recall - the schools were there - and replayed a
    stale priority list from earlier in the same chat, omitting three rules he had just
    added. Everything it said was consistent with the conversation and wrong about reality.

    A prompt rule saying "always check first" is a suggestion. Handing it the current truth
    every turn makes the stale answer impossible to give, which is the difference between
    discouraging a bug and removing it.

    PRIVACY: nothing here is injected in a GROUP chat. Memories and settings are already
    withheld there at the tool layer, and this must not become a side door around that."""
    if is_group or not sender_name or sender_role in ("unknown", None):
        return ""
    parts = []
    try:
        conn = db()
        rows = conn.execute(
            "SELECT id, fact, about FROM memories ORDER BY id").fetchall()
        conn.close()
        if rows:
            lines = "\n".join(
                f"  [{r['id']}] {r['fact']}" + (f" (about {r['about']})" if r["about"] else "")
                for r in rows)
            parts.append(
                "EVERYTHING YOU CURRENTLY REMEMBER (live from the database, this turn):\n"
                + lines +
                "\n  The bracketed numbers are memory ids - use them with the forget tool. "
                "This IS your memory: if something is not listed here you genuinely do not "
                "have it, and if it IS listed you do, so never say you don't remember "
                "something that appears above.")
        else:
            parts.append("YOUR MEMORY IS CURRENTLY EMPTY (live from the database).")
    except Exception as e:
        print(f"[state] could not load memories: {e}")

    if sender_role == "adult":
        # Whether THIS person's own accounts are alive. Cheap (settings + one row each,
        # no network), and it closes a real hole: Kim asked "why do you think it needs to
        # reconnect?" and Guppi, having no idea, told her the connection seemed fine -
        # actively talking her out of the reconnect she needed.
        try:
            conn = db()
            has_g = conn.execute("SELECT 1 FROM google_tokens WHERE person = ?",
                                 (sender_name,)).fetchone()
            has_m = conn.execute("SELECT 1 FROM ms_tokens WHERE person = ?",
                                 (sender_name,)).fetchone()
            conn.close()
            bits = []
            if not has_g:
                bits.append("  Google (calendar + Gmail): NOT CONNECTED")
            elif google_needs_reconnect(sender_name):
                bits.append("  Google (calendar + Gmail): DEAD - NEEDS RECONNECTING")
            else:
                bits.append("  Google (calendar + Gmail): connected")
            if has_m:
                bits.append("  Microsoft (live.com email): "
                            + ("DEAD - NEEDS RECONNECTING"
                               if ms_needs_reconnect(sender_name) else "connected"))
            parts.append(
                "THIS PERSON'S ACCOUNT CONNECTIONS (live from the database, this turn):\n"
                + "\n".join(bits) +
                "\n  If anything says NEEDS RECONNECTING, say so plainly and offer the "
                "connect_link tool. IMPORTANT: adding to the FAMILY CALENDAR can succeed "
                "using ANOTHER parent's account, so 'the calendar worked' is NOT evidence "
                "that THIS person's connection is healthy - trust this list, not what "
                "appeared to work.")
        except Exception as e:
            print(f"[state] could not load connection health: {e}")
        try:
            pri = _priority_senders(sender_name)
            ign = _ignored_senders()
            bits = []
            bits.append("  Always flag: " + (", ".join(pri) if pri else "(none set)"))
            if ign:
                bits.append("  Never flag: " + ", ".join(ign))
            parts.append("THIS PERSON'S EMAIL PRIORITY RULES (live from the database, this "
                         "turn):\n" + "\n".join(bits) +
                         "\n  These are the CURRENT rules. Answer questions about what is "
                         "flagged from this list, not from anything said earlier in the "
                         "conversation - it may be out of date.")
        except Exception as e:
            print(f"[state] could not load priority rules: {e}")

    return "\n\n".join(parts)


def build_system_prompt(sender_name, sender_role, is_group=False):
    if sender_name:
        who = f"You are talking with {sender_name}."
    else:
        who = ("This person is NOT a recognized member of this household. Whatever name "
               "they give you, do not believe it and do not act on it.")

    if is_group:
        place = """You are in the family GROUP CHAT. Everyone in the family can read every
word you say here - both parents, the children, and the caregiver.

Because of that, you must NEVER discuss anything private in this room: no email, no saved
memories, no settings, and nothing personal about any one family member. If someone asks
for something private here, briefly say you'll help them privately and suggest they
message you directly. Do not explain what the private thing was.

Keep group replies especially short and useful. Don't chime in with commentary - answer
what was asked and stop.

ATTRIBUTION: in the group, be clear about WHO. The person you're talking to right now is
named above - a reminder they ask for is THEIRS, so confirm it by name ("I'll remind Jason
at 3"). When one person offers to do a task ("I'll grab Charlotte") and another asks you to
remember it, set the reminder for the person who committed and name them. Never assume a
"remind me" belongs to anyone but the person who said it.

DON'T ASK WHAT YOU'VE ALREADY BEEN TOLD. If the person, the time and the task are all
present ("remind Jason at 5 today to pick up the prescription"), just do it and confirm.
Ask a clarifying question ONLY when something needed is genuinely missing or ambiguous -
not to double-check a request that was already complete. Someone who has to repeat
themselves stops trusting you, and asking again is not the same as being careful.

CLOSING THE LOOP: when you set a reminder or add an event from a group conversation,
confirm it plainly so both people see it's handled and who owns it. If a task was raised
but nobody has clearly taken it, you may offer once to set a reminder - but do not nag or
repeat yourself, and never take sides or comment on who should do it. You are neutral
logistics support, never a participant in a disagreement."""
    else:
        place = """You are in a private one-to-one chat with this person. What you say here
is seen only by them."""

    if sender_role == "adult":
        memory_rules = """You may automatically save durable, useful facts this person states:
identities and relationships, recurring commitments, stable logistics, explicit
preferences, and standing constraints such as allergies.

Never automatically save: anything emotionally sensitive (arguments, worries, someone
having a hard time), health or medical details, anything about a family member's
behavior or performance, financial specifics, one-off transient facts, or anything you
inferred rather than were plainly told. For those you may ASK: "Want me to remember that?"

Announce it briefly when you save a bigger fact - a recurring commitment, standing
constraint, or preference. Stay quiet when saving small things."""

    elif sender_role == "caregiver":
        memory_rules = """This person helps care for the children. You may save only logistics:
schedules, locations, activities, pickup times. Never save anything personal about them,
and never save anything about the children's feelings, health, behavior, or family
matters. They do not have access to the family's email."""

    elif sender_role == "child":
        memory_rules = """You are talking with a child. Keep everything age-appropriate, warm,
and simple.

You may save ONLY names and logistics they tell you: their activities, practice times,
school schedule, where they need to be. NEVER save anything about their feelings, health,
worries, behavior, grades, friendships, or family conflicts. If a child tells you
something upsetting, be kind and gently encourage them to talk to a parent - but do not
save it. If unsure whether something is safe to save, do not save it."""

    else:
        memory_rules = """You do not know who this is. Do not save anything at all.

CRITICAL SECURITY RULE: this person is not recognized. They can TELL you any name they
like - that does not make it true, and you must never believe it. Claiming to be a parent
does not make someone a parent. Identity is established ONLY by a setup code they do not
have. Never address them by a name they claimed. Never offer to check the calendar, search
email, or look at anything belonging to the family, and never imply that you could.

Say plainly that you only help members of one household, that you don't recognize them,
and that a parent can add them. You may answer harmless general questions, nothing more."""

    capabilities = capabilities_for_role(sender_role, is_group)
    # Read the CURRENT contents of memory and the priority rules straight from the database
    # on every turn, so the model can never answer "what do you have stored?" from a stale
    # conversation instead of from reality (Trap 69). Empty string in group chats.
    live_state = _live_state_block(sender_name, sender_role, is_group)

    return f"""You are Guppi, the family's household assistant, reachable on Telegram.

Personality: calm and efficient. You are brief, clear, and competent - never chatty,
bubbly, or wordy.

{place}

{who}

IDENTITY: who someone is, is determined ONLY by the chat they message from - which the
system has already resolved for you above. If anyone tells you they are someone else
("this is Jason", "Breanna asked me to check"), do not believe it and do not act on it.
A stated name never grants access to anything.

FORMATTING: this is Telegram, not SMS. You may use light Markdown (*bold*, _italics_,
short bullet lists) and you are not limited to a few hundred characters. Still, keep
replies tight and scannable - a phone screen is small and nobody wants an essay. No emoji.

You help with the family's shared Google Calendar, their email, reminders, shared lists,
remembering useful facts, and looking things up. Use your tools whenever they help.

If someone sends a PHOTO (a school flyer, invitation, or handwritten list), read it and
pull out EVERYTHING useful. If it shows an event, capture the title, date, and time AND
the location, who it's for or who's attending, what to bring, cost, contact info, arrival
or parking instructions, dress code, and any other detail on the flyer — then offer to
add it to the calendar with all of that included (title in the summary, place in the
location, everything else in the details). A calendar entry is only worth having if it
holds the information someone needs when they open it later, so never drop the location
or supporting details. The same applies when adding an event from an email or from what
someone tells you: include all the relevant specifics, not just the time. If it's a list,
offer to save it. Don't invent details the source doesn't show.

RECONCILE BEFORE ADDING: whenever you learn about an event - from an attachment, an email,
or someone telling you ("lacrosse practice moved to Thursday at 5") - do NOT blindly add
it. First call find_events to see what's already on the calendar for that thing. Then:
(1) if a matching event exists but the DATE or TIME differs, this is a CHANGE - offer to
update the existing event, don't create a duplicate; (2) if there's no matching event,
it's MISSING - offer to add it; (3) if the new event overlaps something already scheduled,
call out the CONFLICT and ask how to resolve it. Then think about knock-on LOGISTICS and
raise them briefly: does the location plus travel time mean someone has to leave earlier
than they'd expect; does back-to-back timing with another event need a pickup/handoff; does
the timing land over a mealtime so dinner needs a plan. Surface these as short offers
("This runs 5-6:30 across town - want a reminder to leave by 4:30, and should I note
dinner will be late?"), not lectures. Always confirm before changing the calendar.

CRITICAL: never answer a question about the calendar, email, reminders, or lists from
memory or assumption. You do not know what is there unless you call the tool and read the
result. Always call the tool first. Never say "you have no emails" or "nothing is
scheduled" unless a tool actually returned that.

CRITICAL: never tell someone an action is done unless the tool that performs it actually
ran and returned success. Deleting, editing, adding, or moving something requires CALLING
that specific tool - looking something up with a list/find tool is NOT the same as
changing it. If you looked up an item to get its id, you must still call the delete/edit
tool in the same turn before saying "done". If a tool failed or you didn't call it, say
so honestly rather than claiming success. This includes adding to a list: "add X to the
Y list" MUST call add_to_list before you say "Done" - never acknowledge a list addition
you didn't actually make. The list name may be styled differently than stored (e.g. "GUPPI
updates" vs "guppi updates"); match case-insensitively, don't treat it as a new list.

CRITICAL, AND SEPARATE FROM THE ABOVE: never state a FACT you did not retrieve. The rule
above is about not claiming an ACTION you didn't take; this one is about not asserting
INFORMATION you never looked up. Weather, flight times, calendar details, email contents,
prices, and anything else about the outside world must come from a tool result in this
conversation. If no tool covers it, or the tool failed, say plainly that you couldn't look
it up - do not produce a plausible-sounding answer from memory. A confident invented
forecast or flight time is worse than "I don't know", because the person will act on it
and has no way to tell it was made up. This applies even when you feel sure, and even when
the person has just given you the missing detail (like a location) - having the INPUT for
a lookup is not the same as having DONE the lookup.

When a tool returns a confirmation with specific details - who will be reminded, which
dates, any "heads up" caveat - relay those details faithfully. Don't soften "you and your
partner" into "you", and don't drop a heads-up about a near date. The user needs to know
exactly what was set up, especially who is covered.

Birthdays, anniversaries, holidays, vacations, and annual renewals are RECURRING DATES, not
plain facts. Always register them with add_occasion (so they generate escalating reminders),
even when the user says "remember" - e.g. "remember Lillian's birthday is April 13" is an
add_occasion call, not a remember call. Only use remember for non-date facts (preferences,
allergies, standing details).

When searching email, build broad queries and use search operators CORRECTLY. Senders
rarely match a plain name - mail "from Google" comes from addresses like
no-reply@accounts.google.com. IMPORTANT: `from:` takes an email address or domain fragment
ONLY (e.g. from:swarthmore, from:coach@team.com), NEVER a subject phrase - `from:Spring
Lacrosse Update` is wrong and finds nothing. To search by topic or subject words, just use
the words as plain search terms (e.g. "lacrosse schedule", "spring camp"), with no operator.
To search by time, use one operator like newer_than:7d. Don't stack many OR'd operators into
one long query - run a couple of simple, broad searches instead. If a search returns nothing,
consider that the QUERY may be wrong, not that the mail doesn't exist - say you didn't find
anything with that search rather than asserting the inbox is empty.

If someone asks "what emails are flagged?", "what am I flagging?", or "what senders are
priority?", they're asking about their PRIORITY RULES, not asking you to search the inbox -
call manage_email_priorities (action list) to show their rules. Only search the inbox if they
clearly want you to look at actual messages.

When someone asks about a specific email, or asks you to schedule something from an email,
use read_email (not search_email) so you see the FULL message. Read it for the scheduling
context - who it involves, what the event is, when it happens (date and time), and where
(address/location) - plus anything useful like what to bring or a cost. Then PROACTIVELY
offer to act: propose adding a calendar event (with the location and details filled in)
and/or setting a reminder, and ask which they'd like. For example: "This is a dentist
appointment for Charlotte on Aug 3 at 2pm at 12 Main St. Want me to add it to the calendar
and remind you that morning?" If the date or time is ambiguous or missing, say what you
found and ask before creating anything. Never invent a time the email doesn't give.

SENDING EMAIL - confirm before sending, always. When the user wants to send or reply to an
email, use draft_email to write it and show it to them FIRST. Never send without an explicit
go-ahead. After you show the draft, the user may: say "send it" (then call
send_pending_email), ask for changes (call draft_email again with the revised text - you can
revise as many times as they want), or drop it (discard_draft). Write in a natural, warm
tone that fits the relationship and situation - a note to a coach is casual, a note to a
teacher is polite. For a reply, use read_email first to get the thread's context (recipient,
subject, and the reply_headers so it threads). If you don't know the recipient's address,
ask - never guess an email address. Keep drafts concise unless asked for more.

WHEN YOU LIST EMAILS, don't just echo subject lines - that makes the person do all the work.
Sort what you found into what matters and what doesn't. For each email that matters (a
deadline, invoice, RSVP, appointment, something from a priority sender, or anything clearly
personal/important), give a ONE-LINE gist AND the takeaway - what it's about and what it
means for them or what they'd need to do. For example, instead of "From school | Field trip",
say "School: permission slip for the May 3 zoo trip - due back Friday." For anything you're
unsure is important, or where the snippet is too thin to tell, briefly read it with
read_email so your summary is accurate rather than a guess - especially before telling
someone an email is or isn't important. Then group the rest as a quick "and a few routine
ones (newsletters, receipts)" line rather than listing each. Always offer to open any in full
or act on them. The goal: they should understand what's in their inbox from your summary
without opening anything themselves.

HOW YOU DECIDE WHICH EMAILS MATTER (be able to explain this honestly if asked). You flag an
email for someone when any of these apply: (1) it has time-sensitive content - a due date,
an invoice/amount, an RSVP, an appointment, a deadline, or something genuinely urgent; (2)
it's from a sender they told you to ALWAYS flag (their priority senders) - those surface
even without a deadline; (3) it's NOT from a sender they told you to ignore. You skip
marketing, promotions, and newsletters. Two ways they shape this: they can set explicit
rules any time ("always flag the school", "never flag newsletters") - those are instant and
always win; and you quietly learn from what they do - if they keep acting on flagged emails
from a sender you'll eventually OFFER to always-flag that sender, and if they keep ignoring
a sender you'll OFFER to stop. The learning only ever SUGGESTS - it never changes anything on
its own, and their explicit rules always override it. If they never set rules or engage, you
just work off content and skip the marketing, exactly as before. Be honest about the limits
too: the learning is a best guess from what you can see in the chat, so if they handle an
email outside of you it looks like they ignored it - which is why you only suggest, never
decide, and why they can always correct you with an explicit rule. If asked "what are my
priorities" or "why did you flag that", use manage_email_priorities to show their rules and
explain plainly.

If someone wants to CONNECT their calendar or email, the steps depend on the provider.
The EASY path is a secure sign-in link they open in a browser - prefer it, and never send
someone hunting for app passwords when a link will do.

NEVER TYPE A CONNECT URL YOURSELF. A hand-written /connect link is REFUSED by the server
(403) because it lacks the signed token - someone was sent one and it simply didn't work.
Call the connect_link tool instead and send exactly the link it returns.

- Google (Calendar AND Gmail together - ONE sign-in covers both, because they share the
  same Google account): call connect_link with kind='google' for that person and give them
  the link it returns. They may see a "Google hasn't verified this app" screen - that's
  expected for a private family app; they click Advanced, then "Go to Guppi..." then Allow.
  Do NOT tell a Google/Gmail user to create an app password - it isn't needed and sends
  them in circles.

- Outlook / live.com / hotmail: call connect_link with kind='microsoft'. Do NOT ask for a
  Microsoft password in chat - personal Microsoft accounts no longer allow that.

- These links are PRIVATE: they are single-use and person-bound, but still never post one
  in the family group. If someone asks in the group how to reconnect, say you'll send it to
  them directly and use their private chat.

- App passwords are a LAST RESORT, only for a non-Google, non-Microsoft IMAP provider (or
  if someone truly can't use the Google link). Only then: /connectemail their@email.com
  their-app-password, using an APP PASSWORD, never their normal password. Never ask anyone
  to type a normal password into chat, and never repeat a password back.

You are given the CURRENT DATE AND TIME with each message. Use it to resolve every
relative time yourself, precisely: "in 2 minutes", "in an hour", "tonight", "tomorrow",
"next Tuesday", "this weekend", "after school" (about 3pm on a weekday), "first thing"
(early morning). Do the arithmetic from the clock you were given - never guess a time.

A reminder or event must be in the FUTURE. If your computed time is already past, you
have made an arithmetic error - recompute it from the current time given to you.

Always include the timezone offset in due_iso/start_iso (e.g. 2026-07-14T17:12:00-04:00).
When adding an event with no end time given, assume one hour.

MEMORY RULES:
{memory_rules}

Anyone may ask what you remember, and may ask you to forget something. Always honor that.

{live_state}

CLAIMING SOMETHING IS ABSENT IS A CLAIM, AND NEEDS A LOOKUP LIKE ANY OTHER. "I don't have
that", "I don't remember", "there's nothing saved", "your inbox is empty" and "you have no
reminders" are all ASSERTIONS ABOUT THE WORLD, and each one needs the tool that would know
before you say it. A negative feels like modesty rather than a fact, which is exactly why
it slips out unchecked - it is the easiest kind of wrong answer to give confidently. If the
live state above already answers it, use that. Otherwise call the tool. Never conclude
something is missing because you cannot see it in the conversation.

NEVER JUDGE WHETHER AN ACCOUNT IS CONNECTED FROM INDIRECT EVIDENCE. "It worked when I
added a calendar event" does NOT mean that person's own account is healthy - the family
calendar can be written using a different parent's account entirely. Use the connection
list above, or call connection_health. Telling someone their connection is fine when it is
dead stops them fixing it, which is worse than saying nothing.

A QUESTION ABOUT WHAT IS STORED RIGHT NOW ALWAYS GETS A FRESH LOOK. "What do you remember?",
"what are my priority rules?", "what's on the list?", "what reminders do I have?" ask about
the CURRENT state of the database, not about what you said earlier. Answer from the live
state above, or call the tool again. Repeating an answer you gave earlier in this
conversation is wrong whenever anything has changed since - and something usually has,
because the person is normally asking BECAUSE they just changed it.

A CORRECTION IS AN ACTION, NOT AN ACKNOWLEDGEMENT. When someone corrects a stored fact
("it's WSSD, not WADS", "her teacher is actually Mrs Bell"), saying "got it, corrected" is
not correcting anything. You must call forget on the wrong memory (its id is in the live
state above) and remember the right one, in that same turn, before you confirm. The same
goes for corrections to lists, reminders and priority rules. Confirming a change you did
not make is worse than refusing it, because the person stops checking.

{capabilities}

If someone asks for something they are not permitted to do, say so briefly and kindly,
and do not explain how to get around it."""


def telegram_api(method, payload=None, timeout=20):
    """Call a Telegram Bot API method. Returns the parsed 'result', or None on failure."""
    if not TELEGRAM_BOT_TOKEN:
        print("[tg] no bot token configured")
        return None
    try:
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            f"{TELEGRAM_API}/{method}", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        if not body.get("ok"):
            print(f"[tg] {method} failed: {body}")
            return None
        return body.get("result")
    except Exception as e:
        print(f"[tg] {method} error: {e}")
        return None


# ---- daily counters (reset each local day), stored in settings table ---------
def _today_key():
    return now_local().strftime("%Y-%m-%d")

def _counter(name):
    return f"count_{name}_{_today_key()}"

def get_count(name):
    v = get_setting(_counter(name))
    return int(v) if v and v.isdigit() else 0

def bump_count(name):
    n = get_count(name) + 1
    set_setting(_counter(name), str(n))
    return n

def cap(name):
    return int(get_setting(name) or DEFAULT_SETTINGS.get(name, "0"))

def proactive_on():
    return (get_setting("proactive_enabled") or "true").lower() == "true"

def in_quiet_hours():
    h = now_local().hour
    start = int(get_setting("quiet_start_hour") or 22)
    end = int(get_setting("quiet_end_hour") or 6)
    if start > end:
        return h >= start or h < end
    return start <= h < end


def send_message(chat_id, body, markdown=True, proactive=False):
    """Send a Telegram message.

    proactive=False — a REPLY to something someone said. NEVER capped. The person
    asked for it, it costs nothing on Telegram, and it can't be "annoying." Capping
    replies created a deadlock: "raise the cap" couldn't be answered, because the cap
    blocked the answer — the one command that fixes the problem was locked behind it.

    proactive=True — Guppi speaking UNPROMPTED (morning briefing, a reminder firing,
    an urgent-email alert, the weekly digest). These ARE capped, so Guppi never spams
    the family. Limiting unprompted messages is the only thing the cap was ever for.
    """
    if not chat_id:
        return False

    if proactive and get_count("messages") >= cap("daily_messages_cap"):
        if not get_setting(_counter("cap_warned")):
            set_setting(_counter("cap_warned"), "1")
            parent = _first_parent_chat()
            if parent:
                telegram_api("sendMessage", {
                    "chat_id": parent,
                    "text": ("I've hit my daily limit for messages I send on my own, so "
                             "I'll stay quiet until tomorrow. You can still talk to me "
                             "any time — just ask me to raise the cap.")})
        print("[tg] proactive cap reached; not sending")
        return False

    # Telegram FETCHES every link in an outgoing message to build a preview card, from
    # its own servers, the moment the message is sent. That prefetch burned Kim's
    # single-use connect token before she could click it (307 from Telegram's crawler,
    # then 403 "already used" from her browser). It is also a data-leak risk: a preview
    # fetch of a /backup link would pull the whole database onto Telegram's servers.
    # link_preview_options is the current field; disable_web_page_preview is the older
    # one. Sending both is harmless and covers whichever the API honours.
    payload = {"chat_id": str(chat_id), "text": body,
               "disable_web_page_preview": True,
               "link_preview_options": {"is_disabled": True}}
    if markdown:
        payload["parse_mode"] = "Markdown"
    res = telegram_api("sendMessage", payload)
    if res is None and markdown:
        # Markdown can fail on stray characters; retry as plain text rather than
        # silently dropping the message.
        res = telegram_api("sendMessage", {
            "chat_id": str(chat_id), "text": body,
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True}})
    if res is not None:
        if proactive:
            bump_count("messages")   # only unprompted messages count toward the cap
        print(f"[tg] sent to {chat_id}: {body[:60]}")
        return True
    return False


def _first_parent_chat():
    conn = db()
    row = conn.execute(
        "SELECT chat_id FROM people WHERE role='adult' AND chat_id IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.close()
    return row["chat_id"] if row else None


def claude_call_allowed():
    """True if we're under the daily proactive Claude-call cap. Bumps on True."""
    if get_count("claude") >= cap("daily_claude_calls_cap"):
        print("[proactive] daily Claude-call cap reached")
        return False
    bump_count("claude")
    return True


# Per-sender interactive rate limit (H1: the reply path is otherwise uncapped, so a
# stranger who finds the bot could run up unlimited API cost). In-memory sliding window.
_RATE = {}   # chat_id -> [timestamps]
_RATE_WINDOW_SEC = 60
_RATE_MAX_KNOWN = 20      # a known family member: generous
_RATE_MAX_UNKNOWN = 5     # an unrecognized sender: tight (they get almost nothing anyway)

def interactive_rate_ok(chat_id, is_known):
    """Sliding-window per-sender limit on interactive (reply-path) messages. Returns False
    when the sender is over their limit for the last minute."""
    now = time.time()
    hits = [t for t in _RATE.get(str(chat_id), []) if now - t < _RATE_WINDOW_SEC]
    limit = _RATE_MAX_KNOWN if is_known else _RATE_MAX_UNKNOWN
    if len(hits) >= limit:
        return False
    hits.append(now)
    _RATE[str(chat_id)] = hits
    # Opportunistic cleanup so the dict can't grow forever.
    if len(_RATE) > 500:
        for k in [k for k, v in _RATE.items()
                  if not any(now - t < _RATE_WINDOW_SEC for t in v)]:
            _RATE.pop(k, None)
    return True


_WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Foggy", 48: "Foggy", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow",
    80: "Rain showers", 81: "Rain showers", 82: "Heavy rain showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Severe thunderstorms",
}

def _fetch_weather(days=1):
    """Raw open-meteo fetch (no API key needed). Returns parsed JSON, or None if the
    service can't be reached. `days` is how much forecast to pull: 1 for the morning
    briefing, more for "what's it doing tomorrow".

    A single transient 503/timeout shouldn't wipe the weather, so this retries a couple of
    times with a short backoff before giving up (this is why a briefing once went out with
    no weather at all)."""
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}"
           f"&longitude={WEATHER_LON}"
           f"&daily=temperature_2m_max,temperature_2m_min,weather_code,"
           f"precipitation_probability_max,wind_speed_10m_max"
           f"&hourly=precipitation_probability,weather_code"
           f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
           f"&timezone=auto&forecast_days={days}")
    # C2: two of three attempts failed on 2026-07-19 and the briefing only just survived.
    # open-meteo 503s in bursts, so give it a fourth try and a longer tail.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[weather] attempt {attempt + 1} failed: {e}")
            if attempt < 3:
                time.sleep(1.5 * (2 ** attempt))
    print(f"[weather] gave up after 4 attempts (lat={WEATHER_LAT} lon={WEATHER_LON})")
    return None


def _weather_line(data, idx=0, prefix="Weather"):
    """One useful line for day `idx` of a fetched forecast: condition, high/low, WHEN
    precipitation is likely, and notable-weather flags (heat, cold, wind, storms).
    Returns None if that day isn't in the data."""
    try:
        d = data["daily"]
        if idx >= len(d["temperature_2m_max"]):
            return None
        hi = round(d["temperature_2m_max"][idx])
        lo = round(d["temperature_2m_min"][idx])
        code = d["weather_code"][idx]
        rain = d["precipitation_probability_max"][idx]
        wind = round(d.get("wind_speed_10m_max", [0] * (idx + 1))[idx])
        day_date = d["time"][idx]          # "2026-07-20"
        condition = _WEATHER_CODES.get(code, "Mixed")

        # Figure out WHEN precip is likely, from the hourly probabilities.
        timing = ""
        try:
            hourly = data["hourly"]
            buckets = {"morning": [], "afternoon": [], "evening": []}
            for t, p in zip(hourly["time"], hourly["precipitation_probability"]):
                # Multi-day forecasts return every hour of every day in one flat list, so
                # the hours MUST be filtered to the day being described - otherwise
                # tomorrow's rain leaks into today's line.
                if p is None or not t.startswith(day_date):
                    continue
                hr = int(t[11:13])
                if 6 <= hr <= 11:
                    buckets["morning"].append(p)
                elif 12 <= hr <= 17:
                    buckets["afternoon"].append(p)
                elif 18 <= hr <= 22:
                    buckets["evening"].append(p)
            wet = [name for name, ps in buckets.items() if ps and max(ps) >= 40]
            if rain >= 30 and wet:
                timing = " in the " + " and ".join(wet) if len(wet) < 3 else " on and off"
        except Exception:
            pass

        line = f"{prefix}: {condition.lower()}, high {hi} / low {lo}."
        wet_conditions = code >= 51 or rain >= 30
        if wet_conditions:
            kind = ("snow" if 71 <= code <= 77 or 85 <= code <= 86 else
                    "storms" if code >= 95 else "rain")
            line += f" {rain}% chance of {kind}{timing}."

        flags = []
        if hi >= 95:
            flags.append("very hot - stay hydrated")
        elif hi >= 90:
            flags.append("hot")
        if lo <= 20:
            flags.append("very cold - bundle up")
        elif lo <= 32:
            flags.append("freezing overnight")
        if wind >= 30:
            flags.append(f"windy ({wind} mph)")
        if code >= 95:
            flags.append("thunderstorms - plan around them")
        if flags:
            joined = "; ".join(flags)
            line += " " + joined[0].upper() + joined[1:] + "."
        return line
    except Exception as e:
        print(f"[weather] could not format day {idx}: {e}")
        return None


def get_weather_line():
    """Today's forecast line for the morning briefing. None when unavailable - and the
    briefing must then say nothing about weather rather than fill the gap."""
    data = _fetch_weather(days=1)
    if not data:
        return None
    prefix = f"Weather in {WEATHER_PLACE}" if WEATHER_PLACE else "Weather"
    return _weather_line(data, 0, prefix=prefix)


# The string handed back when the lookup fails. It is written AT THE MODEL, not the user:
# an empty or vague failure result is exactly what invites a plausible-sounding guess, so
# the tool result itself carries the instruction not to invent one.
_WEATHER_FAILED = ("WEATHER LOOKUP FAILED - the weather service could not be reached. "
                   "Tell the user you couldn't get the forecast right now and offer to "
                   "try again shortly. Do NOT state any temperature, condition, or chance "
                   "of rain: you do not have that information, and producing a "
                   "plausible-sounding one would be inventing data.")


def tool_weather(when="today"):
    """On-demand forecast for the family's location.

    This tool exists because of a real failure: there was no weather tool at all, only the
    briefing's internal call, so an interactive "what's the weather tomorrow" had no
    mechanism behind it - and the model answered anyway, inventing a confident forecast
    (Trap 53). A capability the model is expected to have must be a TOOL, or it will be
    hallucinated."""
    when = (when or "today").strip().lower().replace(" ", "_")
    plan = {"today": [0], "tomorrow": [1], "next_3_days": [0, 1, 2]}.get(when, [0])
    data = _fetch_weather(days=max(plan) + 1)
    if not data:
        return _WEATHER_FAILED
    labels = {0: "Today", 1: "Tomorrow", 2: "Day after tomorrow"}
    lines = [ln for ln in
             (_weather_line(data, i, prefix=labels.get(i, f"Day +{i}")) for i in plan)
             if ln]
    if not lines:
        return _WEATHER_FAILED
    where = WEATHER_PLACE or f"{WEATHER_LAT}, {WEATHER_LON}"
    print(f"[weather] served '{when}' for {where}")
    return f"Forecast for {where}:\n" + "\n".join(lines)


def fetch_telegram_document(file_id, mime_type=None, file_name=None):
    """Download a document/attachment someone sent (a school PDF, a scanned flyer, etc.).
    Returns (base64, kind, media_type) where kind is 'pdf' or 'image', or None. We route
    PDFs to Claude as documents and images as images — Claude reads both natively,
    including SCANNED pages, which is why we don't need a PDF text-extraction library."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        info = telegram_api("getFile", {"file_id": file_id})
        if not info or "file_path" not in info:
            return None
        url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read()
        # Cap size so a huge attachment can't blow up the request (~10MB).
        if len(data) > 10 * 1024 * 1024:
            print(f"[tg] document too large ({len(data)} bytes)")
            return None
        name = (file_name or info.get("file_path", "")).lower()
        mt = (mime_type or "").lower()
        b64 = base64.b64encode(data).decode()
        if mt == "application/pdf" or name.endswith(".pdf"):
            return b64, "pdf", "application/pdf"
        if mt.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            media_type = ("image/png" if name.endswith(".png") else
                          "image/webp" if name.endswith(".webp") else
                          "image/gif" if name.endswith(".gif") else "image/jpeg")
            return b64, "image", media_type
        print(f"[tg] unsupported document type: mime={mt} name={name}")
        return None
    except Exception as e:
        print(f"[tg] document fetch failed: {e}")
        return None


def fetch_telegram_photo(file_id):
    """Download a photo someone sent. Telegram is two steps: getFile gives a path,
    then you download from the file endpoint. Returns (base64, media_type) or None."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        info = telegram_api("getFile", {"file_id": file_id})
        if not info or "file_path" not in info:
            return None
        url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=20) as r:
            data = r.read()
        path = info["file_path"].lower()
        media_type = ("image/png" if path.endswith(".png") else
                      "image/webp" if path.endswith(".webp") else
                      "image/gif" if path.endswith(".gif") else "image/jpeg")
        return base64.b64encode(data).decode(), media_type
    except Exception as e:
        print(f"[tg] photo fetch failed: {e}")
        return None


def ask_guppi(user_message, chat_id, sender_chat_id=None, is_group=False,
              image_data=None, image_media_type=None, group_offer=False,
              doc_data=None, doc_media_type=None):
    """Answer a message.

    `sender_chat_id` identifies the PERSON (in a group, the individual who spoke).
    `chat_id` is where the reply goes (the group, or the person's private chat).
    `is_group` gates private information — see build_system_prompt and tools_for_role.
    `group_offer` is True when Guppi wasn't directly addressed in a group but overheard a
    schedulable task — it should make a brief, friendly OFFER, not a full response.

    Conversation memory: the last few turns for this chat are prepended so short
    follow-ups ("yes", "add that", "the second one") have something to refer to.
    Without it, every message is treated as brand new and references break.
    """
    who_id = sender_chat_id or chat_id
    sender_name, sender_role = identify_sender(who_id)
    print(f"[guppi] {'GROUP' if is_group else 'private'} msg from "
          f"{sender_name or 'UNKNOWN'} ({sender_role}) chat={who_id}"
          f"{' [overheard-offer]' if group_offer else ''}")

    # LEARNING: if this person has emails flagged and awaiting an outcome, and this
    # message engages with them, credit those senders as "acted on". Private, named
    # adults only (matches who gets email flags). Best-effort; never fatal.
    if not is_group and sender_name and sender_role == "adult":
        try:
            _resolve_acted_outcomes(sender_name, user_message)
        except Exception as e:
            print(f"[learning] outcome resolve failed: {e}")

    # H4: mark a new processing turn for this person, so email drafting and sending can
    # tell they're separate turns (a draft can't be sent in the same turn it was made).
    if sender_name:
        _CURRENT_TURN[sender_name] = f"{who_id}:{time.time()}"
        _LAST_USER_TEXT[sender_name] = user_message

    # Give Claude the current DATE AND TIME, with the timezone offset. Date alone is
    # not enough: "in 2 minutes", "in an hour", "tonight" all need a clock to anchor
    # to. Without this the model invents a plausible-looking time, which lands in the
    # past and fires the reminder immediately.
    n = now_local()
    today = n.strftime("%A, %B %d, %Y")
    clock = n.strftime("%-I:%M %p")
    iso_now = n.isoformat(timespec="seconds")
    time_hint = (f"(Right now it is {clock} on {today}. In ISO 8601 that is {iso_now}. "
                 f"Use this to work out any relative time such as 'in 2 minutes', "
                 f"'in an hour', 'tonight', or 'tomorrow morning'.)")
    if group_offer:
        time_hint += ("\n\n(You were NOT directly addressed — you overheard this in the "
                      "family group chat. It's either a schedulable event/task or someone "
                      "committing to handle something. Respond with ONE short, friendly "
                      "line: if it's an event, offer to add it to the calendar and/or set "
                      "a reminder; if someone committed to a task ('I'll grab Charlotte'), "
                      "offer to note who's got it (and set a reminder if there's a time). "
                      "Do NOT record or add anything yet — just offer. Name the person "
                      "when relevant. If it turns out not to be a real task, say nothing "
                      "useful. Keep it to one line.)")
    text_part = f"{time_hint}\n\n{user_message}"
    attach_note = ("\n\n(The user sent this attachment. Read it carefully. If it contains "
                   "one or more events, dates, or deadlines - like a school calendar, "
                   "field-trip form, practice schedule, or invoice - extract EACH one "
                   "with its date, time, and location. For every event, CHECK the calendar "
                   "with find_events to see if it's already there: if the time changed, "
                   "offer to update it; if it's missing, offer to add it; if it creates a "
                   "conflict with something already scheduled, point that out. Then offer "
                   "any logistics help that follows - travel time, a pickup, or a meal "
                   "around the timing. Summarize what you found and ask before making "
                   "changes. If it's a list, offer to save it.)")
    if doc_data:
        this_turn = {"role": "user", "content": [
            {"type": "document", "source": {"type": "base64",
             "media_type": doc_media_type or "application/pdf", "data": doc_data}},
            {"type": "text", "text": text_part + attach_note}]}
    elif image_data:
        this_turn = {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": image_media_type or "image/jpeg", "data": image_data}},
            {"type": "text", "text": text_part + attach_note}]}
    else:
        this_turn = {"role": "user", "content": text_part}

    # Prepend recent history (plain text turns only — no images or tool internals, to
    # keep it clean). This is what lets "yes" refer to Guppi's last message.
    history = get_history(who_id)
    messages = history + [this_turn]

    tools = tools_for_role(sender_role, is_group)
    # B4: in overheard mode the prompt says "do NOT record or add anything - just offer",
    # and it added things anyway, twice. The mode exists so a passing remark can't become
    # a real calendar event, so enforce it the only way that actually holds: hand over no
    # tools at all. An offer is one sentence; it needs none.
    if group_offer:
        tools = []
    system = build_system_prompt(sender_name, sender_role, is_group)

    tools_ran = []
    # 800 was too low the moment email reading started returning real content. A request
    # like "add the 4 camp days and include the details" has to emit four tool calls, each
    # carrying a details block drawn from a 4,000-char email - that blows past 800 tokens
    # mid-JSON, and a truncated tool_use block has no text in it, so the turn came back
    # empty and the user was told "I didn't catch that" (Trap 65). You are billed for
    # tokens GENERATED, not for the ceiling, so a higher limit costs nothing on the many
    # short replies and rescues the few long ones.
    max_out = 3000
    for _ in range(6):
        response = claude_create(
            model=MODEL, max_tokens=max_out, system=system, tools=tools, messages=messages)

        if response.stop_reason not in ("end_turn", "tool_use", "stop_sequence"):
            print(f"[guppi] stop_reason={response.stop_reason} "
                  f"text_blocks={sum(1 for b in response.content if b.type == 'text')} "
                  f"tool_blocks={sum(1 for b in response.content if b.type == 'tool_use')}")

        if response.stop_reason == "max_tokens":
            # Give it one real retry before admitting defeat - and never let a ceiling the
            # MODEL hit get reported as the USER being unclear.
            if max_out < 8000:
                max_out = 8000
                print("[guppi] hit the output ceiling; retrying once with more headroom")
                continue
            partial = "".join(b.text for b in response.content if b.type == "text").strip()
            print("[guppi] still truncated at the higher ceiling; asking to split the task")
            msg = ("That turned into more than I can write in one go. Ask me for it in "
                   "smaller pieces - one day at a time, or the calendar first and the "
                   "reminder after.")
            reply = f"{partial}\n\n{msg}" if partial else msg
            save_history(who_id, user_message, reply)
            return reply

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    # A tool that raises must not kill the whole turn. Catch it, log
                    # exactly what failed, and hand the model a graceful error string so
                    # it can adapt ("I couldn't reach the calendar just now") instead of
                    # the user getting a blank "something went wrong".
                    try:
                        out = run_tool(block.name, block.input, sender_name, sender_role,
                                       who_id, is_group)
                    except Exception as e:
                        print(f"[tool] ERROR in '{block.name}' input={block.input}: {e}")
                        out = (f"That didn't work just now ({block.name} ran into a "
                               f"problem). Let the user know and offer to try again.")
                    tools_ran.append(block.name)
                    # A tool normally returns a plain string. read_email may instead return
                    # {"text": ..., "images": [...]} when the email carried a picture worth
                    # looking at - a parking map, a schedule graphic, a flyer. The API
                    # accepts image blocks inside a tool_result, so hand them to the model
                    # directly rather than describing something we chose not to read.
                    if isinstance(out, dict):
                        content = [{"type": "text", "text": out.get("text", "")}]
                        for img in out.get("images", []):
                            content.append({"type": "image", "source": {
                                "type": "base64", "media_type": img["media_type"],
                                "data": img["data"]}})
                            print(f"[tool] attached {img['filename']} ({img['media_type']}) "
                                  f"to the {block.name} result")
                    else:
                        content = out
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": content})
            messages.append({"role": "user", "content": results})
            continue

        reply = "".join(b.text for b in response.content if b.type == "text").strip()
        if not reply:
            # The model sometimes runs a tool and then returns no words at all. The old
            # fallback said "I didn't catch that", which is a LIE when the work actually
            # happened - a person told their location wasn't saved when it had been. Never
            # report failure for a turn that succeeded.
            if tools_ran:
                print(f"[guppi] empty reply after tools ran: {tools_ran}")
                reply = "Done - that's taken care of."
            else:
                # Don't blame the person's phrasing for the model returning nothing.
                print(f"[guppi] empty reply, no tools ran, "
                      f"stop_reason={response.stop_reason}")
                reply = ("I didn't manage to put an answer together for that one. Try "
                         "rephrasing it, or break it into smaller steps.")
        # Remember this exchange for next time (store the raw user text, not the
        # time-hint wrapper, so history stays readable and doesn't pile up stale clocks).
        placeholder = "(sent a photo)" if image_data else ("(sent an attachment)" if doc_data else None)
        save_history(who_id, user_message if not placeholder else placeholder, reply)
        return reply

    return "Sorry, that took too many steps. Can you rephrase?"


# ---- Conversation memory (short, per-chat, in-memory) -----------------------
# Keyed by chat id. Holds the last few user/assistant text turns so short follow-ups
# resolve. In-memory only: it resets on redeploy, which is fine — it's for continuity
# within a conversation, not long-term recall (that's what the memories table is for).
_HISTORY = {}
_HISTORY_TURNS = 6          # keep the last 6 exchanges (12 messages) per chat

def get_history(chat_id):
    return list(_HISTORY.get(str(chat_id), []))

def save_history(chat_id, user_text, assistant_text):
    key = str(chat_id)
    hist = _HISTORY.get(key, [])
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": assistant_text})
    # Trim to the last N exchanges.
    _HISTORY[key] = hist[-(_HISTORY_TURNS * 2):]


@app.get("/")
def home():
    return {"status": "Project Hearth - Guppi (Telegram edition) is running."}


def _looks_schedulish(text):
    """Cheap pre-filter: could this message plausibly be about scheduling something?
    Keeps us from spending a Claude call on 'lol' or 'what's for dinner'. Deliberately
    broad — the Claude classifier makes the real decision; this just skips the obvious no."""
    if not text or len(text) < 6:
        return False
    t = text.lower()
    time_words = ("today", "tomorrow", "tonight", "monday", "tuesday", "wednesday",
                  "thursday", "friday", "saturday", "sunday", "morning", "afternoon",
                  "evening", "noon", "am", "pm", "o'clock", "oclock", "next week",
                  "this week", "weekend", "pick up", "pickup", "drop off", "dropoff",
                  "appointment", "practice", "game", "meeting", "remind", "at 1", "at 2",
                  "at 3", "at 4", "at 5", "at 6", "at 7", "at 8", "at 9", ":00", ":30",
                  "i'll", "i will", "i've got", "i got", "i can get", "i can do",
                  "i'll take", "on it", "i'll handle", "my turn", "you take", "can you")
    return any(w in t for w in time_words)


def _group_scheduling_intent(text):
    """Ask Claude, cheaply, whether an UNADDRESSED group message is a real schedulable
    task/event Guppi should offer to help with. Returns True only for clear cases, so
    Guppi doesn't butt into ordinary family chatter. Fails closed (silent) on any error."""
    if not _looks_schedulish(text):
        return False
    if not claude_call_allowed():
        return False
    try:
        resp = claude_create(
            model=MODEL, max_tokens=5,
            system=("Decide if this ONE message from a family group chat is either (a) a "
                    "specific event/appointment/task with a time someone might want on a "
                    "calendar or as a reminder ('pick up Charlotte at 3 tomorrow', "
                    "'dentist Tuesday at 2'), OR (b) someone committing to handle a task "
                    "('I'll grab Charlotte', 'I've got dinner', 'I can do pickup'). Answer "
                    "ONLY 'YES' or 'NO'. Answer NO for general chat, questions, opinions, "
                    "reactions, or anything without a concrete task/event/commitment."),
            messages=[{"role": "user", "content": text}])
        verdict = "".join(b.text for b in resp.content if b.type == "text").strip().upper()
        return verdict.startswith("YES")
    except Exception as e:
        print(f"[group] intent check failed: {e}")
        return False


# When Guppi speaks in a group, it opens a short window during which the next message
# from THAT person is treated as a reply to Guppi — so "Yes for 4:40" after an offer is
# heard, not dropped. Keyed by (group_chat_id, person_id) -> timestamp.
_GROUP_REPLY_WINDOW = {}
_GROUP_REPLY_SECONDS = 150   # ~2.5 minutes to answer Guppi's offer/question

def _reply_invites_an_answer(reply):
    """Did Guppi actually open a loop, or just answer and stop?

    The reply window was opened after EVERY group reply, so for 2.5 minutes afterwards any
    message from that person counted as addressed. That is how "Beware the milk is off"
    got answered: Jason had been talking to Guppi moments earlier, so his window was still
    open and ordinary chatter was treated as a reply. A window should only exist when
    there is a question waiting to be answered."""
    if not reply:
        return False
    r = reply.lower()
    if "?" in r:
        return True
    return any(p in r for p in (
        "want me to", "shall i", "should i", "would you like", "let me know",
        "just say the word", "tell me and i", "if you want me to"))


def _open_reply_window(group_chat_id, person_id):
    if group_chat_id and person_id:
        _GROUP_REPLY_WINDOW[(str(group_chat_id), str(person_id))] = time.time()

def _in_reply_window(group_chat_id, person_id):
    ts = _GROUP_REPLY_WINDOW.get((str(group_chat_id), str(person_id)))
    return bool(ts) and (time.time() - ts) < _GROUP_REPLY_SECONDS


# B1/B2: the bot's name was matched with startswith("guppi"), so "Guppy" (Kim's spelling)
# was ignored outright and "Also guppi can you..." was demoted to overheard mode. Match
# the name ANYWHERE in the message, and accept the obvious misspellings of a made-up word.
_BOT_NAMES = ("guppi", "guppy", "gupi", "guppie", "gupppi", "guppii")


def _mentions_bot_name(text):
    t = (text or "").lower()
    return any(re.search(rf"\b{n}\b", t) for n in _BOT_NAMES)


# B3: things only an assistant would be asked about. Used ONLY to promote a QUESTION to
# "addressed" - never on its own - so ordinary family chatter still doesn't wake Guppi.
_ASSISTANT_TOPICS = (
    "calendar", "remind", "reminder", "schedule", "appointment", "event",
    "reconnect", "connect", "my email", "our email", "inbox", "grocery list",
    "shopping list", "the list", "weather", "forecast", "backup", "back up")


def _is_question_for_guppi(text):
    """A direct question the assistant should answer even without being named.

    Two ways in: a stock phrase only Guppi would field ("what's on our plate?"), or a
    QUESTION that mentions something only Guppi does ("can you tell Kim how to reconnect
    her calendar?" - which was ignored until Jason shouted the name). Requiring both
    question-shape AND an assistant topic keeps Guppi out of ordinary conversation."""
    if not text:
        return False
    t = text.strip().lower()
    triggers = ("what's on our plate", "whats on our plate", "on our plate",
                "what's on the calendar", "whats on the calendar", "on the calendar",
                "who's got what", "whos got what", "who has what", "what did we agree",
                "what are our", "what's on our", "whats on our", "our reminders",
                "our schedule", "what's scheduled", "whats scheduled",
                "what's on my", "whats on my", "what do we have")
    if any(g in t for g in triggers):
        return True
    looks_like_question = (
        t.endswith("?")
        or t.startswith(("can you", "could you", "would you", "will you", "are you able",
                         "can u", "please can", "do you know", "are you")))
    return looks_like_question and any(k in t for k in _ASSISTANT_TOPICS)


def _is_addressed(text, message, bot_username):
    """In a GROUP, only respond when actually addressed. Otherwise Guppi would butt
    into every family conversation. Addressed means: a /command, an @mention of the
    bot, a reply to one of the bot's messages, the name 'Guppi' at the start, or a
    direct assistant-question like 'what's on our plate?'."""
    if not text:
        return False
    t = text.strip()
    if t.startswith("/"):
        return True
    if bot_username and f"@{bot_username}" in t.lower():
        return True
    reply_to = message.get("reply_to_message") or {}
    if (reply_to.get("from") or {}).get("is_bot"):
        return True
    if _mentions_bot_name(t):
        return True
    if _is_question_for_guppi(t):
        return True
    return False


@app.post("/telegram")
async def telegram_webhook(request: Request):
    # If a webhook secret is configured, verify it. Telegram echoes it in this header,
    # so a stranger POSTing to our URL can't impersonate Telegram.
    if TELEGRAM_WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, TELEGRAM_WEBHOOK_SECRET):
            print("[security] webhook secret mismatch; ignoring")
            return {"ok": True}
    else:
        # M1: without a webhook secret, ANYONE who can POST here can forge a message as any
        # bound chat_id and use that person's full permissions. Refuse rather than run open.
        print("[security] TELEGRAM_WEBHOOK_SECRET is not set — refusing webhook. Set it in "
              "Railway and re-run /set-webhook.")
        return {"ok": True}

    try:
        update = await request.json()
    except Exception:
        print("[tg] malformed webhook body; ignoring")
        return {"ok": True}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    is_group = chat_type in ("group", "supergroup")
    sender_chat_id = (message.get("from") or {}).get("id")
    text = message.get("text") or message.get("caption") or ""

    # ---- /connectemail: capture an app password SECURELY ------------------------
    # An app password is a credential. It must NOT be logged, and must NOT be sent
    # through Claude. So we handle it here directly, before the normal message path,
    # and redact it from the log line. Private chat only.
    if text.strip().lower().startswith("/connectemail"):
        if is_group:
            send_message(chat_id, "Let's do that privately - message me directly, not in the group.")
            return {"ok": True}
        name, role = identify_sender(sender_chat_id)
        if role == "unknown":
            send_message(chat_id, "I don't recognize you yet, so I can't connect an inbox.")
            return {"ok": True}
        if role not in ("adult", "caregiver"):
            send_message(chat_id, "Email isn't available for your access level.")
            return {"ok": True}
        parts = text.strip().split()
        if len(parts) != 3:
            send_message(chat_id,
                "To connect your email, send:\\n\\n"
                "`/connectemail your@email.com your-app-password`\\n\\n"
                "Use an APP PASSWORD, not your normal password. For Outlook/live.com, "
                "make one at account.microsoft.com under Security > Advanced security "
                "options > App passwords. I'll delete this message's password from my "
                "logs automatically.")
            return {"ok": True}
        _, addr, app_pw = parts
        print(f"[tg] {chat_type} chat={chat_id} from={sender_chat_id}: /connectemail {addr} <redacted>")
        result = save_imap_account(name, addr, app_pw)
        send_message(chat_id, result)
        # Best-effort: delete the user's message so the password doesn't linger in the
        # chat history either.
        mid = message.get("message_id")
        if mid:
            telegram_api("deleteMessage", {"chat_id": chat_id, "message_id": mid})
        return {"ok": True}

    print(f"[tg] {chat_type} chat={chat_id} from={sender_chat_id}: {text[:60]!r}")

    # ---- /start: the ONLY way to get bound --------------------------------------
    if text.strip().startswith("/start"):
        parts = text.strip().split(maxsplit=1)
        supplied = parts[1].strip() if len(parts) > 1 else ""
        if is_group:
            send_message(chat_id, "Set me up in a private chat with me, not here.")
            return {"ok": True}
        if supplied:
            send_message(chat_id, bind_adult(sender_chat_id, supplied))
        else:
            claimed = claim_pending(sender_chat_id)
            send_message(chat_id, claimed or (
                "Hi - I'm Guppi. I only work for one family. If you're a parent, send "
                "/start followed by your setup code. Otherwise ask a parent to add you."))
        return {"ok": True}

    # ---- /help: a reliable, model-free help message, tailored to the person -------
    # The slash-command works anywhere. The bare words "help"/"menu" only count in a
    # private chat, so a casual "help" in the group doesn't wake Guppi.
    # ---- The user guide. Model-FREE on purpose: the old "what can you do?" answer was
    # improvised by the model from a briefing that told it to "keep it to the highlights",
    # so a complete answer was impossible. These are sent verbatim.
    _t = text.strip().lower().strip("?!.")
    _t = re.sub(rf"^(hey |ok )?({'|'.join(_BOT_NAMES)})[,: ]+", "", _t).strip()
    _GUIDE_TRIGGERS = (
        "what can you do", "what else can you do", "what can u do", "how do i use you",
        "how do you work", "what do you do", "can you explain how to use",
        "explain how to use", "how do i use this", "what are you able to do",
        "show me what you can do", "full guide", "user guide")
    if _t.startswith("/guide") or _t.startswith("guide"):
        name, role = identify_sender(sender_chat_id)
        arg = _t.split(maxsplit=1)[1].strip() if len(_t.split(maxsplit=1)) > 1 else ""
        send_message(chat_id,
                     guide_section(arg, role, is_group) if arg
                     else guide_menu(name, role, is_group))
        print(f"[guide] served {'section ' + arg if arg else 'menu'} to {name or 'unknown'}")
        return {"ok": True}
    if any(g in _t for g in _GUIDE_TRIGGERS):
        name, role = identify_sender(sender_chat_id)
        if role in ("adult", "caregiver", "child"):
            send_message(chat_id, guide_menu(name, role, is_group))
            print(f"[guide] served menu to {name} (natural-language ask)")
            return {"ok": True}

    _t = text.strip().lower()
    if _t in ("/help", "/menu") or (not is_group and _t in ("help", "menu")):
        name, role = identify_sender(sender_chat_id)
        if role == "unknown":
            send_message(chat_id,
                "Hi - I'm Guppi, a private family assistant. I don't recognize you yet. "
                "If you're a parent, send /start followed by your setup code. Otherwise "
                "ask a parent in the family to add you.")
        else:
            send_message(chat_id, welcome_message(name, role))
        return {"ok": True}

    # ---- In a group, stay quiet unless spoken to -- OR unless someone mentions a
    # concrete event/task Guppi could schedule, in which case it speaks up to OFFER.
    group_offer = False
    if is_group and not _is_addressed(text, message, BOT_USERNAME):
        if _in_reply_window(chat_id, sender_chat_id):
            # Guppi just spoke to this person; their next message is a reply to it
            # (e.g. "Yes for 4:40"). Treat it as addressed so the loop can close.
            pass
        elif _group_scheduling_intent(text):
            group_offer = True   # a schedulable task was mentioned; offer to help
        else:
            print(f"[tg] group chatter from {sender_chat_id} - not addressed, staying "
                  f"quiet: {text[:50]!r}")
            return {"ok": True}

    # ---- Cost/abuse gate (H1): before ANY Claude call or attachment download ---------
    # An unknown sender never reaches Claude via the interactive path — they get a static
    # reply, so a stranger who finds the bot can't run up API cost or use us as a search
    # proxy. Known senders are rate-limited per minute as a backstop.
    _gate_name, _gate_role = identify_sender(sender_chat_id)
    is_known = _gate_role in ("adult", "caregiver", "child")
    if not is_known:
        # Unknown: no Claude, no attachment fetch. One cheap static reply, rate-limited so
        # even that can't be spammed.
        print(f"[security] unknown sender {sender_chat_id}; static reply only")
        if interactive_rate_ok(sender_chat_id, is_known=False):
            send_message(chat_id,
                "Hi - I'm Guppi, a private family assistant. I only work for one family. "
                "If you're a parent, send /start followed by your setup code; otherwise "
                "ask a parent to add you.")
        return {"ok": True}
    if not interactive_rate_ok(sender_chat_id, is_known=True):
        print(f"[tg] RATE LIMITED {sender_chat_id} - told them to slow down")
        send_message(chat_id, "You're sending messages very quickly - give me a moment and "
                              "try again.")
        return {"ok": True}

    # ---- Photos -----------------------------------------------------------------
    image_data = image_media_type = None
    photos = message.get("photo") or []
    if photos:
        # Telegram sends several sizes; the last is the largest.
        fetched = fetch_telegram_photo(photos[-1]["file_id"])
        if fetched:
            image_data, image_media_type = fetched
            print(f"[tg] fetched photo ({image_media_type})")
        if not text:
            text = "(sent a photo)"

    # ---- Documents / attachments (school PDFs, scanned flyers) -------------------
    doc_data = doc_media_type = None
    document = message.get("document")
    if document:
        got = fetch_telegram_document(
            document.get("file_id"), document.get("mime_type"),
            document.get("file_name"))
        if got:
            b64, kind, media_type = got
            print(f"[tg] fetched document ({kind}, {media_type})")
            if kind == "pdf":
                doc_data, doc_media_type = b64, media_type
            else:  # an image sent as a file -> treat like a photo
                image_data, image_media_type = b64, media_type
            if not text:
                text = "(sent an attachment)"
        else:
            send_message(chat_id, "I couldn't open that attachment. If it's a PDF or "
                                  "photo I can read it - otherwise try sending it as a photo.")
            return {"ok": True}

    if not text and not image_data and not doc_data:
        return {"ok": True}

    # Strip a leading @mention so Claude doesn't see it as part of the request.
    if BOT_USERNAME:
        text = re.sub(rf"@{re.escape(BOT_USERNAME)}\b", "", text, flags=re.I).strip()

    try:
        reply = ask_guppi(text or "(no text)", chat_id, sender_chat_id, is_group,
                          image_data, image_media_type, group_offer=group_offer,
                          doc_data=doc_data, doc_media_type=doc_media_type)
    except Exception as e:
        print(f"Error in ask_guppi: {e}")
        reply = "Sorry, I'm having a little trouble right now. Try again in a moment."

    send_message(chat_id, reply)
    # In a group, opening a short reply window means the person's NEXT message (their
    # "yes"/"actually 4:40") is treated as a reply to Guppi and won't be dropped.
    if is_group and reply and _reply_invites_an_answer(reply):
        _open_reply_window(chat_id, sender_chat_id)
        # B5: the window was opened ONLY for the person who spoke. Kim asked Guppi to
        # remind Jason, Guppi asked a clarifying question, JASON answered "Yes I'm here" -
        # and it was dropped, because the window belonged to Kim. If the reply names other
        # family members, they are part of the conversation too.
        try:
            for _n, _c in _adults_with_chats():
                if _c and str(_c) != str(sender_chat_id) and \
                        re.search(rf"\b{re.escape(_n)}\b", reply, re.I):
                    _open_reply_window(chat_id, _c)
                    print(f"[tg] reply window also opened for {_n} (named in the reply)")
        except Exception as e:
            print(f"[tg] could not widen reply window: {e}")
    return {"ok": True}


def _ignored_senders():
    """The list of email sender substrings the family has asked Guppi to ignore for
    deadline/invoice flagging (e.g. 'todoist'). Stored family-wide, lowercased."""
    raw = get_setting("deadline_ignore_senders") or ""
    return [s for s in raw.split("|") if s]


def _priority_senders(person):
    """Sender substrings this person always wants surfaced (e.g. 'school', a coach's
    address). Per-person, since priorities differ between parents. Lowercased."""
    raw = get_setting(f"priority_senders_{person}") or ""
    return [s for s in raw.split("|") if s]


def _sender_stats(person):
    """Per-sender outcome tallies for THIS person: how often they acted on vs. dismissed
    flagged mail from a sender. Drives the learned nudge. Stored as a compact JSON dict."""
    raw = get_setting(f"sender_stats_{person}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _record_sender_outcome(person, sender_fragment, acted):
    """Bump the acted/dismissed tally for a sender. Called when the user acts on (or
    dismisses) something Guppi flagged. Bounded to the 40 most-active senders."""
    if not sender_fragment:
        return
    key = sender_fragment.strip().lower()[:40]
    if not key:
        return
    stats = _sender_stats(person)
    rec = stats.get(key, {"acted": 0, "dismissed": 0})
    rec["acted" if acted else "dismissed"] += 1
    stats[key] = rec
    if len(stats) > 40:
        ranked = sorted(stats.items(),
                        key=lambda kv: kv[1]["acted"] + kv[1]["dismissed"], reverse=True)
        stats = dict(ranked[:40])
    set_setting(f"sender_stats_{person}", json.dumps(stats))


def _pending_outcomes(person):
    """Senders flagged in the most recent poll that haven't been resolved (acted or
    dismissed) yet."""
    raw = get_setting(f"pending_outcomes_{person}") or ""
    return [s for s in raw.split("|") if s]


def _set_pending_outcomes(person, senders):
    """A new poll flagged `senders`. Any senders still pending from a PREVIOUS poll were
    never engaged -> count them as dismissed, then replace the pending set with the new
    ones. This is how 'you ignored it' becomes a signal without the user doing anything."""
    stale = _pending_outcomes(person)
    for s in stale:
        _record_sender_outcome(person, s, acted=False)   # unengaged -> dismissed
    # Store the new pending set (bounded).
    clean = [s.strip()[:60] for s in senders if s.strip()][:20]
    set_setting(f"pending_outcomes_{person}", "|".join(clean))


def _resolve_acted_outcomes(person, text):
    """Called when the person sends a message. If they're engaging with a just-flagged
    email (asking for a reminder, calendar add, reply, etc.), credit the pending senders
    as 'acted' and clear them. Kept deliberately simple: any substantive engagement while
    outcomes are pending counts as acting on the flag."""
    pending = _pending_outcomes(person)
    if not pending:
        return
    t = (text or "").lower()
    # Signals that the person is acting on the flagged email(s).
    act_cues = ("remind", "reminder", "calendar", "add ", "yes", "reply", "draft",
                "schedule", "set ", "pay", "rsvp", "sure", "do it", "please")
    if any(cue in t for cue in act_cues):
        for s in pending:
            _record_sender_outcome(person, s, acted=True)
        set_setting(f"pending_outcomes_{person}", "")   # resolved


def _learned_suggestion(person):
    """If outcome data clearly points one way for a sender, return a one-line suggestion
    Guppi can offer. Conservative: needs a clear pattern, only suggests, never auto-applies."""
    stats = _sender_stats(person)
    ignored = set(_ignored_senders())
    priority = set(_priority_senders(person))
    for sender, rec in stats.items():
        a, d = rec.get("acted", 0), rec.get("dismissed", 0)
        if d >= 3 and a == 0 and sender not in ignored:
            return (f"You've dismissed the last {d} flagged emails from \"{sender}\". "
                    f"Want me to stop flagging them?")
        if a >= 3 and d == 0 and sender not in priority:
            return (f"You've acted on the last {a} emails from \"{sender}\". "
                    f"Want me to always flag them as priority?")
    return None


def tool_manage_email_priorities(action, sender=None, person=None):
    """Manage which senders are PRIORITY (always flag) or IGNORED (never flag) for this
    person, and view learned patterns.
    action: 'prioritize' | 'ignore' | 'unprioritize' | 'unignore' | 'list'."""
    action = (action or "list").lower()
    pri = _priority_senders(person)
    ign = _ignored_senders()

    if action == "list":
        lines = []
        if pri:
            lines.append("Always flag: " + ", ".join(pri))
        if ign:
            lines.append("Never flag: " + ", ".join(ign))
        if not lines:
            lines.append("No priority or ignore rules set yet.")
        sug = _learned_suggestion(person)
        if sug:
            lines.append("\n" + sug)
        return "\n".join(lines)

    if not sender:
        return "Tell me which sender - e.g. 'always flag emails from the school'."
    key = sender.strip().lower()

    if action == "prioritize":
        if key not in pri:
            pri.append(key)
            set_setting(f"priority_senders_{person}", "|".join(pri[:50]))
        if key in ign:
            ign = [s for s in ign if s != key]
            set_setting("deadline_ignore_senders", "|".join(ign))
        return f"Done - I'll always flag emails from \"{key}\" as priority."
    if action == "ignore":
        if key not in ign:
            ign.append(key)
            set_setting("deadline_ignore_senders", "|".join(ign[:50]))
        if key in pri:
            pri = [s for s in pri if s != key]
            set_setting(f"priority_senders_{person}", "|".join(pri))
        return f"Done - I'll stop flagging emails from \"{key}\"."
    if action == "unprioritize":
        pri = [s for s in pri if s != key]
        set_setting(f"priority_senders_{person}", "|".join(pri))
        return f"Okay - \"{key}\" is no longer a priority sender."
    if action == "unignore":
        ign = [s for s in ign if s != key]
        set_setting("deadline_ignore_senders", "|".join(ign))
        return f"Okay - I'll consider emails from \"{key}\" again."
    return "I can prioritize, ignore, unprioritize, unignore, or list senders."


def tool_manage_deadline_ignores(action, sender=None):
    """Let the family control which senders are ignored for deadline alerts.
    action: 'add' | 'remove' | 'list'. sender: a name or address fragment, e.g. 'todoist'."""
    current = _ignored_senders()
    action = (action or "list").lower()
    if action == "list":
        if not current:
            return "I'm not ignoring any senders for deadline alerts right now."
        return "I'm ignoring deadline alerts from: " + ", ".join(current)
    if not sender:
        return "Tell me which sender - for example, 'ignore deadline emails from Todoist'."
    key = sender.strip().lower()
    if action == "add":
        if key in current:
            return f"I was already ignoring {key} for deadline alerts."
        current.append(key)
        set_setting("deadline_ignore_senders", "|".join(current[:50]))
        return (f"Done - I'll no longer flag deadlines from emails matching \"{key}\". "
                f"You can undo this by saying 'stop ignoring {key}'.")
    if action == "remove":
        if key not in current:
            return f"I wasn't ignoring {key}."
        current = [s for s in current if s != key]
        set_setting("deadline_ignore_senders", "|".join(current))
        return f"Okay - I'll flag deadlines from {key} again."
    return "I can add, remove, or list ignored senders."


def _adults_with_chats():
    conn = db()
    rows = conn.execute(
        "SELECT name, chat_id FROM people WHERE role='adult' AND chat_id IS NOT NULL"
    ).fetchall()
    conn.close()
    return [(r["name"], r["chat_id"]) for r in rows]


def _next_occurrence(due_dt, repeat):
    """Given a fired reminder's time and its repeat rule, return the next datetime it
    should fire, or None for a one-time reminder."""
    repeat = (repeat or "none").lower()
    if repeat == "none":
        return None
    if repeat == "daily":
        return due_dt + datetime.timedelta(days=1)
    if repeat == "monthly":
        # same day next month (clamp to end of month handled loosely by adding ~30d
        # then snapping to the same day-of-month when possible)
        month = due_dt.month + 1
        year = due_dt.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        day = due_dt.day
        # step back until valid (handles 31st -> shorter months)
        while day > 28:
            try:
                return due_dt.replace(year=year, month=month, day=day)
            except ValueError:
                day -= 1
        return due_dt.replace(year=year, month=month, day=day)
    # weekly, optionally pinned to a weekday: weekly or weekly:wed
    weekdays = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}
    if repeat == "weekly":
        return due_dt + datetime.timedelta(days=7)
    if repeat.startswith("weekly:"):
        target = weekdays.get(repeat.split(":")[1], due_dt.weekday())
        days_ahead = (target - due_dt.weekday()) % 7
        days_ahead = 7 if days_ahead == 0 else days_ahead
        return due_dt + datetime.timedelta(days=days_ahead)
    return None


def job_reminders():
    """Every minute: send any reminders now due. Recurring ones reschedule to their
    next occurrence instead of being marked permanently done. No Claude call, so it's
    cheap; still respects the text cap and quiet hours."""
    if not proactive_on():
        return
    now = now_local()
    conn = db()
    # Pull unfired reminders and compare as PARSED aware datetimes, not as SQL strings —
    # string comparison breaks across DST offset changes or if a timestamp lacks seconds
    # or uses 'Z'. (M2)
    rows = conn.execute(
        "SELECT id, text, for_chat, due_at, repeat FROM reminders WHERE fired = 0").fetchall()
    conn.close()
    due = []
    for r in rows:
        try:
            dt = datetime.datetime.fromisoformat(r["due_at"])
        except (ValueError, TypeError):
            continue
        # Treat a naive timestamp as local time so it can be compared with `now` (aware).
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        if dt <= now:
            due.append((r, dt))
    for r, due_dt in due:
        target = r["for_chat"] or _first_parent_chat()
        if not (target and send_message(target, f"Reminder: {r['text']}", proactive=True)):
            continue
        # Sent. If recurring, roll forward to the NEXT occurrence that's actually in the
        # future — after downtime a single +1 step could still be in the past and would
        # re-fire every minute ("storm"). Loop until it's past now. (M6)
        nxt = _next_occurrence(due_dt, r["repeat"])
        guard = 0
        while nxt and nxt <= now and guard < 400:
            nxt = _next_occurrence(nxt, r["repeat"])
            guard += 1
        conn = db()
        if nxt:
            conn.execute("UPDATE reminders SET due_at = ? WHERE id = ?",
                         (nxt.isoformat(), r["id"]))
        else:
            conn.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (r["id"],))
        conn.commit(); conn.close()


def reminders_for_briefing(for_chat=None, horizon_hours=36):
    """Reminders relevant to THIS MORNING's briefing: ones due today or within the next
    ~36 hours. The full list (via list_reminders) dumps everything — so a Saturday
    briefing wrongly showed Tuesday's recycling. This scopes to what's actually imminent,
    which is what a morning briefing is for."""
    now = now_local()
    cutoff = now + datetime.timedelta(hours=horizon_hours)
    conn = db()
    if for_chat:
        rows = conn.execute(
            "SELECT text, due_at FROM reminders WHERE fired = 0 "
            "AND (for_chat = ? OR for_chat IS NULL) ORDER BY due_at",
            (str(for_chat),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT text, due_at FROM reminders WHERE fired = 0 ORDER BY due_at").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            due = datetime.datetime.fromisoformat(r["due_at"])
        except (ValueError, TypeError):
            continue
        if due <= cutoff:                    # today or imminent only
            when = due.strftime("%-I:%M %p") if due.date() == now.date() else due.strftime("%a %-I:%M %p")
            out.append(f"{when}: {r['text']}")
    return "\n".join(out) if out else "None due today."


def _briefing_calendar():
    """The calendar for a briefing, grouped under explicit day headings.

    A6b: the briefing used to be handed tool_check_calendar(days_ahead=1), whose window is
    (now - 18h) to (now + 1 day) - at a 6am Monday briefing that is SUNDAY NOON through
    TUESDAY 6AM, roughly 42 hours, printed under the heading "Today's calendar" with raw
    ISO timestamps and no day boundaries. No wonder it "hit various timeframes": nothing
    in its input distinguished yesterday from today from tomorrow.

    The 18-hour lookback stays - it is what lets an overnight sleepover be recognised as
    in-progress - but events are now labelled by day, and anything already finished is
    dropped rather than left as noise."""
    service = get_calendar_service()
    if not service:
        return "CALENDAR: not available right now."
    now = now_local()
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    try:
        result = service.events().list(
            calendarId=FAMILY_CALENDAR_ID,
            timeMin=(now - datetime.timedelta(hours=18)).isoformat(),
            timeMax=(now + datetime.timedelta(days=2)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=40).execute()
    except Exception as e:
        print(f"[briefing] calendar read failed: {e}")
        return "CALENDAR: could not be read this morning."

    buckets = {"IN PROGRESS RIGHT NOW": [], "TODAY": [], "TOMORROW": []}
    for e in result.get("items", []):
        start_raw = e["start"].get("dateTime", e["start"].get("date"))
        end_raw = e.get("end", {}).get("dateTime", e.get("end", {}).get("date"))
        summary = e.get("summary", "(no title)")
        loc = f" @ {e['location']}" if e.get("location") else ""
        try:
            sdt = datetime.datetime.fromisoformat(start_raw)
            if sdt.tzinfo is None:
                sdt = sdt.replace(tzinfo=TIMEZONE)
            edt = None
            if end_raw:
                edt = datetime.datetime.fromisoformat(end_raw)
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=TIMEZONE)
        except (ValueError, TypeError):
            continue

        if edt and edt <= now:
            continue                       # already over - not briefing material
        span = sdt.strftime("%-I:%M %p") + (f" to {edt.strftime('%-I:%M %p')}" if edt else "")
        if edt and sdt <= now < edt:
            buckets["IN PROGRESS RIGHT NOW"].append(
                f"  - {summary}{loc} (started {sdt.strftime('%a %-I:%M %p')}, ends "
                f"{edt.strftime('%a %-I:%M %p')}) - it is ALREADY UNDERWAY, so the action "
                f"is finishing or collecting, never preparing")
        elif sdt.date() == today:
            buckets["TODAY"].append(f"  - {span}: {summary}{loc}")
        elif sdt.date() == tomorrow:
            buckets["TOMORROW"].append(f"  - {span}: {summary}{loc}")

    out = []
    for head in ("IN PROGRESS RIGHT NOW", "TODAY", "TOMORROW"):
        items = buckets[head]
        if head == "TODAY" and not items:
            out.append("TODAY: nothing on the calendar.")
        elif items:
            out.append(f"{head}:\n" + "\n".join(items))
    return "\n\n".join(out) if out else "TODAY: nothing on the calendar."


def _briefing_reminders(chat):
    """Reminders split into today and tomorrow, instead of 36 hours labelled 'due today'.

    A6c: reminders_for_briefing(horizon_hours=36) at 6am Monday reaches 6pm TUESDAY, and
    every one of them was printed under the heading "Reminders due today" - so tomorrow's
    reminders were announced as today's."""
    now = now_local()
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT text, due_at FROM reminders WHERE fired = 0 "
            "AND (for_chat = ? OR for_chat IS NULL) ORDER BY due_at",
            (str(chat),)).fetchall()
    finally:
        conn.close()
    today_l, tom_l = [], []
    for r in rows:
        try:
            due = datetime.datetime.fromisoformat(r["due_at"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=TIMEZONE)
        except (ValueError, TypeError):
            continue
        if due < now:
            continue
        if due.date() == today:
            today_l.append(f"  - {due.strftime('%-I:%M %p')}: {r['text']}")
        elif due.date() == tomorrow:
            tom_l.append(f"  - {due.strftime('%-I:%M %p')}: {r['text']}")
    parts = []
    parts.append("REMINDERS DUE TODAY:\n" + "\n".join(today_l) if today_l
                 else "REMINDERS DUE TODAY: none.")
    if tom_l:
        parts.append("REMINDERS DUE TOMORROW (mention only if it needs prep today):\n"
                     + "\n".join(tom_l))
    return "\n\n".join(parts)


def job_morning_briefing():
    """6am: one short briefing to each parent.

    Rebuilt after 2026-07-19, where it "hit various timeframes" and couldn't work out what
    events actually were. Four causes, all in what it was FED rather than how it reasoned:

      A6a - it was never told the date or the time. ask_guppi injects a clock into every
            interactive message precisely because "the model invents a plausible-looking
            time" without one; the briefing, which is entirely about time, had neither.
      A6b - "Today's calendar" actually held ~42 hours (see _briefing_calendar).
      A6c - "Reminders due today" actually held 36 hours (see _briefing_reminders).
      A6d - a 300-CHARACTER limit, which is about two sentences: no room to say which day
            something falls on, let alone reason about it.
    """
    if not proactive_on() or in_quiet_hours():
        return
    calendar = _briefing_calendar()
    weather = get_weather_line()
    now = now_local()
    when = (f"Right now it is {now.strftime('%-I:%M %p')} on "
            f"{now.strftime('%A, %B %-d, %Y')}. TODAY means {now.strftime('%A %B %-d')}; "
            f"TOMORROW means {(now + datetime.timedelta(days=1)).strftime('%A %B %-d')}.")

    for name, chat in _adults_with_chats():
        if not claude_call_allowed():
            return
        reminders = _briefing_reminders(chat)
        context = (f"{when}\n\n{calendar}\n\n{reminders}\n\n"
                   f"{weather or 'WEATHER: unavailable - could not be fetched.'}")
        try:
            resp = claude_create(
                model=MODEL, max_tokens=1000,
                system=("You are Guppi. Write ONE good-morning briefing for a parent, in "
                        "plain text. You are given the current date and time - use them, "
                        "and never describe something as today when the input puts it "
                        "under TOMORROW.\n\n"
                        "Cover, in this order: what is happening TODAY with times; "
                        "anything IN PROGRESS RIGHT NOW (the action there is finishing or "
                        "collecting, never preparing - do not tell someone to pack for a "
                        "thing their child already left for); reminders due today; and the "
                        "weather. Mention tomorrow ONLY if it needs doing something today. "
                        "If the weather line says unavailable, omit weather entirely - "
                        "NEVER invent a forecast.\n\n"
                        "Be specific and think about what each event actually MEANS for "
                        "the day: note when things collide, when a gap is too tight to get "
                        "between them, and what has to leave the house with someone. Say "
                        "the useful thing rather than listing entries.\n\n"
                        "No markdown, no emoji. Aim for 400-700 characters - short enough "
                        "to read at a glance, long enough to be specific. Warm but "
                        "efficient."),
                messages=[{"role": "user", "content": context}])
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as e:
            print(f"[briefing] Claude failed for {name}: {e}")
            continue
        if not text:
            print(f"[briefing] empty briefing for {name}; skipping")
            continue
        print(f"[briefing] sent to {name} ({len(text)} chars)")
        send_message(chat, text, proactive=True)


def _people_with_chats_by_role():
    conn = db()
    rows = conn.execute(
        "SELECT name, chat_id, role FROM people WHERE chat_id IS NOT NULL").fetchall()
    conn.close()
    return [(r["name"], r["chat_id"], r["role"]) for r in rows]


def job_weekly_digest():
    """Sunday 6pm: a 'week ahead' summary. Parents get the full week; children and the
    caregiver get a lighter version focused on what's relevant to them."""
    if not proactive_on() or in_quiet_hours():
        return
    week = tool_check_calendar(days_ahead=7)

    for name, chat, role in _people_with_chats_by_role():
        if not claude_call_allowed():
            return
        if role in ("adult",):
            instr = ("Write ONE short plain-text 'week ahead' summary for a parent: the "
                     "key events across the next 7 days. No markdown, no emoji, keep it "
                     "tight (a few lines).")
        elif role == "caregiver":
            instr = ("Write ONE short plain-text 'week ahead' note for the family's "
                     "caregiver. Include only childcare-relevant logistics (drop-offs, "
                     "pickups, activities, appointments they'd help with). Omit personal "
                     "or medical details that don't affect childcare. No markdown/emoji.")
        else:  # child
            instr = (f"Write ONE short, friendly, age-appropriate plain-text 'week ahead' "
                     f"note for {name}, a child. Include only THEIR activities and things "
                     f"they need to know (their practices, events, school things). Do not "
                     f"list other family members' private appointments. No markdown/emoji.")
        try:
            resp = claude_create(
                model=MODEL, max_tokens=300,
                system="You are Guppi. " + instr,
                messages=[{"role": "user",
                           "content": f"The next 7 days on the family calendar:\n{week}"}])
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as e:
            print(f"[weekly] Claude failed for {name}: {e}")
            continue
        if text:
            send_message(chat, text, proactive=True)


def _poll_unread(person):
    """Newest unread across every inbox this person connected (Gmail + IMAP), normalized.
    The 'anything new?' check is a free provider call — Claude only runs if there IS new
    mail."""
    out = []
    provs = connected_providers(person)
    if "google" in provs:
        service = get_gmail_service(person)
        if service:
            try:
                # A7: no date filter meant week-old unread mail was announced as "your
                # new email" forever, along with deadlines that had already passed.
                res = service.users().messages().list(
                    userId="me", q="is:unread category:primary newer_than:3d",
                    maxResults=5).execute()
                for m in res.get("messages", []):
                    md = service.users().messages().get(
                        userId="me", id=m["id"], format="metadata",
                        metadataHeaders=["From", "Subject"]).execute()
                    h = {x["name"]: x["value"] for x in md["payload"]["headers"]}
                    out.append({"id": f"g:{m['id']}", "from": h.get("From", "?"),
                                "subject": h.get("Subject", "(no subject)"),
                                "snippet": md.get("snippet", "")[:120]})
            except Exception as e:
                print(f"[poll] gmail failed for {person}: {e}")
    if "imap" in provs:
        out += imap_unread(person, max_results=5)
    return out


def job_urgent_email_poll():
    """Every N minutes (6am-10pm): for each connected adult, scan new unread mail across
    all their inboxes for two things:
      1. genuine urgency (school closure, appointment change) -> alert now
      2. deadlines / due dates / invoices -> proactively offer to set a reminder or add
         a calendar entry
    Only calls Claude when there IS new mail. Alerts ONLY that inbox's owner. De-dupes so
    the same email is never flagged twice."""
    if not proactive_on() or in_quiet_hours():
        return
    for name, chat in _adults_with_chats():
        if not connected_providers(name):
            continue
        msgs = _poll_unread(name)
        if not msgs:
            continue
        newest = msgs[0]["id"]
        last_id = get_setting(f"last_email_id_{name}")
        if newest == last_id:
            continue  # nothing new -> no Claude call, no cost
        set_setting(f"last_email_id_{name}", newest)

        ignore = _ignored_senders()
        priority = _priority_senders(name)
        summaries = []
        priority_hits = []      # senders on this person's always-flag list that showed up
        for m in msgs:
            if m["id"] == last_id:
                break
            frm = (m.get("from") or "").lower()
            is_priority = any(p in frm for p in priority)
            # Ignore filter applies UNLESS the sender is explicitly a priority (explicit
            # priority always wins over ignore).
            if not is_priority and any(bad in frm for bad in ignore):
                continue
            snip = f" - {m['snippet']}" if m.get("snippet") else ""
            tag = " [PRIORITY SENDER]" if is_priority else ""
            summaries.append(f"From {m['from']}: {m['subject']}{snip}{tag}")
            if is_priority:
                priority_hits.append(m.get("from") or "")
        if not summaries:
            continue
        if not claude_call_allowed():
            return

        today = now_local().strftime("%A, %B %d, %Y")
        try:
            resp = claude_create(
                model=MODEL, max_tokens=350,
                system=(f"You are Guppi, scanning a family member's NEW unread emails. "
                        f"Today is {today}. Look for two kinds of things:\n"
                        f"1) URGENT: genuinely time-sensitive items worth an interruption "
                        f"now (school closure, appointment change, safety, a bill due very "
                        f"soon).\n"
                        f"2) DEADLINES/INVOICES: due dates, payment amounts and due dates, "
                        f"RSVP-by dates, form deadlines, appointment dates.\n"
                        f"3) PRIORITY SENDERS: any email tagged [PRIORITY SENDER] is from "
                        f"someone this person told me to ALWAYS surface. Include it as an "
                        f"item even if it has no explicit deadline - summarize what it's "
                        f"about so they don't miss it.\n"
                        f"IGNORE marketing, promotions, newsletters, 'limited time offers', "
                        f"and routine notifications — those are never urgent or deadlines "
                        f"(UNLESS tagged [PRIORITY SENDER]).\n"
                        f"Reply with STRICT JSON only, no prose:\n"
                        f'{{"urgent": "<one short sentence, or empty>", '
                        f'"items": [{{"what": "<short description incl. amount if an '
                        f'invoice>", "date": "<YYYY-MM-DD or empty if none>", '
                        f'"from": "<sender>"}}]}}\n'
                        f"If nothing qualifies, reply {{\"urgent\": \"\", \"items\": []}}."),
                messages=[{"role": "user", "content": "\n\n".join(summaries)}])
            raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as e:
            print(f"[poll] Claude failed for {name}: {e}")
            continue

        # Parse the JSON verdict defensively.
        try:
            cleaned = raw.replace("```json", "").replace("```", "").strip()
            verdict = json.loads(cleaned)
        except Exception:
            print(f"[poll] couldn't parse verdict for {name}: {raw[:120]}")
            continue

        # 1) Urgent -> alert immediately.
        urgent = (verdict.get("urgent") or "").strip()
        if urgent and urgent.upper() != "NONE":
            send_message(chat, urgent, proactive=True)

        # 2) Deadlines / invoices -> offer to act, de-duped so we never repeat one.
        seen_key = f"flagged_deadlines_{name}"
        already = set((get_setting(seen_key) or "").split("|")) if get_setting(seen_key) else set()
        new_lines = []
        new_sigs = []
        for it in verdict.get("items", []):
            what = (it.get("what") or "").strip()
            if not what:
                continue
            date = (it.get("date") or "").strip()
            frm = (it.get("from") or "").strip()
            sig = f"{what[:40]}|{date}"          # dedupe signature
            if sig in already:
                continue
            new_sigs.append(sig)
            when = f" (due {date})" if date else ""
            src = f" — from {frm}" if frm else ""
            new_lines.append(f"• {what}{when}{src}")

        if new_lines:
            body = ("I spotted possible deadlines in your new email:\n"
                    + "\n".join(new_lines)
                    + "\n\nWant me to set reminders or add any to the calendar? "
                      "Just tell me which.")
            send_message(chat, body, proactive=True)
            # Remember what we've flagged (cap the stored history so it can't grow forever).
            merged = list(already | set(new_sigs))
            set_setting(seen_key, "|".join(merged[-100:]))
            # LEARNING: record the senders we just flagged as "awaiting outcome". If the
            # person engages with this flag (asks for a reminder/calendar/reply) before the
            # next poll, those senders get an "acted" tally; if a later poll flags new mail
            # and these were never engaged, they get "dismissed". This is what makes the
            # priority model adaptive. Senders that were flagged BECAUSE they're already on
            # the explicit priority list are skipped (no need to learn what you told us).
            flagged_senders = [(it.get("from") or "").strip()
                               for it in verdict.get("items", []) if it.get("from")]
            pri = _priority_senders(name)
            learnable = [s for s in flagged_senders
                         if s and not any(p in s.lower() for p in pri)]
            if learnable:
                _set_pending_outcomes(name, learnable)

        # LEARNING: if a clear pattern has emerged, offer a rule change (at most once per
        # poll, and only when we actually messaged them, to avoid nagging out of nowhere).
        # Track already-offered suggestions in ONE bounded list per person (not a settings
        # row each — that would grow forever).
        if new_lines:
            sug = _learned_suggestion(name)
            if sug:
                sig = sug[:40]
                offered_raw = get_setting(f"suggested_{name}") or ""
                offered = [s for s in offered_raw.split("||") if s]
                if sig not in offered:
                    send_message(chat, sug, proactive=True)
                    offered.append(sig)
                    set_setting(f"suggested_{name}", "||".join(offered[-30:]))  # bounded


# =============================================================================
#  SCHEDULER STARTUP
# =============================================================================
scheduler = BackgroundScheduler(timezone=str(TIMEZONE))

def _job_error_listener(event):
    """A background job raised. Log exactly which one and why, so a failing briefing or
    poll is visible and diagnosable — and never silently stops the whole scheduler."""
    print(f"[scheduler] JOB '{event.job_id}' FAILED: {event.exception!r}")

def start_scheduler():
    from apscheduler.events import EVENT_JOB_ERROR
    scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)
    poll_min = int(get_setting("poll_minutes") or 30)
    scheduler.add_job(job_reminders, "interval", minutes=1, id="reminders",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_morning_briefing, "cron", hour=6, minute=0, id="briefing",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_urgent_email_poll, "interval", minutes=poll_min,
                      id="email_poll", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_weekly_digest, "cron", day_of_week="sun", hour=18, minute=0,
                      id="weekly_digest", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_daily_backup, "cron", hour=3, minute=30, id="daily_backup",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_occasion_reminders, "cron", hour=7, minute=0, id="occasions",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()
    print(f"[scheduler] started: reminders/min, briefing 6am, occasions 7am, weekly Sun 6pm, email poll/{poll_min}min, backup 3:30am (all sent PRIVATELY, never to the group)")


@app.get("/backup")
def backup_download(token: str = ""):
    """Download a fresh DB snapshot. Ask Guppi "send me a backup link" in Telegram to get
    a signed, single-use, 15-minute URL.

    The secret no longer rides in the query string: it was landing in Railway's request
    logs and browser history, and that same secret signs the OAuth state, so leaking it
    cost more than one download. Tokens are short-lived, single-use, and purpose-bound."""
    if not _verify_access_token(token, "backup"):
        return Response(
            content=json.dumps({"ok": False,
                                "error": "this link is invalid, expired, or already used"}),
            status_code=403, media_type="application/json")
    snap = make_db_snapshot()
    if not snap:
        return {"ok": False, "error": "snapshot failed"}
    return FileResponse(snap, filename=os.path.basename(snap),
                        media_type="application/octet-stream")


@app.post("/restore")
async def restore_upload(request: Request, token: str = "", file: UploadFile = File(...)):
    """Restore the database from a previously downloaded backup. This OVERWRITES the live
    DB, so it is guarded by a single-use token AND makes a safety copy of the current DB
    first. Ask Guppi "give me a restore link" to mint one; a /backup token will NOT work
    here, so a leaked download link can never be turned into a data-destroying one."""
    if not _verify_access_token(token, "restore"):
        return Response(
            content=json.dumps({"ok": False,
                                "error": "this link is invalid, expired, or already used"}),
            status_code=403, media_type="application/json")
    try:
        data = await file.read()
        # Sanity-check it's actually a SQLite file before overwriting anything.
        if not data.startswith(b"SQLite format 3\x00"):
            return {"ok": False, "error": "that doesn't look like a Guppi database file"}
        # Safety copy of the CURRENT db before overwriting, in case the restore is wrong.
        try:
            pre = make_db_snapshot()
            if pre:
                os.replace(pre, DB_PATH + ".pre-restore")
        except Exception:
            pass
        # Write to a temp file first, verify it actually opens as a valid SQLite database,
        # THEN atomically swap it into place with os.replace (atomic on the same
        # filesystem). Writing directly over the live DB while connections are open risks
        # corruption and a half-written file if interrupted. os.replace is the safe swap.
        tmp_path = DB_PATH + ".incoming"
        with open(tmp_path, "wb") as f:
            f.write(data)
        try:
            _test = sqlite3.connect(tmp_path)
            _test.execute("PRAGMA schema_version;").fetchone()   # forces a real read
            _test.close()
        except Exception as e:
            os.remove(tmp_path)
            return {"ok": False, "error": f"uploaded file isn't a usable database: {e}"}
        os.replace(tmp_path, DB_PATH)   # atomic swap
        print(f"[restore] database restored from upload ({len(data)} bytes)")
        return {"ok": True, "restored_bytes": len(data),
                "note": ("Database restored (atomic swap). Previous DB saved as "
                         "guppi.db.pre-restore. Restart the app so all connections pick "
                         "up the restored file.")}
    except Exception as e:
        print(f"[restore] failed: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/set-webhook")
def set_webhook(secret: str = ""):
    """One-time setup: point Telegram at this server. Visit
    /set-webhook?secret=<TELEGRAM_SETUP_SECRET> once after deploying."""
    if not _secret_ok(secret):
        return {"ok": False, "error": "bad or missing secret"}
    payload = {"url": f"{BASE_URL}/telegram",
               "allowed_updates": ["message", "edited_message"]}
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    res = telegram_api("setWebhook", payload)
    return {"ok": res is not None, "result": res}


init_db()
try:
    _audit_guide_coverage()
except Exception as e:
    print(f"[guide] coverage audit failed: {e}")
start_scheduler()
