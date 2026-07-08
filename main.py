# =============================================================================
#  PROJECT HEARTH  —  PHASE 3: Memory, Reminders, Lists, and People
#  The assistant is GUPPI.
# =============================================================================
#
#  WHAT CHANGED FROM PHASE 2
#  -------------------------
#  Phase 2 gave Guppi calendar + email. But it had amnesia: every text arrived with
#  no idea who was texting or anything about the family. Phase 3 adds:
#
#    1. PEOPLE + ROLES. Guppi knows who's texting (by phone number) and what they're
#       allowed to do. Permissions are enforced in CODE, not left to the prompt.
#    2. MEMORY. Durable facts about the family, with strict rules about what may be
#       saved automatically vs. only on request.
#    3. REMINDERS. Stored now; they actually FIRE in Phase 4 (needs the scheduler).
#    4. SHARED LISTS. Grocery/to-do lists anyone can add to.
#    5. Memory is INSPECTABLE and DELETABLE ("what do you remember?" / "forget that").
#
#  THE PERMISSION MODEL (enforced in code):
#    adult     (Jason, Kim)          calendar read+write, email yes, full memory
#    caregiver (Breanna)             calendar read+write, email NO,  logistics memory
#    child     (Lillian, Charlotte)  calendar READ ONLY,  email NO,  names/logistics only
#    unknown   (any other number)    calendar read only,  email NO,  no memory; asks who
#
#  An unknown number gets CHILD-level caution by default. This "fails safe" — the
#  cautious path is the default, never the permissive one. Nobody can text
#  "I'm Jason" and gain email access: adult numbers come from private environment
#  variables, never from a text message.
#
# =============================================================================

import os
import re
import json
import sqlite3
import datetime
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from twilio.twiml.messaging_response import MessagingResponse
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
    "https://www.googleapis.com/auth/gmail.readonly",
]

# ---- Known adults, loaded privately from Railway env vars -------------------
# Set these in Railway (NOT in this code, NOT in GitHub):
#   ADULT_PHONE_1 = +1XXXXXXXXXX   (Jason)
#   ADULT_PHONE_2 = +1XXXXXXXXXX   (Kim)
# Anyone whose number is not in this list can never be an adult, no matter what
# they claim in a text. This is the security boundary.
ADULT_PHONES = {
    p.strip() for p in [
        os.environ.get("ADULT_PHONE_1", ""),
        os.environ.get("ADULT_PHONE_2", ""),
    ] if p.strip()
}

# Family roster: names seeded here, phone numbers linked later via text setup.
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
    "unknown":   {"calendar_read": True, "calendar_write": False, "email": False},
}


# =============================================================================
#  DATABASE
# =============================================================================
def init_db():
    os.makedirs("/app/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS google_token (
        id INTEGER PRIMARY KEY CHECK (id = 1), token_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL,
        phone TEXT UNIQUE)""")
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
        for_phone TEXT,
        created_by TEXT,
        fired INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS list_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_name TEXT NOT NULL,
        item TEXT NOT NULL,
        added_by TEXT,
        created_at TEXT NOT NULL)""")
    conn.commit()
    for name, role in SEEDED_PEOPLE:
        conn.execute("INSERT OR IGNORE INTO people (name, role) VALUES (?, ?)", (name, role))
    conn.commit()
    conn.close()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
#  WHO IS TEXTING?  (identity + permissions)
# =============================================================================
def identify_sender(phone):
    """Return (name, role). A number in ADULT_PHONES is always an adult. Anyone
    unrecognized is 'unknown' and gets child-level caution. Nobody can upgrade
    their own role by texting."""
    if phone and phone in ADULT_PHONES:
        conn = db()
        row = conn.execute(
            "SELECT name FROM people WHERE phone = ? AND role = 'adult'", (phone,)).fetchone()
        conn.close()
        return (row["name"] if row else "a parent"), "adult"

    conn = db()
    row = conn.execute("SELECT name, role FROM people WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    if row:
        # Safety: only ADULT_PHONES grants adult, even if the DB says otherwise.
        role = row["role"] if row["role"] != "adult" else "caregiver"
        return row["name"], role
    return None, "unknown"


def link_phone(name, phone, requester_role):
    """Link a phone to a seeded person. Parents only. Cannot link into an adult slot."""
    if requester_role != "adult":
        return "Only a parent can set up who's who."
    conn = db()
    row = conn.execute("SELECT role FROM people WHERE name = ?", (name,)).fetchone()
    if not row:
        conn.close()
        return f"I don't have anyone named {name} on the family list."
    if row["role"] == "adult":
        conn.close()
        return f"{name} is a parent - their number is configured privately, not by text."
    conn.execute("UPDATE people SET phone = ? WHERE name = ?", (phone, name))
    conn.commit()
    conn.close()
    return f"Linked {phone} to {name}."


# =============================================================================
#  GOOGLE  (unchanged from Phase 2)
# =============================================================================
def save_google_token(creds):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO google_token (id, token_json) VALUES (1, ?)",
                 (creds.to_json(),))
    conn.commit()
    conn.close()


