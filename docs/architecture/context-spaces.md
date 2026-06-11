# Context Spaces

> Multiple parallel memory threads per member — work, personal, a specific project, a recurring domain — each with its own ledger, its own facts, its own promoted tool set. The user keeps one continuous conversation; the system routes it across specialist threads invisibly.

## The problem

The default shape of a chat assistant is one thread. Everything goes in the thread. A message about a client proposal, a message about a family logistics question, a message about a research deep-dive, a message about a grocery list — all land in the same context window, share the same recent history, share the same pool of relevant memory.

This works for short sessions. It stops working once the user has a life wide enough that the thread can't be a specialist in everything at once. The model that just helped you draft a contract is now being asked to help plan a birthday party; the context it pulls from is half-contract, half-party, half-whatever-you-said-yesterday, and it loses the thread on all three. The typical workaround — *start a new chat* — throws away everything the assistant learned, and now you have two threads that don't know each other.

This is the same shape whether the user is one person juggling domains or a three-person team juggling projects. The thread grows until it breaks, and splitting it into multiple threads fragments the memory that was the whole point of the assistant.

Kernos resolves this with **context spaces** — multiple parallel specialist threads under one conversational identity, routed transparently.

## One conversation, many domains

A context space is an isolated memory surface within a single member's agent. Each space has:

- Its own **ledger** — the conversational arc for that domain, compressed at its own compaction boundary
- Its own **facts** — the structured knowledge durable to that domain
- Its own **promoted tool set** — the tools this domain has surfaced as useful
- Its own **compaction rhythm** — boundary-triggered from this domain's activity, not shared with others
- Its own **procedures** — domain-specific workflows loaded on every turn into the `PROCEDURES` zone

```python
@dataclass
class ContextSpace:
    id: str                          # "space_{uuid8}"
    instance_id: str
    name: str
    member_id: str = ""              # Owner member
    description: str = ""            # Router uses this for routing decisions
    space_type: str = "general"      # "general" | "domain" | "subdomain" | "system"
    parent_id: str = ""              # Parent space ID (empty = root level)
    aliases: list[str] = field(default_factory=list)  # Previous names for routing
    depth: int = 0                   # 0 = root, 1 = domain, 2 = subdomain
    local_affordance_set: dict = field(default_factory=dict)  # Promoted tools
    # ... (see kernos/kernel/spaces.py)
```

Spaces are per-member, not per-install. In a household of three people, or a team of three colleagues, each member has their own set of spaces — member A's "work" space is not member B's "work" space, even though the word is the same.

**Conversation logs** are keyed to the triple `(instance, space, member)`:

```
{data_dir}/tenants/{instance_id}/spaces/{space_id}/members/{member_id}/logs/
```

(`kernos/kernel/conversation_log.py:3-7`)

A member's turn in space A updates the log for `(instance, A, member)`; the log for `(instance, B, member)` is untouched. The agent in space A has no line-of-sight into what was said in space B — per-space isolation of the ledger is enforced by file path.

## The router does the routing

What makes this architecture different is that **the user does not switch spaces**. They just talk. A message lands in the handler, and a lightweight LLM cohort — the **router** — reads the message and the recent conversation and decides which space the turn belongs to. The agent then receives the turn with the space already assigned.

```
handler.py:5553    ctx.router_result = await self._router.route(...)
handler.py:5586    ctx.active_space_id = ctx.router_result.focus
```

The router's job is narrow:

1. **TAG** — which space(s) does this message belong to?
2. **FOCUS** — which single space should get the agent's full attention right now?
3. **CONTINUATION** — is this a short affirmation or reaction that should ride conversational momentum and stay put?

(`kernos/kernel/router.py:19` — the full router system prompt)

The router runs first, before any of the agent's attention is spent. The agent never sees the routing prompt or the routing decision. By the time the agent reads the turn, the space is already selected, the tool catalog is already scoped, the memory is already loaded from *this* space, and the ledger it reasons over is *this* space's ledger.

## Hierarchy: root, domain, subdomain

