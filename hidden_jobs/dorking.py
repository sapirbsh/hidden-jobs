"""Google dorking via a SERP API.

Note: Google's own Custom Search JSON API is closed to new customers and shuts down on
1 Jan 2027, so we use a third-party provider. Serper.dev returns real Google results
(2,500 free queries on sign-up); Brave Search API is an independent index alternative.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .ats import Job, detect_ats
from .cache import DAY, Cache
from .careers import JOB_URL_HINTS
from .config import JOB_BOARD_DOMAINS
from .matcher import RoleMatcher, RoleQuery
from .util import Budget, BudgetExceeded, Progress, is_ignored_site, is_job_board, registrable_domain

ATS_SITES = ["comeet.com", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co",
             "jobs.ashbyhq.com", "apply.workable.com", "jobs.smartrecruiters.com", "breezy.hr", "bamboohr.com"]
# Google ignores everything after 32 words, so keep exclusions short
EXCLUDE = " ".join(f"-site:{d}" for d in JOB_BOARD_DOMAINS[:10])


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider:
    def __init__(self, provider: str, api_key: str, client: httpx.AsyncClient, cache: Cache,
                 budget: Budget, progress: Progress):
        self.provider, self.api_key, self.client = provider, api_key, client
        self.cache, self.budget, self.progress = cache, budget, progress

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, page: int = 1) -> list[SearchResult]:
        key = f"{self.provider}|{page}|{query}"
        cached = self.cache.get("serp", key, max_age=2 * DAY)
        if cached is not None:
            return [SearchResult(**r) for r in cached]
        self.budget.take()
        self.progress.inc("search_requests")
        if self.provider == "brave":
            r = await self.client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "country": "IL", "count": 20, "offset": page - 1},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"})
            r.raise_for_status()
            items = [SearchResult(x.get("title", ""), x.get("url", ""), x.get("description", ""))
                     for x in r.json().get("web", {}).get("results", [])]
        else:  # serper (Google)
            r = await self.client.post(
                "https://google.serper.dev/search",
                json={"q": query, "gl": "il", "hl": "he", "num": 10, "page": page},
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"})
            r.raise_for_status()
            items = [SearchResult(x.get("title", ""), x.get("link", ""), x.get("snippet", ""))
                     for x in r.json().get("organic", [])]
        self.cache.set("serp", key, [i.__dict__ for i in items])
        return items


def build_dorks(roles: list[RoleQuery], cities: list[tuple[str, str]], terms_per_role: int = 3) -> list[str]:
    """cities = [(hebrew name, english name)]"""
    dorks: list[str] = []
    ats_or = " OR ".join(f"site:{s}" for s in ATS_SITES)
    for rq in roles:
        # the term the user typed + the most distinctive synonyms
        terms = [rq.original] + [t for t in rq.terms if t != rq.original][: terms_per_role - 1]
        for he, en in cities:
            city = f'("{he}" OR "{en}")' if en and en != he else f'"{he}"'
            for t in terms:
                dorks.append(f'({ats_or}) "{t}" {city}')
                dorks.append(f'(inurl:careers OR inurl:jobs OR inurl:career OR intitle:דרושים OR intitle:קריירה) "{t}" {city} {EXCLUDE}')
            dorks.append(f'(דרושים OR דרושה OR דרוש OR "משרות פתוחות") "{rq.original}" "{he}" {EXCLUDE}')
    return list(dict.fromkeys(dorks))


async def run_dorks(provider: SearchProvider, dorks: list[str], matcher: RoleMatcher, pages: int,
                    progress: Progress) -> tuple[list[Job], dict[str, str]]:
    """Return (direct job hits, {domain: url} of employer sites discovered)."""
    jobs: dict[str, Job] = {}
    domains: dict[str, str] = {}
    for q in dorks:
        for page in range(1, pages + 1):
            try:
                results = await provider.search(q, page)
            except BudgetExceeded:
                progress.add_log("⚠ הגעת לתקרת בקשות החיפוש (dorking) — ממשיך עם מה שנמצא")
                return list(jobs.values()), domains
            except httpx.HTTPError as e:
                progress.add_log(f"שגיאת חיפוש: {e}")
                break
            for r in results:
                if not r.url or is_job_board(r.url):
                    continue
                hit = matcher.match(r.title) or matcher.match(r.snippet)
                ats = detect_ats("", r.url)
                if ats:
                    if hit and r.url not in jobs:
                        jobs[r.url] = Job(_clean_title(r.title), r.url, source=f"dork:{ats[0].kind}",
                                          extra={"role": hit[0], "term": hit[1], "company": ats[0].token.split("/")[0]})
                    continue
                if is_ignored_site(r.url):
                    continue
                dom = registrable_domain(r.url)
                domains.setdefault(dom, r.url)
                if hit and JOB_URL_HINTS.search(r.url) and r.url not in jobs:
                    jobs[r.url] = Job(_clean_title(r.title), r.url, source="dork",
                                      extra={"role": hit[0], "term": hit[1], "company": dom})
            if len(results) < 8:
                break
        progress.counters["dork_hits"] = len(jobs)
        progress.counters["dork_domains"] = len(domains)
        progress.tick(current="dork: " + q[:60])
    return list(jobs.values()), domains


def _clean_title(t: str) -> str:
    for sep in (" | ", " - ", " – ", " — "):
        if sep in t and len(t.split(sep)[0]) > 3:
            return t.split(sep)[0].strip()
    return t.strip()