def load_google_token():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT token_json FROM google_token WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(row[0]), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            save_google_token(creds)
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return None
    return creds


def make_flow():
    client_config = {"web": {
        "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI]}}
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)


@app.get("/connect")
def connect():
    flow = make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    flow = make_flow()
    # Railway serves HTTPS at its edge but forwards internally as HTTP; the OAuth
    # library refuses non-HTTPS. Rebuild as https (it IS secure end to end).
    callback_url = str(request.url).replace("http://", "https://", 1)
    flow.fetch_token(authorization_response=callback_url)
    save_google_token(flow.credentials)
    return HTMLResponse("<h2>Guppi is connected to your Google account.</h2>"
                        "<p>You can close this window and text Guppi now.</p>")


def get_calendar_service():
    creds = load_google_token()
    return build("calendar", "v3", credentials=creds) if creds else None


def get_gmail_service():
    creds = load_google_token()
    return build("gmail", "v1", credentials=creds) if creds else None


# =============================================================================
#  TOOL IMPLEMENTATIONS
# =============================================================================
def tool_check_calendar(days_ahead=7):
    service = get_calendar_service()
    if not service:
        return "The Google account isn't connected yet."
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    later = now + datetime.timedelta(days=days_ahead)
    result = service.events().list(
        calendarId="primary", timeMin=now.isoformat(), timeMax=later.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=20).execute()
    events = result.get("items", [])
    if not events:
        return f"No events in the next {days_ahead} days."
    return "\n".join(
        f"{e['start'].get('dateTime', e['start'].get('date'))}: {e.get('summary','(no title)')}"
        for e in events)


def tool_add_calendar_event(summary, start_iso, end_iso):
    service = get_calendar_service()
    if not service:
        return "The Google account isn't connected yet."
    service.events().insert(calendarId="primary", body={
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso}}).execute()
    return f"Added '{summary}' on {start_iso}."


def tool_search_email(query, max_results=5):
    """Auto-widens: narrow queries often return nothing. See BUILD_LOG Trap 17."""
    service = get_gmail_service()
    if not service:
        return "The Google account isn't connected yet."
    attempts = [query]
    no_date = re.sub(r"\b(newer_than|older_than|after|before):\S+", "", query).strip()
    if no_date and no_date != query:
        attempts.append(no_date)
    loose = re.sub(r"\bfrom:(\S+)", r"\1", no_date or query).strip()
    if loose and loose not in attempts:
        attempts.append(loose)

    for attempt in attempts:
        print(f"[search_email] trying query: {attempt!r}")
        res = service.users().messages().list(
            userId="me", q=attempt, maxResults=max_results).execute()
        messages = res.get("messages", [])
        if messages:
            out = []
            for m in messages:
                msg = service.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]).execute()
                h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
                out.append(f"From {h.get('From','?')} | {h.get('Subject','(no subject)')}: "
                           f"{msg.get('snippet','')[:120]}")
            print(f"[search_email] found {len(out)} with {attempt!r}")
            return "\n".join(out)
    return "No matching emails found (searched broadly)."


# ---- Memory -----------------------------------------------------------------
def tool_remember(fact, about, added_by):
    conn = db()
    conn.execute("INSERT INTO memories (fact, about, added_by, created_at) VALUES (?,?,?,?)",
                 (fact, about, added_by, datetime.datetime.now().isoformat()))
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
def tool_add_reminder(text, due_iso, for_phone, created_by):
    conn = db()
    conn.execute("INSERT INTO reminders (text, due_at, for_phone, created_by) VALUES (?,?,?,?)",
                 (text, due_iso, for_phone, created_by))
    conn.commit()
    conn.close()
    return f"Reminder set: '{text}' for {due_iso}."


def tool_list_reminders():
    conn = db()
    rows = conn.execute(
        "SELECT id, text, due_at FROM reminders WHERE fired = 0 ORDER BY due_at").fetchall()
    conn.close()
    if not rows:
        return "No upcoming reminders."
    return "\n".join(f"[{r['id']}] {r['due_at']}: {r['text']}" for r in rows)


