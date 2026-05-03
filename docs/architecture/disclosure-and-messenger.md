# Multi-Member Disclosure Layering

> The architecture that lets one personal agent serve multiple people in the same household or team without collapsing their privacy into a single shared context.

## The problem

A personal agent that serves more than one person has a problem most agent frameworks don't acknowledge. The obvious failure mode is easy: don't let member A read member B's private data. Every serious system has some form of access control for this.

The interesting failure mode is the one access control cannot fix: **member A is allowed to see something about member B, but telling member A right now would damage member B.** Your spouse *can* see your calendar — you share one — but you haven't told them yet about the therapy appointment you booked for Thursday. Or: your co-owner *can* query the shared project tracker — you built it together — but they haven't seen yet that you've flagged your strongest engineer as a flight risk. The permission matrix says "allow." The relationship, or the moment, says "not yet."

A single-user chatbot never hits this. A multi-user system with strict isolation never hits this. A multi-member system with realistic relationship graphs — whether the members are family or colleagues — hits this every day.

Kernos treats this as a two-layer problem and solves it with two architectural elements that sit on top of a conventional permission matrix.

## The layers

```
Layer 0 — Per-member identity       one hatched agent per member
Layer 1 — Permission matrix         can this exchange happen at all?
Layer 2 — Messenger cohort          should the response, as written, actually be sent?
```

Each layer gates on a different axis. The three axes don't overlap and don't substitute for each other.

### Layer 0 — Per-member identity

Kernos hatches one agent per member, not one per installation. When a household of three people — or a three-person product team — uses Kernos, there are three agents, three memory threads, three sets of covenants. The agents are siblings — same kernel, same code, same behavioral substrate — but their context windows never touch. Nothing member A tells their agent appears in member B's agent's context. This is table-stakes isolation; the interesting work happens on top of it.

Members are created through invite codes (`KERN-XXXX`, platform-locked) and go through a guided onboarding where the agent is unnamed until the member names it. Each member's agent has its own display name, its own ledger, its own facts, its own relationship permissions. From the agents' point of view, other members are external parties whose access to each member's information is explicitly declared.

### Layer 1 — Permission matrix

Relationships between members are pairwise and directional. For any ordered pair `(A, B)`, Kernos records one of four profile values representing "how much of A's world can B see":

- **`full-access`** — B can ask A's agent anything, same-as-self
- **`by-permission`** — B can ask A's agent topic-scoped questions; disclosure happens per-covenant
- **`read-only`** — B can observe status and presence, cannot query private data
- **`none`** — the default for non-declared relationships

The matrix is consulted first on every cross-member exchange (`kernos/kernel/relational_dispatch.py:150`). If the requested intent isn't permitted under the current profile, the exchange is rejected with a structured reason before any LLM work happens. This is the deterministic, lookup-based layer — no judgment, no LLM call, just a matrix query against declared state.

The matrix is declared by the members themselves during onboarding and adjustable afterward. Kernos never infers permissions from inferred closeness or from content patterns; the relationship is a first-class declared object, not an emergent property.

### Layer 2 — Messenger cohort

After the permission matrix says "this exchange can happen," a second layer runs: the **Messenger cohort**. The Messenger is a bounded LLM worker that answers one question — *"does the response as written, in this moment, serve the disclosing member's welfare?"* — and returns one of three outcomes:

- **Unchanged** — pass the response through as-is; no welfare concern
- **Revise** — rewrite the response to honor what the disclosing member has shared about what matters
- **Refer** — the underlying question is real, but the right response is to hand it back to the disclosing member themselves rather than smooth over it

The Messenger is the welfare layer, not the permission layer. Permission has already been granted. The question Messenger answers is strictly about *this response, in this context, to this addressee, given what the disclosing member has told me matters* — a judgment the permission matrix has no mechanism to make.

## Why this isn't just a second filter

The natural critique of a two-layer design is that it looks redundant — why not fold the welfare judgment into the permission profile? Three reasons that shaped the architecture:

**Permissions are structural; welfare is contextual.** A permission profile is a stable declaration about a relationship. It changes when people decide it changes. A welfare judgment is a contextual evaluation of a single moment: *this disclosure, this addressee, this phrasing, this point in the disclosing member's arc.* The same permission profile generates different welfare outcomes every day. Trying to encode welfare into the permission layer means either making permissions ephemeral (brittle) or carrying every contextual nuance into the matrix (intractable).

**Permissions are deterministic; welfare is judgment.** The permission matrix is a lookup against declared state — fast, predictable, testable, never wrong for the reason the user can't see. The Messenger is LLM judgment — slower, probabilistic, works-most-of-the-time, can be prompt-iterated when it drifts. Collapsing them erases the property that makes permissions trustworthy.

**Permissions scale linearly; welfare scales with the relationship.** Adding a new member adds a row to the matrix. Adding a new relationship nuance (the therapy appointment, the unannounced reorg, the surprise party, the compensation number) doesn't touch the matrix at all — it becomes new evidence the Messenger consults on the next exchange. Keeping them separate means relationship evolution doesn't force schema change.

