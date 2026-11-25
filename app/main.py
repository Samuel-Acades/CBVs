from __future__ import annotations

import csv
import io
import logging
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from openpyxl.styles import Font

from . import (
    config,
    auth,
    store,
    sheets,
    data as data_mod,
    analytics,
    bundles as bundles_mod,
    recommend,
    normalize,
)
import json
import os
import re
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="ACADES CBV Dashboard", docs_url=None, redoc_url=None)
auth.install(app)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
app.mount("/assets", StaticFiles(directory=config.BASE_DIR / "assets"), name="assets")

templates = Jinja2Templates(directory=config.TEMPLATES_DIR)

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

STATUS_LABELS = {
    "forced": "Forced in by you",
    "eligible": "Recommended",
    "received": "Received last month",
    "no_report": "No encounters",
    "excluded": "Excluded by you",
}


def month_label(m: str) -> str:
    try:
        return f"{MONTH_NAMES[int(m[5:7]) - 1]} {int(m[:4])}"
    except (ValueError, TypeError, IndexError):
        return m or "-"


templates.env.globals["month_label"] = month_label
templates.env.globals["status_labels"] = STATUS_LABELS


# ------------------------------------------------------------------ helpers
def load_ds(request: Request, force: bool = False):
    merges = store.get_merges()
    try:
        return data_mod.load(force=force, merges=merges), None
    except sheets.SheetAccessError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - never crash the UI on a parse hiccup
        log.exception("dataset load failed")
        return None, f"Failed to read the sheet: {e}"


def load_bundles() -> tuple[list[bundles_mod.BundleRecord], list[str]]:
    try:
        records, errors = bundles_mod.scan()
        return bundles_mod.apply_merges(records, store.get_merges()), errors
    except Exception as e:  # noqa: BLE001
        log.warning("bundle scan failed: %s", e)
        return [], [str(e)]


def default_month(ds) -> str:
    if not ds.months:
        return ""
    last_complete = analytics._last_complete_month()
    return last_complete if last_complete in ds.months else ds.months[-1]


def months_choices(ds, selected: str = "") -> list[dict]:
    return [
        {"value": m, "label": month_label(m) + (" (in progress)" if analytics._month_is_partial(m) else ""), "selected": m == selected}
        for m in reversed(ds.months)
    ]


def ctx(request: Request, ds=None, sheet_error: str | None = None, **extra):
    base = {
        "request": request,
        "user": request.session.get(auth.SESSION_KEY),
        "refresh_summary": request.session.pop("refresh_summary", None),
        "sheet_url": config.SHEET_URL,
        "sheet_error": sheet_error,
        "loaded_at": ds.loaded_at if ds else None,
        "age_seconds": round(time.time() - ds.loaded_at) if ds else None,
        "low_threshold": config.LOW_ENCOUNTER_THRESHOLD,
        "problems": ds.problems if ds else [],
        "now": time.strftime("%Y-%m-%d %H:%M"),
        "active_nav": "",
    }
    base.update(extra)
    return base


def error_page(request: Request, err: str):
    return templates.TemplateResponse(request, "error.html", ctx(request, sheet_error=err), status_code=503)


