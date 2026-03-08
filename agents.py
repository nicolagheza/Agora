from __future__ import annotations

import json
import re

from agno.agent import Agent
from agno.models.lmstudio import LMStudio
from agno.models.openai import OpenAIChat

from config import ModelConfig
from world import World

SYSTEM_PROMPT = """\
You are {name}, the {role} of {village} village.

Personality: {personality}

You live in a small medieval village. Each turn you observe your surroundings and decide what to do.

You MUST respond with ONLY a valid JSON object (no markdown, no extra text):
{{
  "thought": "your inner reasoning about what to do",
  "action": "MOVE or WORK or REST or STAY or EAT or SELL or BUY",
  "target": "location name if MOVE, agent name if SELL/BUY, otherwise empty string",
  "detail": "for SELL/BUY: item:qty:price e.g. food:2:6, otherwise empty string",
  "message": "what you do if WORK, otherwise empty string"
}}

Available actions:
- MOVE: Walk to an adjacent location. target must be one of the connected locations listed.
- WORK: Perform work at your workplace. Only produces items at your designated craft location. Costs energy and restores fulfillment.
- REST: Take a break and recover energy. Recovers more during NIGHT.
- STAY: Remain at your current location, observing and available. Small energy recovery.
- EAT: Consume 1 food from your inventory to restore hunger. No target or detail needed.
- SELL: Offer items for sale. For a P2P offer to someone at your location, set target=<their name>. For an open market offer that anyone can accept from anywhere, set target="" or target="market". Either way, set detail="item:qty:price" (e.g. "food:2:6" = 2 food for 6 coins). If the counterparty has a matching buy request, the trade completes immediately; otherwise your offer is posted.
- BUY: Request or accept a trade. To accept an existing sell offer from a specific agent (no co-presence needed), set target=<their name> and detail matching their offer — completes immediately. To post an open market request that anyone can fulfill from anywhere, set target="" or target="market". To post a P2P buy request, set target=<agent name at your location> and detail="item:qty:price".

You have four needs (0-100, higher is better):
- Hunger: Decays over time. Use EAT to restore it when you have food.
- Energy: Lost by working. Rest to restore it.
- Sociality: Decays over time. Restored by conversations with other villagers.
- Fulfillment: Decays over time. Restored by working at your craft location.

When a need is low, prioritize restoring it.
When your sociality is low, consider moving to social locations like the Tavern or Town Square to meet others.
When hunger is low and you have food, use EAT.
To trade P2P: both agents must be at the same location to post a new offer. Once posted, the counterparty can accept with BUY from anywhere — no co-presence needed for acceptance.
To trade via market: use target="" with SELL or BUY to post an open order — no meeting required, any agent can accept from anywhere. Use market orders when the person you need is far away.

Guidelines:
- Stay in character as {name} the {role} at all times.
- Make realistic decisions based on the time of day, your energy, and your inventory.
- During NIGHT you should REST (sleep).
- During DAWN consider waking up and starting your day.
- Conversations with nearby villagers happen naturally — focus on positioning yourself where you want to be.
- If you are a producer (Farmer, Blacksmith): WORK is your most important action. Do not skip farming or smithing just because goods are currently available on the market — supply will run out if you stop producing. Check your [DUTY] reminder each turn.
- Do not rely on the market as a substitute for doing your own job. Market orders expire and supply is limited.
- Respond ONLY with the JSON object, nothing else.
"""

CONVERSATION_PROMPT = """\
You are {name}, the {role}.

It is currently {time}.
You are at {location} with {others}.

{dialogue_so_far}

Respond in character as {name}. Say what you would naturally say in this moment.
Keep your response to 1-2 sentences. Respond with ONLY your dialogue, no JSON, no quotes.
"""

DEFAULT_ACTION: dict = {"thought": "...", "action": "REST", "target": "", "message": ""}


def _parse_action(text: str) -> dict:
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if "action" in data:
                data["action"] = data["action"].upper()
                return data
    except (json.JSONDecodeError, AttributeError):
        pass
    return dict(DEFAULT_ACTION)


