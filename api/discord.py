# api/discord.py
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import os, json, time
from datetime import datetime
from zoneinfo import ZoneInfo

# Discord signature verification
import nacl.signing
import nacl.exceptions

# Google Sheets API
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv
import requests
load_dotenv(r'../.env')
def notify_approver(name, from_date, to_date, reason):
    approver_id = os.environ.get("APPROVER_USER_ID", "")
    channel_id = os.environ.get("APPROVER_CHANNEL_ID", "")
    bot_token = os.environ.get("BOT_TOKEN", "")

    if not bot_token:
        print("❌ BOT_TOKEN missing in env")
        return

    message = (
        f"📩 **Leave Request from {name}**\n"
        f"🗓️ **From:** {from_date}\n"
        f"🗓️ **To:** {to_date}\n"
        f"💬 **Reason:** {reason}\n\n"
        f"Please review and respond accordingly."
    )

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }

    # Prefer sending to channel if defined, else DM the approver
    if channel_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    elif approver_id:
        # Create DM channel first
        dm_res = requests.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=headers,
            json={"recipient_id": approver_id},
        )
        dm_channel_id = dm_res.json().get("id")
        url = f"https://discord.com/api/v10/channels/{dm_channel_id}/messages"
    else:
        print("⚠️ No approver target configured")
        return

    try:
        requests.post(url, headers=headers, json={"content": message})
        print(f"✅ Notified approver ({approver_id or channel_id}) successfully.")
    except Exception as e:
        print(f"❌ Failed to notify approver: {e}")
  # fine for local; ignored in Vercel if no file
  # Load env vars from .env file for local testing
app = FastAPI(title="Discord Attendance → Google Sheets")

# ========= ENV VARS =========
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
SHEET_ID = os.environ.get("SHEET_ID", "")           # e.g. 1AbCd... (the spreadsheet ID)
SHEET_RANGE = os.environ.get("SHEET_RANGE", "Attendance!A:C")  # Tab & range to append into
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "")  # Full JSON (string)

# ========= HELPERS =========
def verify_signature(signature: str, timestamp: str, body: bytes) -> bool:
    if not DISCORD_PUBLIC_KEY:
        return False
    try:
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        verify_key.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except Exception:
        return False

def normalize_action(action_raw: str | None) -> str:
    if not action_raw:
        return "Login"
    a = action_raw.strip().lower()
    return "Login" if a == "login" else "Logout"

def get_ist_timestamp() -> str:
    # Asia/Kolkata, 24h format, e.g. 2025-10-08 10:05:12
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
def create_google_meet_event(summary: str, start_str: str, end_str: str, guests: list[str]):
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    service = build("calendar", "v3", credentials=creds)

    event = {
        'summary': summary,
        'start': {'dateTime': start_str, 'timeZone': 'Asia/Kolkata'},
        'end': {'dateTime': end_str, 'timeZone': 'Asia/Kolkata'},
        'attendees': [{'email': g} for g in guests],
        'conferenceData': {
            'createRequest': {
                'requestId': f"discord-meet-{int(time.time())}",
                'conferenceSolutionKey': {'type': 'hangoutsMeet'},
            }
        },
    }

    event = service.events().insert(
        calendarId='primary',
        body=event,
        conferenceDataVersion=1
    ).execute()

    return event.get("hangoutLink", "No Meet Link Found")

