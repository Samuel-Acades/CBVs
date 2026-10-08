"""ACADES CBV sheet -> normalized dataset.

Sheet tabs (read-only):
  * Submissions  - one row per CBV report form (questions farmers asked, self-reported reach)
  * Farmers      - one row per farmer encounter (comments, satisfaction, follow-up)
  * Monthly CBV Summary - stale manual pivot, NOT used for numbers (row tabs win)
"""
from __future__ import annotations

import io
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from . import sheets, config, normalize

log = logging.getLogger("data")

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "dataset": None}

SUBMISSIONS_TAB = "Submissions"
FARMERS_TAB = "Farmers"


@dataclass
class Encounter:
    cbv_raw: str
    cbv: str
    cbv_key: str
    month: str
    date: str
    farmer: str
    district: str
    ta: str
    group: str
    satisfied: str
    follow_up: str
    comment: str


@dataclass
class Submission:
    cbv_raw: str
    cbv: str
    cbv_key: str
    month: str
    date: str
    timestamp: str
    district: str
    ta: str
    group: str
    reached: int
    num_farmers: int
    questions: str
    is_duplicate: bool = False


@dataclass
class Dataset:
    encounters: list[Encounter]
    submissions: list[Submission]
    tabs: list[str]
    headers: dict[str, list[str]]
    raw_names: list[str]
    months: list[str]
    loaded_at: float
    source: str
    problems: list[str] = field(default_factory=list)

    @property
    def total_encounters(self) -> int:
        return len(self.encounters)

    @property
    def unique_submissions(self) -> list[Submission]:
        return [s for s in self.submissions if not s.is_duplicate]


def _clean(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def to_int(v) -> int:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(round(v))
    import re

    m = re.search(r"-?\d[\d,]*", str(v))
    if not m:
        return 0
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return 0


_ISO_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%d/%Y",
)


def to_date(v) -> tuple[str, str]:
    """-> (YYYY-MM, ISO date). Tries exact formats first; never mis-parses ISO dates."""
    if v is None:
        return "", ""
    if isinstance(v, float) and pd.isna(v):
        return "", ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m"), v.date().isoformat()
    if hasattr(v, "year") and hasattr(v, "month") and not isinstance(v, str):
        try:
            return f"{v.year:04d}-{v.month:02d}", v.date().isoformat() if hasattr(v, "date") else ""
        except Exception:  # noqa: BLE001
            return "", ""
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none", "null"):
        return "", ""
    for fmt in _ISO_FORMATS:
        try:
            ts = datetime.strptime(s, fmt)
            return ts.strftime("%Y-%m"), ts.date().isoformat()
        except ValueError:
            continue
    try:
        ts = pd.to_datetime(s, dayfirst=True, errors="raise")
        if not pd.isna(ts):
            return f"{ts.year:04d}-{ts.month:02d}", ts.date().isoformat()
    except Exception:  # noqa: BLE001
        return "", ""
    return "", ""


def _find_col(headers: list[str], *needles: str, exclude: str = "") -> int | None:
    """First column whose header contains ALL needles (case-insensitive)."""
    for i, h in enumerate(headers):
        hn = h.lower()
        if exclude and exclude in hn:
            continue
        if all(n in hn for n in needles):
            return i
    return None


def _header_row(df: pd.DataFrame) -> tuple[int, list[str]]:
    for i in range(min(8, len(df))):
        vals = [_clean(v) for v in df.iloc[i].tolist()]
        joined = " ".join(vals).lower()
        if "name" in joined and ("date" in joined or "district" in joined or "satisfied" in joined or "reached" in joined):
            return i, vals
    return 0, [_clean(v) for v in df.iloc[0].tolist()]


