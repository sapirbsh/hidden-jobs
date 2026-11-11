"""Shared helpers: URL/domain normalisation, request budget, progress reporting."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse, urlunparse

from .config import IGNORED_WEBSITE_DOMAINS, JOB_BOARD_DOMAINS


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


def is_job_board(url: str) -> bool:
    return _domain_in(registrable_domain(url), JOB_BOARD_DOMAINS)


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
    on_change: Callable[["Progress"], None] | None = None

    def set_stage(self, stage: str, label: str) -> None:
        self.stage, self.stage_label = stage, label
        self.add_log(f"▶ {label}")

    def add_log(self, msg: str) -> None:
        self.message = msg
        self.log.append(msg)
        if len(self.log) > 400:
            self.log = self.log[-400:]

    def inc(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def to_dict(self) -> dict:
        return {
            "stage": self.stage, "stage_label": self.stage_label, "message": self.message,
            "counters": self.counters, "log": self.log[-60:], "done": self.done, "error": self.error,
        }
