#!/usr/bin/env python3
"""Compare .env between the live install and the dev checkout.

Why this exists: `.env` is gitignored, so no push, pull, deploy or test will
ever surface a difference between two Kernos directories. On 2026-08-05 a model
change was applied to the dev copy, reported as done, and the live bot kept
running the old model — invisible to every check we had.

Values are NEVER printed. Only key names, and whether each side has a value,
so this is safe to run and safe to paste.

    python3 tools/env_drift.py                 # default pair
    python3 tools/env_drift.py A/.env B/.env   # explicit

Exit 0 = no drift, 1 = drift found, 2 = usage/read error.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

#: Keys whose values are expected to differ per install (identity, ports, paths).
#: Presence is still compared; a key missing on one side is always reported.
EXPECTED_TO_DIFFER = {
    "KERNOS_DATA_DIR",
    "KERNOS_REPO_DIR",
    "KERNOS_INSTANCE_ID",
}

#: Keys where SAMENESS is the alarm, not difference. These two directories are
#: two DIFFERENT Discord bots; identical credentials would mean two processes
#: fighting over one gateway connection, which is a real incident.
MUST_DIFFER = {
    "DISCORD_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
}

DEFAULT_PAIR = (
    Path("/home/k/Kernos-main/.env"),   # live
    Path("/home/k/Kernos/.env"),        # dev
)


def load_keys(path: Path) -> dict:
    """Return {key: (has_value, digest)}.

    The digest lets us detect a same-key-different-value drift — the case that
    actually caused the incident — WITHOUT ever holding or printing the value.
    A tool that could not catch its own motivating failure would be theatre.
    """
    out: dict = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        digest = hashlib.sha256(value.encode()).hexdigest()[:12] if value else ""
        out[key] = (bool(value), digest)
    return out


def main(argv: list) -> int:
    if len(argv) == 3:
        left, right = Path(argv[1]), Path(argv[2])
    elif len(argv) == 1:
        left, right = DEFAULT_PAIR
    else:
        print(__doc__)
        return 2

    for p in (left, right):
        if not p.is_file():
            print(f"error: not readable: {p}", file=sys.stderr)
            return 2

    a, b = load_keys(left), load_keys(right)
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    shared_keys = set(a) & set(b)
    presence = sorted(k for k in shared_keys if a[k][0] != b[k][0])
    # THE incident case: present and non-blank on both sides, different values.
    differing = sorted(
        k for k in shared_keys
        if k not in EXPECTED_TO_DIFFER and k not in MUST_DIFFER
        and a[k][0] and b[k][0] and a[k][1] != b[k][1]
    )
    # Inverted alarm: these MUST differ, so equality is the finding.
    collided = sorted(
        k for k in shared_keys & MUST_DIFFER
        if a[k][0] and b[k][0] and a[k][1] == b[k][1]
    )

    print(f"A = {left}\nB = {right}\n")
    drift = False

    if only_a:
        drift = True
        print(f"only in A ({len(only_a)}):")
        for k in only_a:
            print(f"  {k}")
    if only_b:
        drift = True
        print(f"only in B ({len(only_b)}):")
        for k in only_b:
            print(f"  {k}")
    if presence:
        drift = True
        print("set on one side, blank on the other:")
        for k in presence:
            print(f"  {k}: A={'set' if a[k][0] else 'blank'} "
                  f"B={'set' if b[k][0] else 'blank'}")
    if differing:
        drift = True
        print(f"DIFFERENT VALUES on both sides ({len(differing)}) "
              f"— the silent case:")
        for k in differing:
            print(f"  {k}")

    if collided:
        drift = True
        print(f"IDENTICAL where they MUST differ ({len(collided)}) "
              f"— two installs sharing one credential:")
        for k in collided:
            print(f"  {k}")

    agreeing = len(shared_keys) - len(presence) - len(differing) - len(collided)
    print(f"\n{agreeing} keys agree on both sides "
          f"({len(EXPECTED_TO_DIFFER & shared_keys)} exempt keys not value-compared).")

    print("\nDRIFT FOUND" if drift else "\nno key-level drift")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
