#!/usr/bin/env python3
"""
Job Tracker Toolkit
A daily-running script that monitors company job boards for new postings
matching your criteria. Supports Greenhouse, Ashby, and Workday.

Created for the Job Tracker Toolkit guide.
License: MIT — free to use, modify, and share with attribution.

Usage:
    python job_tracker.py

State is persisted in seen_jobs.json so you only get notified once per posting.
GitHub Actions runs this daily at 7am Mountain by default.
"""

import json
import os
import re
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

# =============================================================================
# CONFIG — edit this block based on your LLM intake output
# =============================================================================

# Companies on Greenhouse. The slug is the part after `greenhouse.io/` in
# their public job board URL (e.g. job-boards.greenhouse.io/mercury -> "mercury")
GREENHOUSE_COMPANIES = [
    # Examples — replace with your own targets
    # "mercury",
    # "plaid",
    # "affirm",
]

# Companies on Ashby. The slug is the part after `ashbyhq.com/` in their public
# job board URL (e.g. jobs.ashbyhq.com/ramp -> "ramp")
ASHBY_COMPANIES = [
    # Examples — replace with your own targets
    # "ramp",
    # "notion",
]

# Companies on Workday. Workday URLs look like:
#   {tenant}.wd1.myworkdayjobs.com/{site}
# (Sometimes wd1 is wd5, wd103, etc. — match what the company's URL shows.)
# Each entry needs both the tenant and site.
WORKDAY_COMPANIES = [
    # Example: {"tenant": "nvidia", "site": "NVIDIAExternalCareerSite", "wd": "wd5"},
    # Example: {"tenant": "salesforce", "site": "External_Career_Site", "wd": "wd1"},
]

# A job title must contain at least one of these (case-insensitive) to match.
KEYWORDS = [
    # Examples — your LLM intake will help you pick these
    # "engineer",
    # "manager",
    # "marketing",
]

# A job title must also contain at least one of these seniority signals.
# Set to [] to disable the seniority filter.
SENIORITY = [
    "senior",
    "lead",
    "principal",
    "staff",
    "manager",
    "director",
    "head",
    "vp",
    "chief",
]

# Location must contain at least one of these substrings (case-insensitive).
# Set to [] to disable the location filter.
LOCATIONS_OK = [
    "remote",
    "united states",
    "us",
]

# Skip any job whose title contains any of these (case-insensitive).
TITLE_EXCLUDE = [
    "intern",
    "internship",
    "contractor",
]

# Email notification settings. Set ENABLE_EMAIL=True after configuring.
ENABLE_EMAIL = False
EMAIL_FROM = "your.email@gmail.com"
EMAIL_TO = "your.email@gmail.com"
GMAIL_PASSWORD_ENV = "GMAIL_APP_PASSWORD"  # name of env var holding the password

# How many days back to treat as "recent" for Workday (which returns large lists)
WORKDAY_LOOKBACK_DAYS = 14

STATE_FILE = "seen_jobs.json"
HTTP_TIMEOUT = 30
USER_AGENT = "JobTrackerToolkit/1.0"

# =============================================================================
# Salary extraction
# =============================================================================

# Matches multiple salary formats:
#   $128,600 - $144,600
#   $128,600-$144,600
#   $128,600 to $144,600
#   $128,600—$144,600 (em-dash)
#   $128,600.00 - $144,600.00 (with decimals)
SALARY_PAIR_RE = re.compile(
    r"\$\s?(\d{2,3},\d{3})(?:\.\d+)?"
    r"\s*(?:-|–|—|to)\s*"
    r"\$?\s?(\d{2,3},\d{3})(?:\.\d+)?",
    re.IGNORECASE,
)


def extract_salary(text):
    """Pull US-dollar salary ranges out of a job description.
    Returns a clean formatted string or '' if none found.
    """
    if not text:
        return ""
    matches = SALARY_PAIR_RE.findall(text)
    if not matches:
        return ""
    seen = []
    for low, high in matches:
        pair = f"${low}-${high}"
        if pair not in seen:
            seen.append(pair)
    return " / ".join(seen[:3])


# =============================================================================
# HTTP helpers
# =============================================================================

def _http_get_json(url, headers=None):
    """GET a URL and parse the response as JSON. Returns None on failure."""
    try:
        req_headers = {"User-Agent": USER_AGENT}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"    HTTP {exc.code} for {url}: {exc.reason}")
    except Exception as exc:
        print(f"    Fetch error for {url}: {exc}")
    return None


def _http_post_json(url, payload, headers=None):
    """POST JSON and parse the response as JSON. Returns None on failure."""
    try:
        req_headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"    HTTP {exc.code} for {url}: {exc.reason}")
    except Exception as exc:
        print(f"    Fetch error for {url}: {exc}")
    return None


# =============================================================================
# Fetchers
# =============================================================================

def fetch_greenhouse(slug):
    """Return a list of normalized jobs from a Greenhouse-hosted company."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _http_get_json(url)
    if not data:
        return []
    jobs = []
    for job in data.get("jobs", []):
        content = job.get("content", "") or ""
        jobs.append({
            "id": f"gh-{slug}-{job.get('id')}",
            "company": slug,
            "title": job.get("title", ""),
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
            "salary": extract_salary(content),
            "source": "greenhouse",
        })
    return jobs


def fetch_ashby(slug):
    """Return a list of normalized jobs from an Ashby-hosted company."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    data = _http_get_json(url)
    if not data:
        return []
    jobs = []
    for job in data.get("jobs", []):
        location = job.get("location", "") or job.get("locationName", "") or ""
        description = job.get("descriptionPlain", "") or job.get("descriptionHtml", "") or ""
        jobs.append({
            "id": f"ash-{slug}-{job.get('id')}",
            "company": slug,
            "title": job.get("title", ""),
            "location": location,
            "url": job.get("jobUrl") or job.get("applyUrl") or "",
            "salary": extract_salary(description),
            "source": "ashby",
        })
    return jobs


