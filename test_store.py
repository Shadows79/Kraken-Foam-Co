#!/usr/bin/env python3
"""Prove the state round-trip and dedupe with fake records. No network, no LLM.

Runs entirely in a temp directory — never touches the real sweep files.
Run: python test_store.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import store
from grid import cell_by_index

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got:  {got!r}")
        print(f"        want: {want!r}")
        FAILURES.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kraken-test-"))
    store.STATE_PATH = tmp / "state.json"
    store.RECORDS_PATH = tmp / "records.jsonl"
    store.CSV_PATH = tmp / "prospects.csv"

    # Two fake records for cell 0: Fort Myers | summer and day camps.
    cell0 = cell_by_index(0)
    fake = [
        {
            "organization": "Gulf Coast Kids Camp",
            "type": "summer and day camps",
            "city": "Fort Myers",
            "website": "https://example-camp.test",
            "phone": "239-555-0100",
            "email": None,
            "contact_name": None,
            "size_signal": "120 campers",
            "event_signal": "Publishes a weekly summer activity calendar",
            "fit_score": 5,
            "reasoning": "Large camp with a recurring activity calendar.",
            "source_url": "https://example-camp.test/summer",
        },
        {
            "organization": "Riverside Day Camp",
            "type": "summer and day camps",
            "city": "Fort Myers",
            "website": None,
            "phone": None,
            "email": None,
            "contact_name": None,
            "size_signal": None,
            "event_signal": None,  # no evidence -> score must clamp to 2
            "fit_score": 5,
            "reasoning": "Listed as a camp but no event evidence found.",
            "source_url": "https://example-directory.test/riverside",
        },
    ]

    print("\n1. empty state")
    before = store.status()
    check("starts with 0 cells done", before["done"], 0)
    check("96 cells pending", before["pending"], 96)

    print("\n2. add 2 records to cell 0")
    added = store.add_records(cell0, fake)
    check("add_records returned 2", added, 2)

    print("\n3. state round-trip (re-read both files from disk)")
    state = json.loads(store.STATE_PATH.read_text())
    check("cell 0 marked done", "0" in state["done"], True)
    check("cell 0 count recorded", state["done"]["0"]["count"], 2)
    check("done_indices reads back", store.done_indices(), {0})
    lines = store.RECORDS_PATH.read_text().strip().splitlines()
    check("records.jsonl has 2 lines", len(lines), 2)
    check("each line tagged with its cell", {json.loads(l)["cell"] for l in lines}, {0})
    check("records load back", len(store.load_records()), 2)

    print("\n4. house rule enforced on write")
    riverside = next(r for r in store.load_records()
                     if r["organization"] == "Riverside Day Camp")
    check("no event_signal clamps fit_score 5 -> 2", riverside["fit_score"], 2)

    print("\n5. next_cells skips completed work")
    check("next 4 starts at cell 1",
          [c.index for c in store.next_cells(4)], [1, 2, 3, 4])

    print("\n6. a done cell cannot be double-added")
    try:
        store.add_records(cell0, fake)
        check("re-adding cell 0 raises", False, True)
    except ValueError:
        check("re-adding cell 0 raises", True, True)

    print("\n7. uncommitted records are ignored (simulated mid-cell crash)")
    with store.RECORDS_PATH.open("a") as fh:
        fh.write(json.dumps({"cell": 42, "organization": "Ghost Org",
                             "city": "Naples", "fit_score": 5,
                             "source_url": "https://ghost.test"}) + "\n")
    check("orphan line on disk", len(store.RECORDS_PATH.read_text().strip().splitlines()), 3)
    check("but dropped on read", len(store.load_records()), 2)

    print("\n8. dedupe merges the same org across cells")
    # Cell 9 (Fort Myers | kids gyms) re-finds the same camp, named differently,
    # with a phone the first pass missed and a shorter reasoning.
    store.add_records(cell_by_index(9), [
        {
            "organization": "The Gulf Coast Kids Camp, LLC",
            "type": "kids gyms and youth sports leagues",
            "city": "Fort Myers, FL",
            "website": "https://example-camp.test",
            "phone": "239-555-0100",
            "email": "info@example-camp.test",
            "contact_name": "Programs Office",
            "size_signal": None,
            "event_signal": "Hosts an end-of-season field day every spring",
            "fit_score": 4,
            "reasoning": "Runs field days.",
            "source_url": "https://example-camp.test/contact",
        },
        # Same folded name, genuinely different city -> must NOT merge.
        {
            "organization": "Riverside Day Camp",
            "type": "kids gyms and youth sports leagues",
            "city": "Naples",
            "website": None, "phone": None, "email": None, "contact_name": None,
            "size_signal": None,
            "event_signal": "Summer camp calendar posted",
            "fit_score": 3,
            "reasoning": "Different Riverside, in Collier county.",
            "source_url": "https://example-naples.test/camp",
        },
    ])
    check("name folding strips The/LLC/punctuation",
          store.normalize_name("The Gulf Coast Kids Camp, LLC"),
          store.normalize_name("Gulf Coast Kids Camp"))
    merged = store.dedupe()
    check("4 raw records", len(store.load_records()), 4)
    check("3 unique orgs after dedupe", len(merged), 3)

    gulf = next(r for r in merged if "gulf" in r["organization"].lower())
    check("merged from 2 records", gulf["merged_count"], 2)
    check("both source_urls collected", len(gulf["source_urls"]), 2)
    check("richest email kept", gulf["email"], "info@example-camp.test")
    check("richest size_signal kept (only one had it)", gulf["size_signal"], "120 campers")
    check("longest event_signal kept",
          gulf["event_signal"], "Hosts an end-of-season field day every spring")
    check("highest fit_score kept", gulf["fit_score"], 5)
    check("found in both cells", gulf["found_in_cells"], [0, 9])
    check("both categories kept in types",
          gulf["types"],
          ["summer and day camps", "kids gyms and youth sports leagues"])
    check("type keeps one primary label", isinstance(gulf["type"], str), True)
    solo = next(r for r in merged if r["city"] == "Naples")
    check("single-category org has a one-item types",
          solo["types"], ["kids gyms and youth sports leagues"])

    riversides = [r for r in merged if "riverside" in r["organization"].lower()]
    check("same name, different city stays split", len(riversides), 2)

    print("\n9. csv writer")
    rows = store.write_csv()
    check("wrote 3 rows", rows, 3)
    import csv as _csv
    with store.CSV_PATH.open(newline="", encoding="utf-8") as fh:
        rows_out = list(_csv.DictReader(fh))
    check("source_urls column replaces source_url",
          "source_urls" in rows_out[0] and "source_url" not in rows_out[0], True)
    check("types column sits beside type",
          list(rows_out[0])[:3], ["organization", "type", "types"])
    check("types pipe-joined in csv",
          next(r["types"] for r in rows_out if "Gulf" in r["organization"]),
          "summer and day camps | kids gyms and youth sports leagues")
    scores = [int(r["fit_score"]) for r in rows_out]
    check("sorted by fit_score desc", scores, sorted(scores, reverse=True))
    check("pipe-joined source_urls",
          next(r["source_urls"] for r in rows_out if "Gulf" in r["organization"]),
          "https://example-camp.test/summer | https://example-camp.test/contact")

    print("\n10. status()\n")
    final = store.status()
    check("2 cells done", final["done"], 2)
    check("3 unique orgs", final["unique"], 3)

    print(f"\ntemp dir: {tmp}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
