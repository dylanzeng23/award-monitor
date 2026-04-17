import logging
import re
from datetime import date, datetime
from pathlib import Path

import yaml
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from models import AwardResult, Config, SearchRoute
from wechat import send_wechat_message
import db

logger = logging.getLogger(__name__)

# Airport code to city name mapping for Cathay Europe routes
EUROPE_CITIES = {
    "CDG": "Paris", "FRA": "Frankfurt", "FCO": "Rome", "MXP": "Milan",
    "AMS": "Amsterdam", "BCN": "Barcelona", "MAD": "Madrid", "ZRH": "Zurich",
    "LHR": "London", "MAN": "Manchester",
}

# Asia Miles required per cabin (HKG to Europe, all same)
MILES_REQUIRED = {"economy": 27000, "premium": 50000, "business": 88000, "first": 125000}

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def format_alert(result: AwardResult) -> str:
    """Format an award availability alert for Telegram."""
    dest_city = EUROPE_CITIES.get(result.destination, result.destination)
    weekday = WEEKDAYS[result.flight_date.weekday()]
    avail = result.departure_time.replace("Avail: ", "") if result.departure_time else ""

    # Use actual miles from scraper; fall back to CX pricing only for cathay scraper
    if result.miles_cost and result.miles_cost > 0:
        program = result.operating_carrier or result.scraper.split(":")[-1]
        miles_line = f"{result.miles_cost:,} {program} miles"
    else:
        miles = MILES_REQUIRED.get(result.cabin.lower(), 0)
        miles_line = f"{miles:,} Asia Miles" if miles else ""

    lines = [
        f"[{result.cabin[0]}] {result.origin} -> {result.destination} ({dest_city})",
        f"{weekday} {result.flight_date} | {avail}",
    ]
    if miles_line:
        lines.append(miles_line)

    return "\n".join(lines)


