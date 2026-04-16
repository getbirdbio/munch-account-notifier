#!/usr/bin/env python3
"""
Munch Account Debit Notifier
Detects account-charged sales via the ledger, fetches full sale detail for
items + member phone, and sends a WhatsApp notification via Twilio.
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
MESSAGING_SVC  = "MG37372de845ecfc9e05f2ce97db4ed0cc"   # WhatsApp messaging service

MUNCH_EMAIL    = os.environ["MUNCH_EMAIL"]
MUNCH_PASSWORD = os.environ["MUNCH_PASSWORD"]

STATE_FILE        = os.path.join(os.path.dirname(__file__), "state", "notified_transactions.json")
CACHE_FILE        = os.path.join(os.path.dirname(__file__), "state", "member_phone_cache.json")
MAX_STATE_ENTRIES = 500
LOOKBACK_HOURS    = 4    # catch-up: covers 6am SAST to now


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
    return f"R{abs(cents) / 100:.2f}"

def format_sast(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (dt + timedelta(hours=2)).strftime("%-d %b %Y at %H:%M")
    except Exception:
        return iso_str

def format_phone(phone):
    p = str(phone).strip().replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "+27" + p[1:]
    if not p.startswith("+"):
        p = "+" + p
    return p


# ── Munch Auth ─────────────────────────────────────────────────────────────

def munch_login():
    """Three-step auth → (scoped_token, employee_id, org_id)"""

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
    emp           = r.json()["data"]["employee"]
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

    # Step 2: Retrieve operating context → get scopeId
    r = requests.post(f"{MUNCH_API}/operating-context/retrieve",
                      json={}, headers=base_h, timeout=30)
    r.raise_for_status()
    orgs     = r.json()["data"]["operatingContexts"]["organisations"]
    scope_id = orgs[0]["scopeId"]
    org_id   = orgs[0]["id"]
    print(f"✓ Step 2: Scope retrieved ({org_id})")

    # Step 3: Select context — must include BOTH scopeId AND organisationId
    r = requests.post(f"{MUNCH_API}/operating-context/select",
                      json={"scopeId": scope_id, "organisationId": org_id},
                      headers=base_h, timeout=30)
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

def get_recent_debits(token, employee_id, org_id):
    """
    Fetch ledger items for the last LOOKBACK_HOURS and return account debits.
    Each item includes saleId for fetching full sale detail.
    """
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end   = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    r = requests.post(
        f"{MUNCH_API}/account/retrieve-ledger",
        json={"accountId": ACCOUNT_ID, "companyId": COMPANY_ID,
              "startDate": start, "endDate": end},
        headers=api_headers(token, employee_id, org_id),
        timeout=30,
    )
    r.raise_for_status()
    items = r.json().get("data", {}).get("accountLedgerItems", [])
    print(f"✓ Ledger fetched: {len(items)} items in {LOOKBACK_HOURS}h window")

    debits = []
    for item in items:
        # Only account-payment debits (negative amounts)
        method = (item.get("payment") or {}).get("paymentMethod", {}).get("displayName", "")
        if item.get("amount", 0) >= 0 or method.lower() != "account":
            continue
        user = item.get("user") or {}
        debits.append({
            "sale_id":      item.get("saleId", ""),
            "amount_cents": item["amount"],
            "balance_cents":item.get("balance", 0),
            "created_at":   item.get("createdAt", ""),
            "user_name":    f"{user.get('firstName','')} {user.get('lastName','')}".strip(),
        })

    print(f"  → {len(debits)} account debit(s)")
    return debits


def get_sale_detail(token, employee_id, org_id, sale_id):
    """Fetch full sale including saleItems and payments[].user."""
    try:
        r = requests.post(
            f"{MUNCH_API}/sale/retrieve",
            json={"id": sale_id, "timezone": "Africa/Johannesburg"},
            headers=api_headers(token, employee_id, org_id),
            timeout=15,
        )
        r.raise_for_status()
        sales = r.json().get("data", {}).get("sales", [])
        return sales[0] if sales else None
    except Exception as e:
        print(f"  ⚠ sale/retrieve failed for {sale_id}: {e}")
        return None


# ── Twilio ─────────────────────────────────────────────────────────────────

def send_whatsapp(phone, first_name, amount, items, date_str, balance):
    """
    Template (5 vars):
      Hi {{1}}, your Getbird Birdhaven account was charged {{2}} for {{3}}
      on {{4}}. Your remaining balance is {{5}}.
    """
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    payload = {
        "MessagingServiceSid": MESSAGING_SVC,
        "To":                  f"whatsapp:{phone}",
        "ContentSid":          CONTENT_SID,
        "ContentVariables":    json.dumps({
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

    state       = load_json(STATE_FILE, {"notified": [], "last_updated": ""})
    notified    = set(state.get("notified", []))
    cache       = load_json(CACHE_FILE, {"members": {}, "last_updated": ""})
    phone_cache = cache.get("members", {})

    print(f"State:       {len(notified)} previously notified sale IDs")
    print(f"Phone cache: {len(phone_cache)} members\n")

    token, employee_id, org_id = munch_login()
    print()

    debits = get_recent_debits(token, employee_id, org_id)

    # Filter already-notified
    candidates   = [d for d in debits if d["sale_id"] and d["sale_id"] not in notified]
    already_seen = len(debits) - len(candidates)

    print(f"\n  Already notified : {already_seen}")
    print(f"  To process       : {len(candidates)}\n")

    if not candidates:
        print("Nothing to do. Exiting.")
        return

    sent_ids = []
    errors   = []

    for debit in candidates:
        sale_id    = debit["sale_id"]
        user_name  = debit["user_name"]
        first_name = user_name.split()[0] if user_name else "Member"
        amount_str  = cents_to_rand(debit["amount_cents"])
        balance_str = cents_to_rand(debit["balance_cents"])
        date_str    = format_sast(debit["created_at"])

        print(f"Processing: {user_name}  |  {amount_str}  |  {date_str}")

        # Get phone + items from sale detail
        phone      = None
        items_str  = "your order"
        sale       = get_sale_detail(token, employee_id, org_id, sale_id)

        if sale:
            acct_pmts = [p for p in sale.get("payments", []) if p.get("method") == "account"]
            if acct_pmts:
                user_obj  = acct_pmts[0].get("user") or {}
                phone_raw = user_obj.get("phone", "")
                if phone_raw:
                    phone = format_phone(phone_raw)
                    print(f"  Phone (sale)  : {phone}")
                # Items
                sale_items = sale.get("saleItems", [])
                joined = ", ".join(
                    i.get("displayName") or i.get("name", "")
                    for i in sale_items
                    if i.get("displayName") or i.get("name")
                )
                if joined:
                    items_str = joined

        # Fall back to phone cache if sale/retrieve didn't give us the phone
        if not phone:
            name_lower = user_name.lower()
            if name_lower in phone_cache:
                phone = phone_cache[name_lower]
                print(f"  Phone (cache) : {phone}")
            else:
                print(f"  ⚠ No phone found for {user_name} — skipping")
                errors.append(f"No phone for {user_name} (sale {sale_id})")
                continue

        print(f"  Items         : {items_str}")

        try:
            sid = send_whatsapp(phone, first_name, amount_str, items_str, date_str, balance_str)
            print(f"  ✓ WhatsApp sent — SID: {sid}")
            sent_ids.append(sale_id)
        except Exception as e:
            print(f"  ✗ Twilio error: {e}")
            errors.append(f"Twilio error for {user_name}: {e}")

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
    print(f"  Sent    : {len(sent_ids)}")
    print(f"  Skipped : {already_seen} already notified")
    if errors:
        print(f"  Errors  : {len(errors)}")
        for e in errors:
            print(f"    - {e}")
    print(f"{'='*55}\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
