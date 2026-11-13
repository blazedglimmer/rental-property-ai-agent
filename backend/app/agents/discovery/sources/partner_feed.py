"""Licensed inventory feeds (JSON / CSV) from brokers, societies or listing partners.

config: {"url": "...", "format": "json"|"csv", "name": "Acme Realty", "mapping": {"rent": "price", ...},
         "sample": false}
`url` may be an http(s) URL or a local path (file under the backend directory).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import httpx

from ....config import BASE_DIR, get_settings
from ....schemas import ListingData, SearchCriteria
from .. import extraction
from .base import RawListing, Source, SourceError

FIELDS = set(ListingData.model_fields)


class PartnerFeedSource(Source):
    name = "partner_feed"
    per_locality = False
    compliance = "Feeds provided under agreement with the inventory owner."

    def available(self):
        return (bool(self.config.get("url")), "feed url missing")

    def _load(self) -> list[dict]:
        url = self.config["url"]
        if url.startswith("http"):
            r = httpx.get(url, timeout=30, headers={"User-Agent": get_settings().http_user_agent},
                          follow_redirects=True)
            r.raise_for_status()
            body = r.text
        else:
            p = (BASE_DIR / url).resolve()
            if BASE_DIR not in p.parents:
                raise SourceError("feed path must be inside the backend directory")
            body = Path(p).read_text()
        if self.config.get("format", "json") == "csv":
            return list(csv.DictReader(io.StringIO(body)))
        data = json.loads(body)
        return data.get("listings", data) if isinstance(data, dict) else data

    def search(self, c: SearchCriteria, locality: str | None) -> list[RawListing]:
        try:
            rows = self._load()
        except (httpx.HTTPError, OSError, json.JSONDecodeError) as e:
            raise SourceError(f"feed load failed: {e}") from e
        mapping = self.config.get("mapping", {})
        sample = bool(self.config.get("sample"))
        out = []
        for i, row in enumerate(rows):
            d = {k: row.get(v) for k, v in mapping.items()} | {k: v for k, v in row.items() if k in FIELDS}
            for k in ("rent", "deposit", "maintenance", "brokerage", "area_sqft", "bathrooms"):
                if d.get(k) not in (None, ""):
                    try:
                        d[k] = int(float(str(d[k]).replace(",", "")))
                    except ValueError:
                        d[k] = None
            for k in ("amenities", "tenant_pref", "photos"):
                if isinstance(d.get(k), str):
                    d[k] = [x.strip() for x in d[k].split("|" if "|" in d[k] else ",") if x.strip()]
            try:
                ld = ListingData(**{k: v for k, v in d.items() if k in FIELDS and v not in ("",)})
            except Exception:
                ld = extraction.heuristic_extract(json.dumps(row), None, c.city)
            if c.city and ld.city and ld.city.lower() != c.city.lower():
                continue
            ext = str(row.get("id") or row.get("external_id") or i)
            out.append(RawListing(ld, f"feed:{self.config.get('name', 'partner')}", ld.source_url, ext,
                                  raw=row, is_sample=sample))
        return out
