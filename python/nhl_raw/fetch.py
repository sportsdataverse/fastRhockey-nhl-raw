"""HTTP layer for the NHL scraper — retrying GET against api-web / stats endpoints.

Mirror of ``scrape_nhl_raw.R``'s ``.safe_get_json`` (returns ``None`` on failure so the
raw assembly can proceed with whatever endpoints succeeded) and ``fastRhockey``'s
``.retry_request`` (exponential backoff on transient failures).
"""

from __future__ import annotations

import time

import requests

_UA = {"User-Agent": "nhl-raw/0.0.1 (sportsdataverse)"}
_GAMECENTER = "https://api-web.nhle.com/v1/gamecenter/{gid}/{ep}"


def get_json(
    url: str, *, retries: int = 4, timeout: int = 45, session: requests.Session | None = None
) -> dict | list | None:
    """GET ``url`` with exponential backoff; ``None`` on exhausted retries (R's NULL)."""
    sess = session or requests
    delay = 1.0
    for _ in range(retries):
        try:
            r = sess.get(url, timeout=timeout, headers=_UA)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 30.0)
    return None


def fetch_endpoint(game_id: int, endpoint: str, *, session: requests.Session | None = None) -> dict | None:
    """Fetch one ``/v1/gamecenter/{game_id}/{endpoint}`` payload."""
    return get_json(_GAMECENTER.format(gid=game_id, ep=endpoint), session=session)
