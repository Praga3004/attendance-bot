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
import requests

def notify_approver(name, from_date, to_date, reason, fallback_channel_id=None):
    approver_id = os.environ.get("APPROVER_USER_ID", "").strip()
    channel_id  = os.environ.get("APPROVER_CHANNEL_ID", "").strip()
    bot_token   = os.environ.get("BOT_TOKEN", "").strip()

    if not bot_token:
        print("❌ BOT_TOKEN missing in env")
        return False

    msg = (
        f"📩 **Leave Request from {name}**\n"
        f"🗓️ **From:** {from_date}\n"
        f"🗓️ **To:** {to_date}\n"
        f"💬 **Reason:** {reason}\n"
        f"Please review and respond accordingly."
    )

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://example.com, 1.0)"
    }
    components = [
        {
            "type": 1,  # action row
            "components": [
                {
                    "type": 2, "style": 3, "label": "Approve",
                    "custom_id": "leave_approve"  # handled in interaction type 3
                },
                {
                    "type": 2, "style": 4, "label": "Reject",
                    "custom_id": "leave_reject"
                }
            ]
        }
    ]

    def post_message(to_channel_id: str):
        url = f"https://discord.com/api/v10/channels/{to_channel_id}/messages"
        r = requests.post(
            url, headers=headers,
            json={"content": msg, "components": components},
            timeout=15
        )
        print(f"POST {url} -> {r.status_code} {r.text}")
        r.raise_for_status()
        return True

    try:
        if channel_id:
            return post_message(channel_id)

        if approver_id:
            # Create DM then post
            dm_res = requests.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers, json={"recipient_id": approver_id}, timeout=15
            )
            print(f"Create DM -> {dm_res.status_code} {dm_res.text}")
            dm_res.raise_for_status()
            dm_channel_id = dm_res.json().get("id")
            return post_message(dm_channel_id)

        if fallback_channel_id:
            return post_message(fallback_channel_id)

        print("⚠️ No target configured for approver notification.")
        return False

    except requests.HTTPError as e:
        print(f"❌ Discord API error: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error notifying approver: {e}")
        return False

