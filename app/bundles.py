"""Bundle history: reads the Excel/CSV files you drop into /bundles.

Month resolution order:
  1. month in the FILE NAME  (June, sept, 2026-09 ...)  <- most reliable
  2. a full date in the sheet's title rows (e.g. '31/07/2026')
  3. left as '(no month detected)'
Year: filename -> title date -> current year.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import config, normalize, data as data_mod

VALID_EXT = {".xlsx", ".xls", ".csv"}

MONTH_WORDS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?!\d)")
YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass
class BundleRecord:
    cbv_raw: str
    cbv: str
    cbv_key: str
    month: str
    received_date: str
    source_file: str
    extra: dict = field(default_factory=dict)


def matched_keys(cbv_key: str, keys) -> set[str]:
    """Return exact/shortened name matches, or one clear spelling match."""
    keys = set(keys)
    exact = {key for key in keys if normalize.key_matches(cbv_key, [key])}
    if exact:
        return exact

    scores = sorted(
        ((normalize.similarity(cbv_key, key), key) for key in keys),
        reverse=True,
    )
    if (
        scores
        and scores[0][0] >= 80
        and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 8)
    ):
        return {scores[0][1]}
    return set()


def matches(cbv_key: str, keys) -> bool:
    """Match exact/shortened names, then high-confidence, unambiguous spellings."""
    return bool(matched_keys(cbv_key, keys))


def _month_from_filename(name: str) -> tuple[int, str]:
    """-> (month 1-12, year 'YYYY' or '')"""
    stem = Path(name).stem
    m = re.search(r"(20\d{2})[\s_\-]?(\d{1,2})", stem)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(2)), m.group(1)

    year = ""
    y = YEAR_RE.search(stem)
    if y:
        year = y.group(1)

    month = 0
    for word in re.split(r"[^a-z]+", stem.lower()):
        if word in MONTH_WORDS:
            month = MONTH_WORDS[word]
    return month, year


def _title_date(df: pd.DataFrame, h_idx: int) -> tuple[str, str]:
    """Find a full date in the rows ABOVE the header (sheet titles like '31/07/2026 Bundle')."""
    for i in range(min(h_idx, 4)):
        for v in df.iloc[i].tolist():
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            s = str(v)
            d = DATE_RE.search(s)
            if d:
                try:
                    day, mon, yr = int(d.group(1)), int(d.group(2)), int(d.group(3))
                    yr = yr + 2000 if yr < 100 else yr
                    if 1 <= mon <= 12 and 1 <= day <= 31:
                        return f"{yr:04d}-{mon:02d}", f"{yr:04d}-{mon:02d}-{day:02d}"
                except ValueError:
                    pass
            if re.fullmatch(r"20\d{2}-\d{2}-\d{2}[ 0:0-9.]*", s.strip()):
                m, dte = data_mod.to_date(s)
                if m:
                    return m, dte
    return "", ""


def _read(path: Path) -> list[pd.DataFrame]:
    if path.suffix.lower() == ".csv":
        try:
            return [pd.read_csv(path, header=None, dtype=object)]
        except UnicodeDecodeError:
            return [pd.read_csv(path, header=None, dtype=object, encoding="latin-1")]
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
        return list(sheets.values())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{path.name}: {e}") from e


_SKIP_NAMES = {"name", "names", "cbv name", "district", "month", "date", "total", "status"}
_JUNK = re.compile(
    r"(?i)\d{1,2}[/-]\d{1,2}|\b(bundle|bundles|requisition|request|justification|form|total|sheet)\b"
)


def _is_junk(raw: str) -> bool:
    s = raw.strip()
    if not s or s[0].isdigit():
        return True
    if _JUNK.search(s):
        return True
    if not re.search(r"[A-Za-z]{3}", s):
        return True
    return s.lower() in _SKIP_NAMES or s.lower().startswith(("total", "grand total"))


def _pick(hs: list[str], *needles: str, exclude: str = "") -> int | None:
    for n in needles:
        c = data_mod._find_col(hs, n, exclude=exclude)
        if c is not None:
            return c
    return None


def _extract(path: Path) -> tuple[list[BundleRecord], list[str]]:
    fn_month, fn_year = _month_from_filename(path.name)
    out: list[BundleRecord] = []
    notes: list[str] = []
    now_year = str(pd.Timestamp.now().year)

    for df in _read(path):
        if df is None or df.empty:
            continue
        h_idx, hs = data_mod._header_row(df)
        c_name = _pick(hs, "name", "cbv", "recipient", "volunteer", exclude="number")
        if c_name is None:
            notes.append(f"{path.name}: a sheet has no recipient-name column - ignored")
            continue

        c_date = _pick(hs, "date")
        c_dist = _pick(hs, "district", "boma")
        c_line = _pick(hs, "line", "phone", "contact")
        c_status = _pick(hs, "status")

        title_month, title_date = _title_date(df, h_idx)
        # month priority: FILE NAME > title date > none (year: filename > title > now)
        if fn_month:
            year = fn_year or title_month[:4] or now_year
            sheet_month = f"{year}-{fn_month:02d}"
        else:
            sheet_month = title_month

        for _, row in df.iloc[h_idx + 1 :].iterrows():
            vals = row.tolist()
            if c_name >= len(vals):
                continue
            name = data_mod._clean(vals[c_name])
            if _is_junk(name):
                continue

            month, received = ("", "")
            if c_date is not None and c_date < len(vals):
                month, received = data_mod.to_date(vals[c_date])
            if not month:
                month = sheet_month
                received = title_date if title_month == sheet_month else ""
            elif title_month == sheet_month and not received:
                received = title_date

            extra = {}
            for label, idx in (("district", c_dist), ("line", c_line), ("status", c_status)):
                if idx is not None and idx < len(vals):
                    v = data_mod._clean(vals[idx])
                    if v:
                        extra[label] = v

            out.append(
                BundleRecord(
                    cbv_raw=name,
                    cbv=normalize.display_name(name),
                    cbv_key=normalize.name_key(name),
                    month=month,
                    received_date=received,
                    source_file=path.name,
                    extra=extra,
                )
            )

    if not out:
        notes.append(f"{path.name}: no CBV rows found")
    return out, notes


def scan() -> tuple[list[BundleRecord], list[str]]:
    records: list[BundleRecord] = []
    errors: list[str] = []
    for path in sorted(config.BUNDLES_DIR.iterdir()):
        if path.name.startswith("~$") or path.is_dir() or path.suffix.lower() not in VALID_EXT:
            continue
        try:
            recs, notes = _extract(path)
            records.extend(recs)
            errors.extend(notes)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    return _deduplicate_records(records), errors


def apply_merges(records: list[BundleRecord], merges: dict[str, str]) -> list[BundleRecord]:
    """Apply dashboard name decisions to bundle recipients and collapse duplicates."""
    for record in records:
        if record.cbv_key in merges:
            record.cbv = normalize.resolve_merge(record.cbv, merges)
            record.cbv_key = normalize.name_key(record.cbv)
    return _deduplicate_records(records)


def _deduplicate_records(records: list[BundleRecord]) -> list[BundleRecord]:
    unique: list[BundleRecord] = []
    by_month_and_cbv: dict[tuple[str, str], BundleRecord] = {}
    for record in records:
        if not record.month or not record.cbv_key:
            unique.append(record)
            continue

        key = (record.month, record.cbv_key)
        existing = by_month_and_cbv.get(key)
        if existing is None:
            by_month_and_cbv[key] = record
            unique.append(record)
            continue

        if not existing.received_date and record.received_date:
            existing.received_date = record.received_date
        existing.extra.update(record.extra)

    return unique


def received_by_month(records: list[BundleRecord]) -> dict[str, set[str]]:
    """{YYYY-MM: {cbv_key, ...}}"""
    out: dict[str, set[str]] = {}
    for r in records:
        if r.month:
            out.setdefault(r.month, set()).add(r.cbv_key)
    return out


def months_with_bundles(records: list[BundleRecord]) -> list[str]:
    return sorted({r.month for r in records if r.month})


def save_upload(filename: str, content: bytes) -> Path:
    safe = re.sub(r"[^\w\.\- ]", "_", filename).strip() or "upload.xlsx"
    dest = config.BUNDLES_DIR / safe
    dest.write_bytes(content)
    return dest
