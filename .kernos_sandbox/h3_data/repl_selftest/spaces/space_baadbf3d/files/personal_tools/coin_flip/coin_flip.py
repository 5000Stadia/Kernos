#!/usr/bin/env python3
"""Tiny local personal tool: flip a fair coin."""
import random

def flip_coin():
    return random.choice(["heads", "tails"])

if __name__ == "__main__":
    print(flip_coin())
