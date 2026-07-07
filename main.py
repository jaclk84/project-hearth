# =============================================================================
#  PROJECT HEARTH  —  PHASE 1: "The Brain"
# =============================================================================
#
#  WHAT CHANGED FROM PHASE 0
#  -------------------------
#  Phase 0 echoed your text back ("You said: hello"). Phase 1 replaces that
#  single echo line with a real call to Claude. Now when someone texts the
#  number, their message is sent to Claude along with a "system prompt" (the
#  assistant's personality + family context), and Claude's reply is texted back.
#
#  Still NO calendar, email, memory, or proactive briefing yet — this phase is
#  just "a smart texter that knows your family from its instructions." Those
#  other capabilities come in later phases.
#
#  THE FLOW NOW
#  ------------
#  Phone -> Twilio -> [our /sms endpoint] -> ask Claude -> Claude's reply
#        -> hand reply back to Twilio -> Twilio texts it to the phone.
#
# =============================================================================

# --- Step 1: bring in the tools we need -------------------------------------
import os                                    # lets us read the secret API key
from fastapi import FastAPI, Request, Response
from twilio.twiml.messaging_response import MessagingResponse
from anthropic import Anthropic              # the official Claude library


# --- Step 2: create the server ----------------------------------------------
app = FastAPI()


# --- Step 3: connect to Claude ----------------------------------------------
# This creates our connection to Claude. It automatically reads the secret key
# from the environment variable named ANTHROPIC_API_KEY — the one you added in
# Railway. The key is NEVER written here in the code; the code just looks it up
# by name. That's why we could safely make this repo public.
claude = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Which Claude model to use. We start with Haiku — the fastest and cheapest —
# which is perfect while we're testing. To upgrade the assistant's quality
# later, change this ONE line to "claude-sonnet-4-6" and redeploy. Nothing else
# needs to change.
MODEL = "claude-haiku-4-5"


# --- Step 4: the assistant's personality and rules --------------------------
# This "system prompt" is the assistant's instructions. It's sent with every
# message and shapes how Claude behaves. Think of it as the job description you
# hand a new family assistant on day one. Edit this text to change the
# personality, tone, or family facts. (Later phases will load real family data
# here automatically; for now it's written by hand.)
SYSTEM_PROMPT = """You are the family assistant for the household, reachable by text message.

Your job is to help the family stay organized and answer questions in a warm, brief,
practical way. You are texting, so keep replies short and to the point — usually one
to three sentences, the way a helpful person would text. Avoid long paragraphs and
avoid bullet points unless truly needed.

Right now you do not yet have access to the family's calendar or email — those
capabilities are coming soon. If someone asks you to check the calendar, add an
event, or read email, kindly let them know that feature isn't connected yet, but
you can still help them think it through or answer general questions.

Be friendly and down-to-earth. If a child texts you, keep everything age-appropriate
and kind. Never share anything unsafe or inappropriate."""


# --- Step 5: a simple "is it alive?" page -----------------------------------
@app.get("/")
def home():
    return {"status": "Project Hearth Phase 1 (the brain) is running."}


# --- Step 6: THE TEXT-MESSAGE ENDPOINT --------------------------------------
# Same "/sms" mail slot as Phase 0. The difference is what happens in the middle:
# instead of echoing, we ask Claude for a real reply.
@app.post("/sms")
async def sms_reply(request: Request):

    # Read the incoming text's details from Twilio (same as Phase 0).
    form_data = await request.form()
    incoming_message = form_data.get("Body", "")
    sender_number = form_data.get("From", "unknown number")
    print(f"Received a text from {sender_number}: {incoming_message}")

    # --- Ask Claude for a reply ---------------------------------------------
    # We send Claude the system prompt (its instructions) plus the person's
    # message. "max_tokens" caps how long the reply can be — 300 is plenty for
    # a text message and keeps costs down. We wrap this in try/except so that
    # if the Claude call ever fails, the family still gets a friendly message
    # instead of silence.
    try:
        response = claude.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": incoming_message}
            ],
        )

        # Claude's reply comes back as a list of "content blocks." For a normal
        # text reply there's one block, and its .text is what we want. We loop
        # through and collect any text blocks, just to be safe.
        reply_text = ""
        for block in response.content:
            if block.type == "text":
                reply_text += block.text

        # Safety net: if for some reason the reply came back empty, say something.
        if not reply_text.strip():
            reply_text = "Sorry, I didn't catch that — can you say it another way?"

    except Exception as error:
        # If anything goes wrong talking to Claude, log it (visible in Railway
        # logs) and send a graceful fallback so the person isn't left hanging.
        print(f"Error calling Claude: {error}")
        reply_text = "Sorry, I'm having a little trouble thinking right now. Try again in a moment!"

    # --- Send the reply back via Twilio (same as Phase 0) -------------------
    twiml = MessagingResponse()
    twiml.message(reply_text)
    return Response(content=str(twiml), media_type="application/xml")


# =============================================================================
#  WHAT'S NEXT (not in this file yet):
#    - Phase 2: Google Calendar + Gmail tools (so it can actually check/add things)
#    - Phase 3: a database for memory, reminders, and shared lists
#    - Phase 4: the proactive morning briefing + urgent email alerts
# =============================================================================
