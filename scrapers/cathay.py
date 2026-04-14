"""
Cathay Pacific award availability via public API.
No authentication required.

API: https://api.cathaypacific.com/afr/search/availability/zh.{origin}.{dest}.{cabin}.CX.1.{start}.{end}.json

Availability values: "H" = high, "L" = low, "NA" = none
"""
import logging
from datetime import date, timedelta

import httpx

from models import AwardResult, SearchRoute

logger = logging.getLogger(__name__)

API_URL = "https://api.cathaypacific.com/afr/search/availability/zh.{origin}.{dest}.{cabin}.CX.1.{start}.{end}.json"

CABIN_MAP = {
    "economy": "eco",
    "premium": "pey",
    "business": "bus",
    "first": "fir",
}


class CathayScraper:
    name = "cathay"

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def start(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Accept": "*/*",
                "Origin": "https://www.cathaypacific.com",
                "Referer": "https://www.cathaypacific.com/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.2 Safari/605.1.15",
            },
        )

    async def stop(self):
        if self._client:
            await self._client.aclose()

    async def search_route(self, route: SearchRoute) -> list[AwardResult]:
        """Search full date range in one API call (the API supports date ranges)."""
        cabin_code = CABIN_MAP.get(route.cabin.lower(), "bus")
        start = route.date_range[0].strftime("%Y%m%d")
        end = route.date_range[1].strftime("%Y%m%d")

        url = API_URL.format(
            origin=route.origin,
            dest=route.destination,
            cabin=cabin_code,
            start=start,
            end=end,
        )

        logger.info(f"[cathay] Searching {route.origin}-{route.destination} {cabin_code} {start}-{end}")

        try:
            resp = await self._client.get(url)
            if resp.status_code != 200:
                logger.warning(f"Cathay API {resp.status_code}: {resp.text[:200]}")
                return []

            data = resp.json()
            return self._parse_response(data, route)

        except Exception as e:
            logger.error(f"Cathay API error: {e}")
            return []

    def _parse_response(self, data: dict, route: SearchRoute) -> list[AwardResult]:
        results = []
        availabilities = data.get("availabilities", {})
        update_time = availabilities.get("updateTime", "")

        for entry in availabilities.get("std", []):
            avail = entry.get("availability", "NA")
            if avail == "NA":
                continue

            date_str = entry.get("date", "")
            try:
                flight_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
            except (ValueError, IndexError):
                continue

            avail_label = {"H": "High", "L": "Low"}.get(avail, avail)

            results.append(AwardResult(
                scraper="cathay",
                origin=route.origin,
                destination=route.destination,
                flight_date=flight_date,
                flight_number="CX",
                cabin=route.cabin.title(),
                miles_cost=0,  # API doesn't return miles cost, just availability
                stops=0,
                operating_carrier="CX",
                departure_time=f"Avail: {avail_label}",
                arrival_time=f"Updated: {update_time}",
            ))

        logger.info(f"[cathay] Found {len(results)} available dates for {route.origin}-{route.destination}")
        return results

    async def search(self, route: SearchRoute, search_date: date) -> list[AwardResult]:
        """Search a single date (wraps search_route for compatibility)."""
        single_route = SearchRoute(
            origin=route.origin,
            destination=route.destination,
            cabin=route.cabin,
            date_range=(search_date, search_date),
            programs=route.programs,
        )
        return await self.search_route(single_route)
