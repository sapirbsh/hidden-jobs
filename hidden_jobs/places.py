"""Stage 1 — collect every place with a website inside the requested cities.

Google Places Text Search (New) returns at most 60 results per query (3 pages × 20).
To be truly exhaustive we use an *adaptive quadtree*:
  1. search a category inside the city's bounding box;
  2. if the box is "saturated" (≈60 results) split it into 4 sub-boxes and search each;
  3. repeat until boxes are no longer saturated or the max depth is reached.
Dense areas (e.g. Ramat HaHayal) get many small tiles, empty areas cost one request.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict

import httpx

from .cache import DAY, Cache
from .role_profiles import is_excluded_type
from .util import Budget, BudgetExceeded, Progress, is_ignored_site, registrable_domain

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# websiteUri is what we really need (Enterprise SKU). Keep the mask minimal = cheaper.
PLACE_FIELDS = "places.id,places.displayName,places.websiteUri,places.formattedAddress,places.types,places.location"
SATURATION = 55          # treat a box as saturated when it returned at least this many results
MIN_BOX_DEG = 0.004      # ~400 m — never split below this
DEPTH_BY_LEVEL = {"quick": 0, "normal": 2, "deep": 4}


@dataclass
class Box:
    south: float
    west: float
    north: float
    east: float

    def split(self) -> list["Box"]:
        mid_lat = (self.south + self.north) / 2
        mid_lng = (self.west + self.east) / 2
        return [
            Box(self.south, self.west, mid_lat, mid_lng), Box(self.south, mid_lng, mid_lat, self.east),
            Box(mid_lat, self.west, self.north, mid_lng), Box(mid_lat, mid_lng, self.north, self.east),
        ]

    def rect(self) -> dict:
        return {"rectangle": {"low": {"latitude": self.south, "longitude": self.west},
                              "high": {"latitude": self.north, "longitude": self.east}}}

    def key(self) -> str:
        return f"{self.south:.5f},{self.west:.5f},{self.north:.5f},{self.east:.5f}"

    @property
    def size(self) -> float:
        return min(self.north - self.south, self.east - self.west)


@dataclass
class Place:
    place_id: str
    name: str
    website: str
    domain: str
    address: str
    city: str
    types: list
    lat: float | None = None
    lng: float | None = None
    source: str = "maps"


@dataclass
class City:
    query: str
    name_he: str
    name_en: str
    box: Box


class PlacesClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient, cache: Cache, budget: Budget,
                 progress: Progress):
        self.api_key = api_key
        self.client = client
        self.cache = cache
        self.budget = budget
        self.progress = progress
        self.office_only = False   # drop shops/food/leisure places (set per search)

    async def _post(self, body: dict, field_mask: str) -> dict:
        cache_key = repr(sorted(body.items())) + field_mask
        cached = self.cache.get("places", cache_key, max_age=14 * DAY)
        if cached is not None:
            self.progress.inc("places_cache_hits")
            return cached
        self.budget.take()
        self.progress.inc("places_requests")
        r = await self.client.post(
            SEARCH_URL, json=body,
            headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": field_mask,
                     "Content-Type": "application/json"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Places API {r.status_code}: {r.text[:300]}")
        data = r.json()
        self.cache.set("places", cache_key, data)
        return data

    # ---------- city geocoding ----------
    async def resolve_city(self, query: str) -> City:
        mask = "places.displayName,places.viewport,places.location"
        he = await self._post({"textQuery": query, "languageCode": "he", "regionCode": "IL",
                               "includedType": "locality", "pageSize": 1}, mask)
        if not he.get("places"):
            # some cities are not typed 'locality' — retry without the type filter
            he = await self._post({"textQuery": query, "languageCode": "he", "regionCode": "IL",
                                   "pageSize": 1}, mask)
        if not he.get("places"):
            raise ValueError(f"העיר '{query}' לא נמצאה בגוגל מפות")
        p = he["places"][0]
        en = await self._post({"textQuery": query, "languageCode": "en", "regionCode": "IL",
                               "includedType": "locality", "pageSize": 1}, "places.displayName")
        name_en = (en.get("places") or [{}])[0].get("displayName", {}).get("text", query)
        vp = p.get("viewport")
        if vp:
            box = Box(vp["low"]["latitude"], vp["low"]["longitude"], vp["high"]["latitude"], vp["high"]["longitude"])
        else:
            loc = p["location"]
            d = 0.03
            box = Box(loc["latitude"] - d, loc["longitude"] - d, loc["latitude"] + d, loc["longitude"] + d)
        return City(query=query, name_he=p["displayName"]["text"], name_en=name_en, box=box)

    # ---------- tile search ----------
    async def search_box(self, category: str, box: Box) -> list[dict]:
        results: list[dict] = []
        token = None
        for _ in range(3):  # API hard limit: 3 pages
            body = {"textQuery": category, "languageCode": "he", "regionCode": "IL",
                    "pageSize": 20, "locationRestriction": box.rect()}
            if token:
                body["pageToken"] = token
            data = await self._post(body, PLACE_FIELDS + ",nextPageToken")
            results.extend(data.get("places", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return results

    async def crawl_category(self, city: City, category: str, max_depth: int, sem: asyncio.Semaphore,
                             sink: dict[str, Place]) -> None:
        async def visit(box: Box, depth: int) -> None:
            async with sem:
                try:
                    found = await self.search_box(category, box)
                except BudgetExceeded:
                    return
            for raw in found:
                self._add(raw, city, sink)
            if len(found) >= SATURATION and depth < max_depth and box.size / 2 >= MIN_BOX_DEG:
                self.progress.inc("tiles_split")
                await asyncio.gather(*(visit(b, depth + 1) for b in box.split()))

        await visit(city.box, 0)

    def _add(self, raw: dict, city: City, sink: dict[str, Place]) -> None:
        website = raw.get("websiteUri")
        if not website or is_ignored_site(website):
            return
        if self.office_only and is_excluded_type(raw.get("types", [])):
            self.progress.inc("places_filtered_type")
            return
        domain = registrable_domain(website)
        if domain in sink:
            return
        loc = raw.get("location") or {}
        sink[domain] = Place(
            place_id=raw.get("id", ""), name=raw.get("displayName", {}).get("text", domain),
            website=website, domain=domain, address=raw.get("formattedAddress", ""),
            city=city.name_he, types=raw.get("types", []),
            lat=loc.get("latitude"), lng=loc.get("longitude"),
        )
        self.progress.counters["places_with_site"] = len(sink)


async def collect_places(client: PlacesClient, cities: list[City], categories: list[str], level: str,
                         progress: Progress) -> dict[str, Place]:
    sink: dict[str, Place] = {}
    sem = asyncio.Semaphore(6)
    max_depth = DEPTH_BY_LEVEL.get(level, 2)
    for city in cities:
        progress.add_log(f"מפות: סורק את {city.name_he} ({len(categories)} קטגוריות, עומק {max_depth})")
        async def one(cat: str) -> None:
            await client.crawl_category(city, cat, max_depth, sem, sink)
            progress.tick(current=f"{city.name_he} · {cat}")

        await asyncio.gather(*(one(c) for c in categories))
        if client.budget.exhausted:
            progress.add_log("⚠ הגעת לתקרת הבקשות ל-Google Places — ממשיך עם מה שנאסף")
            break
    return sink


def place_to_dict(p: Place) -> dict:
    return asdict(p)
