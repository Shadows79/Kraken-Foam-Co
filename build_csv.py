#!/usr/bin/env python3
"""Flatten prospects.md into prospects.csv — a workable call sheet.

prospects.md is the source of truth. This script only reads and reshapes it;
it never invents, dedupes or edits records. Run it again after any edit to
prospects.md and commit both files together.

    python3 build_csv.py

Structure it parses:

    ## <City>                  city section
    ### <N>. <Category>        category subheading (not a record)
    ### <Organization>         record start, if the next "- " line is "- type:"
    - type: / city: / website: / phone: / email: / contact: / size signal:
    - event signal: / fit score: / why: / source:

Prose between records (category notes, NO RESULTS findings, competitive
summary tables) has no "- type:" line and is skipped.
"""

import csv
import re

SRC = "prospects.md"
OUT = "prospects.csv"

# Field label in prospects.md -> column name in the CSV.
FIELDS = {
    "type": "type_raw",
    "city": "city_raw",
    "website": "website",
    "phone": "phone",
    "email": "email",
    "contact": "contact",
    "size signal": "size_signal",
    "event signal": "event_signal",
    "fit score": "fit_score",
    "why": "why",
    "source": "sources",
}

COLUMNS = [
    "city",
    "category",
    "fit_score",
    "status",
    "organization",
    "phone",
    "email",
    "contact",
    "website",
    "size_signal",
    "event_signal",
    "why",
    "sources",
    "type_raw",
    "city_raw",
    "md_line",
]

CATEGORY_HEADING = re.compile(r"^### \d+\.\s")
FIELD_LINE = re.compile(r"^- ([a-z ]+):\s?(.*)$")


def status_of(record):
    """Derive a call-sheet status from markers the file states explicitly.

    Every branch keys off literal text already in prospects.md — an uppercase
    flag in the organization name, or an explicit assertion in the event
    signal or the why. Nothing here is a judgement call; records the file
    does not flag come back as an empty status.
    """
    name = record["organization"]
    signal = record["event_signal"]
    why = record["why"]

    if "PERMANENTLY GONE" in name or "DOES NOT EXIST" in record["type_raw"]:
        return "defunct — do not chase"
    if (
        "COMPETITOR" in name
        or "CONFIRMED COMPETITOR" in signal
        or "ALREADY OWNS FOAM" in signal
        or "direct competitor" in why.lower()
    ):
        return "competitor"
    if "LIKELY COMPETITOR" in signal:
        return "likely competitor"
    if "NOT YET OPEN" in name:
        return "not open yet — re-check"
    return ""


def parse(path):
    """Yield one dict per record, in file order."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    records = []
    city = ""
    pending = None  # an H3 heading not yet confirmed to be a record
    current = None

    for number, line in enumerate(lines, start=1):
        if line.startswith("## ") and not line.startswith("### "):
            city = line[3:].strip()
            pending, current = None, None
            continue

        if line.startswith("### "):
            pending, current = None, None
            if not CATEGORY_HEADING.match(line):
                pending = (line[4:].strip(), number)
            continue

        match = FIELD_LINE.match(line)
        if not match:
            # Prose, a table, a rule or a blank line ends any open record and
            # disqualifies a heading that was waiting to become one.
            if line.strip():
                pending, current = None, None
            continue

        label, value = match.group(1), match.group(2).strip()
        column = FIELDS.get(label)
        if column is None:
            continue

        if label == "type":
            if pending is None:
                continue  # a type line with no heading above it: not a record
            name, heading_line = pending
            current = dict.fromkeys(COLUMNS, "")
            current["organization"] = name
            current["md_line"] = heading_line
            current["city"] = city
            records.append(current)
            pending = None

        if current is not None:
            current[column] = value

    for record in records:
        # Canonical category: the part of `type:` before any parenthetical or
        # trailing flag, e.g. "country clubs (beach resort)" -> "country clubs".
        record["category"] = re.split(r"\s+\(|\s+—\s+", record["type_raw"])[0].strip()
        # `city` is the "## City" section the record sits under — the seven
        # cities of the grid, and the only grouping that sorts cleanly.
        # `city_raw` keeps the record's own "city:" line, which often carries a
        # boundary or postal caveat ("Estero (POSTAL MISMATCH — see below)").
        record["status"] = status_of(record)

    return records


def main():
    records = parse(SRC)

    fit = lambda r: int(r["fit_score"]) if r["fit_score"].isdigit() else 0
    cities = {}
    for record in records:
        cities.setdefault(record["city"], len(cities))

    # Call-sheet order: cities in the order the file introduces them, then
    # category, then best-fit first, then organization name.
    records.sort(key=lambda r: (cities[r["city"]], r["category"], -fit(r), r["organization"]))

    with open(OUT, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(records)

    print(f"{len(records)} records -> {OUT}")
    for city in cities:
        print(f"  {city}: {sum(1 for r in records if r['city'] == city)}")


if __name__ == "__main__":
    main()
