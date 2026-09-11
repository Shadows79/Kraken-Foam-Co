#!/usr/bin/env python3
"""Kraken Foam Co prospect research harness.

Parallel web-research harness that builds a Southwest Florida prospect call
sheet. Run unattended from a terminal:

    export ANTHROPIC_API_KEY=sk-ant-...
    python harness.py --grid          # print the 96-cell grid, no API calls
    python harness.py --worker 0      # run one worker serially, dump raw output

Build state: STAGE 1 (grid) + STAGE 2 (single worker). Stages 3-5 to come.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

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

WORKER_MAX_TOKENS = 16000
WORKER_TIMEOUT_SECONDS = 300.0

# A worker's turn can pause when the server-side search loop hits its iteration
# cap (stop_reason="pause_turn"). How many times to resume before giving up.
MAX_PAUSE_RESUMES = 3

# Price per million tokens (input, output). Edit if rates change.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
WEB_SEARCH_COST_PER_1K = 10.00  # $10.00 per 1,000 searches

# Business context the workers score against.
SERVICE_AREA = "Lee and Collier counties, Southwest Florida"
PACKAGES = (
    "Classic $349 for 60 minutes; "
    "Deluxe $449 for 90 minutes; "
    "Commercial and Camps $649 for 2 hours"
)

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


# ---------------------------------------------------------------------------
# STAGE 2 - WORKERS
# ---------------------------------------------------------------------------

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

WORKER_SYSTEM = f"""\
You are a B2B prospect researcher for Kraken Foam Co, a mobile foam party \
service operating in {SERVICE_AREA}.

Your job: research ONE city and ONE organization category, then return \
qualified prospect records.

OUTPUT FORMAT
Return ONLY a JSON array. No preamble, no explanation, no markdown code \
fences. The first character of your response must be [ and the last must be ].

Each element is an object with exactly these keys:
  organization   string
  type           string - the category you were given, copied verbatim
  city           string
  website        string or null
  phone          string or null
  email          string or null
  contact_name   string or null
  size_signal    string or null - headcount, enrollment, or capacity if findable
  event_signal   string or null - evidence they actually run events
  fit_score      integer 1 to 5
  reasoning      string - one sentence
  source_url     string - the page you actually read

HARD RULES
- Publicly listed business contact information only. Never return personal \
contact details for private individuals. A director's name and office line \
published on an organization's own staff page is business contact information; \
a personal cell number or home address is not, and must be omitted.
- Do not invent records. If you find nothing that fits, return an empty array: []
- Do not invent phone numbers, email addresses, or URLs. null is the correct \
answer when you could not find the value. A guessed or pattern-constructed \
value is worse than null.
- source_url must be a page you actually retrieved, not a guess at where the \
information would live.

SCORING
- event_signal is the most important field. It is evidence the organization \
actually runs events: a published events calendar, a field day, a family fun \
night, a summer program schedule, a past party or carnival, a festival listing.
- An organization with no evidence of running events scores 2 or below, no \
matter how large or well known it is.
- Party and event rental companies are PARTNERS, not competitors. Score them on \
whether they could subcontract foam to fill a gap in their own lineup, not on \
whether they compete with us. A bounce house or water slide rental company with \
no foam offering is a strong partner lead.
- Fit scoring context. Packages: {PACKAGES}. Service area: {SERVICE_AREA}. \
A 5 is an organization with budget, a recurring event calendar, and a group of \
children in the right age range inside the service area. A 1 is out of area, \
defunct, or has no plausible use for the service.
"""


def build_worker_prompt(worker: Worker) -> str:
    return f"""\
City: {worker.city}, Florida
Category: {worker.category}

{worker.subquestion}

