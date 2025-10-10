# api/discord.py
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import os, json, time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# Discord signature verification
import nacl.signing
import nacl.exceptions

# Google APIs
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Utils
from dotenv import load_dotenv
import requests

# Load local .env only for local testing; ignored on Vercel
load_dotenv(r"../.env")

app = FastAPI(title="Discord Attendance → Google Sheets")

# ========= ENV VARS =========
DISCORD_PUBLIC_KEY      = os.environ.get("DISCORD_PUBLIC_KEY", "")
SHEET_ID                = os.environ.get("SHEET_ID", "")
SHEET_RANGE             = os.environ.get("SHEET_RANGE", "Attendance!A:C")
SERVICE_ACCOUNT_JSON    = os.environ.get("SERVICE_ACCOUNT_JSON", "")
BOT_TOKEN               = os.environ.get("BOT_TOKEN", "")
APPROVER_CHANNEL_ID     = os.environ.get("APPROVER_CHANNEL_ID", "")
APPROVER_USER_ID        = os.environ.get("APPROVER_USER_ID", "")
LEAVE_STATUS_CHANNEL_ID = os.environ.get("LEAVE_STATUS_CHANNEL_ID", "")
HR_ROLE_ID              = os.environ.get("HR_ROLE_ID", "")
ATTENDANCE_CHANNEL_ID   = os.environ.get("ATTENDANCE_CHANNEL_ID", "")

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

def get_ist_timestamp() -> str:
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

# ----- Attendance helpers -----
def fetch_attendance_rows() -> list[list[str]]:
    """Returns rows from 'Attendance'!A:C (including header if present)."""
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="Attendance!A:C"
    ).execute()
    return resp.get("values", []) or []

