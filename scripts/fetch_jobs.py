#!/usr/bin/env python3
"""Fetch Cloud / DevOps / Platform roles with python-jobspy → jobs.json.

Two passes:
  1. Sri Lanka  — DevOps / Platform / Cloud Engineer on LinkedIn + Google Jobs.
  2. International — the same roles searched globally, kept only when the
     posting mentions visa sponsorship.

Results are de-duplicated, split into `sri-lanka` / `international` buckets,
and written in the shape public/../index.html expects:

    { "updated": "YYYY-MM-DD", "jobs": [ { ... }, ... ] }

No API key required — jobspy scrapes the job boards directly. Because LinkedIn
and Google aggressively rate-limit datacenter IPs (e.g. GitHub Actions
runners), every scrape is wrapped in exception handling: a throttled or failed
source is logged and skipped rather than crashing the run, and if *every*
source fails we keep the previous jobs.json instead of clobbering the live
site with an empty list.

Optional: set JOBSPY_PROXIES (comma-separated host:port or user:pass@host:port)
to route requests through proxies and dodge throttling on shared CI IPs.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import pandas as pd

try:
    from jobspy import scrape_jobs
except ImportError:  # pragma: no cover - clearer message than a raw traceback
    raise SystemExit(
        "python-jobspy is not installed. Run: pip install -r requirements.txt"
    )

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ROLES = ["DevOps Engineer", "Platform Engineer", "Cloud Engineer"]

# jobspy's LinkedIn scraper validates `location` against a fixed country enum
# that does NOT include Sri Lanka, and a LinkedIn error aborts the whole
# combined call. So the Sri Lanka pass uses Google Jobs only (Google keys off
# google_search_term, not the country enum); the global pass uses both.
SITES_SRI_LANKA = ["google"]
SITES_INTERNATIONAL = ["linkedin", "google"]

# Keyword that qualifies an international posting as relocation-friendly.
VISA_KEYWORDS = [
    "visa sponsorship",
    "visa sponsor",
    "sponsor visa",
    "sponsorship available",
    "will sponsor",
    "relocation assistance",
    "relocation package",
    "work permit",
]

# Tag vocabulary surfaced on the cards / used as quick filters.
TAG_VOCAB = [
    "DevOps", "Cloud", "AWS", "Azure", "GCP", "Kubernetes", "K8s",
    "Terraform", "Ansible", "Docker", "CI/CD", "SRE", "Platform",
    "Python", "Go", "Linux", "Jenkins", "GitOps", "Helm",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "jobs.json"
RESULTS_PER_SEARCH = 25         # per role, per site
HOURS_OLD = 720                 # last 30 days
PROXIES = [
    p.strip() for p in os.environ.get("JOBSPY_PROXIES", "").split(",") if p.strip()
] or None


# --------------------------------------------------------------------------- #
# Scraping (with graceful failure handling)
# --------------------------------------------------------------------------- #

def safe_scrape(*, sites: list[str], search_term: str, google_search_term: str, location: str | None) -> pd.DataFrame:
    """Scrape one query across `sites`, swallowing throttling / network errors.

    Returns a (possibly empty) DataFrame. A failure on one source never aborts
    the run — it's logged and that source contributes nothing.
    """
    try:
        df = scrape_jobs(
            site_name=sites,
            search_term=search_term,
            google_search_term=google_search_term,
            location=location,
            results_wanted=RESULTS_PER_SEARCH,
            hours_old=HOURS_OLD,
            linkedin_fetch_description=True,  # needed to scan for visa wording
            proxies=PROXIES,
            verbose=0,
        )
        if df is None or df.empty:
            print(f"  · no results for {search_term!r} ({location or 'global'})")
            return pd.DataFrame()
        print(f"  · {len(df):>3} rows for {search_term!r} ({location or 'global'})")
        return df
    except Exception as exc:  # noqa: BLE001 - any scrape error must not crash CI
        # LinkedIn/Google throttling typically surfaces as HTTP 429 or a
        # connection/parse error; treat all of them as "this source is
        # unavailable right now" and move on.
        print(
            f"  ! scrape failed for {search_term!r} ({location or 'global'}): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return pd.DataFrame()


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #

def _val(row: pd.Series, key: str, default: str = "") -> str:
    v = row.get(key, default)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    return str(v)


def extract_tags(text: str) -> list[str]:
    blob = text.lower()
    found: list[str] = []
    for tag in TAG_VOCAB:
        if tag.lower() in blob:
            label = "Kubernetes" if tag == "K8s" else tag
            if label not in found:
                found.append(label)
    return found


def format_salary(row: pd.Series) -> str | None:
    lo, hi = row.get("min_amount"), row.get("max_amount")
    cur = _val(row, "currency")
    interval = _val(row, "interval")
    has_lo = lo is not None and not pd.isna(lo)
    has_hi = hi is not None and not pd.isna(hi)
    if has_lo and has_hi:
        return f"{cur} {int(lo):,}–{int(hi):,} {interval}".strip()
    if has_lo:
        return f"{cur} {int(lo):,}+ {interval}".strip()
    return None


def normalise(row: pd.Series, *, category: str, relocation: bool) -> dict:
    title = _val(row, "title", "Untitled role")
    desc = _val(row, "description")
    location = _val(row, "location") or ("Remote" if row.get("is_remote") else "Location not specified")
    return {
        "category": category,
        "title": title,
        "company": _val(row, "company", "Unknown company"),
        "location": location,
        "salary": format_salary(row),
        "url": _val(row, "job_url_direct") or _val(row, "job_url") or "#",
        "relocation": relocation,
        "tags": extract_tags(f"{title} {desc}"),
        "_id": _val(row, "id") or _val(row, "job_url"),
    }


def has_visa_signal(row: pd.Series) -> bool:
    blob = f"{_val(row, 'title')} {_val(row, 'description')}".lower()
    return any(kw in blob for kw in VISA_KEYWORDS)


# --------------------------------------------------------------------------- #
# De-duplication
# --------------------------------------------------------------------------- #

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
# Query orchestration
# --------------------------------------------------------------------------- #

def fetch_sri_lanka() -> list[dict]:
    print("Pass 1 — Sri Lanka (Google Jobs)")
    collected: list[dict] = []
    for role in ROLES:
        df = safe_scrape(
            sites=SITES_SRI_LANKA,
            search_term=role,
            google_search_term=f"{role} jobs in Sri Lanka",
            location="Sri Lanka",
        )
        for _, row in df.iterrows():
            collected.append(normalise(row, category="sri-lanka", relocation=False))
    return collected


def fetch_international() -> list[dict]:
    print("Pass 2 — International (visa sponsorship)")
    collected: list[dict] = []
    for role in ROLES:
        df = safe_scrape(
            sites=SITES_INTERNATIONAL,
            search_term=f"{role} visa sponsorship",
            google_search_term=f"{role} jobs with visa sponsorship",
            location=None,  # global
        )
        for _, row in df.iterrows():
            if not has_visa_signal(row):
                continue
            collected.append(normalise(row, category="international", relocation=True))
    return collected


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
        # Every source was throttled / empty. Don't overwrite a good jobs.json
        # with an empty list — leave the live site intact and signal failure.
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
