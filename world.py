from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

MAX_EVENTS = 500


class TimePhase(Enum):
    DAWN = "Dawn"
    MORNING = "Morning"
    AFTERNOON = "Afternoon"
    EVENING = "Evening"
    NIGHT = "Night"


PHASE_ORDER = list(TimePhase)


class ActionType(str, Enum):
    MOVE = "MOVE"
    WORK = "WORK"
    REST = "REST"
    STAY = "STAY"
    EAT = "EAT"
    SELL = "SELL"
    BUY = "BUY"
    TALK = "TALK"
    TRADE = "TRADE"
    OFFER_EXPIRED = "OFFER_EXPIRED"
    REQUEST_EXPIRED = "REQUEST_EXPIRED"


@dataclass
class Location:
    name: str
    description: str
    connections: list[str]


@dataclass
class WorldEvent:
    day: int
    phase: TimePhase
    actor: str
    action: str
    detail: str
    location: str


@dataclass
class TradeOffer:
    seller: str
    buyer: str        # specific target — only this agent can BUY
    item: str
    quantity: int
    price: int        # in coins
    expires_tick: int # world tick when this expires


@dataclass
class BuyRequest:
    buyer: str
    seller: str       # specific target — only this agent can SELL to fulfill
    item: str
    quantity: int
    price: int        # price willing to pay
    expires_tick: int


class World:
    def __init__(self, village_name: str = "Embervale"):
        self.village_name = village_name
        self.day: int = 1
        self.phase_index: int = 0
        self.locations: dict[str, Location] = {}
        self.agent_locations: dict[str, str] = {}
        self.events: list[WorldEvent] = []
        self.pending_offers: list[TradeOffer] = []
        self.pending_requests: list[BuyRequest] = []
        self.market_rates: dict[str, int] = {}
        self.agent_inventories: dict[str, dict] = {}
        self._setup_village()

    @property
    def phase(self) -> TimePhase:
        return PHASE_ORDER[self.phase_index]

    def _setup_village(self):
        self.locations = {
            "Town Square": Location(
                "Town Square",
                "The central gathering place with a stone fountain and old oak tree.",
                ["Market", "Farm", "Blacksmith", "Tavern"],
            ),
            "Market": Location(
                "Market",
                "An open-air market with wooden stalls selling goods and produce.",
                ["Town Square", "Tavern"],
            ),
            "Farm": Location(
                "Farm",
                "Rolling fields of wheat and vegetables, with a small barn.",
                ["Town Square", "River"],
            ),
            "Blacksmith": Location(
                "Blacksmith",
                "A sturdy stone forge with bellows, anvil, and the smell of hot iron.",
                ["Town Square"],
            ),
            "Tavern": Location(
                "Tavern",
                "A warm tavern with a crackling fireplace and the scent of ale.",
                ["Town Square", "Market", "River"],
            ),
            "River": Location(
                "River",
                "A gentle stream with clear water, good for fishing and washing.",
                ["Farm", "Tavern"],
            ),
        }

    def current_tick(self) -> int:
        return self.day * 5 + self.phase_index

    def advance_time(self):
        self.phase_index += 1
        if self.phase_index >= len(PHASE_ORDER):
            self.phase_index = 0
            self.day += 1

    def move_agent(self, agent_name: str, target: str) -> bool:
        current = self.agent_locations.get(agent_name)
        if current and target in self.locations and target in self.locations[current].connections:
            self.agent_locations[agent_name] = target
            return True
        return False

    def get_agents_at(self, location: str) -> list[str]:
        return [name for name, loc in self.agent_locations.items() if loc == location]

    def add_event(self, event: WorldEvent):
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS:]

    def get_visible_events(self, agent_name: str, count: int = 10) -> list[WorldEvent]:
        location = self.agent_locations.get(agent_name, "")
        visible = [
            e
            for e in self.events[-30:]
            if e.location == location or e.actor == agent_name
        ]
        return visible[-count:]

    def to_dict(self) -> dict:
        return {
            "village_name": self.village_name,
            "day": self.day,
            "phase_index": self.phase_index,
            "agent_locations": dict(self.agent_locations),
            "events": [
                {
                    "day": e.day, "phase": e.phase.value, "actor": e.actor,
                    "action": e.action, "detail": e.detail, "location": e.location,
                }
                for e in self.events
            ],
            "pending_offers": [
                {
                    "seller": o.seller, "buyer": o.buyer, "item": o.item,
                    "quantity": o.quantity, "price": o.price, "expires_tick": o.expires_tick,
                }
                for o in self.pending_offers
            ],
            "pending_requests": [
                {
                    "buyer": r.buyer, "seller": r.seller, "item": r.item,
                    "quantity": r.quantity, "price": r.price, "expires_tick": r.expires_tick,
                }
                for r in self.pending_requests
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "World":
        world = cls(village_name=data["village_name"])
        world.day = data["day"]
        world.phase_index = data["phase_index"]
        world.agent_locations = data["agent_locations"]
        phase_map = {p.value: p for p in TimePhase}
        world.events = [
            WorldEvent(
                day=e["day"], phase=phase_map[e["phase"]], actor=e["actor"],
                action=e["action"], detail=e["detail"], location=e["location"],
            )
            for e in data.get("events", [])
        ]
        world.pending_offers = [
            TradeOffer(
                seller=o["seller"], buyer=o["buyer"], item=o["item"],
                quantity=o["quantity"], price=o["price"], expires_tick=o["expires_tick"],
            )
            for o in data.get("pending_offers", [])
        ]
        world.pending_requests = [
            BuyRequest(
                buyer=r["buyer"], seller=r["seller"], item=r["item"],
                quantity=r["quantity"], price=r["price"], expires_tick=r["expires_tick"],
            )
            for r in data.get("pending_requests", [])
        ]
        return world
