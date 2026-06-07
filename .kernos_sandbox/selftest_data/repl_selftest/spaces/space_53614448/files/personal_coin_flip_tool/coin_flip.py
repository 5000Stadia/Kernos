"""Personal coin-flip tool for the local KERNOS workspace."""
import random

def flip_coin():
    """Return one random coin-flip result: 'heads' or 'tails'."""
    return random.choice(["heads", "tails"])

if __name__ == "__main__":
    print(flip_coin())
