#!/usr/bin/env python3
"""Fetch Cloud / DevOps / Platform roles → jobs.json (hybrid sources).

Two passes, each using the source that actually works for it:

  1. Sri Lanka  — JSearch API (RapidAPI). jobspy's LinkedIn scraper rejects
     Sri Lanka (not in its country enum) and Google Jobs returns nothing from
     CI runner IPs, so JSearch is the reliable source for local roles.
  2. International — python-jobspy scraping LinkedIn, kept only when the
     posting mentions visa sponsorship. LinkedIn gives strong global coverage.

Results are de-duplicated, split into `sri-lanka` / `international` buckets,
and written in the shape ../index.html expects:

    { "updated": "YYYY-MM-DD", "jobs": [ { ... }, ... ] }

Configuration (all via environment, nothing hard-coded):
  JSEARCH_API_KEY / RAPIDAPI_KEY   RapidAPI key for the Sri Lanka pass.
  JOBSPY_PROXIES                   Optional comma-separated host:port proxies
                                   to dodge LinkedIn throttling on CI IPs.

Every network call is wrapped in exception handling: a throttled or failed
source is logged and skipped rather than crashing the run. If *every* source
yields nothing we keep the previous jobs.json instead of blanking the site.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

try:
    from jobspy import scrape_jobs
except ImportError:  # pragma: no cover
    raise SystemExit("python-jobspy is not installed. Run: pip install -r requirements.txt")

# jobspy pulls in pandas; import lazily-safe for type checks below.
import pandas as pd

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ROLES = ["DevOps Engineer", "Platform Engineer", "Cloud Engineer"]

# --- Sri Lanka pass (JSearch) ---
JSEARCH_HOST = "jsearch.p.rapidapi.com"
JSEARCH_URL = f"https://{JSEARCH_HOST}/search"
JSEARCH_KEY = os.environ.get("JSEARCH_API_KEY") or os.environ.get("RAPIDAPI_KEY")
JSEARCH_PAGES = 1
JSEARCH_PAUSE = 1.0  # seconds between calls, polite to the rate limiter

# --- International pass (jobspy) ---
INTL_SITES = ["linkedin"]  # Google Jobs returns nothing from CI IPs
RESULTS_PER_SEARCH = 25
HOURS_OLD = 720  # last 30 days
PROXIES = [p.strip() for p in os.environ.get("JOBSPY_PROXIES", "").split(",") if p.strip()] or None

# Locations to scrape for the international pass. Without a location, jobspy's
# LinkedIn scraper defaults to US-centric results, so we target specific hubs
# instead. US roles are explicitly excluded (see is_us_location); the targeted
# set covers EU + UK plus the Gulf and South-East Asia visa-sponsor hubs.
# Override via INTL_LOCATIONS (comma-separated) if you want a different set.
DEFAULT_INTL_LOCATIONS = [
    "Germany",
    "Netherlands",
    "Ireland",
    "Sweden",
    "United Kingdom",
    "Switzerland",
    "Denmark",
    "Poland",
    "Spain",
    "United Arab Emirates",
    "Qatar",
    "Malaysia",
    "Singapore",
]
INTL_LOCATIONS = [
    loc.strip()
    for loc in os.environ.get("INTL_LOCATIONS", ",".join(DEFAULT_INTL_LOCATIONS)).split(",")
] or DEFAULT_INTL_LOCATIONS

VISA_KEYWORDS = [
    "visa sponsorship", "visa sponsor", "sponsor visa", "sponsorship available",
    "will sponsor", "relocation assistance", "relocation package", "work permit",
]

TAG_VOCAB = [
    "DevOps", "Cloud", "AWS", "Azure", "GCP", "Kubernetes", "K8s",
    "Terraform", "Ansible", "Docker", "CI/CD", "SRE", "Platform",
    "Python", "Go", "Linux", "Jenkins", "GitOps", "Helm",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "jobs.json"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def extract_tags(text: str) -> list[str]:
    blob = text.lower()
    found: list[str] = []
    for tag in TAG_VOCAB:
        if tag.lower() in blob:
            label = "Kubernetes" if tag == "K8s" else tag
            if label not in found:
                found.append(label)
    return found


def dedupe(jobs: list[dict]) -> list[dict]:
    seen: set = set()
    unique: list[dict] = []
    for job in jobs:
        key = job.get("_id") or (
            job["title"].lower().strip(),
            job["company"].lower().strip(),
            job["location"].lower().strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


# --------------------------------------------------------------------------- #
# Sri Lanka pass — JSearch API
# --------------------------------------------------------------------------- #

def jsearch_query(query: str) -> list[dict]:
    headers = {"X-RapidAPI-Key": JSEARCH_KEY, "X-RapidAPI-Host": JSEARCH_HOST}
    params = {"query": query, "num_pages": str(JSEARCH_PAGES), "country": "lk", "date_posted": "month"}
    try:
        resp = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("data", []) or []
    except requests.RequestException as exc:
        print(f"  ! JSearch request failed for {query!r}: {exc}", file=sys.stderr)
        return []


def normalise_jsearch(raw: dict) -> dict:
    bits = [raw.get("job_city"), raw.get("job_state"), raw.get("job_country")]
    location = ", ".join(b for b in bits if b) or "Sri Lanka"
    lo, hi, cur = raw.get("job_min_salary"), raw.get("job_max_salary"), raw.get("job_salary_currency") or ""
    if lo and hi:
        salary = f"{cur} {int(lo):,}–{int(hi):,}".strip()
    elif lo:
        salary = f"{cur} {int(lo):,}+".strip()
    else:
        salary = None
    title = raw.get("job_title", "Untitled role")
    desc = raw.get("job_description", "")
    return {
        "category": "sri-lanka",
        "title": title,
        "company": raw.get("employer_name", "Unknown company"),
        "location": location,
        "salary": salary,
        "url": raw.get("job_apply_link") or raw.get("job_google_link") or "#",
        "relocation": False,
        "tags": extract_tags(f"{title} {desc}"),
        "_id": raw.get("job_id"),
    }


def fetch_sri_lanka() -> list[dict]:
    print("Pass 1 — Sri Lanka (JSearch)")
    if not JSEARCH_KEY:
        print("  ! JSEARCH_API_KEY not set — skipping Sri Lanka pass.", file=sys.stderr)
        return []
    collected: list[dict] = []
    for role in ROLES:
        query = f"{role} in Sri Lanka"
        rows = jsearch_query(query)
        print(f"  · {len(rows):>3} rows for {query!r}")
        collected.extend(normalise_jsearch(r) for r in rows)
        time.sleep(JSEARCH_PAUSE)
    return collected


# --------------------------------------------------------------------------- #
# International pass — jobspy / LinkedIn
# --------------------------------------------------------------------------- #

def safe_scrape(*, search_term: str, google_search_term: str, location: str = "") -> pd.DataFrame:
    where = location or "global"
    try:
        df = scrape_jobs(
            site_name=INTL_SITES,
            search_term=search_term,
            google_search_term=google_search_term,
            location=location or None,
            results_wanted=RESULTS_PER_SEARCH,
            hours_old=HOURS_OLD,
            linkedin_fetch_description=True,
            proxies=PROXIES,
            verbose=0,
        )
        if df is None or df.empty:
            print(f"  · no results for {search_term!r} ({where})")
            return pd.DataFrame()
        print(f"  · {len(df):>3} rows for {search_term!r} ({where})")
        return df
    except Exception as exc:  # noqa: BLE001 - throttling must not crash CI
        print(f"  ! scrape failed for {search_term!r} ({where}): {type(exc).__name__}: {exc}", file=sys.stderr)
        return pd.DataFrame()


def _val(row: pd.Series, key: str, default: str = "") -> str:
    v = row.get(key, default)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    return str(v)


def has_visa_signal(row: pd.Series) -> bool:
    blob = f"{_val(row, 'title')} {_val(row, 'description')}".lower()
    return any(kw in blob for kw in VISA_KEYWORDS)


# US state codes used to detect "City, ST" style American locations.
_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}


def is_us_location(location: str) -> bool:
    """True if the location string looks American — so we can drop it."""
    blob = location.lower()
    if "united states" in blob or "usa" in blob or "u.s." in blob:
        return True
    # Trailing ", ST" (e.g. "Austin, TX") — a state code as the last segment.
    tail = blob.replace(".", "").split(",")[-1].strip()
    return tail in _US_STATES


def normalise_jobspy(row: pd.Series) -> dict:
    title = _val(row, "title", "Untitled role")
    desc = _val(row, "description")
    location = _val(row, "location") or ("Remote" if row.get("is_remote") else "Location not specified")
    lo, hi, cur, interval = row.get("min_amount"), row.get("max_amount"), _val(row, "currency"), _val(row, "interval")
    if lo is not None and not pd.isna(lo) and hi is not None and not pd.isna(hi):
        salary = f"{cur} {int(lo):,}–{int(hi):,} {interval}".strip()
    elif lo is not None and not pd.isna(lo):
        salary = f"{cur} {int(lo):,}+ {interval}".strip()
    else:
        salary = None
    return {
        "category": "international",
        "title": title,
        "company": _val(row, "company", "Unknown company"),
        "location": location,
        "salary": salary,
        "url": _val(row, "job_url_direct") or _val(row, "job_url") or "#",
        "relocation": True,
        "tags": extract_tags(f"{title} {desc}"),
        "_id": _val(row, "id") or _val(row, "job_url"),
    }


def fetch_international() -> list[dict]:
    print("Pass 2 — International (LinkedIn, visa sponsorship)")
    print(f"  locations: {', '.join(loc or 'global' for loc in INTL_LOCATIONS)}")
    collected: list[dict] = []
    for location in INTL_LOCATIONS:
        for role in ROLES:
            df = safe_scrape(
                search_term=f"{role} visa sponsorship",
                google_search_term=f"{role} jobs with visa sponsorship",
                location=location,
            )
            for _, row in df.iterrows():
                if not has_visa_signal(row):
                    continue
                job = normalise_jobspy(row)
                if is_us_location(job["location"]):
                    continue  # exclude American roles
                collected.append(job)
    return collected


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def strip_internal(jobs: list[dict]) -> list[dict]:
    for job in jobs:
        job.pop("_id", None)
    return jobs


def write_output(jobs: list[dict]) -> None:
    output = {"updated": dt.date.today().isoformat(), "jobs": jobs}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    sri_lanka = dedupe(fetch_sri_lanka())
    international = dedupe(fetch_international())

    # Keep a posting in only one bucket if it surfaced in both passes.
    sl_ids = {j.get("_id") for j in sri_lanka if j.get("_id")}
    international = [j for j in international if j.get("_id") not in sl_ids]

    combined = strip_internal(sri_lanka + international)

    if not combined:
        print(
            "\nNo jobs scraped (all sources throttled or empty). "
            "Leaving existing jobs.json unchanged.",
            file=sys.stderr,
        )
        return 1

    write_output(combined)
    print(
        f"\nWrote {len(combined)} roles "
        f"({len(sri_lanka)} Sri Lanka, {len(international)} international) "
        f"→ {OUTPUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
