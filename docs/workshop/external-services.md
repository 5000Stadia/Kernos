# Building tools that touch external services

This guide is for the workshop's user — a Kernos member commissioning a tool that needs to talk to an external service (Notion, GitHub, Slack, anything with credentials). It walks through the worked example of building a small Notion reader.

If you just want to use a stock integration (Notion, Gmail, Slack, GitHub, Drive, Discord-richer), you don't need this — those ship with their service registration already done. This guide is for the workshop case where you're commissioning a custom tool against a service Kernos doesn't ship stock support for.

## The mental model

Tools are universal capabilities — one copy on the host. Invocations are member contexts — every call carries your identity, your data directory, your credentials. The runtime enforces this by handing the tool a `context` object at invocation time. Tools that try to escape the context (hardcoded paths, hardcoded member IDs) get rejected at registration with the equivalent of a "this app tried to write to System32 instead of AppData" message.

You write tools using `context.data_dir` for files, `context.credentials.get()` for tokens, and `context.member_id` if you need to know who you are. The runtime takes care of isolation.

## Worked example: read a Notion page

The complete flow has four steps. Skip ahead to whichever you need.

### 1. Onboard your Notion credential

```
python -m kernos.credentials onboard --service notion
```

This is the API-token flow. The CLI prompts for your Notion integration token (created in your Notion workspace's settings → integrations); the token gets encrypted and stored under `data/<instance>/credentials/<your_member_id>/notion.enc.json`.

The matrix only allows API-token onboarding from the CLI — pasting a long-lived secret into Discord, SMS, or Telegram would leave it sitting in adapter history. If you try to onboard via Discord, the flow refuses with a clear pointer to the CLI alternative.

(For services that support OAuth device-code, onboarding works from any adapter — you'd see a short code on Discord, type it on the service's website on your phone, and the flow completes.)

### 2. Write the tool

Two files in your space's directory:

`notion_reader.tool.json`:

```json
{
  "name": "notion_reader",
  "description": "Read the markdown content of a Notion page by ID.",
  "input_schema": {
    "type": "object",
    "properties": {
      "page_id": {"type": "string"}
    },
    "required": ["page_id"]
  },
  "implementation": "notion_reader.py",
  "service_id": "notion",
  "authority": ["read_pages"],
  "operations": [
    {"operation": "read_pages", "classification": "read"}
  ],
  "audit_category": "notion"
}
```

The new fields explained:
- `service_id: "notion"` binds this tool to the Notion service registration. The runtime will hand the tool a credentials accessor scoped to your Notion token.
- `authority: ["read_pages"]` declares what the tool can do. Notion's service descriptor declares `read_pages` as a valid operation; the registration validates the subset.
- `operations: [...]` classifies the operation for the dispatch gate. `read_pages` is a read; the gate doesn't fire confirmation. (A tool that wrote pages would classify `write_pages` as `hard_write`, which fires confirmation.)
- `audit_category: "notion"` is the operator-readable label for the audit log.

`notion_reader.py`:

```python
import httpx

NOTION_API = "https://api.notion.com/v1"

def execute(input_data, context):
    page_id = input_data["page_id"]
    credential = context.credentials.get()
    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Notion-Version": "2022-06-28",
    }
    with httpx.Client() as client:
        resp = client.get(f"{NOTION_API}/blocks/{page_id}/children", headers=headers)
        resp.raise_for_status()
        return {"blocks": resp.json().get("results", [])}
```

The tool uses `context.credentials.get()` — the runtime returns *your* Notion token only when *you* invoke the tool. If your spouse-member invokes the same tool, they get *their* Notion token, against their workspace.

You write nothing about your member ID. You write nothing about file paths. The runtime context is the only authority on those.

### 3. Register the tool

From the agent: "Register the notion_reader tool I just wrote." The agent calls `register_tool` with your descriptor file; the registration validates the descriptor against the Notion service registration, scans the implementation for unsafe patterns, computes the registration hash, and stores the tool in the catalog.

If your implementation has hardcoded paths or member IDs, you'll see an error message that names the offending lines and quotes the AppData / System32 analogy. Fix the code or register with `force=True` to override (force-registered tools surface only to you).

### 4. Invoke

Just call the tool by name, like any other. The dispatcher:

1. Verifies the tool's hash matches what was recorded at registration.
2. Verifies the invoked operation is still in your authority and Notion's declared operations.
3. Verifies your Notion credential exists and isn't expired.
4. Builds the runtime context with your member ID, your data directory, your credentials.
5. Runs the tool.
6. Writes an audit entry with your member ID, the operation, the SHA-256 digest of the input payload, and the result status.

If anything between registration and invocation drifted (you edited the implementation, the Notion service descriptor lost an operation, your credential expired), the corresponding check fires and surfaces a clear message. The tool doesn't run.

## When to register with `force=True`

Almost never. Force is for the case where a finding is intentional and unavoidable — for example, a tool that deliberately reads from a fixed shared file your operator manages outside of Kernos's per-member sandbox. Force-registered tools surface only to the author; other members never see them. Runtime enforcement still applies, so even a force-registered tool can't escape its credentials scoping or its sandbox at invocation time.

If you find yourself reaching for force often, the right move is usually to widen the runtime context (a feature request) or to use the workshop differently. The author's first instinct should be to fix the finding, not bypass it.

## Cross-member tools

Tools that aggregate data across members (a household-spending summary, a shared invoice register) are reserved for v2. The descriptor enum value `aggregation: cross_member` exists in the validation surface but registration rejects it with a pointer to the future spec. v1 tools are member-scoped only.

## What this primitive does not solve

- **OAuth redirect flows that need a public callback URL.** Server deployments often don't have one. Use OAuth device-code instead, or wait for a deployment-topology spec to land that addresses redirect-flow support.
- **Cookie-upload auth.** The enum value is reserved for `BROWSER-COOKIE-IMPORT`. Until that spec ships, services without an API alternative cannot be onboarded.
- **UI for credential management.** v1 is CLI plus adapter prompts. Richer UX is future work.
- **Federation across Kernos instances.** Out.

If your tool needs one of these, it's the primitive that needs to grow — not your tool. File a kick-back through the design review inbox naming the gap.
