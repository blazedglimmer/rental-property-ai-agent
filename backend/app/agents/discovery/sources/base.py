from __future__ import annotations

import logging
import urllib.robotparser
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse

import httpx

from ....config import get_settings
from ....schemas import ListingData, SearchCriteria

log = logging.getLogger(__name__)


@dataclass
class RawListing:
    data: ListingData
    source: str
    url: str | None = None
    external_id: str | None = None
    raw: dict = field(default_factory=dict)
    is_sample: bool = False


class SourceError(RuntimeError):
    pass


class Source:
    name: str = "base"
    per_locality: bool = True  # False → run once per search run, not per locality
    compliance: str = ""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def available(self) -> tuple[bool, str]:
        return True, ""

    def search(self, criteria: SearchCriteria, locality: str | None) -> list[RawListing]:
        raise NotImplementedError


# ---------- fetch policy: robots.txt + operator domain allowlist ----------

class FetchNotPermitted(SourceError):
    pass


@lru_cache(maxsize=256)
def _robots(origin: str) -> urllib.robotparser.RobotFileParser | None:
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = httpx.get(origin + "/robots.txt", timeout=10, follow_redirects=True,
                      headers={"User-Agent": get_settings().http_user_agent})
    except httpx.HTTPError:
        return None
    if r.status_code in (401, 403):
        rp.disallow_all = True
    elif r.status_code >= 400:
        rp.allow_all = True
    else:
        rp.parse(r.text.splitlines())
    return rp


def check_fetch_allowed(url: str, user_initiated: bool = False) -> None:
    """Raise FetchNotPermitted unless the URL may be fetched.

    Rules: robots.txt must allow our user agent. Automated (non-user-initiated) fetches also
    require the domain to be on the operator's allowlist (set after reviewing its terms).
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise FetchNotPermitted("only http(s) URLs")
    host = p.hostname or ""
    allowed = get_settings().allowed_domains
    on_list = any(host == d or host.endswith("." + d) for d in allowed)
    if not user_initiated and not on_list:
        raise FetchNotPermitted(f"{host} is not on the fetch allowlist (FETCH_ALLOWED_DOMAINS)")
    rp = _robots(f"{p.scheme}://{p.netloc}")
    if rp is None:
        raise FetchNotPermitted(f"could not read robots.txt for {host}")
    if not rp.can_fetch(get_settings().http_user_agent, url):
        raise FetchNotPermitted(f"robots.txt for {host} disallows this path")


def polite_get(url: str, user_initiated: bool = False) -> httpx.Response:
    check_fetch_allowed(url, user_initiated)
    r = httpx.get(url, timeout=20, follow_redirects=True,
                  headers={"User-Agent": get_settings().http_user_agent, "Accept-Language": "en-IN,en"})
    if r.status_code in (401, 403, 429):
        raise FetchNotPermitted(f"{urlparse(url).hostname} refused automated access ({r.status_code}); "
                                "paste the listing text instead")
    r.raise_for_status()
    return r
