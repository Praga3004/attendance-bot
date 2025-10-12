# api/discord.py
from __future__ import annotations

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import os, json, time, requests, re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Any, Tuple, List

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
DISCORD_PUBLIC_KEY           = (os.environ.get("DISCORD_PUBLIC_KEY", "") or "").strip()
SHEET_ID                     = (os.environ.get("SHEET_ID", "") or "").strip()
SERVICE_ACCOUNT_JSON         = (os.environ.get("SERVICE_ACCOUNT_JSON", "") or "").strip()
BOT_TOKEN                    = (os.environ.get("BOT_TOKEN", "") or "").strip()

# Channels / roles
# Channels / roles
FINANCE_CHANNEL_ID           = (os.environ.get("FINANCE_CHANNEL_ID", "") or "").strip()
APPROVER_CHANNEL_ID          = (os.environ.get("APPROVER_CHANNEL_ID", "") or "").strip()
APPROVER_USER_ID             = (os.environ.get("APPROVER_USER_ID", "") or "").strip()
LEAVE_STATUS_CHANNEL_ID      = (os.environ.get("LEAVE_STATUS_CHANNEL_ID", "") or "").strip()
HR_ROLE_ID                   = (os.environ.get("HR_ROLE_ID", "") or "").strip()
ATTENDANCE_CHANNEL_ID        = (os.environ.get("ATTENDANCE_CHANNEL_ID", "") or "").strip()
CONTENT_REQUESTS_CHANNEL_ID  = (os.environ.get("CONTENT_REQUESTS_CHANNEL_ID", "") or "").strip()
ASSETS_REVIEWS_CHANNEL_ID    = (os.environ.get("ASSETS_REVIEWS_CHANNEL_ID", "") or "").strip()
LEAVE_REQUESTS_CHANNEL_ID    = (os.environ.get("LEAVE_REQUESTS_CHANNEL_ID", "") or "").strip()
CONTENT_TEAM_CHANNEL_ID      = (os.environ.get("CONTENT_TEAM_CHANNEL_ID", "") or "").strip()

# ========= CONSTANT SHEET RANGES =========
# We always read/write A:E so we can store UserID + Progress
ATTENDANCE_READ_RANGE  = "Attendance!A:E"
ATTENDANCE_WRITE_RANGE = "Attendance!A:E"

# Where each command is allowed to be invoked
CMD_ALLOWED_CHANNELS = {
    "leaverequest":  {LEAVE_REQUESTS_CHANNEL_ID},
    "wfh":           {LEAVE_REQUESTS_CHANNEL_ID},
    "leavecount":    {LEAVE_REQUESTS_CHANNEL_ID},
    "attendance":    {ATTENDANCE_CHANNEL_ID},
    "contentrequest": {CONTENT_REQUESTS_CHANNEL_ID},
    "assetreview":    {ASSETS_REVIEWS_CHANNEL_ID},
}
CHANNEL_LABELS = {
    LEAVE_REQUESTS_CHANNEL_ID: "#leave-requests",
    ATTENDANCE_CHANNEL_ID: "#attendance",
    CONTENT_REQUESTS_CHANNEL_ID: "#content-requests",
    ASSETS_REVIEWS_CHANNEL_ID: "#assets-reviews",
    CONTENT_TEAM_CHANNEL_ID: "#content-team",
}
CHANNEL_LABELS.update({
    FINANCE_CHANNEL_ID: "#finance",
})
CMD_ALLOWED_CHANNELS.update({
    "recordinvoice": {FINANCE_CHANNEL_ID},
    "clearinvoice":  {FINANCE_CHANNEL_ID},
    "viewinvoice":   {FINANCE_CHANNEL_ID},
    "viewfinstatus": {FINANCE_CHANNEL_ID},
    "recordtax":     {FINANCE_CHANNEL_ID},
})
INVOICES_RANGE        = "'Invoices'!A:E"        
INVOICE_CLEARS_RANGE  = "'Invoice Clears'!A:D"  
TAXES_RANGE           = "'Taxes'!A:E"           

def _get_opt(opts_list, name: str, default: str = "") -> str:
    """Case-insensitive option getter for slash command options."""
    for o in (opts_list or []):
        if (o.get("name") or "").lower() == name.lower():
            return (o.get("value") or "").strip()
    return default

