# =============================================================================
#  PROJECT HEARTH  —  PHASE 2: "Google Integration" (Calendar + Gmail + tools)
#  The assistant is named GUPPI.
# =============================================================================
#
#  WHAT CHANGED FROM PHASE 1
#  -------------------------
#  Phase 1 was a smart texter with no real-world access. Phase 2 adds:
#    1. A DATABASE (SQLite on the persistent /app/data volume) to remember the
#       Google login token across restarts.
#    2. A "connect your Google account" flow (visit /connect once in a browser).
#    3. Four TOOLS Guppi can use: check calendar, add calendar event,
#       search email, and web search.
#    4. Guppi's personality: calm, efficient, brief. No emoji.
#
#  Now texting "what's Thursday look like?" makes Guppi actually check the
#  family calendar and answer.
#
# =============================================================================

import os
import json
import sqlite3
import datetime
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from twilio.twiml.messaging_response import MessagingResponse
from anthropic import Anthropic

# Google libraries
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# =============================================================================
#  CONFIGURATION
# =============================================================================
app = FastAPI()

claude = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-haiku-4-5"   # cheap for testing; change to claude-sonnet-4-6 later

# The persistent volume is mounted at /app/data (set up in Railway). The database
# file lives there so it survives restarts and redeploys.
DB_PATH = "/app/data/guppi.db"

# Google OAuth settings. Client ID/secret come from Railway env vars (never in code).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

# The public address of this server + the redirect path we registered in Google.
BASE_URL = "https://web-production-5fa1fd.up.railway.app"
REDIRECT_URI = f"{BASE_URL}/oauth/callback"

# The permissions we ask Google for (must match what we set on the consent screen).
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Guppi's personality and rules — sent with every message.
SYSTEM_PROMPT = """You are Guppi, the family's household assistant, reachable by text message.

Personality: calm and efficient. You are brief, clear, and competent — never chatty,
bubbly, or wordy. You are texting, so keep replies short, usually one to three
sentences. Do not use emoji. Do not use bullet points unless truly necessary.

You can help the family with their shared Google Calendar (checking the schedule and
adding events), searching their Gmail, answering general questions, and looking things
up on the web. Use your tools when they would help answer accurately. When you add a
calendar event or make a change, briefly confirm what you did.

If a child texts you, keep everything age-appropriate and kind. Never share anything
unsafe or inappropriate. If you are ever unsure about a request, ask a short
clarifying question rather than guessing."""


# =============================================================================
#  DATABASE — stores the Google token so we remember the connection
# =============================================================================
def init_db():
    """Create the database and its tables if they don't exist yet. Safe to call
    every startup — it only creates things that are missing."""
    os.makedirs("/app/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS google_token (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token_json TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_google_token(creds):
    """Save the Google credentials (as JSON text) so they survive restarts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO google_token (id, token_json) VALUES (1, ?)",
        (creds.to_json(),),
    )
    conn.commit()
    conn.close()


def load_google_token():
    """Load saved Google credentials, or None if the account isn't connected yet.
    Automatically refreshes the token if it has expired."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT token_json FROM google_token WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(row[0]), SCOPES)
    # If expired but we have a refresh token, refresh and re-save.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            save_google_token(creds)
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return None
    return creds


# =============================================================================
#  GOOGLE CONNECTION FLOW  (visit /connect once in a browser)
# =============================================================================
def make_flow():
    """Builds the Google OAuth 'flow' object using our client credentials."""
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)


@app.get("/connect")
def connect():
    """Step 1 of connecting: send the user to Google's approval screen."""
    flow = make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",      # so we get a refresh token (stays connected)
        prompt="consent",           # force the consent screen so a refresh token is issued
        include_granted_scopes="true",
    )
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    """Step 2: Google sends the user back here with an approval code. We exchange
    it for a token and save it to the database."""
    flow = make_flow()
    # Rebuild the full URL Google redirected to (it carries the approval code).
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials
    save_google_token(creds)
    return HTMLResponse(
        "<h2>Guppi is connected to your Google account.</h2>"
        "<p>You can close this window and text Guppi now.</p>"
    )


# =============================================================================
#  THE TOOLS  —  what Guppi can actually DO
# =============================================================================
def get_calendar_service():
    creds = load_google_token()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def get_gmail_service():
    creds = load_google_token()
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def tool_check_calendar(days_ahead=7):
    """Read upcoming events for the next N days from the primary calendar."""
    service = get_calendar_service()
    if not service:
        return "The Google account isn't connected yet."
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    later = now + datetime.timedelta(days=days_ahead)
    result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=later.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=20,
    ).execute()
    events = result.get("items", [])
    if not events:
        return f"No events in the next {days_ahead} days."
    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        lines.append(f"{start}: {e.get('summary', '(no title)')}")
    return "\n".join(lines)