def _ts_to_date_ist(ts_str: str) -> date | None:
    """
    Parse 'YYYY-MM-DD HH:MM:SS' (IST string we wrote) to date.
    If format differs, try first 10 chars as YYYY-MM-DD.
    """
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str.strip()[:19], "%Y-%m-%d %H:%M:%S").date()
    except Exception:
        try:
            return datetime.strptime(ts_str.strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return None

def today_ist_date() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()

def get_today_actions_for_name(name: str) -> set[str]:
    """
    Scan Attendance sheet and return actions {'Login','Logout'} recorded today (IST) for this name.
    """
    rows = fetch_attendance_rows()
    actions = set()
    if not rows:
        return actions

    # If header likely exists, keep it simple and just iterate all rows safely.
    today = today_ist_date()
    for r in rows:
        if len(r) < 3:
            continue
        ts, n, action = (r[0] or ""), (r[1] or ""), (r[2] or "")
        if not n or not action:
            continue
        if n.strip().lower() != name.strip().lower():
            continue
        d = _ts_to_date_ist(ts)
        if d == today:
            a = action.strip().lower()
            if a == "login":
                actions.add("Login")
            elif a == "logout":
                actions.add("Logout")
    return actions

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

def record_attendance_auto(name: str) -> tuple[str | None, str]:
    """
    Decide what to do for today's attendance for 'name'.
    Returns (action_taken, human_message).
      - action_taken is 'Login' or 'Logout' when a row was added; None if ignored.
    Logic:
      - No entry today -> add Login
      - Only Login exists -> add Logout
      - Login & Logout exist -> ignore
    """
    acts = get_today_actions_for_name(name)
    if "Login" in acts and "Logout" in acts:
        return None, f"ℹ️ Already logged **Login** and **Logout** for today."
    elif "Login" in acts:
        # add Logout
        append_attendance_row(name, "Logout")
        return "Logout", f"✅ Recorded **Logout** for today."
    else:
        # no Login today yet => add Login
        append_attendance_row(name, "Login")
        return "Login", f"✅ Recorded **Login** for today."

def broadcast_attendance(name: str, action: str, user_id: str, fallback_channel_id: str | None):
    """
    Sends a message that @mentions the HR role *and* the user who logged in/out.
    Posts to ATTENDANCE_CHANNEL_ID if set, else to the interaction channel.
    Also DMs the user for their own record.
    """
    bot_token = BOT_TOKEN.strip()
    if not bot_token:
        print("⚠️ No BOT_TOKEN; skipping attendance broadcast.")
        return False

    channel_id = (ATTENDANCE_CHANNEL_ID.strip() or (fallback_channel_id or "").strip())
    if not channel_id:
        print("⚠️ No channel to post attendance broadcast.")
        return False

    role_ping = f"<@&{HR_ROLE_ID.strip()}>" if HR_ROLE_ID.strip() else "HR"
    user_ping = f"<@{user_id}>" if user_id else name
    icon = "🟢" if action.lower() == "login" else "🔴"

    content = (
        f"{icon} **Attendance**\n"
        f"👤 {user_ping} — **{name}**\n"
        f"🕒 {get_ist_timestamp()} IST\n"
        f"📝 Action: **{action}**\n"
        f"{role_ping} please take note."
    )

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://example.com, 1.0)",
    }

    # 1) Post in channel (mention HR role + user)
    try:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        body = {
            "content": content,
            "allowed_mentions": {
                "parse": [],
                "roles": [HR_ROLE_ID] if HR_ROLE_ID.strip() else [],
                "users": [user_id] if user_id else [],
            },
        }
        r = requests.post(url, headers=headers, json=body, timeout=15)
        print(f"POST attendance broadcast -> {r.status_code} {r.text}")
        r.raise_for_status()
    except Exception as e:
        print(f"❌ Failed to post attendance broadcast: {e}")

    # 2) DM the user a receipt
    try:
        if user_id:
            dm = requests.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers,
                json={"recipient_id": user_id},
                timeout=15,
            )
            print(f"Create DM for attendance -> {dm.status_code} {dm.text}")
            dm.raise_for_status()
            dm_ch = dm.json().get("id")
            if dm_ch:
                dm_msg = (
                    f"{icon} Attendance recorded for **{name}**\n"
                    f"🕒 {get_ist_timestamp()} IST\n"
                    f"Action: **{action}**"
                )
                requests.post(
                    f"https://discord.com/api/v10/channels/{dm_ch}/messages",
                    headers=headers,
                    json={"content": dm_msg},
                    timeout=15,
                )
    except Exception as e:
        print(f"⚠️ Failed to DM attendance receipt: {e}")

    return True

# ----- Leave helpers (unchanged from your last version) -----
def append_leave_row(name: str, from_date: str, to_date: str, reason: str) -> None:
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID env var missing")
    service = get_service()
    values = [[get_ist_timestamp(), name, from_date, to_date, reason]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'Leave Requests'!A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def append_leave_decision_row(name: str, from_date: str, to_date: str, reason: str,
                              decision: str, reviewer: str) -> None:
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID env var missing")
    service = get_service()
    values = [[get_ist_timestamp(), name, from_date, to_date, reason, decision, reviewer]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'Leave Decisions'!A:G",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def post_leave_status_update(name: str, from_date: str, to_date: str, reason: str,
                             decision: str, reviewer: str, fallback_channel_id: str | None):
    bot_token = BOT_TOKEN.strip()
    status_channel_id = (LEAVE_STATUS_CHANNEL_ID.strip()
                         or APPROVER_CHANNEL_ID.strip()
                         or (fallback_channel_id or "").strip())
    if not bot_token or not status_channel_id:
        print("⚠️ Skipping status post (missing BOT_TOKEN or channel id).")
        return False
    icon = "✅" if decision.lower() == "approved" else "❌"
    content = (
        f"{icon} **Leave {decision}**\n"
        f"👤 **Employee:** {name}\n"
        f"🗓️ **From:** {from_date}\n"
        f"🗓️ **To:** {to_date}\n"
        f"💬 **Reason:** {reason}\n"
        f"🧑‍💼 **Reviewer:** {reviewer} — **{get_ist_timestamp()} IST**"
    )
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://example.com, 1.0)",
    }
    url = f"https://discord.com/api/v10/channels/{status_channel_id}/messages"
    try:
        r = requests.post(url, headers=headers, json={"content": content}, timeout=15)
        print(f"POST {url} -> {r.status_code} {r.text}")
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ Failed to post leave status update: {e}")
        return False

