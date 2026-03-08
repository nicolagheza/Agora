import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from actions import ActionContext, execute_action
from agents import VillagerAgent
from config import ModelConfig, SimConfig
from economy import compute_market_rates, expire_offers
from persistence import EventLogger, load_state, save_state
from visualization import GameRenderer, build_snapshot
from world import World, WorldEvent, ActionType

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# -- Villager definitions ---------------------------------------------------

VILLAGERS = [
    {
        "name": "Aldric",
        "role": "Farmer",
        "personality": (
            "Hardworking and practical. Wakes up at dawn to tend his fields. "
            "Friendly but quiet, he speaks with a slow, thoughtful cadence. "
            "Enjoys sharing his harvest with neighbors and visiting the tavern after a long day."
        ),
        "location": "Farm",
        "inventory": {"coins": 5, "food": 8, "tools": 8},
    },
    {
        "name": "Bjorn",
        "role": "Blacksmith",
        "personality": (
            "Strong and stoic with a booming laugh. Proud of his craft and always "
            "tinkering with new designs. Has a soft spot for ale and storytelling. "
            "Protective of the village and its people."
        ),
        "location": "Blacksmith",
        "inventory": {"coins": 10, "food": 4, "tools": 3},
    },
    {
        "name": "Elena",
        "role": "Merchant",
        "personality": (
            "Sharp-witted and charming. Knows everyone's business and loves to gossip. "
            "A shrewd trader who always finds a bargain. Social butterfly who connects "
            "people and spreads news around the village."
        ),
        "location": "Market",
        "inventory": {"coins": 20, "food": 2, "tools": 1},
    },
]

# -- Conversation phase ----------------------------------------------------