def tool_add_calendar_event(summary, start_iso, end_iso):
    """Add an event to the primary calendar. Times are ISO strings."""
    service = get_calendar_service()
    if not service:
        return "The Google account isn't connected yet."
    event = {
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    created = service.events().insert(calendarId="primary", body=event).execute()
    return f"Added '{summary}' on {start_iso}."


def tool_search_email(query, max_results=5):
    """Search Gmail and return short summaries of matching messages."""
    service = get_gmail_service()
    if not service:
        return "The Google account isn't connected yet."
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = results.get("messages", [])
    if not messages:
        return "No matching emails found."
    summaries = []
    for m in messages:
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        snippet = msg.get("snippet", "")
        summaries.append(
            f"From {headers.get('From','?')} | {headers.get('Subject','(no subject)')}: {snippet[:120]}"
        )
    return "\n".join(summaries)


# Tool definitions Claude sees. Claude decides when to call these.
TOOLS = [
    {
        "name": "check_calendar",
        "description": "Check upcoming events on the family's Google Calendar. Use when asked about the schedule, what's coming up, or availability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "How many days ahead to look (default 7)."}
            },
        },
    },
    {
        "name": "add_calendar_event",
        "description": "Add an event to the family's Google Calendar. Provide start and end as ISO 8601 datetimes with timezone offset, e.g. 2026-07-12T10:00:00-04:00.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title."},
                "start_iso": {"type": "string", "description": "Start datetime, ISO 8601 with offset."},
                "end_iso": {"type": "string", "description": "End datetime, ISO 8601 with offset."},
            },
            "required": ["summary", "start_iso", "end_iso"],
        },
    },
    {
        "name": "search_email",
        "description": "Search the family's Gmail. Use when asked to find, check, or summarize an email. Query uses Gmail search syntax (e.g. 'from:school newer_than:7d').",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query."}
            },
            "required": ["query"],
        },
    },
    {
        "type": "web_search_20250305",
        "name": "web_search",
    },
]


def run_tool(name, tool_input):
    """Execute a tool by name and return its text result."""
    if name == "check_calendar":
        return tool_check_calendar(tool_input.get("days_ahead", 7))
    if name == "add_calendar_event":
        return tool_add_calendar_event(
            tool_input["summary"], tool_input["start_iso"], tool_input["end_iso"]
        )
    if name == "search_email":
        return tool_search_email(tool_input["query"])
    return "Unknown tool."


# =============================================================================
#  THE MESSAGE LOOP  —  now with tools
# =============================================================================
def ask_guppi(user_message):
    """Send the message to Claude with tools available. If Claude calls a tool,
    run it, give the result back, and let Claude write the final reply. This loop
    repeats until Claude is done using tools."""
    # Give Claude today's date so it can reason about "Thursday", "tomorrow", etc.
    today = datetime.datetime.now().strftime("%A, %B %d, %Y")
    messages = [
        {"role": "user", "content": f"(Today is {today}.)\n\n{user_message}"}
    ]

    # Loop: let Claude think, call tools, and think again until it gives a text reply.
    for _ in range(5):  # safety cap on tool rounds
        response = claude.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # If Claude wants to use one or more tools, run them and continue the loop.
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Otherwise Claude is done — collect its text reply.
        reply = ""
        for block in response.content:
            if block.type == "text":
                reply += block.text
        return reply.strip() or "Sorry, I didn't catch that — can you say it another way?"

    return "Sorry, that took too many steps. Can you rephrase?"


@app.get("/")
def home():
    return {"status": "Project Hearth Phase 2 (Guppi + Google) is running."}


@app.post("/sms")
async def sms_reply(request: Request):
    form_data = await request.form()
    incoming_message = form_data.get("Body", "")
    sender_number = form_data.get("From", "unknown number")
    print(f"Received a text from {sender_number}: {incoming_message}")

    try:
        reply_text = ask_guppi(incoming_message)
    except Exception as error:
        print(f"Error in ask_guppi: {error}")
        reply_text = "Sorry, I'm having a little trouble right now. Try again in a moment."

    twiml = MessagingResponse()
    twiml.message(reply_text)
    return Response(content=str(twiml), media_type="application/xml")


# Create the database tables on startup.
init_db()
