# Munch Account Notifier

Sends WhatsApp notifications to Getbird Birdhaven members whenever their account is debited via Munch POS.

Runs every 10 minutes via GitHub Actions during trading hours (06:00–20:00 SAST).

## Setup

### 1. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `MUNCH_EMAIL` | Your Munch portal login email |
| `MUNCH_PASSWORD` | Your Munch portal password |
| `TWILIO_ACCOUNT_SID` | Your Twilio Account SID (starts with `AC...`) |
| `TWILIO_AUTH_TOKEN` | Your Twilio auth token |

### 2. Enable workflow permissions

Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions** (needed to commit state back to the repo).

### 3. Trigger manually (first run)

Go to **Actions → Munch Account Notifier → Run workflow** to test it immediately.

## How it works

1. Logs into the Munch API with your credentials
2. Fetches the latest ledger entries for the Getbird Birdhaven account
3. Filters for recent debits (last 25 hours) that haven't been notified yet
4. Looks up each member's phone number (cached in `state/member_phone_cache.json`)
5. Sends a WhatsApp message via Twilio
6. Commits the updated state back to the repo so no duplicate notifications are sent

## State files

- `state/notified_transactions.json` — tracks which transactions have been notified
- `state/member_phone_cache.json` — caches member phone numbers to avoid re-fetching
