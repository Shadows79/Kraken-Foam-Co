#!/usr/bin/env python3
"""The research grid: 8 cities x 12 categories = 96 cells.

Deterministic and order-stable. Cell index is the identity used in state.json
and records.jsonl, so never reorder CITIES or CATEGORIES once a sweep starts —
that would silently remap which cells are marked done.
"""

from __future__ import annotations

from dataclasses import dataclass

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

# The record schema. Order here is the CSV column order.
RECORD_FIELDS = (
    "organization",
    "type",
    "city",
    "website",
    "phone",
    "email",
    "contact_name",
    "size_signal",
    "event_signal",
    "fit_score",
    "reasoning",
    "source_url",
)

# Scoring context, for reference while researching.
SERVICE_AREA = "Lee and Collier counties, Southwest Florida"
PACKAGES = (
    "Classic $349/60min; Deluxe $449/90min; Commercial and Camps $649/2hr"
)


@dataclass(frozen=True)
class Cell:
    """One grid cell: a single city x category research job."""

    index: int
    city: str
    category: str

    @property
    def label(self) -> str:
        return f"[{self.index:02d}] {self.city} | {self.category}"

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, "city": self.city, "category": self.category}


def build_grid() -> list[Cell]:
    """Cross product of CITIES x CATEGORIES, in stable order."""
    return [
        Cell(index=i, city=city, category=category)
        for i, (city, category) in enumerate(
            (c, cat) for c in CITIES for cat in CATEGORIES
        )
    ]


def cell_by_index(index: int) -> Cell:
    grid = build_grid()
    if not 0 <= index < len(grid):
        raise IndexError(f"cell index must be 0..{len(grid) - 1}, got {index}")
    return grid[index]


if __name__ == "__main__":
    grid = build_grid()
    print(f"Grid: {len(CITIES)} cities x {len(CATEGORIES)} categories "
          f"= {len(grid)} cells")
    for cell in grid:
        print(f"  {cell.label}")