def notify_approver(name: str, from_date: str, to_date: str, reason: str,
                    fallback_channel_id: str | None = None) -> bool:
    bot_token = BOT_TOKEN.strip()
    approver_channel_id = APPROVER_CHANNEL_ID.strip()
    approver_user_id    = APPROVER_USER_ID.strip()
    if not bot_token:
        print("❌ BOT_TOKEN missing in env")
        return False
    content = (
        f"📩 **Leave Request from {name}**\n"
        f"🗓️ **From:** {from_date}\n"
        f"🗓️ **To:** {to_date}\n"
        f"💬 **Reason:** {reason}\n\n"
        f"Please review and respond accordingly."
    )
    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "Approve", "custom_id": "leave_approve"},
            {"type": 2, "style": 4, "label": "Reject",  "custom_id": "leave_reject" }
        ]
    }]
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://example.com, 1.0)",
    }
    def post_to_channel(channel_id: str) -> bool:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
        print(f"POST {url} -> {r.status_code} {r.text}")
        r.raise_for_status()
        return True
    try:
        if approver_channel_id:
            return post_to_channel(approver_channel_id)
        if approver_user_id:
            dm = requests.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers, json={"recipient_id": approver_user_id}, timeout=15
            )
            print(f"Create DM -> {dm.status_code} {dm.text}")
            dm.raise_for_status()
            dm_channel_id = dm.json().get("id")
            return post_to_channel(dm_channel_id)
        if fallback_channel_id:
            return post_to_channel(fallback_channel_id)
        print("⚠️ No APPROVER_CHANNEL_ID/APPROVER_USER_ID/fallback; not notifying.")
        return False
    except requests.HTTPError as e:
        print(f"❌ Discord API error while notifying approver: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error while notifying approver: {e}")
        return False

def discord_response_message(content: str, ephemeral: bool = True) -> JSONResponse:
    data = {"content": content}
    if ephemeral:
        data["flags"] = 1 << 6  # ephemeral flag = 64
    return JSONResponse({"type": 4, "data": data})  # CHANNEL_MESSAGE_WITH_SOURCE

