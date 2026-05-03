## ⚠️ HISTORICAL — Last updated 2026-02-27

This document captures the original vision. For current architecture see
`docs/TECHNICAL-ARCHITECTURE.md`. For current planning see the Notion workspace.

# KERNOS

## Master Project Blueprint v0.1

> **The Name:** KERNOS draws from an ancient Greek ceremonial vessel — a central ring with multiple small cups attached, each holding different offerings. As an architectural metaphor: a central kernel with multiple agents and memory containers (MemCubes) attached, each holding their own context, all connected to the same governing structure. Phonetically evokes “kernel OS,” immediately legible to technical audiences. Expands to *Kernel for Extensible Runtime, Networked Orchestration Services* — though the name doesn’t need the acronym.
> 
> **What this document is:** The single source of truth for this project. Bring this file to every Claude session and every Claude Code session. It contains the full vision, architectural decisions, implementation plan, and current status. Update the STATUS sections as work is completed.
> 
> **Project origin:** Conversation on February 26, 2026, between the owner and Claude, deriving agentic OS principles from first principles of traditional operating systems.

-----

## PART 1: VISION & PHILOSOPHY

### The One-Sentence Pitch

A personal intelligence that lives in the cloud, works for you 24/7, is reachable by text message, and earns your trust through thousands of correct small actions.

### The Core Insight

Every existing agentic OS project is built for developers. Nobody has built the system where a non-technical person texts a phone number and has agents working for them within an hour. The technology exists. The protocols exist. The kernel concepts are proven. What’s missing is the design that makes it all disappear into usefulness.

### The Historical Analogy

LLMs today are where computers were in the 1960s — powerful but singular-purpose, accessed through specialists. The agentic OS is the missing layer that transforms raw capability into a universal tool, just as traditional operating systems transformed mainframes into personal computers.

### Four Design Principles

1. **Conservative by Default, Expansive by Permission.** The system does little autonomously out of the box. It watches, learns, suggests, and asks. Over time, as you approve actions, it learns what it’s trusted to do silently. Trust is earned, not assumed.
1. **Memory as the Moat.** The longer you use it, the more valuable it becomes. But memory is yours — exportable, portable, inspectable. The moat is quality, not lock-in.
1. **Ambient, Not Demanding.** The system feels like an environment you inhabit, not a tool you use. It surfaces things when they matter and stays quiet otherwise.
1. **No Destructive Deletions.** KERNOS never destroys user data. Every “delete” operation is a relocation to a shadow archive. The system treats storage as cheap and user data as irreplaceable. See Part 3 for full specification.

### Why Not Just Use Claude/ChatGPT?

**Claude is someone you talk to. KERNOS is something that works for you.**

A chatbot — even one with memory and tool access — is reactive. You open it, ask a question, get an answer, close it. KERNOS is an operating system: it runs when you’re not looking, it manages resources on your behalf, and it assembles the right capabilities around your needs without you thinking about integrations.

**The plumber example:** A plumber texts the KERNOS number: “Can you schedule customers for me?” The system doesn’t just answer the question — it provisions the infrastructure. It connects calendar access, offers to store customer contacts automatically, sets up inbound scheduling so customers can text the same number, asks about available hours, and learns the plumber’s patterns over time. If the plumber later asks about invoicing, billing tools get piped in. The plumber never thinks about “tools” or “integrations” — they said what they needed, and the system assembled the right capabilities, the way an OS allocates memory, file handles, and network sockets when a program requests them.

**Three things KERNOS does that a chatbot cannot:**

1. **Works when you’re not talking to it.** Agents run 24/7 — monitoring email, fielding scheduling requests from your customers, triaging what needs your attention.
1. **Dynamically provisions capabilities.** The system discovers what tools and services you need based on what you’re trying to do, and connects them. You describe your work; the OS assembles the resources.
1. **Accumulates operational understanding.** Not just “remembers facts about you” but builds a working model of your business, your preferences, your relationships, your patterns — and every agent benefits from that shared understanding.

-----

## PART 2: THE SIX PILLARS

These are the defining structural characteristics of any agentic OS. Each must be present for the system to deserve the name. Removing any one breaks the system fundamentally.

### Pillar 1: Capability Abstraction

Uniform access to tools, data, and models. Agents don’t need to know which LLM, which API, or which data source they’re using — the OS routes requests to the appropriate capability through a uniform interface.

### Pillar 2: Agent Lifecycle & Resource Management

Spawning, scheduling, suspending, resuming, and terminating agents. Managing token budgets, compute allocation, and priority. The process scheduler of the agentic world.

### Pillar 3: Persistent Context (Memory)

The system’s evolving, accumulated understanding of the user and their world. Not a flat profile — a living, versioned, composable knowledge structure that every agent can query and contribute to.

### Pillar 4: Identity, Trust & Boundaries

Who agents are, what they’re allowed to do and know, and the enforcement of those limits. Includes graduated autonomy — agents earn broader permissions over time through demonstrated reliability.

### Pillar 5: Inter-Agent Communication & Coordination

Discovery, messaging, delegation, and collaboration between agents. Both internal (within the OS) and external (across the internet with other users’ agents).

### Pillar 6: The User-System Interface Contract

The bidirectional rules governing how humans express intent and how the system presents its work. Includes the SMS entry point, the app experience layer, the briefing surface, and the trust dashboard.

-----

## PART 3: ARCHITECTURAL DECISIONS

### Kernel: Built from First Principles

The KERNOS kernel architecture was derived from first-principles analysis of what traditional operating system concepts (process scheduling, memory management, resource abstraction, access control) look like when applied to LLM-based agents serving non-technical users.

