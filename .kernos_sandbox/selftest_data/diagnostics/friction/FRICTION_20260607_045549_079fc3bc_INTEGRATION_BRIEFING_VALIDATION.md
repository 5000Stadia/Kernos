# Friction Report: INTEGRATION_BRIEFING_VALIDATION
Generated: 2026-06-07T04:55:49.590146+00:00

## Description
Integration synthesis failed after 3 retry attempts. Final attempt's component was `briefing_validation`. The retry harness surfaced a hard system-error to the user; this report captures per-iteration timing so the bottleneck (model latency / tool dispatch / payload size) can be attributed.

## Recommendation: INVESTIGATE
Review per-iteration metrics below. Likely levers, in rough order of cost: (1) extend `KERNOS_INTEGRATION_TIMEOUT_SECONDS` if model+dispatch wall-time consistently overruns the budget by a small margin; (2) shrink tool-result payloads if `tool_result_chars` dominates; (3) parallelize multi-target retrieval if the model issues several `request_reference` calls sequentially; (4) swap the `lightweight` chain to a faster provider if model_ms dominates and payloads are small.

## Context
- instance_id: `repl:selftest`
- member_id: `mem_c1316361`
- space_id: `space_76f2f954`
- turn_id: `operator`
- integration_run_id: `int-613cc4a3a71c`
- integration_timeout_seconds: `600.0`
- max_iterations: `50`
- max_retries: `3`

## User message (preview)

```
Hey, I want to see you actually use everything you've got. Go read your self-test checklist — it's in your docs at docs/V1-SELF-TEST.md — and then just work through it all yourself, for real: actually do each thing with your own tools, not describe it. Take them one at a time. Don't pass it off to another agent — I want to see you do it. When you're done, write up how it went in a file and tell me straight what worked, what was rough, and where you're at.
```

## Per-attempt breakdown

### Attempt 1/3 — `briefing_validation`
- iterations: 2
- tools_called: ['read_file:iter1']
- phase_durations_ms: {'collect': 0, 'integrate_iter_1': 3157, 'dispatch_iter_1': 2590, 'integrate_iter_2': 36137}
- per-iteration metrics:
  - iter 1: model_ms=3157, dispatch_ms=2590, tool=`read_file`, tool_result_chars=6069, input_tokens=7964, output_tokens=86
  - iter 2: model_ms=36137, dispatch_ms=None, tool=``, tool_result_chars=None, input_tokens=9559, output_tokens=1929

### Attempt 2/3 — `briefing_validation`
- iterations: 2
- tools_called: ['read_file:iter1']
- phase_durations_ms: {'collect': 0, 'integrate_iter_1': 3738, 'dispatch_iter_1': 31, 'integrate_iter_2': 31445}
- per-iteration metrics:
  - iter 1: model_ms=3738, dispatch_ms=31, tool=`read_file`, tool_result_chars=6069, input_tokens=8057, output_tokens=82
  - iter 2: model_ms=31445, dispatch_ms=None, tool=``, tool_result_chars=None, input_tokens=9652, output_tokens=1429

### Attempt 3/3 — `briefing_validation`
- iterations: 2
- tools_called: ['read_file:iter1']
- phase_durations_ms: {'collect': 0, 'integrate_iter_1': 4110, 'dispatch_iter_1': 27, 'integrate_iter_2': 26894}
- per-iteration metrics:
  - iter 1: model_ms=4110, dispatch_ms=27, tool=`read_file`, tool_result_chars=6069, input_tokens=8098, output_tokens=117
  - iter 2: model_ms=26894, dispatch_ms=None, tool=``, tool_result_chars=None, input_tokens=9693, output_tokens=1425

## Aggregate signals

- total iterations across all attempts: 6
- sum model_ms: 105481
- sum dispatch_ms: 2648
- max tool_result_chars: 6069
- tools observed: ['read_file']
- **Hypothesis:** model latency dominates (sum_model >> sum_dispatch). Lever: faster chain or smaller input.