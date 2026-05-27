# YWH Scope Monitor

Telegram bot that watches **YesWeHack** public programs and notifies on scope changes
(new programs, scope added/removed, severity tier change, out-of-scope rule edits,
program archived/disabled, bounty range change).

Bot: [@ywhtheplanet_bot](https://t.me/ywhtheplanet_bot)
Researcher handle: `mrcslvknm`

## Architecture

One-shot Python script (`ywh_monitor.py`) — fetch + diff + notify in a single run.
Designed for **GitHub Actions cron** so it runs 24/7 with zero infra cost, even
when your laptop is off.

```
ywh_client.py    — YWH API client (paginated /programs + /programs/{slug})
ywh_monitor.py   — main: load state → fetch → diff → notify → save state
find_chat_id.py  — one-time helper to get your Telegram chat_id
state.json       — current snapshot (committed back to repo each run)
diff_log.jsonl   — audit log of every change event
```

## One-time setup

### 1. Get your Telegram chat_id

```bash
export TG_TOKEN="<your bot token>"
# Open Telegram, message @ywhtheplanet_bot with /start, then:
python3 find_chat_id.py
```

Copy the `chat_id` from the JSON output.

### 2. Push this repo to GitHub

```bash
cd "/home/kali/Bug Bounty File/ywh_bot"
git init
git add .
git commit -m "init: YWH scope monitor"
gh repo create ywh-scope-monitor --public --source=. --push
```

(Public repo = unlimited free Actions minutes. Private = 2000 min/mo limit.)

### 3. Add GitHub repo secrets

```bash
gh secret set TG_TOKEN   --body "<your bot token>"
gh secret set TG_CHAT_ID --body "<your chat id from step 1>"
```

### 4. Seed initial state (so first real run does not spam 65 notifications)

```bash
gh workflow run "YWH Scope Monitor" -f seed_only=true
```

Wait ~1 min, confirm green run on Actions tab. After this, every subsequent
run only notifies on **changes** since the seed.

### 5. Done

GitHub Actions cron runs every 10 minutes. You will receive Telegram messages
whenever the YWH scope catalogue changes.

## Local testing

```bash
pip install -r requirements.txt
export TG_TOKEN="..." TG_CHAT_ID="..."

# dry run (prints what would be sent, doesn't actually send):
DRY_RUN=1 python3 ywh_monitor.py

# seed state without sending anything:
SEED_ONLY=1 python3 ywh_monitor.py

# real run:
python3 ywh_monitor.py
```

## Manual trigger

From the GitHub Actions UI:
- "Run workflow" → pick branch → optionally tick `dry_run` or `seed_only`.

Or from CLI:
```bash
gh workflow run "YWH Scope Monitor"
gh workflow run "YWH Scope Monitor" -f dry_run=true
```

## Event types

| Emoji | Kind | When |
|---|---|---|
| 🆕 | `program_new` | New program appears in public catalogue |
| 🗑️ | `program_removed` | Program disappears from public catalogue |
| ➕ | `scope_added` | New asset added to a program's scope |
| ➖ | `scope_removed` | Asset removed from scope |
| 🎯 | `scope_severity_change` | Asset value changed (HIGH → CRITICAL, etc.) |
| ⛔ | `oos_added` | New out-of-scope rule |
| ✅ | `oos_removed` | Out-of-scope rule lifted |
| 🔧 | `field_change` | Program flag changed (status, archived, bounty range, etc.) |

## Notes

- API base: `https://api.yeswehack.com` — public, no auth needed for public programs.
- Per-run cap: 200 program detail fetches (`MAX_DETAIL` env). On a typical run only
  programs whose `scopes_count` or status changed are re-fetched, so the budget is
  rarely exhausted.
- State is committed back to the repo every run; large changes may produce noisy
  commits but the git history doubles as an immutable audit log.
- DNS quirk on the dev machine: ISP resolver returns `lamanlabuh.id` for NXDOMAIN.
  Not an issue in GitHub Actions runners; if you run locally and DNS misbehaves,
  swap to `1.1.1.1` via `/etc/resolv.conf` or use `systemd-resolved`.
