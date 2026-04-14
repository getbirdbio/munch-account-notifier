#!/usr/bin/env python3
"""
Munch Account Debit Notifier
Watches recent closed sales paid via Account and sends WhatsApp to the member.
Uses /api/sale/list → /api/sale/retrieve for full detail (items + phone).
No phone cache required — phone comes directly from payments[].user.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────
MUNCH_API      = "https://api.munch.cloud/api"
ACCOUNT_ID     = "3e92a480-5f21-11ec-b43f-dde416ab9f61"
COMPANY_ID     = "28c5e780-3707-11ec-88a8-dde416ab9f61"
ORG_ID         = "1476d7a5-b7b2-4b18-85c6-33730cf37a12"

TWILIO_SID     = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM    = "whatsapp:+27600192724"
CONTENT_SID    = "HXcc32f85ac944517098c4c212978e938c"   # 5-var template

MUNCH_EMAIL    = os.environ["MUNCH_EMAIL"]
MUNCH_PASSWORD = os.environ["MUNCH_PASSWORD"]

STATE_FILE        = os.path.join(os.path.dirname(__file__), "state", "notified_transactions.json")
MAX_STATE_ENTRIES = 500
LOOKBACK_HOURS    = 25


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

def cents_to_rand(cents):
    """Convert integer cents to display string: 3300 → 'R33.00'"""
    return f"R{abs(cents) / 100:.2f}"

def format_sast(iso_str):
    """ISO UTC timestamp → '14 Apr 2026 at 08:47' in SAST."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_sast = dt + timedelta(hours=2)
        return dt_sast.strftime("%-d %b %Y at %H:%M")
    except Exception:
        return iso_str

def format_phone(phone):
    """Normalise to +27... format."""
    p = str(phone).strip().replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "+27" + p[1:]
    if not p.startswith("+"):
        p = "+" + p
    return p


# ── Munch Auth ─────────────────────────────────────────────────────────────

def munch_login():
    """
    Three-step auth:
      1. POST /auth-internal/login           → initial token
      2. POST /operating-context/retrieve    → scopeId
      3. POST /operating-context/select      → scoped token (with org context)
    Returns (scoped_token, employee_id, org_id)
    """
    # Step 1: Login
    r = requests.post(
        f"{MUNCH_API}/auth-internal/login",
        json={"email": MUNCH_EMAIL, "password": MUNCH_PASSWORD},
        headers={"Content-Type": "application/json",
                 "Munch-Platform": "cloud.munch.portal",
                 "Munch-Version": "2.20.1"},
        timeout=30,
    )
    r.raise_for_status()
    emp = r.json()["data"]["employee"]
    initial_token = emp["accessToken"]
    employee_id   = emp["id"]
    print("✓ Step 1: Login successful")

    base_h = {
        "Authorization":      f"Bearer {initial_token}",
        "Authorization-Type": "internal",
        "Content-Type":       "application/json",
        "Munch-Platform":     "cloud.munch.portal",
        "Munch-Version":      "2.20.1",
        "Munch-Organisation": ORG_ID,
    }

    # Step 2: Get scopeId
    r = requests.post(f"{MUNCH_API}/operating-context/retrieve",
                      json={}, headers=base_h, timeout=30)
    r.raise_for_status()
    orgs = r.json()["data"]["operatingContexts"]["organisations"]
    scope_id = orgs[0]["scopeId"]
    org_id   = orgs[0]["id"]
    print(f"✓ Step 2: Scope retrieved ({org_id})")

    # Step 3: Select scope → scoped token
    r = requests.post(f"{MUNCH_API}/operating-context/select",
                      json={"scopeId": scope_id}, headers=base_h, timeout=30)
    r.raise_for_status()
    scoped_token = r.json()["data"]["employee"]["accessToken"]
    print("✓ Step 3: Scoped token obtained")

    return scoped_token, employee_id, org_id


def api_headers(token, employee_id, org_id):
    return {
        "Authorization":      f"Bearer {token}",
        "Authorization-Type": "internal",
        "Content-Type":       "application/json",
        "Locale":             "en",
        "Munch-Employee":     employee_id,
        "Munch-Organisation": org_id,
        "Munch-Platform":     "cloud.munch.portal",
        "Munch-Timezone":     "Africa/Johannesburg",
        "Munch-Version":      "2.20.1",
    }


# ── Munch API ──────────────────────────────────────────────────────────────

