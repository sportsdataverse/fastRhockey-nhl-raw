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


class FetchError(RuntimeError):
    """The fetch FAILED, so the answer is UNKNOWN.

    Deliberately distinct from a ``None`` return, which means the fetch
    SUCCEEDED and the answer is "there is no such record". Collapsing the two is
    silent data loss: a caller that counts a rate-limit or a connection reset as
    "this entity does not exist" reports a healthy no-op run while having
    captured nothing. Only raised when ``strict=True``.
    """


def get_json(
    url: str,
    *,
    retries: int = 4,
    timeout: int = 45,
    session: requests.Session | None = None,
    strict: bool = False,
) -> dict | list | None:
    """GET ``url`` with exponential backoff.

    Default (``strict=False``) is the historical contract this scraper is built
    on: ``None`` for every non-200, mirroring R's ``.safe_get_json`` NULL so raw
    assembly proceeds with whatever endpoints succeeded.

    ``strict=True`` narrows ``None`` to mean ONLY a 404 -- the fetch worked and
    there is genuinely no such record. Everything else (403, an exhausted 429/5xx
    budget, a connection reset, a timeout, a body that will not parse) raises
    :class:`FetchError`. Callers that need to tell "absent" from "broken" -- a
    scheduled sweep whose exit code is the only failure signal -- must pass it.
    """
    sess = session or requests
    delay = 1.0
    last = "no attempt made"
    for attempt in range(retries):
        try:
            r = sess.get(url, timeout=timeout, headers=_UA)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None  # fetch succeeded; the record does not exist
            # Only transient statuses are worth retrying; a permanent non-404
            # (403/410/...) returns immediately instead of stalling.
            if r.status_code not in (429, 500, 502, 503, 504):
                if strict:
                    raise FetchError(f"{url} -> HTTP {r.status_code}")
                return None
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            # RequestException covers ConnectionError, Timeout AND the
            # JSONDecodeError from r.json() on a non-JSON body.
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries - 1:
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    if strict:
        raise FetchError(f"{url} failed after {retries} attempts ({last})")
    return None


def fetch_endpoint(
    game_id: int, endpoint: str, *, session: requests.Session | None = None, strict: bool = False
) -> dict | None:
    """Fetch one ``/v1/gamecenter/{game_id}/{endpoint}`` payload.

    Pass ``strict=True`` when a ``None`` would be BANKED -- written to disk, or
    used to decide there is nothing to do. See :func:`get_json`.
    """
    return get_json(_GAMECENTER.format(gid=game_id, ep=endpoint), session=session, strict=strict)
