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

def cents_to_display(cents):
    """Convert integer cents to display string: -41800 → '-R418.00'"""
    rands = abs(cents) / 100
    prefix = "-" if cents < 0 else ""
    return f"{prefix}R{rands:.2f}"

def iso_to_sast_key(iso_str):
    """Convert ISO timestamp to SAST display key: '2026-04-09T06:47:00Z' → '09/04/26 - 08:47'"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_sast = dt + timedelta(hours=2)
        return dt_sast.strftime("%d/%m/%y - %H:%M")
    except Exception:
        return iso_str

def format_date_display(iso_str):
    """Convert ISO timestamp to friendly display: '2026-04-09T06:47:00Z' → '9 Apr 2026, 08:47 AM'"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_sast = dt + timedelta(hours=2)
        return dt_sast.strftime("%-d %b %Y, %I:%M %p")
    except Exception:
        return iso_str

def transaction_key(tx):
    """Build a stable dedup key from a normalised transaction dict."""
    return f"{tx['date_key']}_{tx['user']}_{tx['amount_display']}"

def format_phone(phone):
    """Normalise to +27... format."""
    p = str(phone).replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "+27" + p[1:]
    if not p.startswith("+"):
        p = "+" + p
    return p


# ── Munch Auth ─────────────────────────────────────────────────────────────