- **Origin:** A February 26, 2026 conversation between the owner and Claude, deriving agentic OS pillars from traditional OS theory. Refined through architectural brainstorming and a structured interview with OSBuilder (OpenClaw's primary agent), whose real-world experience running a production agent system validated key design decisions and revealed critical failure modes.
- **Architecture:** Five primitives (Event Stream, State Store, Capability Graph, Reasoning Service, Task Engine) composing into three operational modes (reactive, proactive, generative). See `docs/reference/kernel-architecture-outline.md` for the full design.
- **Core inversion:** "The agent thinks, the kernel remembers." Unlike existing systems where agents manage their own memory, tool discovery, and safety enforcement, KERNOS separates reasoning (agent) from infrastructure (kernel). The agent receives pre-assembled context, reasons about the current moment, and returns a response. Everything else is kernel responsibility.
- **Language:** Python for faster iteration in the kernel. No external kernel codebase is forked or integrated — the kernel is KERNOS-native.

**Prior art acknowledged:** The AIOS project (Rutgers University, COLM 2025) independently validated the OS-as-metaphor approach for LLM agent systems. Their work on treating LLMs as cores and building scheduling abstractions was useful early validation that the conceptual direction was sound. However, AIOS solves a fundamentally different problem — efficient GPU resource scheduling for concurrent agent workloads — and shares no architectural DNA with KERNOS. KERNOS is an application-layer kernel mediating between user intent and AI capabilities, not a hardware resource scheduler.

### Memory Architecture: Open Design Problem

Long-term memory is the hardest unsolved architectural problem in KERNOS and the foundation of the project’s competitive moat (Pillar 3: "Memory as the Moat"). The current implementation provides persistence infrastructure (event stream for raw history, State Store for structured facts), but the memory architecture — how the system retrieves, consolidates, decays, and composes knowledge over months and years of use — is not yet designed.

**What we have today:**

- An append-only Event Stream that captures everything that happens (messages, tool calls, agent actions, capability changes). This is a transaction log, not a query surface.
- A State Store that holds structured, queryable state (user facts, behavioral contracts, capability state). Currently implemented as simple file-based storage with tenant isolation.
- Memory projectors (designed, not yet built) that will extract facts from the event stream and write them to the State Store.

**What we need and don’t yet have:**

- **Semantic retrieval** — finding relevant knowledge across thousands of accumulated facts, not just keyword lookup. "What did we discuss about the Portland project?" requires understanding, not string matching.
- **Memory consolidation** — compressing and strengthening frequently-accessed knowledge while gracefully decaying unused information. Without this, the State Store grows without bound and relevance degrades.
- **Temporal reasoning** — understanding when things happened and how knowledge has changed. "What did I tell you last March?" is a time-scoped query against accumulated knowledge.
- **Composable context assembly** — assembling the right subset of knowledge for a specific agent, task, and moment. The inline annotation pattern (placing relevant context exactly where it’s relevant in the user’s message) requires sophisticated retrieval and relevance scoring.
- **Provenance and versioning** — tracking where knowledge came from and how it’s changed. "User’s name is Greg" (stated directly) vs. "User prefers afternoon meetings" (inferred from 12 interactions) have different confidence levels and different update semantics.

**Research inputs for this design:**

| Project | What’s relevant to us | Status |
|---|---|---|
| MemOS (MemTensor) | MemCube abstraction — portable, versionable, composable memory units with metadata and provenance. MCP integration. 159% improvement over OpenAI memory on temporal reasoning. | Evaluate for State Store implementation. The MemCube model (self-contained memory units with provenance) aligns well with our needs. |
| Mem0 | Production-grade memory layer with graph-based retrieval. Apache 2.0. Simpler than MemOS but battle-tested. | Evaluate as practical alternative or complement. |
| MemoryOS (BAI-LAB) | Hierarchical memory management (short-term, mid-term, long-term with distinct retrieval strategies). EMNLP 2025 Oral. | Study for hierarchical design patterns. |
| A-MEM (AIOS team) | Agentic memory research — how agents interact with structured memory. | Study for agent-memory interaction patterns. |

**Design principle:** The "agent thinks, kernel remembers" inversion means the kernel owns memory entirely. Agents don’t manage, query, or maintain their own memory — they receive pre-assembled context from the kernel and return observations the kernel persists. Whatever memory architecture we adopt must support this inversion, not fight it.

**Timeline:** Memory architecture design begins after Phase 1B kernel foundation is complete. The current State Store abstraction is deliberately simple — it needs to be replaced with a real memory system, not incrementally extended. This is a design-first problem: get the architecture right before building.

### Security Model: Built for Our Threat Surface

KERNOS security is built around behavioral contracts as the primary safety mechanism, not access restriction (see "The Trust Model: Access vs. Contract" section). The system’s actual threat surface differs significantly from local-execution agent systems:

**Our threat surface:**

- **Prompt injection** — malicious content in user messages, emails, or retrieved documents that attempts to override agent instructions
- **Credential exposure** — API keys, OAuth tokens, and user secrets must be isolated per instance and never leaked through agent responses
- **Unauthorized MCP tool calls** — agents calling tools they shouldn’t, or calling tools with parameters that violate behavioral contracts
- **Channel spoofing** — SMS/caller ID is trivially spoofable (see Sender Authentication section); unauthenticated channels get restricted capabilities
- **Cross-tenant data leakage** — one tenant’s data appearing in another tenant’s context (the most critical multi-tenancy concern)

**What we don’t need to solve (yet):**

- WASM sandboxing for local code execution (we call cloud APIs, not running untrusted binaries)
- Merkle audit trails (our append-only event stream provides audit capability natively)
- Ed25519 manifest signing (no agent package distribution in Phase 1-2)

**Prior art acknowledged:** OpenFang (RightNow-AI) provided the conceptual insight that safety lives in behavioral specification, not in access restriction — a lesson learned from the OpenClaw incident. Their specific technical patterns (WASM sandboxing, taint tracking, manifest signing) are designed for a Rust-based local execution environment and are not directly applicable to KERNOS. However, their graduated approval gates and kill/pause/resume mechanisms informed our behavioral contract design. If KERNOS later supports local code execution or untrusted plugin installation, OpenFang’s sandboxing patterns become directly relevant.

### Non-Destructive Deletion: Shadow Archive Architecture

**Principle: KERNOS never destroys user data. Every “delete” is a relocation.**

This is a system-wide architectural commitment, not a per-feature decision. No agent, no operation, no user command results in permanent data destruction under normal operation. “Delete” always means “move to shadow archive.”

**Why this matters beyond safety:**

The OpenClaw incident is conventionally understood as a permissions failure — the agent shouldn’t have been allowed to delete emails. That framing is incomplete. The deeper question is: why did the system have a permanently-destructive delete operation at all? If the worst possible agent malfunction can only *relocate* data to an archive, the damage ceiling drops from catastrophic to inconvenient. Storage is cheap. User data is irreplaceable. The economics are obvious.

**How it works across data types:**

|Data Type                        |“Delete” means                           |Archive location                        |Metadata preserved                                               |
|---------------------------------|-----------------------------------------|----------------------------------------|-----------------------------------------------------------------|
|Emails                           |Move to archive folder                   |`{tenant}/archive/email/{timestamp}/`   |Original folder, who requested, conversation context, timestamp  |
|Files (brainstorming, docs, etc.)|Move to shadow archive                   |`{tenant}/archive/files/{timestamp}/`   |Original path, version history, conversation that led to deletion|
|Calendar events                  |Mark archived, remove from active view   |`{tenant}/archive/calendar/{timestamp}/`|Original event data, who cancelled, reason                       |
|Contacts                         |Mark archived, remove from active queries|`{tenant}/archive/contacts/{timestamp}/`|Full contact record, relationship history                        |
|Memory (MemCubes)                |Seal and remove from active queries      |`{tenant}/archive/memory/{timestamp}/`  |Full MemCube with provenance, reason for removal                 |
|Agent configurations             |Snapshot and archive                     |`{tenant}/archive/agents/{timestamp}/`  |Full agent state at time of removal                              |

**Agent behavior:**

- When the user says “delete that file,” the agent says “Done” and moves it to the shadow archive. No explanation needed. No ceremony. The user’s intent (“I don’t want this anymore”) is respected. The system’s commitment (“we don’t destroy data”) is also respected. These aren’t in conflict.
- When the user later says “actually, can I see that file we got rid of?” the agent retrieves it from the archive. Silently. No “I saved it just in case” — just the file.
- When an agent is working on a task (organizing files, managing a project, cleaning up a workspace) and determines something should be removed, it relocates to the archive. The agent can reference archived items if the user asks about them later.

**The only path to true deletion — storage pressure:**

Permanent deletion exists only as a deliberate, high-friction operation triggered by genuine storage constraints. The process:

1. **System identifies storage pressure** (user approaching storage limits, or user explicitly requests space recovery)
1. **Agent presents archive contents** with full context — what’s there, how long it’s been archived, what it was, why it was archived
1. **User reviews and confirms** specific items for permanent deletion. The agent does not batch-delete archives without item-level confirmation for anything non-trivial.
1. **Grace period** — even after confirmation, a 30-day grace period before physical deletion where possible. The user can still recover.
1. **Permanent deletion executes** with a final audit log entry recording exactly what was destroyed and when.

For the cloud photos example: the user says “delete all the photos from last summer’s Vancouver trip.” The agent confirms scope (“847 photos from June 2025, 12.3 GB”), presents a sample for verification, offers alternatives (compress, offload to cheaper storage), and if the user still wants them gone, stages the deletion with a grace period. At no point does the agent assume “delete” means “destroy immediately.”

**Implementation notes:**

- Shadow archive is a first-class storage concept from Phase 1A — even the earliest persistence layer has an archive path
- Archive entries are queryable by agents (with appropriate context about why they were archived)
- Archive storage counts toward the user’s total but is visually separated in any storage dashboard
- The `instance_id` scoping applies to archives identically — archived data is as isolated as active data
- Memory archives (sealed MemCubes) are excluded from normal agent queries but can be unsealed by the user through the trust dashboard

### The Trust Model: Access vs. Contract

KERNOS distinguishes between two separate layers of safety:

**Access Control** determines what an agent *can physically do* — which APIs it can call, which data it can read, which systems it can reach. This is binary: the agent either has email access or it doesn’t. An email agent that can’t delete emails is a crippled email agent. If the user wants full email management, the agent needs full email access.

**Behavioral Contracts** determine how an agent *exercises* the access it has — the rules governing when and how it uses its capabilities. This is where safety actually lives.

**Behavioral contracts are specifications, not configurations.** A contract is not a settings page with sliders. It is a structured specification expressed in four categories:

- **Musts** — what the agent is required to do (“always notify me when an email arrives from my lawyer”)
- **Must-nots** — what the agent is forbidden from doing (“never send an email to a client without my approval”)
- **Preferences** — what the agent should favor when multiple valid approaches exist (“prefer to schedule meetings in the afternoon”)
- **Escalation triggers** — conditions that require human decision rather than autonomous action (“if a calendar conflict involves more than 3 people, ask me”)

The user doesn’t write these in structured form. They say things like “never send an invoice without me seeing it first” — that’s a must-not with an escalation trigger. “You can confirm appointments on your own if it’s during business hours” — that’s a must with a constraint. The system’s job is to translate natural language intent into structured specifications that agents execute against, and to surface the current specification in readable form through the trust dashboard when the user wants to review or modify it.

A newly onboarded email agent with full access might operate under this contract:

|Action          |Classification|What happens                                                      |
|----------------|--------------|------------------------------------------------------------------|
|Read email      |Silent        |Agent reads without notification                                  |
|Categorize/label|Silent        |Agent organizes without notification                              |
|Draft response  |Notify        |User sees the draft, can ignore                                   |
|Send email      |Confirm       |Agent cannot send without explicit approval                       |
|Archive         |Silent        |Low-risk organizational action (moves to archive, not destructive)|
|Delete          |Silent        |Moves to shadow archive — non-destructive by system design        |

Over time, through progressive autonomy, the specification evolves. Every user approval, rejection, or correction is a signal that refines the contract. If the agent consistently proposes spam deletions the user approves, “delete spam” graduates from Confirm to Silent — not because a confidence score crossed a threshold, but because the specification has been refined by observed intent. “Delete from known contacts” stays at Confirm because no pattern of approval exists for that action class. The user can also manually adjust any part of the specification at any time through the trust dashboard, which presents the living contract in readable form.

**The specification is the source of truth, not a black-box model.** The user can always ask “what are you allowed to do with my email?” and get a clear, auditable answer derived from the contract — not an opaque confidence percentage.

**The OpenClaw lesson, correctly understood:** The failure was not that the agent had email access — that was a legitimate and necessary capability grant. The failure was the absence of a behavioral contract specifying confirmation gates on destructive actions. Access control was fine. The contract was missing. KERNOS treats the behavioral contract as the primary safety mechanism, not access restriction. And by eliminating destructive deletion entirely, even a contract failure cannot cause permanent data loss.

### Sender Authentication

Phone number is **identification**, not **authentication**. SMS and caller ID are trivially spoofable — an attacker texts from a spoofed version of the owner’s number, and if the agent trusts caller ID alone, that’s a complete breach. More critically, SMS cannot maintain secure sessions: two sequential messages from the same number may come from different physical devices, and there’s no equivalent of a session token to prove continuity.

KERNOS treats this as a channel capability problem, not a user behavior problem.

**Channel trust levels:**

|Channel                   |Auth confidence|Why                                                                 |
|--------------------------|---------------|--------------------------------------------------------------------|
|SMS / Voice call          |Low            |Caller ID spoofable, no session, no device verification             |
|Discord / Slack / Telegram|Medium-High    |Authenticated account sessions, spoofing requires account compromise|
|KERNOS App (future)       |High           |Full session auth, biometrics, device binding                       |

**The principle: SMS is the universal entry point, not the secure channel.**

Everyone can reach their agent via SMS. But the agent adapts its security posture to the channel’s capabilities:

- **SMS:** Suitable for low-sensitivity operations (check schedule, general questions, quick commands). For sensitive requests, the agent provides redacted summaries and steers toward a secure channel: *“You have 3 new emails from your bank. For full content, message me on Discord where I can verify your identity.”*
- **Authenticated platforms (Discord, Slack, Telegram):** Suitable for medium-high sensitivity operations. The agent verifies the user’s platform account ID, which is bound to an authenticated session. Full email content, calendar details, and most actions are available here.
- **KERNOS App (Phase 3):** Full sensitivity operations. Proper session auth, biometrics, device binding. The secure channel for everything.

**Onboarding nudge:** During setup, the agent encourages connecting at least one authenticated platform alongside SMS. *“I can help you with a lot over text, but for things like reading your emails or managing sensitive info, it’s much more secure if we also connect on Discord or Telegram. Want to set that up?”* This is framed as expanding capability, not as SMS being broken.

**External contact authentication:**

- All unknown numbers/accounts are untrusted by default
- Access limited to public-facing capabilities only (scheduling, basic info requests)
- Unknown sender asking “read my emails” → *“I can help with scheduling. Who should I tell [owner] is trying to reach them?”*
- Owner can explicitly trust specific contacts for specific capabilities

**Action sensitivity tiers:**

|Sensitivity|Examples                                              |Minimum channel                                |
|-----------|------------------------------------------------------|-----------------------------------------------|
|Low        |Check schedule, general questions                     |SMS (phone number only)                        |
|Medium     |Read email subjects, view calendar details            |SMS with PIN, or authenticated platform        |
|High       |Read email content, send messages, modify calendar    |Authenticated platform (Discord/Slack/Telegram)|
|Critical   |Change permissions, export memory, purge archived data|Authenticated platform + confirmation          |

**Implementation by phase:**

- **1A:** Owner phone number = access to low-sensitivity operations. PIN for medium. Sensitive requests get “message me on [connected platform] for that.” External numbers = friendly redirect to public capabilities.
- **2:** Platform account verification for Discord/Telegram adapters. Channel-aware sensitivity routing fully operational.
- **3:** App with full session auth becomes the primary secure channel. SMS remains the universal fallback and notification layer.

### Protocol Stack

| Protocol | Purpose | Status |
|---|---|---|
| MCP (Model Context Protocol) | Capability abstraction — tool and data access | **Adopted.** In production. Core to Capability Graph. |
| OAuth 2.1 | Authorization and tool access | **Adopted.** Industry standard for capability connections. |
| A2A (Agent-to-Agent Protocol) | Inter-agent communication (enterprise/local) | **Monitor.** Google-backed, relevant when we build inter-user agent communication (Phase 4+). Not adopted yet — no current use case. |
| ANP (Agent Network Protocol) | Inter-agent communication (open internet) | **Monitor.** Emerging. Relevant for Phase 4+ marketplace and open agent ecosystem. |
| AG-UI (Agent-User Interaction Protocol) | Agent-to-frontend event streaming | **Evaluate for Phase 3.** Relevant when building the mobile app. Not adopted yet. |
| A2UI (Agent-to-UI) | Agent-generated interface widgets | **Evaluate for Phase 3.** Google-backed. Relevant for dynamic UI in the app layer. |
| W3C DID | Decentralized agent identity | **Monitor.** Speculative. Relevant if the ecosystem moves toward decentralized identity. No current use case. |

**Principle:** Adopt protocols when we have a production use case, not speculatively. MCP and OAuth earned their place through implementation. Everything else earns its place when its phase arrives.

### Primary Interface: Platform-Agnostic Messaging Layer

The system communicates through a **unified message ingress/egress layer** that is source-neutral. All inbound messages are normalized into a common internal format before reaching the kernel. The agent is informed of the source platform as metadata but the conversation context is unified — the user’s day is one continuous thread regardless of which platform any individual message arrived from.

**Architecture:**

```
[Twilio SMS]    ──┐
[Voice Call]    ──┤  (STT → text in, text out → TTS)
[Discord]       ──┤
[Telegram]      ──┼──→ [Message Gateway] ──→ [Normalized Message Format] ──→ [Kernel]
[WhatsApp]      ──┤         ↑
[App Chat]      ──┘    Platform metadata
                       (source, channel_id,
                        capabilities, auth_level, etc.)
```

**Voice adapter (Phase 2-3):** Inbound voice calls are transcribed (speech-to-text), normalized, and processed identically to text messages. Responses are synthesized (text-to-speech via ElevenLabs or similar API) and delivered over the call. The kernel never knows the interaction was voice — it’s just another adapter. The same phone number handles both SMS and voice calls.

**Normalized Message Format (internal):**

- `content`: The message text/media
- `sender`: User identity (resolved across platforms)
- `sender_auth_level`: Authentication confidence (owner_verified, owner_unverified, trusted_contact, unknown)
- `platform`: Where this message came from (sms, discord, telegram, voice, app, etc.)
- `platform_capabilities`: What this channel supports (rich text, images, buttons, voice, etc.)
- `conversation_id`: Unified — same conversation regardless of platform
- `timestamp`: When received
- `context`: Reference to the user’s ongoing day/state

**Key Principles:**

- A user can start a conversation via SMS, continue it in Discord, and finish it in the app. Context carries seamlessly because context belongs to the *user*, not the *channel*.
- The agent adapts its response formatting to platform capabilities (plain text for SMS, rich embeds for Discord, full UI components for the app) but the *substance* is identical.
- External contacts (contractor, client) reach the agent through whichever channel is configured for that relationship.
- New platforms are added by writing a thin adapter that translates to/from the normalized format. The kernel never knows or cares about platform specifics.
- **Outbound channel selection:** The agent doesn’t just *receive* on multiple channels — it chooses the right channel for outbound messages based on content type. SMS for time-sensitive notifications and quick pings (“Your 2:00 appointment is in 15 minutes, head out now”). Authenticated platforms for substantive content (email summaries, detailed briefings, decisions that need context). The app for dashboards and control. Each channel does what it’s best at — nothing’s forced into a role it can’t handle.

### Cloud Architecture

- Kernel runs persistently in the cloud (agents work 24/7).
- Memory (MemCubes) encrypted at rest with user-held keys.
- User can export/migrate their memory to another provider.
- API keys for LLM providers managed per-user in encrypted vault.

### Multi-Tenancy: Design Multi-Tenant, Deploy Single-Tenant First

**This is a foundational decision made at project start, not a future feature.**

Every user gets an isolated **personal kernel instance** — their own scheduler, memory space (MemCubes), agent processes, permission configuration, and encryption keys. The architecture does not distinguish between “one user on a laptop” and “one user among ten thousand on a cloud server.” The isolation boundaries are identical.

**What is isolated per instance (always):**

- All MemCubes and persistent context (encrypted with user-held keys)
- All agent instances and their state
- All permission and autonomy configurations
- All audit logs and action history
- All conversation context across platforms
- All API keys and credentials (in per-instance encrypted vault)
- All shadow archive data (archived items are as isolated as active items)

**What is shared infrastructure in cloud deployment (never user data):**

- Messaging gateway (routes messages to correct tenant’s kernel)
- Compute resources (CPU/GPU/memory allocated across tenants)
- Platform adapters (one Twilio number, one Discord bot, routing to appropriate tenants)
- Base model access (shared LLM endpoints, but conversations are per-instance)

**Implementation rule:** Every piece of state is keyed to a `instance_id` from day one. Every MemCube, every agent process, every message, every audit entry, every archive entry. No code ever assumes a single user. This costs almost nothing to do now and is nearly impossible to retrofit later.

**Deployment modes:**

```
LOCAL MODE (OpenClaw-style):
[Your Machine] → [Single Tenant Kernel] → [Your MemCubes]
                                         → [Your Agents]
                                         → [Your Messaging Adapters]

CLOUD MODE (SaaS):
[Cloud Infrastructure]
  ├── [Messaging Gateway] ──→ routes by tenant
  ├── [Tenant A Kernel] → [A's MemCubes] → [A's Agents]
  ├── [Tenant B Kernel] → [B's MemCubes] → [B's Agents]
  └── [Tenant C Kernel] → [C's MemCubes] → [C's Agents]

HYBRID MODE (self-hosted cloud):
[Your Server] → [Your Tenant Kernel] → [Your MemCubes]
  (same architecture as cloud, you just own the machine)
```

**Why this matters beyond technical cleanliness:**

- Users who want OpenClaw-style local control can have it — same binary, same architecture, just runs on their hardware.
- Users who want zero-ops cloud convenience can have it — sign up, start texting.
- Organizations can self-host for compliance/privacy requirements — same system, their infrastructure.
- The transition from “indie project” to “funded startup” doesn’t require an architecture rewrite.

### Workspace Lifecycle & Onboarding

The tenant workspace is the fundamental unit of account management, whether the account belongs to an individual consumer, a small business owner, or an employee provisioned by an enterprise IT administrator. The lifecycle is identical in all cases.

**Workspace states:**

|State         |Behavior                                                          |Trigger examples                                             |
|--------------|------------------------------------------------------------------|-------------------------------------------------------------|
|`provisioning`|Workspace being created, agent initializing                       |Sign-up completed, IT admin creates employee account         |
|`active`      |Full operation — agents process, messages flow                    |Onboarding complete                                          |
|`suspended`   |Agents stop, inbound messages get status reply, all data preserved|Failed payment, employee leave, voluntary pause, admin action|
|`cancelled`   |Grace period (30 days), user can export data, then deletion       |Subscription cancelled, employee terminated, admin action    |

**Key principle:** Suspension preserves everything. The workspace can be reactivated instantly — all memory, all agent state, all configuration. This matters equally for a consumer who pauses their subscription and an employee who returns from leave.

**Onboarding flow (consumer):**

1. Sign up: name, email, phone number, one question about what they need help with
1. Phone verification (SMS code)
1. System provisions workspace (`instance_id` created, status → `provisioning`)
1. User receives first text from their agent (status → `active`)
1. Agent starts learning from conversation — no further configuration required

The first text is the product moment. It should feel like meeting someone, not configuring software. The answer to “what do you need help with” seeds the agent’s initial context so it knows whether to suggest calendar access or email management first.

**Onboarding flow (enterprise):**

1. Organization admin provisions workspace for employee
1. Employee receives SMS from their agent with activation link/code
1. Employee verifies, workspace activates
1. Organization admin retains ability to suspend/cancel any workspace under their org

**Phone number strategy:**

- Shared number (default): One Twilio number routes to tenant by sender phone number. Cheapest, simplest.
- Dedicated number (premium/business): Tenant gets their own number. Required for use cases where the user’s customers text the agent (the plumber gives out “their” agent’s number). ~$1/month per number.

**Workspace administration:**

- `set_workspace_status(instance_id, status)` — the core control command
- Triggerable by: billing system, admin dashboard, API call, org administrator
- On suspension: all agents pause, inbound messages get friendly status response, no data lost
- On cancellation: grace period with data export option, then full deletion (the one case where permanent deletion applies — account termination after explicit user action and grace period)
- On reactivation: workspace resumes exactly where it left off

**Implementation note:** The `status` field belongs on the tenant record from Phase 1A. The gateway checks status before routing any inbound message. Suspended/cancelled tenants get a response without ever hitting the kernel. This is a few lines of code, not a system.

-----

## PART 4: IMPLEMENTATION PHASES

### Phase 1A: First Spark

**Goal:** A working pipeline — message comes in, LLM processes it, response goes out — with one real capability. Something you use yourself daily. No scheduler, no memory system, no SDK. Just proof that the architecture works and the product idea is real.

#### Deliverables:

- [x] **1A.1** Evaluate AIOS codebase — COMPLETE. Decision: reference-only, later determined architecturally unrelated. Kernel designed from first principles.
- [ ] **1A.2** Twilio SMS gateway with normalized message format
  - Normalized message dataclass (content, sender, platform, capabilities, conversation_id, timestamp)
  - Twilio adapter (translates inbound SMS → normalized format, normalized response → SMS)
- Handler receives normalized message, calls Claude API, sends response
  - Handler has no knowledge of Twilio; adapter has no knowledge of handler
  - Platform capability constraints: SMS adapter enforces 160-character awareness — responses that exceed SMS limits are either truncated with a continuation prompt ("Reply MORE for the rest") or the agent is instructed at the system prompt level to produce SMS-appropriate output. The adapter owns this constraint; the handler produces full responses and the adapter handles formatting.
  - Graceful error handling: every failure mode produces a friendly SMS response, never a silent crash or raw exception. Minimum cases: LLM API timeout or error → "Something went wrong on my end — try again in a moment."; MCP tool failure (calendar unavailable) → "I couldn't reach your calendar right now. Try again or check your connection settings."; malformed or unparseable input → neutral acknowledgment that prompts a retry. All errors logged internally with full context. The user always gets a response.
- [ ] **1A.3** One real capability: calendar access
  - Connect Google Calendar via MCP
  - “What’s on my schedule today?” works via SMS
  - System responds with actual calendar data
- [ ] **1A.4** Basic persistence (even just a JSON file per user — `instance_id` and `status` field keyed from day one, shadow archive path exists from day one)

#### Phase 1A Completion Criteria:

You text the number, ask about your schedule, and get a real answer. You use it yourself at least once a day. The architecture already separates platform adapter from handler from capability.

-----

### Phase 1B: The Kernel

**Goal:** Working kernel with proper scheduling, memory, security, and agent lifecycle. Developer CLI. The foundation everything else builds on.

#### Deliverables:

- [ ] **1B.1** Fork or rebuild kernel core (based on 1A.1 evaluation)
  - Scheduler (FIFO + priority)
  - Context manager (snapshot/restore)
  - Memory manager (wire to MemOS)
  - Storage manager (persistent state via MemOS MemCubes)
  - Tool manager (MCP integration)
  - Access manager (permission framework)
- [ ] **1B.2** Integrate MemOS as the memory/storage backend
  - MemCube as fundamental persistence unit
  - User profile memory (preferences, patterns, relationships)
  - Conversation memory (session history with provenance)
  - Task memory (what’s been done, what’s pending)
- [ ] **1B.3** Implement core security framework
  - Sandboxed tool execution (adapt OpenFang patterns)
  - Action classification: silent / notify / confirm / block
  - Audit trail for all agent actions
  - Per-agent permission scopes
  - Kill/pause/resume for any agent
- [ ] **1B.4** Build the agent SDK (extend Cerebrum)
  - System call interface for agents
  - Memory read/write API
  - Tool invocation API
  - Inter-agent messaging (internal bus)
- [ ] **1B.5** Basic CLI interface for testing
  - Start/stop kernel
  - Spawn/kill agents
  - Query memory
  - View audit log
- [ ] **1B.6** Tenant isolation
  - `instance_id` on all data models from day one (MemCubes, agents, messages, audit logs, archives)
  - All queries filter by tenant context
  - Tenant configuration (API keys, preferences, autonomy settings) isolated
  - Works identically in local mode (single tenant) and cloud mode (multi-instance)
  - Verify: two tenants on same instance cannot access each other’s data
- [ ] **1B.7** Test suite
  - Kernel boot and stability
  - Concurrent agent execution
  - Memory persistence across restarts
  - Permission enforcement
  - Agent isolation (one agent can’t corrupt another’s state)
  - Shadow archive: verify “delete” operations relocate, not destroy

#### Phase 1B Completion Criteria:

Can spawn 3+ agents that run concurrently, access shared memory safely, use MCP tools, respect permission boundaries, survive kernel restart with state intact, and — critically — two tenant instances on the same machine cannot see each other’s data.

-----

### Phase 2: The Core Agents & Connective Tissue

**Goal:** Three working agents that do real things — email, calendar, and research — connected via the messaging layer. The first time a real user can message the system and get value.

#### Deliverables:

- [ ] **2.1** Email Agent
  - Connect to Gmail/Outlook via MCP
  - Read, categorize, summarize, draft responses
  - Never send without explicit user approval (Phase 1 trust level)
  - Learn sender importance from user behavior over time
  - “Delete” moves to shadow archive — never permanent
- [ ] **2.2** Calendar Agent
  - Connect to Google Calendar/Outlook via MCP
  - Read schedule, detect conflicts, propose rescheduling
  - Coordinate with email agent for meeting-related emails
  - Handle scheduling requests from external texters
- [ ] **2.3** Research Agent
  - Web search and synthesis via MCP tools
  - Produce briefings on topics
  - Save findings to memory for future reference
  - Fact-check with source attribution
- [ ] **2.4** Expand Messaging Gateway (built in Phase 1A)
  - Discord adapter (second platform — proves multi-platform architecture)
  - Additional adapters (Telegram, WhatsApp, etc.) are trivial once the pattern exists
  - Unified user identity resolution across platforms
  - Seamless conversation continuity across platforms (user’s context is their day, not their channel)
  - External contact handling (third parties message the agent through configured channels)
- [ ] **2.5** Intent Interpretation Layer
  - Parse natural language texts into agent-dispatchable tasks
  - Handle ambiguity (ask clarifying questions via text)
  - Context-aware routing (mentioning “schedule” → calendar agent, etc.)
  - Multi-step task decomposition (“get me ready for Thursday’s meeting”)
- [ ] **2.6** Progressive Autonomy via Living Specifications
  - Each agent maintains a behavioral specification (musts, must-nots, preferences, escalation triggers)
  - Specifications are initialized from defaults and refined by every user interaction (approvals, rejections, corrections, explicit instructions)
  - Natural language instructions from the user are parsed into structured specification updates (“never do X” → must-not, “you can do Y on your own” → must with autonomy grant)
  - Trust dashboard presents the living specification in readable form — user can review, modify, or reset any part
  - Automatic escalation when an action falls outside the current specification (novel action types, high-stakes contexts, ambiguous intent)
  - The specification is the auditable source of truth for agent behavior — not an opaque confidence score
- [ ] **2.7** Inter-agent coordination
  - Internal message bus for agent-to-agent communication
  - Shared context via MemOS (agents can read relevant MemCubes)
  - Task delegation (email agent asks research agent for background)
  - Conflict resolution (calendar agent and email agent both want to act on same event)

#### Phase 2 Completion Criteria:

A real person can text the Twilio number and: ask about their schedule, request email summaries, ask for research on a topic, tell the system to schedule something, and have external contacts text the number for scheduling. System learns preferences over 50+ interactions. **At least 3-5 real users (friends/family) are using the SMS interface and providing feedback by mid-Phase 2.**

-----

### Phase 3: The Experience Layer

**Goal:** The GUI moment. A mobile-first app that makes the system accessible to non-technical users. The transition from developer tool to consumer product.

#### Deliverables:

- [ ] **3.1** The Briefing Surface
  - Morning briefing: curated narrative of what matters today
  - Real-time updates: what’s changed, what needs attention
  - Priority triage: decisions needed, FYIs, handled silently
  - Time-of-day sensitivity (morning briefing ≠ evening summary)
  - Not a feed. Not notifications. A curated narrative.
- [ ] **3.2** The Steering Interface
  - Live view of active agents and their current tasks
  - Intervention controls (redirect, pause, cancel, modify)
  - Collaborative workspace for complex tasks (proposals, research)
  - Natural language + structured controls hybrid
- [ ] **3.3** The Trust Dashboard
  - What every agent is allowed to do (permission map)
  - What every agent has done (action log with drill-down)
  - What’s pending approval (action queue)
  - Kill switch per agent and system-wide
  - Autonomy level controls per agent and per action type
  - Shadow archive browser — see what’s been “deleted,” restore anything
  - Always accessible, never buried in settings
- [ ] **3.4** Agent-to-Agent Social Layer (early)
  - Your agent can negotiate with other KERNOS users’ agents
  - Scheduling negotiation (find mutual availability)
  - Information exchange with permission controls
  - Trust relationships between agent-pairs
- [ ] **3.5** Mobile App (primary platform)
  - AG-UI as the underlying protocol
  - Messaging-native feel (conversation threads with the system)
  - Briefing surface as home screen
  - Trust dashboard accessible from every screen
  - Push notifications for items requiring decisions
- [ ] **3.6** Onboarding Flow
  - SMS-first: text the number, system guides setup via text
  - Progressive profile building from conversations
  - Tool connections (email, calendar) via OAuth
  - First-week guided experience with explicit trust calibration
- [ ] **3.7** Encrypted Memory
  - User-held encryption keys for MemCubes at rest
  - Export/import functionality for memory portability
  - Memory inspection tools (what does the system know about me?)
  - Selective memory deletion (archive by default, permanent purge as high-friction option in trust dashboard)

#### Phase 3 Completion Criteria:

A non-technical person can: sign up via text, connect their email and calendar, use the app daily for a week, see their trust dashboard, export their data, and describe the system to a friend in one sentence.

-----

### Phase 4: The Ecosystem

**Goal:** Open the platform. Let others build agents. Create the marketplace. This is the App Store moment.

#### Deliverables:

- [ ] **4.1** Public Agent SDK with documentation
- [ ] **4.2** Agent behavioral contract specification (the standard format for musts/must-nots/preferences/escalation triggers that third-party agents must implement)
- [ ] **4.3** Agent marketplace (discover, install, review agents)
- [ ] **4.4** ANP integration for open internet agent-to-agent communication
- [ ] **4.5** Business/team version (shared agents across an organization)
- [ ] **4.6** API for third-party app integrations
- [ ] **4.7** Vertical agent packs (real estate, healthcare, legal, creative, etc.)

-----

## PART 5: WORKING PROTOCOL

### Specification Philosophy

**Every document in this project is a specification an agent will eventually execute against.**

This Blueprint is not just a planning document for humans — it is the first specification in the KERNOS system. The structure is intentional: deliverables have acceptance criteria, phases have completion criteria, architectural decisions have constraints, and principles have clear boundaries. This is the discipline of specification engineering applied to the project itself.

**Why this matters from day one:**

Prompting is no longer one skill. The shift from synchronous chat-based AI interaction to autonomous agents running for hours or days without human check-ins means that everything you once did in real-time — catching mistakes, providing missing context, course-correcting drift — must be encoded *before* the agent starts. The quality of the specification determines the ceiling of what the agent can accomplish.

This applies at every layer of KERNOS:

- **This Blueprint** is a specification. It has self-contained problem statements (the vision), acceptance criteria (completion criteria per phase), constraint architecture (the four principles, the six pillars), decomposition (phases and deliverables), and evaluation design (how we know each phase is done). Any Claude session or Claude Code session should be able to pick up this document and execute against it without additional context.
- **Behavioral contracts** are specifications. When a user says “never send emails without asking me,” the system translates that into a structured specification (must-not + escalation trigger) that the agent executes against autonomously.
- **Agent task specs** are specifications. When Claude produces an implementation spec for a deliverable, that spec must be self-contained enough for Claude Code to execute without seeking clarification — complete context, clear acceptance criteria, explicit constraints, and defined evaluation.

**The discipline:** When writing anything in this project — a Blueprint update, an implementation spec, a behavioral contract, a test case — ask: “Could an agent execute against this without asking me a question?” If not, the specification is incomplete.

### How We Work Together

**Your Role (Owner):**

- Directional vision and corrective steering
- Approving or rejecting deliverables
- Testing from a user perspective
- Flagging when something feels wrong even if you can’t articulate why

**Claude’s Role (Design Review/Planner):**

- Breaking deliverables into Claude Code-executable tasks
- Writing detailed implementation specs for each task
- Reviewing completed work for alignment with pillars and principles
- Updating this document as work progresses

**Claude Code’s Role (Builder):**

- Executing implementation tasks from specs
- Writing tests
- Debugging
- Producing working code

### Session Protocol

**When you open a new chat to work on this project:**

1. Upload or reference this document
1. State which deliverable you want to work on (e.g., “Let’s work on 1A.2”)
1. Claude will produce a detailed implementation spec for that deliverable
1. You review and approve/modify the spec
1. Claude or Claude Code executes
1. You test and provide feedback
1. Update the status in this document

**When you want to course-correct:**

1. Upload this document
1. Describe what feels wrong or what you’ve learned
1. Claude proposes updates to the plan
1. You approve
1. This document gets updated

### Status Tracking

Mark deliverables as:

- [ ] Not started
- [~] In progress
- [x] Complete
- [!] Blocked (add note)

-----

## PART 6: TECHNICAL PREREQUISITES

### Infrastructure Needed

- [x] GitHub repository
- [ ] Cloud hosting account (AWS/GCP/Fly.io for kernel runtime)
- [ ] Twilio account (phone number, SMS API — first messaging adapter)
- [ ] Discord bot application (second messaging adapter)
- [ ] LLM API keys (Anthropic and/or OpenAI — at minimum one)
- [ ] Domain name (for eventual web/app presence)

### Development Environment

- Python 3.11+ (kernel, agents, MemOS)
- Node.js (potential frontend/app work in Phase 3)
- Docker (containerized kernel deployment)
- Git (version control, this document lives in the repo)

### Reference Projects

The KERNOS kernel is built from first principles, not forked from any existing project. The following projects are reference material — studied for concepts and lessons, not integrated as dependencies.

| Project | What we learned | Current relevance |
|---|---|---|
| MemOS (MemTensor) | MemCube abstraction, memory provenance, temporal reasoning | **High.** Primary candidate for memory architecture design. Evaluate when designing the long-term memory system. |
| Mem0 | Production memory patterns, graph-based retrieval | **High.** Practical alternative or complement to MemOS for memory architecture. |
| OpenFang (RightNow-AI) | Behavioral contracts as safety mechanism, graduated trust | **Medium.** Conceptual lessons absorbed. Specific patterns relevant if we add local code execution. |
| AIOS (Rutgers) | Validated OS-as-metaphor for agent systems | **Low.** Architecturally unrelated. Solves GPU resource scheduling, not user-facing intelligence. |
| MemoryOS (BAI-LAB) | Hierarchical memory tiers | **Medium.** Study for memory architecture design. |

-----

## PART 7: RISK REGISTER

|Risk                                              |Likelihood           |Impact|Mitigation                                                                                                                                                                     |
|--------------------------------------------------|---------------------|------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|Project dies in chat                              |HIGH                 |FATAL |This document exists. Phase 1A is achievable in a weekend. Ship something tiny first.                                                                                          |
|Scope creep kills momentum                        |HIGH                 |HIGH  |Phases are strict. No Phase 2 work until Phase 1 criteria met.                                                                                                                 |
|AIOS codebase too academic/messy to fork cleanly  |HIGH                 |HIGH  |Read before forking (1A.1). If fork is unworkable, use AIOS as reference architecture and rebuild core modules from scratch using its design. Plan for this as the likely path.|
|MemOS integration harder than expected            |MEDIUM               |MEDIUM|Mem0 is the fallback — simpler, more production-proven, less ambitious but functional.                                                                                         |
|LLM API costs during development                  |MEDIUM               |MEDIUM|Use local models (Ollama) for development. Cloud APIs for testing only.                                                                                                        |
|Single developer bottleneck                       |HIGH                 |HIGH  |Claude Code handles implementation. Owner focuses only on steering. Don’t try to understand every line of code.                                                              |
|Trust problem unsolved (another OpenClaw incident)|LOW (with our design)|FATAL |Conservative-by-default principle. Graduated autonomy. Non-destructive deletion. Never ship without the trust dashboard.                                                       |

-----

## PART 8: FIRST STEPS (Do This Week)

The single biggest risk is that this dies here. So the first steps are designed to create irreversible momentum — once you’ve done them, the project exists in the world and has gravity.

### This Week:

1. [x] Create a GitHub repository called `kernos`
1. [ ] Commit this document as `BLUEPRINT.md` in the repo root
1. [ ] Commit the brainstorm checklist as `CHECKLIST.md`
1. ~~Spend a few hours reading AIOS source~~ — **COMPLETE.** Evaluated, determined reference-only, later determined architecturally unrelated. KERNOS kernel designed from first principles. See kernel architecture outline.
1. [ ] Create a Twilio account, get a phone number
1. [ ] Build the SMS echo-bot with the normalized messaging architecture:
- Normalized message dataclass (content, sender, platform, capabilities, conversation_id, timestamp)
- Twilio SMS adapter (translates inbound SMS → normalized format, normalized response → SMS)
- Handler that calls Claude API and sends a real conversational response (not just echo)
- Handler has no knowledge of Twilio; adapter has no knowledge of handler
1. [ ] Add one real capability: connect Google Calendar via MCP, make “What’s on my schedule today?” work via SMS

Steps 6 and 7 give you something you’ll actually use yourself daily. That’s the strongest forcing function for continued development. The architecture is already right from day one — swapping in a Discord adapter later is just another translator.

-----

## APPENDIX A: Existing Protocol Landscape

(Reference from research conducted 2026-02-26)

### Communication Protocols

- **MCP** (Anthropic → AAIF/Linux Foundation): Agent-to-tool/data. Universal standard. 10K+ servers.
- **A2A** (Google → AAIF/Linux Foundation): Agent-to-agent. Enterprise focus. Agent Cards for discovery.
- **ACP** (IBM → Linux Foundation): Agent workflow orchestration. RESTful task management.
- **ANP** (Open source): Open internet agent discovery. W3C DID identity. Three-layer architecture.
- **AGP** (gRPC-based): High-performance agent messaging. Cloud and edge deployments.

### Interface Protocols

- **AG-UI** (CopilotKit): Bidirectional agent-user event streaming. ~16 event types.
- **A2UI** (Google): Agent-generated UI widgets. Cross-platform.
- **AGENTS.md** (OpenAI → AAIF): Project-specific agent behavior guidance. 60K+ repos.

### Identity & Security

- **W3C DID**: Decentralized agent identity.
- **OAuth 2.1**: Authorization standard. Used by MCP.
- **NIST CAISI**: AI agent security standards initiative.
- **IETF identity chaining**: Delegation across trust domains.

### Memory

- **MemOS** (MemTensor): Memory OS with MemCube abstraction. MIT license.
- **MemoryOS** (BAI-LAB): Hierarchical memory management. EMNLP 2025 Oral.
- **Mem0**: Production memory layer. Apache 2.0.
- **A-MEM** (AIOS team): Agentic memory research.

### Governance

- **AAIF** (Linux Foundation): Agentic AI Foundation. Founded Dec 2025.
  - Members: Anthropic, OpenAI, Block, AWS, Google, Microsoft, Bloomberg.
  - Projects: MCP, Goose, AGENTS.md.
- **Eclipse LMOS**: Enterprise agentic platform. In production at Deutsche Telekom.

-----

## APPENDIX B: Pillar-to-Implementation Mapping

| Pillar | Primary Implementation | Protocols | Phase |
|---|---|---|---|
| 1. Capability Abstraction | Capability Graph + MCP | MCP | 1A-1B |
| 2. Lifecycle & Resources | Task Engine + Reasoning Service | — | 1B |
| 3. Persistent Context | Event Stream + State Store + Memory Architecture (TBD) | MCP (for tool-based memory access) | 1B-2 |
| 4. Identity, Trust & Boundaries | Behavioral Contracts + Channel Trust | OAuth 2.1 | 1B-3 |
| 5. Inter-Agent Communication | Shared State Store (coordination) + structured deliberation | A2A (evaluate Phase 4) | 2-4 |
| 6. User-System Interface | Message Gateway → App (Phase 3) | AG-UI (evaluate Phase 3) | 1A-3 |

-----

*Last updated: 2026-02-27*
*Status: PLANNING — Repo created, no code written yet*
*Next action: PART 8 — First Steps (commit Blueprint, evaluate AIOS, build SMS gateway)*
