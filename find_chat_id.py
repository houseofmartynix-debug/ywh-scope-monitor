"""One-time helper: discover Telegram chat_id(s) that have messaged the bot.

Usage:
  1. In Telegram, open @ywhtheplanet_bot and send /start
  2. Run: TG_TOKEN=... python3 find_chat_id.py
  3. Copy the chat_id, set it as TG_CHAT_ID (locally or as a GitHub secret).
"""
from __future__ import annotations

import json
import os
import sys

import requests


def main() -> int:
    token = os.getenv("TG_TOKEN")
    if not token:
        print("Set TG_TOKEN env var first", file=sys.stderr)
        return 2
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    r.raise_for_status()
    data = r.json()
    seen: dict[int, dict] = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid and cid not in seen:
            seen[cid] = {
                "chat_id": cid,
                "type": chat.get("type"),
                "title": chat.get("title"),
                "username": chat.get("username"),
                "first_name": chat.get("first_name"),
                "last_name": chat.get("last_name"),
            }
    if not seen:
        print(
            "No chats found. Open Telegram, message the bot (/start), then run this again.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(list(seen.values()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
