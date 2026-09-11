#!/usr/bin/env python3
"""Kraken Foam Co prospect research harness.

Parallel web-research harness that builds a Southwest Florida prospect call
sheet. Run unattended from a terminal:

    export ANTHROPIC_API_KEY=sk-ant-...
    python harness.py

Build state: STAGE 1 only (grid generator). Stages 2-5 land in later steps.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# CONFIG - edit these, not the logic below
# ---------------------------------------------------------------------------

WORKER_MODEL = "claude-opus-5"
MERGE_MODEL = "claude-haiku-4-5"
SYNTH_MODEL = "claude-opus-5"

MAX_CONCURRENT = 8
SEARCHES_PER_WORKER = 3
DRY_RUN_LIMIT = None  # set to an int to only run the first N workers; 0 = grid only
OUT_DIR = "runs"

# Server-side web search tool version. The _20260209 variant adds dynamic
# filtering (Claude filters results before they hit the context window) and is
# supported on opus-5 / sonnet-5 / opus-4.6+. Set to "web_search_20250305" for
# the basic variant or for older models.
SEARCH_TOOL_TYPE = "web_search_20260209"

# Thinking depth for worker calls: low | medium | high | xhigh | max
WORKER_EFFORT = "medium"
SYNTH_EFFORT = "high"

# Price per million tokens (input, output). Edit if rates change.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
WEB_SEARCH_COST_PER_1K = 10.00  # $10.00 per 1,000 searches

# ---------------------------------------------------------------------------
# STAGE 1 - GRID GENERATOR (deterministic, no LLM call)
# ---------------------------------------------------------------------------

CITIES = [
    "Fort Myers",
    "Cape Coral",
    "Naples",
    "Bonita Springs",
    "Estero",
    "Lehigh Acres",
    "Fort Myers Beach",
    "Marco Island",
]

CATEGORIES = [
    "summer and day camps",
    "preschools and daycare centers",
    "church youth programs",
    "schools and PTOs",
    "municipal parks and recreation departments",
    "country clubs",
    "apartment and condo communities with event staff",
    "birthday party venues",
    "party and event rental companies",
    "kids gyms and youth sports leagues",
    "festival and community event organizers",
    "breweries and food truck parks that host family events",
]


@dataclass(frozen=True)
class Worker:
    """One grid cell: a single city x category research job."""

    index: int
    city: str
    category: str

    @property
    def subquestion(self) -> str:
        return (
            f"Find organizations in the category '{self.category}' located in or "
            f"serving {self.city}, Florida that could book a mobile foam party service."
        )


def build_grid() -> list[Worker]:
    """Cross product of CITIES x CATEGORIES, in stable order."""
    return [
        Worker(index=i, city=city, category=category)
        for i, (city, category) in enumerate(
            (c, cat) for c in CITIES for cat in CATEGORIES
        )
    ]


def print_grid(workers: list[Worker], limit: int | None = None) -> None:
    shown = workers if limit is None else workers[:limit]
    width = len(str(len(workers)))
    for w in shown:
        print(f"  [{w.index:0{width}d}] {w.city:<18} | {w.category}")
    if limit is not None and len(workers) > limit:
        print(f"  ... and {len(workers) - limit} more")


def main() -> int:
    workers = build_grid()
    print(
        f"Grid: {len(CITIES)} cities x {len(CATEGORIES)} categories "
        f"= {len(workers)} workers"
    )
    print_grid(workers)

    if DRY_RUN_LIMIT == 0:
        print("\nDRY_RUN_LIMIT = 0 - grid only, no API calls made.")
        return 0

    print("\nStages 2-5 not built yet. Grid verified; stopping here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