def fetch_workday(tenant, site, wd="wd1"):
    """Return a list of normalized jobs from a Workday-hosted company.
    Workday uses a POST endpoint that returns paginated JSON.
    """
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    payload = {
        "appliedFacets": {},
        "limit": 50,
        "offset": 0,
        "searchText": "",
    }
    data = _http_post_json(url, payload)
    if not data:
        return []
    jobs = []
    posts = data.get("jobPostings", [])
    for job in posts:
        external_path = job.get("externalPath", "")
        full_url = f"https://{tenant}.{wd}.myworkdayjobs.com{external_path}" if external_path else ""
        # Workday doesn't return descriptions in the list endpoint, only on
        # individual job pages. We skip per-job description fetching to avoid
        # rate limits — salaries from Workday will usually be blank.
        jobs.append({
            "id": f"wd-{tenant}-{job.get('bulletFields', [''])[0] or external_path}",
            "company": tenant,
            "title": job.get("title", ""),
            "location": job.get("locationsText", "") or "",
            "url": full_url,
            "salary": "",  # Workday doesn't expose salary in the list API
            "source": "workday",
        })
    return jobs


# =============================================================================
# Filters
# =============================================================================

def matches_filters(job):
    """True if the job passes the keyword/seniority/location/exclude filters."""
    title = (job.get("title") or "").lower()
    location = (job.get("location") or "").lower()

    for word in TITLE_EXCLUDE:
        if word.lower() in title:
            return False

    if KEYWORDS and not any(k.lower() in title for k in KEYWORDS):
        return False

    if SENIORITY and not any(s.lower() in title for s in SENIORITY):
        return False

    if LOCATIONS_OK and location.strip():
        if not any(loc.lower() in location for loc in LOCATIONS_OK):
            return False

    return True


# =============================================================================
# State management
# =============================================================================

def load_seen():
    path = Path(STATE_FILE)
    if not path.exists():
        return set()
    try:
        with path.open() as f:
            return set(json.load(f).get("seen_ids", []))
    except Exception:
        return set()


def save_seen(seen_ids):
    with Path(STATE_FILE).open("w") as f:
        json.dump(
            {
                "last_run": datetime.now(timezone.utc).isoformat(),
                "seen_ids": sorted(seen_ids),
            },
            f,
            indent=2,
        )


# =============================================================================
# Output / notification
# =============================================================================

def format_job_line(job):
    salary = job.get("salary", "")
    salary_str = f"    💵 {salary}\n" if salary else ""
    source = job.get("source", "").upper()
    return (
        f"  • [{job['company'].upper()}] {job['title']} ({source})\n"
        f"    📍 {job['location'] or 'Location not listed'}\n"
        f"{salary_str}"
        f"    🔗 {job['url']}\n"
    )


def send_email(subject, body):
    password = os.environ.get(GMAIL_PASSWORD_ENV)
    if not password:
        print(f"[WARN] {GMAIL_PASSWORD_ENV} not set — skipping email.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, password)
            smtp.send_message(msg)
        print(f"[OK] Email sent to {EMAIL_TO}")
        return True
    except Exception as exc:
        print(f"[ERROR] Email send failed: {exc}")
        return False


# =============================================================================
# Main
# =============================================================================

def main():
    print(f"=== Job Tracker — {datetime.now().isoformat(timespec='seconds')} ===\n")

    seen = load_seen()
    all_matches = []
    new_matches = []

    for slug in GREENHOUSE_COMPANIES:
        print(f"[greenhouse] {slug}")
        jobs = fetch_greenhouse(slug)
        print(f"    {len(jobs)} total jobs")
        for job in jobs:
            if matches_filters(job):
                all_matches.append(job)
                if job["id"] not in seen:
                    new_matches.append(job)
                    seen.add(job["id"])

    for slug in ASHBY_COMPANIES:
        print(f"[ashby] {slug}")
        jobs = fetch_ashby(slug)
        print(f"    {len(jobs)} total jobs")
        for job in jobs:
            if matches_filters(job):
                all_matches.append(job)
                if job["id"] not in seen:
                    new_matches.append(job)
                    seen.add(job["id"])

    for wd_company in WORKDAY_COMPANIES:
        tenant = wd_company.get("tenant")
        site = wd_company.get("site")
        wd = wd_company.get("wd", "wd1")
        print(f"[workday] {tenant}/{site}")
        jobs = fetch_workday(tenant, site, wd)
        print(f"    {len(jobs)} total jobs")
        for job in jobs:
            if matches_filters(job):
                all_matches.append(job)
                if job["id"] not in seen:
                    new_matches.append(job)
                    seen.add(job["id"])

    print(f"\n=== Summary ===")
    print(f"  Matching jobs found: {len(all_matches)}")
    print(f"  New since last run:  {len(new_matches)}\n")

    if new_matches:
        print("=== NEW MATCHES ===\n")
        body_parts = [
            f"Found {len(new_matches)} new matching job(s) on "
            f"{datetime.now().strftime('%Y-%m-%d')}:\n"
        ]
        for job in new_matches:
            line = format_job_line(job)
            print(line)
            body_parts.append(line)
        if ENABLE_EMAIL:
            send_email(
                subject=f"[Job Tracker] {len(new_matches)} new match(es)",
                body="\n".join(body_parts),
            )
    elif all_matches:
        print("All current matches were already seen on a prior run:\n")
        for job in all_matches:
            print(format_job_line(job))

    save_seen(seen)
    print(f"State saved: {len(seen)} job IDs tracked in {STATE_FILE}\n")


if __name__ == "__main__":
    main()
