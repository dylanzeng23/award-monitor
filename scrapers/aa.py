import logging
import random
import re
from datetime import date

import httpx

from models import AwardResult, SearchRoute
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

AA_API_URL = "https://www.aa.com/booking/api/search/itinerary"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


class AAScraper(BaseScraper):
    name = "aa"

    async def start(self):
        """Initialize HTTP client instead of browser."""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": "https://www.aa.com",
                "Referer": "https://www.aa.com/booking/find-flights",
                "Sec-Ch-Ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        # Hit the main page first to get cookies
        try:
            resp = await self._client.get(
                "https://www.aa.com/homePage.do",
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
            logger.info(f"AA homepage status: {resp.status_code}, cookies: {len(self._client.cookies)}")
        except Exception as e:
            logger.warning(f"Failed to get AA homepage cookies: {e}")

    async def stop(self):
        if hasattr(self, "_client") and self._client:
            await self._client.aclose()

    async def search(self, route: SearchRoute, search_date: date) -> list[AwardResult]:
        payload = {
            "metadata": {
                "selectedProducts": [],
                "tripType": "OneWay",
                "udo": {},
            },
            "passengers": [{"type": "adult", "count": 1}],
            "queryParams": {
                "sliceIndex": 0,
                "sessionId": "",
                "solutionId": "",
                "solutionSet": "",
            },
            "requestHeader": {"clientId": "AAcom"},
            "slices": [
                {
                    "allCarriers": True,
                    "cabin": "",
                    "connectionCity": None,
                    "departureDate": search_date.isoformat(),
                    "destination": route.destination,
                    "includeNearbyAirports": False,
                    "maxStops": None,
                    "origin": route.origin,
                    "departureTime": "040001",
                }
            ],
            "tripOptions": {
                "locale": "en_US",
                "searchType": "Award",
            },
            "loyaltyInfo": None,
        }

        try:
            resp = await self._client.post(AA_API_URL, json=payload)
            logger.info(f"AA API response: {resp.status_code}")

            if resp.status_code == 403:
                logger.warning("AA API returned 403 - blocked by Akamai")
                return []

            if resp.status_code != 200:
                logger.warning(f"AA API error: {resp.status_code} {resp.text[:500]}")
                return []

            data = resp.json()
            return self._parse_api_response(data, route, search_date)

        except Exception as e:
            logger.error(f"AA API request failed: {e}")
            return []

    def _parse_api_response(self, data: dict, route: SearchRoute, search_date: date) -> list[AwardResult]:
        results = []

        # Check for errors
        error = data.get("error")
        if error:
            error_num = error.get("errorNumber")
            if error_num in (309, 1100):
                logger.info(f"No flights: error {error_num}")
                return []
            logger.warning(f"AA API error: {error}")
            return []

        # Parse slices -> flights
        slices = data.get("slices", [])
        for sl in slices:
            segments = sl.get("segments", [])
            products = sl.get("productsBySegment") or sl.get("products", [])

            for flight_group in segments if segments else [sl]:
                legs = flight_group.get("legs", [])
                if not legs:
                    continue

                first_leg = legs[0]
                last_leg = legs[-1]

                origin = first_leg.get("origin", {}).get("code", route.origin)
                dest = last_leg.get("destination", {}).get("code", route.destination)
                dep_time = first_leg.get("departureDateTime", "")
                arr_time = last_leg.get("arrivalDateTime", "")
                flight_num = first_leg.get("flightNumber", "")
                carrier = first_leg.get("operatingCarrier", {}).get("code", "")
                aircraft = first_leg.get("aircraft", {}).get("name", "")
                stops = len(legs) - 1

                # Extract award pricing from products
                for product_list in (products if isinstance(products, list) else [products]):
                    if isinstance(product_list, dict):
                        product_list = [product_list]
                    if not isinstance(product_list, list):
                        continue

                    for product in product_list:
                        cabin = product.get("cabin", "") or product.get("productType", "")
                        if route.cabin.lower() not in cabin.lower() and "bus" not in cabin.lower():
                            continue

                        miles = 0
                        # Try different pricing paths in the response
                        per_pax = product.get("perPassengerAwardPoints") or product.get("milesPoints")
                        if per_pax:
                            miles = int(per_pax)
                        else:
                            prices = product.get("prices", [])
                            for price in prices:
                                if price.get("currency") == "AAmiles" or "mile" in str(price.get("currency", "")).lower():
                                    miles = int(price.get("amount", 0))
                                    break

                        if miles > 0:
                            results.append(AwardResult(
                                scraper="aa",
                                origin=origin,
                                destination=dest,
                                flight_date=search_date,
                                flight_number=f"{carrier}{flight_num}" if carrier else flight_num,
                                cabin=cabin.title() if cabin else "Business",
                                miles_cost=miles,
                                stops=stops,
                                departure_time=dep_time,
                                arrival_time=arr_time,
                                aircraft=aircraft,
                                operating_carrier=carrier,
                            ))

        # Also try top-level "flights" key
        flights = data.get("flights", [])
        for flight in flights:
            self._parse_flight_entry(flight, route, search_date, results)

        return results

    def _parse_flight_entry(self, flight: dict, route: SearchRoute, search_date: date, results: list):
        """Parse a flight entry from the 'flights' key if present."""
        origin = flight.get("origin", route.origin)
        dest = flight.get("destination", route.destination)
        dep_time = flight.get("departureTime", "")
        arr_time = flight.get("arrivalTime", "")
        stops = flight.get("stops", 0)
        carrier = flight.get("operatingCarrier", "")
        flight_num = flight.get("flightNumber", "")

        products = flight.get("products", {})
        for cabin_key, product in products.items():
            if route.cabin.lower() not in cabin_key.lower() and "bus" not in cabin_key.lower():
                continue
            miles = product.get("miles", 0) or product.get("awardPoints", 0)
            if miles > 0:
                results.append(AwardResult(
                    scraper="aa",
                    origin=origin,
                    destination=dest,
                    flight_date=search_date,
                    flight_number=f"{carrier}{flight_num}",
                    cabin=cabin_key.title(),
                    miles_cost=miles,
                    stops=stops,
                    departure_time=dep_time,
                    arrival_time=arr_time,
                    operating_carrier=carrier,
                ))