class VillagerAgent:
    def __init__(
        self,
        name: str,
        role: str,
        personality: str,
        starting_location: str,
        model_config: ModelConfig,
        village_name: str = "Embervale",
        inventory: dict[str, int] | None = None,
        energy: int = 100,
        hunger: int = 100,
        sociality: int = 100,
        fulfillment: int = 100,
    ):
        self.name = name
        self.role = role
        self.starting_location = starting_location
        self.inventory: dict[str, int] = inventory if inventory else {"coins": 10}
        self.energy = energy
        self.hunger = hunger
        self.sociality = sociality
        self.fulfillment = fulfillment
        # Reserve tokens for system prompt + model response (~3 chars per token).
        self._context_chars_budget = max(2000, (model_config.context_window - 800) * 3)
        if model_config.provider == "openai":
            model = OpenAIChat(id=model_config.model_id)
            converse_model = OpenAIChat(id=model_config.model_id)
        else:
            model = LMStudio(id=model_config.model_id)
            converse_model = LMStudio(id=model_config.model_id)
        self.agent = Agent(
            model=model,
            instructions=SYSTEM_PROMPT.format(
                name=name, role=role, personality=personality, village=village_name,
            ),
            markdown=False,
        )
        # Separate agent for conversations — no JSON instruction so it replies in plain prose.
        self._converse_agent = Agent(model=converse_model, markdown=False)

    @property
    def is_starving(self) -> bool:
        return self.hunger <= 0 and self.inventory.get("food", 0) == 0

    def _build_context(self, world: World) -> str:
        location_name = world.agent_locations[self.name]
        loc = world.locations[location_name]
        others = [n for n in world.get_agents_at(location_name) if n != self.name]
        events = world.get_visible_events(self.name)

        inv_parts = [f"{qty} {item}" for item, qty in self.inventory.items() if qty > 0]
        inv_str = ", ".join(inv_parts) if inv_parts else "(nothing)"

        needs = {
            "Hunger": self.hunger,
            "Energy": self.energy,
            "Sociality": self.sociality,
            "Fulfillment": self.fulfillment,
        }
        needs_lines = []
        for label, value in needs.items():
            flag = " [LOW!]" if value < 30 else ""
            needs_lines.append(f"  {label}: {value}/100{flag}")

        lines = [
            "=== Current Situation ===",
            f"Time: Day {world.day}, {world.phase.value}",
            f"Location: {location_name} ({loc.description})",
            f"Connected locations: {', '.join(loc.connections)}",
            f"People here: {', '.join(others) if others else '(nobody else)'}",
            "Needs:",
            *needs_lines,
            f"Inventory: {inv_str}",
        ]

        # Economy section
        if world.market_rates:
            rate_parts = [f"{item} ~{price}c" for item, price in world.market_rates.items()]
            lines.append(f"Market rates: {', '.join(rate_parts)}")

        offers_to_me = [o for o in world.pending_offers if o.buyer == self.name]
        my_offers = [o for o in world.pending_offers if o.seller == self.name]
        requests_to_me = [r for r in world.pending_requests if r.seller == self.name]
        my_requests = [r for r in world.pending_requests if r.buyer == self.name]
        open_offers = [o for o in world.pending_offers if o.buyer is None and o.seller != self.name]
        open_requests = [r for r in world.pending_requests if r.seller is None and r.buyer != self.name]
        if offers_to_me:
            lines.append("Pending sell offers TO YOU (use BUY target=<seller> to accept from anywhere):")
            for o in offers_to_me:
                lines.append(f"  {o.seller} offers {o.quantity}x{o.item} for {o.price} coins")
        if my_offers:
            lines.append("Your pending sell offers:")
            for o in my_offers:
                buyer_label = o.buyer if o.buyer else "market (open)"
                lines.append(f"  Selling {o.quantity}x{o.item} to {buyer_label} for {o.price} coins")
        if requests_to_me:
            lines.append("Buy requests FROM others (use SELL target=<buyer> to fulfill from anywhere):")
            for r in requests_to_me:
                lines.append(f"  {r.buyer} wants to buy {r.quantity}x{r.item} for {r.price} coins")
        if my_requests:
            lines.append("Your pending buy requests:")
            for r in my_requests:
                seller_label = r.seller if r.seller else "market (open)"
                lines.append(f"  Requested {r.quantity}x{r.item} from {seller_label} for {r.price} coins")
        if open_offers:
            lines.append("Open market sell offers (use BUY target=<seller name> to accept from anywhere):")
            for o in open_offers:
                lines.append(f"  {o.seller} offers {o.quantity}x{o.item} for {o.price} coins [open market]")
        if open_requests:
            lines.append("Open market buy requests (use SELL target=<buyer name> to fulfill from anywhere):")
            for r in open_requests:
                lines.append(f"  {r.buyer} wants {r.quantity}x{r.item} for {r.price} coins [open market]")

        if self.role == "Merchant" and location_name == "Market" and world.agent_inventories:
            lines.append("Agent inventories (market insight):")
            for name, inv in world.agent_inventories.items():
                if name != self.name:
                    inv_parts = [f"{qty} {item}" for item, qty in inv.items() if qty > 0]
                    lines.append(f"  {name}: {', '.join(inv_parts) if inv_parts else 'nothing'}")

        if self.role == "Farmer":
            tools = self.inventory.get("tools", 0)
            if tools == 0:
                lines.append("[WARNING] You have NO tools — you cannot farm! Your PRIMARY task right now is to buy tools from Bjorn the Blacksmith (post a market BUY request or go to the Blacksmith to trade directly).")
            else:
                food_in_inv = self.inventory.get("food", 0)
                lines.append(f"[DUTY] Your PRIMARY job is to farm. You have {tools} tool(s) and {food_in_inv} food. WORK at the Farm to produce food — the village depends on your harvest. Only skip farming if your energy or hunger is critically low.")
        elif self.role == "Blacksmith":
            tools_in_inv = self.inventory.get("tools", 0)
            lines.append(f"[DUTY] Your PRIMARY job is to forge tools. You have {tools_in_inv} tools in stock. WORK at the Blacksmith to produce tools — the Farmer cannot grow food without them. Only skip smithing if your energy or hunger is critically low.")

        lines.append("")
        lines.append("What do you do?")
        base = "\n".join(lines)

        if not events:
            return base

        # Add recent events only while within the context budget.
        event_lines = [f"- [{e.phase.value}] {e.detail}" for e in events]
        header = "Recent events:"
        available = self._context_chars_budget - len(base) - len(header) - 1
        kept: list[str] = []
        for line in reversed(event_lines):
            if len(line) + 1 > available:
                break
            kept.append(line)
            available -= len(line) + 1
        if kept:
            lines.insert(-1, header)
            lines[-1:-1] = reversed(kept)

        return "\n".join(lines)

    def converse(self, world: World, other_names: list[str], dialogue_lines: list[str]) -> str:
        location_name = world.agent_locations[self.name]
        others_str = ", ".join(other_names) if other_names else "(nobody)"

        if dialogue_lines:
            dialogue_so_far = "Conversation so far:\n" + "\n".join(dialogue_lines)
        else:
            dialogue_so_far = "Start a conversation."

        time_str = f"Day {world.day}, {world.phase.value}"
        prompt = CONVERSATION_PROMPT.format(
            name=self.name,
            role=self.role,
            time=time_str,
            location=location_name,
            others=others_str,
            dialogue_so_far=dialogue_so_far,
        )

        try:
            response = self._converse_agent.run(prompt)
            text = response.content.strip() if response.content else "..."
            # Remove any quotes the LLM might wrap the response in
            if text.startswith('"') and text.endswith('"'):
                text = text[1:-1]
            # Fallback: if the model returned JSON despite the prompt, extract message/thought
            if text.startswith("{"):
                try:
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    if match:
                        data = json.loads(match.group())
                        text = data.get("message") or data.get("thought") or "..."
                except (json.JSONDecodeError, AttributeError):
                    pass
            return text
        except Exception:
            return "..."

    def decide(self, world: World) -> dict:
        context = self._build_context(world)

        try:
            response = self.agent.run(context)
            text = response.content if response.content else ""
        except Exception:
            return dict(DEFAULT_ACTION)

        return _parse_action(text)