def get_service():
    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON env var missing")
    sa_info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def append_attendance_row(name: str, action: str) -> None:
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID env var missing")
    service = get_service()
    values = [[get_ist_timestamp(), name, action]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=SHEET_RANGE,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()
def append_leave_row(name: str, from_date: str, to_date: str, reason: str) -> None:
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID env var missing")
    service = get_service()
    values = [[get_ist_timestamp(), name, from_date, to_date, reason]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'Leave Requests'!A:E",  # ← Create a sheet/tab named exactly this
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def discord_response_message(content: str, ephemeral: bool = True) -> JSONResponse:
    data = {"content": content}
    # Ephemeral flag = 64
    if ephemeral:
        data["flags"] = 1 << 6
    return JSONResponse(
        {
            "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
            "data": data,
        }
    )

# ========= ROUTE =========
@app.post("/")
async def discord_interaction(
    request: Request,
    x_signature_ed25519: str = Header(..., alias="X-Signature-Ed25519"),
    x_signature_timestamp: str = Header(..., alias="X-Signature-Timestamp"),
):
    body = await request.body()

    # Verify Discord signature
    if not verify_signature(x_signature_ed25519, x_signature_timestamp, body):
        raise HTTPException(status_code=401, detail="invalid request signature")

    payload = await request.json()
    t = payload.get("type")

    # 1️⃣ PING → PONG
    if t == 1:
        return JSONResponse({"type": 1})

    # 2️⃣ Handle Application Command
    if t == 2:
        data = payload.get("data", {})
        cmd_name = data.get("name", "")

        # ====== 🟢 ATTENDANCE ======
        if cmd_name == "attendance":
            options = data.get("options", []) or []
            name_opt = None
            action_opt = None
            for opt in options:
                if opt.get("name") == "name":
                    name_opt = opt.get("value")
                if opt.get("name") == "action":
                    action_opt = opt.get("value")

            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            fallback_name = user.get("global_name") or user.get("username") or "Unknown"
            name = (name_opt or fallback_name).strip()
            action = normalize_action(action_opt)

            try:
                append_attendance_row(name=name, action=action)
            except Exception as e:
                return discord_response_message(
                    f"❌ Failed to record attendance. {type(e).__name__}: {str(e)}",
                    ephemeral=True,
                )

            return discord_response_message(
                f"✅ Recorded: **{name}** — **{action}** at **{get_ist_timestamp()} IST**",
                ephemeral=True,
            )

        # ====== 🟢 LEAVE REQUEST ======
        if cmd_name == "leaverequest":
            options = data.get("options", []) or []
            name = from_opt = to_opt = reason_opt = None

            for opt in options:
                if opt.get("name") == "name":
                    name = opt.get("value")
                elif opt.get("name") == "from":
                    from_opt = opt.get("value")
                elif opt.get("name") == "to":
                    to_opt = opt.get("value")
                elif opt.get("name") == "reason":
                    reason_opt = opt.get("value")

            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            fallback_name = user.get("global_name") or user.get("username") or "Unknown"
            name = (name or fallback_name).strip()

            try:
                append_leave_row(name=name, from_date=from_opt, to_date=to_opt, reason=reason_opt)
                notify_approver(name, from_opt, to_opt, reason_opt)
            except Exception as e:
                return discord_response_message(
                    f"❌ Failed to record leave. {type(e).__name__}: {str(e)}",
                    ephemeral=True,
                )

            return discord_response_message(
                f"✅ Leave request submitted by **{name}** from **{from_opt}** to **{to_opt}**.\nReason: {reason_opt}",
                ephemeral=True,
            )

        # ====== 🟢 SCHEDULE MEET ======
        if cmd_name == "schedulemeet":
            options = data.get("options", []) or []
            title, start_str, end_str, guests_str = None, None, None, None

            for opt in options:
                if opt.get("name") == "title":
                    title = opt.get("value")
                elif opt.get("name") == "start":
                    start_str = opt.get("value")
                elif opt.get("name") == "end":
                    end_str = opt.get("value")
                elif opt.get("name") == "guests":
                    guests_str = opt.get("value")

            if not title or not start_str or not end_str:
                return discord_response_message("❌ Missing required fields (title/start/end).", ephemeral=True)

            # Parse guest emails (comma separated)
            guests = [g.strip() for g in (guests_str or "").split(",") if g.strip()]

            # Create meet
            try:
                meet_link = create_google_meet_event(title, start_str, end_str, guests)
            except Exception as e:
                return discord_response_message(
                    f"❌ Failed to schedule meet. {type(e).__name__}: {str(e)}",
                    ephemeral=True,
                )

            return discord_response_message(
                f"✅ **Google Meet Scheduled!**\n📅 **{title}**\n🕒 {start_str} → {end_str}\n🔗 Meet Link: {meet_link}",
                ephemeral=False,
            )

        return discord_response_message("Unknown command.", ephemeral=True)

    return discord_response_message("Unsupported interaction type.", ephemeral=True)