Spaces form a tree. The default (General) space is the root. Domain spaces hang off the root (work, personal, real-estate, a specific client). Subdomain spaces hang off a domain space (a specific client's Phoenix project; a specific renovation under real-estate).

The router uses the hierarchy when it routes:

- Broad domain content → the parent space. *"What's our standard contract template?"* from within a specific client space steps up to the Legal parent.
- Specific ongoing work → the child space. *"Send the invoice to Martinez"* from the Legal parent steps into the Martinez-specific child.
- When unsure, stay. The cost of a wrong switch (breaking conversational context) is high; the cost of staying is low.

(`kernos/kernel/router.py:31-46`)

Depth is bounded at 2. Spaces don't nest arbitrarily — a three-level tree (General → Business → Phoenix) is the maximum the router is asked to reason about. Deeper nesting produces ambiguity the router can't resolve without becoming expensive.

## How new domains form

New spaces are never created by topic drift alone. Formation is conservative and evidence-driven, in three layers:

1. **Hint accumulation (per turn).** When the router sees a recurring topic that has no space, it tags the message with a snake_case hint (`dnd_campaign`, `kitchen_reno`). Hints are aggregated into a per-space recurrence ledger (`TopicHintLedger`, `data/{instance}/state/topic_hints.json`) — count, first seen, last seen. Recording is best-effort; a ledger failure never affects routing.
2. **Assessment (per compaction).** After each compaction of a general/domain space, a cheap strict-schema model call judges whether the conversation has earned a dedicated space. Its evidence is the *most recent* slice of the compaction document (latest ledger entry + Living State — the document is ordered oldest-first, so a head slice would judge stale content) plus the top accumulated hints as direct recurrence receipts. Creation requires **high** confidence, a non-duplicate name (with drift detection — "DnD" won't fork alongside "D&D"), and respects a space cap. On creation, domain-specific covenants, files, and procedure sections migrate semantically from the parent. System spaces are excluded — platform work never fragments into domains.
3. **Escalation ladder (across assessments).** A medium-confidence verdict is recorded as a *near-miss* rather than discarded. When the same candidate recurs (threshold: 2), the system surfaces a one-time ambient suggestion — "this is starting to look like a recurring D&D thread, want me to give it its own space?" — instead of silently re-judging from zero. Auto-creation stays high-confidence-only; the user gets the borderline call.

(`kernos/messages/handler.py::_assess_domain_creation`; `kernos/kernel/topic_hints.py`; `kernos/kernel/compaction.py::domain_assessment_evidence`)

## Query mode, work mode, continuation

The router returns a structured routing decision, not just a space ID:

- **query_mode** — "the user is asking a quick question about another domain from the current one." The system runs a downward search into that other domain and returns a brief answer into the `RESULTS` zone, but the focus stays in the current space. This is the moment the user says *"by the way, when is our next check-in with Martinez?"* while in the middle of a personal-space conversation — the answer arrives, the conversation doesn't derail.
- **work_mode** — "this is intentional domain-specific work, switch confidently." The user has moved topic deliberately; the system commits to the new space.
- **continuation** — "this is a short affirmation or reaction; ride the momentum." The user said *"ok"* or *"lol"* or *"sounds good"*; the system stays put regardless of what the content hints at.

(`kernos/messages/handler.py:5555-5586`)

These three modes are why space-switching in Kernos feels natural. A household user can say *"remind me to grab milk"* while deep in a work conversation and not have the conversation break; a teammate can ask *"what's the status of the Ortiz proposal?"* from the personal space and get an answer without derailing what they were doing.

## Per-space tool promotion

Each space grows its own tool set. The universal tool catalog (kernel tools, MCP tools, workspace-built tools) is available everywhere in principle, but in practice only a handful of tools are *surfaced* in the prompt for any given turn. The space's `local_affordance_set` tracks which tools have been used here recently and how many tokens they cost:

```python
local_affordance_set: dict = field(default_factory=dict)
# {tool_name: {"last_turn": N, "tokens": N}}
```

(`kernos/kernel/spaces.py:42`)

The three-tier surfacing discipline (described in the [Skill Model](../design-frames/)) runs per-space. A tool the user invokes successfully in the Phoenix project space gets promoted there; it doesn't automatically appear in the personal space, because the personal space is a different cognitive domain with different working patterns.

The payoff: each space becomes a specialist in its own domain. The tool window the agent sees in the Phoenix project is biased toward what Phoenix needs; the tool window in the personal space is biased toward what the personal space needs. The agent never has to browse a catalog of hundreds of tools per turn — it gets the tools that matter *here*.

## Why "100 domains > 100 chat threads"

The README's headline claim about context spaces is that **100 spaces in Kernos is better than 100 chat threads with one model**. The reason is continuity.

- **100 chat threads forget each other.** Each is a fresh context. Nothing one thread learns lands in another.
- **100 chat threads forget the user.** The model in each thread has no memory beyond what fits in its context window.
- **100 chat threads require manual switching.** The user has to decide which thread a message belongs to and navigate there, every time.

Context spaces flip all three:

- **Spaces share an identity.** They're all owned by one member's agent, under one continuous arc. The agent the user is talking to is the same one across every space.
- **Spaces share what should be shared.** Cross-domain notices, whispers, and insights are surfaced when they'd be useful in another domain, governed by disclosure rules; the boundaries are explicit.
- **Spaces don't require switching.** The user talks; the router routes. A conversation that drifts from personal to work to a specific project simply drifts, and each slice lands where it belongs.

The same shape serves a household that maintains a shared calendar, a home-renovation budget, and a kid's-school-logistics thread; and a small team that maintains a client pipeline, an internal-operations thread, and a research thread — without either one discovering at turn 500 that the assistant has forgotten context that mattered on turn 100.

## What this architecture makes easy

- **Spinning up a new domain.** When a user starts talking about something recurring, the router can tag an emerging topic (`snake_case_hint`) and suggest a new space. Once declared, it becomes a first-class memory thread with its own ledger, facts, and tool set.
- **Moving work between domains.** A procedure written in one space can be referenced from another. A fact captured in the personal space can be surfaced into the work space if both domains touch it (disclosure-controlled).
- **Specializing without siloing.** The agent has one continuous identity across spaces. Member-level facts (their name, their timezone, their communication style) travel everywhere; space-level facts (the Phoenix deadline, the renovation budget) stay where they belong.
- **Switching mid-conversation.** The user says *"hey before I forget, did I ever RSVP to the Thompson dinner?"* — the system steps out, answers, steps back in, and the work thread the user was on is undisturbed on return.

## What this architecture explicitly does not try to do

- **It does not enforce strict domain isolation.** Spaces isolate the *ledger*. Member-level state (identity, timezone, relationships) and cross-domain notices are explicitly shared across spaces. The goal is specialization, not compartmentalization.
- **It does not require the user to declare every domain upfront.** Spaces can grow organically — the router tags emerging topics, the user (or the agent) names them when they've earned a place.
- **It does not pretend the router is infallible.** When the router gets it wrong, the user can say *"this is a work thing"* and the system records the correction. The routing decision is always visible in the turn trace for debugging, and the router has its own eval suite for drift detection.
- **It does not treat spaces as agents.** A space is a memory surface, not an independent actor. The agent is one identity per member; the spaces are the domains in which that identity works.

## Related architecture

- **[Cohort architecture](cohort-and-judgment.md)** — the router is the first cohort to run on every turn; spaces are a downstream consumer of its decision
- **[Memory](memory.md)** — how the ledger and facts stores are keyed per-space and reconciled at boundaries
- **[Multi-member disclosure layering](disclosure-and-messenger.md)** — per-member spaces complement per-member identity

## Code entry points

- `kernos/kernel/spaces.py:6` — `ContextSpace` dataclass and the space model
- `kernos/kernel/router.py:114` — `LLMRouter`; the routing cohort
- `kernos/kernel/router.py:19` — the router's system prompt (full text)
- `kernos/kernel/conversation_log.py:3-7` — per-space+member log path discipline
- `kernos/messages/handler.py:5553-5604` — router invocation and space assignment in the turn pipeline