def append_leave_decision_row(name: str, from_date: str, to_date: str, reason: str,
                              decision: str, reviewer: str) -> None:
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID env var missing")
    service = get_service()
    values = [[get_ist_timestamp(), name, from_date, to_date, reason, decision, reviewer]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'Leave Decisions'!A:G",  # Create a tab named exactly this
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()
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
    x_signature_ed25519: str = Header(None, alias="X-Signature-Ed25519"),
    x_signature_timestamp: str = Header(None, alias="X-Signature-Timestamp"),
):
    body: bytes = await request.body()

    # Some envs change header casing; try lowercase if missing
    if not x_signature_ed25519 or not x_signature_timestamp:
        h = request.headers
        x_signature_ed25519 = x_signature_ed25519 or h.get("x-signature-ed25519")
        x_signature_timestamp = x_signature_timestamp or h.get("x-signature-timestamp")

    # Verify Discord signature with RAW body
    if not verify_signature(x_signature_ed25519 or "", x_signature_timestamp or "", body):
        raise HTTPException(status_code=401, detail="invalid request signature")

    payload = await request.json()
    t = payload.get("type")

    # 1) PING -> PONG
    if t == 1:
        return JSONResponse({"type": 1})

    # 2) APPLICATION_COMMAND
    if t == 2:
        data = payload.get("data", {}) or {}
        cmd_name = data.get("name", "")

        # ----- ATTENDANCE -----
        if cmd_name == "attendance":
            options = data.get("options", []) or []
            action_opt = None
            for opt in options:
                if opt.get("name") == "action":
                    action_opt = opt.get("value")

            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            name = (user.get("global_name") or user.get("username") or "Unknown").strip()
            action = normalize_action(action_opt)

            try:
                append_attendance_row(name=name, action=action)
            except Exception as e:
                return discord_response_message(
                    f"❌ Failed to record attendance. {type(e).__name__}: {e}",
                    ephemeral=True,
                )
            return discord_response_message(
                f"✅ Recorded: **{name}** — **{action}** at **{get_ist_timestamp()} IST**",
                ephemeral=True,
            )

        # ----- LEAVE REQUEST -----
        if cmd_name == "leaverequest":
            options = data.get("options", []) or []
            name = from_opt = to_opt = reason_opt = None

            for opt in options:
                n = opt.get("name")
                if n == "name":
                    name = opt.get("value")
                elif n == "from":
                    from_opt = opt.get("value")
                elif n == "to":
                    to_opt = opt.get("value")
                elif n == "reason":
                    reason_opt = opt.get("value")

            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            fallback_name = user.get("global_name") or user.get("username") or "Unknown"
            name = (name or fallback_name).strip()

            try:
                append_leave_row(name=name, from_date=from_opt, to_date=to_opt, reason=reason_opt)
                channel_id_from_payload = payload.get("channel_id")
                notify_approver(name, from_opt, to_opt, reason_opt, fallback_channel_id=channel_id_from_payload)
            except Exception as e:
                return discord_response_message(
                    f"❌ Failed to record leave. {type(e).__name__}: {e}",
                    ephemeral=True,
                )

            return discord_response_message(
                f"✅ Leave request submitted by **{name}** from **{from_opt}** to **{to_opt}**.\nReason: {reason_opt}",
                ephemeral=True,
            )

        # ----- SCHEDULE MEET (optional) -----
        if cmd_name == "schedulemeet":
            options = data.get("options", []) or []
            title = start_str = end_str = guests_str = None
            for opt in options:
                n = opt.get("name")
                if n == "title":  title = opt.get("value")
                elif n == "start": start_str = opt.get("value")
                elif n == "end":   end_str = opt.get("value")
                elif n == "guests": guests_str = opt.get("value")

            if not title or not start_str or not end_str:
                return discord_response_message("❌ Missing required fields (title/start/end).", ephemeral=True)

            guests = [g.strip() for g in (guests_str or "").split(",") if g.strip()]
            try:
                meet_link = create_google_meet_event(title, start_str, end_str, guests)
            except Exception as e:
                return discord_response_message(
                    f"❌ Failed to schedule meet. {type(e).__name__}: {e}",
                    ephemeral=True,
                )
            return discord_response_message(
                f"✅ **Google Meet Scheduled!**\n📅 **{title}**\n🕒 {start_str} → {end_str}\n🔗 Meet Link: {meet_link}",
                ephemeral=False,
            )

        return discord_response_message("Unknown command.", ephemeral=True)

    # 3) MESSAGE_COMPONENT (button clicks)
    if t == 3:
        data = payload.get("data", {}) or {}
        custom_id = data.get("custom_id", "")
        message = payload.get("message", {}) or {}
        content = message.get("content", "") or ""

        member = payload.get("member", {}) or {}
        user = member.get("user", {}) or payload.get("user", {}) or {}
        reviewer = (user.get("global_name") or user.get("username") or "Unknown").strip()

        # naive parse of our earlier message format
        def extract(field: str) -> str:
            key = f"**{field}:** "
            if key in content:
                after = content.split(key, 1)[1]
                return after.split("\n", 1)[0].strip()
            return ""

        req_name = extract("Leave Request from").strip("* ") or extract("Leave Request from")
        from_str = extract("From")
        to_str   = extract("To")
        reason   = extract("Reason")

        if custom_id in ("leave_approve", "leave_reject"):
            decision = "Approved" if custom_id == "leave_approve" else "Rejected"

            # record decision
            try:
                append_leave_decision_row(req_name, from_str, to_str, reason, decision, reviewer)
            except Exception as e:
                return JSONResponse({
                    "type": 4,
                    "data": {"content": f"❌ Failed to record decision. {type(e).__name__}: {e}", "flags": 1 << 6}
                })

            # update original message & disable buttons
            new_content = content + f"\n\n**Status:** {decision} by **{reviewer}** at **{get_ist_timestamp()} IST**"
            disabled_components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "leave_approve", "disabled": True},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "leave_reject",  "disabled": True},
                ]
            }]
            return JSONResponse({"type": 7, "data": {"content": new_content, "components": disabled_components}})

        return JSONResponse({"type": 4, "data": {"content": "Unsupported action.", "flags": 1 << 6}})

    return discord_response_message("Unsupported interaction type.", ephemeral=True)