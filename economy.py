from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from world import WorldEvent, ActionType

if TYPE_CHECKING:
    from agents import VillagerAgent
    from world import World

# (role, location) -> (item_produced, quantity, energy_cost)
PRODUCTION_RULES: dict[tuple[str, str], tuple[str, int, int]] = {
    ("Farmer", "Farm"): ("food", 2, 20),
    ("Blacksmith", "Blacksmith"): ("tools", 1, 25),
}


def compute_market_rates(agents: list[VillagerAgent], base_prices: dict[str, int]) -> dict[str, int]:
    rates: dict[str, int] = {}
    for item, base in base_prices.items():
        total_supply = max(1, sum(a.inventory.get(item, 0) for a in agents))
        raw = round(base * 6 / total_supply)
        rates[item] = max(1, min(base * 3, raw))
    return rates


def expire_offers(world: World, log_fn: Callable[[WorldEvent], None]) -> None:
    current_tick = world.current_tick()

    expired_offers = [o for o in world.pending_offers if o.expires_tick <= current_tick]
    for o in expired_offers:
        event = WorldEvent(
            world.day, world.phase, o.seller, ActionType.OFFER_EXPIRED,
            f"{o.seller}'s offer to {o.buyer or 'market'} ({o.quantity}x{o.item}) expired",
            world.agent_locations.get(o.seller, ""),
        )
        world.add_event(event)
        log_fn(event)
    if expired_offers:
        world.pending_offers = [o for o in world.pending_offers if o.expires_tick > current_tick]

    expired_requests = [r for r in world.pending_requests if r.expires_tick <= current_tick]
    for r in expired_requests:
        event = WorldEvent(
            world.day, world.phase, r.buyer, ActionType.REQUEST_EXPIRED,
            f"{r.buyer}'s buy request to {r.seller or 'market'} ({r.quantity}x{r.item}) expired",
            world.agent_locations.get(r.buyer, ""),
        )
        world.add_event(event)
        log_fn(event)
    if expired_requests:
        world.pending_requests = [r for r in world.pending_requests if r.expires_tick > current_tick]
