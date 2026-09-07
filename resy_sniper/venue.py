"""Resolve the numeric venue_id and read Resy's own statement of the booking window."""

from __future__ import annotations

import logging
from typing import Optional

from .client import ApiError, ResyClient, ResyError, extract_venue_id


class VenueInfo:
    def __init__(self, venue_id: int, name: str = "", lead_time_in_days: Optional[int] = None, time_zone: str = ""):
        self.venue_id = venue_id
        self.name = name
        self.lead_time_in_days = lead_time_in_days
        self.time_zone = time_zone

    def __str__(self) -> str:
        return (
            f"venue_id={self.venue_id} name={self.name!r} lead_time_in_days={self.lead_time_in_days} "
            f"time_zone={self.time_zone or '?'}"
        )


def _lead_time(obj: dict) -> Optional[int]:
    v = obj.get("lead_time_in_days")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def resolve_venue(client: ResyClient, url_slug: str, location: str, configured_id: Optional[int], log: logging.Logger) -> VenueInfo:
    """Look the venue up by slug (GET /3/venue), falling back to venue search; then read /2/config."""
    info: Optional[VenueInfo] = None
    if configured_id:
        info = VenueInfo(configured_id)
        log.info("using configured venue_id=%s (skipping slug lookup)", configured_id)
    else:
        try:
            v = client.get_venue_by_slug(url_slug, location)
            vid = extract_venue_id(v)
            if vid:
                info = VenueInfo(
                    vid,
                    name=str(v.get("name") or ""),
                    lead_time_in_days=_lead_time(v),
                    time_zone=str(((v.get("locale") or {}).get("time_zone")) or ((v.get("location") or {}).get("time_zone")) or ""),
                )
                log.info("/3/venue resolved: %s", info)
            else:
                log.warning("/3/venue returned no recognizable id; keys=%s", sorted(v.keys()) if isinstance(v, dict) else type(v))
        except ApiError as e:
            log.warning("/3/venue lookup failed (%s); falling back to venue search", e)
        if info is None:
            query = url_slug.replace("-", " ")
            hits = client.search_venues(query)
            for h in hits:
                if str(h.get("url_slug") or "").lower() == url_slug.lower():
                    vid = extract_venue_id(h)
                    if vid:
                        info = VenueInfo(vid, name=str(h.get("name") or ""), lead_time_in_days=_lead_time(h))
                        log.info("/3/venuesearch/search resolved: %s", info)
                        break
            if info is None:
                names = [(h.get("name"), h.get("url_slug"), extract_venue_id(h)) for h in hits]
                raise ResyError(f"could not resolve venue_id for slug {url_slug!r}; search hits: {names}")

    # /2/config states lead_time_in_days directly. Informational; failures are non-fatal.
    try:
        cfg = client.get_venue_config(info.venue_id)
        lt = _lead_time(cfg)
        if lt is not None:
            if info.lead_time_in_days is not None and info.lead_time_in_days != lt:
                log.warning("lead_time_in_days differs: /3/venue=%s /2/config=%s (using /2/config)", info.lead_time_in_days, lt)
            info.lead_time_in_days = lt
        vname = ((cfg.get("venue") or {}).get("name")) if isinstance(cfg, dict) else None
        if vname and not info.name:
            info.name = str(vname)
        log.info("/2/config: lead_time_in_days=%s venue=%s", lt, vname)
    except ResyError as e:
        log.warning("/2/config lookup failed (non-fatal): %s", e)
    return info
