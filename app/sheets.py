"""Read-only Google Sheets access.

Hard rule: this module only ever GETs from Google. No write endpoints are used.
"""
from __future__ import annotations

import time
import threading
import logging

import httpx

from . import config

log = logging.getLogger("sheets")

_LOCK = threading.Lock()
_MEM: dict[str, tuple[float, bytes]] = {}

EXPORT_CSV = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv&gid={gid}"
EXPORT_XLSX = "https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
}


class SheetAccessError(RuntimeError):
    pass


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    last: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
                r = client.get(url)
                if r.status_code == 200 and r.content:
                    return r.content
                last = SheetAccessError(
                    f"HTTP {r.status_code} from Google for {url[:80]}... "
                    "(sheet must be shared as 'Anyone with the link = Viewer')"
                )
        except Exception as e:  # noqa: BLE001 - retry any transport error
            last = e
        time.sleep(0.8 * (attempt + 1))
    raise last if last else SheetAccessError("unknown error")


def _cached(key: str, refetch) -> bytes:
    with _LOCK:
        hit = _MEM.get(key)
        if hit and time.time() - hit[0] < config.CACHE_TTL:
            return hit[1]
    data = refetch()
    with _LOCK:
        _MEM[key] = (time.time(), data)
    return data


def fetch_csv(gid: int | None = None, refresh: bool = False) -> str:
    gid = config.SHEET_GID if gid is None else gid
    key = f"csv:{gid}"
    if refresh:
        with _LOCK:
            _MEM.pop(key, None)
    data = _cached(key, lambda: _http_get(EXPORT_CSV.format(id=config.SHEET_ID, gid=gid)))
    return data.decode("utf-8-sig", errors="replace")


def fetch_workbook(refresh: bool = False) -> bytes:
    """Whole workbook (all tabs) as xlsx."""
    key = "workbook"
    if refresh:
        with _LOCK:
            _MEM.pop(key, None)
    return _cached(key, lambda: _http_get(EXPORT_XLSX.format(id=config.SHEET_ID)))


def probe() -> dict:
    try:
        wb = fetch_workbook(refresh=True)
        return {"ok": True, "bytes": len(wb)}
    except SheetAccessError as e:
        return {"ok": False, "error": str(e)}
