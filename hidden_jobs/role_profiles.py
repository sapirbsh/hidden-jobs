"""Role-aware Google Maps targeting.

Instead of scanning every business in the city, each role family maps to the kinds of
places that actually employ it. A SOC analyst search scans cyber/IT/telecom/finance
companies — not kiosks, restaurants or hair salons.
"""
from __future__ import annotations

from dataclasses import dataclass

from .matcher import RoleQuery, _term_regex


@dataclass
class Profile:
    name: str
    triggers: list[str]          # if any role term contains one of these → profile applies
    categories: list[str]        # Google Maps text queries
    office_only: bool = True     # drop shops/food/leisure places by their Google type


CYBER = Profile(
    "סייבר / SOC",
    ["soc", "cyber", "סייבר", "security", "אבטחת מידע", "infosec", "pentest", "penetration", "בודק חדירות",
     "red team", "incident response", "dfir", "forensic", "פורנזיקה", "grc", "compliance", "tier 1", "tier 2"],
    ["חברת סייבר", "cyber security company", "אבטחת מידע", "information security", "managed security services",
     "חברת הייטק", "software company", "technology company", "IT services", "managed service provider",
     "data center", "telecommunications company", "בנק", "חברת ביטוח", "insurance company",
     "defense company", "תעשייה ביטחונית", "headquarters"],
)
NETWORK_IT = Profile(
    "NOC / IT / רשתות",
    ["noc", "network", "רשת", "רשתות", "sysadmin", "system administrator", "מנהל מערכות", "system engineer",
     "helpdesk", "help desk", "תמיכה טכנית", "it support", "desktop support", "תומך מחשוב", "data center",
     "דאטה סנטר", "חוות שרתים", "איש סיסטם", "it administrator"],
    ["IT services", "שירותי מחשוב", "computer support", "network services", "telecommunications company",
     "חברת תקשורת", "internet service provider", "data center", "cloud hosting", "managed service provider",
     "חברת הייטק", "software company", "technology company", "cyber security company", "בנק", "headquarters"],
)
SOFTWARE = Profile(
    "פיתוח / DevOps / QA / Data",
    ["software", "developer", "מפתח", "מתכנת", "backend", "frontend", "full stack", "fullstack", "devops",
     "דבאופס", "sre", "platform engineer", "qa", "בודק תוכנה", "automation", "data analyst", "אנליסט נתונים",
     "bi analyst", "bi developer", "data engineer", "data scientist"],
    ["software company", "חברת תוכנה", "חברת הייטק", "startup", "technology company", "מרכז פיתוח",
     "R&D center", "fintech company", "cyber security company", "IT consulting", "headquarters"],
)
PROFILES = [CYBER, NETWORK_IT, SOFTWARE]

# Generic fallback when no profile matches (e.g. "מנהלת חשבונות", "לוגיסטיקה")
GENERIC = ["חברה", "משרדים", "company", "corporate office", "headquarters"]

# Google place types that are never an office employer for tech roles
EXCLUDED_TYPES = {
    "restaurant", "cafe", "bar", "bakery", "meal_takeaway", "meal_delivery", "fast_food_restaurant",
    "coffee_shop", "ice_cream_shop", "food_store", "convenience_store", "grocery_store", "supermarket",
    "liquor_store", "kiosk", "clothing_store", "shoe_store", "jewelry_store", "gift_shop", "florist",
    "beauty_salon", "hair_care", "hair_salon", "nail_salon", "barber_shop", "spa", "gym", "fitness_center",
    "car_repair", "car_wash", "car_dealer", "gas_station", "lodging", "hotel", "pet_store", "laundry",
    "furniture_store", "home_goods_store", "book_store", "toy_store", "pharmacy", "drugstore",
    "dentist", "doctor", "physiotherapist", "veterinary_care", "school", "preschool", "place_of_worship",
    "park", "tourist_attraction", "night_club", "movie_theater", "shopping_mall", "department_store",
}
# types that rescue a place even if it also carries an excluded type (e.g. a company HQ inside a mall)
KEEP_TYPES = {"corporate_office", "bank", "insurance_agency", "telecommunications_service_provider",
              "internet_service_provider", "consultant", "accounting", "hospital", "university"}


@dataclass
class Targeting:
    categories: list[str]
    office_only: bool
    profiles: list[str]


def targeting_for_roles(roles: list[RoleQuery], extra: list[str] | None = None,
                        only_extra: bool = False) -> Targeting:
    extra = extra or []
    if only_extra and extra:
        hits = _matching_profiles(roles)
        return Targeting(list(dict.fromkeys(extra)), any(p.office_only for p in hits), [p.name for p in hits])
    hits = _matching_profiles(roles)
    cats: list[str] = []
    for p in hits:
        cats += p.categories
    unmatched = [r for r in roles if not any(_role_hits(r, p) for p in PROFILES)]
    if unmatched:
        # unknown role: search the role itself on Maps + generic office queries
        cats += [r.original for r in unmatched] + GENERIC
    cats += extra
    office_only = bool(hits) and not unmatched
    return Targeting(list(dict.fromkeys(cats)), office_only, [p.name for p in hits])


def is_excluded_type(types: list[str]) -> bool:
    t = set(types or [])
    return bool(t & EXCLUDED_TYPES) and not (t & KEEP_TYPES)


def _role_hits(role: RoleQuery, p: Profile) -> bool:
    text = " | ".join([role.original] + role.terms)
    return any(_term_regex(trig.strip()).search(text) for trig in p.triggers)


def _matching_profiles(roles: list[RoleQuery]) -> list[Profile]:
    return [p for p in PROFILES if any(_role_hits(r, p) for r in roles)]
