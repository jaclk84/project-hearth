# =============================================================================
#  PROJECT HEARTH  —  PHASE 0: "The Plumbing"
# =============================================================================
#
#  WHAT THIS FILE IS
#  -----------------
#  This is the entire server for Phase 0. Its only job: when someone texts our
#  Twilio phone number, text back an echo of what they said. No AI yet. This
#  proves the "pipe" works end to end before we make anything smart.
#
#  HOW TO READ THIS FILE
#  ---------------------
#  You do NOT need to understand every line to deploy it. But it's written so a
#  curious non-programmer CAN follow it. Comments (lines starting with #) are
#  notes for humans — the computer ignores them.
#
#  THE FLOW, IN ONE BREATH
#  -----------------------
#  Phone -> Twilio -> [this server's /sms address] -> we build a reply
#        -> hand the reply back to Twilio -> Twilio texts it to the phone.
#
# =============================================================================

# --- Step 1: bring in the tools we need -------------------------------------
# "import" means "load a helper library so we can use its features."
# FastAPI is the framework that lets us build a web server quickly.
from fastapi import FastAPI, Request, Response

# MessagingResponse is a helper from Twilio's library. It builds the little
# TwiML (XML) reply that Twilio expects. We don't have to write raw XML by hand.
from twilio.twiml.messaging_response import MessagingResponse


# --- Step 2: create the server ----------------------------------------------
# This one line creates our web application. Everything below attaches to it.
app = FastAPI()


# --- Step 3: a simple "is it alive?" page -----------------------------------
# When you visit the server's main web address in a browser, this responds.
# It's just a health check so you can confirm the server is running at all,
# separate from the texting feature. If you see this text in your browser,
# the server is live.
@app.get("/")
def home():
    return {"status": "Project Hearth Phase 0 is running."}


# --- Step 4: THE IMPORTANT PART — the text-message endpoint -----------------
# This is the "mail slot" Twilio drops incoming texts into. We named it "/sms".
# When we set up Twilio, we tell it: "for any incoming text, send the details to
# https://our-server-address/sms". Twilio then POSTs the message here.
#
# "async def" and "await" below are just how modern Python waits for data to
# arrive without freezing — you can safely ignore the keywords for now.
@app.post("/sms")
async def sms_reply(request: Request):

    # Twilio sends us the text's details as form data. We read it out.
    form_data = await request.form()

    # "Body" is the actual text the person typed. "From" is their phone number.
    # These field names ("Body", "From") are defined by Twilio, not by us — we
    # have to use exactly these names because that's what Twilio sends.
    incoming_message = form_data.get("Body", "")
    sender_number = form_data.get("From", "unknown number")

    # (Handy for debugging: this prints to the server's logs so we can watch
    #  texts arrive in real time in the Railway dashboard.)
    print(f"Received a text from {sender_number}: {incoming_message}")

    # --- Build the reply ----------------------------------------------------
    # In Phase 0 the reply is dead simple: echo it back. Later phases replace
    # this single line with a call to Claude.
    reply_text = f"You said: {incoming_message}"

    # Wrap our reply in the TwiML format Twilio expects.
    twiml = MessagingResponse()
    twiml.message(reply_text)

    # Hand the TwiML back to Twilio. Twilio reads it and sends the SMS for us.
    # The "media_type" tells Twilio we're speaking XML.
    return Response(content=str(twiml), media_type="application/xml")


# =============================================================================
#  THAT'S THE WHOLE PHASE 0 SERVER.
#
#  Notice what is NOT here yet, on purpose:
#    - No Claude / AI            (Phase 1)
#    - No Google Calendar        (Phase 2)
#    - No memory / database      (Phase 3)
#    - No morning briefing       (Phase 4)
#
#  We are only proving the pipe. Keep it boring. Boring = debuggable.
# =============================================================================
