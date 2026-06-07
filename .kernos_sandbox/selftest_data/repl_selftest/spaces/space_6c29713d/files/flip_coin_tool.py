#!/usr/bin/env python3
"""Simple coin-flip tool implementation for Kernos self-test Test 7."""

import random


def flip_coin() -> str:
    """Return either 'heads' or 'tails'."""
    return random.choice(["heads", "tails"])


if __name__ == "__main__":
    print(flip_coin())
