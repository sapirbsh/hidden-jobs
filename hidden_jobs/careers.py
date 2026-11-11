"""Stage 2 + 3 — find the careers ("דרושים") page of each website and extract matching roles."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .ats import AtsRef, Job, detect_ats, fetch_ats_jobs
from .cache import DAY, Cache
from .fetcher import Fetcher
from .matcher import RoleMatcher
from .util import is_job_board, registrable_domain

# ---------------------------------------------------------------- keywords
HREF_KEYWORDS = [
    "careers", "career", "jobs", "job-openings", "open-positions", "positions", "join-us", "joinus",
    "join-our-team", "work-with-us", "workwithus", "vacancies", "opportunities", "hiring", "recruitment",
    "דרושים", "דרושות", "קריירה", "משרות", "הצטרפו", "עבודה", "גיוס",
]
TEXT_KEYWORDS = [
    "careers", "career", "jobs", "join us", "join our team", "we're hiring", "we are hiring", "work with us",
    "open positions", "job openings", "vacancies", "work at",
    "דרושים", "דרושות", "קריירה", "משרות", "הצטרפו", "הצטרפו אלינו", "בואו לעבוד", "לעבוד אצלנו",
    "עבודה אצלנו", "משרות פתוחות", "הצטרפו לצוות", "דרושים/ות", "גיוס עובדים",
]
COMMON_PATHS = [  # probed in parallel, first match (in this order) wins
    "/careers", "/jobs", "/career", "/join-us", "/he/careers",
    "/%D7%93%D7%A8%D7%95%D7%A9%D7%99%D7%9D",  # /דרושים
    "/%D7%A7%D7%A8%D7%99%D7%99%D7%A8%D7%94",  # /קריירה
]
# links on a careers page that probably lead to the full list / more pages
LISTING_HINTS = re.compile(
    r"(all (open )?(jobs|positions|roles)|open positions|view (all )?(jobs|positions|openings)|see (all )?(jobs|openings)"
    r"|משרות פתוחות|לכל המשרות|כל המשרות|צפו במשרות|למשרות|הצג עוד|page=\d|/page/\d)", re.I)
JOB_URL_HINTS = re.compile(r"/(jobs?|positions?|careers?|openings?|vacanc|join|משרה|משרות|דרושים)[/\-_]", re.I)
GENERIC_LINK_TEXT = re.compile(r"^(apply|apply now|read more|more|details|learn more|view|להגשת מועמדות|הגש מועמדות|"
                               r"פרטים נוספים|לפרטים|קרא עוד|קראו עוד|למשרה|הגשת מועמדות|שלח קו״ח|שלחו קו\"ח)\s*[›»>]*$", re.I)
NEG_TEXT = re.compile(r"(steve jobs|blog|news|privacy|terms)", re.I)

_TEXT_RX = re.compile("|".join(re.escape(k) for k in TEXT_KEYWORDS), re.I)

# phrases that identify a job board / placement agency from its homepage title, description or H1
JOB_BOARD_PHRASES = re.compile(
    r"(לוח (ה)?דרושים|לוח (ה)?משרות|אתר (ה)?דרושים|חיפוש עבודה|מחפשים עבודה|מחפש עבודה|אלפי משרות|"
    r"משרות (חמות|בכל הארץ|באזור)|חברת השמה|השמת עובדים|גיוס והשמה|השמה וגיוס|ייעוץ תעסוקתי|"
    r"job board|find (a |your next )?jobs?|search (for )?jobs|thousands of jobs|jobs in israel|"
    r"staffing (agency|company)|recruitment agency|headhunt)", re.I)


@dataclass
class CareerInfo:
    domain: str
    homepage: str
    career_url: str | None = None
    method: str = ""                  # "link", "path", "ats-on-home", "dork"
    ats: list[AtsRef] = field(default_factory=list)
    status: str = "ok"                # ok | unreachable | no-careers | job-board

    def to_dict(self) -> dict:
        return {"domain": self.domain, "homepage": self.homepage, "career_url": self.career_url,
                "method": self.method, "status": self.status,
                "ats": [{"kind": a.kind, "token": a.token} for a in self.ats]}

    @staticmethod
    def from_dict(d: dict) -> "CareerInfo":
        return CareerInfo(d["domain"], d["homepage"], d.get("career_url"), d.get("method", ""),
                          [AtsRef(a["kind"], a["token"], "") for a in d.get("ats", [])], d.get("status", "ok"))


def _same_site(url: str, domain: str) -> bool:
    host = registrable_domain(url)
    base = ".".join(domain.split(".")[-3:]) if domain.endswith((".co.il", ".org.il", ".ac.il", ".gov.il")) \
        else ".".join(domain.split(".")[-2:])
    return host == domain or host.endswith("." + base) or host == base


def _score_link(href: str, text: str) -> int:
    h = unquote(href).lower()
    t = " ".join(text.split()).lower()
    if NEG_TEXT.search(t) or h.startswith(("mailto:", "tel:", "javascript:")):
        return 0
    score = 0
    path = urlparse(h).path + "?" + urlparse(h).query
    if any(f"/{k}" in path or f"{k}." in urlparse(h).netloc for k in HREF_KEYWORDS):
        score += 3
    if t and len(t) < 40 and _TEXT_RX.search(t):
        score += 4
    return score


def find_career_links(html: str, base_url: str, domain: str) -> list[tuple[int, str]]:
    soup = BeautifulSoup(html, "lxml")
    cands: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        text = a.get_text(" ", strip=True) or a.get("title", "") or a.get("aria-label", "")
        s = _score_link(href, text)
        if s and (_same_site(href, domain) or detect_ats("", href)):
            cands[href.split("#")[0]] = max(cands.get(href.split("#")[0], 0), s)
    return sorted(((s, u) for u, s in cands.items()), reverse=True)


def looks_like_job_board(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    head = " ".join([
        soup.title.get_text(" ", strip=True) if soup.title else "",
        meta.get("content", "") if meta else "",
        " ".join(h.get_text(" ", strip=True) for h in soup.find_all("h1")[:3]),
    ])
    return bool(JOB_BOARD_PHRASES.search(head))


def page_looks_like_careers(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text() if soup.title else "") + " " + " ".join(
        h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:6])
    return bool(_TEXT_RX.search(title)) or bool(detect_ats(html))


# ---------------------------------------------------------------- stage 2
async def find_careers(domain: str, website: str, fetcher: Fetcher, cache: Cache) -> CareerInfo:
    cached = cache.get("careers2", domain, max_age=7 * DAY)
    if cached:
        return CareerInfo.from_dict(cached)
    info = await _find_careers_uncached(domain, website, fetcher)
    cache.set("careers2", domain, info.to_dict())
    return info


async def _find_careers_uncached(domain: str, website: str, fetcher: Fetcher) -> CareerInfo:
    home = await fetcher.get(website, render_if_empty=True, try_http=True)
    info = CareerInfo(domain=domain, homepage=website)
    if not home or home.status >= 400:
        info.status = "unreachable"
        return info
    if is_job_board(home.url) or looks_like_job_board(home.html):
        info.status = "job-board"
        return info
    info.ats = detect_ats(home.html, home.url)

    links = find_career_links(home.html, home.url, domain)
    for _score, url in links[:3]:
        if ats := detect_ats("", url):
            info.career_url, info.method = url, "link"
            info.ats = list({(a.kind, a.token.lower()): a for a in info.ats + ats}.values())
            return info
        page = await fetcher.get(url, render_if_empty=True)
        if page and page.status < 400:
            info.career_url, info.method = page.url, "link"
            info.ats = list({(a.kind, a.token.lower()): a for a in info.ats + detect_ats(page.html, page.url)}.values())
            return info

    # no visible link — probe the usual paths
    root = f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}"
    probes = await asyncio.gather(*(fetcher.get(root + path, timeout=7) for path in COMMON_PATHS))
    for page in probes:
        if page and page.status == 200 and urlparse(page.url).path not in ("", "/") and page_looks_like_careers(page.html):
            info.career_url, info.method = page.url, "path"
            info.ats = list({(a.kind, a.token.lower()): a for a in info.ats + detect_ats(page.html, page.url)}.values())
            return info

    if info.ats:
        info.career_url, info.method = info.ats[0].url if info.ats[0].url.startswith("http") else home.url, "ats-on-home"
        return info
    info.status = "no-careers"
    return info


# ---------------------------------------------------------------- stage 3
def _text_fragment_link(url: str, text: str) -> str:
    snippet = " ".join(text.split())[:60]
    return f"{url.split('#')[0]}#:~:text={quote(snippet)}"


def extract_jobs_from_html(html: str, page_url: str, matcher: RoleMatcher) -> list[Job]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    jobs: dict[tuple[str, str], Job] = {}

    # 1) anchors whose own text is a matching job title
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True) or a.get("title", "")
        if not text or len(text) > 140:
            continue
        hit = matcher.match(text)
        if hit:
            url = urljoin(page_url, a["href"])
            jobs.setdefault((text.lower(), url), Job(" ".join(text.split()), url, source="career-page",
                                                     extra={"role": hit[0], "term": hit[1]}))

    # 2) headings / list items / cards with a matching title (link via nearest anchor or text-fragment)
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "li", "strong", "b", "td", "p", "span", "div"]):
        if el.find(["h1", "h2", "h3", "h4", "li", "div", "p"]):  # only leaf-ish blocks
            continue
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 120:
            continue
        hit = matcher.match(text)
        if not hit:
            continue
        link = None
        node = el
        for _ in range(4):  # climb a few levels looking for the card's link
            if node is None:
                break
            a = node if getattr(node, "name", None) == "a" and node.get("href") else node.find("a", href=True)
            if a and (GENERIC_LINK_TEXT.match(a.get_text(" ", strip=True)) or JOB_URL_HINTS.search(a["href"])):
                link = urljoin(page_url, a["href"])
                break
            node = node.parent
        url = link or _text_fragment_link(page_url, text)
        key = (" ".join(text.split()).lower(), url.split("#")[0])
        if not any(k[0] == key[0] for k in jobs):
            jobs[key] = Job(" ".join(text.split()), url, source="career-page",
                            extra={"role": hit[0], "term": hit[1]})
    return list(jobs.values())


def listing_links(html: str, page_url: str, domain: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"]).split("#")[0]
        text = a.get_text(" ", strip=True)
        if href == page_url.split("#")[0] or not _same_site(href, domain):
            continue
        if LISTING_HINTS.search(text) or LISTING_HINTS.search(href):
            out.append(href)
    return list(dict.fromkeys(out))


async def extract_jobs(info: CareerInfo, matcher: RoleMatcher, fetcher: Fetcher,
                       max_subpages: int = 8) -> tuple[list[Job], int]:
    """Return (matching jobs, total positions seen on the careers site)."""
    matched: list[Job] = []
    total = 0
    ats_refs = list(info.ats)

    if info.career_url and not detect_ats("", info.career_url):
        page = await fetcher.get(info.career_url, render_if_empty=True)
        if page and page.status < 400:
            ats_refs += [a for a in detect_ats(page.html, page.url) if a not in ats_refs]
            matched += extract_jobs_from_html(page.html, page.url, matcher)
            subs = listing_links(page.html, page.url, info.domain)[:max_subpages]
            pages = await asyncio.gather(*(fetcher.get(u, render_if_empty=True) for u in subs))
            for sp in pages:
                if sp and sp.status < 400:
                    ats_refs += [a for a in detect_ats(sp.html, sp.url) if a not in ats_refs]
                    matched += extract_jobs_from_html(sp.html, sp.url, matcher)
    elif info.career_url:
        ats_refs += [a for a in detect_ats("", info.career_url) if a not in ats_refs]

    seen_ats = set()
    for ref in ats_refs:
        if (ref.kind, ref.token.lower()) in seen_ats:
            continue
        seen_ats.add((ref.kind, ref.token.lower()))
        ats_jobs = await fetch_ats_jobs(ref, fetcher)
        total += len(ats_jobs)
        for j in ats_jobs:
            hit = matcher.match(f"{j.title} {j.department}")
            if hit:
                j.extra = {"role": hit[0], "term": hit[1]}
                matched.append(j)

    # dedupe
    uniq: dict[tuple[str, str], Job] = {}
    for j in matched:
        uniq.setdefault((j.title.lower(), j.url.split("#")[0]), j)
    return list(uniq.values()), max(total, len(uniq))