# ---- Shared lists -----------------------------------------------------------
def tool_add_to_list(list_name, item, added_by):
    conn = db()
    conn.execute("INSERT INTO list_items (list_name, item, added_by, created_at) VALUES (?,?,?,?)",
                 (list_name.lower(), item, added_by, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return f"Added '{item}' to the {list_name} list."


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


def tool_remove_from_list(item_id):
    conn = db()
    cur = conn.execute("DELETE FROM list_items WHERE id = ?", (item_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return "Removed." if deleted else "I couldn't find that item."


# =============================================================================
#  TOOL DEFINITIONS  (filtered per role before Claude ever sees them)
# =============================================================================
def tools_for_role(role):
    """Return only the tools this person may use. Claude never even SEES a tool the
    sender isn't permitted to call. Permissions live in code, not in the prompt."""
    perms = PERMISSIONS.get(role, PERMISSIONS["unknown"])
    tools = []

    if perms["calendar_read"]:
        tools.append({
            "name": "check_calendar",
            "description": "Check upcoming events on the family's Google Calendar.",
            "input_schema": {"type": "object", "properties": {
                "days_ahead": {"type": "integer", "description": "Days ahead (default 7)."}}}})

    if perms["calendar_write"]:
        tools.append({
            "name": "add_calendar_event",
            "description": ("Add an event to the family's Google Calendar. Times are "
                            "ISO 8601 with timezone offset, e.g. 2026-07-12T10:00:00-04:00."),
            "input_schema": {"type": "object", "properties": {
                "summary": {"type": "string"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"}},
                "required": ["summary", "start_iso", "end_iso"]}})

    if perms["email"]:
        tools.append({
            "name": "search_email",
            "description": "Search the family's Gmail. Uses Gmail search syntax.",
            "input_schema": {"type": "object", "properties": {
                "query": {"type": "string"}}, "required": ["query"]}})

    tools += [
        {"name": "remember",
         "description": "Save a durable fact about the family. Follow the memory rules strictly.",
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
        {"name": "add_reminder",
         "description": "Store a reminder. due_iso is ISO 8601 with timezone offset.",
         "input_schema": {"type": "object", "properties": {
             "text": {"type": "string"}, "due_iso": {"type": "string"}},
             "required": ["text", "due_iso"]}},
        {"name": "list_reminders",
         "description": "Show upcoming reminders.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "add_to_list",
         "description": "Add an item to a shared list, e.g. 'grocery' or 'todo'.",
         "input_schema": {"type": "object", "properties": {
             "list_name": {"type": "string"}, "item": {"type": "string"}},
             "required": ["list_name", "item"]}},
        {"name": "show_list",
         "description": "Show a shared list.",
         "input_schema": {"type": "object", "properties": {
             "list_name": {"type": "string"}}, "required": ["list_name"]}},
        {"name": "remove_from_list",
         "description": "Remove an item from a list by its id (ids come from show_list).",
         "input_schema": {"type": "object", "properties": {
             "item_id": {"type": "integer"}}, "required": ["item_id"]}},
        {"type": "web_search_20250305", "name": "web_search"},
    ]

    if role == "adult":
        tools.append({
            "name": "link_person_phone",
            "description": ("Link a phone number to a family member during setup. "
                            "Parents only. Cannot be used for parents themselves."),
            "input_schema": {"type": "object", "properties": {
                "name": {"type": "string"}, "phone": {"type": "string"}},
                "required": ["name", "phone"]}})
    return tools


def run_tool(name, tool_input, sender_name, sender_role, sender_phone):
    print(f"[tool] {sender_name or 'unknown'} ({sender_role}) called '{name}': {tool_input}")
    perms = PERMISSIONS.get(sender_role, PERMISSIONS["unknown"])

    # Belt-and-braces: re-check permission at execution time, not only at listing time.
    if name == "check_calendar":
        if not perms["calendar_read"]:
            return "You don't have calendar access."
        return tool_check_calendar(tool_input.get("days_ahead", 7))

    if name == "add_calendar_event":
        if not perms["calendar_write"]:
            return "Only a parent or caregiver can add calendar events."
        return tool_add_calendar_event(tool_input["summary"], tool_input["start_iso"],
                                       tool_input["end_iso"])

    if name == "search_email":
        if not perms["email"]:
            return "You don't have email access."
        return tool_search_email(tool_input["query"])

    if name == "remember":
        return tool_remember(tool_input["fact"], tool_input.get("about"), sender_name)
    if name == "recall":
        return tool_recall(tool_input.get("about"))
    if name == "forget":
        return tool_forget(tool_input["memory_id"])

    if name == "add_reminder":
        return tool_add_reminder(tool_input["text"], tool_input["due_iso"],
                                 sender_phone, sender_name)
    if name == "list_reminders":
        return tool_list_reminders()

    if name == "add_to_list":
        return tool_add_to_list(tool_input["list_name"], tool_input["item"], sender_name)
    if name == "show_list":
        return tool_show_list(tool_input["list_name"])
    if name == "remove_from_list":
        return tool_remove_from_list(tool_input["item_id"])

    if name == "link_person_phone":
        return link_phone(tool_input["name"], tool_input["phone"], sender_role)

    return "Unknown tool."


# =============================================================================
#  GUPPI'S INSTRUCTIONS  (personality + memory rules, tailored to who is texting)
# =============================================================================
def build_system_prompt(sender_name, sender_role):
    if sender_name:
        who = f"You are texting with {sender_name}."
    else:
        who = ("You do not recognize this phone number. Politely ask who it is. "
               "Do not save anything about them.")

    if sender_role == "adult":
        memory_rules = """You may automatically save durable, useful facts this person states:
identities and relationships, recurring commitments, stable logistics, explicit
preferences, and standing constraints such as allergies.

Never automatically save: anything emotionally sensitive (arguments, worries, someone
having a hard time), health or medical details, anything about a family member's
behavior or performance, financial specifics, one-off transient facts, or anything you
inferred rather than were plainly told. For those you may ASK: "Want me to remember that?"

Announce it briefly when you save a bigger fact - a recurring commitment, standing
constraint, or preference ("Noted - Lillian has lacrosse Tuesdays."). Stay quiet when
saving small things like linking a name to a phone number."""

    elif sender_role == "caregiver":
        memory_rules = """This person helps care for the children. You may save only logistics:
schedules, locations, activities, pickup times. Never save anything personal about them,
and never save anything about the children's feelings, health, behavior, or family
matters. They do not have access to the family's email."""

    elif sender_role == "child":
        memory_rules = """You are texting with a child. Keep everything age-appropriate, warm,
and simple.

You may save ONLY names and logistics they tell you: their activities, practice times,
school schedule, where they need to be. NEVER save anything about their feelings, health,
worries, behavior, grades, friendships, or family conflicts. If a child tells you
something upsetting, be kind and helpful and gently encourage them to talk to a parent -
but do not save it. If you are unsure whether something is safe to save, do not save it."""

    else:
        memory_rules = """You do not know who this is. Do not save anything at all. Be polite,
help only with general questions, and ask them to identify themselves."""

    return f"""You are Guppi, the family's household assistant, reachable by text message.

Personality: calm and efficient. You are brief, clear, and competent - never chatty,
bubbly, or wordy. You are texting, so keep replies short, usually one to three
sentences. Do not use emoji. Avoid bullet points unless truly necessary.

{who}

You help with the family's shared Google Calendar, their email, reminders, shared lists,
remembering useful facts, and looking things up. Use your tools whenever they help.

CRITICAL: never answer a question about the calendar, email, reminders, or lists from
memory or assumption. You do not know what is there unless you call the tool and read the
result. Always call the tool first. Never say "you have no emails" or "nothing is
scheduled" unless a tool actually returned that.

When searching email, build broad queries. Senders rarely match a plain name - mail
"from Google" actually comes from addresses like no-reply@accounts.google.com.

MEMORY RULES:
{memory_rules}

Anyone may ask what you remember, and may ask you to forget something. Always honor that.

If someone asks for something they are not permitted to do, say so briefly and kindly,
and do not explain how to get around it."""


# =============================================================================
#  THE MESSAGE LOOP
# =============================================================================
def ask_guppi(user_message, sender_phone):
    sender_name, sender_role = identify_sender(sender_phone)
    print(f"[guppi] message from {sender_name or 'UNKNOWN'} ({sender_role}) {sender_phone}")

    today = datetime.datetime.now().strftime("%A, %B %d, %Y")
    messages = [{"role": "user", "content": f"(Today is {today}.)\n\n{user_message}"}]
    tools = tools_for_role(sender_role)
    system = build_system_prompt(sender_name, sender_role)

    for _ in range(6):
        response = claude.messages.create(
            model=MODEL, max_tokens=500, system=system, tools=tools, messages=messages)

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    out = run_tool(block.name, block.input, sender_name, sender_role,
                                   sender_phone)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": out})
            messages.append({"role": "user", "content": results})
            continue

        reply = "".join(b.text for b in response.content if b.type == "text")
        return reply.strip() or "Sorry, I didn't catch that - can you say it another way?"

    return "Sorry, that took too many steps. Can you rephrase?"


@app.get("/")
def home():
    return {"status": "Project Hearth Phase 3 (Guppi: memory, reminders, lists) is running."}


@app.post("/sms")
async def sms_reply(request: Request):
    form = await request.form()
    incoming = form.get("Body", "")
    sender = form.get("From", "")
    print(f"Received a text from {sender}: {incoming}")
    try:
        reply_text = ask_guppi(incoming, sender)
    except Exception as e:
        print(f"Error in ask_guppi: {e}")
        reply_text = "Sorry, I'm having a little trouble right now. Try again in a moment."
    twiml = MessagingResponse()
    twiml.message(reply_text)
    return Response(content=str(twiml), media_type="application/xml")


init_db()
