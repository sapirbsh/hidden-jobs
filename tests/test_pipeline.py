"""End-to-end test with a fully mocked internet (no API keys / network needed)."""
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hidden_jobs.ats import detect_ats  # noqa: E402
from hidden_jobs.config import Settings  # noqa: E402
from hidden_jobs.matcher import RoleMatcher, expand_roles  # noqa: E402
from hidden_jobs.pipeline import SearchRequest, run_search  # noqa: E402
from hidden_jobs.places import Box  # noqa: E402
from hidden_jobs.util import Progress  # noqa: E402

CITY_BOX = {"low": {"latitude": 32.05, "longitude": 34.85}, "high": {"latitude": 32.13, "longitude": 34.93}}


def place(i, name, site):
    return {"id": f"p{i}", "displayName": {"text": name}, "websiteUri": site,
            "formattedAddress": "פתח תקווה", "types": ["corporate_office"],
            "location": {"latitude": 32.09, "longitude": 34.88}}


REAL = [place(1, "Acme", "https://www.acme.co.il/"), place(2, "Beta Security", "https://beta.com"),
        place(3, "Gamma Networks", "https://gamma.co.il"), place(4, "Delta Bakery", "https://delta.com"),
        place(5, "Down Corp", "https://down.com"), place(6, "FB Only", "https://facebook.com/somebiz")]
FILLER = [place(100 + i, f"Filler {i}", f"https://filler{i}.com") for i in range(60)]
calls = {"places": 0, "boxes": set()}


def places_handler(body):
    calls["places"] += 1
    if body.get("includedType") == "locality" or "locationRestriction" not in body:
        name = "Petah Tikva" if body.get("languageCode") == "en" else "פתח תקווה"
        return {"places": [{"displayName": {"text": name}, "viewport": CITY_BOX,
                            "location": {"latitude": 32.09, "longitude": 34.89}}]}
    rect = body["locationRestriction"]["rectangle"]
    is_full = abs(rect["low"]["latitude"] - 32.05) < 1e-9 and abs(rect["high"]["latitude"] - 32.13) < 1e-9 \
        and abs(rect["high"]["longitude"] - 34.93) < 1e-9 and abs(rect["low"]["longitude"] - 34.85) < 1e-9
    calls["boxes"].add(json.dumps(rect, sort_keys=True))
    if body["textQuery"] == "company" and is_full:
        # saturated → must trigger a split
        page = int(body.get("pageToken", "0"))
        chunk = FILLER[page * 20:(page + 1) * 20]
        return {"places": chunk, **({"nextPageToken": str(page + 1)} if page < 2 else {})}
    if body["textQuery"] == "company":
        return {"places": REAL}
    return {"places": []}


SITES = {
    "https://www.acme.co.il/": '<html><body><nav><a href="/about">אודות</a></nav>'
                               '<a href="/careers">דרושים</a></body></html>',
    "https://www.acme.co.il/careers": '<html><head><title>דרושים - Acme</title></head><body>'
        '<div class="job"><h3>אנליסט/ית SOC Tier 1</h3><p>משמרות</p><a href="/careers/soc-123">פרטים נוספים</a></div>'
        '<div class="job"><h3>מנהלת חשבונות</h3><a href="/careers/acc-9">פרטים נוספים</a></div>'
        '<a href="/careers?page=2">הצג עוד</a></body></html>',
    "https://www.acme.co.il/careers?page=2": '<html><body><ul><li><a href="/careers/ir-5">Incident Response Analyst</a></li></ul></body></html>',
    "https://beta.com/": "<html><body><h1>Beta Security</h1><a href='/products'>Products</a></body></html>",
    "https://beta.com/careers": "<html><head><title>Careers at Beta</title></head><body>"
        "<script src='https://boards.greenhouse.io/embed/job_board/js?for=betasec'></script></body></html>",
    "https://gamma.co.il/": "<html><body><a href='https://www.comeet.com/jobs/gamma/A1.B2C'>Join us</a></body></html>",
    "https://delta.com/": "<html><body><h1>Fresh bread</h1><a href='/menu'>Menu</a></body></html>",
    "https://epsilon.co.il/": "<html><body><a href='/jobs'>Jobs</a></body></html>",
    "https://epsilon.co.il/jobs": "<html><head><title>Jobs</title></head><body><h2>Open positions</h2>"
        "<a href='/jobs/noc-1'>NOC Engineer - Night shifts</a></body></html>",
}
GREENHOUSE = {"jobs": [
    {"title": "SOC Analyst", "absolute_url": "https://boards.greenhouse.io/betasec/jobs/1", "location": {"name": "Petah Tikva, Israel"}},
    {"title": "Social Media Manager", "absolute_url": "https://boards.greenhouse.io/betasec/jobs/2", "location": {"name": "Tel Aviv"}},
    {"title": "SOC Team Lead", "absolute_url": "https://boards.greenhouse.io/betasec/jobs/3", "location": {"name": "New York, NY"}},
]}
COMEET = ('<html><script>COMPANY_POSITIONS_DATA = [{"name": "NOC Engineer", "url_comeet_hosted_page": '
          '"https://www.comeet.com/jobs/gamma/A1.B2C/noc-engineer/11.111", "location": {"city": "Petah Tikva"}, '
          '"department": "IT"}, {"name": "Sales Rep", "url_comeet_hosted_page": "https://x", "location": {"city": "Petah Tikva"}}];</script></html>')
