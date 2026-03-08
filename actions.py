from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from economy import PRODUCTION_RULES
from world import WorldEvent, TradeOffer, BuyRequest, ActionType, TimePhase

if TYPE_CHECKING:
    from agents import VillagerAgent
    from config import SimConfig
    from world import World

REST_ENERGY = 30
REST_ENERGY_NIGHT = 50


@dataclass
class ActionContext:
    world: World
    agent_map: dict[str, VillagerAgent]
    cfg: SimConfig
    log: Callable[[WorldEvent], None]


# -- Inventory helpers ------------------------------------------------------


def _parse_trade_detail(detail: str) -> tuple[str, int, int] | None:
    parts = detail.split(":")
    if len(parts) != 3:
        return None
    item, qty_str, price_str = parts
    item = item.strip().lower()
    item = {"tool": "tools", "coin": "coins"}.get(item, item)
    try:
        # Be lenient: extract the first integer from qty/price strings
        # so "6 coins" or " 2 " still work
        qty_match = re.search(r"\d+", qty_str)
        price_match = re.search(r"\d+", price_str)
        if not qty_match or not price_match:
            return None
        return item, int(qty_match.group()), int(price_match.group())
    except ValueError:
        return None


def _decrement_inventory(inventory: dict[str, int], item: str, qty: int = 1) -> bool:
    if inventory.get(item, 0) >= qty:
        inventory[item] -= qty
        if inventory[item] == 0:
            del inventory[item]
        return True
    return False


def _increment_inventory(inventory: dict[str, int], item: str, qty: int) -> None:
    inventory[item] = inventory.get(item, 0) + qty


def _mk_event(
    ctx: ActionContext, agent: "VillagerAgent", action_type, detail: str, location: str
) -> WorldEvent:
    return WorldEvent(ctx.world.day, ctx.world.phase, agent.name, action_type, detail, location)


# -- Action handlers --------------------------------------------------------


