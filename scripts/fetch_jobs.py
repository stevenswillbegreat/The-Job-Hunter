#!/usr/bin/env python3
"""Fetch Cloud / DevOps roles and write them to public/jobs.json.

Two passes:
  1. Sri Lanka  — Senior/Lead DevOps, Cloud Architect, Senior Cloud Engineer.
  2. International — the same roles worldwide, kept only when the posting
     mentions visa sponsorship, relocation assistance, or international remote.

Results are de-duplicated, split into `sri-lanka` / `international` buckets,
and written in the shape that public/index.html expects:

    { "updated": "YYYY-MM-DD", "jobs": [ { ... }, ... ] }

Data source: JSearch (https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch).
Provide the API key via the JSEARCH_API_KEY (or RAPIDAPI_KEY) environment
variable — nothing is hard-coded.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

API_HOST = "jsearch.p.rapidapi.com"
API_URL = f"https://{API_HOST}/search"
API_KEY = os.environ.get("JSEARCH_API_KEY") or os.environ.get("RAPIDAPI_KEY")

# Roles we care about. Used to build query strings.
ROLES = [
    "Senior DevOps Engineer",
    "Lead DevOps Engineer",
    "Cloud Architect",
    "Senior Cloud Engineer",
]

# Keywords that mark an international posting as relocation-friendly.
RELOCATION_KEYWORDS = [
    "visa sponsorship",
    "visa sponsor",
    "sponsor visa",
    "relocation assistance",
    "relocation package",
    "relocation support",
    "will relocate",
    "remote international",
    "work permit",
]

# Tag vocabulary surfaced on the cards / used as quick filters.
TAG_VOCAB = [
    "DevOps", "Cloud", "AWS", "Azure", "GCP", "Kubernetes", "K8s",
    "Terraform", "Ansible", "Docker", "CI/CD", "SRE", "Platform",
    "Python", "Go", "Linux", "Jenkins", "GitOps", "Helm",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "public" / "jobs.json"
PAGES_PER_QUERY = 1            # bump to widen coverage (costs more API calls)
REQUEST_PAUSE_SECONDS = 1.0    # be polite to the rate limiter


# --------------------------------------------------------------------------- #
# API access
# --------------------------------------------------------------------------- #

def search_jobs(query: str, *, num_pages: int = 1, country: str | None = None) -> list[dict]:
    """Call JSearch for a single query string and return raw job dicts."""
    if not API_KEY:
        raise SystemExit(
            "Missing API key. Set JSEARCH_API_KEY (or RAPIDAPI_KEY) in the "
            "environment before running this script."
        )

    headers = {
        "X-RapidAPI-Key": API_KEY,
        "X-RapidAPI-Host": API_HOST,
    }
    params = {
        "query": query,
        "num_pages": str(num_pages),
        "date_posted": "month",
    }
    if country:
        params["country"] = country

    try:
        resp = requests.get(API_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! request failed for {query!r}: {exc}", file=sys.stderr)
        return []

    payload = resp.json()
    return payload.get("data", []) or []


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #

def _text_blob(raw: dict) -> str:
    """Lowercase concatenation of the fields worth scanning for keywords."""
    parts = [
        raw.get("job_title", ""),
        raw.get("job_description", ""),
        raw.get("job_highlights", "") if isinstance(raw.get("job_highlights"), str) else "",
        " ".join(str(v) for v in (raw.get("job_highlights") or {}).values())
        if isinstance(raw.get("job_highlights"), dict) else "",
    ]
    return " ".join(parts).lower()


def extract_tags(raw: dict) -> list[str]:
    """Pick known tech tags out of the title + description."""
    blob = _text_blob(raw)
    found: list[str] = []
    for tag in TAG_VOCAB:
        needle = tag.lower()
        if needle in blob and tag not in found:
            # Normalise a couple of synonyms.
            label = "Kubernetes" if tag == "K8s" else tag
            if label not in found:
                found.append(label)
    return found


def format_location(raw: dict) -> str:
    bits = [raw.get("job_city"), raw.get("job_state"), raw.get("job_country")]
    loc = ", ".join(b for b in bits if b)
    if raw.get("job_is_remote"):
        loc = f"{loc} (Remote)" if loc else "Remote"
    return loc or "Location not specified"


def format_salary(raw: dict) -> str | None:
    lo, hi = raw.get("job_min_salary"), raw.get("job_max_salary")
    cur = raw.get("job_salary_currency") or ""
    period = raw.get("job_salary_period") or ""
    if lo and hi:
        return f"{cur} {int(lo):,}–{int(hi):,} {period}".strip()
    if lo:
        return f"{cur} {int(lo):,}+ {period}".strip()
    return None


def has_relocation_signal(raw: dict) -> bool:
    blob = _text_blob(raw)
    return any(kw in blob for kw in RELOCATION_KEYWORDS)


def normalise(raw: dict, *, category: str, relocation: bool) -> dict:
    """Map a JSearch record to the card shape used by index.html."""
    return {
        "category": category,
        "title": raw.get("job_title", "Untitled role"),
        "company": raw.get("employer_name", "Unknown company"),
        "location": format_location(raw),
        "salary": format_salary(raw),
        "url": raw.get("job_apply_link") or raw.get("job_google_link") or "#",
        "relocation": relocation,
        "tags": extract_tags(raw),
        # internal key, stripped before output — used for de-duplication
        "_id": raw.get("job_id"),
    }


# --------------------------------------------------------------------------- #
# De-duplication
# --------------------------------------------------------------------------- #

def dedupe(jobs: list[dict]) -> list[dict]:
    """Remove duplicates by job id, falling back to (title, company, location)."""
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
    print("Query 1 — Sri Lanka roles")
    collected: list[dict] = []
    for role in ROLES:
        query = f"{role} in Sri Lanka"
        print(f"  · {query}")
        for raw in search_jobs(query, num_pages=PAGES_PER_QUERY, country="lk"):
            collected.append(normalise(raw, category="sri-lanka", relocation=False))
        time.sleep(REQUEST_PAUSE_SECONDS)
    return collected


def fetch_international() -> list[dict]:
    print("Query 2 — International roles (visa / relocation)")
    collected: list[dict] = []
    for role in ROLES:
        # Bias the query toward relocation-friendly postings, then verify
        # against the description before keeping a result.
        query = f"{role} visa sponsorship relocation"
        print(f"  · {query}")
        for raw in search_jobs(query, num_pages=PAGES_PER_QUERY):
            if not has_relocation_signal(raw):
                continue
            collected.append(normalise(raw, category="international", relocation=True))
        time.sleep(REQUEST_PAUSE_SECONDS)
    return collected


def strip_internal(jobs: list[dict]) -> list[dict]:
    for job in jobs:
        job.pop("_id", None)
    return jobs


def main() -> int:
    sri_lanka = dedupe(fetch_sri_lanka())
    international = dedupe(fetch_international())

    # Guard against a Sri Lankan role leaking into the international bucket
    # (and vice versa) when both queries return the same posting.
    sl_ids = {j.get("_id") for j in sri_lanka if j.get("_id")}
    international = [j for j in international if j.get("_id") not in sl_ids]

    combined = strip_internal(sri_lanka + international)

    output = {
        "updated": dt.date.today().isoformat(),
        "jobs": combined,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")

    print(
        f"\nWrote {len(combined)} roles "
        f"({len(sri_lanka)} Sri Lanka, {len(international)} international) "
        f"→ {OUTPUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
