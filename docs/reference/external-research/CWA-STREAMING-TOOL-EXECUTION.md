# CWA Reference: Streaming Tool Execution

**Source:** ClaudeWebagent (Research/ClaudeWebagent/)
**Relevance:** Performance optimization for Kernos — tools begin executing during API streaming, not after.

---

## Key Insight

CWA starts executing tools **during the API response stream**, not after it completes. When the API yields a complete `tool_use` block (at `content_block_stop`), execution begins immediately while remaining blocks are still streaming.

For a turn with 5 parallel tool calls, this means tool A can finish before tool E even arrives from the API.

---

## Architecture

### StreamingToolExecutor

The executor maintains a queue of tools in three states: `queued` → `executing` → `completed` → `yielded`.

**addTool()** — called as each `content_block_stop` event yields a complete tool_use block from the SSE stream. Adds to queue, triggers `processQueue()`.

**processQueue()** — concurrency-aware dispatcher:
- **Concurrent-safe tools** (read-only): multiple can run in parallel
- **Non-concurrent tools** (writes): must execute alone
- Determination via `tool.isConcurrencySafe(parsedInput)` — a per-tool function

**getCompletedResults()** — non-blocking generator called during the API stream loop. Yields tool_result messages for completed tools. Progress messages (partial output) are yielded immediately.

### Tool Input Accumulation

Tool inputs arrive as JSON fragments via `input_json_delta` SSE events. The SDK accumulates these into a string. At `content_block_stop`, the full input is parsed and the tool_use block is emitted. The tool is NOT considered ready until `content_block_stop` — no partial execution.

### Parallel Execution Flow

```
API Stream Start
  ↓ (tool_use A completes at content_block_stop)
addTool(A) → A starts executing
  ↓ (tool_use B completes while A is running)
addTool(B) → B starts if concurrent-safe with A
  ↓ (A finishes while B still running and C streaming)
getCompletedResults() → yields A's tool_result
  ↓ (B finishes, tool_use C completes)
addTool(C) → C starts, yields B's result
  ↓ (API stream ends, C still running)
getRemainingResults() → waits for C, yields its result
  ↓ (all tools complete)
Feed tool_results back → next API call
```

### Error Handling

- **Bash errors cascade**: If a Bash tool fails, all sibling tools are aborted via `siblingAbortController.abort('sibling_error')`. Non-Bash errors don't cascade.
- **Synthetic error messages**: Aborted tools get synthetic `tool_result` with `is_error: true` and explanation ("Cancelled: parallel tool call X errored").
- **Streaming fallback**: If the SSE stream fails mid-response, the executor is `discard()`ed and rebuilt. Tombstone messages are yielded for orphaned assistant messages.

---

## Applicability to Kernos

### Current Kernos Architecture
Kernos's reasoning loop (`reason()` in reasoning.py) calls `_provider.complete()`, waits for the full response, then processes all tool_use blocks sequentially or in concurrent batches. The Codex provider streams SSE but collects the entire response before returning.

### What Would Change

1. **Provider returns an async generator** instead of a complete ProviderResponse. Each `content_block_stop` yields a complete tool_use block.

2. **Reasoning loop processes tools during stream** — `_execute_single_tool()` fires as each block arrives.

3. **Concurrency classification** already exists — `_is_concurrent_safe()` in reasoning.py. The infrastructure for parallel dispatch is partially there.

4. **The Codex SSE provider** already processes SSE events in `_collect_sse_response()`. Converting it to yield tool_use blocks at `content_block_stop` (instead of collecting everything) is architecturally straightforward.

### Estimated Impact

For a 5-tool turn where each tool takes ~1s:
- **Current**: API response (~2s) + sequential tools (~5s) = ~7s
- **Streaming + parallel**: API response streams while tools run = ~3-4s

The biggest wins come from:
- Long-running MCP tools (browser navigation, web search)
- Parallel read tools (list-events + get-current-time + remember)

### Complexity Cost

Medium. Requires:
- Provider interface change (complete → async generator)
- Reasoning loop restructure (collect-then-process → process-as-streaming)
- Tool result ordering guarantees (maintain message-order for API)
- Error cascading logic (which tool failures abort siblings)

Recommend as a Phase 5+ optimization, not a prerequisite.
