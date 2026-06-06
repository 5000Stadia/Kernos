"""Tiny local coin-flip tool for Test 7."""

import random


def coin_flip():
    """Return either 'heads' or 'tails'."""
    return random.choice(["heads", "tails"])


def run(*args, **kwargs):
    """Tool entry point."""
    return {"result": coin_flip()}


if __name__ == "__main__":
    print(run()["result"])