def run_conversation_phase(agents: list[VillagerAgent], world: World, logger: EventLogger):
    locations_with_agents: dict[str, list[VillagerAgent]] = {}
    for agent in agents:
        loc = world.agent_locations[agent.name]
        locations_with_agents.setdefault(loc, []).append(agent)

    for location, agents_here in locations_with_agents.items():
        if len(agents_here) < 2:
            continue
        if not any(a.sociality < 50 for a in agents_here):
            continue

        initiator = min(agents_here, key=lambda a: a.sociality)
        others = [a for a in agents_here if a != initiator]

        dialogue_lines: list[str] = []
        participants = [initiator] + others
        for i in range(4):
            speaker = participants[i % len(participants)]
            other_names = [a.name for a in agents_here if a != speaker]
            message = speaker.converse(world, other_names, dialogue_lines)
            dialogue_lines.append(f"{speaker.name}: {message}")

            event = WorldEvent(
                world.day, world.phase, speaker.name, ActionType.TALK,
                f'{speaker.name} says: "{message}"', location,
            )
            world.add_event(event)
            logger.log(event)

        exchanges = len(dialogue_lines)
        for agent in agents_here:
            agent.sociality = min(100, agent.sociality + 15 * (exchanges // 2))


# -- Needs decay -----------------------------------------------------------


def apply_needs_decay(agents: list[VillagerAgent], cfg: SimConfig):
    for agent in agents:
        agent.hunger = max(0, agent.hunger - 5)
        agent.sociality = max(0, agent.sociality - 5)
        agent.fulfillment = max(0, agent.fulfillment - 3)
        if agent.is_starving:
            agent.energy = min(agent.energy, cfg.starving_energy_cap)


# -- Simulation loop (runs in background thread) ---------------------------


def sim_loop(
    renderer: GameRenderer,
    world: World,
    agents: list[VillagerAgent],
    sim: SimConfig,
    logger: EventLogger,
    ctx: ActionContext,
):
    # Publish initial snapshot so the renderer shows something immediately
    prev_locations = dict(world.agent_locations)
    renderer.publish_snapshot(build_snapshot(world, agents, prev_locations, [], sim.tick_delay))

    while not renderer.is_stopping():
        renderer.wait_if_paused()
        if renderer.is_stopping():
            break

        # Snapshot agent locations before any moves this tick
        prev_locations = dict(world.agent_locations)

        # Compute market rates and inventory snapshot for agent context
        world.market_rates = compute_market_rates(agents, sim.base_prices)
        world.agent_inventories = {a.name: dict(a.inventory) for a in agents}

        # 1. Parallel LLM decisions
        decisions: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=len(agents)) as pool:
            futures = {pool.submit(agent.decide, world): agent for agent in agents}
            for future in as_completed(futures):
                if renderer.is_stopping():
                    break
                agent = futures[future]
                try:
                    decisions[agent.name] = future.result()
                except Exception as exc:
                    logging.warning("%s decision failed: %s", agent.name, exc)
                    decisions[agent.name] = {
                        "thought": "...", "action": ActionType.REST,
                        "target": "", "message": "",
                    }

        if renderer.is_stopping():
            break

        # 2. Execute actions — MOVEs first to settle locations
        last_thoughts: list[tuple[str, dict]] = []
        for agent in agents:
            action = decisions[agent.name]
            last_thoughts.append((agent.name, action))
            if action.get("action", "").upper() == ActionType.MOVE:
                execute_action(agent, action, ctx)

        for agent in agents:
            action = decisions[agent.name]
            if action.get("action", "").upper() != ActionType.MOVE:
                execute_action(agent, action, ctx)

        # 3. Expire pending trade offers
        expire_offers(world, logger.log)

        # 4. Conversation phase
        run_conversation_phase(agents, world, logger)

        # 5. Needs decay
        apply_needs_decay(agents, sim)

        # 6. Advance time + flush log
        world.advance_time()
        logger.flush()

        # 7. Publish snapshot for renderer
        renderer.publish_snapshot(
            build_snapshot(world, agents, prev_locations, last_thoughts, sim.tick_delay)
        )

        # 8. Wait for next tick (interruptible)
        deadline = time.monotonic() + sim.tick_delay
        while time.monotonic() < deadline:
            if renderer.is_stopping():
                break
            time.sleep(0.05)


# -- Main ------------------------------------------------------------------


def main():
    sim = SimConfig()
    model = ModelConfig()
    state_dir = Path(sim.state_dir)
    log_path = Path(sim.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    saved = load_state(state_dir)
    resuming = bool(saved)

    world = World.from_dict(saved["world"]) if resuming else World(village_name=sim.village_name)
    state_dir.mkdir(parents=True, exist_ok=True)

    agents: list[VillagerAgent] = []
    for v in VILLAGERS:
        agent = VillagerAgent(
            name=v["name"],
            role=v["role"],
            personality=v["personality"],
            starting_location=v["location"],
            model_config=model,
            village_name=sim.village_name,
            inventory=dict(v["inventory"]),
        )
        agents.append(agent)

        if not resuming:
            world.agent_locations[agent.name] = agent.starting_location

    if resuming:
        saved_agents = {a["name"]: a for a in saved["agents"]}
        for agent in agents:
            if agent.name in saved_agents:
                s = saved_agents[agent.name]
                agent.energy = s["energy"]
                agent.hunger = s.get("hunger", 100)
                agent.sociality = s.get("sociality", 100)
                agent.fulfillment = s.get("fulfillment", 100)
                agent.inventory = s["inventory"]

    agent_map = {a.name: a for a in agents}
    logger = EventLogger(log_path)
    ctx = ActionContext(world=world, agent_map=agent_map, cfg=sim, log=logger.log)

    renderer = GameRenderer(sim)

    def _on_signal(*_):
        renderer.request_stop()

    signal.signal(signal.SIGINT, _on_signal)

    # Start simulation in background daemon thread
    sim_thread = threading.Thread(
        target=sim_loop,
        args=(renderer, world, agents, sim, logger, ctx),
        daemon=True,
        name="sim-loop",
    )
    sim_thread.start()

    # Pygame event loop on main thread (blocks until window closed)
    renderer.run()

    # Cleanup after window closes
    sim_thread.join(timeout=5)
    save_state(state_dir, world, agents)
    logger.flush()
    print(f"\nState saved. Run again to resume from Day {world.day}, {world.phase.value}.\n")


if __name__ == "__main__":
    main()
