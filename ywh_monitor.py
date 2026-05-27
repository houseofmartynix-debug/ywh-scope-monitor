"""YWH scope monitor — one-shot poll + diff + Telegram notify.

Designed to run from cron/GitHub Actions/systemd timer (not a long-lived process).
Each run:
  1. Load previous state (state.json)
  2. Fetch all public programs from YWH API
  3. Fetch detail for: new programs + programs whose list-level scopes_count changed
  4. Compute diff vs prev state
  5. Send Telegram notifications for each change
  6. Append audit row to diff_log.jsonl
  7. Save new state

Env:
  TG_TOKEN     — Telegram bot token (required)
  TG_CHAT_ID   — chat id to notify (required; use find_chat_id.py once to discover)
  YWH_UA       — override user-agent (optional)
  STATE_FILE   — override state.json path (optional, default: ./state.json)
  AUDIT_FILE   — override diff_log.jsonl path (optional)
  MAX_DETAIL   — cap per-run detail fetches (optional, default 200)
  DRY_RUN      — if set, don't send telegram messages
  SEED_ONLY    — if set, snapshot current state without sending any messages
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

from ywh_client import YWHClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ywh-monitor")

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
AUDIT_FILE = Path(os.getenv("AUDIT_FILE", "diff_log.jsonl"))
MAX_DETAIL = int(os.getenv("MAX_DETAIL", "200"))

TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"programs": {}, "last_run": None, "version": 1}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log.error("state file corrupt, starting fresh: %s", e)
        return {"programs": {}, "last_run": None, "version": 1}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def snapshot_list_item(item: dict) -> dict:
    """Fields tracked from the list endpoint (cheap to fetch)."""
    return {
        "title": item.get("title"),
        "slug": item.get("slug"),
        "type": item.get("type"),                     # bug-bounty | vdp
        "status": item.get("status"),                 # V | C | etc
        "public": item.get("public"),
        "bounty": item.get("bounty"),
        "vdp": item.get("vdp"),
        "archived": item.get("archived"),
        "disabled": item.get("disabled"),
        "secured": item.get("secured"),
        "demo": item.get("demo"),
        "scopes_count": item.get("scopes_count"),
        "bounty_reward_min": item.get("bounty_reward_min"),
        "bounty_reward_max": item.get("bounty_reward_max"),
        "country": item.get("country"),
        "activity_area": item.get("activity_area"),
    }


def snapshot_detail(detail: dict) -> dict:
    """Fields tracked from the detail endpoint (scope list)."""
    scopes = []
    for s in detail.get("scopes", []) or []:
        scopes.append(
            {
                "scope": s.get("scope"),
                "type": s.get("scope_type"),
                "asset_value": s.get("asset_value"),
            }
        )
    return {
        "scopes": sorted(scopes, key=lambda x: (x.get("type") or "", x.get("scope") or "")),
        "out_of_scope": sorted(detail.get("out_of_scope", []) or []),
        "scopes_count": detail.get("scopes_count"),
    }


def diff_program(slug: str, old: dict, new: dict) -> list[dict]:
    """Return list of change events for a single program."""
    events: list[dict] = []

    list_old = old.get("list", {}) or {}
    list_new = new.get("list", {}) or {}
    for field in (
        "type",
        "status",
        "public",
        "bounty",
        "vdp",
        "archived",
        "disabled",
        "secured",
        "bounty_reward_min",
        "bounty_reward_max",
    ):
        if list_old.get(field) != list_new.get(field):
            events.append(
                {
                    "kind": "field_change",
                    "field": field,
                    "old": list_old.get(field),
                    "new": list_new.get(field),
                }
            )

    detail_old = old.get("detail") or {}
    detail_new = new.get("detail") or {}
    if detail_new:
        old_scopes = {(s["type"], s["scope"]): s for s in detail_old.get("scopes", []) or []}
        new_scopes = {(s["type"], s["scope"]): s for s in detail_new.get("scopes", []) or []}

        for key, s in new_scopes.items():
            if key not in old_scopes:
                events.append({"kind": "scope_added", "scope": s})
        for key, s in old_scopes.items():
            if key not in new_scopes:
                events.append({"kind": "scope_removed", "scope": s})
        for key, sn in new_scopes.items():
            so = old_scopes.get(key)
            if so and so.get("asset_value") != sn.get("asset_value"):
                events.append(
                    {
                        "kind": "scope_severity_change",
                        "scope": sn,
                        "old": so.get("asset_value"),
                        "new": sn.get("asset_value"),
                    }
                )

        old_oos = set(detail_old.get("out_of_scope", []) or [])
        new_oos = set(detail_new.get("out_of_scope", []) or [])
        for added in sorted(new_oos - old_oos):
            events.append({"kind": "oos_added", "rule": added})
        for removed in sorted(old_oos - new_oos):
            events.append({"kind": "oos_removed", "rule": removed})

    return events


def fmt_event(slug: str, title: str, ev: dict) -> str:
    program_url = f"https://yeswehack.com/programs/{slug}"
    head = f"<b>{title}</b>\n<a href=\"{program_url}\">{slug}</a>"
    k = ev["kind"]
    if k == "scope_added":
        s = ev["scope"]
        return (
            f"➕ <b>Scope ADDED</b>\n{head}\n"
            f"<code>{s.get('scope')}</code>\n"
            f"type: {s.get('type')} · value: {s.get('asset_value')}"
        )
    if k == "scope_removed":
        s = ev["scope"]
        return (
            f"➖ <b>Scope REMOVED</b>\n{head}\n"
            f"<code>{s.get('scope')}</code>\n"
            f"type: {s.get('type')} · value: {s.get('asset_value')}"
        )
    if k == "scope_severity_change":
        s = ev["scope"]
        return (
            f"🎯 <b>Scope SEVERITY CHANGE</b>\n{head}\n"
            f"<code>{s.get('scope')}</code>\n"
            f"{ev['old']} → <b>{ev['new']}</b>"
        )
    if k == "oos_added":
        return f"⛔ <b>Out-of-scope rule added</b>\n{head}\n<i>{ev['rule']}</i>"
    if k == "oos_removed":
        return f"✅ <b>Out-of-scope rule removed</b>\n{head}\n<i>{ev['rule']}</i>"
    if k == "field_change":
        return (
            f"🔧 <b>Program field changed</b>\n{head}\n"
            f"{ev['field']}: <code>{ev['old']}</code> → <code>{ev['new']}</code>"
        )
    if k == "program_new":
        return (
            f"🆕 <b>NEW PROGRAM</b>\n{head}\n"
            f"type: {ev.get('type')} · bounty: {ev.get('bounty_reward_min')}-{ev.get('bounty_reward_max')}"
        )
    if k == "program_removed":
        return f"🗑️ <b>Program REMOVED</b>\n{head}"
    return f"{k}: {ev}"


def send_telegram(token: str, chat_id: str, text: str, dry: bool = False) -> bool:
    if dry:
        log.info("[DRY] would send: %s", text.replace("\n", " | ")[:200])
        return True
    try:
        r = requests.post(
            TG_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not r.ok:
            log.error("telegram send failed: %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.error("telegram send exception: %s", e)
        return False


def append_audit(rows: list[dict]) -> None:
    if not rows:
        return
    with AUDIT_FILE.open("a") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def run() -> int:
    token = os.getenv("TG_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    dry = bool(os.getenv("DRY_RUN"))
    seed_only = bool(os.getenv("SEED_ONLY"))

    if not seed_only and (not token or not chat_id):
        log.error("TG_TOKEN and TG_CHAT_ID must be set (or run with SEED_ONLY=1)")
        return 2

    state = load_state()
    prev_programs: dict = state.get("programs", {})

    client = YWHClient(ua=os.getenv("YWH_UA") or "ywh-scope-monitor/1.0")
    log.info("fetching program list from YWH...")
    current_list = {}
    for item in client.iter_programs():
        slug = item.get("slug")
        if not slug:
            continue
        current_list[slug] = snapshot_list_item(item)
    log.info("fetched %d programs", len(current_list))

    detail_targets: list[str] = []
    for slug, lst in current_list.items():
        prev = prev_programs.get(slug) or {}
        prev_list = prev.get("list", {})
        if slug not in prev_programs:
            detail_targets.append(slug)
        elif prev_list.get("scopes_count") != lst.get("scopes_count"):
            detail_targets.append(slug)
        elif any(
            prev_list.get(f) != lst.get(f)
            for f in ("status", "archived", "disabled", "bounty", "vdp", "secured")
        ):
            detail_targets.append(slug)

    detail_targets = detail_targets[:MAX_DETAIL]
    log.info("fetching detail for %d programs (new + changed)", len(detail_targets))
    details: dict[str, dict] = {}
    for i, slug in enumerate(detail_targets, 1):
        try:
            d = client.get_program(slug)
            details[slug] = snapshot_detail(d)
            time.sleep(0.4)
        except Exception as e:
            log.warning("detail fetch failed for %s: %s", slug, e)

    new_programs: dict = {}
    for slug, lst in current_list.items():
        prev = prev_programs.get(slug) or {}
        new_programs[slug] = {
            "list": lst,
            "detail": details.get(slug, prev.get("detail")),
        }

    events: list[dict] = []
    for slug, new in new_programs.items():
        if slug not in prev_programs:
            lst = new["list"]
            events.append(
                {
                    "slug": slug,
                    "title": lst.get("title") or slug,
                    "event": {
                        "kind": "program_new",
                        "type": lst.get("type"),
                        "bounty_reward_min": lst.get("bounty_reward_min"),
                        "bounty_reward_max": lst.get("bounty_reward_max"),
                    },
                }
            )
            continue
        prog_events = diff_program(slug, prev_programs[slug], new)
        for ev in prog_events:
            events.append(
                {
                    "slug": slug,
                    "title": new["list"].get("title") or slug,
                    "event": ev,
                }
            )

    for slug in prev_programs.keys() - current_list.keys():
        events.append(
            {
                "slug": slug,
                "title": prev_programs[slug].get("list", {}).get("title") or slug,
                "event": {"kind": "program_removed"},
            }
        )

    log.info("computed %d change events", len(events))

    is_first_run = not prev_programs
    if seed_only or is_first_run:
        if is_first_run and not seed_only:
            log.info("first run detected — seeding state without sending %d events", len(events))
        else:
            log.info("seed-only mode — saving state and exiting")
        state["programs"] = new_programs
        state["last_run"] = int(time.time())
        save_state(state)
        return 0

    sent = 0
    audit_rows: list[dict] = []
    for ev_entry in events:
        text = fmt_event(ev_entry["slug"], ev_entry["title"], ev_entry["event"])
        ok = send_telegram(token, chat_id, text, dry=dry)
        audit_rows.append(
            {
                "ts": int(time.time()),
                "slug": ev_entry["slug"],
                "title": ev_entry["title"],
                "event": ev_entry["event"],
                "sent": ok,
            }
        )
        if ok:
            sent += 1
        time.sleep(0.35)

    append_audit(audit_rows)
    state["programs"] = new_programs
    state["last_run"] = int(time.time())
    save_state(state)
    log.info("done — %d events, %d notifications sent", len(events), sent)
    return 0


if __name__ == "__main__":
    sys.exit(run())
