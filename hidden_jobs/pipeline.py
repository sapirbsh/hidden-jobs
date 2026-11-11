"""Orchestrates the whole search: Maps → careers pages → matching roles (+ dorking in parallel)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field

import httpx

from .ats import Job
from .cache import Cache
from .careers import CareerInfo, extract_jobs, find_careers
from .config import Settings
from .dorking import SearchProvider, build_dorks, run_dorks
from .fetcher import Fetcher
from .matcher import RoleMatcher, expand_roles
from .places import City, Place, PlacesClient, collect_places
from .role_profiles import targeting_for_roles
from .util import Budget, Progress, homepage_of

ISRAEL_WORDS = ["israel", "ישראל", "remote", "hybrid", "היברידי", "מרחוק"]


@dataclass
class SearchRequest:
    cities: list[str]
    roles: list[str]
    level: str = "normal"                 # quick | normal | deep
    use_maps: bool = True
    use_dorking: bool = True
    use_synonyms: bool = True
    extra_categories: list[str] = field(default_factory=list)
    only_extra_categories: bool = False


def _location_ok(location: str, city_names: list[str]) -> bool:
    """True when the job location is empty, one of the searched cities, Israel or remote."""
    if not location:
        return True
    low = location.lower()
    if any(c.lower() in low for c in city_names):
        return True
    return any(w in low for w in ISRAEL_WORDS) or bool(re.search(r"\b(il|isr)\b", low))


async def run_search(req: SearchRequest, settings: Settings, progress: Progress,
                     transport: httpx.AsyncBaseTransport | None = None) -> dict:
    t0 = time.time()
    cache = Cache(settings.cache_path)
    roles = expand_roles(req.roles, req.use_synonyms)
    matcher = RoleMatcher(roles)
    places_budget = Budget(settings.max_places_requests, "Google Places")
    search_budget = Budget(settings.max_search_requests, "Search API")

    client_kw = dict(follow_redirects=True, headers={"User-Agent": settings.user_agent,
                                                     "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"},
                     limits=httpx.Limits(max_connections=settings.concurrency * 2))
    if transport is not None:
        client_kw["transport"] = transport
    async with httpx.AsyncClient(**client_kw) as client:
        fetcher = Fetcher(settings, client)
        places_client = PlacesClient(settings.google_maps_api_key, client, cache, places_budget, progress)
        search_key = settings.serper_api_key if settings.search_provider == "serper" else settings.brave_api_key
        provider = SearchProvider(settings.search_provider, search_key, client, cache, search_budget, progress)

        use_maps = req.use_maps and bool(settings.google_maps_api_key)
        use_dork = req.use_dorking and provider.enabled
        if req.use_maps and not use_maps:
            progress.add_log("⚠ אין GOOGLE_MAPS_API_KEY — מדלג על שלב גוגל מפות")
        if req.use_dorking and not use_dork:
            progress.add_log("⚠ אין מפתח ל-Search API — מדלג על Google Dorking")
        if not use_maps and not use_dork:
            raise RuntimeError("צריך לפחות מפתח אחד: GOOGLE_MAPS_API_KEY או SERPER_API_KEY/BRAVE_API_KEY")

        # ---------------- stage 1: places (+ dorking in parallel) ----------------
        progress.set_stage("places", "שלב 1 — איסוף כל המקומות בעיר/ערים")
        cities: list[City] = []
        if use_maps:
            for c in req.cities:
                try:
                    city = await places_client.resolve_city(c)
                    cities.append(city)
                    progress.add_log(f"עיר זוהתה: {city.name_he} / {city.name_en}")
                except Exception as e:  # noqa: BLE001
                    progress.add_log(f"⚠ {e}")
        city_pairs = [(c.name_he, c.name_en) for c in cities] or [(c, "") for c in req.cities]
        city_names = [n for pair in city_pairs for n in pair if n]

        targeting = targeting_for_roles(roles, req.extra_categories, req.only_extra_categories)
        categories = targeting.categories
        if req.level == "quick":
            categories = categories[:8] + [c for c in req.extra_categories if c not in categories[:8]]
        places_client.office_only = targeting.office_only
        progress.add_log(f"פרופיל תפקיד: {', '.join(targeting.profiles) or 'כללי'} — "
                         f"{len(categories)} קטגוריות רלוונטיות"
                         + (" (מסנן חנויות/מסעדות/בילוי)" if targeting.office_only else ""))

        dorks = build_dorks(roles, city_pairs, terms_per_role=2 if req.level == "quick" else 4) if use_dork else []
        # stage-1 progress unit = one category in one city, or one dork query
        progress.set_total((len(categories) * len(cities) if use_maps else 0) + len(dorks))

        async def maps_task() -> dict[str, Place]:
            if not use_maps or not cities:
                return {}
            return await collect_places(places_client, cities, categories, req.level, progress)

        async def dork_task():
            if not use_dork:
                return [], {}
            progress.add_log(f"Dorking: {len(dorks)} שאילתות")
            pages = {"quick": 1, "normal": 1, "deep": 3}.get(req.level, 1)
            return await run_dorks(provider, dorks, matcher, pages, progress)

        places, (dork_jobs, dork_domains) = await asyncio.gather(maps_task(), dork_task())
        for dom, url in dork_domains.items():
            if dom not in places:
                places[dom] = Place("", dom, homepage_of(url), dom, "", "", [], source="dork")
        progress.counters["companies_total"] = len(places)
        progress.add_log(f"נאספו {len(places)} אתרים ייחודיים")

        # ---------------- stage 2: careers pages ----------------
        progress.set_stage("careers", "שלב 2 — חיפוש עמודי קריירה / דרושים באתרי הבית", total=len(places))
        infos: dict[str, CareerInfo] = {}

        async def one(p: Place):
            try:
                # hard cap per site so one slow server can never freeze the stage
                return p.domain, await asyncio.wait_for(find_careers(p.domain, p.website, fetcher, cache), 75)
            except Exception:  # noqa: BLE001
                return p.domain, CareerInfo(p.domain, p.website, status="unreachable")

        for fut in asyncio.as_completed([one(p) for p in places.values()]):
            dom, info = await fut
            infos[dom] = info
            progress.inc("sites_checked")
            progress.tick(current=dom)
            if info.career_url or info.ats:
                progress.inc("with_careers")
            if progress.stage_done % 100 == 0:
                progress.add_log(f"נבדקו {progress.stage_done}/{len(places)} אתרים · "
                                 f"{progress.counters.get('with_careers', 0)} עם עמוד קריירה")
        with_careers = [i for i in infos.values() if i.career_url or i.ats]
        progress.add_log(f"נמצאו {len(with_careers)} אתרים עם עמוד קריירה מתוך {len(infos)}")

        # ---------------- stage 3: match roles ----------------
        progress.set_stage("jobs", "שלב 3 — חיפוש התפקידים בעמודי הדרושים", total=len(with_careers))
        all_jobs: list[dict] = []
        companies_out: list[dict] = []

        async def scan(info: CareerInfo):
            try:
                return info, await asyncio.wait_for(extract_jobs(info, matcher, fetcher), 120)
            except Exception:  # noqa: BLE001
                return info, ([], 0)

        for fut in asyncio.as_completed([scan(i) for i in with_careers]):
            info, (jobs, total) = await fut
            place = places.get(info.domain)
            progress.inc("careers_scanned")
            progress.tick(current=info.domain)
            companies_out.append({
                "company": place.name if place else info.domain, "domain": info.domain,
                "city": place.city if place else "", "address": place.address if place else "",
                "career_url": info.career_url, "ats": [a.kind for a in info.ats],
                "positions_seen": total, "matches": len(jobs), "source": place.source if place else "",
            })
            for j in jobs:
                all_jobs.append(_job_row(j, place, info.domain, city_names))
            progress.counters["matches"] = len(all_jobs)

        # dork hits that were not already found via careers pages
        seen = {r["url"].split("#")[0].rstrip("/") for r in all_jobs}
        for j in dork_jobs:
            if j.url.split("#")[0].rstrip("/") not in seen:
                all_jobs.append(_job_row(j, places.get(j.extra.get("company", "")), j.extra.get("company", ""), city_names))
        progress.counters["matches"] = len(all_jobs)
        await fetcher.close()

    all_jobs.sort(key=lambda r: (not r["location_ok"], r["company"].lower()))
    companies_out.sort(key=lambda c: (-c["matches"], -c["positions_seen"], c["company"].lower()))
    result = {
        "request": asdict(req),
        "cities": city_pairs,
        "role_terms": {r.original: r.terms for r in roles},
        "stats": {**progress.counters, "places_api_requests": places_budget.used,
                  "search_api_requests": search_budget.used, "seconds": round(time.time() - t0)},
        "jobs": all_jobs,
        "companies_with_careers": companies_out,
        "companies_without_careers": [
            {"company": places[d].name if d in places else d, "domain": d, "homepage": i.homepage, "status": i.status}
            for d, i in infos.items() if not (i.career_url or i.ats)
        ],
    }
    return result


def _job_row(j: Job, place: Place | None, domain: str, city_names: list[str]) -> dict:
    return {
        "title": j.title, "url": j.url, "location": j.location, "department": j.department,
        "company": place.name if place else domain, "domain": domain,
        "company_city": place.city if place else "", "source": j.source,
        "role": j.extra.get("role", ""), "matched_term": j.extra.get("term", ""),
        "location_ok": _location_ok(j.location, city_names),
    }


def save_result(result: dict, settings: Settings, job_id: str) -> str:
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    path = settings.results_dir / f"{job_id}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