# ------------------------------------------------------------------ auth
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/"):
    if request.session.get(auth.SESSION_KEY) == config.AUTH_USERNAME:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", ctx(request, next=next, error=None))


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if auth.check(username, password):
        request.session[auth.SESSION_KEY] = config.AUTH_USERNAME
        return RedirectResponse(next or "/", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html", ctx(request, next=next, error="Wrong username or password."), status_code=401
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.post("/refresh")
def refresh(request: Request):
    previous, _ = load_ds(request)
    current, error = load_ds(request, force=True)
    if current is None:
        request.session["refresh_summary"] = {"error": error or "Refresh failed."}
    elif previous is None:
        request.session["refresh_summary"] = {"unavailable": True}
    else:
        before = analytics.monthly_series(previous)
        after = analytics.monthly_series(current)
        before_by_month = {row["month"]: row for row in before}
        after_by_month = {row["month"]: row for row in after}
        changes = []
        for month in sorted(before_by_month.keys() | after_by_month.keys()):
            old = before_by_month.get(month, {})
            new = after_by_month.get(month, {})
            encounter_delta = new.get("encounters", 0) - old.get("encounters", 0)
            submission_delta = new.get("submissions", 0) - old.get("submissions", 0)
            if encounter_delta or submission_delta:
                changes.append(
                    {
                        "month": month,
                        "encounters": encounter_delta,
                        "submissions": submission_delta,
                    }
                )
        request.session["refresh_summary"] = {"changes": changes}
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


# ------------------------------------------------------------------ overview
@app.get("/", response_class=HTMLResponse)
def overview(request: Request, month: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)

    records, _ = load_bundles()
    sel = month or default_month(ds)
    kpis = analytics.kpis(ds, records)
    series = kpis["series"]
    proj = kpis["projection"]

    labels = [month_label(s["month"]) + (" (in progress)" if s["partial"] else "") for s in series]
    enc_values = [s["encounters"] for s in series]
    sub_values = [s["submissions"] for s in series]
    line_values: list = list(enc_values)
    rr = kpis["run_rate"]
    if rr and line_values:
        line_values[-1] = rr["forecast"]
    if proj:
        labels.append(month_label(proj["target_month"]) + " (proj.)")
        enc_values.append(None)
        sub_values.append(None)
        line_values.append(proj["forecast"])

    top = analytics.per_cbv(ds, month=sel)[:15]
    rec = recommend.build(ds, records, store.get_overrides(), sel)
    low_rows = [r for r in analytics.per_cbv(ds, month=sel) if r["encounters"] <= config.LOW_ENCOUNTER_THRESHOLD]

    return templates.TemplateResponse(
        request,
        "overview.html",
        ctx(
            request,
            ds,
            kpis=kpis,
            month=sel,
            months=months_choices(ds, sel),
            chart={
                "labels": labels,
                "months": [s["month"] for s in series] + ([""] if proj else []),
                "encounters": enc_values,
                "submissions": sub_values,
                "line": line_values,
                "forecast": proj["forecast"] if proj else None,
                "run_rate": rr,
                "drilldown": True,
            },
            top=top,
            status_counts=rec["counts"],
            low_rows=low_rows[:8],
            follow_up_count=len(analytics.follow_up_rows(ds, records, sel)),
            active_nav="overview",
        ),
    )


@app.get("/month-details", response_class=HTMLResponse)
def month_details(request: Request, month: str, kind: str):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)
    if month not in ds.months or kind not in ("encounters", "submissions"):
        return PlainTextResponse("Choose a valid month and chart series.", status_code=404)

    if kind == "encounters":
        rows = sorted(
            (encounter for encounter in ds.encounters if encounter.month == month),
            key=lambda row: (row.date, row.cbv, row.farmer),
            reverse=True,
        )
        count_label = "Farmer encounters"
    else:
        rows = sorted(
            (submission for submission in ds.unique_submissions if submission.month == month),
            key=lambda row: (row.date, row.cbv),
            reverse=True,
        )
        count_label = "Submitted forms (exact duplicates excluded)"

    return templates.TemplateResponse(
        request,
        "month_details.html",
        ctx(
            request,
            ds,
            month=month,
            kind=kind,
            rows=rows,
            count_label=count_label,
            active_nav="overview",
        ),
    )


@app.get("/follow-up", response_class=HTMLResponse)
def follow_up(request: Request, month: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)
    selected_month = month or default_month(ds)
    if selected_month not in ds.months:
        return PlainTextResponse("invalid month", status_code=400)
    records, _ = load_bundles()
    rows = analytics.follow_up_rows(ds, records, selected_month)
    return templates.TemplateResponse(
        request,
        "follow_up.html",
        ctx(
            request,
            ds,
            month=selected_month,
            months=months_choices(ds, selected_month),
            rows=rows,
            active_nav="follow-up",
        ),
    )


@app.get("/follow-up/export.csv", response_class=PlainTextResponse)
def follow_up_export(request: Request, month: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return PlainTextResponse("sheet unavailable", status_code=503)
    selected_month = month or default_month(ds)
    if selected_month not in ds.months:
        return PlainTextResponse("invalid month", status_code=400)
    records, _ = load_bundles()
    rows = analytics.follow_up_rows(ds, records, selected_month)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["CBV", "Phone", "District", "Encounters", "Forms submitted", "Self-reported reach", "Last bundle", "Items to check"])
    writer.writerows(
        [
            row["cbv"],
            row["phone"],
            row["district"],
            row["encounters"],
            row["submission_count"],
            row["reported_reach"],
            row["last_bundle"],
            "; ".join(row["flags"]),
        ]
        for row in rows
    )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="cbv_follow_up_{selected_month}.csv"'},
    )


