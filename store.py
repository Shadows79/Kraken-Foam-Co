#!/usr/bin/env python3
"""Bookkeeping for the Kraken Foam Co prospect sweep.

No LLM calls anywhere in here. The research is done by hand (by Claude, in
session); this module only reads and writes state, dedupes, and writes the CSV.

Files, all relative to the project root:
  state.json     which cells are done. The single source of truth for progress.
  records.jsonl  one record per line, each tagged with the cell that found it.
  prospects.csv  final call sheet, regenerated from records.jsonl on demand.

COMMIT SEMANTICS
state.json is the commit marker. add_records() writes records.jsonl first, then
state.json; a crash in between leaves records tagged with a cell that is still
pending, and those lines are treated as uncommitted — dropped on read, purged
on the next write. So a cell is either fully recorded and done, or absent and
pending. Never half of each.

CLI:
  python store.py status
  python store.py next 4
  python store.py add --cell N [--file records.json]   # or JSON on stdin
  python store.py csv
  python store.py reset --cell N
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from grid import RECORD_FIELDS, Cell, build_grid, cell_by_index

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
RECORDS_PATH = ROOT / "records.jsonl"
CSV_PATH = ROOT / "prospects.csv"

STATE_VERSION = 1

# Text fields where "richest" means the longest non-empty string.
TEXT_FIELDS = tuple(f for f in RECORD_FIELDS if f not in ("fit_score", "source_url"))

# Corporate suffixes stripped when comparing organization names.
NAME_SUFFIXES = (
    "llc", "l l c", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited", "pllc", "pa", "pc", "lp", "llp",
)


# ---------------------------------------------------------------------------
# atomic file writes
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically: temp file in the same dir, fsync, rename."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict[str, Any]:
    """Read state.json, or return a fresh empty state."""
    if not STATE_PATH.exists():
        return {"version": STATE_VERSION, "created_at": _now(),
                "updated_at": _now(), "done": {}}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("done", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True) + "\n")


def done_indices(state: dict[str, Any] | None = None) -> set[int]:
    state = state if state is not None else load_state()
    return {int(k) for k in state["done"]}


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def load_records(committed_only: bool = True) -> list[dict[str, Any]]:
    """Read records.jsonl. Uncommitted lines (cell not marked done) are dropped."""
    if not RECORDS_PATH.exists():
        return []
    done = done_indices() if committed_only else None
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(
        RECORDS_PATH.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"warning: records.jsonl line {lineno} is not valid JSON: {exc}",
                  file=sys.stderr)
            continue
        if done is not None and int(row.get("cell", -1)) not in done:
            continue
        rows.append(row)
    return rows


def normalize_record(record: dict[str, Any], cell: Cell) -> dict[str, Any]:
    """Coerce one hand-built record to the schema. Every field present, typed."""
    out: dict[str, Any] = {}
    for key in RECORD_FIELDS:
        value = record.get(key)
        if isinstance(value, str):
            value = value.strip() or None
        out[key] = value

    # The grid is authoritative for category and city.
    out["type"] = out["type"] or cell.category
    out["city"] = out["city"] or cell.city

    if not out["organization"]:
        raise ValueError(f"record is missing an organization name: {record!r}")
    if not out["source_url"]:
        raise ValueError(
            f"record for {out['organization']!r} is missing source_url"
        )

    try:
        score = int(out["fit_score"])
    except (TypeError, ValueError):
        raise ValueError(
            f"record for {out['organization']!r} has a non-integer "
            f"fit_score: {out['fit_score']!r}"
        ) from None
    score = max(1, min(5, score))
    # House rule, enforced here so it cannot be forgotten while researching:
    # no evidence of running events caps the score at 2.
    if not out["event_signal"]:
        score = min(score, 2)
    out["fit_score"] = score

    unknown = set(record) - set(RECORD_FIELDS)
    if unknown:
        print(f"warning: dropped unknown field(s) {sorted(unknown)} from "
              f"{out['organization']!r}", file=sys.stderr)
    return out


def add_records(cell: int | Cell, records: Iterable[dict[str, Any]]) -> int:
    """Append a cell's records and mark it done. Both files, or neither.

    Rewrites records.jsonl wholesale (cheap at this scale) so that uncommitted
    lines from an interrupted earlier call are purged rather than duplicated.
    """
    cell = cell if isinstance(cell, Cell) else cell_by_index(int(cell))
    state = load_state()
    if str(cell.index) in state["done"]:
        raise ValueError(
            f"cell {cell.index} is already done — use `reset --cell "
            f"{cell.index}` first if you mean to redo it"
        )

    normalized = [normalize_record(r, cell) for r in records]

    # Keep only committed lines, then append this cell's.
    kept = load_records(committed_only=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in kept]
    lines += [
        json.dumps({"cell": cell.index, "found_at": _now(), **rec},
                   ensure_ascii=False)
        for rec in normalized
    ]
    _atomic_write(RECORDS_PATH, "\n".join(lines) + ("\n" if lines else ""))

    # state.json is the commit marker — written second, on purpose.
    state["done"][str(cell.index)] = {
        "city": cell.city,
        "category": cell.category,
        "count": len(normalized),
        "at": _now(),
    }
    save_state(state)
    return len(normalized)


def reset_cell(index: int) -> bool:
    """Mark a cell pending again and drop its records. Used to redo a cell."""
    state = load_state()
    if str(index) not in state["done"]:
        return False
    kept = [r for r in load_records(committed_only=True)
            if int(r.get("cell", -1)) != index]
    del state["done"][str(index)]
    _atomic_write(RECORDS_PATH,
                  "\n".join(json.dumps(r, ensure_ascii=False) for r in kept)
                  + ("\n" if kept else ""))
    save_state(state)
    return True


# ---------------------------------------------------------------------------
# work queue
# ---------------------------------------------------------------------------

def next_cells(n: int) -> list[Cell]:
    """The next n pending cells, in grid order."""
    done = done_indices()
    return [c for c in build_grid() if c.index not in done][:n]


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Fold an organization name for comparison only."""
    text = (name or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    # Strip trailing corporate suffixes, repeatedly ("Foo Inc LLC").
    changed = True
    while changed:
        changed = False
        for suffix in NAME_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
    return text


def _normalize_city(city: str) -> str:
    text = (city or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\b(fl|florida|usa)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _same_place(a: str, b: str) -> bool:
    """True when two city strings plausibly name the same place."""
    na, nb = _normalize_city(a), _normalize_city(b)
    if not na or not nb:
        return True
    return na == nb or na.startswith(nb) or nb.startswith(na)


def _richest_text(values: Iterable[Any]) -> Any:
    """Longest non-empty string wins — more characters, more information."""
    best = None
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        if best is None or len(text) > len(str(best)):
            best = text
    return best


def merge_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse one group of duplicate records into a single merged record."""
    merged: dict[str, Any] = {}
    for key in TEXT_FIELDS:
        merged[key] = _richest_text(r.get(key) for r in group)
    # Highest score wins. It is the record that found event evidence, and
    # event_signal already merged to the richest value above.
    merged["fit_score"] = max(int(r.get("fit_score") or 1) for r in group)

    urls: list[str] = []
    for record in group:
        url = (record.get("source_url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    merged["source_urls"] = urls

    cells = sorted({int(r["cell"]) for r in group if "cell" in r})
    merged["found_in_cells"] = cells
    merged["merged_count"] = len(group)
    return merged


def dedupe(records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Group records by folded organization name and merge each group.

    Same folded name in the same place merges. Same name in a clearly different
    city stays separate — there is a First Baptist in every city down here.
    """
    records = load_records() if records is None else records
    groups: dict[str, list[list[dict[str, Any]]]] = {}
    for record in records:
        key = normalize_name(record.get("organization", ""))
        buckets = groups.setdefault(key, [])
        for bucket in buckets:
            if _same_place(bucket[0].get("city", ""), record.get("city", "")):
                bucket.append(record)
                break
        else:
            buckets.append([record])
    return [merge_group(bucket)
            for buckets in groups.values() for bucket in buckets]


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

CSV_COLUMNS = tuple(f for f in RECORD_FIELDS if f != "source_url") + (
    "source_urls", "merged_count",
)


def write_csv(path: Path | None = None) -> int:
    """Dedupe, sort by fit_score desc then city, write the call sheet."""
    # Resolved at call time, not import time, so the path stays overridable.
    path = path or CSV_PATH
    merged = dedupe()
    merged.sort(key=lambda r: (-r["fit_score"], (r.get("city") or "").lower(),
                               (r.get("organization") or "").lower()))
    rows = []
    for record in merged:
        row = {key: record.get(key) for key in CSV_COLUMNS}
        row["source_urls"] = " | ".join(record.get("source_urls", []))
        rows.append(row)

    buffer = tempfile.SpooledTemporaryFile(mode="w+", newline="", encoding="utf-8")
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS),
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    buffer.seek(0)
    _atomic_write(path, buffer.read())
    buffer.close()
    return len(rows)


def status() -> dict[str, Any]:
    """Print and return sweep progress."""
    grid = build_grid()
    state = load_state()
    done = done_indices(state)
    raw = load_records()
    merged = dedupe(raw)
    pending = [c for c in grid if c.index not in done]

    print(f"cells done        {len(done)}/{len(grid)}")
    print(f"cells pending     {len(pending)}")
    print(f"raw records       {len(raw)}")
    print(f"unique orgs       {len(merged)} (after dedupe)")
    if raw:
        collapsed = len(raw) - len(merged)
        print(f"duplicates merged {collapsed}")
    if pending:
        print(f"next up           {pending[0].label}")
    elif done:
        print("next up           grid complete")
    return {"done": len(done), "pending": len(pending),
            "raw": len(raw), "unique": len(merged)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_next = sub.add_parser("next")
    p_next.add_argument("n", type=int, nargs="?", default=4)
    p_add = sub.add_parser("add")
    p_add.add_argument("--cell", type=int, required=True)
    p_add.add_argument("--file", help="JSON array of records; omit to read stdin")
    sub.add_parser("csv")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--cell", type=int, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "status":
        status()
    elif args.cmd == "next":
        cells = next_cells(args.n)
        if not cells:
            print("grid complete — no pending cells")
        for cell in cells:
            print(cell.label)
    elif args.cmd == "add":
        text = (Path(args.file).read_text(encoding="utf-8") if args.file
                else sys.stdin.read())
        records = json.loads(text)
        if not isinstance(records, list):
            print("error: expected a JSON array of records", file=sys.stderr)
            return 2
        cell = cell_by_index(args.cell)
        count = add_records(cell, records)
        print(f"{cell.city} | {cell.category} | {count} found")
    elif args.cmd == "csv":
        count = write_csv()
        print(f"wrote {CSV_PATH.name}: {count} rows")
    elif args.cmd == "reset":
        print("reset" if reset_cell(args.cell) else "cell was not done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
