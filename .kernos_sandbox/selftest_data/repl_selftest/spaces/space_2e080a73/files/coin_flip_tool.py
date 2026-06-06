import random

def execute(input_data):
    try:
        return {"result": random.choice(["heads", "tails"])}
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    print(execute({}))
