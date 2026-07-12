#!/usr/bin/env python3
"""Standalone audit verifier — the whole point is that it does not trust us.

    python scripts/verify_audit.py audit/audit.jsonl

An auditor should never have to ask the kernel "are you honest?", because
the kernel is the thing being audited. So this script imports nothing from
llm_os, has no dependencies beyond the standard library, and can be copied
to any machine on its own. It recomputes every record's SHA-256 from the
record itself and checks that each one commits to its predecessor.

Edit one byte of any historical line and verification fails at that line
and every line after it. Delete a line and the chain breaks at the seam.
Re-hashing a forged record does not help: its hash then no longer matches
what the NEXT record already committed to.

Exit code 0 = intact, 1 = broken, 2 = unusable file.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

GENESIS_HASH = "0" * 64

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)


def canonical(record: dict) -> str:
    """Byte-for-byte what the kernel hashed: the record without its own
    hash field, serialized with sorted keys and no incidental whitespace."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def verify(path: Path) -> int:
    try:
        lines = [l for l in path.read_text().splitlines() if l.strip()]
    except OSError as exc:
        print(f"{RED}cannot read {path}: {exc}{RESET}")
        return 2

    if not lines:
        print(f"{YELLOW}{path} is empty — nothing to verify.{RESET}")
        return 2

    print(f"\n{BOLD}🔗 Verifying {path}{RESET}")
    print(f"{DIM}   {len(lines)} records · SHA-256 hash chain{RESET}\n")

    prev_hash = GENESIS_HASH
    events: dict = {}
    first_ts = last_ts = None

    for number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"{RED}✗ record {number}: not valid JSON ({exc}){RESET}")
            return 1

        claimed = record.pop("hash", None)
        if claimed is None:
            print(f"{RED}✗ record {number}: has no hash field{RESET}")
            return 1

        if record.get("prev_hash") != prev_hash:
            print(f"{RED}✗ record {number}: BROKEN CHAIN{RESET}")
            print(f"{DIM}    expected prev_hash {prev_hash[:32]}…{RESET}")
            print(f"{DIM}    found              {str(record.get('prev_hash'))[:32]}…{RESET}")
            print(f"\n{RED}{BOLD}  The log was altered at or before record {number}.{RESET}")
            print(f"{DIM}  A record was edited, deleted, or inserted here.{RESET}\n")
            return 1

        recomputed = hashlib.sha256(canonical(record).encode()).hexdigest()
        if recomputed != claimed:
            print(f"{RED}✗ record {number}: CONTENT TAMPERED{RESET}")
            print(f"{DIM}    the record's own hash says {claimed[:32]}…{RESET}")
            print(f"{DIM}    its contents hash to       {recomputed[:32]}…{RESET}")
            event = record.get("event", "?")
            print(f"\n{RED}{BOLD}  Record {number} ({event}) was modified after it was written.{RESET}\n")
            return 1

        prev_hash = claimed
        events[record.get("event", "?")] = events.get(record.get("event", "?"), 0) + 1
        ts = record.get("ts")
        first_ts = first_ts or ts
        last_ts = ts or last_ts

    print(f"{GREEN}{BOLD}  ✓ CHAIN INTACT — all {len(lines)} records verify.{RESET}\n")
    print(f"{DIM}  period     {first_ts}  →  {last_ts}{RESET}")
    print(f"{DIM}  final hash {prev_hash}{RESET}")
    print(f"\n{BOLD}  What happened:{RESET}")
    for event, count in sorted(events.items(), key=lambda kv: -kv[1]):
        print(f"    {count:>5}  {event}")
    print(
        f"\n{DIM}  Every record commits to the one before it. To alter history\n"
        f"  undetectably, an attacker would have to rewrite every record from\n"
        f"  the edit forward — and this file's final hash would still change.{RESET}\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logfile", nargs="?", default="audit/audit.jsonl",
                        help="the JSONL audit log (default: audit/audit.jsonl)")
    args = parser.parse_args()
    return verify(Path(args.logfile))


if __name__ == "__main__":
    sys.exit(main())
