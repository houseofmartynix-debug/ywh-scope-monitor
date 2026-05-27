"""YesWeHack public API client.

Fetches program list + per-program scope/reward detail.
No auth required for public programs.

API base: https://api.yeswehack.com
- GET /programs?page=N        — paginated list (42 results/page)
- GET /programs/{slug}        — full program detail (scopes, out_of_scope, rewards)
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.yeswehack.com"
DEFAULT_TIMEOUT = 30
DEFAULT_UA = "ywh-scope-monitor/1.0 (+telegram bot; researcher: mrcslvknm)"


class YWHClient:
    def __init__(self, ua: str = DEFAULT_UA, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "user-agent": ua,
            }
        )
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None, retries: int = 3) -> dict:
        url = f"{API_BASE}{path}"
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    wait = int(r.headers.get("retry-after", 5))
                    log.warning("rate limited, sleeping %ss", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                log.warning("attempt %d/%d failed for %s: %s", attempt + 1, retries, path, e)
                time.sleep(2 ** attempt)
        raise RuntimeError(f"giving up on {path}: {last_err}")

    def iter_programs(self) -> Iterator[dict]:
        """Yield every public program (list-level fields only)."""
        page = 1
        while True:
            data = self._get("/programs", params={"page": page})
            items = data.get("items", [])
            if not items:
                return
            for item in items:
                yield item
            pag = data.get("pagination", {})
            if page >= pag.get("nb_pages", 0):
                return
            page += 1

    def get_program(self, slug: str) -> dict:
        """Full program detail incl scopes/out_of_scope/rewards."""
        return self._get(f"/programs/{slug}")