# ------------------------------------------------------------------ CBV list
@app.get("/cbvs", response_class=HTMLResponse)
def cbv_list(request: Request, month: str = "", q: str = "", low: int = 0, sort: str = "encounters"):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)

    records, _ = load_bundles()
    from .bundles import matches, received_by_month

    got = received_by_month(records)
    all_time_view = month == "all"
    sel = default_month(ds) if all_time_view else (month or default_month(ds))
    ranking = analytics.per_cbv(ds, month=None if all_time_view else sel)
    all_time = {r["key"]: r for r in analytics.per_cbv(ds)}
    got_any = set().union(*got.values()) if got else set()

    qn = q.strip().lower()
    rows = []
    for r in ranking:
        if qn and qn not in r["cbv"].lower() and qn not in r.get("district", "").lower():
            continue
        if low and r["encounters"] > config.LOW_ENCOUNTER_THRESHOLD:
            continue
        key = r["key"]
        last_bundle = ""
        for m in reversed(sorted(got)):
            if matches(key, got[m]):
                last_bundle = m
                break
        rows.append(
            {
                **r,
                "all_time_encounters": all_time.get(key, {}).get("encounters", r["encounters"]),
                "last_bundle": last_bundle,
                "got_this_month": matches(key, got_any) if all_time_view else matches(key, got.get(sel, set())),
            }
        )

    if sort == "name":
        rows.sort(key=lambda x: x["cbv"])
    elif sort == "total":
        rows.sort(key=lambda x: (-x["all_time_encounters"], x["cbv"]))
    elif sort == "reported":
        rows.sort(key=lambda x: (-x["reported"], x["cbv"]))
    else:
        rows.sort(key=lambda x: (-x["encounters"], -x["submissions"], x["cbv"]))

    low_count = sum(1 for r in ranking if r["encounters"] <= config.LOW_ENCOUNTER_THRESHOLD)

    return templates.TemplateResponse(
        request,
        "cbvs.html",
        ctx(
            request,
            ds,
            rows=rows,
            month="all" if all_time_view else sel,
            months=months_choices(ds, "" if all_time_view else sel),
            q=q,
            low=low,
            sort=sort,
            low_count=low_count,
            total_in_month=len(ranking),
            active_nav="cbvs",
        ),
    )


