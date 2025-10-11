# api/discord.py
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import os, json, time, requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# Discord signature verification
import nacl.signing

# Google APIs
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Utils
from dotenv import load_dotenv

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
def _date_opts(start: date, days: int) -> list[dict]:
    days = max(0, min(days, 25))  # Discord limit
    return [{
        "label": f"{(start + timedelta(i)).isoformat()} ({(start + timedelta(i)).strftime('%a')})",
        "value": (start + timedelta(i)).isoformat()
    } for i in range(days)]
# ========= CORE HELPERS =========
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

def today_ist_date() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()

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

def discord_response_message(content: str, ephemeral: bool = True) -> JSONResponse:
    data = {"content": content}
    if ephemeral:
        data["flags"] = 1 << 6  # ephemeral flag = 64
    return JSONResponse({"type": 4, "data": data})

# ========= ATTENDANCE =========
def fetch_attendance_rows() -> list[list[str]]:
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Attendance!A:C"
    ).execute()
    return resp.get("values", []) or []

def _ts_to_date_ist(ts_str: str) -> date | None:
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts_str.strip()[:len(fmt)], fmt).date()
        except Exception:
            continue
    return None

def get_today_actions_for_name(name: str) -> set[str]:
    rows = fetch_attendance_rows()
    actions = set()
    tday = today_ist_date()
    for r in rows:
        if len(r) < 3:  # ts, name, action
            continue
        ts, n, action = (r[0] or ""), (r[1] or ""), (r[2] or "")
        if not n or not action:
            continue
        if n.strip().lower() != name.strip().lower():
            continue
        d = _ts_to_date_ist(ts)
        if d == tday:
            a = action.strip().lower()
            if a == "login":
                actions.add("Login")
            elif a == "logout":
                actions.add("Logout")
    return actions

def append_attendance_row(name: str, action: str) -> None:
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
    acts = get_today_actions_for_name(name)
    if "Login" in acts and "Logout" in acts:
        return None, "ℹ️ Already logged **Login** and **Logout** for today."
    elif "Login" in acts:
        append_attendance_row(name, "Logout")
        return "Logout", "✅ Recorded **Logout** for today."
    else:
        append_attendance_row(name, "Login")
        return "Login", "✅ Recorded **Login** for today."

def broadcast_attendance(name: str, action: str, user_id: str, fallback_channel_id: str | None):
    bot_token = BOT_TOKEN.strip()
    if not bot_token:
        return False
    channel_id = (ATTENDANCE_CHANNEL_ID.strip() or (fallback_channel_id or "").strip())
    if not channel_id:
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
        r.raise_for_status()
    except Exception as e:
        print(f"❌ Attendance broadcast failed: {e}")

    # DM user receipt (best effort)
    try:
        if user_id:
            dm = requests.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers,
                json={"recipient_id": user_id},
                timeout=15,
            )
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
        print(f"⚠️ Attendance DM failed: {e}")
    return True

