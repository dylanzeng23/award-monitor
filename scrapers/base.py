import asyncio
import logging
import random
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth

from models import AwardResult, SearchRoute

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

CONTEXT_DIR = Path(__file__).parent.parent / "browser_data"


class BaseScraper(ABC):
    name: str = "base"

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._stealth = Stealth()

    async def start(self):
        CONTEXT_DIR.mkdir(exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            storage_state=self._storage_state_path()
            if self._storage_state_path().exists()
            else None,
        )
        await self._stealth.apply_stealth_async(self._context)

    async def stop(self):
        if self._context:
            try:
                await self._context.storage_state(
                    path=str(self._storage_state_path())
                )
            except Exception:
                pass
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def _storage_state_path(self) -> Path:
        return CONTEXT_DIR / f"{self.name}_state.json"

    async def new_page(self) -> Page:
        page = await self._context.new_page()
        return page

    async def random_delay(self, min_sec: float = 2.0, max_sec: float = 6.0):
        delay = random.uniform(min_sec, max_sec)
        await asyncio.sleep(delay)

    async def human_type(self, page: Page, selector: str, text: str):
        """Type text with human-like delays between keystrokes."""
        await page.click(selector)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        for char in text:
            await page.keyboard.type(char, delay=random.randint(50, 150))

    @abstractmethod
    async def search(self, route: SearchRoute, search_date: date) -> list[AwardResult]:
        """Search for award availability on a specific date. Returns list of results."""
        ...

    async def search_route(self, route: SearchRoute) -> list[AwardResult]:
        """Search all dates in the route's date range."""
        all_results = []
        current = route.date_range[0]
        end = route.date_range[1]
        while current <= end:
            try:
                logger.info(f"[{self.name}] Searching {route.origin}-{route.destination} on {current}")
                results = await self.search(route, current)
                all_results.extend(results)
                logger.info(f"[{self.name}] Found {len(results)} results for {current}")
            except Exception as e:
                logger.error(f"[{self.name}] Error searching {current}: {e}")
            await self.random_delay(3.0, 8.0)
            current = date.fromordinal(current.toordinal() + 1)
        return all_results
