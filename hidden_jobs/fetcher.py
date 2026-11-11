"""Polite async HTTP fetcher: robots.txt, per-host throttling, optional headless browser."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings


@dataclass
class Page:
    url: str          # final URL after redirects
    status: int
    html: str
    rendered: bool = False


class Fetcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}   # one lock PER SITE (a global lock serialised everything)
        self._host_last: dict[str, float] = {}
        self._host_fail: dict[str, int] = {}
        self._sem = asyncio.Semaphore(settings.concurrency)
        self._browser = None
        self._pw = None
        self._browser_lock = asyncio.Lock()

    def _dead(self, host: str) -> bool:
        return self._host_fail.get(host, 0) >= 2

    def _fail(self, host: str) -> None:
        self._host_fail[host] = self._host_fail.get(host, 0) + 1

    # ---------- robots ----------
    async def allowed(self, url: str) -> bool:
        if not self.s.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            lock = self._robots_locks.setdefault(base, asyncio.Lock())
            async with lock:
                if base not in self._robots:
                    rp = None
                    try:
                        async with self._sem:
                            r = await self.client.get(base + "/robots.txt", timeout=6)
                        if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                            rp = RobotFileParser()
                            rp.parse(r.text.splitlines())
                    except Exception:
                        rp = None
                    self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.s.user_agent, url)

    # ---------- fetch ----------
    async def get(self, url: str, render_if_empty: bool = False) -> Page | None:
        host = urlparse(url).netloc
        if self._dead(host) or not await self.allowed(url):
            return None
        # at most ~2 requests/second per host (wait OUTSIDE the semaphore so other sites keep going)
        wait = 0.5 - (time.monotonic() - self._host_last.get(host, 0))
        self._host_last[host] = time.monotonic() + max(wait, 0)
        if wait > 0:
            await asyncio.sleep(wait)
        r = None
        async with self._sem:
            for attempt in ([url, "http://" + url[8:]] if url.startswith("https://") else [url]):
                if self._dead(host):
                    break
                try:
                    r = await self.client.get(attempt, timeout=self.s.request_timeout)
                    break
                except Exception:
                    self._fail(host)
        if r is None:
            return None
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype and "json" not in ctype:
            return None
        page = Page(url=str(r.url), status=r.status_code, html=r.text)
        if render_if_empty and self.s.use_playwright and r.status_code == 200 and _looks_js_only(r.text):
            rendered = await self.render(str(r.url))
            if rendered:
                return rendered
        return page

    async def get_json(self, url: str) -> dict | list | None:
        try:
            async with self._sem:
                r = await self.client.get(url, timeout=self.s.request_timeout,
                                          headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None

    # ---------- optional headless rendering (for React/Angular career pages) ----------
    async def render(self, url: str) -> Page | None:
        async with self._browser_lock:
            if self._browser is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError:
                    self.s.use_playwright = False
                    return None
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True)
        ctx = await self._browser.new_context(user_agent=self.s.user_agent)
        try:
            pg = await ctx.new_page()
            await pg.goto(url, wait_until="networkidle", timeout=25000)
            return Page(url=pg.url, status=200, html=await pg.content(), rendered=True)
        except Exception:
            return None
        finally:
            await ctx.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


def _looks_js_only(html: str) -> bool:
    """Heuristic: page is a JS app shell with almost no visible text."""
    low = html.lower()
    text_len = len(low) - low.count("<script") * 200
    return ("id=\"root\"" in low or "id=\"app\"" in low or "ng-app" in low or "__next" in low) and text_len < 4000