def _to_number(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def append_invoice_row(company: str, invoice_no: str, value: str, comments: str) -> None:
    service = get_service()
    values = [[get_ist_timestamp(), company, invoice_no, _to_number(value), comments or ""]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=INVOICES_RANGE,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def append_invoice_clear_row(invoice_no: str, cleared_value: str, comments: str) -> None:
    service = get_service()
    values = [[get_ist_timestamp(), invoice_no, _to_number(cleared_value), comments or ""]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=INVOICE_CLEARS_RANGE,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def append_tax_row(invoice_no: str, tax_type: str, tax_value: str, comments: str) -> None:
    service = get_service()
    values = [[get_ist_timestamp(), invoice_no, tax_type, _to_number(tax_value), comments or ""]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=TAXES_RANGE,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def fetch_invoices():
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=INVOICES_RANGE,
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="SERIAL_NUMBER",
    ).execute()
    return resp.get("values", []) or []

def fetch_invoice_clears():
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=INVOICE_CLEARS_RANGE,
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="SERIAL_NUMBER",
    ).execute()
    return resp.get("values", []) or []

def fetch_taxes():
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=TAXES_RANGE,
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="SERIAL_NUMBER",
    ).execute()
    return resp.get("values", []) or []

def compute_fin_status():
    """Returns (total_invoiced, total_cleared, outstanding_total, taxes_by_type dict, outstanding_by_invoice dict)."""
    inv = fetch_invoices()
    cl  = fetch_invoice_clears()
    tx  = fetch_taxes()

    # Skip header if present (detect by string in value col)
    inv_start = 1 if inv and (len(inv[0])>=4 and isinstance(inv[0][3], str)) else 0
    cl_start  = 1 if cl  and (len(cl[0]) >=3 and isinstance(cl[0][2], str)) else 0
    tx_start  = 1 if tx  and (len(tx[0]) >=4 and isinstance(tx[0][3], str)) else 0

    totals_by_invoice = {}
    for r in inv[inv_start:]:
        if len(r) < 4: 
            continue
        inv_no = str(r[2]).strip()
        val = _to_number(r[3])
        totals_by_invoice[inv_no] = totals_by_invoice.get(inv_no, 0.0) + val

    cleared_by_invoice = {}
    for r in cl[cl_start:]:
        if len(r) < 3: 
            continue
        inv_no = str(r[1]).strip()
        val = _to_number(r[2])
        cleared_by_invoice[inv_no] = cleared_by_invoice.get(inv_no, 0.0) + val

    taxes_by_type = {}
    for r in tx[tx_start:]:
        if len(r) < 4: 
            continue
        tax_type = str(r[2]).strip() or "Unspecified"
        val = _to_number(r[3])
        taxes_by_type[tax_type] = taxes_by_type.get(tax_type, 0.0) + val

    outstanding_by_invoice = {}
    for inv_no, total in totals_by_invoice.items():
        cleared = cleared_by_invoice.get(inv_no, 0.0)
        outstanding_by_invoice[inv_no] = max(total - cleared, 0.0)

    total_invoiced = sum(totals_by_invoice.values())
    total_cleared  = sum(cleared_by_invoice.values())
    outstanding_total = max(total_invoiced - total_cleared, 0.0)

    return total_invoiced, total_cleared, outstanding_total, taxes_by_type, outstanding_by_invoice

def _get_attachment_from_options(interaction_payload: dict, option_name: str):
    """
    Returns (filename, url, content_type, size) for the attachment option.
    Discord sends attachment IDs in `data.options` values and full objects in `data.resolved.attachments`.
    """
    data = interaction_payload.get("data", {}) or {}
    options = data.get("options", []) or []
    resolved = data.get("resolved", {}) or {}
    atts = resolved.get("attachments", {}) or {}

    att_id = None
    for opt in options:
        if opt.get("name") == option_name:
            att_id = opt.get("value")
            break
    if not att_id:
        return None

    a = atts.get(str(att_id)) or {}
    return (
        a.get("filename"),
        a.get("url"),
        a.get("content_type"),
        a.get("size"),
    )
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

# ========= Small helpers =========
def channel_allowed(cmd: str, cid: str) -> bool:
    allowed = CMD_ALLOWED_CHANNELS.get(cmd.lower(), set())
    return bool(cid) and cid in allowed

def deny_wrong_channel(cmd: str, cid: str):
    allowed = [c for c in CMD_ALLOWED_CHANNELS.get(cmd.lower(), set()) if c]
    if not allowed:
        where = "the configured channel"
    else:
        where = " or ".join(CHANNEL_LABELS.get(c, f"<#{c}>") for c in allowed)
    msg = f"⛔ **/{cmd}** isn’t allowed here. Use it in {where}."
    return discord_response_message(msg, True)


def _post_to_channel(cid: str, content: str):
    if not (BOT_TOKEN and cid and content):
        return False
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{cid}/messages"
    try:
        r = requests.post(url, headers=headers, json={
            "content": content,
            "allowed_mentions": {"parse": []}
        }, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ post_to_channel({cid}) failed: {e}")
        return False

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

# ========= Timestamp parsing (supports Google Sheets serials) =========
_SHEETS_EPOCH = datetime(1899, 12, 30)  # Google Sheets epoch (day 0)

def _sheets_serial_to_dt_ist(value: Any) -> datetime | None:
    """Convert Google Sheets numeric date/time serial -> IST-aware datetime."""
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None
    dt = _SHEETS_EPOCH + timedelta(days=days)
    return dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))

def _ts_cell_to_date_ist(ts_val: Any) -> date | None:
    # 1) Try numeric serial first
    dt = _sheets_serial_to_dt_ist(ts_val)
    if dt:
        return dt.date()

    s = ("" if ts_val is None else str(ts_val)).strip()
    if not s:
        return None

    # 2) Try strict known formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).date()
        except Exception:
            pass

    # 3) Try ISO 8601
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        # If it has tz info, convert to IST before taking date
        if dt.tzinfo:
            dt = dt.astimezone(ZoneInfo("Asia/Kolkata"))
        return dt.date()
    except Exception:
        pass

    # 4) Try common locale like DD/MM/YYYY
    for fmt in ("%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass

    return None


# ========= ATTENDANCE =========
def fetch_attendance_rows() -> List[List[str]]:
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=ATTENDANCE_READ_RANGE,
        valueRenderOption="UNFORMATTED_VALUE",   # << get raw serials/numbers
        dateTimeRenderOption="SERIAL_NUMBER", 
    ).execute()
    return resp.get("values", []) or []

def _row_matches_user(row: List[str], target_name: str, target_user_id: str) -> bool:
    # row: [ts, name, action, user_id?, progress?]
    uid = (row[3] if len(row) > 3 else "").strip()
    nm  = (row[1] if len(row) > 1 else "").strip()
    if uid and target_user_id:
        return uid == target_user_id
    return (nm or "").strip().lower() == (target_name or "").strip().lower()

