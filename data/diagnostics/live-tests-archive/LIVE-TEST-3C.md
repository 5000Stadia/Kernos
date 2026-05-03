# Live Test: SPEC-3C — Proactive Awareness

**Tenant ID (copy-paste):** `discord:000000000000000000`
**Date:** 2026-03-18
**Test script:** `tests/live/run_3c_live.py`
**Result:** FULL PASS (15/15)

**Prerequisites:**
- ANTHROPIC_API_KEY set in .env
- Real tenant data in ./data with active knowledge store
- Handler fully wired (reasoning, state, events)

---

## Step-by-Step Test Results

### Step 0: Architecture verification
**Check:** Whisper and SuppressionEntry dataclasses exist, AwarenessEvaluator has `run_time_pass`, EventType.PROACTIVE_INSIGHT exists.
**Result:** PASS
```
Whisper=True, Suppression=True, Evaluator=True, EventType=True
```

### Step 1: Evaluator starts and stops cleanly
**Check:** Start evaluator, verify `_running=True` and task exists. Stop it, verify `_running=False`.
**Result:** PASS
```
started=True, stopped=True
```

### Step 2: Time pass with no foresight signals
**Check:** Run `run_time_pass()` on store with no foresight-bearing entries.
**Result:** PASS
```
whispers_produced=0
```

### Step 3: Create foresight signal knowledge entry
**Check:** Save a KnowledgeEntry with `foresight_signal="Dentist appointment at 3pm"` and `foresight_expires` 6 hours from now. Verify retrieval.
**Result:** PASS
```
entry_id=know_live3c_dentist, signal=Dentist appointment at 3pm
```

### Step 4: Time pass detects foresight signal
**Check:** `run_time_pass()` finds the entry and produces a whisper with delivery_class="stage" (<12h).
**Result:** PASS
```
whispers=1, text=Upcoming: Dentist appointment at 3pm. This is relevant in the next few hours (ex...
```

### Step 5: Whisper queued after full evaluate
**Check:** `_evaluate()` runs time pass, checks suppression (none), saves whisper to pending queue, emits PROACTIVE_INSIGHT event.
**Result:** PASS
```
pending_total=1, dentist_whispers=1
```

### Step 6: Session-start whisper injection
**Message sent:** `Hey, what's up?`
**Check:** Agent response naturally mentions the dentist appointment.
**Result:** PASS
```
Response: Hey again! Same as before — just here. 😄

Still got that 3pm dentist appointment today if you haven't already taken care of it. Anything on your mind?
```
**Analysis:** Agent wove the insight naturally into conversation — not as a system dump. Exactly as intended by the "LLM owns judgment" principle.

### Step 7a: Suppression created after surfacing
**Check:** After injection, whisper marked as surfaced. Suppression entry created with `resolution_state="surfaced"`.
**Result:** PASS
```
pending_dentist=0, surfaced_suppressions=1
```

### Step 7b: Suppression prevents re-queueing
**Check:** Run evaluator again. Same knowledge entry should NOT produce a new whisper (suppressed).
**Result:** PASS
```
dentist_whispers_after_re_evaluate=0
```
Logs confirmed: `AWARENESS: suppressed whisper=wsp_... signal='Upcoming: Dentist appointment...'`

### Step 8: Dismiss whisper updates suppression
**Check:** Call `_handle_dismiss_whisper()` with the surfaced whisper ID. Suppression updated to `resolution_state="dismissed"`.
**Result:** PASS
```
result=Dismissed whisper wsp_1773818664800069_52af. Won't bring this up again., dismissed=True
```

### Step 9: Knowledge update clears surfaced suppression
**Check:** Create a "surfaced" suppression, then simulate the clearing logic from `llm_extractor.py`. Only surfaced suppressions are deleted.
**Result:** PASS
```
remaining_after_clear=0
```

### Step 10: Expired signal not picked up
**Check:** Create a knowledge entry with `foresight_expires` 2 hours in the past. Time pass should not detect it.
**Result:** PASS
```
expired_whispers=0
```

### Step 11: Queue bounded to max 10
**Check:** Create 15 whispers, enforce bound of 10. Only 10 survive.
**Result:** PASS
```
bound_test_whispers=10, total_pending=10
```
Logs confirmed: `AWARENESS: trimmed whisper=wsp_bound_test_00[0-4] (queue bound 10)`

### Step 12: PROACTIVE_INSIGHT events in event stream
**Check:** Event file contains at least one `proactive.insight` event.
**Result:** PASS
```
Found 2 proactive.insight events
```

### Step 13: Clean evaluator shutdown
**Check:** Start a new evaluator, stop it. No errors, task cancelled cleanly.
**Result:** PASS
```
No errors on stop
```

---

## Acceptance Criteria Mapping

| AC# | Description | Verified |
|-----|-------------|----------|
| 1 | Evaluator runs on interval | Steps 1, 5 |
| 2 | Foresight signals detected | Steps 3, 4 |
| 3 | Whisper injected at session start | Step 6 |
| 4 | Suppression prevents nagging | Steps 7a, 7b |
| 5 | Dismissal suppresses | Step 8 |
| 6 | Resolution clears suppression | Step 9 |
| 7 | Expired signals excluded | Step 10 |
| 8 | Whisper queue bounded | Step 11 |
| 9 | PROACTIVE_INSIGHT events emitted | Step 12 |
| 10 | Evaluator shutdown clean | Steps 1, 13 |
| 11 | Atomic persistence | Unit tests (test_awareness.py) |

---

## Test Statistics

- **Unit tests:** 57 new (926 total, all passing)
- **Live test steps:** 15/15 PASS
- **API calls:** 3 (1 router, 1 reasoning, 1 token count)
- **Evaluator behavior confirmed via logs:** time pass, suppression, queue trimming, event emission

---

## Quick Reference Commands

```bash
# Run live test
source .venv/bin/activate && python tests/live/run_3c_live.py

# Run unit tests only
pytest tests/test_awareness.py -v

# Full suite
pytest tests/ -q

# Check awareness logs
python tests/live/run_3c_live.py 2>&1 | grep "AWARENESS:"

# Inspect whisper queue for a tenant
python -c "
import json
from pathlib import Path
p = Path('data/discord_000000000000000000/awareness/whispers.json')
if p.exists():
    data = json.loads(p.read_text())
    for w in data:
        print(f'{w[\"whisper_id\"]} [{w[\"delivery_class\"]}] surfaced={bool(w[\"surfaced_at\"])} {w[\"insight_text\"][:60]}')
else:
    print('No whispers file')
"
```
