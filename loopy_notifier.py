#!/usr/bin/env python3
"""
Loopy Loyalty Push Notifier
Sends targeted push notifications to Getbird Birdhaven loyalty members.

Scenarios (run daily via GitHub Actions):
  1. ALMOST_THERE  -- currentStamps >= 9 (3 away from free coffee at 12)
  2. COME_BACK     -- no stamp earned in 14+ days, but still has stamps
  3. LOYAL         -- totalStampsEarned >= 24 (2+ full cycles) -- monthly
"""

import json, os, sys, requests
from datetime import datetime, timezone, timedelta
import anthropic

# -- Config ------------------------------------------------------------------
LL_API       = "https://api.loopyloyalty.com"
CAMPAIGN_ID  = "hZd5mudqN2NiIrq2XoM46"
MAX_STAMPS   = 12   # stamps for a free coffee

LL_USERNAME   = os.environ["LL_USERNAME"]
LL_PASSWORD   = os.environ["LL_PASSWORD"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

STATE_FILE   = os.path.join(os.path.dirname(__file__), "state", "loopy_state.json")
RUN_LOG_FILE = os.path.join(os.path.dirname(__file__), "state", "run_log.json")

ALMOST_THERE_MIN      = 9    # stamps >= this triggers "almost there"
COME_BACK_DAYS        = 14   # days since last stamp before nudge
LOYAL_THRESHOLD       = 24   # lifetime stamps >= this = loyal customer

ALMOST_THERE_COOLDOWN = 1    # re-notify after N days
COME_BACK_COOLDOWN    = 7
LOYAL_COOLDOWN        = 30

MAX_COME_BACK_PER_RUN = 50   # cap to avoid mass spam


# -- Helpers -----------------------------------------------------------------
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

def is_on_cooldown(state, card_id, scenario, cooldown_days):
    key = f"{card_id}:{scenario}"
    last_sent = state.get("sent", {}).get(key)
    if not last_sent:
        return False
    last_dt = datetime.fromisoformat(last_sent)
    return (datetime.now(timezone.utc) - last_dt).days < cooldown_days

def mark_sent(state, card_id, scenario):
    state.setdefault("sent", {})[f"{card_id}:{scenario}"] = datetime.now(timezone.utc).isoformat()

# -- Loopy Loyalty Auth ------------------------------------------------------
def ll_login():
    r = requests.post(f"{LL_API}/account/login",
                      json={"username": LL_USERNAME, "password": LL_PASSWORD},
                      timeout=15)
    r.raise_for_status()
    token = r.json()["token"]
    print("+ Logged in to Loopy Loyalty")
    return token

def ll_headers(token):
    return {"Authorization": token, "Content-Type": "application/json"}

# -- Loopy Loyalty API -------------------------------------------------------
def list_all_cards(token):
    """Fetch all cards with pagination."""
    headers = ll_headers(token)
    all_cards, start, page_size = [], 0, 200
    while True:
        r = requests.post(f"{LL_API}/card/cid/{CAMPAIGN_ID}",
                          json={"dt": {"start": start, "length": page_size,
                                       "order": {"column": "created", "dir": "desc"}}},
                          headers=headers, timeout=30)
        r.raise_for_status()
        data  = r.json()
        cards = data.get("data", [])
        all_cards.extend(cards)
        total = data.get("recordsTotal", 0)
        if len(all_cards) >= total or not cards:
            break
        start += page_size
    print(f"+ Fetched {len(all_cards)} / {total} cards")
    return all_cards

def send_individual_push(token, card_id, message):
    r = requests.post(f"{LL_API}/card/push",
                      json={"cardID": card_id, "message": message},
                      headers=ll_headers(token), timeout=15)
    r.raise_for_status()
    return r.json()

# -- Claude Message Generation -----------------------------------------------
def generate_message(scenario, context=""):
    """Generate a fun, trendy <90-char push message with emojis via Claude Haiku."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompts = {
        # context = remaining stamps needed (e.g. "3")
        "almost_there": (
            f"Write a single fun, trendy push notification for a coffee loyalty card app. "
            f"The customer needs exactly {context} more stamp(s) to earn a free coffee "
            f"(our loyalty card gives 1 free coffee every 12 stamps). "
            f"Mention the exact number of stamps still needed and reference the free coffee reward. "
            f"Use 1-2 emojis. MAX 90 characters. Return ONLY the message text, nothing else."
        ),
        # context = current stamp count (e.g. "5")
        "come_back": (
            f"Write a single fun, warm push notification to re-engage a coffee shop customer "
            f"who hasn't visited in a while. They already have {context} stamps saved toward "
            f"their next free coffee (12 stamps = 1 free coffee). "
            f"Encourage them to pop in and keep building their stamps. "
            f"Use 1-2 emojis. MAX 90 characters. Return ONLY the message text, nothing else."
        ),
        "loyal": (
            "Write a single fun, celebratory push notification to thank a super loyal coffee "
            "customer who has earned multiple free coffees with us (12 stamps each). "
            "Make them feel appreciated and special — they're a true regular. "
            "Use 1-2 emojis, keep it warm and trendy. "
            "MAX 90 characters. Return ONLY the message text, nothing else."
        ),
    }
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=120,
        messages=[{"role": "user", "content": prompts[scenario]}]
    )
    text = msg.content[0].text.strip().strip('"').strip("'")
    return text[:90] if len(text) > 90 else text

# -- Scenario 1: Almost There ------------------------------------------------
def run_almost_there(token, state, cards):
    """Returns (sent_count, notifications_list)."""
    print("\n-- Scenario 1: Almost There ----------------------------------------")
    candidates = [
        c for c in cards
        if ALMOST_THERE_MIN <= c.get("currentStamps", 0) < MAX_STAMPS
        and c.get("status") == "installed"
        and not is_on_cooldown(state, c["id"], "almost_there", ALMOST_THERE_COOLDOWN)
    ]
    print(f"  Candidates: {len(candidates)}")
    if not candidates:
        return 0, []

    # Group by stamp count so we generate one message per count (max 3 calls)
    by_count = {}
    for c in candidates:
        by_count.setdefault(c["currentStamps"], []).append(c)

    sent = 0
    notifications = []
    for stamps, group in sorted(by_count.items()):
        remaining = MAX_STAMPS - stamps
        message = generate_message("almost_there", str(remaining))
        print(f"  [{stamps} stamps -> {remaining} to go] {message!r}")
        for card in group:
            name = card.get("customerDetails", {}).get("Name", "Member")
            try:
                send_individual_push(token, card["id"], message)
                mark_sent(state, card["id"], "almost_there")
                print(f"    + {name}")
                sent += 1
                notifications.append({
                    "ts":       datetime.now(timezone.utc).isoformat(),
                    "scenario": "almost_there",
                    "card_id":  card["id"],
                    "name":     name,
                    "stamps":   stamps,
                    "message":  message,
                })
            except Exception as e:
                print(f"    x {name}: {e}")
    print(f"  Sent: {sent}")
    return sent, notifications

# -- Scenario 2: Come Back ---------------------------------------------------
def run_come_back(token, state, cards):
    """Returns (sent_count, notifications_list)."""
    print("\n-- Scenario 2: Come Back -------------------------------------------")
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=COME_BACK_DAYS)
    candidates = []
    for card in cards:
        if card.get("currentStamps", 0) <= 0 or card.get("status") != "installed":
            continue
        last_raw = card.get("lastStampEarned")
        if not last_raw:
            continue
        try:
            last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
            if last_dt < cutoff and not is_on_cooldown(state, card["id"], "come_back", COME_BACK_COOLDOWN):
                candidates.append(card)
        except Exception:
            pass

    candidates = candidates[:MAX_COME_BACK_PER_RUN]
    print(f"  Candidates: {len(candidates)}")
    if not candidates:
        return 0, []

    message = generate_message("come_back", "a few")
    print(f"  Message: {message!r}")
    sent = 0
    notifications = []
    for card in candidates:
        name   = card.get("customerDetails", {}).get("Name", "Member")
        stamps = card.get("currentStamps", 0)
        try:
            send_individual_push(token, card["id"], message)
            mark_sent(state, card["id"], "come_back")
            print(f"    + {name} (last: {card.get('lastStampEarned','?')[:10]})")
            sent += 1
            notifications.append({
                "ts":       datetime.now(timezone.utc).isoformat(),
                "scenario": "come_back",
                "card_id":  card["id"],
                "name":     name,
                "stamps":   stamps,
                "message":  message,
            })
        except Exception as e:
            print(f"    x {name}: {e}")
    print(f"  Sent: {sent}")
    return sent, notifications

# -- Scenario 3: Loyal Customers ---------------------------------------------
def run_loyal(token, state, cards):
    """Returns (sent_count, notifications_list)."""
    print("\n-- Scenario 3: Loyal Customers -------------------------------------")
    candidates = [
        c for c in cards
        if c.get("totalStampsEarned", 0) >= LOYAL_THRESHOLD
        and c.get("status") == "installed"
        and not is_on_cooldown(state, c["id"], "loyal", LOYAL_COOLDOWN)
    ]
    print(f"  Candidates: {len(candidates)}")
    if not candidates:
        return 0, []

    message = generate_message("loyal")
    print(f"  Message: {message!r}")
    sent = 0
    notifications = []
    for card in candidates:
        name   = card.get("customerDetails", {}).get("Name", "Member")
        stamps = card.get("totalStampsEarned", 0)
        try:
            send_individual_push(token, card["id"], message)
            mark_sent(state, card["id"], "loyal")
            print(f"    + {name} ({stamps} lifetime stamps)")
            sent += 1
            notifications.append({
                "ts":       datetime.now(timezone.utc).isoformat(),
                "scenario": "loyal",
                "card_id":  card["id"],
                "name":     name,
                "stamps":   stamps,
                "message":  message,
            })
        except Exception as e:
            print(f"    x {name}: {e}")
    print(f"  Sent: {sent}")
    return sent, notifications

# -- Main --------------------------------------------------------------------
def main():
    now_sast = datetime.now(timezone.utc) + timedelta(hours=2)
    print(f"\n{'='*55}")
    print(f"Loopy Loyalty Notifier -- {now_sast.strftime('%Y-%m-%d %H:%M:%S')} SAST")
    print(f"{'='*55}")

    state = load_json(STATE_FILE, {"sent": {}})
    # Prune state older than 60 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if v > cutoff}

    token = ll_login()
    cards = list_all_cards(token)

    all_notifications = []
    total = 0

    n, notifs = run_almost_there(token, state, cards)
    total += n; all_notifications.extend(notifs)

    n, notifs = run_come_back(token, state, cards)
    total += n; all_notifications.extend(notifs)

    n, notifs = run_loyal(token, state, cards)
    total += n; all_notifications.extend(notifs)

    save_json(STATE_FILE, state)

    # Save run log -----------------------------------------------------------
    run_log = load_json(RUN_LOG_FILE, {"runs": []})
    run_entry = {
        "run_ts":    now_sast.isoformat(),
        "total_sent": total,
        "by_scenario": {
            "almost_there": sum(1 for n in all_notifications if n["scenario"] == "almost_there"),
            "come_back":    sum(1 for n in all_notifications if n["scenario"] == "come_back"),
            "loyal":        sum(1 for n in all_notifications if n["scenario"] == "loyal"),
        },
        "notifications": all_notifications,
    }
    run_log["runs"].append(run_entry)
    # Keep last 90 days
    cutoff90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    run_log["runs"] = [r for r in run_log["runs"] if r["run_ts"] > cutoff90]
    save_json(RUN_LOG_FILE, run_log)

    print(f"\n{'='*55}")
    print(f"TOTAL SENT: {total}")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    main()