def _default_month(today: datetime | None = None) -> str:
    """Default reporting month: last *completed* calendar month (fallback: latest with data)."""
    t = today or datetime.now()
    y, m = (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def parse(workbook: bytes) -> Dataset:
    problems: list[str] = []
    frames = pd.read_excel(io.BytesIO(workbook), sheet_name=None, header=None, dtype=object)
    tabs = list(frames.keys())

    if SUBMISSIONS_TAB not in frames or FARMERS_TAB not in frames:
        problems.append(
            f"Expected tabs '{SUBMISSIONS_TAB}' and '{FARMERS_TAB}', found: {', '.join(tabs)}. "
            "Check the sheet tab names."
        )

    headers: dict[str, list[str]] = {}
    encounters: list[Encounter] = []
    submissions: list[Submission] = []
    raw_names: list[str] = []
    months: set[str] = set()

    # ---------------- Submissions
    if SUBMISSIONS_TAB in frames:
        df = frames[SUBMISSIONS_TAB]
        h_idx, headers[SUBMISSIONS_TAB] = _header_row(df)
        hs = headers[SUBMISSIONS_TAB]
        c_name = _find_col(hs, "name", exclude="number") or _find_col(hs, "name")
        c_date = _find_col(hs, "submission", "date") or _find_col(hs, "date")
        c_ts = _find_col(hs, "timestamp")
        c_reached = _find_col(hs, "reached")
        c_num = _find_col(hs, "number", "farmers")
        c_q = _find_col(hs, "question") or _find_col(hs, "maf")
        c_dist = _find_col(hs, "district") or _find_col(hs, "boma")
        c_ta = next((i for i, h in enumerate(hs) if h.lower().strip() in ("t/a", "ta", "traditional authority")), None)
        c_group = _find_col(hs, "group") or _find_col(hs, "gulu")

        if c_name is None:
            problems.append("Submissions tab: could not find the CBV name column.")
        else:
            seen: dict[tuple, int] = {}
            for _, row in df.iloc[h_idx + 1 :].iterrows():
                vals = row.tolist()
                get = lambda i: _clean(vals[i]) if i is not None and i < len(vals) else ""  # noqa: E731
                name = get(c_name)
                if not name or not re.search(r"[A-Za-z]{2}", name):
                    continue
                if name.lower().startswith(("total", "grand total")):
                    continue
                month, date = to_date(get(c_date)) if c_date is not None else ("", "")
                ts = get(c_ts)
                if not month and ts:
                    month, _ = to_date(ts)
                s = Submission(
                    cbv_raw=name,
                    cbv=normalize.display_name(name),
                    cbv_key=normalize.name_key(name),
                    month=month,
                    date=date,
                    timestamp=ts,
                    district=get(c_dist),
                    ta=get(c_ta),
                    group=get(c_group),
                    reached=to_int(get(c_reached)),
                    num_farmers=to_int(get(c_num)),
                    questions=get(c_q),
                )
                sig = (s.cbv_key, s.date, s.reached, s.questions[:60])
                s.is_duplicate = sig in seen
                seen[sig] = seen.get(sig, 0) + 1
                submissions.append(s)
                raw_names.append(name)
                if month:
                    months.add(month)

    # ---------------- Farmers
    if FARMERS_TAB in frames:
        df = frames[FARMERS_TAB]
        h_idx, headers[FARMERS_TAB] = _header_row(df)
        hs = headers[FARMERS_TAB]
        c_date = _find_col(hs, "submission", "date") or _find_col(hs, "date")
        c_cbv = _find_col(hs, "cbv") or _find_col(hs, "name")
        c_farmer = _find_col(hs, "farmer", "name") or _find_col(hs, "farmer")
        c_dist = _find_col(hs, "district") or _find_col(hs, "boma")
        c_ta = next((i for i, h in enumerate(hs) if h.lower().strip() in ("t/a", "ta", "traditional authority")), None)
        c_group = _find_col(hs, "group")
        c_sat = _find_col(hs, "satisf")
        c_fu = _find_col(hs, "follow")
        c_com = _find_col(hs, "comment") or _find_col(hs, "zowonjezera")

        if c_cbv is None:
            problems.append("Farmers tab: could not find the CBV name column.")
        else:
            for _, row in df.iloc[h_idx + 1 :].iterrows():
                vals = row.tolist()
                get = lambda i: _clean(vals[i]) if i is not None and i < len(vals) else ""  # noqa: E731
                name = get(c_cbv)
                if not name or not re.search(r"[A-Za-z]{2}", name):
                    continue
                month, date = to_date(get(c_date)) if c_date is not None else ("", "")
                encounters.append(
                    Encounter(
                        cbv_raw=name,
                        cbv=normalize.display_name(name),
                        cbv_key=normalize.name_key(name),
                        month=month,
                        date=date,
                        farmer=get(c_farmer),
                        district=get(c_dist),
                        ta=get(c_ta),
                        group=get(c_group),
                        satisfied=get(c_sat),
                        follow_up=get(c_fu),
                        comment=get(c_com),
                    )
                )
                raw_names.append(name)
                if month:
                    months.add(month)

    if not encounters and not submissions:
        problems.append("No rows parsed - sheet may be empty or headers changed.")

    # Rows whose Submission Date was cleared in the sheet: the sheet's own
    # 'Monthly CBV Summary' counts them under the month of the next dated row
    # (verified: block matches 2026-09 exactly), so do the same here.
    inferred = _infer_undated_dates(encounters)
    if inferred:
        detail = ", ".join(f"{n} in {m}" for m, n in sorted(inferred.items()))
        problems.append(
            f"{sum(inferred.values())} Farmers rows have an empty Submission Date "
            f"and were counted by month as: {detail} (matches 'Monthly CBV Summary')."
        )
    months = sorted({e.month for e in encounters if e.month} | {s.month for s in submissions if s.month})

    # stale summary check
    if "Monthly CBV Summary" in frames and encounters:
        problems_note = _summary_is_stale(frames["Monthly CBV Summary"], encounters)
        if problems_note:
            problems.append(problems_note)

    return Dataset(
        encounters=encounters,
        submissions=submissions,
        tabs=tabs,
        headers=headers,
        raw_names=sorted(set(raw_names)),
        months=sorted(months),
        loaded_at=time.time(),
        source="google-sheet",
        problems=problems,
    )


def _infer_undated_dates(encounters: list[Encounter]) -> dict[str, int]:
    """Give date-less encounter rows the month of the NEAREST FOLLOWING dated row.

    The sheet's big undated block sits between the August rows and the
    25-Sep rows, and the sheet's own summary assigns it to September -
    so backward-fill from the next dated row (never from the previous one).
    """
    inferred: dict[str, int] = {}
    next_month = ""
    for e in reversed(encounters):
        if e.month:
            next_month = e.month
        elif next_month:
            e.month = next_month
            inferred[next_month] = inferred.get(next_month, 0) + 1
    return inferred


def _summary_is_stale(summary_df: pd.DataFrame, encounters: list[Encounter]) -> str:
    """The sheet's own Monthly CBV Summary is hand-pasted; warn when it drifts."""
    try:
        computed: dict[str, int] = {}
        for e in encounters:
            if e.month:
                computed[e.month] = computed.get(e.month, 0) + 1
        listed: dict[str, int] = {}
        for _, row in summary_df.iterrows():
            vals = [_clean(v) for v in row.tolist()]
            for i, v in enumerate(vals):
                if v and i + 2 < len(vals) and vals[i + 1]:
                    m, _ = to_date(v)
                    if m and re_full_month(v):
                        listed[m] = listed.get(m, 0) + to_int(vals[i + 2])
        for m, n in computed.items():
            if m in listed and listed[m] != n:
                return (
                    f"'Monthly CBV Summary' tab looks stale: it shows {listed[m]} for {m} "
                    f"but row-level data has {n}. Dashboard uses row-level data."
                )
    except Exception:  # noqa: BLE001 - never break the dashboard over a heuristic
        return ""
    return ""


def re_full_month(v: str) -> bool:
    import re

    return bool(re.match(r"^(20\d{2}[-/]\d{1,2}|\d{1,2}/\d{4}|[A-Za-z]{3,9}[- ]20\d{2})", v.strip()))


# ---------------------------------------------------------------- load / merge

def build_dataset(force: bool = False) -> Dataset:
    with _lock:
        cached: Dataset | None = _cache["dataset"]
        if not force and cached and time.time() - _cache["at"] < config.CACHE_TTL:
            return cached
        raw = sheets.fetch_workbook(refresh=force)
        ds = parse(raw)
        _cache["dataset"] = ds
        _cache["at"] = time.time()
        return ds


def apply_merges(ds: Dataset, merges: dict[str, str]) -> Dataset:
    if not merges:
        return ds
    import copy

    ds = copy.copy(ds)
    ds.encounters = [copy.copy(e) for e in ds.encounters]
    ds.submissions = [copy.copy(s) for s in ds.submissions]

    for e in ds.encounters:
        target = normalize.resolve_merge(e.cbv, merges) if e.cbv_key in merges else ""
        if target:
            e.cbv = target
            e.cbv_key = normalize.name_key(target)
    for s in ds.submissions:
        target = normalize.resolve_merge(s.cbv, merges) if s.cbv_key in merges else ""
        if target:
            s.cbv = target
            s.cbv_key = normalize.name_key(target)

    names = {e.cbv for e in ds.encounters} | {s.cbv for s in ds.submissions}
    ds.raw_names = sorted(names | set(ds.raw_names))
    return ds


def load(force: bool = False, merges: dict[str, str] | None = None) -> Dataset:
    ds = build_dataset(force=force)
    return apply_merges(ds, merges or {})
