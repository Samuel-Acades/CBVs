from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from google import genai

from app import analytics, bundles as bundles_mod, data as data_mod, recommend, store


def build_cbv_rows(ds, bundle_records, ref_month: str):
    by_cbv = defaultdict(Counter)
    for e in ds.encounters:
        if e.month:
            by_cbv[e.cbv_key][e.month] += 1

    q_by = defaultdict(int)
    sub_months = defaultdict(set)
    for s in ds.unique_submissions:
        if s.month:
            sub_months[s.cbv_key].add(s.month)
        if s.questions:
            q_by[s.cbv_key] += 1

    last_bundle = {}
    bundle_months = defaultdict(list)
    for r in bundle_records:
        if r.month:
            bundle_months[r.cbv_key].append(r.month)
            if r.cbv_key not in last_bundle or r.month > last_bundle[r.cbv_key]:
                last_bundle[r.cbv_key] = r.month

    names = {}
    districts = {}
    for e in ds.encounters:
        names.setdefault(e.cbv_key, e.cbv)
        if e.district:
            districts.setdefault(e.cbv_key, e.district)
    for s in ds.submissions:
        names.setdefault(s.cbv_key, s.cbv)
        if s.district:
            districts.setdefault(s.cbv_key, s.district)

    months = ds.months
    rows = []
    for key in sorted(set(by_cbv) | set(sub_months)):
        monthly = {m: by_cbv[key].get(m, 0) for m in months}
        total = sum(monthly.values())
        active = [m for m, c in monthly.items() if c > 0]
        recent = months[-3:]
        older = months[:-3]
        recent_total = sum(monthly.get(m, 0) for m in recent)
        older_avg = (sum(monthly.get(m, 0) for m in older) / len(older)) if older else 0
        recent_avg = (recent_total / len(recent)) if recent else 0
        first = active[0] if active else ""
        last = active[-1] if active else ""
        rows.append({
            "key": key,
            "name": names.get(key, key),
            "district": districts.get(key, ""),
            "monthly_encounters": monthly,
            "total_encounters": total,
            "months_active": len(active),
            "first_active": first,
            "last_active": last,
            "recent_3m_total": recent_total,
            "recent_3m_avg": round(recent_avg, 2),
            "older_month_avg": round(older_avg, 2),
            "questions_asked": q_by.get(key, 0),
            "last_bundle_month": last_bundle.get(key, ""),
            "bundle_months": sorted(set(bundle_months.get(key, []))),
            "received_bundle_in_ref_month": any(m == ref_month for m in bundle_months.get(key, [])),
        })

    rows.sort(key=lambda r: (-r["recent_3m_total"], -r["total_encounters"], r["name"]))
    return rows


def main():
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY missing in environment/.env")

    merges = store.get_merges()
    ds = data_mod.load(force=False, merges=merges)
    bundle_records, bundle_errors = bundles_mod.scan()
    bundle_records = bundles_mod.apply_merges(bundle_records, merges)
    ref_month = analytics._last_complete_month()
    if ref_month not in ds.months and ds.months:
        ref_month = ds.months[-1]

    rows = build_cbv_rows(ds, bundle_records, ref_month)
    existing_rec = recommend.build(ds, bundle_records, store.get_overrides(), ref_month)

    prompt = {
        "task": "Create a shortlist of CBVs highly recommended to receive bundles",
        "constraints": {
            "max_recommended": 35,
            "hard_max": 40,
            "prefer_count": "35 or fewer unless strong justification",
            "consider": [
                "strong performance over months",
                "recent consistency/activity",
                "promising newcomers with good recent activity",
                "avoid duplicates/merged identities as data already merged",
                "do not recommend CBVs who already received bundle in ref month if any",
            ],
        },
        "context": {
            "ref_month": ref_month,
            "months_in_data": ds.months,
            "bundle_errors": bundle_errors,
            "existing_rule_based_recommended_count": len(existing_rec["recommended"]),
        },
        "cbvs": rows,
        "output_format": {
            "type": "json",
            "schema": {
                "summary": "string",
                "recommended": [
                    {
                        "rank": "number",
                        "name": "string",
                        "district": "string",
                        "reason": "string",
                        "signals": ["string"],
                        "last_bundle_month": "string|null",
                        "recent_3m_total": "number",
                        "total_encounters": "number",
                    }
                ],
                "watchlist": [
                    {"name": "string", "district": "string", "reason": "string"}
                ],
                "excluded_notable": [
                    {"name": "string", "reason": "string"}
                ],
            },
        },
    }

    client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            "You are an analyst helping select CBVs for bundle distribution. "
            "Use only the provided data. Be fair, prioritize sustained performance and promising recent newcomers. "
            "Return valid JSON only, no markdown fences.",
            json.dumps(prompt, ensure_ascii=False),
        ],
        config={
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    )

    text = (response.text or "").strip()
    data = json.loads(text)

    recommended = data.get("recommended", [])
    if len(recommended) > 40:
        data["recommended"] = recommended[:40]
        data.setdefault("notes", []).append("Trimmed to hard max 40.")

    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    out_json = out_dir / "gemini_bundle_shortlist.json"
    out_md = out_dir / "gemini_bundle_shortlist.md"
    out_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"# Gemini Bundle Shortlist ({ref_month})",
        "",
        data.get("summary", "").strip(),
        "",
        f"Recommended: {len(data.get('recommended', []))}",
        "",
        "| # | CBV | District | Recent 3m | Total | Last bundle | Reason |",
        "|---:|---|---|---:|---:|---|---|",
    ]
    for r in data.get("recommended", []):
        reason = str(r.get("reason", "")).replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {r.get('rank','')} | {r.get('name','')} | {r.get('district','')} | {r.get('recent_3m_total','')} | {r.get('total_encounters','')} | {r.get('last_bundle_month') or 'Never'} | {reason} |"
        )

    if data.get("watchlist"):
        lines += ["", "## Watchlist", ""]
        for w in data["watchlist"]:
            lines.append(f"- **{w.get('name','')}** ({w.get('district','')}): {w.get('reason','')}")

    if data.get("excluded_notable"):
        lines += ["", "## Notable Exclusions", ""]
        for e in data["excluded_notable"]:
            lines.append(f"- **{e.get('name','')}**: {e.get('reason','')}")

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"Recommended: {len(data.get('recommended', []))}")
    for r in data.get("recommended", [])[:10]:
        print(f"{r.get('rank')}. {r.get('name')} - {r.get('recent_3m_total')} recent / {r.get('total_encounters')} total")


if __name__ == "__main__":
    main()
