import logging
from datetime import datetime

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from models import AwardResult, Config, SearchRoute
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
    miles = MILES_REQUIRED.get(result.cabin.lower(), 0)
    avail = result.departure_time.replace("Avail: ", "") if result.departure_time else ""

    lines = [
        f"[{result.cabin[0]}] {result.origin} -> {result.destination} ({dest_city})",
        f"{weekday} {result.flight_date} | {avail}",
    ]
    if miles:
        lines.append(f"{miles:,} Asia Miles")

    return "\n".join(lines)


def format_cathay_europe_report(results: list[AwardResult], routes_checked: int, cabin_label: str = "Business") -> str:
    """Format a full Cathay Europe search report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cabin_key = {"premium econ": "premium", "business": "business", "economy": "economy", "first": "first"}.get(cabin_label.lower(), cabin_label.lower())
    miles = MILES_REQUIRED.get(cabin_key, 0)
    miles_str = f" | {miles:,} miles/seat" if miles else ""
    header = f"CX Europe {cabin_label}{miles_str}\n{now} | {routes_checked} routes\n{'=' * 30}\n"

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
_search_callback = None  # callback function to run immediate search


def set_config(config: Config):
    global _config
    _config = config


def set_search_callback(callback):
    global _search_callback
    _search_callback = callback


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages like 'cathay europe biz'."""
    text = (update.message.text or "").strip().lower()

    # Parse cabin from message
    cabin = None
    if text in ("cathay europe", "cathay eu", "cathay europe biz", "cathay eu biz", "search", "搜"):
        cabin = "business"
    elif text in ("cathay europe econ", "cathay eu econ", "搜经济"):
        cabin = "economy"
    elif text in ("cathay europe prem", "cathay europe premium", "cathay eu prem", "搜超经"):
        cabin = "premium"
    elif text in ("cathay europe first", "cathay eu first"):
        cabin = "first"

    if cabin:
        cabin_label = {"business": "Business", "economy": "Economy", "premium": "Premium Econ", "first": "First"}[cabin]
        await update.message.reply_text(f"Searching CX Europe {cabin_label}... (~5 seconds)")

        if _search_callback:
            results, routes_checked = await _search_callback(cabin=cabin)
            report = format_cathay_europe_report(results, routes_checked, cabin_label)
            await update.message.reply_text(report)
        else:
            await update.message.reply_text("Search not available (monitor not running).")

    elif text in ("help", "帮助", "/help"):
        await update.message.reply_text(
            "Commands:\n"
            "  cathay europe biz - Business class\n"
            "  cathay europe econ - Economy\n"
            "  cathay europe prem - Premium Economy\n"
            "  cathay europe - Business (default)\n"
            "  /status - Last run info\n"
            "  /routes - Show monitored routes\n"
            "  /recent - Recent availability found"
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
