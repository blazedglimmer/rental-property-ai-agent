"""Discovery via Claude's server-side web search tool.

Searches run on Anthropic's infrastructure through an official API; we receive titles,
snippets and URLs. We do not fetch portal pages unless the domain is allowlisted and
robots.txt permits it. Leads without contact details are still useful: they are ranked
and the user can open them or paste details.
"""
from __future__ import annotations

import hashlib

from ....config import get_settings
from ....schemas import ListingData, SearchCriteria
from ....services.llm import LLMUnavailable, get_llm
from .base import RawListing, Source, SourceError

SYSTEM = """You are the discovery component of a tenant's rental assistant in India.
Use web search to find CURRENT rental listings (posted in the last ~30 days where dates are visible)
that match the criteria. Search property portals, classifieds and broker sites.
Report only listings you actually saw in search results — never invent listings, prices or phone numbers.
For each listing return an object with keys:
title, url, locality, city, society, property_type, bhk, rent, deposit, maintenance, furnishing,
area_sqft, contact_role ("owner"|"broker"|"unknown"), posted ("as shown" or null), snippet (≤300 chars, verbatim).
Use null for anything not shown. Finish with ONE fenced ```json array and nothing after it."""


class WebSearchSource(Source):
    name = "web_search"
    per_locality = True
    compliance = "Anthropic web search API; result pages are not scraped."

    def available(self):
        if not get_settings().enable_web_search_discovery:
            return False, "disabled in settings"
        if not get_llm().available:
            return False, "needs ANTHROPIC_API_KEY"
        return True, ""

    def search(self, c: SearchCriteria, locality: str | None) -> list[RawListing]:
        ok, why = self.available()
        if not ok:
            raise SourceError(why)
        where = ", ".join(filter(None, [locality, c.city]))
        want = [
            f"{'/'.join(c.property_types) or 'flat'} for rent in {where}",
            f"budget ₹{c.min_rent or 0:,}–₹{c.max_rent:,}/month" if c.max_rent else "",
            f"furnishing: {', '.join(c.furnishing)}" if c.furnishing else "",
            f"tenant: {c.tenant_type}" if c.tenant_type else "",
            "owner listings / no brokerage preferred" if c.brokerage == "no_brokerage" else "",
            f"must have: {', '.join(c.amenities)}" if c.amenities else "",
        ]
        prompt = "Find rental listings for:\n- " + "\n- ".join(w for w in want if w) + \
                 "\nReturn up to 15 distinct listings."
        try:
            items, cited = get_llm().web_search_json(SYSTEM, prompt, max_searches=self.config.get("max_searches", 4))
        except LLMUnavailable as e:
            raise SourceError(str(e)) from e
        cited_set = set(cited)
        out = []
        for it in items:
            url = it.get("url")
            # Guard against hallucinated URLs: must appear in actual search results.
            if not url or (cited_set and url not in cited_set):
                continue
            try:
                ld = ListingData(
                    title=it.get("title"), locality=it.get("locality") or locality, city=it.get("city") or c.city,
                    society=it.get("society"), property_type=(it.get("property_type") or "").upper().replace(" ", "") or None,
                    bhk=_f(it.get("bhk")), rent=_i(it.get("rent")), deposit=_i(it.get("deposit")),
                    maintenance=_i(it.get("maintenance")), area_sqft=_i(it.get("area_sqft")),
                    furnishing=it.get("furnishing") if it.get("furnishing") in ("furnished", "semi-furnished", "unfurnished") else None,
                    contact_role=it.get("contact_role") if it.get("contact_role") in ("owner", "broker") else None,
                    description=it.get("snippet"), source_url=url)
            except Exception:
                continue
            out.append(RawListing(ld, self.name, url, hashlib.sha1(url.encode()).hexdigest()[:16], raw=it))
        return out


def _i(v):
    try:
        return int(float(str(v).replace(",", ""))) if v not in (None, "", "null") else None
    except ValueError:
        return None


def _f(v):
    try:
        return float(v) if v not in (None, "", "null") else None
    except (ValueError, TypeError):
        return None
