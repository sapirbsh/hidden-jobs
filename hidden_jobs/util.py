"""Shared helpers: URL/domain normalisation, request budget, progress reporting."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse, urlunparse

from .config import BASE_DIR, IGNORED_WEBSITE_DOMAINS, JOB_BOARD_DOMAINS, JOB_BOARD_NAME_PATTERN


def registrable_domain(url: str) -> str:
    """Return host without 'www.' (good enough for de-duplication)."""
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _domain_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_ignored_site(url: str) -> bool:
    host = registrable_domain(url)
    return not host or _domain_in(host, IGNORED_WEBSITE_DOMAINS)


_NAME_RX = re.compile(JOB_BOARD_NAME_PATTERN, re.I)
BLOCKLIST_PATH = BASE_DIR / "data" / "blocklist.txt"
_block_cache: tuple[float, set[str]] = (0.0, set())


def blocked_domains() -> set[str]:
    """Domains the user blocked from the UI (data/blocklist.txt, one per line)."""
    global _block_cache
    try:
        mtime = BLOCKLIST_PATH.stat().st_mtime
    except FileNotFoundError:
        return set()
    if mtime != _block_cache[0]:
        lines = BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
        _block_cache = (mtime, {registrable_domain(x.strip()) for x in lines if x.strip() and not x.startswith("#")})
    return _block_cache[1]


def block_domain(domain: str) -> None:
    domain = registrable_domain(domain)
    if not domain or domain in blocked_domains():
        return
    BLOCKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BLOCKLIST_PATH.open("a", encoding="utf-8") as f:
        f.write(domain + "\n")


def is_job_board(url: str) -> bool:
    host = registrable_domain(url)
    if not host:
        return False
    if _domain_in(host, JOB_BOARD_DOMAINS) or _domain_in(host, blocked_domains()):
        return True
    main_label = host.split(".")[0] if not host.startswith(("jobs.", "careers.")) else host.split(".")[1]
    # e.g. "jobify360", "drushim-online" — but NOT company career subdomains like careers.acme.com
    return bool(_NAME_RX.search(main_label))


def homepage_of(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    return urlunparse((p.scheme or "https", p.netloc, "/", "", "", ""))


class BudgetExceeded(Exception):
    pass


class Budget:
    """Thread-safe counter that stops paid API calls once the limit is hit."""

    def __init__(self, limit: int, name: str):
        self.limit = limit
        self.name = name
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise BudgetExceeded(f"{self.name}: request budget of {self.limit} reached")
            self.used += 1

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


# share of the total run each stage usually takes (used for the overall % bar)
STAGE_WEIGHTS = {"places": 0.15, "careers": 0.60, "jobs": 0.25}
STAGE_ORDER = ["places", "careers", "jobs"]


@dataclass
class Progress:
    """Mutable progress object read by the web UI."""
    stage: str = "queued"
    stage_label: str = "ממתין"
    message: str = ""
    counters: dict = field(default_factory=dict)
    log: list = field(default_factory=list)
    done: bool = False
    error: str | None = None
    stage_total: int = 0
    stage_done: int = 0
    stage_started: float = field(default_factory=time.time)
    started: float = field(default_factory=time.time)
    current: str = ""          # what is being processed right now (shown under the bar)

    def set_stage(self, stage: str, label: str, total: int = 0) -> None:
        self.stage, self.stage_label = stage, label
        self.stage_total, self.stage_done = total, 0
        self.stage_started = time.time()
        self.add_log(f"▶ {label}")

    def set_total(self, total: int) -> None:
        self.stage_total = total

    def tick(self, n: int = 1, current: str = "") -> None:
        self.stage_done += n
        if current:
            self.current = current

    def add_log(self, msg: str) -> None:
        self.message = msg
        self.log.append(msg)
        if len(self.log) > 400:
            self.log = self.log[-400:]

    def inc(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    # ----- derived -----
    def stage_pct(self) -> float:
        if self.stage == "done":
            return 100.0
        if not self.stage_total:
            return 0.0
        return min(100.0, 100.0 * self.stage_done / self.stage_total)

    def eta_seconds(self) -> int | None:
        """Remaining time for the current stage, from the average speed so far."""
        if not self.stage_total or self.stage_done < 3 or self.stage_done >= self.stage_total:
            return None
        elapsed = time.time() - self.stage_started
        return int(elapsed / self.stage_done * (self.stage_total - self.stage_done))

    def overall_pct(self) -> float:
        if self.stage == "done" or self.done:
            return 100.0
        if self.stage not in STAGE_ORDER:
            return 0.0
        idx = STAGE_ORDER.index(self.stage)
        before = sum(STAGE_WEIGHTS[s] for s in STAGE_ORDER[:idx])
        return round(100 * (before + STAGE_WEIGHTS[self.stage] * self.stage_pct() / 100), 1)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage, "stage_label": self.stage_label, "message": self.message,
            "counters": self.counters, "log": self.log[-60:], "done": self.done, "error": self.error,
            "stage_total": self.stage_total, "stage_done": self.stage_done,
            "stage_pct": round(self.stage_pct(), 1), "overall_pct": self.overall_pct(),
            "eta_seconds": self.eta_seconds(), "elapsed": int(time.time() - self.started),
            "current": self.current,
        }
