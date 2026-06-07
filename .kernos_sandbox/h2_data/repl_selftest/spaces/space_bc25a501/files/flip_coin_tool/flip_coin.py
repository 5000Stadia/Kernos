"""Tiny local personal flip-coin tool for Test 7."""

def flip_coin():
    """Return either heads or tails."""
    import secrets
    return secrets.choice(["heads", "tails"])

if __name__ == "__main__":
    print(flip_coin())