def _handle_move(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    target = action.get("target", "")
    location = ctx.world.agent_locations[agent.name]
    if target not in ctx.world.locations:
        for loc_name in ctx.world.locations:
            if target.startswith(loc_name):
                target = loc_name
                break
    if ctx.world.move_agent(agent.name, target):
        return _mk_event(ctx, agent, ActionType.MOVE,
            f"{agent.name} arrived at {target}.", target)
    return _mk_event(ctx, agent, ActionType.REST,
        f"{agent.name} wanted to go to {target} but the path is not connected.", location)


def _handle_eat(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    if _decrement_inventory(agent.inventory, "food"):
        agent.hunger = min(100, agent.hunger + ctx.cfg.hunger_restore)
        return _mk_event(ctx, agent, ActionType.EAT,
            f"{agent.name} ate food (hunger +{ctx.cfg.hunger_restore})", location)
    return _mk_event(ctx, agent, ActionType.EAT,
        f"{agent.name} tried to eat but has no food!", location)


def _handle_sell(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    target = action.get("target", "").strip()
    location = ctx.world.agent_locations[agent.name]
    raw_detail = action.get("detail", "")
    trade = _parse_trade_detail(raw_detail)

    if not trade:
        return _mk_event(ctx, agent, ActionType.SELL,
            f"{agent.name}'s sell offer failed: bad detail format '{raw_detail}'", location)

    item, qty, price = trade

    # --- Market offer path (no target or "market") — no co-presence needed ---
    if not target or target.lower() == "market":
        already_offered = sum(
            o.quantity for o in ctx.world.pending_offers
            if o.seller == agent.name and o.item == item
        )
        available = agent.inventory.get(item, 0) - already_offered
        if available < qty:
            return _mk_event(ctx, agent, ActionType.SELL,
                f"{agent.name}'s market offer failed: only {available} {item} available "
                f"({agent.inventory.get(item, 0)} in hand, {already_offered} already offered)", location)
        ctx.world.pending_offers.append(TradeOffer(
            seller=agent.name, buyer=None, item=item, quantity=qty, price=price,
            expires_tick=ctx.world.current_tick() + 10,
        ))
        return _mk_event(ctx, agent, ActionType.SELL,
            f"{agent.name} posts open market offer: {qty}x {item} for {price} coins", location)

    # --- P2P path (specific target) ---
    if target not in ctx.agent_map:
        return _mk_event(ctx, agent, ActionType.SELL,
            f"{agent.name}'s sell offer failed: unknown target '{target}'", location)
    if ctx.world.agent_locations.get(target) != location:
        target_loc = ctx.world.agent_locations.get(target, "unknown")
        return _mk_event(ctx, agent, ActionType.SELL,
            f"{agent.name}'s sell offer failed: {target} is at {target_loc}, not {location}", location)
    if agent.inventory.get(item, 0) < qty:
        return _mk_event(ctx, agent, ActionType.SELL,
            f"{agent.name}'s sell offer failed: not enough {item} "
            f"(have {agent.inventory.get(item, 0)}, need {qty})", location)

    buyer_agent = ctx.agent_map[target]
    # Path 1: fulfill a pending buy request (targeted or open market)
    matching_request = next(
        (r for r in ctx.world.pending_requests
         if r.buyer == target and (r.seller == agent.name or r.seller is None)
         and r.item == item and r.quantity == qty and r.price == price),
        None,
    )
    if matching_request and buyer_agent.inventory.get("coins", 0) >= price:
        _decrement_inventory(buyer_agent.inventory, "coins", price)
        _increment_inventory(buyer_agent.inventory, item, qty)
        _increment_inventory(agent.inventory, "coins", price)
        _decrement_inventory(agent.inventory, item, qty)
        ctx.world.pending_requests.remove(matching_request)
        return _mk_event(ctx, agent, ActionType.TRADE,
            f"TRADE: {agent.name} sold {qty}x {item} to {target} for {price} coins (fulfilled request)", location)
    # Path 2: post a P2P sell offer
    ctx.world.pending_offers.append(TradeOffer(
        seller=agent.name, buyer=target, item=item, quantity=qty, price=price,
        expires_tick=ctx.world.current_tick() + 10,
    ))
    return _mk_event(ctx, agent, ActionType.SELL,
        f"{agent.name} offers {qty}x {item} to {target} for {price} coins", location)


def _handle_buy(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    target = action.get("target", "").strip()
    location = ctx.world.agent_locations[agent.name]
    raw_detail = action.get("detail", "")
    trade = _parse_trade_detail(raw_detail)

    if not trade:
        return _mk_event(ctx, agent, ActionType.BUY,
            f"{agent.name}'s buy failed: bad detail format '{raw_detail}'", location)

    item, qty, price = trade

    # --- Market request path (no target or "market") — no co-presence needed ---
    if not target or target.lower() == "market":
        if agent.inventory.get("coins", 0) < price:
            return _mk_event(ctx, agent, ActionType.BUY,
                f"{agent.name}'s market request failed: not enough coins "
                f"(have {agent.inventory.get('coins', 0)}, need {price})", location)
        ctx.world.pending_requests.append(BuyRequest(
            buyer=agent.name, seller=None, item=item, quantity=qty, price=price,
            expires_tick=ctx.world.current_tick() + 10,
        ))
        return _mk_event(ctx, agent, ActionType.BUY,
            f"{agent.name} posts open market request: {qty}x {item} for {price} coins", location)

    # --- P2P path (specific target) ---

    # Path 1: accept an existing sell offer (targeted or open market) — no co-presence needed
    matching_offer = next(
        (o for o in ctx.world.pending_offers
         if o.seller == target and (o.buyer == agent.name or o.buyer is None)
         and o.item == item and o.quantity == qty and o.price == price),
        None,
    )
    seller = ctx.agent_map.get(target)
    if matching_offer and seller:
        if agent.inventory.get("coins", 0) >= price and seller.inventory.get(item, 0) >= qty:
            _decrement_inventory(agent.inventory, "coins", price)
            _increment_inventory(agent.inventory, item, qty)
            _increment_inventory(seller.inventory, "coins", price)
            _decrement_inventory(seller.inventory, item, qty)
            ctx.world.pending_offers.remove(matching_offer)
            return _mk_event(ctx, agent, ActionType.TRADE,
                f"TRADE: {agent.name} bought {qty}x {item} from {target} for {price} coins", location)
        # Offer exists but can't be fulfilled — remove it (stale) and fall through
        ctx.world.pending_offers.remove(matching_offer)

    # Path 2: post a P2P buy request (requires co-presence)
    if target not in ctx.agent_map:
        return _mk_event(ctx, agent, ActionType.BUY,
            f"{agent.name}'s buy request failed: unknown target '{target}'", location)
    if ctx.world.agent_locations.get(target) != location:
        target_loc = ctx.world.agent_locations.get(target, "unknown")
        return _mk_event(ctx, agent, ActionType.BUY,
            f"{agent.name}'s buy request failed: {target} is at {target_loc}, not {location}", location)
    if agent.inventory.get("coins", 0) < price:
        return _mk_event(ctx, agent, ActionType.BUY,
            f"{agent.name}'s buy request failed: not enough coins "
            f"(have {agent.inventory.get('coins', 0)}, need {price})", location)
    ctx.world.pending_requests.append(BuyRequest(
        buyer=agent.name, seller=target, item=item, quantity=qty, price=price,
        expires_tick=ctx.world.current_tick() + 10,
    ))
    return _mk_event(ctx, agent, ActionType.BUY,
        f"{agent.name} requests to buy {qty}x {item} from {target} for {price} coins", location)


def _handle_work(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    message = action.get("message", "")

    if agent.role == "Merchant" and location == "Market":
        agent.energy = max(0, agent.energy - 10)
        agent.fulfillment = min(100, agent.fulfillment + 20)
        desc = message or "working the market stall"
        return _mk_event(ctx, agent, ActionType.WORK,
            f"{agent.name} works: {desc} (-10 energy)", location)

    rule = PRODUCTION_RULES.get((agent.role, location))
    if rule:
        item, qty, cost = rule
        if agent.role == "Farmer" and not _decrement_inventory(agent.inventory, "tools"):
            return _mk_event(ctx, agent, ActionType.WORK,
                f"{agent.name} tried to farm but has no tools!", location)
        agent.energy = max(0, agent.energy - cost)
        _increment_inventory(agent.inventory, item, qty)
        agent.fulfillment = min(100, agent.fulfillment + 20)
        desc = message or "doing their job"
        tool_note = " (used 1 tool)" if agent.role == "Farmer" else ""
        return _mk_event(ctx, agent, ActionType.WORK,
            f"{agent.name} works: {desc} (+{qty} {item}{tool_note}, -{cost} energy)", location)

    return _mk_event(ctx, agent, ActionType.STAY,
        f"{agent.name} has nothing to work on here, stays and observes.", location)


def _handle_stay(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    agent.energy = min(100, agent.energy + 10)
    return _mk_event(ctx, agent, ActionType.STAY,
        f"{agent.name} stays and observes.", location)


def _handle_rest(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    gain = REST_ENERGY_NIGHT if ctx.world.phase == TimePhase.NIGHT else REST_ENERGY
    agent.energy = min(100, agent.energy + gain)
    return _mk_event(ctx, agent, ActionType.REST,
        f"{agent.name} rests (+{gain} energy)", location)


_HANDLERS: dict[str, Callable] = {
    ActionType.MOVE: _handle_move,
    ActionType.EAT: _handle_eat,
    ActionType.SELL: _handle_sell,
    ActionType.BUY: _handle_buy,
    ActionType.WORK: _handle_work,
    ActionType.STAY: _handle_stay,
}


# -- Public dispatch --------------------------------------------------------


def execute_action(agent: VillagerAgent, action: dict, ctx: ActionContext) -> None:
    action_type = action.get("action", ActionType.REST).upper()
    handler = _HANDLERS.get(action_type, _handle_rest)
    event = handler(agent, action, ctx)
    if event:
        ctx.world.add_event(event)
        ctx.log(event)
