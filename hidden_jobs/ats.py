"""Applicant-Tracking-System adapters.

Many company career pages are just a wrapper around an ATS (Comeet is huge in Israel,
also Greenhouse, Lever, Ashby, Workable, SmartRecruiters, BambooHR, Breezy, Recruitee).
Those systems expose public JSON feeds, which are far more reliable than scraping HTML,
and give a direct link to every single position.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .fetcher import Fetcher


@dataclass
class Job:
    title: str
    url: str
    location: str = ""
    department: str = ""
    source: str = ""            # "greenhouse", "career-page", "dork"...
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AtsRef:
    kind: str
    token: str
    url: str


# (kind, regex) — group 1 is the board token
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_\-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_\-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_\-\.]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_\-\.%]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_\-]+)", re.I)),
    ("workable", re.compile(r"https?://([A-Za-z0-9_\-]+)\.workable\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_\-]+)", re.I)),
    ("comeet", re.compile(r"comeet\.(?:com|co)/jobs/([A-Za-z0-9_\-\.%]+/[0-9A-F]{2}\.[0-9A-F]{3})", re.I)),
    ("bamboohr", re.compile(r"https?://([A-Za-z0-9_\-]+)\.bamboohr\.com", re.I)),
    ("breezy", re.compile(r"https?://([A-Za-z0-9_\-]+)\.breezy\.hr", re.I)),
    ("recruitee", re.compile(r"https?://([A-Za-z0-9_\-]+)\.recruitee\.com", re.I)),
]
_IGNORE_TOKENS = {"embed", "api", "www", "jobs", "app", "careers", "static", "assets", "v1", "js", "widget"}


def detect_ats(html: str, url: str = "") -> list[AtsRef]:
    found: dict[tuple[str, str], AtsRef] = {}
    haystack = url + "\n" + html
    for kind, rx in _PATTERNS:
        for m in rx.finditer(haystack):
            token = m.group(1).rstrip("/.")
            if token.lower() in _IGNORE_TOKENS:
                continue
            found.setdefault((kind, token.lower()), AtsRef(kind, token, m.group(0)))
    return list(found.values())


async def fetch_ats_jobs(ref: AtsRef, fetcher: Fetcher) -> list[Job]:
    try:
        handler = _HANDLERS[ref.kind]
    except KeyError:
        return []
    try:
        return await handler(ref.token, fetcher)
    except Exception:
        return []


# ---------------- handlers ----------------
async def _greenhouse(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    return [Job(j["title"], j["absolute_url"], (j.get("location") or {}).get("name", ""), source="greenhouse")
            for j in (data or {}).get("jobs", [])]


async def _lever(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if data is None:
        data = await f.get_json(f"https://api.eu.lever.co/v0/postings/{token}?mode=json")
    return [Job(j["text"], j["hostedUrl"], (j.get("categories") or {}).get("location", ""),
                (j.get("categories") or {}).get("team", ""), source="lever")
            for j in (data or []) if isinstance(j, dict)]


async def _ashby(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    return [Job(j["title"], j.get("jobUrl") or j.get("applyUrl", ""), j.get("location", ""),
                j.get("department", ""), source="ashby")
            for j in (data or {}).get("jobs", [])]


async def _workable(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    jobs = []
    for j in (data or {}).get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        url = j.get("url") or j.get("application_url") or f"https://apply.workable.com/{token}/j/{j.get('shortcode', '')}/"
        jobs.append(Job(j.get("title", ""), url, loc, j.get("department", ""), source="workable"))
    return jobs


async def _smartrecruiters(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100")
    jobs = []
    for j in (data or {}).get("content", []):
        loc = (j.get("location") or {}).get("city", "")
        jobs.append(Job(j.get("name", ""), f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}", loc,
                        (j.get("department") or {}).get("label", ""), source="smartrecruiters"))
    return jobs


async def _bamboohr(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://{token}.bamboohr.com/careers/list")
    jobs = []
    for j in (data or {}).get("result", []):
        loc = (j.get("location") or {}).get("city", "") or ""
        jobs.append(Job(j.get("jobOpeningName", ""), f"https://{token}.bamboohr.com/careers/{j.get('id')}",
                        loc, j.get("departmentLabel", "") or "", source="bamboohr"))
    return jobs


async def _breezy(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://{token}.breezy.hr/json")
    return [Job(j.get("name", ""), j.get("url", ""), (j.get("location") or {}).get("name", ""), source="breezy")
            for j in (data or []) if isinstance(j, dict)]


async def _recruitee(token: str, f: Fetcher) -> list[Job]:
    data = await f.get_json(f"https://{token}.recruitee.com/api/offers/")
    return [Job(j.get("title", ""), j.get("careers_url", ""), j.get("location", ""), j.get("department", "") or "",
                source="recruitee")
            for j in (data or {}).get("offers", [])]


_COMEET_JSON = re.compile(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);", re.S)
_COMEET_LINK = re.compile(r'href="(https?://www\.comeet\.(?:com|co)/jobs/[^"]+/[0-9A-F]{2}\.[0-9A-F]{3,}/[^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


async def _comeet(token: str, f: Fetcher) -> list[Job]:
    page = await f.get(f"https://www.comeet.com/jobs/{token}")
    if not page:
        return []
    jobs: list[Job] = []
    m = _COMEET_JSON.search(page.html)
    if m:
        try:
            for j in json.loads(m.group(1)):
                loc = (j.get("location") or {}).get("city", "") or ""
                url = j.get("url_comeet_hosted_page") or j.get("url_active_page") or ""
                jobs.append(Job(j.get("name", ""), url, loc, j.get("department", "") or "", source="comeet"))
        except (ValueError, TypeError):
            jobs = []
    if not jobs:
        for href, text in _COMEET_LINK.findall(page.html):
            title = re.sub(r"<[^>]+>", " ", text).strip()
            if title:
                jobs.append(Job(" ".join(title.split()), href, source="comeet"))
    return jobs


_HANDLERS = {
    "greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workable": _workable,
    "smartrecruiters": _smartrecruiters, "bamboohr": _bamboohr, "breezy": _breezy,
    "recruitee": _recruitee, "comeet": _comeet,
}