# ========= ROUTE =========
@app.post("/")
async def discord_interaction(
    request: Request,
    x_signature_ed25519: str = Header(None, alias="X-Signature-Ed25519"),
    x_signature_timestamp: str = Header(None, alias="X-Signature-Timestamp"),
):
    body: bytes = await request.body()

    # Header case-insensitivity fallback
    if not x_signature_ed25519 or not x_signature_timestamp:
        h = request.headers
        x_signature_ed25519 = x_signature_ed25519 or h.get("x-signature-ed25519")
        x_signature_timestamp = x_signature_timestamp or h.get("x-signature-timestamp")

    # Verify signature over RAW body
    if not verify_signature(x_signature_ed25519 or "", x_signature_timestamp or "", body):
        raise HTTPException(status_code=401, detail="invalid request signature")

    payload = await request.json()
    t = payload.get("type")

    # 1) PING -> PONG
    if t == 1:
        return JSONResponse({"type": 1})

    # 1.5) Autocomplete no-op
    if t == 4:  # APPLICATION_COMMAND_AUTOCOMPLETE
        return JSONResponse({"type": 8, "data": {"choices": []}})

    # 2) APPLICATION_COMMAND
    if t == 2:
        data = payload.get("data", {}) or {}
        cmd_name = data.get("name", "")

        # ----- ATTENDANCE (NO ARGUMENTS) -----
        if cmd_name == "attendance":
            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            name = (user.get("global_name") or user.get("username") or "Unknown").strip()

            try:
                action_taken, info = record_attendance_auto(name=name)
                # Only broadcast if we actually added a row
                if action_taken is not None:
                    user_id = (user.get("id") or "").strip()
                    fallback_channel_id = payload.get("channel_id")
                    broadcast_attendance(name=name, action=action_taken, user_id=user_id,
                                         fallback_channel_id=fallback_channel_id)
            except Exception as e:
                return discord_response_message(
                    f"❌ Failed to record attendance. {type(e).__name__}: {e}", ephemeral=True
                )

            # Ephemeral summary to invoker
            stamp = get_ist_timestamp()
            return discord_response_message(
                f"{info}\n👤 **{name}** • 🕒 **{stamp} IST**",
                ephemeral=True
            )

        # ----- LEAVE COUNT / LEAVE REQUEST / MEET -----
        # (Your existing handlers remain unchanged)
        if cmd_name == "leavecount":
            # ... (unchanged from your last version) ...
            options = data.get("options", []) or []
            explicit_name = None
            for opt in options:
                if opt.get("name") == "name":
                    explicit_name = (opt.get("value") or "").strip()
            # compute using approved decisions only
            # helper functions are above in your previous version
            # --- BEGIN: reused helpers for leavecount ---
            def _month_bounds_ist():
                now = datetime.now(ZoneInfo("Asia/Kolkata"))
                start = date(year=now.year, month=now.month, day=1)
                if now.month == 12:
                    end = date(year=now.year, month=12, day=31)
                else:
                    first_next = date(year=now.year, month=now.month + 1, day=1)
                    end = first_next - timedelta(days=1)
                return start, end
            def _parse_ymd(s: str):
                try: return datetime.strptime(s.strip(), "%Y-%m-%d").date()
                except Exception: return None
            def _overlap_days(d1s, d1e, d2s, d2e):
                lo = max(d1s, d2s); hi = min(d1e, d2e)
                if lo > hi: return 0
                return (hi - lo).days + 1
            def fetch_leave_decisions_rows():
                service = get_service()
                resp = service.spreadsheets().values().get(
                    spreadsheetId=SHEET_ID, range="'Leave Decisions'!A:G"
                ).execute()
                return resp.get("values", []) or []
            def count_user_leaves_current_month(target_name: str):
                rows = fetch_leave_decisions_rows()
                if not rows: return 0, 0, []
                start_idx = 0
                if rows and rows[0]:
                    header = [c.lower() for c in rows[0]]
                    if ("name" in (header[1] if len(header) > 1 else "")) or ("decision" in (header[5] if len(header) > 5 else "")):
                        start_idx = 1
                month_start, month_end = _month_bounds_ist()
                req_count = total_days = 0
                details = []
                for r in rows[start_idx:]:
                    if len(r) < 6: continue
                    nm = (r[1] or "").strip()
                    dec = (r[5] or "").strip().lower()
                    if not nm or dec != "approved": continue
                    if nm.lower() != (target_name or "").strip().lower(): continue
                    d_from = _parse_ymd(r[2]) if len(r) > 2 else None
                    d_to   = _parse_ymd(r[3]) if len(r) > 3 else None
                    if not d_from or not d_to: continue
                    if d_from > d_to: d_from, d_to = d_to, d_from
                    od = _overlap_days(d_from, d_to, month_start, month_end)
                    if od > 0:
                        req_count += 1; total_days += od; details.append((d_from, d_to, od))
                return req_count, total_days, details
            # --- END helpers ---
            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            fallback_name = user.get("global_name") or user.get("username") or "Unknown"
            target_name = (explicit_name or fallback_name).strip()
            try:
                req_count, total_days, details = count_user_leaves_current_month(target_name)
            except Exception as e:
                return discord_response_message(f"❌ Could not read leave data. {type(e).__name__}: {e}", ephemeral=True)
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
            month_label = now_ist.strftime("%B %Y")
            if req_count == 0:
                return discord_response_message(
                    f"📊 **{target_name}** has **0** approved leave requests in **{month_label}**.",
                    ephemeral=True
                )
            lines = []
            for i, (df, dt, od) in enumerate(details[:5], 1):
                lines.append(f"{i}. {df.isoformat()} → {dt.isoformat()} ({od} day{'s' if od!=1 else ''})")
            extra = ""
            if len(details) > 5:
                extra = f"\n…and {len(details) - 5} more request(s)."
            msg = (
                f"📊 **{target_name}** in **{month_label}**\n"
                f"• Approved requests overlapping this month: **{req_count}**\n"
                f"• Total approved days this month: **{total_days}**\n\n"
                + "\n".join(lines) + extra
            )
            return discord_response_message(msg, ephemeral=True)

        if cmd_name == "leaverequest":
            options = data.get("options", []) or []
            name = from_opt = to_opt = reason_opt = None
            for opt in options:
                n = opt.get("name")
                if n == "name":   name = opt.get("value")
                elif n == "from": from_opt = opt.get("value")
                elif n == "to":   to_opt = opt.get("value")
                elif n == "reason": reason_opt = opt.get("value")
            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            fallback_name = user.get("global_name") or user.get("username") or "Unknown"
            name = (name or fallback_name).strip()
            try:
                append_leave_row(name=name, from_date=from_opt, to_date=to_opt, reason=reason_opt)
                channel_id_from_payload = payload.get("channel_id")
                notify_approver(name, from_opt, to_opt, reason_opt, fallback_channel_id=channel_id_from_payload)
            except Exception as e:
                return discord_response_message(f"❌ Failed to record leave. {type(e).__name__}: {e}", ephemeral=True)
            return discord_response_message(
                f"✅ Leave request submitted by **{name}** from **{from_opt}** to **{to_opt}**.\nReason: {reason_opt}",
                ephemeral=True,
            )

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
            try:
                creds = service_account.Credentials.from_service_account_info(
                    json.loads(SERVICE_ACCOUNT_JSON),
                    scopes=["https://www.googleapis.com/auth/calendar"]
                )
                cal_svc = build("calendar", "v3", credentials=creds)
                event = {
                    'summary': title,
                    'start': {'dateTime': start_str, 'timeZone': 'Asia/Kolkata'},
                    'end':   {'dateTime': end_str,   'timeZone': 'Asia/Kolkata'},
                    'conferenceData': {
                        'createRequest': {
                            'requestId': f"discord-meet-{int(time.time())}",
                            'conferenceSolutionKey': {'type': 'hangoutsMeet'},
                        }
                    },
                }
                evt = cal_svc.events().insert(calendarId='primary', body=event, conferenceDataVersion=1).execute()
                meet_link = evt.get("hangoutLink", "No Meet Link Found")
            except Exception as e:
                return discord_response_message(f"❌ Failed to schedule meet. {type(e).__name__}: {e}", ephemeral=True)
            return discord_response_message(
                f"✅ **Google Meet Scheduled!**\n📅 **{title}**\n🕒 {start_str} → {end_str}\n🔗 {meet_link}",
                ephemeral=False,
            )

        return discord_response_message("Unknown command.", ephemeral=True)

    # 3) MESSAGE_COMPONENT (buttons) and 4) MODAL_SUBMIT (reject modal)
    # (Your existing leave-approval code stays the same as in your last version.)
    if t == 3:
        # ... unchanged from your last version ...
        return JSONResponse({"type": 4, "data": {"content": "Unsupported action.", "flags": 1 << 6}})
    if t == 5:
        # ... unchanged from your last version ...
        return JSONResponse({"type": 4, "data": {"content": "✅ Rejection recorded.", "flags": 1 << 6}})

    # Fallback
    return discord_response_message("Unsupported interaction type.", ephemeral=True)
