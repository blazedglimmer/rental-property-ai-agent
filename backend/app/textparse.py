"""Deterministic text extraction helpers (Indian rental vocabulary).

Used as the no-LLM fallback and as a cross-check on LLM output (numbers in outgoing
messages are validated with these).
"""
from __future__ import annotations

import re

_NUM = r"(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)"
_AMOUNT_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupees)?\s*" + _NUM + r"\s*(k|K|thousand|lakh|lac|lakhs|L)?\b(?!\s*(?:sq|bhk|rk|days?|months?|yrs?|years?|%|am|pm))",
    re.I)


def parse_amount(token: str, unit: str | None = None) -> int | None:
    try:
        v = float(token.replace(",", ""))
    except ValueError:
        return None
    u = (unit or "").lower()
    if u in {"k", "thousand"}:
        v *= 1000
    elif u in {"lakh", "lac", "lakhs", "l"}:
        v *= 100000
    return int(round(v))


def amounts(text: str) -> list[int]:
    """All rupee-looking amounts ≥ 500 in text."""
    out = []
    for m in _AMOUNT_RE.finditer(text):
        v = parse_amount(m.group(1), m.group(2))
        if v is not None and v >= 500:
            out.append(v)
    return out


def amount_near(text: str, keywords: list[str], window: int = 40) -> int | None:
    low = text.lower()
    best = None
    for kw in keywords:
        for km in re.finditer(re.escape(kw), low):
            seg = text[km.end(): km.end() + window]
            m = _AMOUNT_RE.search(seg)
            if m:
                v = parse_amount(m.group(1), m.group(2))
                if v and v >= 500:
                    return v
            # amount before keyword, e.g. "20k rent"
            seg_b = text[max(0, km.start() - 20): km.start()]
            mb = list(_AMOUNT_RE.finditer(seg_b))
            if mb and best is None:
                v = parse_amount(mb[-1].group(1), mb[-1].group(2))
                if v and v >= 500:
                    best = v
    return best


def months_near(text: str, keywords: list[str]) -> int | None:
    low = text.lower()
    for kw in keywords:
        m = re.search(re.escape(kw) + r"[^\d]{0,25}(\d{1,2})\s*(?:months?|mo)", low)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d{1,2})\s*(?:months?|mo)[^\d]{0,15}" + re.escape(kw), low)
        if m:
            return int(m.group(1))
    return None


def bhk(text: str) -> tuple[str | None, float | None]:
    low = text.lower()
    if re.search(r"\b1\s*rk\b", low):
        return "1RK", 0.5
    m = re.search(r"\b(\d(?:\.5)?)\s*-?\s*bhk\b", low)
    if m:
        n = float(m.group(1))
        return f"{m.group(1)}BHK", n
    m = re.search(r"\b(one|two|three|four)\s*(?:bed|bhk)", low)
    if m:
        n = {"one": 1, "two": 2, "three": 3, "four": 4}[m.group(1)]
        return f"{n}BHK", float(n)
    return None, None


def furnishing(text: str) -> str | None:
    low = text.lower()
    if re.search(r"semi[\s-]*furnished", low):
        return "semi-furnished"
    if re.search(r"\b(un[\s-]*furnished|unfurnished|bare shell)\b", low):
        return "unfurnished"
    if re.search(r"\b(fully[\s-]*)?furnished\b", low):
        return "furnished"
    return None


def phone(text: str) -> str | None:
    m = re.search(r"(?:\+?91[\s-]?)?([6-9]\d{4}[\s-]?\d{5})\b", text)
    return ("+91" + re.sub(r"\D", "", m.group(1))) if m else None


def email(text: str) -> str | None:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    return m.group(0) if m else None


def area(text: str) -> int | None:
    m = re.search(r"(\d{3,5})\s*(?:sq\.?\s*ft|sqft|sq\. ft|square feet|sft)", text, re.I)
    return int(m.group(1)) if m else None


AMENITY_WORDS = {
    "lift": ["lift", "elevator"], "power backup": ["power backup", "generator", "dg backup"],
    "security": ["security", "cctv", "guard", "gated"], "gym": ["gym"], "swimming pool": ["pool"],
    "clubhouse": ["club house", "clubhouse"], "garden": ["garden", " park ", "park area", "landscaped"],
    "parking": ["parking"], "wifi": ["wifi", "wi-fi"], "ac": ["air condition", " ac ", "a/c"],
    "washing machine": ["washing machine"], "fridge": ["fridge", "refrigerator"],
    "modular kitchen": ["modular kitchen"], "kitchen": ["kitchen"], "balcony": ["balcony"],
    "ventilation": ["ventilation", "well ventilated", "airy", "cross ventilation"],
    "water supply 24x7": ["24x7 water", "24 hours water", "24/7 water"], "gas pipeline": ["gas pipeline", "piped gas", "png"],
    "pet friendly": ["pet friendly", "pets allowed"],
}


def amenities(text: str) -> list[str]:
    low = f" {text.lower()} "
    return sorted({k for k, words in AMENITY_WORDS.items() if any(w in low for w in words)})


def tenant_prefs(text: str) -> list[str]:
    low = text.lower()
    out = []
    if "family" in low:
        out.append("family")
    if "bachelor" in low:
        out.append("bachelor")
    if "couple" in low:
        out.append("couple")
    if "company lease" in low or "corporate" in low:
        out.append("company")
    return out
