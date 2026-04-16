#!/usr/bin/env python3
"""Award Flight Availability Monitor - searches Cathay Pacific award availability and sends Telegram alerts."""

import argparse
import asyncio
import logging
from datetime import datetime, UTC
from pathlib import Path

import yaml

import db
from models import AwardResult, Config, RunLog, SearchRoute
from notifier import send_alerts, send_message, build_bot_app, set_search_callback, set_config_path, is_search_requested
from scrapers.cathay import CathayScraper
from scrapers.seats_aero import SeatsAeroScraper
from scrapers.seats_aero_pro import SeatsAeroProScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "award_monitor.log"),
    ],
)
logger = logging.getLogger(__name__)

SCRAPERS = {
    "cathay": CathayScraper,
    "seats_aero": SeatsAeroScraper,
    "seats_aero_pro": None,  # Initialized with API key from config
}


def load_config(path: str = "config.yaml") -> Config:
    config_path = Path(__file__).parent / path
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return Config.from_yaml(data)


async def run_search_cycle(config: Config, dry_run: bool = False) -> list[AwardResult]:
    """Run one full search cycle across all routes and programs."""
    all_new_results = []

    for route in config.routes:
        for program in route.programs:
            if program == "seats_aero_pro":
                if not config.seats_aero_key:
                    continue
                scraper = SeatsAeroProScraper(api_key=config.seats_aero_key)
            else:
                scraper_cls = SCRAPERS.get(program)
                if not scraper_cls:
                    logger.warning(f"Unknown scraper: {program}")
                    continue
                scraper = scraper_cls()
            run_log = RunLog(scraper=program, started_at=datetime.now(UTC))
            log_id = db.save_run_log(run_log)

            try:
                await scraper.start()
                results = await scraper.search_route(route)
                run_log.routes_checked = (route.date_range[1] - route.date_range[0]).days + 1
                run_log.results_found = len(results)
                run_log.status = "success"

                for result in results:
                    is_new = db.save_result(result)
                    if is_new:
                        all_new_results.append(result)
                        logger.info(
                            f"NEW: {result.origin}-{result.destination} "
                            f"{result.flight_date} {result.cabin} {result.departure_time}"
                        )

            except Exception as e:
                run_log.status = "error"
                run_log.error_message = str(e)[:500]
                logger.error(f"Search cycle error for {program}: {e}")
            finally:
                await scraper.stop()
                run_log.finished_at = datetime.now(UTC)
                db.update_run_log(log_id, run_log)

    if all_new_results and not dry_run:
        await send_alerts(config, all_new_results)
        for result in all_new_results:
            db.mark_notified(result)

    return all_new_results


async def run_immediate_search(config: Config, cabin: str = "business", search_all: bool = False) -> tuple[list[AwardResult], int]:
    """Run an immediate search (called from Telegram bot).
    search_all=True searches CX + seats.aero Pro + Tokyo routes.
    search_all=False searches CX only.
    Returns (all_results, routes_checked)."""
    all_results = []
    routes_checked = 0

    # CX direct search
    scraper = CathayScraper()
    try:
        await scraper.start()
        for route in config.routes:
            if "cathay" not in route.programs:
                continue
            search_route = SearchRoute(
                origin=route.origin,
                destination=route.destination,
                cabin=cabin,
                date_range=route.date_range,
                programs=route.programs,
            )
            results = await scraper.search_route(search_route)
            all_results.extend(results)
            routes_checked += 1
    except Exception as e:
        logger.error(f"CX search error: {e}")
    finally:
        await scraper.stop()

    # seats.aero Pro search (all programs)
    if search_all and config.seats_aero_key:
        sa_scraper = SeatsAeroProScraper(api_key=config.seats_aero_key)
        try:
            await sa_scraper.start()
            for route in config.routes:
                if "seats_aero_pro" not in route.programs:
                    continue
                search_route = SearchRoute(
                    origin=route.origin,
                    destination=route.destination,
                    cabin=cabin,
                    date_range=route.date_range,
                    programs=route.programs,
                )
                results = await sa_scraper.search_route(search_route)
                all_results.extend(results)
                routes_checked += 1
        except Exception as e:
            logger.error(f"seats.aero search error: {e}")
        finally:
            await sa_scraper.stop()

    return all_results, routes_checked


async def scheduler_loop(config: Config):
    """Main loop that runs searches on a schedule and listens for bot commands."""
    interval_seconds = config.interval_hours * 3600
    logger.info(f"Scheduler started. Interval: {config.interval_hours}h ({interval_seconds:.0f}s)")

    # Set up the search callback for "cathay europe" command
    async def search_callback(cabin: str = "business", search_all: bool = False):
        return await run_immediate_search(config, cabin=cabin, search_all=search_all)

    set_search_callback(search_callback)

    # Start Telegram bot polling
    bot_app = None
    if config.bot_token and config.chat_id:
        bot_app = build_bot_app(config)
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started")
        await send_message(
            config,
            "Award Monitor started.\n\n"
            "Commands:\n"
            "  europe biz - ALL sources (CX+DL/AA/AS+JAL)\n"
            "  cx europe biz - CX only\n"
            "  /check - Search all sources\n"
            "  /status - Last run\n"
            "  /routes - Show routes\n"
            "  /recent - Recent finds\n"
            "  /add CDG - Add route\n"
            "  /remove CDG - Remove route\n"
            f"\nMonitoring {len(config.routes)} routes every {config.interval_hours}h"
        )

    try:
        while True:
            logger.info("Starting scheduled search cycle...")
            try:
                new_results = await run_search_cycle(config)
                logger.info(f"Search cycle complete. {len(new_results)} new results.")
            except Exception as e:
                logger.error(f"Search cycle failed: {e}")

            # Wait for next cycle, checking for manual trigger every 10s
            elapsed = 0
            while elapsed < interval_seconds:
                await asyncio.sleep(10)
                elapsed += 10
                if is_search_requested():
                    logger.info("Immediate search requested via Telegram")
                    break
    finally:
        if bot_app:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()


async def main():
    parser = argparse.ArgumentParser(description="Award Flight Availability Monitor")
    parser.add_argument("--dry-run", action="store_true", help="Run one search cycle without sending alerts")
    parser.add_argument("--once", action="store_true", help="Run one search cycle and exit")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    args = parser.parse_args()

    config = load_config(args.config)
    set_config_path(Path(__file__).parent / args.config)
    db.init_db()

    logger.info(f"Loaded {len(config.routes)} routes")
    for r in config.routes:
        logger.info(f"  {r.origin}-{r.destination} ({r.cabin}) {r.date_range[0]} to {r.date_range[1]}")

    if args.dry_run or args.once:
        results = await run_search_cycle(config, dry_run=args.dry_run)
        print(f"\nFound {len(results)} new award seats:")
        for r in results:
            print(f"  {r.origin}-{r.destination} {r.flight_date} {r.cabin} {r.departure_time}")
    else:
        await scheduler_loop(config)


if __name__ == "__main__":
    asyncio.run(main())
