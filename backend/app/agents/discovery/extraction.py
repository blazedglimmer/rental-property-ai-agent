"""Turn unstructured listing text / HTML into ListingData."""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ... import textparse as tp
from ...schemas import ExtractedListings, ListingData
from ...services.llm import LLMUnavailable, get_llm

SYSTEM = """You extract rental property listings (India) from text into structured data.
Rules:
- Extract only what the text states. Unknown → null. Never guess phone numbers, rent or deposit.
- Amounts are INR integers (₹18k → 18000; "2 months deposit" with rent 18000 → 36000).
- property_type like "1RK", "1BHK", "2BHK"; bhk numeric (1RK = 0.5).
- furnishing must be furnished | semi-furnished | unfurnished.
- contact_role "owner" if posted by owner / "no brokerage"; "broker" if agent/dealer/brokerage mentioned.
- tenant_pref values: family, bachelor, couple, company.
- If the text contains several listings, return each separately. If none, return an empty list."""


def html_to_text(html: str) -> tuple[str, list[dict], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    jsonld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(s.string or "")
            jsonld += d if isinstance(d, list) else [d]
        except (json.JSONDecodeError, TypeError):
            pass
    imgs = [m.get("content") for m in soup.find_all("meta", property="og:image") if m.get("content")]
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        t.decompose()
    text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n")).strip()
    return text, jsonld, imgs


def extract(text: str, source_url: str | None = None, city: str | None = None,
            max_chars: int = 60000) -> list[ListingData]:
    llm = get_llm()
    if llm.available:
        try:
            out = llm.structured(SYSTEM, f"City context: {city or 'unknown'}\nSource: {source_url or 'n/a'}\n\n"
                                         f"<listing_text>\n{text[:max_chars]}\n</listing_text>", ExtractedListings)
            for l in out.listings:
                l.source_url = l.source_url or source_url
                _fill_from_rules(l, text if len(out.listings) == 1 else "")
            return [l for l in out.listings if l.rent or l.title or l.locality]
        except LLMUnavailable:
            pass
    l = heuristic_extract(text, source_url, city)
    return [l] if (l.rent or l.property_type) else []


def _fill_from_rules(l: ListingData, text: str) -> None:
    if not text:
        return
    l.contact_phone = l.contact_phone or tp.phone(text)
    l.area_sqft = l.area_sqft or tp.area(text)


def heuristic_extract(text: str, source_url: str | None = None, city: str | None = None) -> ListingData:
    ptype, n = tp.bhk(text)
    rent = tp.amount_near(text, ["rent", "rental", "per month", "/month", "pm", "monthly"])
    if rent is None:
        am = [a for a in tp.amounts(text) if 3000 <= a <= 500000]
        rent = am[0] if am else None
    deposit = tp.amount_near(text, ["deposit", "security"])
    m = re.search(r"(\d{1,2})\s*months?\s*(?:rent\s*)?(?:as\s*)?(?:security\s*)?deposit", text, re.I)
    if m and rent:
        deposit = int(m.group(1)) * rent
    low = text.lower()
    role = "owner" if re.search(r"\b(owner|no brokerage|zero brokerage|brokerage free)\b", low) else (
        "broker" if re.search(r"\b(broker|agent|realty|properties|consultant|brokerage)\b", low) else None)
    loc = None
    lm = re.search(r"\b(?:in|at)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)\s*,\s*([A-Z][a-z]+)", text)
    if lm:
        loc = lm.group(1)
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:200]
    return ListingData(
        title=first_line or None, city=city, locality=loc, property_type=ptype, bhk=n, rent=rent,
        deposit=deposit, maintenance=tp.amount_near(text, ["maintenance"]),
        brokerage_free=True if re.search(r"no brokerage|zero brokerage|brokerage free", low) else None,
        area_sqft=tp.area(text), furnishing=tp.furnishing(text), amenities=tp.amenities(text),
        tenant_pref=tp.tenant_prefs(text), description=text[:3000], contact_phone=tp.phone(text),
        contact_email=tp.email(text), contact_role=role, source_url=source_url,
        parking="available" if "parking" in low else None)


def from_jsonld(items: list[dict]) -> ListingData | None:
    """schema.org Apartment/Residence/Offer → ListingData (when publishers provide it)."""
    for it in items:
        t = it.get("@type")
        types = t if isinstance(t, list) else [t]
        if not any(x in ("Apartment", "House", "SingleFamilyResidence", "Residence", "Offer",
                         "RealEstateListing", "Product", "Accommodation") for x in types):
            continue
        offer = it.get("offers") or (it if "Offer" in types else {})
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        addr = it.get("address") or {}
        price = offer.get("price") if isinstance(offer, dict) else None
        name = it.get("name") or ""
        ptype, n = tp.bhk(name + " " + str(it.get("description", "")))
        imgs = it.get("image") or []
        return ListingData(
            title=name or None, description=it.get("description"),
            locality=addr.get("addressLocality") if isinstance(addr, dict) else None,
            city=addr.get("addressRegion") if isinstance(addr, dict) else None,
            address=addr.get("streetAddress") if isinstance(addr, dict) else None,
            rent=int(float(price)) if price else None, property_type=ptype, bhk=n,
            photos=imgs if isinstance(imgs, list) else [imgs])
    return None