def list_recent_sales(token, employee_id, org_id):
    """
    Fetch recent sales via /api/sale/list filtered to the last LOOKBACK_HOURS.
    Returns a list of sale dicts.
    """
    now_utc   = datetime.now(timezone.utc)
    date_from = (now_utc - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    date_to   = now_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    r = requests.post(
        f"{MUNCH_API}/sale/list",
        json={
            "accountId": ACCOUNT_ID,
            "companyId": COMPANY_ID,
            "timezone":  "Africa/Johannesburg",
            "dateFrom":  date_from,
            "dateTo":    date_to,
        },
        headers=api_headers(token, employee_id, org_id),
        timeout=30,
    )
    r.raise_for_status()
    sales = r.json().get("data", {}).get("sales", [])
    print(f"✓ Sale list fetched: {len(sales)} sales in window")
    return sales


def get_sale_detail(token, employee_id, org_id, sale_id):
    """
    Fetch full sale detail via /api/sale/retrieve.
    Returns the sale dict including saleItems and payments[].user.
    """
    r = requests.post(
        f"{MUNCH_API}/sale/retrieve",
        json={"id": sale_id, "timezone": "Africa/Johannesburg"},
        headers=api_headers(token, employee_id, org_id),
        timeout=30,
    )
    r.raise_for_status()
    sales = r.json().get("data", {}).get("sales", [])
    return sales[0] if sales else None


# ── Twilio ─────────────────────────────────────────────────────────────────

def send_whatsapp(phone, first_name, amount, items, date_str, balance):
    """
    Send WhatsApp via Twilio using the 5-variable approved template:
      Hi {{1}}, your Getbird Birdhaven account was charged {{2}} for {{3}}
      on {{4}}. Your remaining balance is {{5}}.
    """
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    payload = {
        "From":             TWILIO_FROM,
        "To":               f"whatsapp:{phone}",
        "ContentSid":       CONTENT_SID,
        "ContentVariables": json.dumps({
            "1": first_name,
            "2": amount,
            "3": items,
            "4": date_str,
            "5": balance,
        }),
    }
    r = requests.post(url, data=payload, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=30)
    r.raise_for_status()
    return r.json().get("sid")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    now_sast = datetime.now(timezone.utc) + timedelta(hours=2)
    print(f"\n{'='*55}")
    print(f"Munch Account Notifier — {now_sast.strftime('%Y-%m-%d %H:%M:%S')} SAST")
    print(f"{'='*55}\n")

    # Load state (keyed by sale ID)
    state    = load_json(STATE_FILE, {"notified": [], "last_updated": ""})
    notified = set(state.get("notified", []))
    print(f"State loaded: {len(notified)} previously notified sale IDs\n")

    # Auth
    token, employee_id, org_id = munch_login()
    print()

    # List recent sales
    all_sales = list_recent_sales(token, employee_id, org_id)

    # Filter: closed + has an account payment + not already notified
    candidates    = []
    skipped_status  = 0
    skipped_payment = 0
    skipped_seen    = 0

    for sale in all_sales:
        if sale.get("status") != "closed":
            skipped_status += 1
            continue
        payments = sale.get("payments", [])
        acct_pmts = [p for p in payments if p.get("method") == "account"]
        if not acct_pmts:
            skipped_payment += 1
            continue
        if sale["id"] in notified:
            skipped_seen += 1
            continue
        candidates.append(sale["id"])

    print(f"\nSale list summary:")
    print(f"  Total in window  : {len(all_sales)}")
    print(f"  Wrong status     : {skipped_status}")
    print(f"  No account pmt   : {skipped_payment}")
    print(f"  Already notified : {skipped_seen}")
    print(f"  To process       : {len(candidates)}\n")

    if not candidates:
        print("Nothing to do. Exiting.")
        return

    # Process each candidate — fetch full sale detail
    sent_ids = []
    errors   = []

    for sale_id in candidates:
        print(f"Fetching detail for sale {sale_id}...")
        sale = get_sale_detail(token, employee_id, org_id, sale_id)
        if not sale:
            print(f"  ⚠ Could not retrieve sale detail — skipping")
            errors.append(f"No detail for sale {sale_id}")
            continue

        # Find the account payment
        acct_pmts = [p for p in sale.get("payments", []) if p.get("method") == "account"]
        if not acct_pmts:
            continue
        payment = acct_pmts[0]

        # Extract user
        user       = payment.get("user") or {}
        first_name = (user.get("firstName") or "Member").strip()
        phone_raw  = user.get("phone", "")
        if not phone_raw:
            print(f"  ⚠ No phone for {first_name} — skipping")
            errors.append(f"No phone for user in sale {sale_id}")
            continue
        phone = format_phone(phone_raw)

        # Extract amounts and items
        amount_cents  = payment.get("amount", 0)
        balance_cents = payment.get("account", {}).get("balance", 0)
        amount_str    = cents_to_rand(amount_cents)
        balance_str   = cents_to_rand(balance_cents)

        sale_items = sale.get("saleItems", [])
        items_str  = ", ".join(
            i.get("displayName") or i.get("name", "")
            for i in sale_items
            if i.get("displayName") or i.get("name")
        ) or "your order"

        date_str = format_sast(sale.get("invoicedAt") or sale.get("createdAt", ""))

        print(f"  Member : {first_name}  |  {phone}")
        print(f"  Items  : {items_str}")
        print(f"  Amount : {amount_str}  |  Balance: {balance_str}  |  {date_str}")

        # Send
        try:
            sid = send_whatsapp(phone, first_name, amount_str, items_str, date_str, balance_str)
            print(f"  ✓ WhatsApp sent — SID: {sid}")
            sent_ids.append(sale_id)
        except Exception as e:
            print(f"  ✗ Twilio error: {e}")
            errors.append(f"Twilio error for sale {sale_id} ({first_name}): {e}")

    # Persist state
    if sent_ids:
        notified.update(sent_ids)
        all_ids = list(notified)
        if len(all_ids) > MAX_STATE_ENTRIES:
            all_ids = all_ids[-MAX_STATE_ENTRIES:]
        state["notified"]     = all_ids
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, state)
        print(f"\n✓ State updated: {len(sent_ids)} new sale ID(s) saved")

    # Summary
    print(f"\n{'='*55}")
    print(f"SUMMARY")
    print(f"  Sent        : {len(sent_ids)}")
    print(f"  Skipped     : {skipped_seen} already notified")
    if errors:
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    - {e}")
    print(f"{'='*55}\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
