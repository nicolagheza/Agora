from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    # Provider: "lmstudio" or "openai"
    provider: str = "openai"
    model_id: str = "gpt-5-nano-2025-08-07" # "qwen/qwen3.5-9b"
    # Token budget for the model context window.
    # LMStudio: check Model Info panel. OpenAI: gpt-4o/gpt-4o-mini = 128000.
    context_window: int = 8192


@dataclass
class SimConfig:
    village_name: str = "Embervale"
    tick_delay: float = 2.0
    state_dir: str = "tmp"
    log_file: str = "logs/events.jsonl"
    base_prices: dict = field(default_factory=lambda: {"food": 3, "tools": 8})
    hunger_restore: int = 35
    starving_energy_cap: int = 50
