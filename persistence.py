from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from world import World, WorldEvent, TradeOffer, TimePhase

if TYPE_CHECKING:
    from agents import VillagerAgent

STATE_FILE = "state.json"


class EventLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._buffer: list[dict] = []

    def log(self, event: WorldEvent) -> None:
        self._buffer.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "day": event.day,
            "phase": event.phase.value,
            "actor": event.actor,
            "action": str(event.action),
            "detail": event.detail,
            "location": event.location,
        })

    def flush(self) -> None:
        if not self._buffer:
            return
        with open(self.log_path, "a") as f:
            for entry in self._buffer:
                f.write(json.dumps(entry) + "\n")
        self._buffer.clear()


def save_state(state_dir: Path, world: World, agents: list[VillagerAgent]) -> None:
    state = {
        "world": world.to_dict(),
        "agents": [
            {
                "name": a.name,
                "role": a.role,
                "energy": a.energy,
                "hunger": a.hunger,
                "sociality": a.sociality,
                "fulfillment": a.fulfillment,
                "inventory": dict(a.inventory),
            }
            for a in agents
        ],
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / STATE_FILE).write_text(json.dumps(state, indent=2))


def load_state(state_dir: Path) -> dict | None:
    path = state_dir / STATE_FILE
    if path.exists():
        return json.loads(path.read_text())
    return None