def format_cathay_europe_report(results: list[AwardResult], routes_checked: int, cabin_label: str = "Business", source_label: str = "CX Europe") -> str:
    """Format a search report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"{source_label} {cabin_label}\n{now} | {routes_checked} routes\n{'=' * 30}\n"

    if not results:
        return header + f"\nNo {cabin_label.lower()} availability found."

    lines = [header, f"{len(results)} seat(s) found:\n"]
    for r in results:
        dest_city = EUROPE_CITIES.get(r.destination, r.destination)
        weekday = WEEKDAYS[r.flight_date.weekday()]
        avail = r.departure_time.replace("Avail: ", "") if r.departure_time else ""
        lines.append(f"  {weekday} {r.flight_date} {r.destination} ({dest_city}) - {avail}")

    return "\n".join(lines)


async def send_alerts(config: Config, new_results: list[AwardResult]):
    """Send Telegram alerts for new award availability."""
    if not new_results or not config.bot_token or not config.chat_id:
        return

    bot = Bot(token=config.bot_token)
    header = f"Award Alert - {len(new_results)} new seat{'s' if len(new_results) > 1 else ''} found!\n{'=' * 30}\n"

    messages = []
    current_msg = header
    for result in new_results:
        alert_text = format_alert(result)
        if len(current_msg) + len(alert_text) + 10 > 4000:
            messages.append(current_msg)
            current_msg = ""
        current_msg += "\n" + alert_text + "\n"

    if current_msg.strip():
        messages.append(current_msg)

    for msg in messages:
        try:
            await bot.send_message(chat_id=config.chat_id, text=msg)
            logger.info(f"Sent Telegram alert ({len(msg)} chars)")
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")

    # Also send to WeChat if configured
    if config.wechat_token and config.wechat_user_id:
        full_text = header + "\n".join(format_alert(r) + "\n" for r in new_results)
        await send_wechat_message(config.wechat_token, config.wechat_user_id, full_text)


async def send_message(config: Config, text: str):
    """Send a message to Telegram."""
    if not config.bot_token or not config.chat_id:
        return
    bot = Bot(token=config.bot_token)
    try:
        await bot.send_message(chat_id=config.chat_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")


# --- Telegram Bot Command Handlers ---

_config: Config | None = None
_config_path: Path | None = None
_search_callback = None  # callback function to run immediate search


def set_config(config: Config):
    global _config
    _config = config


def set_config_path(path: Path):
    global _config_path
    _config_path = path


def set_search_callback(callback):
    global _search_callback
    _search_callback = callback


def _save_config():
    """Write current routes back to config YAML, preserving other fields."""
    if not _config_path or not _config:
        return
    with open(_config_path) as f:
        data = yaml.safe_load(f)
    data["routes"] = []
    for r in _config.routes:
        data["routes"].append({
            "origin": r.origin,
            "destination": r.destination,
            "cabin": r.cabin,
            "date_range": [r.date_range[0].isoformat(), r.date_range[1].isoformat()],
            "programs": r.programs,
        })
    with open(_config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    last_run = db.get_last_run()
    if not last_run:
        await update.message.reply_text("No runs recorded yet.")
        return

    text = (
        f"Last run: {last_run['scraper']}\n"
        f"Started: {last_run['started_at']}\n"
        f"Status: {last_run['status']}\n"
        f"Routes checked: {last_run['routes_checked']}\n"
        f"Results found: {last_run['results_found']}"
    )
    if last_run["error_message"]:
        text += f"\nError: {last_run['error_message']}"
    await update.message.reply_text(text)


async def cmd_routes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /routes command."""
    if not _config:
        await update.message.reply_text("Config not loaded.")
        return

    lines = ["Monitored routes:"]
    for i, r in enumerate(_config.routes, 1):
        dest_city = EUROPE_CITIES.get(r.destination, r.destination)
        lines.append(f"{i}. {r.origin}-{r.destination} ({dest_city}) {r.date_range[0]} to {r.date_range[1]}")
    await update.message.reply_text("\n".join(lines))


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recent command."""
    recent = db.get_recent_availability(limit=10)
    if not recent:
        await update.message.reply_text("No availability found yet.")
        return

    lines = ["Recent availability:"]
    for r in recent:
        dest_city = EUROPE_CITIES.get(r["destination"], r["destination"])
        lines.append(f"  {r['flight_date']} {r['origin']}-{r['destination']} ({dest_city})")
    await update.message.reply_text("\n".join(lines))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check command - trigger immediate search."""
    global _search_requested
    _search_requested = True
    await update.message.reply_text("Searching ALL sources Business... (~15 seconds)")
    if _search_callback:
        results, routes_checked = await _search_callback(cabin="business", search_all=True)
        report = format_cathay_europe_report(results, routes_checked, "Business", "All Programs")
        await update.message.reply_text(report)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add command - add a route.
    /add CDG                             — HKG-CDG business, copy dates from first route
    /add HND CDG                         — specify origin
    /add HKG CDG biz 2026-09-23 2026-09-27 — full form
    """
    args = (update.message.text or "").split()[1:]
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  /add CDG — add HKG-CDG business\n"
            "  /add HND CDG — specify origin\n"
            "  /add HKG CDG biz 2026-09-23 2026-09-27"
        )
        return

    if len(args) == 1:
        origin, dest = "HKG", args[0].upper()
        cabin = "business"
        dr = _config.routes[0].date_range if _config.routes else None
        programs = ["cathay", "seats_aero_pro"]
    elif len(args) == 2:
        origin, dest = args[0].upper(), args[1].upper()
        cabin = "business"
        dr = _config.routes[0].date_range if _config.routes else None
        programs = ["cathay", "seats_aero_pro"] if origin == "HKG" else ["seats_aero_pro"]
    elif len(args) >= 5:
        origin, dest = args[0].upper(), args[1].upper()
        cabin_map = {"biz": "business", "business": "business", "econ": "economy",
                     "economy": "economy", "prem": "premium", "first": "first"}
        cabin = cabin_map.get(args[2].lower(), "business")
        try:
            dr = (date.fromisoformat(args[3]), date.fromisoformat(args[4]))
        except ValueError:
            await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
            return
        programs = ["cathay", "seats_aero_pro"] if origin == "HKG" else ["seats_aero_pro"]
    else:
        await update.message.reply_text("Use: /add CDG or /add HKG CDG biz 2026-09-23 2026-09-27")
        return

    if not dr:
        await update.message.reply_text("No existing routes to copy date range from. Use full form.")
        return

    for r in _config.routes:
        if r.origin == origin and r.destination == dest:
            await update.message.reply_text(f"Route {origin}-{dest} already exists.")
            return

    new_route = SearchRoute(origin=origin, destination=dest, cabin=cabin,
                            date_range=dr, programs=programs)
    _config.routes.append(new_route)
    _save_config()

    city = EUROPE_CITIES.get(dest, dest)
    await update.message.reply_text(
        f"Added: {origin}-{dest} ({city}) {cabin}\n"
        f"{dr[0]} to {dr[1]}\n"
        f"Programs: {', '.join(programs)}\n"
        f"Total routes: {len(_config.routes)}"
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove command.
    /remove 3       — by index from /routes
    /remove CDG     — by destination code
    """
    args = (update.message.text or "").split()[1:]
    if not args:
        await update.message.reply_text("Usage: /remove 3 or /remove CDG")
        return

    arg = args[0].upper()
    removed = None

    try:
        idx = int(args[0]) - 1
        if 0 <= idx < len(_config.routes):
            removed = _config.routes.pop(idx)
        else:
            await update.message.reply_text(f"Invalid index. Use 1-{len(_config.routes)}.")
            return
    except ValueError:
        for i, r in enumerate(_config.routes):
            if r.destination == arg:
                removed = _config.routes.pop(i)
                break

    if removed:
        _save_config()
        city = EUROPE_CITIES.get(removed.destination, removed.destination)
        await update.message.reply_text(
            f"Removed: {removed.origin}-{removed.destination} ({city})\n"
            f"Remaining: {len(_config.routes)} routes"
        )
    else:
        await update.message.reply_text(f"No route found for: {arg}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages like 'cathay europe biz'."""
    text = (update.message.text or "").strip().lower()

    # Parse command: "europe biz" = all sources, "cx europe biz" = CX only
    cabin = None
    search_all = False

    if text in ("europe biz", "europe", "search all", "all biz", "搜全部"):
        cabin = "business"
        search_all = True
    elif text in ("europe econ", "all econ"):
        cabin = "economy"
        search_all = True
    elif text in ("cathay europe", "cathay eu", "cathay europe biz", "cathay eu biz",
                   "cx europe", "cx eu", "cx europe biz", "cx eu biz", "search", "搜"):
        cabin = "business"
    elif text in ("cathay europe econ", "cathay eu econ", "cx europe econ", "cx eu econ", "搜经济"):
        cabin = "economy"
    elif text in ("cathay europe prem", "cathay europe premium", "cathay eu prem",
                   "cx europe prem", "cx eu prem", "搜超经"):
        cabin = "premium"
    elif text in ("cathay europe first", "cathay eu first", "cx europe first", "cx eu first"):
        cabin = "first"

    if cabin:
        cabin_label = {"business": "Business", "economy": "Economy", "premium": "Premium Econ", "first": "First"}[cabin]
        if search_all:
            await update.message.reply_text(f"Searching ALL sources {cabin_label}... (~15 seconds)")
        else:
            await update.message.reply_text(f"Searching CX {cabin_label}... (~5 seconds)")

        if _search_callback:
            results, routes_checked = await _search_callback(cabin=cabin, search_all=search_all)
            source_label = "All Programs" if search_all else "CX Europe"
            report = format_cathay_europe_report(results, routes_checked, cabin_label, source_label)
            await update.message.reply_text(report)
        else:
            await update.message.reply_text("Search not available (monitor not running).")

    elif text in ("help", "帮助", "/help"):
        await update.message.reply_text(
            "Commands:\n"
            "  europe biz - Search ALL (CX + DL/AA/AS + JAL)\n"
            "  cx europe biz - CX only\n"
            "  /check - Search all sources\n"
            "  /status - Last run\n"
            "  /routes - Show routes\n"
            "  /recent - Recent finds\n"
            "  /add CDG - Add route\n"
            "  /remove CDG - Remove route"
        )


# Signal for triggering immediate search from bot command
_search_requested = False


def is_search_requested() -> bool:
    global _search_requested
    if _search_requested:
        _search_requested = False
        return True
    return False


def build_bot_app(config: Config) -> Application:
    """Build Telegram bot application with command handlers."""
    set_config(config)
    app = Application.builder().token(config.bot_token).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("routes", cmd_routes))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
