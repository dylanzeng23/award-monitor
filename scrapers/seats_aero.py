import asyncio
import json
import logging
from datetime import date, timedelta

from playwright.async_api import async_playwright, Browser, BrowserContext
from playwright_stealth import Stealth

from models import AwardResult, SearchRoute

logger = logging.getLogger(__name__)

SEARCH_API = "https://seats.aero/_api/search_partial"

CABIN_FIELDS = {
    "business": ("jm", "js", "jc", "jd", "jt"),
    "first": ("fm", "fs", "fc", "fd", "ft"),
    "economy": ("ym", "ys", "yc", "yd", "yt"),
    "premium": ("wm", "ws", "wc", "wd", "wt"),
}


class SeatsAeroScraper:
    name = "seats_aero"

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        stealth = Stealth()
        await stealth.apply_stealth_async(self._context)
        self._page = await self._context.new_page()

        # Load seats.aero to get session/cookies
        await self._page.goto("https://seats.aero/search", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        logger.info("seats.aero session established")

    async def stop(self):
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _fetch_api(self, params: dict) -> dict | None:
        """Call seats.aero API from within the browser context to pass Cloudflare."""
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{SEARCH_API}?{query}"

        result = await self._page.evaluate(f"""
            async () => {{
                try {{
                    const resp = await fetch("{url}");
                    if (!resp.ok) return {{error: true, status: resp.status}};
                    return await resp.json();
                }} catch(e) {{
                    return {{error: true, message: e.message}};
                }}
            }}
        """)

        if isinstance(result, dict) and result.get("error"):
            logger.warning(f"seats.aero API error: {result}")
            return None
        return result

    async def search(self, route: SearchRoute, search_date: date) -> list[AwardResult]:
        params = {
            "origins": route.origin,
            "destinations": route.destination,
            "date": search_date.isoformat(),
            "min_seats": "1",
            "applicable_cabin": "any",
            "max_fees": "40000",
            "disable_live_filtering": "false",
        }

        data = await self._fetch_api(params)
        if not data:
            return []

        if data.get("error"):
            logger.warning(f"seats.aero error: {data.get('errorMessage')}")
            return []

        return self._parse_results(data, route, search_date)

    def _parse_results(self, data: dict, route: SearchRoute, search_date: date) -> list[AwardResult]:
        results = []
        target_cabin = route.cabin.lower()
        fields = CABIN_FIELDS.get(target_cabin, CABIN_FIELDS["business"])
        miles_field, seats_field, carriers_field, direct_field, tax_field = fields

        for entry in data.get("metadata", []):
            miles = entry.get(miles_field, 0)
            if not miles:
                continue

            seats = entry.get(seats_field, 0)
            carriers = entry.get(carriers_field, "")
            is_direct = entry.get(direct_field, False)
            source = entry.get("source", "")
            origin = entry.get("oa", route.origin)
            dest = entry.get("da", route.destination)
            last_seen_hours = entry.get("lsh", 0)

            tax_info = entry.get(tax_field, {})
            tax_amount = tax_info.get("tt", 0)
            tax_currency = tax_info.get("tc", "")

            results.append(AwardResult(
                scraper=f"seats_aero:{source}",
                origin=origin,
                destination=dest,
                flight_date=search_date,
                flight_number=source,
                cabin=target_cabin.title(),
                miles_cost=miles,
                stops=0 if is_direct else 1,
                operating_carrier=carriers,
                departure_time=f"seen {last_seen_hours}h ago" if last_seen_hours else "",
                arrival_time=f"tax: {tax_amount/100:.0f} {tax_currency}" if tax_amount else "",
            ))

        return results

    async def search_route(self, route: SearchRoute) -> list[AwardResult]:
        all_results = []
        current = route.date_range[0]
        end = route.date_range[1]

        while current <= end:
            logger.info(f"[seats_aero] Searching {route.origin}-{route.destination} on {current}")
            results = await self.search(route, current)
            all_results.extend(results)
            logger.info(f"[seats_aero] Found {len(results)} results for {current}")
            await asyncio.sleep(1.5)  # rate limit
            current = current + timedelta(days=1)

        return all_results
