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
    item = item.strip()
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
        return WorldEvent(
            ctx.world.day, ctx.world.phase, agent.name, ActionType.MOVE,
            f"{agent.name} arrived at {target}.", target,
        )
    return WorldEvent(
        ctx.world.day, ctx.world.phase, agent.name, ActionType.REST,
        f"{agent.name} wanted to go to {target} but the path is not connected.", location,
    )


def _handle_eat(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    if _decrement_inventory(agent.inventory, "food"):
        agent.hunger = min(100, agent.hunger + ctx.cfg.hunger_restore)
        return WorldEvent(
            ctx.world.day, ctx.world.phase, agent.name, ActionType.EAT,
            f"{agent.name} ate food (hunger +{ctx.cfg.hunger_restore})", location,
        )
    return WorldEvent(
        ctx.world.day, ctx.world.phase, agent.name, ActionType.EAT,
        f"{agent.name} tried to eat but has no food!", location,
    )


def _handle_sell(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    target = action.get("target", "")
    location = ctx.world.agent_locations[agent.name]
    raw_detail = action.get("detail", "")
    trade = _parse_trade_detail(raw_detail)

    if not trade:
        reason = f"bad detail format '{raw_detail}'"
    elif not target or target not in ctx.agent_map:
        reason = f"unknown target '{target}'"
    elif ctx.world.agent_locations.get(target) != location:
        target_loc = ctx.world.agent_locations.get(target, "unknown")
        reason = f"{target} is at {target_loc}, not {location}"
    else:
        item, qty, price = trade
        if agent.inventory.get(item, 0) < qty:
            reason = f"not enough {item} (have {agent.inventory.get(item, 0)}, need {qty})"
        else:
            buyer_agent = ctx.agent_map.get(target)
            # Path 1: fulfill a pending buy request
            matching_request = next(
                (r for r in ctx.world.pending_requests
                 if r.buyer == target and r.seller == agent.name
                 and r.item == item and r.quantity == qty and r.price == price),
                None,
            )
            if matching_request and buyer_agent and buyer_agent.inventory.get("coins", 0) >= price:
                _decrement_inventory(buyer_agent.inventory, "coins", price)
                _increment_inventory(buyer_agent.inventory, item, qty)
                _increment_inventory(agent.inventory, "coins", price)
                _decrement_inventory(agent.inventory, item, qty)
                ctx.world.pending_requests.remove(matching_request)
                return WorldEvent(
                    ctx.world.day, ctx.world.phase, agent.name, ActionType.TRADE,
                    f"TRADE: {agent.name} sold {qty}x{item} to {target} for {price} coins (fulfilled request)", location,
                )
            # Path 2: post a sell offer
            ctx.world.pending_offers.append(TradeOffer(
                seller=agent.name,
                buyer=target,
                item=item,
                quantity=qty,
                price=price,
                expires_tick=ctx.world.current_tick() + 3,
            ))
            return WorldEvent(
                ctx.world.day, ctx.world.phase, agent.name, ActionType.SELL,
                f"{agent.name} offers {qty}x{item} to {target} for {price} coins", location,
            )

    return WorldEvent(
        ctx.world.day, ctx.world.phase, agent.name, ActionType.SELL,
        f"{agent.name}'s sell offer failed: {reason}", location,
    )


def _handle_buy(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    target = action.get("target", "")
    location = ctx.world.agent_locations[agent.name]
    raw_detail = action.get("detail", "")
    trade = _parse_trade_detail(raw_detail)

    if not trade or not target:
        return WorldEvent(
            ctx.world.day, ctx.world.phase, agent.name, ActionType.BUY,
            f"{agent.name}'s buy failed: bad detail or missing target", location,
        )

    item, qty, price = trade

    # Path 1: accept an existing sell offer
    matching_offer = next(
        (o for o in ctx.world.pending_offers
         if o.seller == target and o.buyer == agent.name
         and o.item == item and o.quantity == qty and o.price == price),
        None,
    )
    seller = ctx.agent_map.get(target)
    if (matching_offer and seller
            and ctx.world.agent_locations.get(target) == location
            and agent.inventory.get("coins", 0) >= price
            and seller.inventory.get(item, 0) >= qty):
        _decrement_inventory(agent.inventory, "coins", price)
        _increment_inventory(agent.inventory, item, qty)
        _increment_inventory(seller.inventory, "coins", price)
        _decrement_inventory(seller.inventory, item, qty)
        ctx.world.pending_offers.remove(matching_offer)
        return WorldEvent(
            ctx.world.day, ctx.world.phase, agent.name, ActionType.TRADE,
            f"TRADE: {agent.name} bought {qty}x{item} from {target} for {price} coins", location,
        )

    # Path 2: post a buy request
    if target not in ctx.agent_map:
        reason = f"unknown target '{target}'"
    elif ctx.world.agent_locations.get(target) != location:
        target_loc = ctx.world.agent_locations.get(target, "unknown")
        reason = f"{target} is at {target_loc}, not {location}"
    elif agent.inventory.get("coins", 0) < price:
        reason = f"not enough coins (have {agent.inventory.get('coins', 0)}, need {price})"
    else:
        ctx.world.pending_requests.append(BuyRequest(
            buyer=agent.name,
            seller=target,
            item=item,
            quantity=qty,
            price=price,
            expires_tick=ctx.world.current_tick() + 3,
        ))
        return WorldEvent(
            ctx.world.day, ctx.world.phase, agent.name, ActionType.BUY,
            f"{agent.name} requests to buy {qty}x{item} from {target} for {price} coins", location,
        )

    return WorldEvent(
        ctx.world.day, ctx.world.phase, agent.name, ActionType.BUY,
        f"{agent.name}'s buy request failed: {reason}", location,
    )


def _handle_work(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    message = action.get("message", "")

    if agent.role == "Merchant" and location == "Market":
        agent.energy = max(0, agent.energy - 10)
        agent.fulfillment = min(100, agent.fulfillment + 20)
        desc = action.get("message", "working the market stall")
        return WorldEvent(
            ctx.world.day, ctx.world.phase, agent.name, ActionType.WORK,
            f"{agent.name} works: {desc} (-10 energy)", location,
        )

    rule = PRODUCTION_RULES.get((agent.role, location))
    if rule:
        item, qty, cost = rule
        if agent.role == "Farmer" and not _decrement_inventory(agent.inventory, "tools"):
            return WorldEvent(
                ctx.world.day, ctx.world.phase, agent.name, ActionType.WORK,
                f"{agent.name} tried to farm but has no tools!", location,
            )
        agent.energy = max(0, agent.energy - cost)
        _increment_inventory(agent.inventory, item, qty)
        agent.fulfillment = min(100, agent.fulfillment + 20)
        desc = message or "doing their job"
        tool_note = " (used 1 tool)" if agent.role == "Farmer" else ""
        return WorldEvent(
            ctx.world.day, ctx.world.phase, agent.name, ActionType.WORK,
            f"{agent.name} works: {desc} (+{qty} {item}{tool_note}, -{cost} energy)", location,
        )

    return WorldEvent(
        ctx.world.day, ctx.world.phase, agent.name, ActionType.STAY,
        f"{agent.name} has nothing to work on here, stays and observes.", location,
    )


def _handle_stay(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    agent.energy = min(100, agent.energy + 10)
    return WorldEvent(
        ctx.world.day, ctx.world.phase, agent.name, ActionType.STAY,
        f"{agent.name} stays and observes.", location,
    )


def _handle_rest(agent: VillagerAgent, action: dict, ctx: ActionContext) -> WorldEvent:
    location = ctx.world.agent_locations[agent.name]
    gain = REST_ENERGY_NIGHT if ctx.world.phase == TimePhase.NIGHT else REST_ENERGY
    agent.energy = min(100, agent.energy + gain)
    return WorldEvent(
        ctx.world.day, ctx.world.phase, agent.name, ActionType.REST,
        f"{agent.name} rests (+{gain} energy)", location,
    )


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
