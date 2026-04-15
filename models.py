from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class SearchRoute:
    origin: str
    destination: str
    cabin: str
    date_range: tuple[date, date]
    programs: list[str]


@dataclass
class AwardResult:
    scraper: str
    origin: str
    destination: str
    flight_date: date
    flight_number: str
    cabin: str
    miles_cost: int
    stops: int
    departure_time: str = ""
    arrival_time: str = ""
    aircraft: str = ""
    operating_carrier: str = ""

    @property
    def dedup_key(self) -> str:
        return f"{self.scraper}:{self.origin}-{self.destination}:{self.flight_date}:{self.flight_number}:{self.cabin}"


@dataclass
class RunLog:
    scraper: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    routes_checked: int = 0
    results_found: int = 0
    error_message: str = ""


@dataclass
class Config:
    routes: list[SearchRoute]
    bot_token: str
    chat_id: str
    interval_hours: float = 8.0
    seats_aero_key: str = ""

    @classmethod
    def from_yaml(cls, data: dict) -> "Config":
        routes = []
        for r in data.get("routes", []):
            dr = r["date_range"]
            routes.append(SearchRoute(
                origin=r["origin"],
                destination=r["destination"],
                cabin=r.get("cabin", "business"),
                date_range=(date.fromisoformat(dr[0]), date.fromisoformat(dr[1])),
                programs=r.get("programs", ["aa"]),
            ))
        tg = data.get("telegram", {})
        sched = data.get("schedule", {})
        return cls(
            routes=routes,
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            seats_aero_key=data.get("seats_aero_key", ""),
            interval_hours=sched.get("interval_hours", 8.0),
        )
