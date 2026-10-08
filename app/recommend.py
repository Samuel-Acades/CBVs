"""Bundle recommendations based on activity and recorded bundle history."""
from __future__ import annotations

from collections import Counter, defaultdict

from .bundles import BundleRecord, matched_keys, matches, received_by_month


def _match_key(cbv_key: str, keys: set[str]) -> bool:
    return matches(cbv_key, keys)


def build(ds, bundle_records: list[BundleRecord], overrides: dict, ref_month: str) -> dict:
    by_month = received_by_month(bundle_records)
    bundle_months = sorted(m for m in by_month if m)
    line_records = [record for record in bundle_records if record.extra.get("line")]
    line_keys = {record.cbv_key for record in line_records}
    encounter_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for encounter in ds.encounters:
        if encounter.month:
            encounter_counts[encounter.month][encounter.cbv_key] += 1

    rows: dict[str, dict] = {}

    def slot(key: str, name: str) -> dict:
        return rows.setdefault(
            key,
            {
                "cbv": name,
                "key": key,
                "encounters": 0,
                "questions": 0,
                "comments": 0,
                "district": "",
            },
        )

    for e in ds.encounters:
        a = slot(e.cbv_key, e.cbv)
        if e.month == ref_month:
            a["encounters"] += 1
            if e.comment:
                a["comments"] += 1
            if e.district and not a["district"]:
                a["district"] = e.district

    for s in ds.unique_submissions:
        a = slot(s.cbv_key, s.cbv)
        if s.month == ref_month:
            if s.questions:
                a["questions"] += 1
            if s.district and not a["district"]:
                a["district"] = s.district

    ref_received = by_month.get(ref_month, set())
    results = []
    for key, a in rows.items():
        matching_line_keys = matched_keys(key, line_keys)
        line_record = max(
            (record for record in line_records if record.cbv_key in matching_line_keys),
            key=lambda record: (record.month, record.cbv_key == key),
            default=None,
        )
        has_encounters = a["encounters"] > 0
        received = _match_key(key, ref_received)

        last_bundle = ""
        for m in reversed([month for month in bundle_months if month <= ref_month]):
            if _match_key(key, by_month[m]):
                last_bundle = m
                break
        last_bundle_encounters = 0
        if last_bundle:
            month_counts = encounter_counts.get(last_bundle, Counter())
            matching_keys = matched_keys(key, month_counts)
            last_bundle_encounters = sum(month_counts[k] for k in matching_keys)

        ovr = overrides.get(key)
        reasons: list[str] = []

        if received:
            status = "received"
            reasons.append(f"Already received a bundle in {ref_month}")
        elif not has_encounters:
            status = "no_report"
            reasons.append(f"No encounters recorded in {ref_month}")
        else:
            status = "eligible"
            reasons.append(f"{a['encounters']} encounters")

        if ovr:
            if ovr["action"] == "exclude":
                status = "excluded"
                reasons.append(f"Excluded by you" + (f": {ovr['note']}" if ovr["note"] else ""))
            elif ovr["action"] == "include":
                status = "forced"
                reasons.append(f"Forced by you" + (f": {ovr['note']}" if ovr["note"] else ""))

        results.append(
            {
                **a,
                "phone": line_record.extra["line"] if line_record else "",
                "status": status,
                "reasons": reasons,
                "received": received,
                "last_bundle": last_bundle,
                "last_bundle_encounters": last_bundle_encounters,
                "override_note": ovr["note"] if ovr else "",
            }
        )

    order = {"forced": 0, "eligible": 1, "received": 2, "no_report": 3, "excluded": 4}
    results.sort(key=lambda x: (order.get(x["status"], 9), -x["encounters"], x["cbv"]))
    for i, r in enumerate([x for x in results if x["status"] in ("forced", "eligible")], 1):
        r["rank"] = i

    return {
        "ref_month": ref_month,
        "items": results,
        "recommended": [x for x in results if x["status"] in ("forced", "eligible")],
        "bundle_months": bundle_months,
        "counts": {
            "recommended": sum(1 for x in results if x["status"] in ("forced", "eligible")),
            "received": sum(1 for x in results if x["status"] == "received"),
            "no_report": sum(1 for x in results if x["status"] == "no_report"),
            "excluded": sum(1 for x in results if x["status"] == "excluded"),
        },
    }
