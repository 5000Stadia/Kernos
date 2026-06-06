import random

def coin_flip():
    """Return a random coin-flip result: 'heads' or 'tails'."""
    return random.choice(["heads", "tails"])

if __name__ == "__main__":
    print(coin_flip())