# ========= LEAVE =========
def append_leave_row(name: str, from_date: str, to_date: str, reason: str) -> None:
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
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{status_channel_id}/messages"
    try:
        r = requests.post(url, headers=headers, json={"content": content}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ Leave status post failed: {e}")
        return False

# ========= LEAVE COUNT (APPROVED ONLY) =========
def _month_bounds_ist() -> tuple[date, date]:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    start = date(now.year, now.month, 1)
    if now.month == 12:
        end = date(now.year, 12, 31)
    else:
        end = date(now.year, now.month + 1, 1) - timedelta(days=1)
    return start, end

def _parse_ymd(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None

def _overlap_days(d1s: date, d1e: date, d2s: date, d2e: date) -> int:
    lo, hi = max(d1s, d2s), min(d1e, d2e)
    return 0 if lo > hi else (hi - lo).days + 1

def fetch_leave_decisions_rows() -> list[list[str]]:
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="'Leave Decisions'!A:G"
    ).execute()
    return resp.get("values", []) or []

def count_user_leaves_current_month(target_name: str) -> tuple[int, int, list[tuple[date, date, int]]]:
    rows = fetch_leave_decisions_rows()
    if not rows:
        return 0, 0, []
    start_idx = 0
    if rows and rows[0]:
        header = [c.lower() for c in rows[0]]
        if ("name" in (header[1] if len(header) > 1 else "")) or ("decision" in (header[5] if len(header) > 5 else "")):
            start_idx = 1

    month_start, month_end = _month_bounds_ist()
    req_count = total_days = 0
    details = []
    for r in rows[start_idx:]:
        if len(r) < 6:
            continue
        nm = (r[1] or "").strip()
        dec = (r[5] or "").strip().lower()
        if not nm or dec != "approved":
            continue
        if nm.lower() != (target_name or "").strip().lower():
            continue
        d_from = _parse_ymd(r[2]) if len(r) > 2 else None
        d_to   = _parse_ymd(r[3]) if len(r) > 3 else None
        if not d_from or not d_to:
            continue
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        od = _overlap_days(d_from, d_to, month_start, month_end)
        if od > 0:
            req_count += 1
            total_days += od
            details.append((d_from, d_to, od))
    return req_count, total_days, details

# ========= WFH =========
def append_wfh_row(name: str, day: str, reason: str) -> None:
    service = get_service()
    values = [[get_ist_timestamp(), name, day, reason]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'WFH Requests'!A:D",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def append_wfh_decision_row(name: str, day: str, reason: str,
                            decision: str, reviewer: str, note: str = "") -> None:
    service = get_service()
    values = [[get_ist_timestamp(), name, day, reason, decision, reviewer, note]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'WFH Decisions'!A:G",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def post_wfh_status_update(name: str, day: str, reason: str,
                           decision: str, reviewer: str, fallback_channel_id: str | None):
    bot_token = BOT_TOKEN.strip()
    status_channel_id = (LEAVE_STATUS_CHANNEL_ID.strip()
                         or APPROVER_CHANNEL_ID.strip()
                         or (fallback_channel_id or "").strip())
    if not bot_token or not status_channel_id:
        return False
    icon = "🏠✅" if decision.lower() == "approved" else "🏠❌"
    content = (
        f"{icon} **WFH {decision}**\n"
        f"👤 **Employee:** {name}\n"
        f"📅 **Date:** {day}\n"
        f"💬 **Reason:** {reason}\n"
        f"🧑‍💼 **Reviewer:** {reviewer} — **{get_ist_timestamp()} IST**"
    )
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{status_channel_id}/messages"
    try:
        r = requests.post(url, headers=headers, json={"content": content}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ WFH status post failed: {e}")
        return False
def _date_opts(start: date, days: int) -> list[dict]:
    """Build select options for `days` from start (inclusive)."""
    return [{"label": f"{(start + timedelta(i)).isoformat()} ({(start + timedelta(i)).strftime('%a')})",
             "value": (start + timedelta(i)).isoformat()} for i in range(days)]

def send_leave_from_picker(channel_id: str) -> bool:
    bot_token = BOT_TOKEN.strip()
    if not (bot_token and channel_id):
        return False

    # ≤ 25 options or Discord will ignore them
    opts = _date_opts(today_ist_date(), 25)

    print(f"[leave_from_picker] options={len(opts)} first={opts[0] if opts else None}")

    body = {
        "content": "📅 Pick the **start** date for your leave:",
        "components": [{
            "type": 1,
            "components": [{
                "type": 3,  # STRING_SELECT
                "custom_id": "leave_from_select",
                "placeholder": "Select start date (From)",
                "min_values": 1, "max_values": 1,
                "options": opts
            }]
        }]
    }
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    r = requests.post(url, headers=headers, json=body, timeout=15)
    print(f"POST leave_from_picker -> {r.status_code} {r.text}")
    r.raise_for_status()
    return True

def send_wfh_date_picker(channel_id: str):
    """Shows a string select with next 14 days."""
    bot_token = BOT_TOKEN.strip()
    if not (bot_token and channel_id):
        return False
    today = today_ist_date()
    options = []
    for i in range(0, 14):
        d = today + timedelta(days=i)
        options.append({"label": f"{d.isoformat()} ({d.strftime('%a')})", "value": d.isoformat()})
    body = {
        "content": "Pick a date for your WFH request:",
        "components": [{
            "type": 1,
            "components": [{
                "type": 3,  # STRING_SELECT
                "custom_id": "wfh_date_select",
                "placeholder": "Select a date",
                "min_values": 1,
                "max_values": 1,
                "options": options
            }]
        }]
    }
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    r = requests.post(url, headers=headers, json=body, timeout=15)
    print(f"POST wfh date picker -> {r.status_code} {r.text}")
    r.raise_for_status()
    return True

def _grab_between(prefix: str, text: str) -> str:
    if prefix in text:
        after = text.split(prefix, 1)[1]
        return after.split("\n", 1)[0].strip()
    return ""

def parse_wfh_card(content: str) -> tuple[str, str, str]:
    """
    Parses a WFH request card text you posted earlier, expected lines like:
    '🏠 **WFH Request from Alice**'
    '📅 **Date:** 2025-10-11'
    '💬 **Reason:** Internet installation'
    Returns: (name, date_str, reason)
    """
    first = (content.split("\n", 1)[0] if content else "").strip()
    name = first
    for m in ["**WFH Request from ", "WFH Request from ", "🏠 **WFH Request from "]:
        if m in name:
            name = name.split(m, 1)[1]
            break
    name = name.strip("* ").strip()
    date_str = _grab_between("**Date:** ", content) or _grab_between("Date:", content)
    reason   = _grab_between("**Reason:** ", content) or _grab_between("Reason:", content)
    return name, date_str, reason

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

    # 1.5) AUTOCOMPLETE
    if t == 4:  # APPLICATION_COMMAND_AUTOCOMPLETE
        data = payload.get("data", {}) or {}
        cmd_name = data.get("name", "")
        focused = None
        for opt in data.get("options", []) or []:
            if opt.get("focused"):
                focused = opt
                break
        if cmd_name == "wfh" and focused and focused.get("name") == "date":
            now_ist = today_ist_date()
            choices = []
            for i in range(0, 14):
                d = now_ist + timedelta(days=i)
                label = f"{d.isoformat()} ({d.strftime('%a')})"
                choices.append({"name": label, "value": d.isoformat()})
            return JSONResponse({"type": 8, "data": {"choices": choices}})
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
                if action_taken is not None:
                    user_id = (user.get("id") or "").strip()
                    fallback_channel_id = payload.get("channel_id")
                    broadcast_attendance(name=name, action=action_taken, user_id=user_id,
                                         fallback_channel_id=fallback_channel_id)
            except Exception as e:
                return discord_response_message(f"❌ Failed to record attendance. {type(e).__name__}: {e}", True)
            return discord_response_message(f"{info}\n👤 **{name}** • 🕒 **{get_ist_timestamp()} IST**", True)

        # ----- LEAVE COUNT -----
        if cmd_name == "leavecount":
            options = data.get("options", []) or []
            explicit_name = None
            for opt in options:
                if opt.get("name") == "name":
                    explicit_name = (opt.get("value") or "").strip()
            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            fallback_name = user.get("global_name") or user.get("username") or "Unknown"
            target_name = (explicit_name or fallback_name).strip()
            try:
                req_count, total_days, details = count_user_leaves_current_month(target_name)
            except Exception as e:
                return discord_response_message(f"❌ Could not read leave data. {type(e).__name__}: {e}", True)
            month_label = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%B %Y")
            if req_count == 0:
                return discord_response_message(
                    f"📊 **{target_name}** has **0** approved leave requests in **{month_label}**.", True
                )
            lines = [f"{i}. {df.isoformat()} → {dt.isoformat()} ({od} day{'s' if od!=1 else ''})"
                     for i, (df, dt, od) in enumerate(details[:5], 1)]
            extra = f"\n…and {len(details) - 5} more request(s)." if len(details) > 5 else ""
            msg = (
                f"📊 **{target_name}** in **{month_label}**\n"
                f"• Approved requests overlapping this month: **{req_count}**\n"
                f"• Total approved days this month: **{total_days}**\n\n"
                + "\n".join(lines) + extra
            )
            return discord_response_message(msg, True)

        # ----- LEAVE REQUEST -----
        if cmd_name == "leaverequest":
            options = data.get("options", []) or []
            from_opt = to_opt = reason_opt = None
            for opt in options:
                n = opt.get("name")
                if n == "from": from_opt = (opt.get("value") or "").strip()
                elif n == "to": to_opt = (opt.get("value") or "").strip()
                elif n == "reason": reason_opt = (opt.get("value") or "").strip()

            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            name = (user.get("global_name") or user.get("username") or "Unknown").strip()

            # If from/to not provided -> show pickers flow
            if not from_opt or not to_opt:
                ch_id = payload.get("channel_id")
                if ch_id:
                    send_leave_from_picker(ch_id)
                return discord_response_message(
                    "🗓️ I posted a **From date** picker. Choose From first; I’ll then show valid **To** dates.",
                    True
                )
            try:
                append_leave_row(name=name, from_date=from_opt, to_date=to_opt, reason=reason_opt or "")
                channel_id_from_payload = payload.get("channel_id")
                # Reuse leave approver notify (buttons handled in t==3 below)
                bot_token = BOT_TOKEN.strip()
                if bot_token:
                    content = (
                        f"📩 **Leave Request from {name}**\n"
                        f"🗓️ **From:** {from_opt}\n"
                        f"🗓️ **To:** {to_opt}\n"
                        f"💬 **Reason:** {reason_opt or '(not provided)'}\n\n"
                        f"Please review and respond accordingly."
                    )
                    components = [{
                        "type": 1,
                        "components": [
                            {"type": 2, "style": 3, "label": "Approve", "custom_id": "leave_approve"},
                            {"type": 2, "style": 4, "label": "Reject",  "custom_id": "leave_reject" }
                        ]
                    }]
                    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
                    def post_to_channel(cid: str):
                        url = f"https://discord.com/api/v10/channels/{cid}/messages"
                        r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
                        r.raise_for_status()
                    if APPROVER_CHANNEL_ID.strip():
                        post_to_channel(APPROVER_CHANNEL_ID.strip())
                    elif APPROVER_USER_ID.strip():
                        dm = requests.post("https://discord.com/api/v10/users/@me/channels",
                                           headers=headers, json={"recipient_id": APPROVER_USER_ID.strip()}, timeout=15)
                        dm.raise_for_status()
                        dm_ch = dm.json().get("id")
                        if dm_ch: post_to_channel(dm_ch)
                    elif channel_id_from_payload:
                        post_to_channel(channel_id_from_payload)
            except Exception as e:
                return discord_response_message(f"❌ Failed to record leave. {type(e).__name__}: {e}", True)

            return discord_response_message(
                f"✅ Leave request submitted by **{name}** from **{from_opt}** to **{to_opt}**.\nReason: {reason_opt or '(not provided)'}",
                True
            )
        

        # ----- WFH -----
        if cmd_name == "wfh":
            options = data.get("options", []) or []
            day = reason = None
            for opt in options:
                n = opt.get("name")
                if n == "date":
                    day = (opt.get("value") or "").strip()
                elif n == "reason":
                    reason = (opt.get("value") or "").strip()

            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            name = (user.get("global_name") or user.get("username") or "Unknown").strip()

            if not day:
                ch_id = payload.get("channel_id")
                if ch_id: send_wfh_date_picker(ch_id)
                return discord_response_message("🗓️ Choose a date from the picker I just posted (or use the autocomplete).", True)

            # 1) log request
            try:
                append_wfh_row(name=name, day=day, reason=reason or "")
            except Exception as e:
                return discord_response_message(f"❌ Failed to record WFH request. {type(e).__name__}: {e}", True)

            # 2) notify approver channel/DM with Approve/Reject buttons
            bot_token = BOT_TOKEN.strip()
            if bot_token:
                content = (
                    f"🏠 **WFH Request from {name}**\n"
                    f"📅 **Date:** {day}\n"
                    f"💬 **Reason:** {reason or '(not provided)'}\n\n"
                    f"Please review and respond accordingly."
                )
                components = [{
                    "type": 1,
                    "components": [
                        {"type": 2, "style": 3, "label": "Approve", "custom_id": "wfh_approve"},
                        {"type": 2, "style": 4, "label": "Reject",  "custom_id": "wfh_reject" }
                    ]
                }]
                headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
                def post_to_channel(cid: str):
                    url = f"https://discord.com/api/v10/channels/{cid}/messages"
                    r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
                    print(f"POST WFH notify -> {r.status_code} {r.text}")
                    r.raise_for_status()
                try:
                    if APPROVER_CHANNEL_ID.strip():
                        post_to_channel(APPROVER_CHANNEL_ID.strip())
                    elif APPROVER_USER_ID.strip():
                        dm = requests.post("https://discord.com/api/v10/users/@me/channels",
                                           headers=headers, json={"recipient_id": APPROVER_USER_ID.strip()}, timeout=15)
                        print(f"Create DM (WFH) -> {dm.status_code} {dm.text}")
                        dm.raise_for_status()
                        dm_ch = dm.json().get("id")
                        if dm_ch: post_to_channel(dm_ch)
                    else:
                        ch_id = payload.get("channel_id")
                        if ch_id: post_to_channel(ch_id)
                except Exception as e:
                    print(f"⚠️ Could not notify approver for WFH: {e}")

            return discord_response_message(
                f"✅ WFH request submitted for **{day}**.\nReason: {reason or '(not provided)'}",
                True
            )

        # ----- SCHEDULE MEET (optional) -----
        if cmd_name == "schedulemeet":
            options = data.get("options", []) or []
            title = start_str = end_str = None
            for opt in options:
                n = opt.get("name")
                if n == "title":  title = opt.get("value")
                elif n == "start": start_str = opt.get("value")
                elif n == "end":   end_str = opt.get("value")
            if not title or not start_str or not end_str:
                return discord_response_message("❌ Missing required fields (title/start/end).", True)
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
                return discord_response_message(f"❌ Failed to schedule meet. {type(e).__name__}: {e}", True)
            return discord_response_message(
                f"✅ **Google Meet Scheduled!**\n📅 **{title}**\n🕒 {start_str} → {end_str}\n🔗 {meet_link}",
                False
            )

        return discord_response_message("Unknown command.", True)

    # 3) MESSAGE_COMPONENT (buttons & selects)
    if t == 3:
        data = payload.get("data", {}) or {}
        custom_id = data.get("custom_id", "")
        message = payload.get("message", {}) or {}
        content = message.get("content", "") or ""

        # who clicked (reviewer)
        member = payload.get("member", {}) or {}
        user = member.get("user", {}) or payload.get("user", {}) or {}
        reviewer = (user.get("global_name") or user.get("username") or "Unknown").strip()

        def grab_between(prefix: str, text: str) -> str:
            if prefix in text:
                after = text.split(prefix, 1)[1]
                return after.split("\n", 1)[0].strip()
            return ""

        # ---- WFH date select
        if custom_id == "wfh_date_select":
            values = (data.get("values") or [])
            picked_date = values[0] if values else None
            if not picked_date:
                return JSONResponse({"type": 4, "data": {"content": "❌ No date selected.", "flags": 1 << 6}})
            return JSONResponse({"type": 4, "data": {"content": f"✅ Selected WFH date: **{picked_date}**", "flags": 1 << 6}})

        # ---- Leave approve/reject buttons (old flow)
        first_line = (content.split("\n", 1)[0] if content else "").strip()
        req_name = first_line
        for marker in ["**Leave Request from ", "Leave Request from ", "📩 **Leave Request from "]:
            if marker in req_name:
                req_name = req_name.split(marker, 1)[1]
                break
        req_name = req_name.strip("* ").strip()
        from_str = grab_between("**From:** ", content)
        to_str   = grab_between("**To:** ", content)
        reason   = grab_between("**Reason:** ", content)

        if custom_id == "leave_approve":
            if not (req_name and from_str and to_str):
                return JSONResponse({"type": 4, "data": {"content": "❌ Could not parse the request details.", "flags": 1 << 6}})
            decision = "Approved"
            try:
                append_leave_decision_row(req_name, from_str, to_str, reason, decision, reviewer)
            except Exception as e:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Failed to record decision. {type(e).__name__}: {e}", "flags": 1 << 6}})
            new_content = content + f"\n\n**Status:** {decision} by **{reviewer}** at **{get_ist_timestamp()} IST**"
            disabled_components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "leave_approve", "disabled": True},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "leave_reject",  "disabled": True},
                ]
            }]
            post_leave_status_update(
                name=req_name, from_date=from_str, to_date=to_str,
                reason=reason, decision=decision, reviewer=reviewer,
                fallback_channel_id=payload.get("channel_id")
            )
            return JSONResponse({"type": 7, "data": {"content": new_content, "components": disabled_components}})

        if custom_id == "leave_reject":
            ch_id  = payload.get("channel_id", "")
            msg_id = message.get("id", "")
            modal_custom_id = f"reject_reason::{ch_id}::{msg_id}"
            return JSONResponse({
                "type": 9,  # MODAL
                "data": {
                    "custom_id": modal_custom_id,
                    "title": "Reject Leave",
                    "components": [{
                        "type": 1,
                        "components": [{
                            "type": 4,  # TEXT_INPUT
                            "custom_id": "reject_reason",
                            "style": 2,  # PARAGRAPH
                            "label": "Reason for rejection",
                            "min_length": 1, "max_length": 1000, "required": True,
                            "placeholder": "Enter the reason for rejection"
                        }]
                    }]
                }
            })
        if custom_id == "leave_from_select":
            values = data.get("values") or []
            from_date = values[0] if values else None
            if not from_date:
                return JSONResponse({"type": 4, "data": {"content": "❌ No start date selected.", "flags": 1 << 6}})

            from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
            to_start = from_dt + timedelta(days=1)

            # ≤ 25
            to_opts = _date_opts(to_start, 25)
            print(f"[leave_to_picker] from={from_date} options={len(to_opts)} first={to_opts[0] if to_opts else None}")

            return JSONResponse({
                "type": 7,  # UPDATE_MESSAGE
                "data": {
                    "content": f"📅 From: **{from_date}**\nNow pick the **end** date:",
                    "components": [{
                        "type": 1,
                        "components": [{
                            "type": 3,
                            "custom_id": f"leave_to_select::{from_date}",
                            "placeholder": "Select end date (To)",
                            "min_values": 1, "max_values": 1,
                            "options": to_opts
                        }]
                    }]
                }
            })


        if custom_id.startswith("leave_to_select::"):
            # Extract from date from custom_id
            _, from_date = custom_id.split("::", 1)
            values = data.get("values") or []
            to_date = values[0] if values else None
            if not to_date:
                return JSONResponse({"type": 4, "data": {"content": "❌ No end date selected.", "flags": 1 << 6}})

            # pop a modal to collect reason
            modal_custom_id = f"leave_reason::{from_date}::{to_date}"
            return JSONResponse({
                "type": 9,  # MODAL
                "data": {
                    "custom_id": modal_custom_id,
                    "title": "Leave Reason",
                    "components": [{
                        "type": 1,
                        "components": [{
                            "type": 4,  # TEXT_INPUT
                            "custom_id": "leave_reason_text",
                            "style": 2,  # PARAGRAPH
                            "label": "Reason (optional)",
                            "required": False,
                            "max_length": 1000,
                            "placeholder": "Why are you requesting this leave?"
                        }]
                    }]
                }
            })

        # ---- WFH approve/reject buttons
        if custom_id in ("wfh_approve", "wfh_reject"):
            # Parse from the WFH card
            name, date_str, wfh_reason = parse_wfh_card(content)
            if not (name and date_str):
                return JSONResponse({"type": 4, "data": {"content": "❌ Could not parse WFH request.", "flags": 1 << 6}})

            if custom_id == "wfh_approve":
                decision = "Approved"
                try:
                    append_wfh_decision_row(name, date_str, wfh_reason, decision, reviewer)
                except Exception as e:
                    return JSONResponse({"type": 4, "data": {"content": f"❌ Failed to record WFH decision. {type(e).__name__}: {e}", "flags": 1 << 6}})
                new_content = content + f"\n\n**Status:** {decision} by **{reviewer}** at **{get_ist_timestamp()} IST**"
                disabled_components = [{
                    "type": 1,
                    "components": [
                        {"type": 2, "style": 3, "label": "Approve", "custom_id": "wfh_approve", "disabled": True},
                        {"type": 2, "style": 4, "label": "Reject",  "custom_id": "wfh_reject",  "disabled": True},
                    ]
                }]
                post_wfh_status_update(
                    name=name, day=date_str, reason=wfh_reason,
                    decision=decision, reviewer=reviewer, fallback_channel_id=payload.get("channel_id")
                )
                return JSONResponse({"type": 7, "data": {"content": new_content, "components": disabled_components}})

            if custom_id == "wfh_reject":
                ch_id  = payload.get("channel_id", "")
                msg_id = message.get("id", "")
                modal_custom_id = f"wfh_reject_reason::{ch_id}::{msg_id}"
                return JSONResponse({
                    "type": 9,  # MODAL
                    "data": {
                        "custom_id": modal_custom_id,
                        "title": "Reject WFH",
                        "components": [{
                            "type": 1,
                            "components": [{
                                "type": 4,  # TEXT_INPUT
                                "custom_id": "reject_reason",
                                "style": 2,  # PARAGRAPH
                                "label": "Reason for rejection",
                                "min_length": 1, "max_length": 1000, "required": True,
                                "placeholder": "Enter the reason for rejection"
                            }]
                        }]
                    }
                })

        # Fallback for unknown buttons/selects
        return JSONResponse({
            "type": 4,
            "data": {"content": f"Unsupported action for button id `{custom_id}`.", "flags": 1 << 6}
        })

    # 4) MODAL_SUBMIT (Leave reject & WFH reject)
    if t == 5:
        data = payload.get("data", {}) or {}
        modal_custom_id = data.get("custom_id", "")
        comps = data.get("components", []) or []

        reject_note = ""
        try:
            reject_note = comps[0]["components"][0]["value"].strip()
        except Exception:
            pass

        # Reviewer
        member = payload.get("member", {}) or {}
        user = member.get("user", {}) or payload.get("user", {}) or {}
        reviewer = (user.get("global_name") or user.get("username") or "Unknown").strip()

        # Leave rejection modal
        if modal_custom_id.startswith("reject_reason::"):
            # Parse ch/msg ids
            _, ch_id, msg_id = (modal_custom_id.split("::") + ["", "", ""])[:3]
            ch_id = ch_id or payload.get("channel_id", "")
            bot_token = BOT_TOKEN.strip()
            if not (bot_token and ch_id and msg_id):
                return JSONResponse({"type": 4, "data": {"content": "❌ Missing context to complete rejection.", "flags": 1 << 6}})
            headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

            # Load original message
            get_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            r = requests.get(get_url, headers=headers, timeout=15)
            if r.status_code != 200:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Could not load original message ({r.status_code}).", "flags": 1 << 6}})
            msg = r.json()
            content = msg.get("content", "") or ""

            def grab_between(prefix: str, text: str) -> str:
                if prefix in text:
                    after = text.split(prefix, 1)[1]
                    return after.split("\n", 1)[0].strip()
                return ""

            first_line = (content.split("\n", 1)[0] if content else "").strip()
            req_name = first_line
            for marker in ["**Leave Request from ", "Leave Request from ", "📩 **Leave Request from "]:
                if marker in req_name:
                    req_name = req_name.split(marker, 1)[1]
                    break
            req_name = req_name.strip("* ").strip()
            from_str = grab_between("**From:** ", content)
            to_str   = grab_between("**To:** ", content)
            req_reason = grab_between("**Reason:** ", content)

            decision = "Rejected"
            try:
                append_leave_decision_row(req_name, from_str, to_str, req_reason, decision, reviewer)
            except Exception as e:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Failed to record decision. {type(e).__name__}: {e}", "flags": 1 << 6}})

            new_content = (
                content
                + f"\n\n**Status:** {decision} by **{reviewer}** at **{get_ist_timestamp()} IST**"
                + (f"\n📝 **Rejection Note:** {reject_note}" if reject_note else "")
            )
            disabled_components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "leave_approve", "disabled": True},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "leave_reject",  "disabled": True},
                ]
            }]
            patch_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            pr = requests.patch(patch_url, headers=headers,
                                json={"content": new_content, "components": disabled_components},
                                timeout=15)
            if pr.status_code not in (200, 201):
                print(f"❌ Failed to edit message: {pr.status_code} {pr.text}")

            combined_reason = req_reason + (f" | Rejection Note: {reject_note}" if reject_note else "")
            post_leave_status_update(
                name=req_name, from_date=from_str, to_date=to_str,
                reason=combined_reason, decision=decision, reviewer=reviewer,
                fallback_channel_id=ch_id
            )
            return JSONResponse({"type": 4, "data": {"content": "✅ Rejection recorded.", "flags": 1 << 6}})

        # WFH rejection modal
        if modal_custom_id.startswith("wfh_reject_reason::"):
            _, ch_id, msg_id = (modal_custom_id.split("::") + ["", "", ""])[:3]
            ch_id = ch_id or payload.get("channel_id", "")
            bot_token = BOT_TOKEN.strip()
            if not (bot_token and ch_id and msg_id):
                return JSONResponse({"type": 4, "data": {"content": "❌ Missing context to complete WFH rejection.", "flags": 1 << 6}})
            headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

            # Load original message to parse details
            get_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            r = requests.get(get_url, headers=headers, timeout=15)
            if r.status_code != 200:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Could not load original WFH message ({r.status_code}).", "flags": 1 << 6}})
            msg = r.json()
            content = msg.get("content", "") or ""

            name, date_str, wfh_reason = parse_wfh_card(content)
            decision = "Rejected"
            try:
                append_wfh_decision_row(name, date_str, wfh_reason, decision, reviewer, note=reject_note or "")
            except Exception as e:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Failed to record WFH rejection. {type(e).__name__}: {e}", "flags": 1 << 6}})

            new_content = (
                content
                + f"\n\n**Status:** {decision} by **{reviewer}** at **{get_ist_timestamp()} IST**"
                + (f"\n📝 **Rejection Note:** {reject_note}" if reject_note else "")
            )
            disabled_components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "wfh_approve", "disabled": True},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "wfh_reject",  "disabled": True},
                ]
            }]
            patch_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            pr = requests.patch(patch_url, headers=headers,
                                json={"content": new_content, "components": disabled_components},
                                timeout=15)
            if pr.status_code not in (200, 201):
                print(f"❌ Failed to edit WFH message: {pr.status_code} {pr.text}")

            combined_reason = wfh_reason + (f" | Rejection Note: {reject_note}" if reject_note else "")
            post_wfh_status_update(
                name=name, day=date_str, reason=combined_reason,
                decision=decision, reviewer=reviewer, fallback_channel_id=ch_id
            )
            return JSONResponse({"type": 4, "data": {"content": "✅ WFH rejection recorded.", "flags": 1 << 6}})
        if modal_custom_id.startswith("leave_reason::"):
            # Parse dates from modal id
            _, from_date, to_date = (modal_custom_id.split("::") + ["", "", ""])[:3]
            comps = data.get("components", []) or []
            reason_text = ""
            try:
                reason_text = comps[0]["components"][0]["value"].strip()
            except Exception:
                pass

            # Invoker name
            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            name = (user.get("global_name") or user.get("username") or "Unknown").strip()

            # Write + notify
            try:
                append_leave_row(name=name, from_date=from_date, to_date=to_date, reason=reason_text or "")
                # Notify approver with buttons (same pattern you already use)
                bot_token = BOT_TOKEN.strip()
                if bot_token:
                    content = (
                        f"📩 **Leave Request from {name}**\n"
                        f"🗓️ **From:** {from_date}\n"
                        f"🗓️ **To:** {to_date}\n"
                        f"💬 **Reason:** {reason_text or '(not provided)'}\n\n"
                        f"Please review and respond accordingly."
                    )
                    components = [{
                        "type": 1,
                        "components": [
                            {"type": 2, "style": 3, "label": "Approve", "custom_id": "leave_approve"},
                            {"type": 2, "style": 4, "label": "Reject",  "custom_id": "leave_reject"}
                        ]
                    }]
                    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
                    def post_to_channel(cid: str):
                        url = f"https://discord.com/api/v10/channels/{cid}/messages"
                        r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
                        r.raise_for_status()
                    if APPROVER_CHANNEL_ID.strip():
                        post_to_channel(APPROVER_CHANNEL_ID.strip())
                    elif APPROVER_USER_ID.strip():
                        dm = requests.post("https://discord.com/api/v10/users/@me/channels",
                                        headers=headers, json={"recipient_id": APPROVER_USER_ID.strip()}, timeout=15)
                        dm.raise_for_status()
                        dm_ch = dm.json().get("id")
                        if dm_ch: post_to_channel(dm_ch)
                    else:
                        ch_id = payload.get("channel_id")
                        if ch_id: post_to_channel(ch_id)
            except Exception as e:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Failed to record leave. {type(e).__name__}: {e}", "flags": 1 << 6}})

            return JSONResponse({"type": 4, "data": {"content": f"✅ Leave requested for **{from_date} → {to_date}**.", "flags": 1 << 6}})


    # Fallback
    return discord_response_message("Unsupported interaction type.", True)