Use web search to find real, currently operating organizations. Prioritize \
depth of contact information and event evidence over record count. Return the \
JSON array only."""


def web_search_tool() -> dict[str, Any]:
    return {
        "type": SEARCH_TOOL_TYPE,
        "name": "web_search",
        "max_uses": SEARCHES_PER_WORKER,
    }


@dataclass
class WorkerResult:
    """Everything one grid cell produced, success or failure."""

    index: int
    city: str
    category: str
    records: list[dict[str, Any]] = field(default_factory=list)
    raw_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    elapsed: float = 0.0
    model: str = WORKER_MODEL
    attempts: int = 1
    error: str | None = None
    parse_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "city": self.city,
            "category": self.category,
            "model": self.model,
            "ok": self.ok,
            "error": self.error,
            "parse_error": self.parse_error,
            "attempts": self.attempts,
            "elapsed": round(self.elapsed, 2),
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "searches": self.searches,
            },
            "record_count": len(self.records),
            "records": self.records,
            # Kept only when parsing failed, so a bad worker is debuggable.
            "raw_text": self.raw_text if self.parse_error else None,
        }


def _collect_text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def _search_count(usage: Any) -> int:
    server = getattr(usage, "server_tool_use", None)
    return getattr(server, "web_search_requests", 0) or 0 if server else 0


def parse_records(text: str, worker: Worker) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a worker's JSON array. Returns (records, parse_error)."""
    candidate = text.strip()
    # Defensive: strip a markdown fence even though the prompt forbids one.
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    if not candidate:
        return [], "empty response"
    if not candidate.startswith("["):
        start, end = candidate.find("["), candidate.rfind("]")
        if start == -1 or end <= start:
            return [], "no JSON array found in response"
        candidate = candidate[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return [], f"JSONDecodeError: {exc}"
    if not isinstance(parsed, list):
        return [], f"expected a JSON array, got {type(parsed).__name__}"

    records, skipped = [], 0
    for item in parsed:
        if not isinstance(item, dict):
            skipped += 1
            continue
        records.append(normalize_record(item, worker))
    return records, f"skipped {skipped} non-object elements" if skipped else None


def normalize_record(item: dict[str, Any], worker: Worker) -> dict[str, Any]:
    """Coerce one record to the schema: every field present, right types."""
    rec: dict[str, Any] = {}
    for key in RECORD_FIELDS:
        value = item.get(key)
        if isinstance(value, str):
            value = value.strip() or None
        rec[key] = value

    # The grid knows the category and city better than the model does.
    rec["type"] = rec["type"] or worker.category
    rec["city"] = rec["city"] or worker.city

    score = rec.get("fit_score")
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 1
    rec["fit_score"] = max(1, min(5, score))

    for key in ("organization", "reasoning", "source_url"):
        rec[key] = rec[key] or ""
    return rec


async def call_worker(
    client: anthropic.AsyncAnthropic, worker: Worker, model: str = WORKER_MODEL
) -> WorkerResult:
    """One API call (plus pause_turn resumes) for one grid cell.

    Raises on API errors so the caller can retry; returns a WorkerResult for
    anything that completed, including a response that failed to parse.
    """
    result = WorkerResult(index=worker.index, city=worker.city,
                          category=worker.category, model=model)
    started = time.monotonic()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_worker_prompt(worker)}
    ]

    for _ in range(MAX_PAUSE_RESUMES + 1):
        response = await client.messages.create(
            model=model,
            max_tokens=WORKER_MAX_TOKENS,
            system=WORKER_SYSTEM,
            messages=messages,
            tools=[web_search_tool()],
            output_config={"effort": WORKER_EFFORT},
            timeout=WORKER_TIMEOUT_SECONDS,
        )
        result.input_tokens += response.usage.input_tokens
        result.output_tokens += response.usage.output_tokens
        result.searches += _search_count(response.usage)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            result.error = f"refusal ({category})"
            break
        if response.stop_reason == "pause_turn":
            # Server-side search loop hit its cap. Re-send as-is; the API
            # resumes from the trailing server_tool_use block.
            messages.append({"role": "assistant", "content": response.content})
            continue

        result.raw_text = _collect_text(response.content)
        if response.stop_reason == "max_tokens":
            result.parse_error = "hit max_tokens - response truncated"
        records, parse_error = parse_records(result.raw_text, worker)
        result.records = records
        result.parse_error = result.parse_error or parse_error
        break
    else:
        result.error = f"still paused after {MAX_PAUSE_RESUMES} resumes"

    result.elapsed = time.monotonic() - started
    return result


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  searches: int) -> float:
    in_rate, out_rate = PRICES.get(model, (0.0, 0.0))
    return (
        input_tokens / 1_000_000 * in_rate
        + output_tokens / 1_000_000 * out_rate
        + searches / 1_000 * WEB_SEARCH_COST_PER_1K
    )


async def run_single_worker(index: int, model: str) -> int:
    """Stage 2 check: run one grid cell serially and dump what came back."""
    workers = build_grid()
    if not 0 <= index < len(workers):
        print(f"error: --worker must be 0..{len(workers) - 1}", file=sys.stderr)
        return 2
    worker = workers[index]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    print(f"[{worker.index:02d}] {worker.city} | {worker.category}")
    print(f"     model={model} effort={WORKER_EFFORT} "
          f"tool={SEARCH_TOOL_TYPE} max_uses={SEARCHES_PER_WORKER}\n")

    async with anthropic.AsyncAnthropic() as client:
        try:
            result = await call_worker(client, worker, model=model)
        except anthropic.APIStatusError as exc:
            print(f"API error {exc.status_code}: {exc.message}", file=sys.stderr)
            return 1
        except anthropic.APIConnectionError as exc:
            print(f"connection error: {exc}", file=sys.stderr)
            return 1

    if result.error:
        print(f"FAILED: {result.error}")
    if result.parse_error:
        print(f"PARSE WARNING: {result.parse_error}")
        print("\n--- raw text ---")
        print(result.raw_text)
        print("--- end raw text ---\n")

    print(json.dumps(result.records, indent=2))
    print(
        f"\n{len(result.records)} records | {result.elapsed:.1f}s | "
        f"in={result.input_tokens} out={result.output_tokens} "
        f"searches={result.searches} | "
        f"~${estimate_cost(model, result.input_tokens, result.output_tokens, result.searches):.4f}"
    )
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", action="store_true",
                        help="print all 96 subquestions and exit")
    parser.add_argument("--worker", type=int, metavar="N",
                        help="run only worker N, serially, and dump raw output")
    parser.add_argument("--model", default=WORKER_MODEL,
                        help=f"override WORKER_MODEL (default {WORKER_MODEL})")
    args = parser.parse_args(argv)

    workers = build_grid()
    if args.grid:
        print(f"Grid: {len(CITIES)} x {len(CATEGORIES)} = {len(workers)} workers")
        print_grid(workers)
        return 0

    if args.worker is not None:
        return asyncio.run(run_single_worker(args.worker, args.model))

    print(f"Grid: {len(CITIES)} cities x {len(CATEGORIES)} categories "
          f"= {len(workers)} workers")
    print_grid(workers, limit=5)
    if DRY_RUN_LIMIT == 0:
        print("\nDRY_RUN_LIMIT = 0 - grid only, no API calls made.")
        return 0
    print("\nStages 3-5 not built yet. "
          "Use --worker N to run one cell, --grid to see all 96.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