@app.get("/cbv", response_class=HTMLResponse)
def cbv_detail(request: Request, key: str, month: str = "", saved: int = 0):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)

    records, _ = load_bundles()
    enc = [e for e in ds.encounters if e.cbv_key == key]
    subs = [s for s in ds.submissions if s.cbv_key == key]
    if not enc and not subs:
        return RedirectResponse("/cbvs", status_code=302)

    name = (enc[0].cbv if enc else subs[0].cbv)
    per_month: dict[str, int] = {}
    for e in enc:
        if e.month:
            per_month[e.month] = per_month.get(e.month, 0) + 1
    months_sorted = sorted(per_month)
    chart = {"labels": [month_label(m) for m in months_sorted], "values": [per_month[m] for m in months_sorted]}

    feedback = analytics.feedback_items(ds, kind="all")
    feedback = [f for f in feedback if normalize.name_key(f["cbv"]) == key or f["cbv"] == name]

    my_bundles = sorted(
        [b for b in records if bundles_mod.matches(key, [b.cbv_key])],
        key=lambda b: b.month,
        reverse=True,
    )
    bundles_by_month: dict[str, list] = {}
    for bundle in my_bundles:
        bundles_by_month.setdefault(bundle.month, []).append(bundle)

    activity_months = sorted(
        {e.month for e in enc if e.month} | set(bundles_by_month),
        reverse=True,
    )
    bundle_activity = [
        {
            "month": activity_month,
            "encounters": sum(1 for e in enc if e.month == activity_month),
            "bundles": bundles_by_month.get(activity_month, []),
        }
        for activity_month in activity_months
    ]

    sel = month or default_month(ds)
    rec = recommend.build(ds, records, store.get_overrides(), sel)
    status = next((i for i in rec["items"] if i["key"] == key), None)

    district = next((e.district for e in enc if e.district), "") or next((s.district for s in subs if s.district), "")
    group = next((e.group for e in enc if e.group), "") or next((s.group for s in subs if s.group), "")

    sat = {"yes": 0, "no": 0}
    for e in enc:
        if e.satisfied.strip().lower() in ("eya", "yes"):
            sat["yes"] += 1
        elif e.satisfied.strip().lower() in ("ayi", "no"):
            sat["no"] += 1

    return templates.TemplateResponse(
        request,
        "cbv.html",
        ctx(
            request,
            ds,
            name=name,
            key=key,
            month=sel,
            months=months_choices(ds, sel),
            enc_rows=sorted(enc, key=lambda r: (r.date, r.month), reverse=True)[:200],
            sub_rows=sorted([s for s in subs if not s.is_duplicate], key=lambda r: (r.date, r.month), reverse=True),
            chart=chart,
            total=len(enc),
            total_submissions=len([s for s in subs if not s.is_duplicate]),
            reported_total=sum(s.reached for s in subs if not s.is_duplicate and s.month),
            active_months=len(months_sorted),
            feedback=feedback[:50],
            bundle_list=my_bundles,
            bundle_activity=bundle_activity,
            status=status,
            district=district,
            group=group,
            sat=sat,
            saved=bool(saved),
            active_nav="cbvs",
        ),
    )


# ------------------------------------------------------------------ feedback
@app.get("/feedback", response_class=HTMLResponse)
def feedback_view(request: Request, month: str = "", q: str = "", kind: str = "all"):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)

    items = analytics.feedback_items(ds, month=month or None, q=q, kind=kind)
    return templates.TemplateResponse(
        request,
        "feedback.html",
        ctx(
            request,
            ds,
            items=items[:400],
            total_items=len(items),
            month=month,
            months=months_choices(ds, month),
            q=q,
            kind=kind,
            active_nav="feedback",
        ),
    )


# ------------------------------------------------------------------ name merges
@app.get("/names", response_class=HTMLResponse)
def names_view(request: Request, done: int = 0, removed: int = 0):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)

    merges = store.get_merges()
    # count how much each raw name is used (rows affected)
    usage: dict[str, int] = {}
    for e in ds.encounters:
        usage[e.cbv_key] = usage.get(e.cbv_key, 0) + 1
    for s in ds.submissions:
        usage[s.cbv_key] = usage.get(s.cbv_key, 0) + 1

    unmerged_raw = sorted({n for n in ds.raw_names if normalize.name_key(n) not in merges})
    suspected = normalize.find_duplicates(unmerged_raw)

    for grp in suspected:
        grp["rows"] = sum(usage.get(normalize.name_key(m), 0) for m in grp["members"])
        grp["months"] = sorted(
            {
                e.month
                for e in ds.encounters
                if e.cbv_key in {normalize.name_key(m) for m in grp["members"]} and e.month
            }
            | {
                s.month
                for s in ds.submissions
                if s.cbv_key in {normalize.name_key(m) for m in grp["members"]} and s.month
            }
        )

    merged_aliases = [
        {
            "key": k,
            "alias": normalize.display_name(k),
            "canonical": v,
            "rows": usage.get(k, 0),
        }
        for k, v in sorted(merges.items(), key=lambda kv: kv[1])
    ]

    return templates.TemplateResponse(
        request,
        "names.html",
        ctx(
            request,
            ds,
            suspected=suspected,
            merged_aliases=merged_aliases,
            n_raw=len(ds.raw_names),
            done=done,
            removed=removed,
            active_nav="names",
        ),
    )


@app.post("/names/merge")
def names_merge(request: Request, canonical: str = Form(""), members: list[str] = Form(default=[])):
    canonical = (canonical or "").strip()
    if canonical:
        canonical = normalize.resolve_merge(canonical, store.get_merges())
        for m in members:
            m = (m or "").strip()
            key = normalize.name_key(m)
            if key and m != canonical:
                store.set_merge(key, m, canonical)
    return RedirectResponse("/names?done=1", status_code=303)