def get_today_status(name: str, user_id: str) -> Tuple[bool, bool]:
    """Returns (has_login_today, has_logout_today) for this user."""
    rows = fetch_attendance_rows()
    tday = today_ist_date()
    has_login = has_logout = False
    for r in rows:
        if len(r) < 3:
            print("r<3")
            continue
        if not _row_matches_user(r, name, user_id):
            print("r,uname,user_id")
            continue
        d = _ts_cell_to_date_ist(r[0])
        print(r[0])
        if d != tday:
            print(f"d!=tday {d} {tday}")
            continue
        a = (r[2] or "").strip().lower()
        if a == "login":
            print("login")
            has_login = True
        elif a == "logout":
            print("logout")
            has_logout = True
        if has_login and has_logout:
            print("Both")
            break
    return has_login, has_logout
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
def append_attendance_row(name: str, action: str, user_id: str, progress: str | None = None) -> None:
    """
    Writes: [=NOW(), name, action, user_id, progress]
    """
    service = get_service()
    values = [["=NOW()", name, action, user_id or "", (progress or "").strip()]]
    body = {"values": values}
    
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=ATTENDANCE_WRITE_RANGE,
        valueInputOption="USER_ENTERED",       # evaluate =NOW() in sheet's TZ
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def broadcast_attendance(name: str, action: str, user_id: str, fallback_channel_id: str | None, progress: str | None = None):
    if not BOT_TOKEN:
        return False
    channel_id = (ATTENDANCE_CHANNEL_ID or (fallback_channel_id or ""))
    if not channel_id:
        return False

    role_ping = f"<@&{HR_ROLE_ID}>" if HR_ROLE_ID else "HR"
    user_ping = f"<@{user_id}>" if user_id else name
    icon = "🟢" if action.lower() == "login" else "🔴"

    content = (
        f"{icon} **Attendance**\n"
        f"👤 {user_ping} — **{name}**\n"
        f"🕒 {get_ist_timestamp()} IST\n"
        f"📝 Action: **{action}**"
    )
    if action.lower() == "logout" and (progress or "").strip():
        content += f"\n📈 **Daily Progress:** {progress.strip()}"
    content += f"\n{role_ping} please take note."

    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://example.com, 1.0)",
    }

    try:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        body = {
            "content": content,
            "allowed_mentions": {
                "parse": [],
                "roles": [HR_ROLE_ID] if HR_ROLE_ID else [],
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
                if action.lower() == "logout" and (progress or "").strip():
                    dm_msg += f"\n📈 Progress: {progress.strip()}"
                requests.post(
                    f"https://discord.com/api/v10/channels/{dm_ch}/messages",
                    headers=headers,
                    json={"content": dm_msg},
                    timeout=15,
                )
    except Exception as e:
        print(f"⚠️ Attendance DM failed: {e}")
    return True

# ========= LEAVE / WFH & Content/Asset helpers (unchanged logic from your last file) =========
def _md_link_parts(line: str) -> tuple[str, str]:
    m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", line or "")
    return (m.group(1), m.group(2)) if m else ("", "")

def _grab(prefix: str, text: str) -> str:
    if prefix in (text or ""):
        after = text.split(prefix, 1)[1]
        return after.split("\n", 1)[0].strip()
    return ""

def parse_content_request_card(content: str) -> tuple[str, str, str, str]:
    first = (content.split("\n", 1)[0] if content else "").strip()
    requester = first
    for marker in ["**Content Request from ", "Content Request from ", "📝 **Content Request from "]:
        if marker in requester:
            requester = requester.split(marker, 1)[1]
            break
    requester = requester.strip("* ").strip()

    topic_line = _grab("**Topic:** ", content) or _grab("Topic:", content)
    file_line  = _grab("**File:** ", content)  or _grab("File:", content)
    filename, file_url = _md_link_parts(file_line)
    return requester, topic_line, filename, file_url

def parse_asset_review_card(content: str) -> tuple[str, str, str, str]:
    first = (content.split("\n", 1)[0] if content else "").strip()
    requester = first
    for marker in ["**Asset Review Request from ", "Asset Review Request from ", "🧪 **Asset Review Request from "]:
        if marker in requester:
            requester = requester.split(marker, 1)[1]
            break
    requester = requester.strip("* ").strip()

    asset_name = _grab("**Name:** ", content) or _grab("Name:", content)
    file_line  = _grab("**File:** ", content) or _grab("File:", content)
    filename, file_url = _md_link_parts(file_line)
    return requester, asset_name, filename, file_url

def append_content_decision_row_from_card(card_content: str, decision: str, reviewer: str, comments: str = "") -> None:
    requester, topic, filename, file_url = parse_content_request_card(card_content)
    service = get_service()
    values = [[
        get_ist_timestamp(), decision, reviewer, requester, topic, filename, file_url, comments or ""
    ]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'Content Decisions'!A:H",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def append_asset_decision_row_from_card(card_content: str, decision: str, reviewer: str, comments: str = "") -> None:
    requester, asset_name, filename, file_url = parse_asset_review_card(card_content)
    service = get_service()
    values = [[
        get_ist_timestamp(), decision, reviewer, requester, asset_name, filename, file_url, comments or ""
    ]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="'Asset Decisions'!A:H",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

def post_leave_status_update(name: str, from_date: str, to_date: str, reason: str,
                             decision: str, reviewer: str, fallback_channel_id: str | None):
    status_channel_id = (LEAVE_STATUS_CHANNEL_ID or APPROVER_CHANNEL_ID or (fallback_channel_id or ""))
    if not (BOT_TOKEN and status_channel_id):
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
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
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

def fetch_leave_decisions_rows() -> List[List[str]]:
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="'Leave Decisions'!A:G"
    ).execute()
    return resp.get("values", []) or []

def count_user_leaves_current_month(target_name: str) -> tuple[int, int, List[tuple[date, date, int]]]:
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
    details: List[tuple[date, date, int]] = []
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
    status_channel_id = (LEAVE_STATUS_CHANNEL_ID or APPROVER_CHANNEL_ID or (fallback_channel_id or ""))
    if not (BOT_TOKEN and status_channel_id):
        return False
    icon = "🏠✅" if decision.lower() == "approved" else "🏠❌"
    content = (
        f"{icon} **WFH {decision}**\n"
        f"👤 **Employee:** {name}\n"
        f"📅 **Date:** {day}\n"
        f"💬 **Reason:** {reason}\n"
        f"🧑‍💼 **Reviewer:** {reviewer} — **{get_ist_timestamp()} IST**"
    )
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{status_channel_id}/messages"
    try:
        r = requests.post(url, headers=headers, json={"content": content}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ WFH status post failed: {e}")
        return False

def send_leave_from_picker(channel_id: str) -> bool:
    if not (BOT_TOKEN and channel_id):
        return False
    opts = _date_opts(today_ist_date(), 25)
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
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    r = requests.post(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return True

def send_wfh_date_picker(channel_id: str):
    """Shows a string select with next 14 days."""
    if not (BOT_TOKEN and channel_id):
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
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    r = requests.post(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return True

def _date_opts(start: date, days: int) -> List[dict]:
    days = max(0, min(days, 25))  # Discord limit
    return [{
        "label": f"{(start + timedelta(i)).isoformat()} ({(start + timedelta(i)).strftime('%a')})",
        "value": (start + timedelta(i)).isoformat()
    } for i in range(days)]

def _grab_between(prefix: str, text: str) -> str:
    if prefix in text:
        after = text.split(prefix, 1)[1]
        return after.split("\n", 1)[0].strip()
    return ""

def parse_wfh_card(content: str) -> tuple[str, str, str]:
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

        # --- /wfh date autocomplete ---
        if cmd_name == "wfh" and focused and focused.get("name") == "date":
            now_ist = today_ist_date()
            choices = []
            for i in range(0, 14):
                d = now_ist + timedelta(days=i)
                label = f"{d.isoformat()} ({d.strftime('%a')})"
                choices.append({"name": label, "value": d.isoformat()})
            return JSONResponse({"type": 8, "data": {"choices": choices}})

        # --- /leaverequest from/to autocomplete ---
        if cmd_name == "leaverequest" and focused:
            channel_id = payload.get("channel_id", "")
            if not channel_allowed(cmd_name, channel_id):
                return JSONResponse({"type": 8, "data": {"choices": []}})

            fname = focused.get("name")
            opts_map = {o.get("name"): (o.get("value") or "") for o in (data.get("options") or [])}

            if fname == "from":
                start = today_ist_date()
                choices = []
                for i in range(25):
                    d = start + timedelta(days=i)
                    label = f"{d.isoformat()} ({d.strftime('%a')})"
                    choices.append({"name": label, "value": d.isoformat()})
                return JSONResponse({"type": 8, "data": {"choices": choices}})

            if fname == "to":
                from_str = (opts_map.get("from") or "").strip()
                try:
                    from_dt = datetime.strptime(from_str, "%Y-%m-%d").date() if from_str else today_ist_date()
                except Exception:
                    from_dt = today_ist_date()
                start = from_dt
                choices = []
                for i in range(25):
                    d = start + timedelta(days=i)
                    label = f"{d.isoformat()} ({d.strftime('%a')})"
                    choices.append({"name": label, "value": d.isoformat()})
                return JSONResponse({"type": 8, "data": {"choices": choices}})

        # default: no choices
        return JSONResponse({"type": 8, "data": {"choices": []}})

    # 2) APPLICATION_COMMAND
    if t == 2:
        data = payload.get("data", {}) or {}
        cmd_name = data.get("name", "")
        channel_id = payload.get("channel_id", "")
                # ----- RECORD INVOICE -----
        
        # ----- ATTENDANCE -----
        if cmd_name == "attendance":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)

            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            user_id = (user.get("id") or "").strip()
            name = (user.get("global_name") or user.get("username") or "Unknown").strip()

            try:
                has_login, has_logout = get_today_status(name, user_id)
            except Exception as e:
                return discord_response_message(f"❌ Could not read attendance. {type(e).__name__}: {e}", True)

            # 1) no login yet -> record LOGIN
            if not has_login:
                try:
                    append_attendance_row(name=name, action="Login", user_id=user_id)
                    broadcast_attendance(name=name, action="Login", user_id=user_id, fallback_channel_id=channel_id)
                except Exception as e:
                    return discord_response_message(f"❌ Failed to record login. {type(e).__name__}: {e}", True)
                return discord_response_message(f"🟢 ✅ Recorded **Login** for **{name}** • 🕒 {get_ist_timestamp()} IST", True)

            # 2) login exists, no logout -> open modal for progress, then record LOGOUT on submit
            if has_login and not has_logout:
                modal_id = f"att_logout_progress::{user_id}"
                return JSONResponse({
                    "type": 9,  # MODAL
                    "data": {
                        "custom_id": modal_id,
                        "title": "Daily progress (required for logout)",
                        "components": [{
                            "type": 1,
                            "components": [{
                                "type": 4,  # TEXT_INPUT
                                "custom_id": "progress_text",
                                "style": 2,  # PARAGRAPH
                                "label": "What did you complete today?",
                                "min_length": 1,
                                "max_length": 2000,
                                "required": True,
                                "placeholder": "Tasks done, blockers, key updates…"
                            }]
                        }]
                    }
                })

            # 3) already both recorded
            return discord_response_message("ℹ️ You’ve already recorded **Login** and **Logout** for today.", True)

        # ----- CONTENT REQUEST -----
        if cmd_name == "contentrequest":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)

            topic = ""
            data_opts = data.get("options", []) or []
            for opt in data_opts:
                if opt.get("name") == "topic":
                    topic = (opt.get("value") or "").strip()
            att = _get_attachment_from_options(payload, "files")
            if not topic or not att:
                return discord_response_message("❌ Provide a **topic** and attach a **file**.", True)

            filename, file_url, content_type, size = att
            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            requester = (user.get("global_name") or user.get("username") or "Unknown").strip()

            if not (BOT_TOKEN and CONTENT_REQUESTS_CHANNEL_ID):
                return discord_response_message("❌ Server not configured for content requests.", True)

            content = (
                f"📝 **Content Request from {requester}**\n"
                f"📌 **Topic:** {topic}\n"
                f"📎 **File:** [{filename}]({file_url})\n\n"
                f"Please review and respond."
            )
            components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "cr_approve"},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "cr_reject"},
                ]
            }]

            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
            url = f"https://discord.com/api/v10/channels/{CONTENT_REQUESTS_CHANNEL_ID}/messages"
            try:
                r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
                r.raise_for_status()
            except Exception as e:
                return discord_response_message(f"❌ Could not post to content-requests. {type(e).__name__}: {e}", True)

            return discord_response_message("✅ Sent to **#content-requests** for review.", True)
        if cmd_name == "recordinvoice":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
            opts = data.get("options", []) or []
            company  = _get_opt(opts, "companyname")
            inv_no   = _get_opt(opts, "invoicenumber")
            inv_val  = _get_opt(opts, "invoicevalue")
            comments = _get_opt(opts, "comments")
            if not (company and inv_no and inv_val):
                return discord_response_message("❌ Missing fields. Required: CompanyName, InvoiceNumber, InvoiceValue.", True)
            try:
                append_invoice_row(company, inv_no, inv_val, comments)
            except Exception as e:
                return discord_response_message(f"❌ Failed to record invoice. {type(e).__name__}: {e}", True)
            return discord_response_message(f"✅ Invoice **{inv_no}** recorded for **{company}** (₹{_to_number(inv_val):,.2f}).", True)

        # ----- CLEAR INVOICE (RECEIPT) -----
        if cmd_name == "clearinvoice":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
            opts = data.get("options", []) or []
            inv_no   = _get_opt(opts, "invoicenumber")
            cleared  = _get_opt(opts, "valuecleared")
            comments = _get_opt(opts, "comments")
            if not (inv_no and cleared):
                return discord_response_message("❌ Missing fields. Required: InvoiceNumber, ValueCleared.", True)
            try:
                append_invoice_clear_row(inv_no, cleared, comments)
            except Exception as e:
                return discord_response_message(f"❌ Failed to record clearance. {type(e).__name__}: {e}", True)
            return discord_response_message(f"✅ Recorded ₹{_to_number(cleared):,.2f} cleared for **{inv_no}**.", True)

        # ----- VIEW INVOICE (list) -----
        if cmd_name == "viewinvoice":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
            try:
                inv = fetch_invoices()
                cl  = fetch_invoice_clears()
            except Exception as e:
                return discord_response_message(f"❌ Could not load invoices. {type(e).__name__}: {e}", True)

            # Build maps
            inv_start = 1 if inv and (len(inv[0])>=4 and isinstance(inv[0][3], str)) else 0
            cl_start  = 1 if cl  and (len(cl[0]) >=3 and isinstance(cl[0][2], str)) else 0
            totals, cleared = {}, {}
            rows = []
            for r in inv[inv_start:]:
                if len(r) < 4: continue
                ts = str(r[0]); company = str(r[1]); inv_no = str(r[2]); val = _to_number(r[3])
                totals[inv_no] = totals.get(inv_no, 0.0) + val
                rows.append((ts, company, inv_no, val))
            for r in cl[cl_start:]:
                if len(r) < 3: continue
                inv_no = str(r[1]); val = _to_number(r[2])
                cleared[inv_no] = cleared.get(inv_no, 0.0) + val

            # Compose a compact list (max 10)
            lines = []
            for i, (ts, company, inv_no, val) in enumerate(rows[:10], 1):
                out = max(totals.get(inv_no,0.0) - cleared.get(inv_no,0.0), 0.0)
                lines.append(f"{i}. **{inv_no}** — {company} • ₹{val:,.2f} • Outst.: ₹{out:,.2f}")
            extra = f"\n…plus {max(len(rows)-10,0)} more." if len(rows) > 10 else ""
            msg = "🧾 **Invoices**\n" + ("\n".join(lines) if lines else "No invoices found.") + extra
            return discord_response_message(msg, True)

        # ----- VIEW FIN STATUS (totals & taxes) -----
        if cmd_name == "viewfinstatus":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
            try:
                total_inv, total_cl, outstanding, taxes_by_type, _ = compute_fin_status()
            except Exception as e:
                return discord_response_message(f"❌ Could not compute status. {type(e).__name__}: {e}", True)

            tax_lines = [f"• {k}: ₹{v:,.2f}" for k, v in sorted(taxes_by_type.items())] or ["• (none)"]
            msg = (
                "💼 **Finance Status**\n"
                f"• Total Invoiced: **₹{total_inv:,.2f}**\n"
                f"• Total Cleared: **₹{total_cl:,.2f}**\n"
                f"• Outstanding: **₹{outstanding:,.2f}**\n\n"
                "🧾 **Taxes recorded (by type)**\n" + "\n".join(tax_lines)
            )
            return discord_response_message(msg, True)

        # ----- RECORD TAX -----
        if cmd_name == "recordtax":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
            opts = data.get("options", []) or []
            inv_no   = _get_opt(opts, "invoicenumber")
            tax_type = _get_opt(opts, "taxtype")
            tax_val  = _get_opt(opts, "taxvalue")
            comments = _get_opt(opts, "comments")
            if not (inv_no and tax_type and tax_val):
                return discord_response_message("❌ Missing fields. Required: InvoiceNumber, TaxType, TaxValue.", True)
            try:
                append_tax_row(inv_no, tax_type, tax_val, comments)
            except Exception as e:
                return discord_response_message(f"❌ Failed to record tax. {type(e).__name__}: {e}", True)
            return discord_response_message(f"✅ Tax recorded for **{inv_no}** — {tax_type} ₹{_to_number(tax_val):,.2f}.", True)

        # ----- ASSET REVIEW -----
        if cmd_name == "assetreview":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)

            asset_name = ""
            data_opts = data.get("options", []) or []
            for opt in data_opts:
                if opt.get("name") == "name":
                    asset_name = (opt.get("value") or "").strip()
            att = _get_attachment_from_options(payload, "file")
            if not asset_name or not att:
                return discord_response_message("❌ Provide **name** and attach a **file**.", True)

            filename, file_url, content_type, size = att
            member = payload.get("member", {}) or {}
            user = member.get("user", {}) or payload.get("user", {}) or {}
            requester = (user.get("global_name") or user.get("username") or "Unknown").strip()

            if not (BOT_TOKEN and ASSETS_REVIEWS_CHANNEL_ID):
                return discord_response_message("❌ Server not configured for asset reviews.", True)

            content = (
                f"🧪 **Asset Review Request from {requester}**\n"
                f"🏷️ **Name:** {asset_name}\n"
                f"📎 **File:** [{filename}]({file_url})\n\n"
                f"Please review and respond."
            )
            components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "ar_approve"},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "ar_reject"},
                ]
            }]

            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
            url = f"https://discord.com/api/v10/channels/{ASSETS_REVIEWS_CHANNEL_ID}/messages"
            try:
                r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
                r.raise_for_status()
            except Exception as e:
                return discord_response_message(f"❌ Could not post to assets-reviews. {type(e).__name__}: {e}", True)

            return discord_response_message("✅ Sent to **#assets-reviews** for verification.", True)

        # ----- LEAVE COUNT -----
        if cmd_name == "leavecount":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
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
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
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
                # Notify approver with buttons
                if BOT_TOKEN:
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
                    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
                    def post_to_channel(cid: str):
                        url = f"https://discord.com/api/v10/channels/{cid}/messages"
                        r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
                        r.raise_for_status()
                    if APPROVER_CHANNEL_ID:
                        post_to_channel(APPROVER_CHANNEL_ID)
                    elif APPROVER_USER_ID:
                        dm = requests.post("https://discord.com/api/v10/users/@me/channels",
                                           headers=headers, json={"recipient_id": APPROVER_USER_ID}, timeout=15)
                        dm.raise_for_status()
                        dm_ch = dm.json().get("id")
                        if dm_ch: post_to_channel(dm_ch)
                    else:
                        post_to_channel(channel_id)
            except Exception as e:
                return discord_response_message(f"❌ Failed to record leave. {type(e).__name__}: {e}", True)

            return discord_response_message(
                f"✅ Leave request submitted by **{name}** from **{from_opt}** to **{to_opt}**.\nReason: {reason_opt or '(not provided)'}",
                True
            )

        # ----- WFH -----
        if cmd_name == "wfh":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
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
            if BOT_TOKEN:
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
                headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
                def post_to_channel(cid: str):
                    url = f"https://discord.com/api/v10/channels/{cid}/messages"
                    r = requests.post(url, headers=headers, json={"content": content, "components": components}, timeout=15)
                    r.raise_for_status()
                try:
                    if APPROVER_CHANNEL_ID:
                        post_to_channel(APPROVER_CHANNEL_ID)
                    elif APPROVER_USER_ID:
                        dm = requests.post("https://discord.com/api/v10/users/@me/channels",
                                           headers=headers, json={"recipient_id": APPROVER_USER_ID}, timeout=15)
                        dm.raise_for_status()
                        dm_ch = dm.json().get("id")
                        if dm_ch: post_to_channel(dm_ch)
                    else:
                        post_to_channel(channel_id)
                except Exception as e:
                    print(f"⚠️ Could not notify approver for WFH: {e}")

            return discord_response_message(
                f"✅ WFH request submitted for **{day}**.\nReason: {reason or '(not provided)'}",
                True
            )

        # ----- SCHEDULE MEET -----
        if cmd_name == "schedulemeet":
            if not channel_allowed(cmd_name, channel_id):
                return deny_wrong_channel(cmd_name, channel_id)
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
            to_opts = _date_opts(from_dt, 25)
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
            _, from_date = custom_id.split("::", 1)
            values = data.get("values") or []
            to_date = values[0] if values else None
            if not to_date:
                return JSONResponse({"type": 4, "data": {"content": "❌ No end date selected.", "flags": 1 << 6}})
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

        # ---- Content request approve/reject
        if custom_id in ("cr_approve", "cr_reject"):
            ch_id  = payload.get("channel_id", "")
            msg_id = message.get("id", "")
            modal_id = ("cr_approve_reason" if custom_id == "cr_approve" else "cr_reject_reason") + f"::{ch_id}::{msg_id}"
            title = "Approve Content (add improvement notes)" if custom_id == "cr_approve" else "Reject Content (add reason)"
            label = "Improvement comments" if custom_id == "cr_approve" else "Rejection comments"
            return JSONResponse({
                "type": 9,  # MODAL
                "data": {
                    "custom_id": modal_id,
                    "title": title,
                    "components": [{
                        "type": 1,
                        "components": [{
                            "type": 4,  # TEXT_INPUT
                            "custom_id": "comments",
                            "style": 2,  # PARAGRAPH
                            "label": label,
                            "min_length": 1, "max_length": 1000, "required": True,
                            "placeholder": "Write your feedback here"
                        }]
                    }]
                }
            })

        # ---- Asset review approve/reject
        if custom_id in ("ar_approve", "ar_reject"):
            ch_id  = payload.get("channel_id", "")
            msg_id = message.get("id", "")
            modal_id = ("ar_approve_reason" if custom_id == "ar_approve" else "ar_reject_reason") + f"::{ch_id}::{msg_id}"
            title = "Approve Asset (add improvement notes)" if custom_id == "ar_approve" else "Reject Asset (add reason)"
            label = "Improvement comments" if custom_id == "ar_approve" else "Rejection comments"
            return JSONResponse({
                "type": 9,  # MODAL
                "data": {
                    "custom_id": modal_id,
                    "title": title,
                    "components": [{
                        "type": 1,
                        "components": [{
                            "type": 4,
                            "custom_id": "comments",
                            "style": 2,
                            "label": label,
                            "min_length": 1, "max_length": 1000, "required": True,
                            "placeholder": "Write your feedback here"
                        }]
                    }]
                }
            })

        # Fallback for unknown buttons/selects
        return JSONResponse({
            "type": 4,
            "data": {"content": f"Unsupported action for button id `{custom_id}`.", "flags": 1 << 6}
        })

    # 4) MODAL_SUBMIT (Attendance Logout, Leave/Content/Asset/WFH reject flows)
    if t == 5:
        data = payload.get("data", {}) or {}
        modal_custom_id = data.get("custom_id", "")
        comps = data.get("components", []) or []

        # Reviewer / actor (for attendance logout it's the same person)
        member = payload.get("member", {}) or {}
        user = member.get("user", {}) or payload.get("user", {}) or {}
        reviewer = (user.get("global_name") or user.get("username") or "Unknown").strip()
        user_id = (user.get("id") or "").strip()
        channel_id = payload.get("channel_id", "")

        # ===== Attendance: logout progress modal =====
        if modal_custom_id.startswith("att_logout_progress::"):
            _, expected_uid = modal_custom_id.split("::", 1)
            if expected_uid and expected_uid != user_id:
                return JSONResponse({"type": 4, "data": {"content": "❌ This modal isn’t for you.", "flags": 1 << 6}})
        
            progress = ""
            try:
                progress = comps[0]["components"][0]["value"].strip()
            except Exception:
                progress = ""

            # Double-check state (avoid duplicates)
            has_login, has_logout = get_today_status(reviewer, user_id)
            if not has_login:
                return JSONResponse({"type": 4, "data": {"content": "⚠️ No **Login** found for today. Please log in first.", "flags": 1 << 6}})
            if has_logout:
                return JSONResponse({"type": 4, "data": {"content": "ℹ️ **Logout** already recorded for today.", "flags": 1 << 6}})

            try:
                append_attendance_row(name=reviewer, action="Logout", user_id=user_id, progress=progress)
                broadcast_attendance(name=reviewer, action="Logout", user_id=user_id, fallback_channel_id=channel_id, progress=progress)
            except Exception as e:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Failed to record logout. {type(e).__name__}: {e}", "flags": 1 << 6}})
            return JSONResponse({"type": 4, "data": {"content": "🔴 ✅ **Logout** recorded with your daily progress. Have a good one!", "flags": 1 << 6}})

        # ===== The rest reuse your existing flows =====
        # Content/Asset/WFH/Leave modals
        reject_note = ""
        try:
            reject_note = comps[0]["components"][0]["value"].strip()
        except Exception:
            pass

        # Leave rejection modal
        if modal_custom_id.startswith("reject_reason::"):
            _, ch_id, msg_id = (modal_custom_id.split("::") + ["", "", ""])[:3]
            ch_id = ch_id or payload.get("channel_id", "")
            if not (BOT_TOKEN and ch_id and msg_id):
                return JSONResponse({"type": 4, "data": {"content": "❌ Missing context to complete rejection.", "flags": 1 << 6}})
            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

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

        # ---- Content request modal submit (Approve/Reject) ----
        if modal_custom_id.startswith(("cr_approve_reason::", "cr_reject_reason::")):
            _, ch_id, msg_id = (modal_custom_id.split("::") + ["", "", ""])[:3]
            comment = reject_note
            if not (BOT_TOKEN and ch_id and msg_id):
                return JSONResponse({"type": 4, "data": {"content": "❌ Missing context.", "flags": 1 << 6}})
            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

            # Load the original card to keep content & disable buttons
            get_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            r = requests.get(get_url, headers=headers, timeout=15)
            if r.status_code != 200:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Could not load message ({r.status_code}).", "flags": 1 << 6}})
            msg = r.json()
            content = msg.get("content", "") or ""

            decision = "Approved" if modal_custom_id.startswith("cr_approve_reason::") else "Rejected"

            new_content = (
                content
                + f"\n\n**Status:** {decision} by **{reviewer}** at **{get_ist_timestamp()} IST**"
                + (f"\n📝 **Comments:** {comment}" if comment else "")
            )
            disabled_components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "cr_approve", "disabled": True},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "cr_reject",  "disabled": True},
                ]
            }]

            patch_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            pr = requests.patch(patch_url, headers=headers, json={"content": new_content, "components": disabled_components}, timeout=15)
            if pr.status_code not in (200, 201):
                print(f"❌ Failed to edit message: {pr.status_code} {pr.text}")

            # Log to Sheets
            append_content_decision_row_from_card(content, decision, reviewer, comment)

            # Also notify content-team
            if CONTENT_TEAM_CHANNEL_ID:
                req, topic, filename, file_url = parse_content_request_card(content)
                team_msg = (
                    "📣 **Content Request Decision**\n"
                    f"🧑‍💼 **Reviewer:** {reviewer}\n"
                    f"✅❌ **Decision:** {decision}"
                    + (f"\n📝 **Comments:** {comment}" if comment else "")
                    + f"\n👤 **Requester:** {req}"
                    + f"\n📌 **Topic:** {topic}"
                    + f"\n📎 **File:** [{filename}]({file_url})"
                )
                _post_to_channel(CONTENT_TEAM_CHANNEL_ID, team_msg)

            return JSONResponse({"type": 4, "data": {"content": "✅ Decision recorded.", "flags": 1 << 6}})

        # ---- Asset review modal submit (Approve/Reject) ----
        if modal_custom_id.startswith(("ar_approve_reason::", "ar_reject_reason::")):
            _, ch_id, msg_id = (modal_custom_id.split("::") + ["", "", ""])[:3]
            comment = reject_note
            if not (BOT_TOKEN and ch_id and msg_id):
                return JSONResponse({"type": 4, "data": {"content": "❌ Missing context.", "flags": 1 << 6}})
            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

            get_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            r = requests.get(get_url, headers=headers, timeout=15)
            if r.status_code != 200:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Could not load message ({r.status_code}).", "flags": 1 << 6}})
            msg = r.json()
            content = msg.get("content", "") or ""

            decision = "Approved" if modal_custom_id.startswith("ar_approve_reason::") else "Rejected"

            new_content = (
                content
                + f"\n\n**Status:** {decision} by **{reviewer}** at **{get_ist_timestamp()} IST**"
                + (f"\n📝 **Comments:** {comment}" if comment else "")
            )
            disabled_components = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": "ar_approve", "disabled": True},
                    {"type": 2, "style": 4, "label": "Reject",  "custom_id": "ar_reject",  "disabled": True},
                ]
            }]

            patch_url = f"https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}"
            pr = requests.patch(patch_url, headers=headers, json={"content": new_content, "components": disabled_components}, timeout=15)
            if pr.status_code not in (200, 201):
                print(f"❌ Failed to edit message: {pr.status_code} {pr.text}")

            # Log to Sheets
            append_asset_decision_row_from_card(content, decision, reviewer, comment)

            # Also notify content-team
            if CONTENT_TEAM_CHANNEL_ID:
                req, asset_name, filename, file_url = parse_asset_review_card(content)
                team_msg = (
                    "📣 **Asset Review Decision**\n"
                    f"🧑‍💼 **Reviewer:** {reviewer}\n"
                    f"✅❌ **Decision:** {decision}"
                    + (f"\n📝 **Comments:** {comment}" if comment else "")
                    + f"\n👤 **Requester:** {req}"
                    + f"\n🏷️ **Asset:** {asset_name}"
                    + f"\n📎 **File:** [{filename}]({file_url})"
                )
                _post_to_channel(CONTENT_TEAM_CHANNEL_ID, team_msg)

            return JSONResponse({"type": 4, "data": {"content": "✅ Decision recorded.", "flags": 1 << 6}})

        # WFH rejection modal
        if modal_custom_id.startswith("wfh_reject_reason::"):
            _, ch_id, msg_id = (modal_custom_id.split("::") + ["", "", ""])[:3]
            ch_id = ch_id or payload.get("channel_id", "")
            if not (BOT_TOKEN and ch_id and msg_id):
                return JSONResponse({"type": 4, "data": {"content": "❌ Missing context to complete WFH rejection.", "flags": 1 << 6}})
            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

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

        # Leave modal (reason after selecting To)
        if modal_custom_id.startswith("leave_reason::"):
            _, from_date, to_date = (modal_custom_id.split("::") + ["", "", ""])[:3]
            comps2 = data.get("components", []) or []
            reason_text = ""
            try:
                reason_text = comps2[0]["components"][0]["value"].strip()
            except Exception:
                pass

            member2 = payload.get("member", {}) or {}
            user2 = member2.get("user", {}) or payload.get("user", {}) or {}
            name2 = (user2.get("global_name") or user2.get("username") or "Unknown").strip()

            try:
                append_leave_row(name=name2, from_date=from_date, to_date=to_date, reason=reason_text or "")
                if BOT_TOKEN:
                    content2 = (
                        f"📩 **Leave Request from {name2}**\n"
                        f"🗓️ **From:** {from_date}\n"
                        f"🗓️ **To:** {to_date}\n"
                        f"💬 **Reason:** {reason_text or '(not provided)'}\n\n"
                        f"Please review and respond accordingly."
                    )
                    components2 = [{
                        "type": 1,
                        "components": [
                            {"type": 2, "style": 3, "label": "Approve", "custom_id": "leave_approve"},
                            {"type": 2, "style": 4, "label": "Reject",  "custom_id": "leave_reject"}
                        ]
                    }]
                    headers2 = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
                    def post_to_channel2(cid: str):
                        url2 = f"https://discord.com/api/v10/channels/{cid}/messages"
                        r2 = requests.post(url2, headers=headers2, json={"content": content2, "components": components2}, timeout=15)
                        r2.raise_for_status()
                    if APPROVER_CHANNEL_ID:
                        post_to_channel2(APPROVER_CHANNEL_ID)
                    elif APPROVER_USER_ID:
                        dm = requests.post("https://discord.com/api/v10/users/@me/channels",
                                           headers=headers2, json={"recipient_id": APPROVER_USER_ID}, timeout=15)
                        dm.raise_for_status()
                        dm_ch = dm.json().get("id")
                        if dm_ch: post_to_channel2(dm_ch)
                    else:
                        ch_id2 = payload.get("channel_id")
                        if ch_id2: post_to_channel2(ch_id2)
            except Exception as e:
                return JSONResponse({"type": 4, "data": {"content": f"❌ Failed to record leave. {type(e).__name__}: {e}", "flags": 1 << 6}})

            return JSONResponse({"type": 4, "data": {"content": f"✅ Leave requested for **{from_date} → {to_date}**.", "flags": 1 << 6}})

    # Fallback
    return discord_response_message("Unsupported interaction type.", True)