## The judgment inputs

The Messenger's context is deliberately minimal. It sees only what's needed to make the welfare call:

```python
@dataclass(frozen=True)
class ExchangeContext:
    disclosing_member_id: str
    disclosing_display_name: str
    requesting_member_id: str
    requesting_display_name: str
    relationship_profile: str            # "full-access" | "by-permission" | ...
    exchange_direction: Literal["outbound", "inbound"]
    content: str
    covenants: list[CovenantEvidence] = field(default_factory=list)
    disclosures: list[Disclosure] = field(default_factory=list)
```

(`kernos/cohorts/messenger.py:71`)

What the Messenger does *not* see: routing metadata, turn history, unrelated covenants, adapter type (SMS vs Discord), or any context from outside this exchange. The judgment-vs-plumbing boundary is enforced by keeping the input struct small.

What the Messenger does see: the two members involved, their relationship profile, the message content itself, a list of covenants the disclosing member declared that match this exchange's topic or addressee, and a list of relevant disclosures the disclosing member has shared with Kernos over time.

**Covenants** are user-declared rules like *"never mention my therapy to my parents,"* *"don't share my compensation specifics with anyone but the CEO,"* or *"escalate any concerns about Dad's medical appointments directly to me."* They're structured objects with `rule_type`, `topic`, and optional `target` anchors — matching the exchange requires a deliberate match against the declared structure, not vibe-matching a raw string.

**Disclosures** are facts the disclosing member has shared with Kernos about their own life, carrying a sensitivity hint (`"open" | "contextual" | "personal" | ""`). Disclosures are how the Messenger learns what's sensitive without the disclosing member having to pre-emptively declare a rule for every sensitive topic. If a member has told Kernos about the therapy appointment, or about being taken off a project, or about an interview process that isn't yet public, that disclosure is available to the Messenger when a related exchange happens later.

## The three outcomes in detail

### Unchanged

The default outcome. No covenant applies; no disclosure flags a welfare concern; the response as-written already serves the disclosing member's interests. The Messenger returns `None` from `judge_exchange` and the exchange passes through to the addressee with the original content.

Most exchanges land here. The Messenger's purpose is not to intervene by default but to catch the specific cases where the simple forwarding would cause harm.

### Revise

The exchange can happen and should happen, but the response as written crosses a line. The Messenger rewrites the response to honor what the disclosing member has declared.

Example from a family context: member A has a covenant *"don't tell my mom how stressed I've been about work."* Member B (Mom) asks A's agent "how is A doing?" The permission matrix permits the exchange — Mom has `by-permission` access and is asking a well-formed question. The Messenger sees the covenant, sees A has declared work stress is off-limits with Mom, and rewrites the response from *"A has been dealing with a stressful quarter at work"* to *"A is doing well, busy with the usual things."* The exchange happens; the response honors the covenant.

Example from a team context: member A has a covenant *"my compensation numbers don't leave this conversation."* Member B, a teammate with `by-permission` access, asks A's agent *"what did A get for the bonus cycle?"* The Messenger sees the covenant and rewrites the response from *"A's bonus was $X"* to *"A's compensation isn't something I can share on A's behalf — check with A directly if you want to compare notes."* Permission stayed; the covenant was honored.

The Messenger returns a `MessengerDecision` with the revised content, which the dispatcher uses in place of the original.

### Refer

The underlying question is real and the addressee deserves a response, but "smoothing harder" would produce a false impression. The right move is to hand the question back to the disclosing member themselves.

Example from a family context: Mom asks A's agent "is A still seeing Jordan?" Member A broke up with Jordan last week and hasn't told Mom yet. There's no clean answer A's agent can give — a yes is false, a no discloses what A hasn't chosen to disclose, and an evasion ("you should ask A") is itself informative. The Messenger chooses refer: it returns a holding response to Mom ("that's something to talk with A about directly") and surfaces a whisper on A's own next turn — *"Mom asked about Jordan today; I told her to check with you. Flag if you want me to handle differently in the future."*

Example from a team context: a colleague asks A's agent *"is A still leading the Phoenix project?"* A was quietly reassigned off Phoenix yesterday and hasn't told the team yet. Yes is false; no discloses a personnel change that isn't the agent's to announce; an evasion is its own signal. The Messenger refers — a holding response ("*that's worth a check-in with A directly*") plus a whisper on A's next turn that the question came up, so A decides when and how to say more.

Refer is the Messenger's most distinctive outcome. It's the option most agent frameworks don't have at all because they lack a notion of routing-a-question-back-to-the-disclosing-party as a first-class dispatcher directive.

## The always-respond invariant

A critical design property: **every exit path from `judge_exchange` produces a response.** There is no silent-fail, no dropped-message, no "the agent just didn't answer." Every path either passes the original content, substitutes a revised content, refers gracefully, or — on Messenger-chain exhaustion — raises `MessengerExhausted`, which the dispatcher catches and delivers as a pre-rendered default-deny response through the platform adapter.