@app.post("/names/unmerge")
def names_unmerge(request: Request, alias_key: str = Form(...)):
    store.remove_merge(alias_key)
    return RedirectResponse("/names?removed=1", status_code=303)


# ------------------------------------------------------------------ bundles
@app.get("/bundles", response_class=HTMLResponse)
def bundles_view(request: Request, done: str = ""):
    ds, err = load_ds(request)
    records, errors = load_bundles()
    files = sorted(
        p.name
        for p in config.BUNDLES_DIR.iterdir()
        if p.is_file() and not p.name.startswith("~$") and p.suffix.lower() in {".xlsx", ".xls", ".csv"}
    )
    encounter_counts: dict[str, dict[str, int]] = {}
    if ds:
        for encounter in ds.encounters:
            if encounter.month:
                month_counts = encounter_counts.setdefault(encounter.month, {})
                month_counts[encounter.cbv_key] = month_counts.get(encounter.cbv_key, 0) + 1

    by_month: dict[str, list] = {}
    for r in records:
        month_counts = encounter_counts.get(r.month, {})
        matching_keys = bundles_mod.matched_keys(r.cbv_key, month_counts)
        encounter_count = sum(month_counts[key] for key in matching_keys) if ds else None
        by_month.setdefault(r.month or "(no month detected)", []).append(
            {
                "record": r,
                "encounters": encounter_count,
                "reported": encounter_count > 0 if encounter_count is not None else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "bundles.html",
        ctx(
            request,
            ds,
            sheet_error=err,
            records=records,
            errors=errors,
            by_month={
                k: sorted(v, key=lambda row: row["record"].cbv)
                for k, v in sorted(by_month.items(), reverse=True)
            },
            files=files,
            done=done,
            active_nav="bundles",
        ),
    )


@app.post("/bundles/upload")
async def bundles_upload(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        return RedirectResponse("/bundles?done=empty", status_code=303)
    dest = bundles_mod.save_upload(file.filename or "upload.xlsx", content)
    return RedirectResponse(f"/bundles?done=saved:{dest.name}", status_code=303)


# ------------------------------------------------------------------ recommendations
@app.get("/recommendations", response_class=HTMLResponse)
def recommendations(request: Request, month: str = "", saved: str = ""):
    return _recommendations_view(request, month, saved, full_view=False)


@app.get("/recommendations/full", response_class=HTMLResponse)
def recommendations_full(request: Request, month: str = "", saved: str = ""):
    return _recommendations_view(request, month, saved, full_view=True)


def _recommendations_view(request: Request, month: str, saved: str, full_view: bool):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)

    records, _ = load_bundles()
    sel = month or default_month(ds)
    rec = recommend.build(ds, records, store.get_overrides(), sel)
    visible_recommended = rec["recommended"] if full_view else rec["recommended"][:10]

    return templates.TemplateResponse(
        request,
        "recommendations.html",
        ctx(
            request,
            ds,
            rec=rec,
            month=sel,
            months=months_choices(ds, sel),
            visible_recommended=visible_recommended,
            recommended_names=[
                f'{r["cbv"]} - {r["phone"]}' if r.get("phone") else r["cbv"]
                for r in visible_recommended
            ],
            saved=saved,
            full_view=full_view,
            active_nav="recommendations",
        ),
    )


@app.post("/recommendations/override")
def rec_override(
    request: Request,
    cbv_key: str = Form(...),
    action: str = Form(...),
    note: str = Form(""),
    month: str = Form(""),
    full: int = Form(0),
    return_to: str = Form("recommendations"),
):
    if action in ("include", "exclude"):
        store.set_override(cbv_key, action, note)
    elif action == "clear":
        store.remove_override(cbv_key)

    if return_to == "cbv":
        params = urlencode({"key": cbv_key, "month": month, "saved": 1})
        return RedirectResponse(f"/cbv?{params}", status_code=303)

    page = "/recommendations/full" if full else "/recommendations"
    return RedirectResponse(f"{page}?month={month}&saved=1", status_code=303)


def _recommendation_export_rows(recommended: list[dict]) -> list[list]:
    return [
        [
            i,
            item["cbv"],
            item.get("phone", ""),
            item.get("district", ""),
            item["encounters"],
            item["last_bundle"] or "Never",
            item["last_bundle_encounters"] if item["last_bundle"] else "",
        ]
        for i, item in enumerate(recommended, 1)
    ]


@app.get("/recommendations/export.csv", response_class=PlainTextResponse)
def rec_export(request: Request, month: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return PlainTextResponse("sheet unavailable", status_code=503)
    records, _ = load_bundles()
    sel = month or default_month(ds)
    rec = recommend.build(ds, records, store.get_overrides(), sel)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "CBV", "Phone", "District", "Encounters", "Last bundle", "Encounters when bundled"])
    w.writerows(_recommendation_export_rows(rec["recommended"]))
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="bundle_recommendations_{sel}.csv"'},
    )


