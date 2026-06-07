#!/usr/bin/env python3
"""Tiny personal coin-flip tool for Test 7."""
import random

def flip_coin():
    return random.choice(["heads", "tails"])

if __name__ == "__main__":
    print(flip_coin())
