"""Role keyword expansion + matching (Hebrew and English)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Built-in synonym groups. If the user types any term of a group, all terms are searched.
SYNONYM_GROUPS: list[list[str]] = [
    ["SOC", "SOC Analyst", "אנליסט SOC", "Security Operations", "Tier 1", "Tier 2", "מוקד סייבר", "אנליסט סייבר", "Cyber Analyst"],
    ["NOC", "NOC Engineer", "מהנדס NOC", "Network Operations", "מוקדן NOC", "אנשי NOC"],
    ["Cyber", "סייבר", "Cyber Security", "Cybersecurity", "Information Security", "אבטחת מידע", "InfoSec"],
    ["Penetration Tester", "PT", "Pentester", "בודק חדירות", "Red Team", "Offensive Security"],
    ["DevOps", "דבאופס", "SRE", "Site Reliability", "Platform Engineer"],
    ["QA", "בודק תוכנה", "Quality Assurance", "Automation Engineer", "QA Automation", "בדיקות תוכנה"],
    ["Helpdesk", "Help Desk", "תמיכה טכנית", "Technical Support", "IT Support", "Desktop Support", "תומך מחשוב"],
    ["System Administrator", "Sysadmin", "מנהל רשת", "מנהל מערכות", "System Engineer", "IT Administrator", "איש סיסטם"],
    ["Network Engineer", "מהנדס רשתות", "Network Administrator", "רשתות תקשורת"],
    ["Data Analyst", "אנליסט נתונים", "BI Analyst", "BI Developer", "Data Analytics"],
    ["Software Engineer", "Software Developer", "מפתח תוכנה", "מתכנת", "Backend Developer", "Full Stack", "Fullstack", "Frontend Developer"],
    ["Data Center", "דאטה סנטר", "Data Center Technician", "טכנאי חוות שרתים", "חוות שרתים"],
    ["Junior", "ג'וניור", "Entry Level", "סטודנט", "Student", "משרת סטודנט", "Intern", "Internship"],
    ["GRC", "Compliance", "רגולציה", "Risk", "ניהול סיכונים", "Governance"],
    ["Incident Response", "IR", "DFIR", "Forensics", "תגובה לאירועים", "פורנזיקה"],
]

# Very short Latin terms (e.g. "IR", "PT") cause false positives — only match them as whole words
_LATIN = re.compile(r"[A-Za-z]")


@dataclass
class RoleQuery:
    original: str
    terms: list[str]


def expand_roles(roles: list[str], use_synonyms: bool = True) -> list[RoleQuery]:
    out: list[RoleQuery] = []
    for role in roles:
        role = role.strip()
        if not role:
            continue
        terms = {role}
        if use_synonyms:
            low = role.lower()
            for group in SYNONYM_GROUPS:
                if any(low == t.lower() or t.lower() in low or (len(low) > 3 and low in t.lower()) for t in group):
                    terms.update(group)
        # ambiguous ultra-short tokens are dropped unless user typed them explicitly
        terms = {t for t in terms if len(t) > 2 or t == role}
        out.append(RoleQuery(original=role, terms=sorted(terms, key=len, reverse=True)))
    return out


def _term_regex(term: str) -> re.Pattern:
    escaped = re.escape(term).replace(r"\ ", r"[\s\-_]*")
    if _LATIN.search(term):
        # word boundaries for latin text so "SOC" does not match "social"
        return re.compile(rf"(?<![A-Za-z]){escaped}(?![A-Za-z])", re.IGNORECASE)
    # Hebrew: allow prefixes like ה/ו/ל/ב before the word
    return re.compile(rf"(?:^|[^֐-׿]|[הולבמש]){escaped}", re.IGNORECASE)


class RoleMatcher:
    def __init__(self, queries: list[RoleQuery]):
        self.queries = queries
        self._compiled = [(q, [(t, _term_regex(t)) for t in q.terms]) for q in queries]

    def match(self, text: str) -> tuple[str, str] | None:
        """Return (original role, matched term) if text matches any role."""
        if not text:
            return None
        for q, patterns in self._compiled:
            for term, rx in patterns:
                if rx.search(text):
                    return q.original, term
        return None

    def all_terms(self) -> list[str]:
        return [t for q in self.queries for t in q.terms]
