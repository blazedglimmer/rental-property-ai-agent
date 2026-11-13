"""Domain schemas shared by agents, API and LLM structured outputs."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Furnishing = Literal["furnished", "semi-furnished", "unfurnished"]
Tenant = Literal["family", "couple", "bachelor_male", "bachelor_female", "bachelor", "company"]


class SearchCriteria(BaseModel):
    city: str | None = None
    localities: list[str] = Field(default_factory=list)
    min_rent: int | None = None
    max_rent: int | None = None
    property_types: list[str] = Field(default_factory=list, description="e.g. 1RK, 1BHK, 2BHK")
    furnishing: list[Furnishing] = Field(default_factory=list)
    preferred_societies: list[str] = Field(default_factory=list)
    building_types: list[str] = Field(default_factory=list, description="gated society, standalone, independent house")
    min_bedrooms: int | None = None
    min_bathrooms: int | None = None
    parking: Literal["none", "two_wheeler", "car", "any"] | None = None
    amenities: list[str] = Field(default_factory=list)
    max_deposit: int | None = None
    brokerage: Literal["no_brokerage", "up_to_half_month", "up_to_one_month", "any"] = "any"
    tenant_type: Tenant | None = None
    move_in_by: date | None = None
    lease_months: int | None = 11
    other_requirements: list[str] = Field(default_factory=list)
    landmarks: list[str] = Field(default_factory=list, description="office/college the user commutes to")

    @field_validator("localities", "property_types", "amenities", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    def brokerage_cap(self) -> int | None:
        if self.max_rent is None:
            return None
        return {"no_brokerage": 0, "up_to_half_month": self.max_rent // 2,
                "up_to_one_month": self.max_rent}.get(self.brokerage)


class ParsedQuery(BaseModel):
    criteria: SearchCriteria
    assumptions: list[str] = Field(default_factory=list, description="defaults the parser filled in")
    unclear: list[str] = Field(default_factory=list, description="things the user should clarify")


class NegotiationBounds(BaseModel):
    target_rent: int | None = None
    max_rent: int | None = None
    max_deposit: int | None = None
    max_brokerage: int | None = None
    max_maintenance: int | None = None
    max_lock_in_months: int = 6
    max_notice_months: int = 1
    max_escalation_pct: float = 5.0
    flexibility: Literal["firm", "moderate", "flexible"] = "moderate"
    max_rounds: int = 3

    def filled(self, c: SearchCriteria) -> "NegotiationBounds":
        b = self.model_copy()
        if b.max_rent is None:
            b.max_rent = c.max_rent
        if b.target_rent is None and b.max_rent:
            b.target_rent = int(round(b.max_rent * 0.9 / 500) * 500)
        if b.max_deposit is None:
            b.max_deposit = c.max_deposit
        if b.max_brokerage is None:
            b.max_brokerage = c.brokerage_cap()
        return b


class ScoringWeights(BaseModel):
    budget: float = 25
    location: float = 20
    condition: float = 15
    furnishing: float = 10
    amenities: float = 10
    deposit_brokerage: float = 10
    connectivity: float = 10

    def normalised(self) -> dict[str, float]:
        d = self.model_dump()
        total = sum(v for v in d.values() if v > 0) or 1
        return {k: max(v, 0) * 100 / total for k, v in d.items()}


class ListingData(BaseModel):
    """Normalised listing produced by any discovery source or extractor."""
    title: str | None = None
    city: str | None = None
    locality: str | None = None
    address: str | None = None
    society: str | None = None
    property_type: str | None = None
    bhk: float | None = None
    bathrooms: int | None = None
    rent: int | None = None
    deposit: int | None = None
    maintenance: int | None = None
    brokerage: int | None = None
    brokerage_free: bool | None = None
    area_sqft: int | None = None
    furnishing: Furnishing | None = None
    parking: str | None = None
    floor: str | None = None
    amenities: list[str] = Field(default_factory=list)
    tenant_pref: list[str] = Field(default_factory=list)
    available_from: str | None = None
    description: str | None = None
    photos: list[str] = Field(default_factory=list)
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    contact_role: Literal["owner", "broker", "unknown"] | None = None
    source_url: str | None = None
    posted_at: str | None = None

    @field_validator("amenities", "tenant_pref", "photos", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


class ExtractedListings(BaseModel):
    listings: list[ListingData]


class ReplyFacts(BaseModel):
    """What an owner/broker reply tells us. Unknown → null, never guessed."""
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    rent: int | None = None
    deposit: int | None = None
    maintenance: int | None = None
    brokerage: int | None = None
    brokerage_free: bool | None = None
    lock_in_months: int | None = None
    notice_months: int | None = None
    escalation_pct: float | None = None
    available_from: str | None = None
    furnishing: Furnishing | None = None
    parking: str | None = None
    tenant_restrictions: list[str] = Field(default_factory=list)
    is_counter_offer: bool = False
    accepts_our_offer: bool = False
    rejects_negotiation: bool = False
    proposed_visit_times: list[str] = Field(default_factory=list, description="ISO 8601 datetimes if stated")
    confirms_visit: bool = False
    contact_role: Literal["owner", "broker", "unknown"] = "unknown"
    asks_for_advance_payment: bool = False
    media_urls: list[str] = Field(default_factory=list)
    summary: str = ""