SERPER = {"organic": [
    {"title": "SOC Analyst - Zeta | Comeet", "link": "https://www.comeet.com/jobs/zeta/C3.D4E/soc-analyst/22.222", "snippet": "Petah Tikva"},
    {"title": "NOC Engineer at Epsilon", "link": "https://epsilon.co.il/jobs/noc-1", "snippet": "פתח תקווה"},
    {"title": "SOC Analyst - LinkedIn", "link": "https://il.linkedin.com/jobs/view/123", "snippet": "x"},
]}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    host = request.url.host
    if host == "places.googleapis.com":
        return httpx.Response(200, json=places_handler(json.loads(request.content)))
    if host == "google.serper.dev":
        return httpx.Response(200, json=SERPER)
    if host == "boards-api.greenhouse.io":
        return httpx.Response(200, json=GREENHOUSE)
    if host == "www.comeet.com":
        return httpx.Response(200, text=COMEET, headers={"content-type": "text/html"})
    if host == "down.com":
        return httpx.Response(500, text="err", headers={"content-type": "text/html"})
    if url.endswith("/robots.txt"):
        return httpx.Response(404, text="")
    for u in (url, url + "/"):
        if u in SITES:
            return httpx.Response(200, text=SITES[u], headers={"content-type": "text/html; charset=utf-8"})
    if host.startswith("filler"):
        return httpx.Response(200, text="<html><body>hi</body></html>", headers={"content-type": "text/html"})
    return httpx.Response(404, text="not found", headers={"content-type": "text/html"})


def test_matcher_word_boundaries():
    m = RoleMatcher(expand_roles(["SOC"]))
    assert m.match("SOC Analyst")
    assert m.match("אנליסט/ית SOC Tier 1")
    assert not m.match("Social Media Manager")
    assert m.match("דרוש/ה אנליסט סייבר")


def test_detect_ats():
    kinds = {(a.kind, a.token) for a in detect_ats(
        "<iframe src='https://boards.greenhouse.io/embed/job_board?for=acme'></iframe>"
        "<a href='https://jobs.lever.co/foo'>x</a><a href='https://www.comeet.com/jobs/bar/AB.CDE'>y</a>")}
    assert ("greenhouse", "acme") in kinds and ("lever", "foo") in kinds and ("comeet", "bar/AB.CDE") in kinds


def test_box_split():
    b = Box(0, 0, 1, 1)
    assert len(b.split()) == 4 and b.split()[3].south == 0.5


def test_end_to_end(tmp_path):
    s = Settings(google_maps_api_key="test", serper_api_key="test", search_provider="serper")
    s.cache_path = tmp_path / "c.sqlite"
    s.results_dir = tmp_path / "r"
    progress = Progress()
    req = SearchRequest(cities=["פתח תקווה"], roles=["SOC", "NOC", "Incident Response"], level="normal",
                        extra_categories=["company"], only_extra_categories=True)
    result = asyncio.run(run_search(req, s, progress, transport=httpx.MockTransport(handler)))

    titles = {j["title"]: j for j in result["jobs"]}
    print(json.dumps(result["jobs"], ensure_ascii=False, indent=1))
    # career page found via "דרושים" link, card title linked to its detail page
    assert titles["אנליסט/ית SOC Tier 1"]["url"] == "https://www.acme.co.il/careers/soc-123"
    # followed "show more" pagination
    assert "Incident Response Analyst" in titles
    # not a match
    assert "מנהלת חשבונות" not in titles and "Social Media Manager" not in titles and "Sales Rep" not in titles
    # greenhouse found by probing /careers, location tagging
    assert titles["SOC Analyst"]["source"] == "greenhouse" and titles["SOC Analyst"]["location_ok"]
    assert titles["SOC Team Lead"]["location_ok"] is False
    # comeet JSON
    assert titles["NOC Engineer"]["url"].startswith("https://www.comeet.com/jobs/gamma")
    # dorking: comeet hit for company not on maps + epsilon discovered and crawled; linkedin excluded
    assert any("zeta" in j["url"] for j in result["jobs"])
    assert any(j["domain"] == "epsilon.co.il" for j in result["jobs"])
    assert not any("linkedin" in j["url"] for j in result["jobs"])
    # same URL found by both careers crawl and dorking appears once
    assert sum(j["url"] == "https://epsilon.co.il/jobs/noc-1" for j in result["jobs"]) == 1
    # facebook-only site skipped, delta has no careers, down is unreachable
    without = {c["domain"]: c["status"] for c in result["companies_without_careers"]}
    assert without.get("delta.com") == "no-careers" and without.get("down.com") == "unreachable"
    assert "facebook.com" not in without
    # quadtree split happened (1 full box + 4 sub-boxes)
    assert progress.counters.get("tiles_split", 0) >= 1 and len(calls["boxes"]) == 5

    # second run is served from cache: no new Places calls
    before = calls["places"]
    asyncio.run(run_search(req, s, Progress(), transport=httpx.MockTransport(handler)))
    assert calls["places"] == before
