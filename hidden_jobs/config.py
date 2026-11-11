"""Central configuration. Values come from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader (no extra dependency)."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


# Google Maps categories are chosen per role — see role_profiles.py

# Sites we never treat as an employer homepage (social, aggregators, builders' default pages)
IGNORED_WEBSITE_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "wa.me", "whatsapp.com", "google.com", "goo.gl", "linktr.ee",
    "easy.co.il", "b144.co.il", "d.co.il", "zap.co.il", "rest.co.il", "booking.com",
    "tripadvisor.com", "waze.com", "yad2.co.il", "wolt.com",
}

# Job boards — the whole point is to find what is NOT here.
JOB_BOARD_DOMAINS = [
    "linkedin.com", "alljobs.co.il", "drushim.co.il", "jobmaster.co.il", "indeed.com",
    "glassdoor.com", "jobnet.co.il", "gotfriends.co.il", "jobify.co.il", "facebook.com",
    "jobinfo.co.il", "sqlink.com", "ethosia.co.il", "nisha.co.il", "jobs.co.il",
    "secrettelaviv.com", "techaviv.com", "builtin.com", "wellfound.com", "ziprecruiter.com",
]


@dataclass
class Settings:
    google_maps_api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_MAPS_API_KEY", ""))
    # Dorking provider: "serper" (Google results) or "brave"
    search_provider: str = field(default_factory=lambda: os.getenv("SEARCH_PROVIDER", "serper"))
    serper_api_key: str = field(default_factory=lambda: os.getenv("SERPER_API_KEY", ""))
    brave_api_key: str = field(default_factory=lambda: os.getenv("BRAVE_API_KEY", ""))

    # Safety limits so a single search can't burn the API budget
    max_places_requests: int = field(default_factory=lambda: int(os.getenv("MAX_PLACES_REQUESTS", "400")))
    max_search_requests: int = field(default_factory=lambda: int(os.getenv("MAX_SEARCH_REQUESTS", "150")))

    # Crawling
    concurrency: int = field(default_factory=lambda: int(os.getenv("CRAWL_CONCURRENCY", "12")))
    request_timeout: float = field(default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT", "15")))
    respect_robots: bool = field(default_factory=lambda: os.getenv("RESPECT_ROBOTS", "1") == "1")
    use_playwright: bool = field(default_factory=lambda: os.getenv("USE_PLAYWRIGHT", "0") == "1")
    user_agent: str = field(default_factory=lambda: os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36 HiddenJobsFinder/1.0",
    ))

    cache_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "cache.sqlite")
    results_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "results")


settings = Settings()
