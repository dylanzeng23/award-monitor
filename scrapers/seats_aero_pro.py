"""
seats.aero Pro API scraper.
Covers all programs (Delta, AA, Alaska, Avios, etc.) beyond just CX.
"""
import logging
from datetime import date, timedelta

import httpx

from models import AwardResult, SearchRoute

logger = logging.getLogger(__name__)

API_URL = "https://seats.aero/partnerapi/search"
API_KEY = ""  # Set from config

# Only alert on programs user has points in
PROGRAMS_OF_INTEREST = {"american", "delta", "alaska", "avios", "finnair", "aeromexico", "united"}

# Exclude operators
EXCLUDE_OPERATORS = {"VN", "EY", "EK", "QR", "TK"}

CABIN_MAP = {"business": "J", "economy": "Y", "premium": "W", "first": "F"}

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

PROGRAM_LABELS = {
    "american": "AA", "delta": "DL", "alaska": "AS", "avios": "BA/Avios",
    "finnair": "AY", "aeromexico": "AM", "united": "UA", "aeroplan": "AC",
    "qantas": "QF", "qatar": "QR", "virginatlantic": "VS",
}


class SeatsAeroProScraper:
    name = "seats_aero_pro"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def start(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Partner-Authorization": self.api_key,
                "Accept": "application/json",
            },
        )

    async def stop(self):
        if self._client:
            await self._client.aclose()

    async def search_route(self, route: SearchRoute) -> list[AwardResult]:
        start = route.date_range[0].isoformat()
        end = route.date_range[1].isoformat()

        params = {
            "origin_airport": route.origin,
            "destination_airport": route.destination,
            "start_date": start,
            "end_date": end,
        }

        try:
            resp = await self._client.get(API_URL, params=params)
            if resp.status_code != 200:
                logger.warning(f"seats.aero API {resp.status_code}: {resp.text[:200]}")
                return []
            data = resp.json()
            return self._parse_results(data, route)
        except Exception as e:
            logger.error(f"seats.aero API error: {e}")
            return []

    def _parse_results(self, data: dict, route: SearchRoute) -> list[AwardResult]:
        results = []
        target = route.cabin.lower()
        avail_key = CABIN_MAP.get(target, "J") + "Available"
        miles_key = CABIN_MAP.get(target, "J") + "MileageCost"
        seats_key = CABIN_MAP.get(target, "J") + "RemainingSeats"

        for entry in data.get("data", []):
            if not entry.get(avail_key):
                continue

            source = entry.get("Route", {}).get("Source", "")
            miles = int(entry.get(miles_key, 0) or 0)

            # Apply filters: max 100K, only programs of interest
            if miles == 0 or miles > 100000:
                continue
            if source not in PROGRAMS_OF_INTEREST:
                continue

            flight_date = date.fromisoformat(entry["Date"])
            seats = entry.get(seats_key, 0)
            origin = entry.get("Route", {}).get("OriginAirport", route.origin)
            dest = entry.get("Route", {}).get("DestinationAirport", route.destination)
            label = PROGRAM_LABELS.get(source, source)
            weekday = WEEKDAYS[flight_date.weekday()]

            results.append(AwardResult(
                scraper=f"sa:{source}",
                origin=origin,
                destination=dest,
                flight_date=flight_date,
                flight_number=label,
                cabin=target.title(),
                miles_cost=miles,
                stops=0,
                operating_carrier=label,
                departure_time=f"{weekday} | {seats} seat{'s' if seats != 1 else ''}",
                arrival_time=f"{label} {miles:,}mi",
            ))

        return results

    async def search(self, route: SearchRoute, search_date: date) -> list[AwardResult]:
        single = SearchRoute(
            origin=route.origin, destination=route.destination,
            cabin=route.cabin,
            date_range=(search_date, search_date),
            programs=route.programs,
        )
        return await self.search_route(single)
