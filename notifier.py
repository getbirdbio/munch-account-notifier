#!/usr/bin/env python3
"""
Munch Account Debit Notifier
Checks the Getbird Birdhaven account ledger and sends WhatsApp notifications
to members when their account is debited.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────
MUNCH_API       = "https://api.munch.cloud/api"
ACCOUNT_ID      = "3e92a480-5f21-11ec-b43f-dde416ab9f61"
COMPANY_ID      = "28c5e780-3707-11ec-88a8-dde416ab9f61"

TWILIO_SID      = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM     = "whatsapp:+27600192724"
CONTENT_SID     = "HX4dadbd0969564ff4a692052268acd51b"

MUNCH_EMAIL     = os.environ["MUNCH_EMAIL"]
MUNCH_PASSWORD  = os.environ["MUNCH_PASSWORD"]

STATE_FILE      = os.path.join(os.path.dirname(__file__), "state", "notified_transactions.json")
CACHE_FILE      = os.path.join(os.path.dirname(__file__), "state", "member_phone_cache.json")

MAX_STATE_ENTRIES = 500
LOOKBACK_HOURS    = 25   # slightly over 24h for safety


# ── Helpers ────────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def format_amount(raw):
    """Turn '-R418.00' into 'R418.00' for the notification."""
    return raw.lstrip("-").strip()

def format_date(raw):
    """Turn '09/04/26 - 08:47' into '9 Apr 2026, 08:47 AM'."""
    try:
        dt = datetime.strptime(raw, "%d/%m/%y - %H:%M")
        return dt.strftime("%-d %b %Y, %I:%M %p")
    except Exception:
        return raw

def transaction_key(tx):
    return f"{tx['date']}_{tx['user']}_{tx['amount']}"

def format_phone(phone):
    """Normalise to +27... format."""
    p = str(phone).replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "+27" + p[1:]
    if not p.startswith("+"):
        p = "+" + p
    return p

def is_recent(date_str, hours=LOOKBACK_HOURS):
    """Return True if the date string is within the last N hours."""
    try:
        dt = datetime.strptime(date_str, "%d/%m/%y - %H:%M")
        # Munch dates appear to be in SAST (UTC+2)
        dt_utc = dt - timedelta(hours=2)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        return (now_utc - dt_utc) <= timedelta(hours=hours)
    except Exception:
        return True  # if we can't parse, include it


# ── Munch API ──────────────────────────────────────────────────────────────

def munch_login():
    resp = requests.post(
        f"{MUNCH_API}/auth-internal/login",
        json={"email": MUNCH_EMAIL, "password": MUNCH_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("data", {}).get("employee", {}).get("accessToken") or data.get("employee", {}).get("accessToken")
    if not token:
        raise RuntimeError(f"Login failed – no accessToken in response: {data}")
    print("✓ Munch login successful")
    return token

def munch_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def get_ledger(token):
    resp = requests.post(
        f"{MUNCH_API}/account/retrieve-ledger",
        json={
            "accountId":  ACCOUNT_ID,
            "companyId":  COMPANY_ID,
            "limit":      50,
        },
        headers=munch_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # Normalise: the API may return items in different shapes
    items = data.get("ledgerItems") or data.get("items") or data.get("transactions") or []
    print(f"✓ Ledger fetched: {len(items)} items")

    normalised = []
    for item in items:
        # Try multiple possible field name patterns
        tx_type   = item.get("transactionType") or item.get("type") or item.get("paymentMethod") or ""
        user      = item.get("user") or item.get("memberName") or item.get("customer") or ""
        if isinstance(user, dict):
            user = user.get("name") or user.get("fullName") or ""
        employee  = item.get("employee") or ""
        if isinstance(employee, dict):
            employee = employee.get("name") or employee.get("fullName") or ""
        amount    = item.get("amount") or item.get("debit") or ""
        if isinstance(amount, (int, float)):
            amount = f"R{abs(amount):.2f}" if amount < 0 else f"R{amount:.2f}"
        balance   = item.get("balance") or item.get("runningBalance") or ""
        if isinstance(balance, (int, float)):
            balance = f"R{balance:.2f}"
        date      = item.get("date") or item.get("createdAt") or item.get("transactionDate") or ""
        # Convert ISO dates to our display format if needed
        if "T" in str(date):
            try:
                dt = datetime.fromisoformat(str(date).replace("Z", "+00:00"))
                dt_sast = dt + timedelta(hours=2)
                date = dt_sast.strftime("%d/%m/%y - %H:%M")
            except Exception:
                pass

        normalised.append({
            "type":     tx_type,
            "user":     user,
            "employee": employee,
            "amount":   str(amount),
            "balance":  str(balance),
            "date":     str(date),
            "raw":      item,
        })

    return normalised

def search_members(token, name):
    resp = requests.post(
        f"{MUNCH_API}/account/retrieve-users",
        json={
            "accountId": ACCOUNT_ID,
            "companyId": COMPANY_ID,
            "search":    name,
            "limit":     10,
        },
        headers=munch_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("users") or data.get("members") or []


# ── Twilio ─────────────────────────────────────────────────────────────────

def send_whatsapp(phone, first_name, amount, date_str, balance):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    payload = {
        "From":             TWILIO_FROM,
        "To":               f"whatsapp:{phone}",
        "ContentSid":       CONTENT_SID,
        "ContentVariables": json.dumps({
            "1": first_name,
            "2": amount,
            "3": date_str,
            "4": balance,
        }),
    }
    resp = requests.post(url, data=payload, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=30)
    resp.raise_for_status()
    return resp.json().get("sid")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"Munch Account Notifier — {datetime.now().strftime('%Y-%m-%d %H:%M:%S SAST')}")
    print(f"{'='*55}\n")

    # Load state
    state = load_json(STATE_FILE, {"notified": [], "last_updated": ""})
    notified = set(state.get("notified", []))
    cache = load_json(CACHE_FILE, {"members": {}, "last_updated": ""})
    phone_cache = cache.get("members", {})

    print(f"State loaded: {len(notified)} previously notified transactions")
    print(f"Phone cache : {len(phone_cache)} cached numbers\n")

    # Auth
    token = munch_login()

    # Fetch ledger
    ledger = get_ledger(token)

    # Filter
    DEBIT_TYPES = {"debit", "payment", "sale", "account"}
    SKIP_USERS  = {"loyalty loyalty", ""}

    candidates = []
    skipped_type = 0
    skipped_old  = 0
    skipped_user = 0
    skipped_seen = 0

    for tx in ledger:
        user_lower = tx["user"].strip().lower()
        type_lower = tx["type"].strip().lower()

        # Skip non-debit types
        if not any(d in type_lower for d in DEBIT_TYPES):
            skipped_type += 1
            continue

        # Skip excluded users
        if user_lower in SKIP_USERS:
            skipped_user += 1
            continue

        # Skip old transactions
        if not is_recent(tx["date"]):
            skipped_old += 1
            continue

        key = transaction_key(tx)

        # Skip already notified
        if key in notified:
            skipped_seen += 1
            continue

        candidates.append((key, tx))

    print(f"Ledger summary:")
    print(f"  Total rows   : {len(ledger)}")
    print(f"  Wrong type   : {skipped_type}")
    print(f"  Too old      : {skipped_old}")
    print(f"  Skipped user : {skipped_user}")
    print(f"  Already sent : {skipped_seen}")
    print(f"  To process   : {len(candidates)}\n")

    if not candidates:
        print("Nothing to do. Exiting.")
        return

    # Process candidates
    sent_keys    = []
    cache_hits   = 0
    cache_misses = 0
    errors       = []

    for key, tx in candidates:
        user = tx["user"].strip()
        user_lower = user.lower()
        first_name = user.split()[0] if user else "Member"
        amount_display  = format_amount(tx["amount"])
        balance_display = tx["balance"]
        date_display    = format_date(tx["date"])

        print(f"Processing: {user}  |  {amount_display}  |  {date_display}")

        # Get phone
        phone = None
        if user_lower in phone_cache:
            phone = phone_cache[user_lower]
            cache_hits += 1
            print(f"  Phone (cache): {phone}")
        else:
            cache_misses += 1
            first = user.split()[0] if user else user
            members = search_members(token, first)
            for m in members:
                m_name = (m.get("name") or m.get("fullName") or
                          f"{m.get('firstName','')} {m.get('lastName','')}").strip()
                m_phone = m.get("contactNumber") or m.get("phone") or m.get("phoneNumber") or ""
                if m_name.lower() == user_lower and m_phone:
                    phone = format_phone(str(m_phone))
                    phone_cache[user_lower] = phone
                    print(f"  Phone (lookup): {phone}")
                    break
            if not phone:
                print(f"  ⚠ No phone found for {user} — skipping")
                errors.append(f"No phone for {user}")
                continue

        # Send WhatsApp
        try:
            sid = send_whatsapp(phone, first_name, amount_display, date_display, balance_display)
            print(f"  ✓ WhatsApp sent — SID: {sid}")
            sent_keys.append(key)
        except Exception as e:
            print(f"  ✗ Twilio error: {e}")
            errors.append(f"Twilio error for {user}: {e}")

    # Update state
    if sent_keys:
        notified.update(sent_keys)
        all_keys = list(notified)
        if len(all_keys) > MAX_STATE_ENTRIES:
            all_keys = all_keys[-MAX_STATE_ENTRIES:]
        state["notified"]     = all_keys
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, state)
        print(f"\n✓ State updated: {len(sent_keys)} new key(s) saved")

    # Update phone cache
    cache["members"]      = phone_cache
    cache["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_json(CACHE_FILE, cache)

    # Summary
    print(f"\n{'='*55}")
    print(f"SUMMARY")
    print(f"  Sent        : {len(sent_keys)}")
    print(f"  Skipped     : {skipped_seen} already notified, {skipped_user} excluded users, {skipped_old} too old")
    print(f"  Cache hits  : {cache_hits}  |  Lookups: {cache_misses}")
    if errors:
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    - {e}")
    print(f"{'='*55}\n")

    if errors:
        sys.exit(1)

if __name__ == "__main__":
    main()
