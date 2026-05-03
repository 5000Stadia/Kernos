# Live Test: Lightpanda Web Browser MCP Integration

**Tenant ID (copy-paste):** `discord:000000000000000000`
**Date:** 2026-03-18
**Test script:** `tests/live/run_lightpanda_live.py`
**Result:** FULL PASS (6/6)

**Prerequisites:**
- ANTHROPIC_API_KEY set in .env
- Lightpanda binary at ~/bin/lightpanda (or LIGHTPANDA_PATH env var)
- Binary: v0.2.6, x86_64 Linux

---

## Step-by-Step Test Results

### Step 0: Lightpanda MCP connected
**Check:** After `connect_all()`, capability status is CONNECTED with all 7 tools discovered.
**Result:** PASS
```
status=CapabilityStatus.CONNECTED
tools=['goto', 'markdown', 'links', 'evaluate', 'semantic_tree', 'interactiveElements', 'structuredData']
```

### Step 1: All 7 expected tools discovered
**Check:** MCP tool discovery returns all expected tools.
**Result:** PASS
```
found=['evaluate', 'goto', 'interactiveElements', 'links', 'markdown', 'semantic_tree', 'structuredData']
missing=[]
```

### Step 2: Tool effect classifications correct
**Check:** Read tools (goto, markdown, semantic_tree, interactiveElements, structuredData, links) classified as "read". evaluate classified as "soft_write".
**Result:** PASS
```
reads_correct=True, evaluate_gated=True
```

### Step 3: Web browsing produces content
**Message sent:** `What are the top 5 stories on Hacker News right now? Go to https://news.ycombinator.com and check.`
**Result:** PASS
```
Here are the top 5 on HN right now:

1. **[JPEG Compression](https://www.sophielwang.com/blog/jpeg)** — 129 points, 19 comments *(3 hrs ago)*
2. **[Write up of my homebrew CPU build](https://willwarren.com/2026/03/12/building-my-own-cpu-part-3-from-simulation-to-hardware/)** — 37 points, 3 comments *(2 hrs ago)*
3. **[Mistral AI Releases Forge](https://mistral.ai/news/forge)** — 402 points, 76 comments *(11 hrs ago)*
4. **[A Decade of Slug](https://terathon.com/blog/decade-slug.html)** — 585 points, ...
```
**Analysis:** Agent used `markdown` tool directly (smart — skipped `goto` since `markdown` accepts an optional URL). Real live HN content returned.

### Step 4: Browser tool.called events emitted
**Check:** Event stream contains tool.called events for browser tools.
**Result:** PASS
```
browser tools used: ['markdown']
```

### Step 5: Read tools bypass dispatch gate
**Check:** No dispatch.gate events for read-classified browser tools (they bypass the gate entirely).
**Result:** PASS
```
read gate events: 0 (reads bypass gate — correct behavior)
```

---

## Summary

| Step | Description | Result |
|------|-------------|--------|
| 0 | MCP connected, 7 tools | PASS |
| 1 | All expected tools discovered | PASS |
| 2 | Tool effect classifications | PASS |
| 3 | Live web browsing | PASS |
| 4 | Tool events emitted | PASS |
| 5 | Gate bypass for reads | PASS |

---

## Architecture Notes

- **Binary:** `~/bin/lightpanda` (v0.2.6, x86_64 Linux)
- **Platform constraint:** x86_64 only. ARM deployment needs alternative browser.
- **Capability name:** `web-browser` (registry). Server name: `lightpanda` (MCP).
- **Fix applied:** `registry.get_by_server_name()` added to handle capability name != server name mismatch in promotion loop.
- **Gate behavior:** All browser tools except `evaluate` are "read" → bypass dispatch gate. `evaluate` (JS execution) is "soft_write" → gated.
