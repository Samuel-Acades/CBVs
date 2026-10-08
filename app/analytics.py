"""Aggregations + projections for the dashboard charts.

encounters  = rows in the Farmers tab (one farmer meeting each; repeats count)
submissions = distinct CBV report forms submitted that month ("sent reports")
reported    = self-reported 'Farmers Reached' summed from submissions (secondary)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from . import config, data as data_mod
from .bundles import matched_keys, matches as bundle_matches, received_by_month


def monthly_series(ds) -> list[dict]:
    enc: dict[str, dict] = defaultdict(lambda: {"encounters": 0, "cbvs": set(), "comments": 0, "sat_no": 0, "follow_no": 0})
    sub: dict[str, dict] = defaultdict(lambda: {"submissions": 0, "reported": 0, "cbvs": set(), "questions": 0})

    for e in ds.encounters:
        if not e.month:
            continue
        a = enc[e.month]
        a["encounters"] += 1
        a["cbvs"].add(e.cbv_key)
        if e.comment:
            a["comments"] += 1
        if e.satisfied.strip().lower() in ("ayi", "no", "a"):
            a["sat_no"] += 1
        if e.follow_up.strip().lower() in ("ayi", "no", "a"):
            a["follow_no"] += 1

    for s in ds.unique_submissions:
        if not s.month:
            continue
        b = sub[s.month]
        b["submissions"] += 1
        b["reported"] += s.reached
        b["cbvs"].add(s.cbv_key)
        if s.questions:
            b["questions"] += 1

    months = sorted(set(enc) | set(sub))
    now_m = datetime.now().strftime("%Y-%m")
    out = []
    for m in months:
        e, s = enc[m], sub[m]
        out.append(
            {
                "month": m,
                "encounters": e["encounters"],
                "submissions": s["submissions"],
                "reported_reached": s["reported"],
                "active_cbvs": len(e["cbvs"] | s["cbvs"]),
                "comments": e["comments"],
                "questions": s["questions"],
                "dissatisfied": e["sat_no"],
                "no_followup": e["follow_no"],
                "partial": m == now_m,
            }
        )
    return out


def follow_up_rows(ds, bundle_records, month: str) -> list[dict]:
    """List CBVs with reporting, contact, or bundle items to check for a month."""
    encounters: dict[str, int] = defaultdict(int)
    for encounter in ds.encounters:
        if encounter.month == month:
            encounters[encounter.cbv_key] += 1

    submission_counts: dict[str, int] = defaultdict(int)
    reported_reach: dict[str, int] = defaultdict(int)
    date_checks: dict[str, list[str]] = defaultdict(list)
    for submission in ds.unique_submissions:
        if submission.month != month:
            continue
        submission_counts[submission.cbv_key] += 1
        reported_reach[submission.cbv_key] += submission.reached
        _, timestamp_date = data_mod.to_date(submission.timestamp)
        if submission.date and timestamp_date:
            try:
                difference = abs(
                    (date.fromisoformat(submission.date) - date.fromisoformat(timestamp_date)).days
                )
            except ValueError:
                continue
            if difference > 7:
                date_checks[submission.cbv_key].append(
                    f"Form date {submission.date} differs from timestamp {timestamp_date} by {difference} days"
                )

    bundled = received_by_month(bundle_records).get(month, set())
    line_records = [record for record in bundle_records if record.extra.get("line")]
    line_keys = {record.cbv_key for record in line_records}
    all_cbvs = per_cbv(ds)
    rows = []
    for cbv in all_cbvs:
        key = cbv["key"]
        matching_line_keys = matched_keys(key, line_keys)
        line_record = max(
            (record for record in line_records if record.cbv_key in matching_line_keys),
            key=lambda record: (record.month, record.cbv_key == key),
            default=None,
        )
        encounter_count = encounters.get(key, 0)
        submission_count = submission_counts.get(key, 0)
        reach = reported_reach.get(key, 0)
        flags = []
        if not encounter_count:
            flags.append("No Farmers-tab encounters")
        if not submission_count:
            flags.append("No submission form")
        if reach > encounter_count:
            flags.append("Self-reported reach exceeds encounters")
        flags.extend(date_checks.get(key, []))
        if not line_record:
            flags.append("Phone number missing")
        if not bundle_matches(key, bundled):
            flags.append("No bundle in selected month")

        if flags:
            rows.append(
                {
                    "cbv": cbv["cbv"],
                    "key": key,
                    "district": cbv["district"],
                    "encounters": encounter_count,
                    "submission_count": submission_count,
                    "reported_reach": reach,
                    "phone": line_record.extra["line"] if line_record else "",
                    "last_bundle": max(
                        (record.month for record in bundle_records if bundle_matches(key, [record.cbv_key])),
                        default="",
                    ),
                    "flags": flags,
                }
            )

    rows.sort(key=lambda row: (-len(row["flags"]), -row["reported_reach"], row["cbv"]))
    return rows


def project_next(series: list[dict], points: int = 6) -> dict | None:
    """Least-squares trend over complete (non-partial) months -> forecast for the month after the latest one."""
    complete = [s for s in series if not s.get("partial")]
    if len(complete) < 2:
        return None
    partial = any(s.get("partial") for s in series)
    vals = [s["encounters"] for s in complete][-points:]
    n = len(vals)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(vals) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = 0.0 if denom == 0 else sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, vals)) / denom
    intercept = mean_y - slope * mean_x
    steps = 1 + (1 if partial else 0)  # past the partial month too, if any
    target_idx = (n - 1) + steps
    forecast = max(0, round(slope * target_idx + intercept))
    last, prev = vals[-1], vals[-2] if n > 1 else vals[-1]
    mom = round((last - prev) / prev * 100, 1) if prev else None
    latest = series[-1]["month"]
    return {
        "forecast": forecast,
        "avg": round(mean_y),
        "last": last,
        "mom_pct": mom,
        "slope": round(slope, 1),
        "points": n,
        "target_month": next_month_label(latest),
        "method": f"linear trend, {n} complete months",
    }


def run_rate(series: list[dict]) -> dict | None:
    """Forecast for the in-progress month from pace so far (e.g. 71 on day 7 -> ~305)."""
    from calendar import monthrange
    from datetime import datetime as _dt

    partial = next((s for s in series if s.get("partial")), None)
    if not partial:
        return None
    today = _dt.now()
    day = min(today.day, monthrange(today.year, today.month)[1])
    days = monthrange(today.year, today.month)[1]
    if day < 2:
        return None
    return {
        "month": partial["month"],
        "so_far": partial["encounters"],
        "day": day,
        "days": days,
        "forecast": round(partial["encounters"] / day * days),
    }


def next_month_label(last_month: str) -> str:
    try:
        y, m = int(last_month[:4]), int(last_month[5:7])
    except (ValueError, TypeError, IndexError):
        return ""
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


def per_cbv(ds, month: str | None = None) -> list[dict]:
    """CBV ranking for a month (or all time), most -> least encounters.

    In a month view only CBVs with activity that month are listed; all-time
    totals and undated-row counts are attached for context.
    """
    agg: dict[str, dict] = {}
    all_time: dict[str, int] = {}
    undated: dict[str, int] = {}

    def slot(key: str, name: str) -> dict:
        return agg.setdefault(
            key,
            {
                "cbv": name,
                "key": key,
                "encounters": 0,
                "submissions": 0,
                "reported": 0,
                "comments": 0,
                "questions": 0,
                "dissatisfied": 0,
                "district": "",
                "last_active": "",
                "months": set(),
            },
        )

    for e in ds.encounters:
        all_time[e.cbv_key] = all_time.get(e.cbv_key, 0) + 1
        if not e.month:
            undated[e.cbv_key] = undated.get(e.cbv_key, 0) + 1
        if month and e.month != month:
            continue
        a = slot(e.cbv_key, e.cbv)
        a["encounters"] += 1
        if e.month:
            a["months"].add(e.month)
        if e.comment:
            a["comments"] += 1
        if e.satisfied.strip().lower() in ("ayi", "no"):
            a["dissatisfied"] += 1
        if e.district and not a["district"]:
            a["district"] = e.district
        if e.date > a["last_active"]:
            a["last_active"] = e.date

    for s in ds.unique_submissions:
        if month and s.month != month:
            continue
        a = slot(s.cbv_key, s.cbv)
        a["submissions"] += 1
        a["reported"] += s.reached
        if s.month:
            a["months"].add(s.month)
        if s.questions:
            a["questions"] += 1
        if s.district and not a["district"]:
            a["district"] = s.district
        if s.date > a["last_active"]:
            a["last_active"] = s.date

    out = []
    for a in agg.values():
        a["active_months"] = len(a.pop("months"))
        a["all_time"] = all_time.get(a["key"], 0)
        a["undated"] = undated.get(a["key"], 0)
        out.append(a)
    out.sort(key=lambda x: (-x["encounters"], -x["submissions"], x["cbv"]))
    for i, a in enumerate(out, 1):
        a["rank"] = i
    return out


def feedback_items(ds, month: str | None = None, q: str = "", kind: str = "all") -> list[dict]:
    """Farmer comments (Farmers tab) + questions farmers asked (Submissions tab)."""
    items: list[dict] = []
    qn = q.strip().lower()

    if kind in ("all", "comment"):
        for e in ds.encounters:
            text = e.comment.strip()
            if not text or (month and e.month != month):
                continue
            if qn and qn not in text.lower() and qn not in e.cbv.lower() and qn not in e.farmer.lower():
                continue
            items.append(
                {
                    "kind": "Farmer comment",
                    "cbv": e.cbv,
                    "key": e.cbv_key,
                    "month": e.month,
                    "date": e.date,
                    "person": e.farmer,
                    "text": text,
                    "district": e.district,
                    "satisfied": e.satisfied,
                }
            )

    if kind in ("all", "question"):
        for s in ds.unique_submissions:
            text = s.questions.strip()
            if not text or (month and s.month != month):
                continue
            if qn and qn not in text.lower() and qn not in s.cbv.lower():
                continue
            items.append(
                {
                    "kind": "Question asked",
                    "cbv": s.cbv,
                    "key": s.cbv_key,
                    "month": s.month,
                    "date": s.date,
                    "person": "",
                    "text": text,
                    "district": s.district,
                    "satisfied": "",
                }
            )

    items.sort(key=lambda x: (x["month"], x["date"], x["cbv"]), reverse=True)
    return items


def low_encounters(ds, month: str, threshold: int | None = None) -> list[dict]:
    thr = threshold if threshold is not None else config.LOW_ENCOUNTER_THRESHOLD
    return [r for r in per_cbv(ds, month=month) if r["encounters"] <= thr]


def kpis(ds, bundle_records) -> dict:
    series = monthly_series(ds)
    months = [s["month"] for s in series]
    latest = months[-1] if months else ""
    last_complete = _last_complete_month()
    ref = last_complete if last_complete in months else (latest or last_complete)
    complete = [s for s in series if not s["partial"]]

    ref_row = next((s for s in series if s["month"] == ref), {})
    ref_idx = next((i for i, s in enumerate(complete) if s["month"] == ref), -1)
    prev_row = complete[ref_idx - 1] if ref_idx > 0 else {}

    got = received_by_month(bundle_records)
    received_ref = got.get(ref, set())

    growth = None
    if prev_row.get("encounters"):
        growth = round((ref_row.get("encounters", 0) - prev_row["encounters"]) / prev_row["encounters"] * 100, 1)

    all_cbvs = {e.cbv_key for e in ds.encounters} | {s.cbv_key for s in ds.submissions}
    # rule: encounters in the month == report submitted that month
    reported_ref = {e.cbv_key for e in ds.encounters if e.month == ref}
    active_ref = reported_ref | {s.cbv_key for s in ds.unique_submissions if s.month == ref}
    undated = sum(1 for e in ds.encounters if not e.month)

    proj = project_next(series)
    rr = run_rate(series)

    return {
        "total_cbvs": len(all_cbvs),
        "total_encounters": len(ds.encounters),
        "undated_encounters": undated,
        "months": len(series),
        "ref_month": ref,
        "latest_month": latest,
        "active_ref_month": len(active_ref),
        "reported_ref_month": len(reported_ref),
        "encounters_ref_month": ref_row.get("encounters", 0),
        "encounters_prev_month": prev_row.get("encounters", 0),
        "prev_month": prev_row.get("month", ""),
        "mom_growth_pct": growth,
        "bundles_ref_month": sum(1 for k in received_ref if bundle_matches(k, all_cbvs)),
        "feedback_count": sum(1 for e in ds.encounters if e.comment)
        + sum(1 for s in ds.unique_submissions if s.questions),
        "projection": proj,
        "run_rate": rr,
        "series": series,
    }


def _prev_month(m: str) -> str:
    try:
        y, mo = int(m[:4]), int(m[5:7])
    except (ValueError, TypeError, IndexError):
        return ""
    y, mo = (y - 1, 12) if mo == 1 else (y, mo - 1)
    return f"{y:04d}-{mo:02d}"


def _month_is_partial(m: str) -> bool:
    return m == datetime.now().strftime("%Y-%m")


def _last_complete_month() -> str:
    """Last completed calendar month relative to today (skips the in-progress month)."""
    now = datetime.now()
    return _prev_month(f"{now.year:04d}-{now.month:02d}")
