from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import pygame

from config import SimConfig

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

WINDOW_W, WINDOW_H = 1280, 720
MAP_W = 800
SIDEBAR_X = 800
SIDEBAR_W = 480
FPS = 60

# ---------------------------------------------------------------------------
# Building geometry (pixel space within the 800px map panel)
# ---------------------------------------------------------------------------

_BUILDING_RECT_DEFS: dict[str, tuple[int, int, int, int]] = {
    "Farm":        (  60,  80, 160, 110),
    "River":       ( 560,  80, 160, 110),
    "Town Square": ( 280, 290, 220, 120),
    "Market":      ( 560, 290, 160, 110),
    "Blacksmith":  (  60, 510, 160, 110),
    "Tavern":      ( 560, 510, 160, 110),
}

BUILDING_CENTERS: dict[str, tuple[int, int]] = {
    name: (x + w // 2, y + h // 2)
    for name, (x, y, w, h) in _BUILDING_RECT_DEFS.items()
}

ROADS: list[tuple[str, str]] = [
    ("Farm", "Town Square"),
    ("Farm", "River"),
    ("Town Square", "Market"),
    ("Town Square", "Blacksmith"),
    ("Town Square", "Tavern"),
    ("Market", "Tavern"),
    ("Tavern", "River"),
]

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

BUILDING_COLORS: dict[str, tuple[int, int, int]] = {
    "Farm":        ( 72, 160,  72),
    "River":       ( 60, 130, 200),
    "Town Square": (200, 185, 155),
    "Market":      (210, 170,  50),
    "Blacksmith":  (160,  60,  50),
    "Tavern":      (160, 100,  40),
}

BUILDING_BORDER_COLORS: dict[str, tuple[int, int, int]] = {
    "Farm":        ( 40, 100,  40),
    "River":       ( 30,  80, 160),
    "Town Square": (140, 120,  90),
    "Market":      (150, 110,  20),
    "Blacksmith":  (100,  30,  20),
    "Tavern":      (110,  60,  10),
}

BUILDING_ATMOSPHERE: dict[str, str] = {
    "Farm":        "crops sway gently",
    "Town Square": "fountain bubbles",
    "Market":      "stalls await",
    "Blacksmith":  "embers glow",
    "Tavern":      "mugs on tables",
    "River":       "water flows past",
}

PHASE_SKY: dict[str, tuple[int, int, int]] = {
    "Dawn":      ( 80,  60, 100),
    "Morning":   (135, 180, 220),
    "Afternoon": (100, 160, 220),
    "Evening":   (180, 100,  50),
    "Night":     ( 20,  25,  40),
}

PHASE_TEXT_COLORS: dict[str, tuple[int, int, int]] = {
    "Dawn":      (220, 180, 255),
    "Morning":   (255, 255, 180),
    "Afternoon": (255, 255, 255),
    "Evening":   (255, 180,  80),
    "Night":     (150, 150, 200),
}

AGENT_COLORS: dict[str, tuple[int, int, int]] = {
    "Farmer":     (100, 200, 100),
    "Blacksmith": (220,  80,  70),
    "Merchant":   (200,  80, 200),
}

ROAD_COLOR        = (120, 105,  85)
ROAD_WIDTH        = 12
GRASS_COLOR       = ( 45,  55,  40)
SHADOW_COLOR      = ( 20,  20,  20)

SIDEBAR_BG        = ( 20,  20,  35)
SIDEBAR_CARD_BG   = ( 28,  28,  48)
SIDEBAR_TEXT      = (220, 220, 230)
SIDEBAR_DIM       = (120, 120, 140)
DIVIDER_COLOR     = ( 60,  60,  80)

BAR_BG     = ( 50,  50,  60)
BAR_GREEN  = ( 80, 200,  80)
BAR_YELLOW = (220, 180,  50)
BAR_RED    = (220,  60,  60)

AGENT_RADIUS         = 14
AGENT_BORDER         = 3
AGENT_STARVING_BORDER: tuple[int, int, int] = (255,  50,  50)

EVENT_COLORS: dict[str, tuple[int, int, int]] = {
    "TRADE":          ( 80, 220,  80),
    "EAT":            (220, 200,  80),
    "SELL":           ( 80, 200, 220),
    "BUY":            ( 80, 200, 220),
    "OFFER_EXPIRED":  (100, 100, 120),
    "WORK":           (180, 180, 220),
    "TALK":           (180, 140, 220),
}
EVENT_DEFAULT_COLOR: tuple[int, int, int] = (180, 180, 180)

# ---------------------------------------------------------------------------
# Snapshot dataclasses  (immutable — safe to pass across threads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSnapshot:
    name: str
    role: str
    location: str
    prev_location: str      # location at start of this tick, for lerp
    energy: int
    hunger: int
    sociality: int
    fulfillment: int
    inventory: tuple        # tuple of (key, value) pairs — hashable
    is_starving: bool
    last_thought: str
    last_action: str
    last_target: str


@dataclass(frozen=True)
class WorldSnapshot:
    village_name: str
    day: int
    phase: str
    agent_snapshots: tuple          # tuple[AgentSnapshot, ...]
    recent_events: tuple            # tuple[(action_str, detail_str), ...]
    tick_timestamp: float           # time.monotonic() when published
    tick_duration: float            # sim.tick_delay, for lerp math


def build_snapshot(
    world,
    agents: list,
    prev_locations: dict[str, str],
    last_thoughts: list[tuple[str, dict]],
    tick_delay: float,
) -> WorldSnapshot:
    """Build an immutable snapshot from live simulation objects."""
    thought_map = {name: action for name, action in last_thoughts}
    agent_snaps = tuple(
        AgentSnapshot(
            name=a.name,
            role=a.role,
            location=world.agent_locations[a.name],
            prev_location=prev_locations.get(a.name, world.agent_locations[a.name]),
            energy=a.energy,
            hunger=a.hunger,
            sociality=a.sociality,
            fulfillment=a.fulfillment,
            inventory=tuple(sorted(a.inventory.items())),
            is_starving=a.is_starving,
            last_thought=thought_map.get(a.name, {}).get("thought", ""),
            last_action=thought_map.get(a.name, {}).get("action", ""),
            last_target=thought_map.get(a.name, {}).get("target", ""),
        )
        for a in agents
    )
    recent_events = tuple(
        (str(e.action), e.detail)
        for e in world.events[-10:]
    )
    return WorldSnapshot(
        village_name=world.village_name,
        day=world.day,
        phase=world.phase.value,
        agent_snapshots=agent_snaps,
        recent_events=recent_events,
        tick_timestamp=time.monotonic(),
        tick_duration=tick_delay,
    )


# ---------------------------------------------------------------------------
# GameRenderer
# ---------------------------------------------------------------------------


class GameRenderer:
    """
    Pygame-based 2D top-down renderer.

    Threading model:
      Main thread  → run()          owns pygame event loop at 60 fps
      Sim thread   → publish_snapshot()  swaps snapshot behind a lock
    """

    def __init__(self, sim: SimConfig):
        self._tick_delay = sim.tick_delay
        self._lock = threading.Lock()
        self._snapshot: WorldSnapshot | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._inspected_agent: str | None = None
        self._screen: pygame.Surface | None = None
        self._fonts: dict[str, pygame.font.Font] = {}

    # -- Public API: main thread --------------------------------------------

    def run(self) -> None:
        """Blocking. Must be called from the OS main thread."""
        pygame.init()
        pygame.display.set_caption("Agora — Embervale")
        self._screen = pygame.display.set_mode((WINDOW_W, WINDOW_H))
        self._init_fonts()
        clock = pygame.time.Clock()
        while not self._stop.is_set():
            self._handle_events()
            self._draw()
            pygame.display.flip()
            clock.tick(FPS)
        pygame.quit()

    # -- Public API: sim thread --------------------------------------------

    def publish_snapshot(self, snap: WorldSnapshot) -> None:
        """Thread-safe snapshot swap. Never blocks."""
        with self._lock:
            self._snapshot = snap

    def request_stop(self) -> None:
        self._stop.set()

    def is_stopping(self) -> bool:
        return self._stop.is_set()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def wait_if_paused(self) -> None:
        """Sim thread calls this to block while paused."""
        while self._paused.is_set() and not self._stop.is_set():
            time.sleep(0.05)

    # -- Internal -----------------------------------------------------------

    def _get_snapshot(self) -> WorldSnapshot | None:
        with self._lock:
            return self._snapshot

    def _init_fonts(self) -> None:
        for size_name, pt, bold in [
            ("large",  20, False),
            ("medium", 15, False),
            ("small",  12, False),
            ("tiny",   10, False),
            ("bold",   15, True),
        ]:
            try:
                self._fonts[size_name] = pygame.font.SysFont("monospace", pt, bold=bold)
            except Exception:
                self._fonts[size_name] = pygame.font.Font(None, pt + 8)

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._stop.set()
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    if self._inspected_agent:
                        self._inspected_agent = None
                    else:
                        self._stop.set()
                elif event.key == pygame.K_p:
                    if self._paused.is_set():
                        self._paused.clear()
                    else:
                        self._paused.set()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                snap = self._get_snapshot()
                if self._inspected_agent:
                    self._inspected_agent = None
                elif snap:
                    self._inspected_agent = self._get_agent_at_pixel(
                        event.pos[0], event.pos[1], snap
                    )

    # -- Drawing ------------------------------------------------------------

    def _draw(self) -> None:
        snap = self._get_snapshot()
        screen = self._screen

        # Sky gradient: top half phase-coloured, bottom half grass
        sky = PHASE_SKY.get(snap.phase if snap else "Morning", GRASS_COLOR)
        screen.fill(sky, pygame.Rect(0, 0, MAP_W, WINDOW_H // 2))
        screen.fill(GRASS_COLOR, pygame.Rect(0, WINDOW_H // 2, MAP_W, WINDOW_H // 2))

        # Sidebar background + divider
        pygame.draw.rect(screen, SIDEBAR_BG, pygame.Rect(SIDEBAR_X, 0, SIDEBAR_W, WINDOW_H))
        pygame.draw.rect(screen, DIVIDER_COLOR, pygame.Rect(SIDEBAR_X - 2, 0, 2, WINDOW_H))

        self._draw_roads()
        self._draw_buildings(snap)

        if snap:
            self._draw_agents(snap)
            self._draw_sidebar(snap)
        else:
            self._draw_loading()

        if self._inspected_agent and snap:
            self._draw_inspect_overlay(snap)

        if self._paused.is_set():
            self._draw_pause_banner()

    def _draw_loading(self) -> None:
        font = self._fonts["large"]
        surf = font.render("Starting simulation...", True, (200, 200, 200))
        self._screen.blit(surf, (MAP_W // 2 - surf.get_width() // 2, WINDOW_H // 2))

    def _draw_roads(self) -> None:
        for loc_a, loc_b in ROADS:
            pygame.draw.line(
                self._screen, ROAD_COLOR,
                BUILDING_CENTERS[loc_a], BUILDING_CENTERS[loc_b],
                ROAD_WIDTH,
            )

    def _draw_buildings(self, snap: WorldSnapshot | None) -> None:
        for name, (x, y, w, h) in _BUILDING_RECT_DEFS.items():
            rect = pygame.Rect(x, y, w, h)
            color = BUILDING_COLORS[name]
            border = BUILDING_BORDER_COLORS[name]

            # Drop shadow
            pygame.draw.rect(self._screen, SHADOW_COLOR, rect.move(4, 4), border_radius=4)
            # Fill + border
            pygame.draw.rect(self._screen, color, rect, border_radius=4)
            pygame.draw.rect(self._screen, border, rect, width=2, border_radius=4)

            # Name label centred at top
            font = self._fonts["medium"]
            label = font.render(name, True, (255, 255, 255))
            self._screen.blit(label, (x + (w - label.get_width()) // 2, y + 6))

            # Atmosphere text when empty
            agents_here = [a for a in snap.agent_snapshots if a.location == name] if snap else []
            if not agents_here:
                atmo = BUILDING_ATMOSPHERE.get(name, "")
                tiny = self._fonts["tiny"]
                atmo_surf = tiny.render(atmo, True, (200, 200, 200))
                self._screen.blit(
                    atmo_surf,
                    (x + (w - atmo_surf.get_width()) // 2, y + h - 20),
                )

    def _agent_screen_pos(
        self, agent: AgentSnapshot, snap: WorldSnapshot, by_location: dict[str, list[AgentSnapshot]]
    ) -> tuple[int, int]:
        """Return the actual drawn position of an agent, including group arc-offset."""
        pos = self._interpolated_pos(agent, snap)
        group = by_location.get(agent.location, [agent])
        n = len(group)
        if n > 1:
            i = group.index(agent)
            angle = 2 * math.pi / n * i
            pos = (
                pos[0] + int(math.cos(angle) * 30),
                pos[1] + int(math.sin(angle) * 30),
            )
        return pos

    def _draw_agents(self, snap: WorldSnapshot) -> None:
        # Group by destination location for arc-offset calculation
        by_location: dict[str, list[AgentSnapshot]] = {}
        for agent in snap.agent_snapshots:
            by_location.setdefault(agent.location, []).append(agent)
        for agents in by_location.values():
            agents.sort(key=lambda a: a.name)   # deterministic order

        for agent in snap.agent_snapshots:
            base_pos = self._agent_screen_pos(agent, snap, by_location)

            color = AGENT_COLORS.get(agent.role, (200, 200, 200))
            border = AGENT_STARVING_BORDER if agent.is_starving else (255, 255, 255)

            # Shadow → border ring → fill → initial
            pygame.draw.circle(self._screen, SHADOW_COLOR, (base_pos[0] + 2, base_pos[1] + 2), AGENT_RADIUS)
            pygame.draw.circle(self._screen, border, base_pos, AGENT_RADIUS + AGENT_BORDER)
            pygame.draw.circle(self._screen, color, base_pos, AGENT_RADIUS)

            letter = self._fonts["bold"].render(agent.name[0], True, (0, 0, 0))
            self._screen.blit(
                letter,
                (base_pos[0] - letter.get_width() // 2, base_pos[1] - letter.get_height() // 2),
            )

            # Name tag above agent
            name_surf = self._fonts["small"].render(agent.name, True, (255, 255, 255))
            shadow_surf = self._fonts["small"].render(agent.name, True, (0, 0, 0))
            tx = base_pos[0] - name_surf.get_width() // 2
            ty = base_pos[1] - AGENT_RADIUS - AGENT_BORDER - name_surf.get_height() - 2
            self._screen.blit(shadow_surf, (tx + 1, ty + 1))
            self._screen.blit(name_surf, (tx, ty))

    def _interpolated_pos(self, agent: AgentSnapshot, snap: WorldSnapshot) -> tuple[int, int]:
        elapsed = time.monotonic() - snap.tick_timestamp
        t = min(1.0, elapsed / max(snap.tick_duration, 0.001))
        t = t * t * (3 - 2 * t)    # smoothstep easing
        start = BUILDING_CENTERS[agent.prev_location]
        end = BUILDING_CENTERS[agent.location]
        return (
            int(start[0] + (end[0] - start[0]) * t),
            int(start[1] + (end[1] - start[1]) * t),
        )

    # -- Sidebar ------------------------------------------------------------

    def _draw_sidebar(self, snap: WorldSnapshot) -> None:
        screen = self._screen
        x0 = SIDEBAR_X + 14
        w = SIDEBAR_W - 28

        phase_color = PHASE_TEXT_COLORS.get(snap.phase, (220, 220, 230))

        # Header
        village_surf = self._fonts["large"].render(snap.village_name, True, (255, 255, 255))
        screen.blit(village_surf, (x0, 12))

        phase_text = f"Day {snap.day}  ·  {snap.phase}"
        phase_surf = self._fonts["bold"].render(phase_text, True, phase_color)
        screen.blit(phase_surf, (x0, 34))

        pygame.draw.line(screen, DIVIDER_COLOR, (SIDEBAR_X + 8, 60), (SIDEBAR_X + SIDEBAR_W - 8, 60), 1)

        # Agent cards
        card_y = 68
        for agent in snap.agent_snapshots:
            self._draw_agent_card(agent, x0, card_y, w)
            card_y += 120

        # Separator before events
        sep_y = card_y + 4
        pygame.draw.line(screen, DIVIDER_COLOR, (SIDEBAR_X + 8, sep_y), (SIDEBAR_X + SIDEBAR_W - 8, sep_y), 1)

        # Events
        events_y = sep_y + 10
        events_label = self._fonts["bold"].render("Events", True, (180, 180, 220))
        screen.blit(events_label, (x0, events_y))
        events_y += 22

        for action_type, detail in snap.recent_events:
            color = EVENT_COLORS.get(action_type, EVENT_DEFAULT_COLOR)
            truncated = detail[:56] + "…" if len(detail) > 56 else detail
            surf = self._fonts["tiny"].render(truncated, True, color)
            screen.blit(surf, (x0, events_y))
            events_y += 20
            if events_y > WINDOW_H - 28:
                break

        # Key hints at very bottom
        hints = "[P] Pause    [Esc] Quit    [Click] Inspect"
        hint_surf = self._fonts["tiny"].render(hints, True, SIDEBAR_DIM)
        screen.blit(
            hint_surf,
            (SIDEBAR_X + (SIDEBAR_W - hint_surf.get_width()) // 2, WINDOW_H - 16),
        )

    def _draw_agent_card(self, agent: AgentSnapshot, x: int, y: int, w: int) -> None:
        screen = self._screen
        color = AGENT_COLORS.get(agent.role, (200, 200, 200))

        # Card background
        card_rect = pygame.Rect(x - 4, y - 2, w + 8, 114)
        pygame.draw.rect(screen, SIDEBAR_CARD_BG, card_rect, border_radius=4)
        # Coloured left accent bar
        accent_rect = pygame.Rect(x - 4, y - 2, 3, 114)
        pygame.draw.rect(screen, color, accent_rect, border_radius=2)

        # Name + role
        name_surf = self._fonts["bold"].render(agent.name, True, color)
        screen.blit(name_surf, (x + 4, y + 2))
        role_surf = self._fonts["small"].render(f"({agent.role})", True, SIDEBAR_DIM)
        screen.blit(role_surf, (x + 4 + name_surf.get_width() + 6, y + 4))
        if agent.is_starving:
            s_surf = self._fonts["small"].render("STARVING", True, (255, 50, 50))
            screen.blit(s_surf, (x + w - s_surf.get_width(), y + 2))

        # Need bars
        bar_y = y + 22
        self._draw_need_bar(screen, x + 4, bar_y, "H", agent.hunger, 72)
        self._draw_need_bar(screen, x + 110, bar_y, "E", agent.energy, 72)
        bar_y2 = y + 40
        self._draw_need_bar(screen, x + 4, bar_y2, "S", agent.sociality, 72)
        self._draw_need_bar(screen, x + 110, bar_y2, "F", agent.fulfillment, 72)

        # Location + inventory
        inv = dict(agent.inventory)
        inv_parts = [f"{v}{k[0].upper()}" for k, v in inv.items() if v > 0]
        inv_str = "  ".join(inv_parts) if inv_parts else "-"
        loc_surf = self._fonts["tiny"].render(f"@ {agent.location}  {inv_str}", True, SIDEBAR_DIM)
        screen.blit(loc_surf, (x + 4, y + 58))

        # Thought snippet
        if agent.last_thought:
            thought = agent.last_thought[:54] + "…" if len(agent.last_thought) > 54 else agent.last_thought
            t_surf = self._fonts["tiny"].render(thought, True, (160, 160, 180))
            screen.blit(t_surf, (x + 4, y + 76))

        # Last action
        if agent.last_action:
            act_txt = f"→ {agent.last_action}"
            if agent.last_target:
                act_txt += f" [{agent.last_target}]"
            act_surf = self._fonts["tiny"].render(act_txt, True, (140, 200, 140))
            screen.blit(act_surf, (x + 4, y + 94))

    def _draw_need_bar(
        self,
        screen: pygame.Surface,
        x: int,
        y: int,
        label: str,
        value: int,
        bar_w: int = 72,
    ) -> None:
        bar_h = 8
        if label:
            label_surf = self._fonts["tiny"].render(label, True, SIDEBAR_DIM)
            screen.blit(label_surf, (x, y + 1))
            bx = x + 14
        else:
            bx = x

        pygame.draw.rect(screen, BAR_BG, pygame.Rect(bx, y + 2, bar_w, bar_h), border_radius=2)
        filled_w = int(bar_w * value / 100)
        bar_color = BAR_GREEN if value > 60 else (BAR_YELLOW if value > 30 else BAR_RED)
        if filled_w > 0:
            pygame.draw.rect(screen, bar_color, pygame.Rect(bx, y + 2, filled_w, bar_h), border_radius=2)

        val_surf = self._fonts["tiny"].render(str(value), True, SIDEBAR_DIM)
        screen.blit(val_surf, (bx + bar_w + 3, y + 1))

    # -- Interactivity helpers ----------------------------------------------

    def _get_agent_at_pixel(self, px: int, py: int, snap: WorldSnapshot) -> str | None:
        if px >= MAP_W:
            return None
        by_location: dict[str, list[AgentSnapshot]] = {}
        for agent in snap.agent_snapshots:
            by_location.setdefault(agent.location, []).append(agent)
        for agents in by_location.values():
            agents.sort(key=lambda a: a.name)
        for agent in snap.agent_snapshots:
            pos = self._agent_screen_pos(agent, snap, by_location)
            if math.hypot(px - pos[0], py - pos[1]) <= AGENT_RADIUS + AGENT_BORDER + 4:
                return agent.name
        return None

    def _draw_inspect_overlay(self, snap: WorldSnapshot) -> None:
        agent = next((a for a in snap.agent_snapshots if a.name == self._inspected_agent), None)
        if not agent:
            return

        # Semi-transparent backdrop
        backdrop = pygame.Surface((WINDOW_W, WINDOW_H), pygame.SRCALPHA)
        backdrop.fill((0, 0, 0, 180))
        self._screen.blit(backdrop, (0, 0))

        # Panel
        pw, ph = 560, 420
        px = (WINDOW_W - pw) // 2
        py = (WINDOW_H - ph) // 2
        panel = pygame.Rect(px, py, pw, ph)
        pygame.draw.rect(self._screen, (28, 28, 50), panel, border_radius=8)
        color = AGENT_COLORS.get(agent.role, (200, 200, 200))
        pygame.draw.rect(self._screen, color, panel, width=2, border_radius=8)

        cx, cy = px + 22, py + 18

        # Name
        name_surf = self._fonts["large"].render(f"{agent.name}  —  {agent.role}", True, color)
        self._screen.blit(name_surf, (cx, cy))
        cy += 34

        # Need bars (wide)
        for lbl, val in [("Hunger", agent.hunger), ("Energy", agent.energy),
                         ("Sociality", agent.sociality), ("Fulfillment", agent.fulfillment)]:
            lbl_surf = self._fonts["small"].render(f"{lbl}:", True, SIDEBAR_DIM)
            self._screen.blit(lbl_surf, (cx, cy + 2))
            self._draw_need_bar(self._screen, cx + 94, cy, "", val, 350)
            cy += 24
        cy += 6

        # Location
        loc_surf = self._fonts["small"].render(f"Location:  {agent.location}", True, SIDEBAR_TEXT)
        self._screen.blit(loc_surf, (cx, cy))
        cy += 22

        # Inventory
        inv = dict(agent.inventory)
        inv_str = "  ".join(f"{v} {k}" for k, v in inv.items()) if inv else "empty"
        inv_surf = self._fonts["small"].render(f"Inventory: {inv_str}", True, SIDEBAR_TEXT)
        self._screen.blit(inv_surf, (cx, cy))
        cy += 22

        if agent.is_starving:
            sv = self._fonts["bold"].render("⚠ STARVING", True, (255, 50, 50))
            self._screen.blit(sv, (cx, cy))
            cy += 22

        cy += 4
        pygame.draw.line(self._screen, DIVIDER_COLOR, (cx, cy), (px + pw - 22, cy))
        cy += 10

        # Thought (word-wrapped)
        th_label = self._fonts["bold"].render("Thought:", True, (180, 180, 220))
        self._screen.blit(th_label, (cx, cy))
        cy += 20

        thought_text = agent.last_thought or "(none)"
        words = thought_text.split()
        line = ""
        for word in words:
            candidate = (line + " " + word).strip()
            if len(candidate) > 64:
                self._screen.blit(self._fonts["tiny"].render(line, True, (200, 200, 220)), (cx, cy))
                cy += 16
                line = word
                if cy > py + ph - 56:
                    break
            else:
                line = candidate
        if line and cy <= py + ph - 56:
            self._screen.blit(self._fonts["tiny"].render(line, True, (200, 200, 220)), (cx, cy))
            cy += 16

        # Action
        if agent.last_action:
            act_txt = f"→ {agent.last_action}"
            if agent.last_target:
                act_txt += f"  [{agent.last_target}]"
            act_surf = self._fonts["small"].render(act_txt, True, (140, 200, 140))
            self._screen.blit(act_surf, (cx, cy + 4))

        # Close hint
        close = self._fonts["tiny"].render("Click anywhere or press Esc to close", True, SIDEBAR_DIM)
        self._screen.blit(close, (px + (pw - close.get_width()) // 2, py + ph - 22))

    def _draw_pause_banner(self) -> None:
        bw, bh = 340, 56
        bx = MAP_W // 2 - bw // 2
        by = WINDOW_H // 2 - bh // 2
        banner = pygame.Surface((bw, bh), pygame.SRCALPHA)
        banner.fill((0, 0, 0, 210))
        text = self._fonts["large"].render("PAUSED  —  [P] to resume", True, (255, 220, 80))
        banner.blit(text, ((bw - text.get_width()) // 2, (bh - text.get_height()) // 2))
        pygame.draw.rect(self._screen, (70, 60, 10), pygame.Rect(bx - 2, by - 2, bw + 4, bh + 4), border_radius=6)
        self._screen.blit(banner, (bx, by))
