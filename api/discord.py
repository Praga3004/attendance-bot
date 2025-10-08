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
load_dotenv('..\.env')  # Load env vars from .env file for local testing
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
    except nacl.exceptions.BadSignatureError:
        return False

def normalize_action(action_raw: str | None) -> str:
    if not action_raw:
        return "Login"
    a = action_raw.strip().lower()
    return "Login" if a == "login" else "Logout"

def get_ist_timestamp() -> str:
    # Asia/Kolkata, 24h format, e.g. 2025-10-08 10:05:12
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

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

    # 1) Verify Discord signature
    if not verify_signature(x_signature_ed25519, x_signature_timestamp, body):
        raise HTTPException(status_code=401, detail="invalid request signature")

    payload = await request.json()
    t = payload.get("type")

    # 2) PING → PONG
    if t == 1:
        return JSONResponse({"type": 1})

    # 3) Application Command
    if t == 2:
        data = payload.get("data", {})
        cmd_name = data.get("name", "")

        if cmd_name != "attendance":
            return discord_response_message("Unknown command.", ephemeral=True)

        # Extract options if provided
        options = data.get("options", []) or []
        # Default to Discord user's display name + action=Login
        # (You can define options in your slash command to send explicit name/action)
        name_opt = None
        action_opt = None
        for opt in options:
            if opt.get("name") == "name":
                name_opt = opt.get("value")
            if opt.get("name") == "action":
                action_opt = opt.get("value")

        # Derive user name if not provided
        member = payload.get("member", {}) or {}
        user = member.get("user", {}) or payload.get("user", {}) or {}
        fallback_name = user.get("global_name") or user.get("username") or "Unknown"
        name = (name_opt or fallback_name).strip()
        action = normalize_action(action_opt)

        # Append to Google Sheets
        try:
            append_attendance_row(name=name, action=action)
        except Exception as e:
            # Return an ephemeral error message to user
            return discord_response_message(
                f"❌ Failed to record attendance. Admins: {type(e).__name__}: {str(e)}",
                ephemeral=True,
            )

        # Success response
        return discord_response_message(
            f"✅ Recorded: **{name}** — **{action}** at **{get_ist_timestamp()} IST**",
            ephemeral=True,
        )

    # Fallback
    return discord_response_message("Unsupported interaction type.", ephemeral=True)