def munch_login():
    """
    Three-step auth flow:
      1. Login → initial token (organisationId: null)
      2. Retrieve operating contexts → get scopeId
      3. Select operating context → scoped token with organisationId populated
    Returns: (scoped_token, employee_id, org_id)
    """
    # Step 1: Login
    resp = requests.post(
        f"{MUNCH_API}/auth-internal/login",
        json={"email": MUNCH_EMAIL, "password": MUNCH_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    login_data = resp.json()
    initial_token = (
        login_data.get("data", {}).get("employee", {}).get("accessToken")
        or login_data.get("employee", {}).get("accessToken")
    )
    employee_id = (
        login_data.get("data", {}).get("employee", {}).get("id")
        or login_data.get("employee", {}).get("id")
    )
    if not initial_token:
        raise RuntimeError(f"Login failed — no accessToken in response: {login_data}")
    print("✓ Step 1: Login successful")

    base_headers = {
        "Authorization": f"Bearer {initial_token}",
        "Authorization-Type": "internal",
        "Content-Type": "application/json",
        "Munch-Platform": "cloud.munch.portal",
        "Munch-Timezone": "Africa/Johannesburg",
        "locale": "en",
    }

    # Step 2: Retrieve operating contexts
    resp = requests.post(
        f"{MUNCH_API}/operating-context/retrieve",
        json={},
        headers=base_headers,
        timeout=30,
    )
    resp.raise_for_status()
    ctx_data = resp.json()
    orgs = ctx_data.get("data", {}).get("operatingContexts", {}).get("organisations", [])
    if not orgs:
        raise RuntimeError(f"No organisations found in operating contexts: {ctx_data}")
    org = orgs[0]
    scope_id = org["scopeId"]
    org_id   = org["id"]
    print(f"✓ Step 2: Operating context retrieved — org: {org.get('name')} ({org_id})")

    # Step 3: Select operating context → get scoped token
    resp = requests.post(
        f"{MUNCH_API}/operating-context/select",
        json={"scopeId": scope_id, "organisationId": org_id},
        headers=base_headers,
        timeout=30,
    )
    resp.raise_for_status()
    select_data = resp.json()
    scoped_token = (
        select_data.get("data", {}).get("employee", {}).get("accessToken")
        or select_data.get("employee", {}).get("accessToken")
    )
    if not scoped_token:
        raise RuntimeError(f"Context select failed — no accessToken: {select_data}")
    print("✓ Step 3: Scoped token obtained")

    return scoped_token, employee_id, org_id


def munch_headers(token, employee_id, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "Authorization-Type": "internal",
        "Content-Type": "application/json",
        "Munch-Organisation": org_id,
        "Munch-Employee": employee_id,
        "Munch-Platform": "cloud.munch.portal",
        "Munch-Timezone": "Africa/Johannesburg",
        "locale": "en",
    }


# ── Munch API ──────────────────────────────────────────────────────────────

def get_ledger(token, employee_id, org_id):
    """Fetch ledger items from the last LOOKBACK_HOURS using date filter."""
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end   = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    resp = requests.post(
        f"{MUNCH_API}/account/retrieve-ledger",
        json={
            "accountId": ACCOUNT_ID,
            "companyId": COMPANY_ID,
            "startDate": start,
            "endDate":   end,
        },
        headers=munch_headers(token, employee_id, org_id),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", {}).get("accountLedgerItems", [])
    total = data.get("data", {}).get("_metadata", {}).get("total", len(items))
    print(f"✓ Ledger fetched: {len(items)} items in window (total matching: {total})")

    normalised = []
    for item in items:
        amount_cents   = item.get("amount", 0)
        balance_cents  = item.get("balance", 0)
        created_at     = item.get("createdAt", "")
        tx_type        = item.get("transactionType", "")
        payment_method = item.get("payment", {}).get("paymentMethod", {}).get("displayName", "")
        user_obj       = item.get("user") or {}
        user_name      = f"{user_obj.get('firstName', '')} {user_obj.get('lastName', '')}".strip()

        normalised.append({
            "type":            tx_type,
            "payment_method":  payment_method,
            "user":            user_name,
            "amount_cents":    amount_cents,
            "amount_display":  cents_to_display(amount_cents),
            "balance_display": cents_to_display(balance_cents),
            "date_key":        iso_to_sast_key(created_at),
            "date_display":    format_date_display(created_at),
            "created_at":      created_at,
        })

    return normalised


def search_members(token, employee_id, org_id, name):
    resp = requests.post(
        f"{MUNCH_API}/account/retrieve-users",
        json={
            "accountId": ACCOUNT_ID,
            "companyId": COMPANY_ID,
            "search":    name,
            "limit":     10,
        },
        headers=munch_headers(token, employee_id, org_id),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return (
        data.get("data", {}).get("users")
        or data.get("data", {}).get("members")
        or data.get("users")
        or data.get("members")
        or []
    )


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
    now_sast = datetime.now(timezone.utc) + timedelta(hours=2)
    print(f"\n{'='*55}")
    print(f"Munch Account Notifier — {now_sast.strftime('%Y-%m-%d %H:%M:%S')} SAST")
    print(f"{'='*55}\n")

    # Load state
    state       = load_json(STATE_FILE, {"notified": [], "last_updated": ""})
    notified    = set(state.get("notified", []))
    cache       = load_json(CACHE_FILE, {"members": {}, "last_updated": ""})
    phone_cache = cache.get("members", {})

    print(f"State loaded: {len(notified)} previously notified transactions")
    print(f"Phone cache : {len(phone_cache)} cached numbers\n")

    # Auth (3-step)
    token, employee_id, org_id = munch_login()
    print()

    # Fetch ledger (date-filtered to last LOOKBACK_HOURS)
    ledger = get_ledger(token, employee_id, org_id)

    # Filter to new debit candidates
    SKIP_USERS = {"loyalty loyalty", ""}

    candidates    = []
    skipped_type  = 0
    skipped_user  = 0
    skipped_seen  = 0

    for tx in ledger:
        user_lower = tx["user"].strip().lower()

        # Only process debits (negative amounts) via Account payment method
        is_debit = tx["amount_cents"] < 0 and tx["payment_method"].lower() == "account"
        if not is_debit:
            skipped_type += 1
            continue

        # Skip excluded users
        if user_lower in SKIP_USERS:
            skipped_user += 1
            continue

        key = transaction_key(tx)

        # Skip already notified
        if key in notified:
            skipped_seen += 1
            continue

        candidates.append((key, tx))

    print(f"\nLedger summary:")
    print(f"  Total rows   : {len(ledger)}")
    print(f"  Wrong type   : {skipped_type}")
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
        user       = tx["user"].strip()
        user_lower = user.lower()
        first_name = user.split()[0] if user else "Member"

        print(f"Processing: {user}  |  {tx['amount_display']}  |  {tx['date_display']}")

        # Get phone
        phone = None
        if user_lower in phone_cache:
            phone = phone_cache[user_lower]
            cache_hits += 1
            print(f"  Phone (cache): {phone}")
        else:
            cache_misses += 1
            members = search_members(token, employee_id, org_id, first_name)
            for m in members:
                m_name = (
                    m.get("name")
                    or m.get("fullName")
                    or f"{m.get('firstName','')} {m.get('lastName','')}".strip()
                )
                m_phone = (
                    m.get("contactNumber")
                    or m.get("phone")
                    or m.get("phoneNumber")
                    or m.get("cellphone")
                    or ""
                )
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
            sid = send_whatsapp(
                phone,
                first_name,
                tx["amount_display"].lstrip("-"),  # template shows "R418.00" (no minus)
                tx["date_display"],
                tx["balance_display"],
            )
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
    print(f"  Skipped     : {skipped_seen} already notified, {skipped_user} excluded")
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