@app.get("/recommendations/export.xlsx")
def rec_export_xlsx(request: Request, month: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return Response("sheet unavailable", status_code=503, media_type="text/plain")
    records, _ = load_bundles()
    sel = month or default_month(ds)
    rec = recommend.build(ds, records, store.get_overrides(), sel)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Recommended CBVs"
    sheet.append(["#", "CBV", "Phone", "District", "Encounters", "Last bundle", "Encounters when bundled"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in _recommendation_export_rows(rec["recommended"]):
        sheet.append(row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in {"A": 8, "B": 30, "C": 16, "D": 20, "E": 14, "F": 16, "G": 26}.items():
        sheet.column_dimensions[column].width = width

    output = io.BytesIO()
    workbook.save(output)
    return Response(
        output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="bundle_recommendations_{sel}.xlsx"'},
    )


# ------------------------------------------------------------------ Gemini recommendations
GEMINI_SHORTLIST_JSON = config.DATA_DIR / "gemini_bundle_shortlist.json"
GEMINI_SHORTLIST_MD = config.DATA_DIR / "gemini_bundle_shortlist.md"
GEMINI_SCRIPT = config.BASE_DIR / "scripts" / "gemini_bundle_shortlist.py"


def _load_gemini_shortlist():
    path = GEMINI_SHORTLIST_JSON
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
        return data, mtime
    except Exception as e:  # noqa: BLE001
        log.warning("failed to read gemini shortlist: %s", e)
        return None, None


def _build_cbv_lookup(ds, records):
    phone_for = _phone_by_key(records)
    by_cbv = {}
    for key in {e.cbv_key for e in ds.encounters} | {s.cbv_key for s in ds.submissions}:
        monthly_enc = {m: 0 for m in ds.months}
        monthly_sub = {m: 0 for m in ds.months}
        name = ""
        district = ""
        questions = 0
        for e in ds.encounters:
            if e.cbv_key != key:
                continue
            name = name or e.cbv
            district = district or e.district
            if e.month and e.month in monthly_enc:
                monthly_enc[e.month] += 1
        for s in ds.unique_submissions:
            if s.cbv_key != key:
                continue
            name = name or s.cbv
            district = district or s.district
            if s.month and s.month in monthly_sub:
                monthly_sub[s.month] += 1
            if s.questions:
                questions += 1
        total_enc = sum(monthly_enc.values())
        total_sub = sum(monthly_sub.values())
        active = [m for m in ds.months if monthly_enc.get(m, 0) > 0 or monthly_sub.get(m, 0) > 0]
        recent = ds.months[-3:]
        recent_enc = sum(monthly_enc.get(m, 0) for m in recent)
        recent_sub = sum(monthly_sub.get(m, 0) for m in recent)
        monthly = {
            m: {"encounters": monthly_enc.get(m, 0), "submissions": monthly_sub.get(m, 0)}
            for m in ds.months
        }
        by_cbv[key] = {
            "key": key,
            "name": name or key,
            "district": district,
            "phone": phone_for(key),
            "monthly": monthly,
            "total_encounters": total_enc,
            "total_submissions": total_sub,
            "total": total_enc,
            "months_active": len(active),
            "recent_3m_encounters": recent_enc,
            "recent_3m_submissions": recent_sub,
            "recent_3m_total": recent_enc,
            "last_active": active[-1] if active else "",
            "questions": questions,
        }
    return by_cbv


def _gemini_decision(row, shortlist, ref_month):
    if not row:
        return None
    recommended = shortlist.get("recommended", []) if shortlist else []
    watchlist = shortlist.get("watchlist", []) if shortlist else []
    key = row["key"]
    rec_item = next((r for r in recommended if normalize.name_key(r.get("name", "")) == key), None)
    watch_item = next((r for r in watchlist if normalize.name_key(r.get("name", "")) == key), None)

    recent_enc = row.get("recent_3m_encounters", row.get("recent_3m_total", 0))
    recent_sub = row.get("recent_3m_submissions", 0)
    total_enc = row.get("total_encounters", row.get("total", 0))
    total_sub = row.get("total_submissions", 0)
    months_active = row["months_active"]
    last_active = row["last_active"]

    if rec_item:
        verdict = "Recommended"
        badge = "green"
        reason = rec_item.get("reason", "Included in Gemini shortlist.")
    elif watch_item:
        verdict = "Watchlist"
        badge = "amber"
        reason = watch_item.get("reason", "On Gemini watchlist.")
    elif recent_enc >= 10 and months_active >= 2:
        verdict = "Should consider"
        badge = "blue"
        reason = f"Strong recent encounter activity ({recent_enc} encounters, {recent_sub} submissions in last 3 months across {months_active} active months) but not in current Gemini shortlist."
    elif recent_enc > 0 or recent_sub > 0:
        verdict = "Not enough recent activity"
        badge = "amber"
        reason = f"Recent activity exists ({recent_enc} encounters, {recent_sub} submissions in last 3 months), but below the usual recommendation strength."
    elif total_enc > 0 or total_sub > 0:
        verdict = "Inactive recently"
        badge = "red"
        reason = f"No recent activity in last 3 months; last active {month_label(last_active) if last_active else 'unknown'}."
    else:
        verdict = "No data found"
        badge = "gray"
        reason = "No encounters/submissions found for this CBV."

    return {
        "verdict": verdict,
        "badge": badge,
        "reason": reason,
        "in_shortlist": bool(rec_item),
        "in_watchlist": bool(watch_item),
        "ref_month": ref_month,
    }


@app.get("/gemini-recommendations", response_class=HTMLResponse)
def gemini_recommendations(request: Request, generated: str = "", q: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return error_page(request, err)

    shortlist, generated_at = _load_gemini_shortlist()
    cbv_rows = {r["key"]: r for r in analytics.per_cbv(ds)}
    records, bundle_errors = load_bundles()
    ref_month = default_month(ds)

    proof_rows = []
    watchlist_rows = []
    excluded_rows = []
    phone_for = _phone_by_key(records)
    if shortlist:
        for item in shortlist.get("recommended", []):
            key = normalize.name_key(item.get("name", ""))
            cbv = cbv_rows.get(key, {})
            proof_rows.append({
                **item,
                "key": key,
                "exists": bool(cbv),
                "phone": phone_for(key),
                "current_encounters": cbv.get("encounters", 0),
                "current_all_time": cbv.get("all_time", item.get("total_encounters", 0)),
                "district": item.get("district") or cbv.get("district", ""),
            })
        for item in shortlist.get("watchlist", []):
            key = normalize.name_key(item.get("name", ""))
            cbv = cbv_rows.get(key, {})
            watchlist_rows.append({**item, "key": key, "exists": bool(cbv), "current_encounters": cbv.get("encounters", 0)})
        for item in shortlist.get("excluded_notable", []):
            key = normalize.name_key(item.get("name", ""))
            cbv = cbv_rows.get(key, {})
            excluded_rows.append({**item, "key": key, "exists": bool(cbv), "current_encounters": cbv.get("encounters", 0)})

    lookup = _build_cbv_lookup(ds, records)
    query = (q or "").strip()
    query_norm = re.sub(r"[^0-9]", "", query)
    matches = []
    if query:
        for row in lookup.values():
            name_hit = query.lower() in row["name"].lower()
            phone = re.sub(r"[^0-9]", "", row.get("phone", ""))
            phone_hit = bool(query_norm) and (query_norm in phone or phone in query_norm and len(query_norm) >= 5)
            if name_hit or phone_hit:
                decision = _gemini_decision(row, shortlist, ref_month)
                matches.append({**row, "decision": decision})
        matches.sort(key=lambda r: (-r["recent_3m_total"], -r["total"], r["name"]))
        matches = matches[:20]

    generated_label = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated_at)) if generated_at else ""

    return templates.TemplateResponse(
        request,
        "gemini_recommendations.html",
        ctx(
            request,
            ds,
            shortlist=shortlist,
            proof_rows=proof_rows,
            watchlist_rows=watchlist_rows,
            excluded_rows=excluded_rows,
            generated=generated,
            generated_label=generated_label,
            ref_month=ref_month,
            bundle_errors=bundle_errors,
            has_api_key=bool(os.environ.get("GEMINI_API_KEY")),
            q=query,
            matches=matches,
            active_nav="gemini-recommendations",
        ),
    )


@app.post("/gemini-recommendations/generate")
def gemini_recommendations_generate(request: Request):
    if not os.environ.get("GEMINI_API_KEY"):
        return RedirectResponse("/gemini-recommendations?generated=no_key", status_code=303)
    if not GEMINI_SCRIPT.exists():
        return RedirectResponse("/gemini-recommendations?generated=missing_script", status_code=303)
    try:
        result = subprocess.run(
            [sys.executable, str(GEMINI_SCRIPT)],
            cwd=str(config.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=180,
            env=os.environ.copy(),
        )
        if result.returncode != 0:
            log.warning("gemini shortlist generation failed: %s", result.stderr[-500:] if result.stderr else result.stdout[-500:])
            return RedirectResponse("/gemini-recommendations?generated=error", status_code=303)
    except Exception as e:  # noqa: BLE001
        log.warning("gemini shortlist generation exception: %s", e)
        return RedirectResponse("/gemini-recommendations?generated=error", status_code=303)
    return RedirectResponse("/gemini-recommendations?generated=1", status_code=303)


def _phone_by_key(records):
    line_records = [r for r in records if r.extra.get("line")]
    line_keys = {r.cbv_key for r in line_records}
    def lookup(key):
        matching = bundles_mod.matched_keys(key, line_keys)
        rec = max((r for r in line_records if r.cbv_key in matching), key=lambda r: (r.month, r.cbv_key == key), default=None)
        return rec.extra["line"] if rec else ""
    return lookup


@app.get("/gemini-recommendations/export.csv", response_class=PlainTextResponse)
def gemini_recommendations_export_csv(request: Request):
    shortlist, _ = _load_gemini_shortlist()
    if not shortlist:
        return PlainTextResponse("No Gemini shortlist found", status_code=404)
    records, _ = load_bundles()
    phone_for = _phone_by_key(records)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Rank", "CBV", "Phone number", "District", "Recent 3m", "Total encounters", "Last bundle", "Reason", "Signals"])
    for r in shortlist.get("recommended", []):
        key = normalize.name_key(r.get("name", ""))
        signals = "; ".join(r.get("signals", []))
        w.writerow([
            r.get("rank", ""),
            r.get("name", ""),
            phone_for(key),
            r.get("district", ""),
            r.get("recent_3m_total", ""),
            r.get("total_encounters", ""),
            r.get("last_bundle_month") or "Never",
            r.get("reason", ""),
            signals,
        ])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gemini_bundle_shortlist.csv"},
    )


# ------------------------------------------------------------------ JSON API
@app.get("/api/overview")
def api_overview(request: Request, month: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return JSONResponse({"error": err}, status_code=503)
    records, _ = load_bundles()
    sel = month or default_month(ds)
    kpis = analytics.kpis(ds, records)
    rec = recommend.build(ds, records, store.get_overrides(), sel)
    series = kpis["series"]
    return {
        "series": series,
        "projection": kpis["projection"],
        "top": analytics.per_cbv(ds, month=sel)[:15],
        "kpis": {k: v for k, v in kpis.items() if k != "series"},
        "status_counts": rec["counts"],
    }


@app.get("/api/cbvs")
def api_cbvs(request: Request, month: str = ""):
    ds, err = load_ds(request)
    if ds is None:
        return JSONResponse({"error": err}, status_code=503)
    sel = month or default_month(ds)
    return {"month": sel, "cbvs": analytics.per_cbv(ds, month=sel)}