The addressee always hears something. The disclosing member always retains control over what that something is. Silence is never the answer Kernos chooses, because silence is itself disclosure — the absence of a response is interpretable.

## Where the Messenger lives in the turn pipeline

The dispatcher enforces the two-layer sequence deterministically:

```
1. Resolve addressee (exact match or disambiguation)
2. Look up permission profile (origin → addressee)
3. If intent not permitted under profile → reject with structured reason
4. [MESSENGER-COHORT hook] Invoke welfare judgment callback
5. Persist envelope with (possibly revised) content
6. Deliver via platform adapter
```

(`kernos/kernel/relational_dispatch.py:172-201`)

The Messenger hook is a single call in the dispatcher that returns either `(content, None)` for pass/revise outcomes or `(holding_content, whisper)` for refer outcomes. The hook itself never raises — it catches exceptions and translates them into safe dispatch directives. Failure of the Messenger LLM call degrades gracefully to permission-only enforcement, which is the same behavior as running without the cohort.

This keeps the dispatcher's control flow boring. Welfare judgment is a callback returning structured data; the dispatcher doesn't know or care whether the judgment came from an LLM, a deterministic rule engine, or a coin flip.

## Why the Messenger runs on cheap-chain only

The Messenger uses Kernos's `cheap` fallback chain by design. No primary-chain escalation on failure. This is a deliberate architectural constraint rooted in the principle that welfare judgment should be fast, cheap, and prompt-iteration-improved rather than expensive.

If the cheap chain proves inadequate for a pattern of cases, the fix is **prompt iteration** — the Messenger's prompt gets sharper, new scenarios get added to the eval suite, the next iteration ships. The fix is not *"spend more money per exchange."* A Messenger that requires the primary chain to get welfare right is a Messenger that's too expensive to run on every exchange, and a Messenger that doesn't run on every exchange is one that has welfare gaps nobody can audit.

The MESSENGER-PROMPT-ITERATION spec is the active iteration cycle. Each round consists of running the full Messenger eval suite, identifying the weakest-performing scenarios, refining the prompt to address them, and verifying the refinement doesn't regress previously-passing scenarios. This is the principled tradeoff: prompt iteration is cheap and reversible; chain escalation is expensive and structurally hides problems.

## What this architecture makes easy

The multi-member disclosure layering makes a specific class of real-life dynamic legible to the system without requiring that members pre-declare every sensitive topic:

- **Evolving relationships.** A member adds a new covenant, and it takes effect on the next exchange. No schema migration, no retraining.
- **Temporary sensitivity.** A member discloses something they don't want surfaced *right now* but might be fine with in a month. The disclosure is evidence the Messenger consults; it doesn't need to be a permanent rule.
- **Surprise and discretion.** A household member planning a surprise party — or a team lead preparing a confidential product launch — can share the plan with Kernos knowing the Messenger will keep it from the honoree (or the unbriefed teammate) without needing a formal permission change.
- **Graceful referral.** Hard questions — medical status, relationship changes, financial decisions, unannounced role or strategy changes — land as referrals to the disclosing member rather than as agent-speculations that the disclosing member has to clean up later.

The system does not try to be omniscient about household or team dynamics. It tries to be the kind of thoughtful steward that errs toward the disclosing member's stated interests, refers when it's uncertain, and never silently fails.

## What this architecture explicitly does not try to do

- **It does not model conflict resolution between members.** The Messenger serves the disclosing member's welfare; if member A and member B have a conflict, the Messenger does not adjudicate it.
- **It does not replace trust.** A household or team whose members don't trust Kernos with sensitive information gets a less useful Kernos. The architecture assumes disclosure is voluntary and cumulative; it does not coerce or bait disclosure.
- **It does not prevent all leakage in principle.** A sufficiently adversarial member asking enough carefully-phrased questions can in principle extract information through inference. The Messenger reduces the common-case leak paths and makes the adversarial case detectable; it does not claim to be impregnable to a dedicated attacker who is also an authorized member of the household or team.
- **It does not replace direct human communication.** The refer outcome exists precisely because the architecture knows its own limits — some things should pass between humans directly, and the right move is to hand the question back rather than answer it.

## Related architecture

- **[Cohort architecture](cohort-and-judgment.md)** — the judgment-vs-plumbing discipline the Messenger embodies
- **[Infrastructure-level safety](safety-and-gate.md)** — the dispatch gate for single-member actions; parallel architecture at a different axis
- **[Context spaces](context-spaces.md)** — per-member space isolation that complements per-member identity

## Code entry points

- `kernos/cohorts/messenger.py` — the Messenger cohort itself; `judge_exchange` is the entry point
- `kernos/cohorts/messenger_prompt.py` — the Messenger's prompt construction, iteration surface
- `kernos/kernel/relational_dispatch.py:150-201` — the two-layer dispatch sequence
- `kernos/cohorts/messenger.py:71` — the `ExchangeContext` struct, the judgment-input boundary
