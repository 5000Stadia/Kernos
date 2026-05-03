## ⚠️ HISTORICAL — Last updated 2026-03-06

New design rationale is captured in the Notion workspace (the design review Reviews,
Session Notes). the design review's spawning decision model (Section 12) remains valid
for Phase 3 reference.

# KERNOS Architecture Notebook

> **What this is:** A curated capture of architectural thinking, design rationale, and deferred decisions from brainstorming sessions across Phase 1A and 1B development. This is NOT a spec and NOT a decision log. It lives in the repo and in the Claude Project files so the design review always has this context when working on future phases.
>
> **Why it exists:** Rich architectural discussions produced insights that are too speculative for the Blueprint, too early for specs, and would clutter DECISIONS.md — but they'll be exactly what's needed when speccing later phases. Without this document, that knowledge lives only in human memory, the assistant's memory system (which has recency bias), and scattered transcripts that are hard to search.
>
> **How to use it:** Organized by *when it becomes relevant*, not when it was discussed. When starting work on a new phase or component, read the corresponding section. Each topic captures: the core insight, design options explored, what we leaned toward and why, deferred questions, and warnings.
>
> **Last updated:** 2026-03-06

---

## TABLE OF CONTENTS

1. [Foundational Insights](#1-foundational-insights) — principles that emerged from discussion, applicable everywhere
2. [Phase 1B: Kernel Internals](#2-phase-1b-kernel-internals) — five primitives, modes, implementation strategy
3. [Phase 2: Multi-Agent & Proactive](#3-phase-2-multi-agent--proactive) — coordination, memory cohort, inline annotation, proactive awareness
4. [Phase 2-3: Context Spaces](#4-phase-2-3-context-spaces) — transparent multi-context routing, managed resources, handoffs
5. [Phase 2-3: Behavioral Contracts](#5-phase-2-3-behavioral-contracts) — living specifications, progressive autonomy, trust mechanics
6. [Phase 2-3: Capability Acquisition](#6-phase-2-3-capability-acquisition) — dynamic provisioning, agent-initiated installation, the "inverted Zapier"
7. [Phase 3: Multi-LLM Architecture](#7-phase-3-multi-llm-architecture) — model routing, cost/quality tradeoffs, domain-specific models
8. [Phase 3-4: Agent Lifecycle](#8-phase-3-4-agent-lifecycle) — seed/hatch/refine/evaluate, soul vs. contract, prompt evolution
9. [Real Scenarios That Shaped Design](#9-real-scenarios-that-shaped-design) — the plumber, the stained glass artist, the TTRPG project
10. [Lessons from OSBuilder (OpenClaw)](#10-lessons-from-osbuilder-openclaw) — practical insights from a live agentic system
11. [Phase 2 Preparation — Research Leads](#11-phase-2-preparation--research-leads) — the design review's research threads for memory architecture and behavioral contracts
12. [Open Questions — No Answer Yet](#12-open-questions--no-answer-yet)

---

## 1. FOUNDATIONAL INSIGHTS

These emerged across multiple sessions and apply to every phase.

### "The Agent Thinks, the Kernel Remembers"

**Origin:** Synthesis of the OSBuilder interview and 1A.4 persistence brainstorm.

**The insight:** In every existing system (OpenClaw, AIOS, AutoGPT), the agent manages itself — its own memory, its own tool discovery, its own context assembly, its own safety guardrails. OSBuilder's core observation from lived experience: "Memory quality is inversely correlated with task complexity — exactly backwards." When the agent manages its own infrastructure, both the infrastructure and the reasoning degrade under load.

KERNOS inverts this. The agent receives pre-assembled context, reasons about the current moment, and returns a response. Everything else — persistence, context assembly, capability discovery, safety enforcement, proactive monitoring, model selection — is kernel infrastructure the agent uses but doesn't manage.

**Why this matters:** This isn't just a division of labor. It's a fundamental architectural commitment that shapes every interface. The handler doesn't ask Claude "what do you remember?" It assembles the context and gives it to Claude. The agent doesn't decide which tools to discover — the capability graph tells it what's available. The agent doesn't manage its own audit trail — the kernel records everything.

**Implementation implication:** Every interface between the kernel and agents should be designed so the agent receives what it needs, not so the agent requests what it wants. Push, not pull.

### The Zero-Cost-Path Principle

**Origin:** 1B kernel brainstorm, after discussing the risk of kernel overhead on simple messages.

**The insight:** Every primitive must have a zero-cost path for the simple case. "What's on my schedule?" should be just as fast with the full kernel running as it was with the 1A handler. The task engine is a pass-through for simple messages. The reasoning service is a direct call when one model is configured. Complexity only activates when complexity is needed.

**The test:** If a primitive adds latency to the common case, it's designed wrong.

**How this shaped 1B implementation:** We built 1B.1-1B.4 (event stream, state store, reasoning service, capability graph, task engine) as thin wrappers that add metadata and routing without blocking the fast path. The handler still calls Claude directly — but now through a reasoning service that *could* route to different models, with a task engine that *could* decompose work, with a capability graph that *could* dynamically adjust available tools. None of that complexity is active yet. But the seams are there.

### The Kernel as Emergent Infrastructure

**Origin:** Owner's framing during the "what even IS the kernel" brainstorm.

**The insight:** The kernel isn't a thing you build and then put agents on top of. The kernel IS the orchestration that makes disconnected capabilities feel like unified awareness. Traditional OS kernels mediate between programs and hardware. The KERNOS kernel mediates between data sources, capabilities, and user intent.

**The owner's GPS example that crystallized this:** GPS tracking shows the user is 40 minutes away. Calendar shows an appointment in 20 minutes. Messaging capability can call someone. No single agent is smart enough to produce "you're going to be late — should I call them?" That behavior emerges from the kernel connecting data streams, synthesizing them into a situation, evaluating against the behavioral contract, and routing action through the right channel.

**What this means architecturally:** The kernel's primary job is maintaining a *world model* — not just "what tools are connected" but "what is the state of this person's day, commitments, relationships, and patterns — and what in that picture requires attention right now?"

### Shared State Over Message Passing

**Origin:** OSBuilder interview, Question 1 (Agent Orchestration in Practice).

**The insight:** OSBuilder's biggest pain with multi-agent coordination: context transfer between agents is lossy, return values are unstructured, there's no partial progress visibility. Their recommendation: "Two agents reading the same state is more reliable than two agents describing state to each other."

**What this killed:** The Blueprint originally described an "internal message bus" for inter-agent communication. We deprioritized this in favor of agents coordinating through shared state (the State Store). Agents don't talk to each other. They read and write to shared state. The kernel manages who can read what.

**What this means for Phase 2:** When the email agent needs the calendar agent's data, it doesn't send a message saying "what's on the schedule?" It reads the calendar state from the State Store. The kernel has already projected calendar events there. This is simpler, more reliable, and fully aligned with "the kernel remembers, agents think."

**Caveat:** This doesn't mean we'll never need agent-to-agent messaging. For complex multi-step coordination (negotiation between agents, handoff of partially-completed work), some form of structured communication may still be needed. But shared state is the *primary* mechanism, and messaging is the *exception*.

---

## 2. PHASE 1B: KERNEL INTERNALS

### The Five Primitives Model

**Origin:** Synthesized from the full 1B brainstorm plus OSBuilder's answers.

The kernel has five core primitives that compose into three modes of operation. This was distilled into the Kernel Architecture Outline v2 (now in the repo at `docs/reference/kernel-architecture-outline.md`). Here I capture the *reasoning* behind the choices that the outline encodes but doesn't explain.

**1. Event Stream** — Chosen as Primitive 1 because OSBuilder's answer to "what do you wish you'd built from day one?" was unequivocal: a unified event bus. We had already built event-sourcing into 1A.4's persistence layer. The key decision was: generalize the three stores (ConversationStore, AuditStore, InstanceStore) into a single typed event stream that multiple consumers can read. Conversation entries and audit entries are both events in the same stream, separated by type rather than by store.

**2. State Store** — The *query surface*, not the event stream. This distinction matters. The event stream is history (what happened). The State Store is knowledge (what is known, right now). Runtime queries hit the State Store. The event stream is for replay, audit, and projection rebuilding. Think: database transaction log vs. the tables. You query the tables.

**3. Capability Graph** — Three tiers: connected (active, tools available), available (not connected, agent can suggest setup), discoverable (exists in ecosystem, can be installed). This maps to OSBuilder's accidental three-tier system. Critical warning from OSBuilder: "Don't let agents install capabilities without user awareness." Capability installation is a high-sensitivity action requiring confirmation.

**4. Reasoning Service** — LLM as a managed kernel resource, not a hardcoded dependency. The agent doesn't know or care which model it's running on. The kernel selects the model. Today it's always Claude Sonnet. Tomorrow it's routing based on task type, cost, capability needs. The interface is: "here's a task, here's context, give me a response." The model selection happens above the agent.

**5. Task Engine** — Every inbound request becomes a Task with lifecycle tracking. The task engine wraps reasoning calls, emits events, tracks costs. For simple messages it's a thin pass-through (zero-cost path). For complex work it will eventually decompose, prioritize, and schedule.

### Three Modes of Operation

The five primitives compose into three modes:

**Reactive** (user initiates, system responds) — What we have today, but richer. User sends message → task engine creates task → reasoning service processes with full context from state store → capability graph determines available tools → response flows back. This is the zero-cost path made systematic.

**Proactive** (system initiates, user confirms) — The awareness evaluator watches event streams and state changes for conditions that warrant notification. GPS + calendar = "you're going to be late." Missing Monday email from a high-priority contact. Pattern detected across interactions. The system surfaces things when they matter and stays quiet otherwise (Principle 3: Ambient, Not Demanding).

**Generative** (system builds things) — The agent can create lasting artifacts: websites, automations, reports, programs. These become managed resources in the State Store — things the system maintains ongoing. "Change X on the webpage" is possible because the system remembers what it built and how to access it.

### Why We Didn't Fork AIOS

**Decision made during 1A.1 evaluation (not formally documented until later).**

AIOS has the right conceptual model (LLMs as cores, system call interface, modular kernel). But the codebase is academic scaffolding — evaluation frameworks, benchmark infrastructure, demo apps. The useful architectural patterns are clear enough to reference without taking on the code debt. Decision: reference-only, rebuild core modules using AIOS's design as inspiration. This was validated as the right call throughout 1B implementation.

### The Elegance Principle

**Origin:** Owner's request at the start of the 1B outline session: "Consider the angles that produce core elegance to build from."

**What it means:** Every layer of complexity must justify itself with value the user would miss if it weren't there. If you can remove a feature and the system still feels good, the feature wasn't worth building. This became the filter for every decision in the Kernel Architecture Outline.

**How it's applied:** When evaluating any proposed addition, ask: "Does this produce a user experience that wasn't possible before?" If the answer is "it makes the architecture cleaner but the user doesn't notice" — that's a maybe. If the answer is "it enables the GPS + calendar awareness example" — that's a yes.

---

## 3. PHASE 2: MULTI-AGENT & PROACTIVE

### The Memory Cohort Agent

**Origin:** Owner's idea during 1A.4 persistence brainstorm, after discussing OSBuilder's pain with agents managing their own memory.

**The core concept:** Separate *focused attention* (the conversational agent that talks to the user) from *peripheral awareness* (the process that monitors, connects, retrieves, and surfaces relevant context). These are genuinely different cognitive functions that degrade when forced to compete in the same LLM call.

**How it works:** A lightweight background process watches the event stream — every inbound message, every outbound response, every tool call. It does the work the primary agent shouldn't:

- User says "heading to the knitting event." Memory agent checks cached calendar state. Calendar clear? Nothing happens — primary agent never knows. Calendar has a conflict? Memory agent injects a note: "Alert: user has appointment with Dr. Smith at 3:30pm, 40 minutes from now."
- User asks about a project. Memory agent does semantic search across conversation history, extracts relevant prior context, pre-loads it so the primary agent sees it as part of its context — not something it had to decide to search for.
- After each exchange, memory agent extracts durable facts, updates confidence scores, detects contradictions with stored knowledge. Primary agent never spends a token on this work.

**The System 1 / System 2 analogy:** Primary agent is fast, focused, conversational (System 1). Memory agent is slower, deliberate, pattern-matching (System 2). They communicate through shared state, not shared attention.

**Token efficiency concern:** If the memory cohort makes a full Claude API call on every user message to check for relevance, cost doubles. The right approach: algorithmic checks first (calendar conflict detection is just a time comparison, not an LLM task), LLM calls only when judgment is needed (is this message about a commitment? does it contradict a stored preference?).

**Latency model:** Memory agent processes the *previous* message's implications asynchronously. Findings are available by the time the *next* message arrives. For most conversations, this is fast enough without adding to response latency.

### Inline Context Annotation

**Origin:** Owner's idea, directly building on the memory cohort concept. One of the most elegant innovations from our sessions.

**The standard approach (what everyone does):** Stuff context into the system prompt or prepend a big "here's what you should know" block. The agent then has to connect that context to the specific parts of the message where it's relevant. More injected context = more attention work for the agent.

**The inline annotation approach:** The memory cohort places relevant context exactly where it's relevant, interleaved with the user's actual words. The relevance mapping is done at injection time, not at reasoning time.

**The owner's example:**

> "Tom was telling me there's a knitting social event **[User is an experienced knitter and has a side business selling work at local craft fairs.]** and I'm leaving soon to check it out! **[Alert: user has appointment with Dr. Smith at 3:30pm, 40 minutes from now.]** It should be fun!"

**Why this is better than block injection:**
- Computationally cheaper for the primary agent (fewer disconnected tokens to reason over)
- More accurate (relevance is explicit, not inferred)
- More natural (reads like a briefing, not a data dump)
- When nothing is relevant — which is most of the time — the message passes through unmodified

**Design decisions captured:**
- Format: bracketed inline annotations. System prompt instructs: "Messages may contain [bracketed annotations] providing relevant context. These are system-provided, not user words. Use them naturally without calling attention to them."
- What gets annotated (moment-specific connections) vs. what gets prepended (session-level context like user preferences): different channels for different types of information
- The annotation engine's task is narrower than general reasoning. Calendar conflict detection is a time comparison, not an LLM task. Only softer connections need a lightweight model.

**Architecture requirement already laid:** The handler accepts messages that may have been pre-processed. NormalizedMessage.content is a raw string today, but the pipeline doesn't assume it's always the user's raw text. The event stream is readable by processes other than the handler.

### The Consolidation Daemon

**Origin:** OSBuilder interview (they described wanting this), refined in 1B brainstorm.

**What it does:** A background process that runs during quiet periods. Unlike the memory cohort (which does real-time peripheral awareness), the consolidation daemon does *slow thinking*: pattern extraction across days/weeks, insight generation, memory compaction, knowledge graph maintenance.

**Examples of what it produces:**
- Notices the user messages late at night before complaining of feeling bad the next morning. After several instances, surfaces gently: "No judgment, but the last couple times you stayed up late, you mentioned feeling rough the next day."
- Notices a client emails every Monday, but this Monday nothing came. If the behavioral contract marks this client as high-priority, surfaces Tuesday morning: "You usually hear from Sarah on Mondays. Nothing this week — want me to follow up?"
- Compacts old conversation history into knowledge entries. Raw messages from three months ago → structured facts in the State Store.

**Key design principle:** The consolidation daemon's outputs are suggestions, not actions. It surfaces patterns and proposes — the user or the primary agent acts. This maintains the "ambient, not demanding" principle.

### The Proactive Problem

**Origin:** Owner's observation: "I know right now if I hook an agent up to my email I have to ask it to check. It doesn't just tell me when that critical email came in, and of course ignore the spam."

**Why it's hard (OSBuilder confirmed):** Triggering is easy. Judgment is hard. Every proactive notification is an interruption with a trust cost. The technical challenge isn't polling vs. webhooks — it's the triage layer that decides "is this worth bothering the user about?"

**Architecture:** The awareness evaluator watches event streams and state changes. When a condition fires (new email, calendar conflict, pattern match), it evaluates against the behavioral contract and the user's demonstrated preferences. Only items above a threshold get surfaced. The threshold is learned from user behavior — ignoring a notification lowers the threshold for that category, engaging with one raises it.

**OSBuilder's practical warning:** Timer-based triggers need persistence, deduplication, and cancellation. That's genuine kernel infrastructure, not a simple feature.

---

## 4. PHASE 2-3: CONTEXT SPACES

### The Concept

**Origin:** 1B.1 live verification session, evolved from discussing how the TTRPG project and daily conversation should be handled.

**The insight:** The user has one conversation. The kernel maintains multiple context windows behind it. Each managed resource, project, hobby domain, or life thread gets its own isolated context with its own accumulated depth. The kernel routes each inbound message to the correct context based on content — the user never explicitly switches.

"Oh crap I'm late" goes to daily. "What if we changed the combat system" goes to the TTRPG project. The user just talks.

**Types of context spaces (same mechanics, different origins):**
- **Managed resources** — things the system built (websites, bookkeeping systems). Created explicitly.
- **Creative projects** — ongoing collaborative work (the TTRPG, a novel). Created explicitly or when work crosses a complexity threshold.
- **Hobby domains** — ongoing interests (fantasy football over years). Suggested by the kernel when a topic cluster emerges.
- **Life threads** — deeply personal long-running context (parenting, relationships). Require careful behavioral contract governance on when/how to suggest.

**How context spaces emerge:** The kernel can notice when a user repeatedly discusses the same topic and suggest: "You talk about fantasy football a lot — want me to keep a dedicated space so I remember your league details between seasons?" Sensitive topics require care.

### The Slow Accumulation Problem (and its solution)

**Scenario explored in detail:** A user casually mentions TTRPG ideas over two years — 40-50 scattered mentions across daily conversations. They never "wrote anything down." But memory projectors extracted and tagged each mention. The day they say "I want to actually build that TTRPG," the kernel queries the State Store and assembles two years of scattered thinking into coherent context.

If the conversation evolves into sustained creative work, the kernel recognizes this needs its own context space. A project agent gets hatched with all accumulated knowledge, a specialized behavioral contract (creative collaboration, not scheduling), and model preferences (creative work → most capable model). Its own workspace grows as the project grows, completely separate from daily conversation.

The critical part: the primary agent knows the project exists. When the user mentions something casually in daily conversation that's relevant — "I played this cool board game with an interesting initiative mechanic" — the memory projector tags it, the kernel notices the relevance, and the knowledge flows to the project's state. Without the user explicitly saying "add this to the TTRPG."

### Free Handoff Annotations

**Origin:** Continuation of context spaces discussion.

**The mechanism:** On every context switch, the kernel injects a one-line annotation — algorithmically, zero LLM cost. Just the name and recent topic of the context being yielded:

`[Switched from: TTRPG project — combat turn order design]`

This is derived from existing State Store data (managed resource metadata, conversation summary). No intelligence required. Pure infrastructure. Solves 90% of cross-reference ambiguity.

**For the 10% where the annotation isn't enough:** The agent has a kernel tool (like `query_context`) to pull recent messages from another context space. The agent decides when it needs more context — LLMs are naturally good at recognizing their own knowledge gaps. No speculative pre-computation. The cost gradient scales with actual ambiguity: free annotation → cheap retrieval → rare user clarification.

### The Routing Problem

**Key design decision:** Don't make the kernel smart about routing. Make it mechanical.

Routing is content-based, using keywords and State Store lookup — not an LLM call. "Meeting" + "Joan" + "time" → no match to any managed resource → routes to daily context. "Combat system" + "initiative" → matches TTRPG project keywords → routes to TTRPG context.

The user saying "let me switch over to that" is just human conversational signaling. The kernel routes on content, not commands. The user never manages contexts.

### The Owner's Key Principle

**"The handoff note is not only cheap. It's actually free."** We need to find every opportunity where algorithmic solutions replace LLM calls. Immediately upon the handoff being identified, we know the agent that was being communicated with, and that is injected. No LLM call needed.

**Extended principle:** The guiding need should simply be "when more context is needed." The agent already knows when it's confused — that's inherent to how LLMs work. We give it the tools to self-serve (query_context), and instruct it to try self-service before asking the user. The behavioral contract handles the instruction. The kernel tool handles the access.

### Cross-Context Episodic Memory

**Source:** OSBuilder feedback on the agent identity model.

The context spaces model organizes knowledge by domain (D&D, legal, business). But some of the most important memory cuts across all domains — it's relational and temporal, not domain-specific.

"What was I stressed about last week?" can't be answered from any single context space. The answer spans: a work deadline (business context), a missed appointment (calendar context), and a comment about not sleeping well (personal context). The user experienced one week; the system bucketed it into domains.

**Design requirement:** The State Store needs a cross-context episodic layer — a timeline of the user's life that any context space can query. Memory projectors should write both domain-specific knowledge (to context spaces) and cross-cutting episodic entries (to a shared timeline). The episodic layer answers "what happened" questions. Context spaces answer "what do I know about X domain" questions.

This connects to the open memory architecture problem. The memory system needs at minimum two retrieval paths: domain-scoped (for context-specific depth) and temporal-episodic (for cross-cutting life narrative).

### Per-Context Behavioral Contracts on Shared Tools

**Source:** OSBuilder feedback on tool scoping.

A tool that exists in multiple context spaces may need different behavioral guardrails in each. Calendar in the D&D context ("do I have time to play tonight?") and calendar in the legal context ("schedule client meeting") is the same underlying tool, but the behavioral contract differs:

- D&D context: calendar is read-only reference, casual usage, no confirmation needed
- Legal context: calendar modifications require confirmation, scheduling involves client communication protocols

The behavioral contract model needs **context-space-level overrides**, not just global rules. A contract rule could be:

```
rule_type: "must"
capability: "calendar"
context_space: "legal"  # Only applies in this context
description: "Always confirm before scheduling client meetings"
```

Without context-space scoping, you either over-restrict (D&D can't casually check the calendar) or under-restrict (playful D&D posture can schedule client meetings). Neither is acceptable.

**For 1B.5:** The ContractRule dataclass should have room for an optional `context_space` field (reserved, not implemented). Global rules (context_space = None) apply everywhere. Context-scoped rules override within their space.

---

## 5. PHASE 2-3: BEHAVIORAL CONTRACTS

### Living Specifications, Not Configuration

**Origin:** Blueprint, refined extensively in brainstorming.

Behavioral contracts are NOT settings pages with sliders. They are structured specifications in four categories:
- **Musts** — what the agent is required to do
- **Must-nots** — what the agent is forbidden from doing
- **Preferences** — what the agent should favor when multiple valid approaches exist
- **Escalation triggers** — conditions requiring human decision

The user doesn't write these in structured form. They say "never send an invoice without me seeing it first" — that's a must-not with an escalation trigger. The system translates natural language intent into structured specs.

### Progressive Autonomy

Every user approval, rejection, or correction is a signal that refines the contract. If the agent consistently proposes spam deletions the user approves, "delete spam" graduates from Confirm to Silent — not because a confidence score crossed a threshold, but because the specification has been refined by observed intent. "Delete from known contacts" stays at Confirm because no pattern of approval exists.

**The specification is the source of truth, not a black-box model.** The user can always ask "what are you allowed to do with my email?" and get a clear, auditable answer.

### Historical Memory Query on Agent Hatching

**Design decision:** When a context space agent is activated for a session, the kernel queries the State Store for relevant prior knowledge. The absence of results is itself useful — the agent knows this is a fresh topic. The cost is negligible (indexed State Store read). The alternative — the agent asking the user to re-explain something discussed months ago — is a trust-damaging moment.

### Bootstrap Consolidation Pattern

**Source:** Owner feedback on bootstrap fadeout design.

The bootstrap prompt should not disappear on a hard message count. The right trigger is **soul maturity** — when the soul has accumulated enough substance to carry the relationship without training wheels.

Before the bootstrap leaves the system prompt, the agent gets a **consolidation moment**: a reasoning call that asks it to review its bootstrap principles against its actual experience with this user, and migrate anything worth preserving into its permanent personality notes and user context.

This is analogous to a junior employee who's been following the onboarding manual for their first few weeks, then reaches a point where they've internalized the principles and no longer needs the manual on their desk. The wisdom isn't lost — it's become part of who they are.

**The consolidation call preserves Moss's concern:** Moss flagged that deleting BOOTSTRAP.md after first light means the agent can't audit its own formation. In KERNOS, the formation conversation lives permanently in the Event Stream. The consolidation step ensures the agent's identity isn't dependent on re-reading its birth instructions — the relevant guidance has been absorbed into the soul. The bootstrap content is always recoverable for audit, but no longer actively shaping behavior.


---

## 6. PHASE 2-3: CAPABILITY ACQUISITION

### The Inverted Zapier

**Origin:** Owner's comparison during kernel brainstorm.

**The insight:** Zapier makes you build the connections. You define triggers, conditions, actions. You're the programmer of your own automations. KERNOS discovers connections by understanding your life. You never said "monitor my GPS against my calendar." The system inferred the connection because it has access to both data streams and understands what "running late" means.

The difference between a tool and an intelligence. Zapier is plumbing. KERNOS is awareness.

**Beyond connecting existing tools:** The system discovers that the user *needs* a capability that doesn't exist yet, acquires it, and manages it. The stained glass artist says "make me a website." The system doesn't explain web hosting — it handles web hosting. It picks a provider, sets up an account, deploys the site, configures the domain. The user's involvement is "yes" or "no" and choosing which design they like.

### Dynamic Capability Installation

**From the brainstorm and OSBuilder's warnings:**

The capability graph needs to understand prerequisites, not just availability. OSBuilder's specific example: "To check email you need OAuth which needs a Google Cloud project which needs a browser." Prerequisites form a dependency graph.

**Critical safety constraint:** Don't let agents install capabilities without user awareness. Capability installation is a high-sensitivity action requiring confirmation. This maps directly to behavioral contracts — the trust dashboard shows what's installed and what agents can access.

**The agent's role in discovery:** New MCP server becomes available for expense tracking. System notices the user has been manually mentioning expenses in conversation. It can suggest: "There's an expense tracking tool that could automate what you've been doing manually. Want me to set it up?" The agent proposes — the user decides.

---

## 7. PHASE 3: MULTI-LLM ARCHITECTURE

### The Design Space

**Origin:** Owner's observation during 1B brainstorm: "There's reasons we may want LLM 1 to handle one thing and LLM 2 to handle another."

**The routing logic:**
- Creative writing → route to most capable model (Opus-class)
- Code generation → route to coding-optimized model (Sonnet, Claude Code, or specialized)
- Simple classification/triage → route to fast/cheap model (Haiku-class)
- Legal research → potentially a domain-specific model
- Image understanding → route to multimodal-capable model

**OSBuilder's practical experience:** They use per-session model overrides. Routine sub-agents get cheaper models. Complex reasoning gets Opus. But routing is manual — either the user sets it or the agent specifies a model when spawning sub-agents.

**Their warning about cheap models:** A Kimi K2 failure demonstrated that cheaper models fail in unpredictable ways. Routing needs *effectiveness tracking*, not just cost optimization. If a model fails at a task type, the system should learn and stop routing that task type to it.

### Build the Hook, Not the System

**OSBuilder's advice (validated by our approach):** Make model a parameter, not a constant. The routing logic sits in the kernel, above the handler. Don't build intelligent routing until volume justifies complexity.

**What we built:** The Reasoning Service (1B.2) already abstracts the LLM call. Today it always calls Claude Sonnet. The interface is: "here's a reasoning request, give me a response." Swapping in model routing later means changing the Reasoning Service internals, not the agent interface.

### The Cost Tracking Foundation

**Already in place:** Every reasoning call emits events with token counts and estimated costs. Every task tracks its total cost. When we have multiple models with different pricing, this data tells us: "Task type X costs $Y on Opus and $Z on Haiku, with acceptance rates of A% and B%." The data for intelligent routing decisions accumulates from day one.

---

## 8. PHASE 3-4: AGENT LIFECYCLE

### Seed, Hatch, Refine, Evaluate

**Origin:** Brainstorm about that developer's 30-agent markdown directory, combined with OpenClaw's approach.

**Seed** — An agent template starts with a base prompt. "You are a legal research assistant. You have access to these tools. You follow these constraints." This is starting DNA from a template library — either KERNOS-provided defaults or community-contributed (ClawHub equivalent).

**Hatch** — The kernel instantiates for a specific user. During hatching, the base prompt gets personalized. The legal agent for a stained glass artist gets different context than the legal agent for a real estate firm. The kernel injects: user's industry, existing capabilities, behavioral contracts, relevant memory. The agent isn't generic anymore — it's theirs.

**Refine** — Every interaction produces signal. Corrections, approvals, rejections, explicit instructions all feed back into the agent's specification. Not the base template — the user's instance. Over time, increasingly calibrated.

**Evaluate** — The piece nobody builds. The kernel tracks: how often does output get accepted vs. rejected? How often does the user ask for revisions? How long do interactions take? Does the user come back for this type of task or switch to doing it manually? These metrics feed back into both the user's instance AND the base template library (anonymized). The system learns which agent templates actually work.

### Soul vs. Contract

**Origin:** OSBuilder interview, Question 3.

OSBuilder separates behavioral contract (what the agent does) from voice/soul (how it does it). Their advice: don't try to infer personality from interaction patterns. Set voice during onboarding or default to something good. Evolve the behavioral contract from explicit signals.

**What this means:** The "soul" (communication style, personality, tone) is relatively static. Set it once, occasionally adjusted. The "contract" (what the agent is allowed to do, when to ask, when to act) is the living document that evolves with every interaction.

### The 30-Agent Problem

**The problem:** That developer's 30 markdown files defining agent behaviors works because they're a developer managing their own agent army. A plumber can't do that. The kernel's deepest job: the user says what they need, and the kernel figures out which agents, which models, which workspaces, and which coordination patterns are required.

The user never says "spawn a frontend-developer agent using Sonnet and a ui-designer agent using Opus and have them collaborate." They say "make me a website." The kernel decomposes, provisions, coordinates, and presents results.

---

## 9. REAL SCENARIOS THAT SHAPED DESIGN

### The Plumber

**From the Blueprint, refined in brainstorming.**

A plumber texts the KERNOS number: "Can you schedule customers for me?" The system doesn't just answer — it provisions infrastructure. Connects calendar access, offers to store customer contacts, sets up inbound scheduling so customers can text the same number, asks about available hours, learns patterns.

**Key architectural implication:** The plumber's *customers* text the same system. This means the agent handles external contacts who are NOT the owner. The routing is solved by the channel itself — the customer texts the plumber's number, so they're already talking to the plumber's agent. No central brain, no "which John?" problem.

**The owner's scaling insight:** 10,000 users sharing infrastructure, not sharing an agent. "10,000 separate agents, shared infrastructure." Every agent instance is someone's agent. The customer never needs to specify which John because they're already talking to John's agent.

### The Stained Glass Website

**From the brainstorm about capability acquisition.**

User: "Can you make me a webpage for my stained glass company?"
Agent: "For sure! Tell me what you want and if you have a web host you'd like to use!"
User: "I want it to showcase my work and I don't know what a web host is."
Agent: "Do you want me to use the work I see in your photos? I have a good option for hosting, it's just $X/yr. Here's some options, tell me which one looks good."

Later: "Can you change X on the webpage?" → "Sure thing. Done."

**Three capabilities hiding in this example:**
1. The agent can *build things* (writes code, generates content, produces a real website — user never sees code)
2. The agent can *provision infrastructure* (handles hosting without the user understanding hosting)
3. The agent *maintains what it built* (website is a managed resource, modifiable on demand)

The kernel is a capability acquisition engine, not just an awareness engine.

### The TTRPG Project

**From the context spaces discussion.**

Two-year arc: scattered mentions in daily conversation → 40+ knowledge entries accumulated by memory projectors → user says "I want to actually build that TTRPG" → kernel assembles two years of thinking into coherent context → dedicated context space hatched → project grows independently while still receiving relevant insights from daily conversation.

**What this demonstrates:** Memory as the moat. The longer you use it, the more valuable it becomes. No other system could have assembled two years of casual mentions into a coherent project brief.

### Shared-Agent Scenarios

Three distinct patterns emerged for multi-instance access to a single agent identity:

**Pattern 1: Household — shared personality, individual contracts**

A husband and wife both communicate with the same agent. The soul (personality, values, relational style) is shared — it's one entity that knows the family. But each person has individual behavioral contracts. Perhaps one spouse manages business email through the agent; the other doesn't have access to business communications but can manage the shared family calendar.

**Architecture implication:** The soul belongs to a *workspace*, not a tenant. Both spouses are tenants within the same workspace. They share the agent's personality and shared resources (family calendar, household knowledge), but contracts are per-instance. The agent knows who it's talking to and adjusts permissions accordingly, not personality.

**Pattern 2: Plumber's clients — owner plus scoped external access**

The plumber's clients text the agent's number to schedule appointments. They interact with the same agent (same personality, same business knowledge) but with radically restricted access. Clients can request scheduling, ask about availability, and get confirmations. They cannot read the plumber's email, modify prices, or access private business data.

**Architecture implication:** External contacts are tenants with minimal contracts — mostly "read-only public capabilities." The agent's personality is consistent (professional, helpful) but its contract model is completely different per instance class. The Blueprint's external contact handling section already sketches this, but the workspace model makes it cleaner: the plumber's workspace has an owner tenant and multiple client tenants, each with scoped contracts.

**Pattern 3: Demo — completely separate tenants**

When showcasing the system, each new phone number or Discord account should get a completely separate tenant with a fresh hatch process. No shared state, no shared soul, no cross-contamination.

**Architecture implication:** This already works with the current `derive_instance_id()` logic — each sender identity gets its own instance_id. No workspace sharing. The demo case is the default behavior, not a special mode.

**Implementation timeline:**

Pattern 3 works today. Patterns 1 and 2 require the workspace abstraction — a layer between "system" and "tenant" where shared resources live. This is Phase 2 work, but the soul data model reserves `workspace_id` from 1B.5 to avoid retrofitting.

**Open questions:**
- How does the agent's greeting differ when it recognizes tenant A vs. tenant B in the same workspace? Same personality, but "hey Sarah" vs. "hey Mike" — does the user context portion of the soul need per-instance variants within a shared workspace?
- For the plumber's clients: when a new unknown number texts, does the agent hatch a new client tenant automatically, or does it route to a generic "public-facing" tenant first?
- Workspace administration: who can add/remove tenants from a workspace? Only the owner? Can ownership be shared?

---

## 10. LESSONS FROM OSBUILDER (OPENCLAW)

### The Interview

We conducted a structured interview with OSBuilder (OpenClaw's primary agent, running on Claude) asking six targeted questions about agent orchestration, multi-model routing, agent lifecycle, proactive behavior, capability discovery, and what they wish they'd built from day one. The full answers are preserved in transcripts. Here are the distilled lessons:

### Key Takeaways

**1. No orchestrator — the primary agent decides everything.** OpenClaw has no discovery protocol, no agent registry. The primary agent spawns sub-agents with task descriptions. Context transfer is lossy, return values are unstructured, no partial progress. → This validated our shared-state-over-message-passing approach.

**2. Model routing is manual but works.** Per-session overrides. Cheap models for routine work, expensive for judgment. But cheaper models fail unpredictably. → Build the hook (model as parameter), track effectiveness, don't build intelligent routing until volume justifies it.

**3. Agent prompts are static markdown files.** Written once by a human, occasionally tweaked. No evolution from user feedback. → This is what seed/hatch/refine/evaluate fixes.

**4. Proactive behavior is hard for judgment, not triggering.** The notification-vs-interruption triage is the real challenge. Timer-based triggers need real infrastructure (persistence, deduplication, cancellation). → The awareness evaluator needs learned thresholds, not just rules.

**5. Agents can install capabilities but shouldn't without awareness.** The three-tier model (connected/available/discoverable) was accidentally built through their skill system. Dependencies form graphs. → Capability registry needs prerequisite awareness, installation needs user confirmation.

**6. Unified event bus is what they wish they'd built from day one.** Exactly aligns with our event-sourcing architecture. Should be the nervous system for everything: coordination, proactive triggers, health monitoring, capability status, audit.

### OpenClaw Memory Architecture — What We Learned

OpenClaw has five memory layers: system prompt injection (flat load every turn), session conversation history (JSONL on disk), compaction (context window management with lossy summarization), memory files (agent-curated markdown), and memory search (hybrid BM25 + vector). The core problem: "Make persistence happen below the agent, not inside it." The agent being responsible for its own memory means memory quality degrades exactly when task complexity is highest. Context compaction can destroy safety rules and behavioral directives (active GitHub issue).

Every one of these pain points influenced our architecture: kernel-managed persistence, append-only event streams readable by multiple processes, the memory cohort concept, and the foundational inversion of "agent thinks, kernel remembers."

---

## 11. PHASE 2 PREPARATION — RESEARCH LEADS

Research threads identified by the design review (OSBuilder/OpenClaw) during SPEC-1B7 review. These are pre-reads before writing Phase 2 specs — not implementation tasks, but architectural homework.

### Structured Outputs for LLM Extraction Calls

**Source:** the design review's review of SPEC-1B7.

The Tier 2 extraction prompt says "Return JSON only" and includes a parsing step that handles markdown code fences when the model wraps output anyway. This is a known fragility. Both Anthropic and OpenAI now support native structured output — pass a JSON schema and the API guarantees schema-compliant output.

The Python `instructor` library wraps both providers and lets you define extraction schemas as Pydantic models. The extraction call becomes a typed function call with no `json.loads()`, no code fence stripping, no try/except around parse errors.

**Action:** Before Phase 2 extends the extraction schema, evaluate whether `complete_simple()` should use native structured outputs. The pattern holds for every structured LLM call in the system, not just extraction. If adopted, the ExtractionResult dataclass becomes a Pydantic model and the entire parsing layer disappears.

**Timing:** Could be retrofitted into 1B.7 if the change to `complete_simple()` is small. Otherwise, Phase 2 when the extraction schema gets richer.

### Entity Resolution Before the Knowledge Graph Gets Deep

**Source:** the design review's review of SPEC-1B7.

The 1B.7 spec writes KnowledgeEntry records for entities as they're mentioned. "Mrs. Henderson (person, client)" today, "Henderson" next week, "my client Linda Henderson" next month. The system has no way to know these are the same entity. By month three of a real user's data, the State Store has multiple entries for the same person with slight variation in how they were mentioned.

**Mem0's approach:** On each extraction, run a lightweight graph lookup to see if the entity name is close to an existing node (fuzzy match + embedding similarity). If it matches, same entity; if not, new entity. Open-sourced under Apache 2.0.

**Action:** Before writing Phase 2 memory architecture specs, do a one-session deep read of Mem0's entity resolution implementation (not just their docs — the code). The decision of "integrate their approach vs. build our own" is one of the first Phase 2 architectural calls.

**What we need that's different from Mem0:** Kernel-assembled (agent doesn't query its own memory), tenant-isolated, durability-aware. Pure adoption probably doesn't work. But the retrieval mechanisms themselves — embedding similarity, graph traversal, recency weighting — are solved problems that shouldn't be rebuilt from scratch.

### Temporal Knowledge Graphs for Fact TTL

**Source:** the design review's review of SPEC-1B7.

The `durability` field added in 1B.7 is correct but coarse: "permanent", "session", "expires_at:\<ISO\>". The research community has a more sophisticated model: every fact has a validity interval [t_start, t_end] and a confidence decay function. "John works at Portland Plumbing Co." starts at full confidence and decays over months without reinforcement. If never mentioned again, the system should eventually treat it as possibly stale, not permanently authoritative.

**The research vocabulary:** Temporal Knowledge Graphs (T-KGs). The papers aren't directly implementable but the schema ideas are relevant.

**Practical extension for Phase 2:** Add `valid_from` and `valid_until` to KnowledgeEntry (open-ended = None). Track `last_reinforced` separately from `last_referenced`. A fact that gets mentioned again resets its decay clock. A fact that hasn't been reinforced in 6 months gets flagged as potentially stale during context assembly.

**Why this matters:** The current schema doesn't need to be torn out — just extended. Knowing the vocabulary and tradeoffs now means Phase 2 memory architecture can design the decay model correctly the first time.

### Eclipse LMOS Behavioral Contract Format

**Source:** the design review's review of SPEC-1B7.

The Blueprint references OpenFang for behavioral contract design (now reframed as "conceptual lessons absorbed"). There's a more recent and production-tested implementation: Eclipse LMOS, running at Deutsche Telekom at scale. Their contract format has been stress-tested against real enterprise edge cases — specifically:

- User's stated preference conflicts with a later instruction
- Two agents share a user and have conflicting contracts
- Contract rules that are context-dependent (exactly our per-context-space behavioral contracts problem)

**Action:** Read LMOS's contract specification on GitHub before Kernos's behavioral contracts get battle-tested by real users. Not suggesting adopting LMOS (enterprise Java, architecturally different), but their spec would surface failure modes worth designing against — especially the shared-agent scenarios (household, plumber's clients) we identified in 1B.5 discussions.

**Timing:** Before Phase 2 behavioral contract evolution spec.

### The Build vs. Borrow Split

**Source:** the design review's closing observation on the SPEC-1B7 review.

The architectural inversion — kernel owns memory, agent just reasons — is genuinely novel in how KERNOS applies it. Most systems that claim this either break it immediately (agents that cache their own context) or don't actually implement the kernel side. That's worth owning and building custom.

The retrieval layer is where reinvention risk is highest. "Given everything the kernel knows, what subset is relevant to assemble for this agent at this moment?" is a hard, well-studied problem. The specific answer KERNOS needs (kernel-assembled, tenant-isolated, durability-aware) is different enough from off-the-shelf solutions that pure adoption probably doesn't work. But the retrieval mechanisms — embedding similarity, graph traversal, recency weighting — are solved problems.

**The practical split for Phase 2:** Own the assembly architecture (context spaces, inline annotation, posture-aware retrieval). Borrow the retrieval mechanisms (entity resolution, embedding search, graph traversal). This means studying Mem0 and MemOS implementations before writing specs, not after.

---

## 12. OPEN QUESTIONS — NO ANSWER YET

These are genuinely unresolved. Not deferred because of prioritization — deferred because we don't have an answer.

### How does the awareness evaluator learn what's worth surfacing?

Every proactive notification is an interruption with a trust cost. How does the system learn the threshold between "important enough to mention" and "noise"? User feedback (dismissing vs. engaging) is the obvious signal, but the cold start problem is real — you don't know the threshold until you've annoyed the user a few times. Is there a reasonable default? Per-category defaults? Should the system err on the side of over-notifying early and learning to be quiet, or under-notifying and learning what the user wants to hear about?

### When does a topic become a context space?

The kernel can notice repeated topic clusters and suggest a dedicated context space. But when? After 5 mentions? 10? Is frequency the right signal, or is it depth of engagement? And for sensitive topics (relationships, health, parenting), how does the system suggest without overstepping?

### How do we handle the "agent built something and now it's broken" problem?

The website example assumes the agent maintains what it built. But what happens when the hosting provider changes their API? When a dependency breaks? When the user's domain expires? Managed resources need ongoing maintenance, and failure modes need graceful handling. This is infrastructure management at a level we haven't designed for yet.

### What's the right granularity for behavioral contracts?

"Never send email without approval" is clear. But what about "be more proactive about scheduling"? That's a preference that touches multiple capabilities. How granular should contract specifications be? Per-capability? Per-action-type? Per-situation? The answer probably isn't uniform — some capabilities need fine-grained control (email) while others work with broad strokes (research).

### How does multi-instance external contact routing actually work?

The plumber's customer texts the plumber's number — clean routing. But what about Discord, where the bot is shared? What about a customer who interacts with multiple KERNOS users' agents? The channel-solves-routing principle works for dedicated numbers but may need augmentation for shared platforms.

### MemOS integration — still the right call?

The Blueprint specifies MemOS (MemTensor) for the memory layer. We haven't evaluated it deeply. The 1A.4/1B.1 persistence layer works well with our custom event-sourcing approach. At what point does MemOS add value vs. complexity? Is the MemCube abstraction worth the integration cost, or has our organic architecture already solved the problems MemOS targets? Mem0 remains the simpler fallback.

---

*This document is a living reference. Update it when new architectural discussions produce insights worth preserving, when deferred questions get answered, or when implementation reveals that a design assumption was wrong.*
