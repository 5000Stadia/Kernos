import json, pathlib, importlib.util

impl = '''import random\n\ndef execute(input_data):\n    try:\n        return {"result": random.choice(["heads", "tails"])}\n    except Exception as e:\n        return {"error": str(e)}\n'''

desc = {
    "name": "flip_coin",
    "description": "Flip a fair coin and return heads or tails.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": True
    },
    "implementation": "flip_coin.py"
}
pathlib.Path('flip_coin.py').write_text(impl)
pathlib.Path('flip_coin.tool.json').write_text(json.dumps(desc, indent=2))

spec = importlib.util.spec_from_file_location('flip_coin', 'flip_coin.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(mod.execute({}))