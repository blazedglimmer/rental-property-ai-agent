"""User-initiated import of a single listing URL or pasted listing text."""
from __future__ import annotations

import hashlib

from ....config import get_settings
from ....schemas import ListingData
from ..extraction import extract, from_jsonld, html_to_text
from .base import FetchNotPermitted, RawListing, check_fetch_allowed, polite_get


def _render_with_playwright(url: str) -> str:  # pragma: no cover - needs browser
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(user_agent=get_settings().http_user_agent)
        page.goto(url, wait_until="networkidle", timeout=45000)
        html = page.content()
        b.close()
    return html


def import_url(url: str, city: str | None = None) -> list[RawListing]:
    """Fetch one page the user pointed us at. Raises FetchNotPermitted with a reason the UI shows."""
    r = polite_get(url, user_initiated=True)
    html = r.text
    text, jsonld, imgs = html_to_text(html)
    if len(text) < 400 and get_settings().playwright_enabled:
        check_fetch_allowed(url, user_initiated=True)
        text, jsonld, imgs = html_to_text(_render_with_playwright(url))
    listings: list[ListingData] = []
    j = from_jsonld(jsonld)
    llm_listings = extract(text, url, city)
    if llm_listings:
        listings = llm_listings[:1]  # detail page → one listing
        if j:
            for k, v in j.model_dump().items():
                if getattr(listings[0], k) in (None, [], "") and v not in (None, [], ""):
                    setattr(listings[0], k, v)
    elif j:
        listings = [j]
    for l in listings:
        l.source_url = url
        if not l.photos and imgs:
            l.photos = imgs
    return [RawListing(l, "url_import", url, hashlib.sha1(url.encode()).hexdigest()[:16],
                       raw={"len": len(text)}) for l in listings]


def import_text(text: str, city: str | None = None, url: str | None = None) -> list[RawListing]:
    out = []
    for l in extract(text, url, city):
        ext = hashlib.sha1(((url or "") + text[:500]).encode()).hexdigest()[:16]
        out.append(RawListing(l, "manual", url, ext, raw={"pasted": True}))
    return out


__all__ = ["import_url", "import_text", "FetchNotPermitted"]
