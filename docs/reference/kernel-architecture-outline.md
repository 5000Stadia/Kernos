# The KERNOS Kernel

## An Architecture Outline — v2

> This document synthesizes the Phase 1B brainstorm between owner, design review, and OSBuilder (OpenClaw's primary agent). It defines what the kernel IS, what its primitives are, how they compose, and what they produce for the end user. It is the anchor document from which 1B specifications will be shaped.
>
> **The elegance principle:** Every layer of complexity must justify itself with value the user would miss if it weren't there. If you can remove a feature and the system still feels good, the feature wasn't worth building. This is the filter for every decision in this document and every spec that follows from it.

---

## Part 1: The Thesis

### What the kernel is NOT

The kernel is not a Linux module (OpenClaw's approach — deeper hardware access, root privileges). Users are on phones, not Linux machines. The kernel is not a chatbot with tools (what we have today — message in, Claude call, response out). The kernel is not a heavyweight scheduler that adds overhead to simple conversations.

### What the kernel IS

**The kernel is the persistent infrastructure that transforms disconnected capabilities into unified awareness.**

Without it: you have a chatbot with tools. You ask it things, it uses tools, it answers. Each conversation is isolated. Each capability is siloed. Nothing happens unless you initiate it.

With it: you have a system that understands your life, notices what matters, assembles capabilities around your needs, builds things for you, and earns your trust through thousands of correct small actions.

The kernel achieves this through five primitives that compose into three modes of operation. The elegance is in the primitives — get those right, and everything from "what's on my schedule?" to "build me a website" to "you're going to be late" emerges from the same architecture.

### The foundational inversion

**The agent's job is to think. The kernel's job is to remember, notice, route, and protect.**

In every existing system (OpenClaw, AIOS, AutoGPT), the agent manages itself — its own memory, its own tool discovery, its own context assembly, its own safety guardrails. OSBuilder's core insight from lived experience: "Memory quality is inversely correlated with task complexity — exactly backwards." When the agent manages its own infrastructure, both the infrastructure and the reasoning degrade under load.

KERNOS inverts this. The agent receives pre-assembled context, reasons about the current moment, and returns a response. Everything else — persistence, context assembly, capability discovery, safety enforcement, proactive monitoring, model selection — is kernel infrastructure the agent uses but doesn't manage.

### The zero-cost-path principle

**Every primitive has a zero-cost path for the simple case.** The plumber texts "what's on my schedule?" and the response is just as fast as our current system. The task engine is a pass-through for simple messages. The reasoning service is a direct call when one model is configured. Complexity only activates when complexity is needed. If a primitive adds latency to the common case, it's designed wrong.

---

## Part 2: The Five Primitives

### Primitive 1: The Event Stream

**What it is:** An append-only, multi-reader log of everything that happens in the system. Every message, tool call, capability change, agent action, trigger firing. One stream per instance.

**Why it's primitive:** This is the nervous system. OSBuilder's answer to "what do you wish you'd built from day one?" was unequivocal: a unified event bus. Without it, every subsystem builds its own logging, its own audit trail, its own state tracking — and they don't talk to each other.

**What reads from it:**
- Memory projectors (extract facts, preferences, patterns into the State Store)
- Audit trail (trust dashboard draws directly from events)
- Awareness evaluator (watches for conditions that warrant notification)
- Behavioral contract evolution (approvals/rejections refine the contract)
- Health monitoring (error patterns, cost tracking, performance degradation)
- Consolidation daemon (background processing — pattern extraction, insight generation)
- Inline annotation engine (Phase 2+ — pre-enriches messages with relevant context)

**What writes to it:**
- Message gateway (inbound and outbound messages)
- Handler (agent responses, tool invocations)
- Capability registry (connection changes, new capabilities)
- Task engine (task creation, completion, failure)
- Background processes (trigger evaluations, daemon outputs)

**Key properties:**
- Immutable — events are never modified after writing
- Ordered — strict chronological ordering per instance
- Typed — every event has a `type`, `instance_id`, `timestamp`, `source`, and `payload`
- Externally readable — not private to any single component
- **Not the query surface** — the event stream is the source of truth, not the runtime lookup mechanism. Runtime queries go to the State Store. The event stream is for replay, audit, projection rebuilding, and stream processing. Think of it as a database transaction log vs. the tables themselves. You query the tables. The log exists so you can rebuild them.

**Foundation already laid:** The 1A.4 three-store architecture (ConversationStore, AuditStore, InstanceStore) is a narrow projection of this. The event stream generalizes it — conversation entries and audit entries are both events in the same stream, separated by type rather than by store.

### Primitive 2: The State Store

**What it is:** The read-write persistent state representing the system's current understanding. Where the event stream is history (what happened), the State Store is knowledge (what is known, right now). It is also the primary coordination mechanism between agents — OSBuilder's insight: "Two agents reading the same state is more reliable than two agents describing state to each other."

**What it contains (by domain, not by implementation):**

**User knowledge** — facts, preferences, patterns, relationships, capabilities, weaknesses. Not a flat profile — structured, indexed, sourced with provenance. "User's name is Greg" (source: user stated, conversation X, date Y). "User prefers afternoon meetings" (source: inferred from 12 scheduling interactions). "User has difficulty remembering things" (source: user stated — influences notification frequency and proactive behavior). "User is an experienced stained glass artist" (source: multiple conversations — influences domain trust level).

This is the query surface for knowledge retrieval. When the user asks "who is John?" the kernel queries the State Store for all entities matching "John" associated with this tenant — fast, indexed lookup, not a search through years of raw events. Returns: "Met John at convention (date), appointment (date), phone number Z." Clean, immediate, the experience the user expects.

**User intent tracking** — not just what the user said, but why. When a user switches from one model because it wasn't good at creative work, the system records: "User switched away from model X because of poor creative output." When a new model appears that excels at creative work, the system can connect those dots: "A new model just launched that's strong at exactly what frustrated you about the old one. Want to try it?"

**Behavioral contracts** — per-capability rules. Musts, must-nots, preferences, escalation triggers. Living specifications refined by every interaction. "Never send email without approval" (source: default). "Can confirm appointments autonomously during business hours" (source: user granted, conversation X, date Y). The auditable source of truth for what agents are allowed to do.

**Capability state** — the three-tier registry (see Primitive 3).

**Managed resources** — things the system has created or provisioned on behalf of the user. Websites, bookkeeping systems, legal document trackers, scheduled reports, automated workflows. Each is an ongoing responsibility the system maintains, modifiable by the user at any time. "Website at stainedglass.com, hosted on Netlify, last updated March 1." "Accounting system — tracks expenses from receipt photos, produces quarterly report."

**Situation state** — the volatile, continuously-updated picture of what's happening now. Updated event-driven (not polling): GPS position arrives → update location. Calendar event starts → update activity estimate. Conversation active → user is engaged. Conversation idle 30 minutes after meeting ended → user probably transitioned. Minimal footprint — only updates when relevant data arrives.

**Key properties:**
- Scoped by instance_id (always)
- Indexed and queryable (this is the runtime lookup surface, not the event stream)
- Readable by any authorized agent or kernel process (access mediated by kernel)
- Versioned where it matters (user knowledge, behavioral contracts — changes tracked with provenance)
- Projected from the event stream where possible (state as a materialized view of events)

### Primitive 3: The Capability Graph

**What it is:** A dynamic registry of everything the system can do, could do, and would need to acquire — with dependency awareness.

**Three tiers:**

**Connected** — working, authenticated, ready to use. Google Calendar via MCP is connected. Events from this capability flow into the event stream. The system prompt reflects its availability.

**Available** — configured or known but not yet connected. Email via Google Workspace is available — the MCP server exists, the system knows how to set it up, but OAuth hasn't been completed. When the user mentions email, the agent says "I have an email tool available. Want me to connect it?" — not "I can't do that."

**Discoverable** — exists in the ecosystem, not yet configured. A Netlify hosting plugin exists on ClawHub. The kernel searches the plugin ecosystem when a capability gap is identified. Phase 4 marketplace territory, but the data model supports it from day one.

**Dependency awareness:** Capabilities have prerequisites. The graph encodes them so the kernel can present the full setup path upfront rather than discovering dependencies at runtime through errors (OSBuilder's explicit pain point). "To connect email: need OAuth → need browser auth flow. To host a website: need hosting provider → costs $X/year → need user approval."

**Capability installation principle:** Only surface decisions that affect the user's world — money, access to personal data, external communication. Everything else, just do it. The plumber says "build me a website." The agent says "Sure! I'll need to set up hosting — about $X/year, and you'll pick a domain name. Want to get started?" NOT "I need to install codegorilla, bluecheese, and 7 packages — is that ok?" Security concerns (malware, package integrity) are handled in a kernel security layer the user never sees.

**Key properties:**
- Dynamic — capabilities come and go (network failures, token expiration, new plugins)
- Dependency-aware — knows prerequisites for capabilities that aren't connected
- Event-emitting — status changes are events in the event stream
- Reflected in system prompt — the agent always knows exactly what it can and cannot do

### Primitive 4: The Reasoning Service

**What it is:** LLMs as managed resources. Agents request reasoning; the kernel routes to the appropriate model. The agent never knows or cares which model it's running on.

**Provider model:** The kernel connects to LLM providers through a uniform interface. Providers include:
- Direct API connections (Anthropic, OpenAI, Google, Mistral, etc.)
- Aggregators (OpenRouter, etc.) — connecting one aggregator may give access to hundreds of models
- Local models (Ollama, etc.)

The user connects whichever providers they already have. The kernel's model registry contains only the models actually available through connected providers. If the user has only an Anthropic key, the registry contains Claude models. If they connect OpenRouter, the registry expands to everything OpenRouter offers.

**The model registry:**

Updated periodically (daily is sufficient) by querying connected providers for:
- Available models (including new releases)
- Pricing (may change)
- Capability tags (vision, tool-use, deep thinking/extended reasoning, code generation, etc.)
- Context window sizes
- Any special parameters or invocation differences

When a new model appears that scores better on criteria relevant to an existing agent template's preferences, the system notes it and adjusts routing — unless the user has explicitly pinned a model, in which case it queues a suggestion for a natural conversational moment.

**User-facing quality/cost setting — five tiers:**

The user never sees model names. They see a quality/cost dial:

| Tier | Label | Routing policy |
|---|---|---|
| 1 | Economy | Cheapest models that meet minimum capability threshold |
| 2 | Balanced | Mid-tier models, good quality/cost ratio |
| 3 | Standard | Default. Strong general-purpose models |
| 4 | Performance | Best available models for each task type |
| 5 | Ultra | Maximum capability, cost secondary |

The plumber on Economy pays less, gets models that are perfectly adequate for scheduling and email triage. The law firm on Performance gets the strongest reasoning models for contract analysis. The user can change this anytime. The system also adapts within the tier — "that webpage kind of sucks" triggers an automatic upgrade to a more capable model for the retry, with a bias toward staying on the upgraded model for similar future tasks.

**Adaptive routing with learned bias:** If a task fails or gets rejected on a lower-tier model and succeeds on a higher-tier model, the system creates a routing bias: this task type, for this user, needs the higher tier. Future similar tasks route there automatically. The bias is logged and transparent (in developer/admin views), but never surfaces model names to the user.

**The abstraction boundary:** The handler's direct Claude call becomes `kernel.reason(task_metadata, context)`. The handler (and any agent) never imports a provider SDK directly. The kernel owns model clients, manages API keys, handles rate limits, tracks costs, and routes requests. Swapping a model is a configuration change, invisible to every agent.

**Cost logging:** Every reasoning call is logged with: model used, tokens consumed, estimated cost, task type, tenant. Background processes (daemons, awareness evaluations) are logged with the same detail. If a passive daemon is burning more compute than expected, it's visible in logs. This feeds into both the quality/cost dial (economy mode reduces daemon frequency) and operational transparency.

**Zero-cost path:** If one model is configured, routing is a no-op. `kernel.reason()` calls that model. The abstraction costs one function call of overhead. Complexity activates only when multiple models exist and routing decisions matter.

### Primitive 5: The Task Engine

**What it is:** The orchestration layer that receives work from any source, routes it to the right agents and models, manages lifecycle, and delivers results.

**Three sources of tasks:**

**Reactive** — user sends a message. This is 90%+ of interactions. For simple messages, the task engine is a pass-through — message to handler, response back. No decomposition, no overhead. The task engine's existence is structural (the interface through which all work enters) but for simple tasks it's invisible, like a highway on-ramp on an empty road.

**Proactive** — the awareness evaluator detects something worth surfacing. "User will be late to appointment." The task engine handles notification composition, behavioral contract check, channel routing.

**Generative** — a complex request requiring multi-step execution. "Build me a website." The task engine decomposes, assigns agents, manages the pipeline, handles checkpoints.

**Task types:**

| Type | Behavior | Example |
|---|---|---|
| Reactive-simple | Pass-through, no decomposition | "What's on my schedule?" |
| Reactive-complex | May need multi-capability routing | "Get me ready for Thursday's meeting" |
| Proactive-alert | Time-sensitive notification | "You'll be late to your 3pm" |
| Proactive-insight | Non-urgent, queued for right moment | "Your gym membership costs $400 since you last went" |
| Generative | Multi-step, multi-agent, checkpointed | "Build me a website" |
| Think | Holistic reasoning, don't decompose | Complex creative/exploratory work |

**The "think" type is important.** Not everything decomposes. A complex architecture discussion, a creative brainstorm, a nuanced analysis — these need the best model with the fullest context doing holistic reasoning. The task engine recognizes when decomposition would harm the output and routes the entire task to a single powerful reasoning pass instead.

**Task priority — borrowing from OS service loading:**

User engagement is always highest priority. When the user is actively conversing, that reactive task preempts everything except critical alerts (the "you're going to be late" interrupt). Background tasks (daemon processing, insight generation, managed resource maintenance) run at lower priority and yield when the user is active. This mirrors Windows service priority: some are immediate-start (user interaction), some are delayed-start (background maintenance).

**Generative task lifecycle — visible at decision points only:**

For multi-step tasks (building a website, creating a bookkeeping system), the user sees progress at meaningful decision points and completion milestones — not every intermediate step. The web dev agent doesn't interrupt the user to confirm minor technical choices. It either has the information in shared state, asks the primary conversational agent (who decides whether to involve the user), or makes a reasonable default and moves on. The user sees: "Here are three design options" → chooses one → "Your site is live. Here's the URL."

**Managed resources as task output:** When a generative task produces something ongoing (a website, an accounting system, a scheduled report), the task engine registers it as a managed resource in the State Store. Future requests referencing that resource ("change the header on my website") are recognized and routed with full context of what was built and where it lives.

---

## Part 3: Three Modes of Operation

The five primitives compose to produce three modes. The kernel runs all three simultaneously. The user experiences one seamless system.

### Mode 1: Reactive

**Trigger:** User sends a message.
**Experience:** "What's on my schedule today?" → "You have three meetings..."

Primitives compose: Message → Event Stream → Task Engine (pass-through for simple) → Capability Graph (calendar connected) → State Store (load history, preferences) → Reasoning Service (select model) → Agent executes → Response → Event Stream.

**This is what we have today, formalized.** The handler is the agent. The persistence stores are early State Store. The MCP client is the Capability Graph. The only new primitives are the Reasoning Service abstraction (model as parameter) and the Task Engine formalization (pass-through for simple cases).

The zero-cost-path principle ensures this mode adds no perceptible overhead over the current system.

### Mode 2: Proactive

**Trigger:** The system notices something the user should know about.
**Experience:** "Heads up — you have a meeting in 20 minutes and traffic suggests you'll need 35 minutes to get there."

**The awareness evaluator** is a kernel process (not an agent) that evaluates incoming events against the State Store. It answers one question: "Is there anything in the current state of the user's world that warrants attention?"

It is event-driven, not polling. When relevant data arrives (GPS update, email notification, calendar change, time threshold crossed), it evaluates. When 99% of events are irrelevant, it does nothing. Minimal footprint — only activates when information arrives that has potential relevance to the user.

**Two levels of evaluation:**
- Algorithmic checks (no LLM needed): time-based triggers, schedule conflicts, threshold alerts, pattern breaks. Milliseconds.
- Contextual evaluation (lightweight model): "Is this email important enough to interrupt?" Uses behavioral contract as guardrails. Cheap, fast model.

**Detection vs. delivery — the timing problem:**

The awareness evaluator produces two outputs:
1. **Detection:** something worth surfacing exists
2. **Delivery assessment:** when is the right moment?

"Don't forget your kid's birthday is tomorrow and you haven't gotten a present" is detected at 2pm. But the user is in a business meeting. The delivery assessment consults the situation model (event-driven, not polling — calendar says meeting until 3pm, conversation is active about business topics) and queues it for after the meeting. Maybe when time and location suggest the user is getting in their car.

**The situation model** maintains a lightweight estimate of what the user is doing right now. Updated by events: calendar event active → "in a meeting." Conversation about business → confirms it. Meeting ends, 15 minutes pass, GPS shows movement → "transitioning, good time for non-urgent notifications." This is a kernel function, not an agent. It reads events, maintains state, and answers queries from the awareness evaluator.

**Proactive insights — the gym membership example:**

Beyond time-sensitive alerts, the system surfaces non-urgent observations:

"I noticed you're still paying for your gym membership — about $400 since you last went. Want me to cancel it, send you some encouragement to go, or just be quiet about that?"

This comes from the consolidation daemon (see Part 5) during idle processing. It's queued as a proactive-insight task — not time-sensitive, delivered at a natural moment, respecting the current conversation context.

**The trust balance:**
- High-confidence, high-stakes → surface promptly (you'll miss your meeting)
- Medium-confidence, medium-stakes → surface per behavioral contract and timing
- Low-confidence, low-stakes → queue for natural moment or daily update
- The behavioral contract evolves from user reactions: consistently dismissed notification types get throttled

### Mode 3: Generative

**Trigger:** User requests something that requires building, provisioning, or multi-step execution.
**Experience:** "Can you make me a webpage for my stained glass company?" → design options → approval → site goes live → ongoing management.

**Worked example — the web dev scenario:**

This example illustrates all five primitives, multiple agents, and the kernel's orchestration. It also covers the complex mid-project pivot the owner described.

**Initial request:**

User: "Can you make me a webpage for my stained glass company?"

1. Message arrives → **Event Stream** → **Task Engine** creates generative task
2. Task Engine calls **Reasoning Service** (high complexity — needs to understand scope and plan)
3. Reasoning Service returns to the primary conversational agent, which responds:
   "Sure! Tell me what you want, and if you have a web host you'd like to use."
4. User: "I want it to showcase my work and I don't know what a web host is."
5. Primary agent consults **Capability Graph** — photos accessible? Yes (if connected). Hosting? Not connected, available in ecosystem.
6. Primary agent: "I can use the work I see in your photos. For hosting, I have a good option — about $X/year. Let me put together some designs and you pick what feels right."
7. User approves → Task Engine decomposes:
   - Subtask: Design (hatched design agent, Reasoning Service routes to high-tier model for creative work)
   - Subtask: Build (hatched coding agent, Reasoning Service routes to mid-tier for implementation)
   - Subtask: Provision hosting (Capability Graph → install hosting plugin, configure, deploy)
8. Design agent produces three options → primary agent presents to user (decision point — user sees this)
9. User picks option B → coding agent builds → hosting provisioned → site deployed
10. Primary agent: "Your site is live at [URL]. Take a look!"
11. **State Store** registers managed resource: website, URL, hosting provider, deployment access, design files

**The mid-project pivot:**

User: "I really don't like this. Actually, I want the whole website redesigned for a completely different purpose, with different hosting. And add a page that integrates with my business calendar — actually, make a new calendar just for this that's my public-facing business calendar. Every morning, sync it from my personal calendar with blocks that just say 'unavailable' for my appointment times."

This is a high-complexity request with multiple interconnected elements. The Task Engine handles it:

1. Primary agent recognizes this as a major scope change → Task Engine creates new generative task, archives the old one
2. Reasoning Service gets the full request (high complexity → best model, "think" type — don't over-decompose yet, need to understand the holistic vision first)
3. Agent reasons about the full scope and decomposes into a plan:
   - New website design + build (different purpose, different hosting)
   - New public-facing Google Calendar creation
   - Daily sync job: personal calendar → public calendar with "unavailable" blocks
   - Website integration with public calendar
4. Each subtask routes to appropriate agents and models
5. The sync job becomes a managed resource — a recurring task the system runs every morning
6. Decision points surface to the user: "Here's the new design direction — thoughts?" and "I've set up your public calendar. Each morning I'll sync your availability. Want to review how it looks?"
7. Technical details (calendar API setup, hosting migration, cron scheduling) happen invisibly

**What the user experienced:** One conversation. No technical jargon. Approval at meaningful decision points. A working result.

**What the kernel orchestrated:** Task decomposition, multiple agent types (design, coding, calendar), capability acquisition (new hosting, new calendar), managed resource registration (website, sync job), behavioral contract adherence (cost approval for hosting), and a complex mid-project pivot handled gracefully.

**Agent coordination in generative mode:**

Agents coordinate through shared state in the State Store, not message passing. The design agent writes design artifacts to shared state. The coding agent reads them. If the coding agent needs to confirm something minor (color of a button), it checks shared state first (user's brand preferences, design spec). If the answer isn't there, it asks the primary conversational agent — who decides whether to ask the user or make a reasonable call. The user is only involved at genuine decision points.

For deliberation (as distinct from coordination) — three specialist agents reasoning together about a complex problem — the kernel can orchestrate a structured conversation: set the agenda, provide shared context, manage turns, collect the synthesis. This is legitimate multi-agent value for complex problems, distinct from the coordination pattern.

---

## Part 4: Agents in this Architecture

### Agents are not persistent processes

In KERNOS, agents are like contractors: the kernel instantiates them when work needs doing and releases them when it's done. The agent's configuration (template) persists. The agent's accumulated state lives in the State Store. But the running process is ephemeral.

This is possible because the kernel handles everything an agent would otherwise need to manage: memory retrieval, context assembly, capability access, state persistence, safety enforcement. The agent just thinks.

### Agent lifecycle: seed, hatch, refine, evaluate

**Seed** — A base template defining a type of work. Contains: system prompt skeleton, required capability types, model preferences (expressed as task metadata, not model names), default behavioral constraints. Seeds come from a template library (KERNOS defaults, community-contributed, or user-created).

**Hatch** — The kernel instantiates a seed for a specific user and task. The template is personalized:
- User context from State Store (industry, preferences, communication style, strengths and weaknesses)
- Behavioral contracts loaded (what this agent type is allowed to do for this user)
- Available capabilities checked
- Model selected via Reasoning Service
- Relevant knowledge assembled

The hatched agent is personalized for this user and equipped for this task.

**Refine** — Every interaction produces signal:
- User approves output → this agent type + model tier works for this user
- User rejects or corrects → behavioral contract or model selection needs adjustment
- User gives explicit instruction → direct contract update
- Patterns emerge → preference extracted to State Store

Refinement modifies the user's instance, not the base template. Two users with the same "email manager" seed diverge over time.

**Evaluate** — The kernel tracks effectiveness:
- Acceptance rate, revision rate, time to completion, cost
- This data feeds model routing (proven effectiveness), template refinement, capability recommendations
- Logged transparently for operational review

### Soul vs. contract

**The behavioral contract** governs *what* the agent does. Evolves from explicit signals — approvals, rejections, instructions. Changes frequently.

**The soul** governs *how* the agent does it. Tone, verbosity, personality, communication style. Set during onboarding or defaults to something good. Changes rarely and only by explicit user request. Don't infer personality preferences from interaction patterns — that way lies uncanny valley (OSBuilder's warning).

### What agents receive from the kernel

1. **Identity** — template + user-specific refinements + soul
2. **Context** — conversation history, relevant user knowledge, situation state. Pre-assembled. Potentially inline-annotated (Phase 2+).
3. **Capabilities** — MCP tool list, connected and authenticated. Available-but-not-connected capabilities flagged so the agent can offer setup.
4. **Contract** — musts, must-nots, preferences, escalation triggers
5. **Task** — what to accomplish

### What agents DON'T do

- Manage their own memory (kernel persists)
- Choose which model to run on (kernel routes)
- Discover or install capabilities (kernel manages the graph)
- Enforce their own safety rules (kernel enforces contracts)
- Know about platform specifics (kernel translates)
- See mechanical kernel state unless it's relevant to their task (booleans, flags, and infrastructure state stay in the kernel — only information the agent needs for its current task enters its context)

### The context boundary principle

Agent context is precious. The kernel is disciplined about what enters it:

- Information the agent needs to serve the user right now → inject
- Information the user needs to know right now → inject (so the agent can deliver it)
- Information queued for later delivery → hold in State Store, don't clutter current context
- Mechanical kernel state (task booleans, routing decisions, daemon status) → never inject unless directly relevant

The memory cohort (Phase 2+) and inline annotation pattern operationalize this: relevant context appears exactly where it's relevant in the user's message, and irrelevant information stays out entirely. Most of the time, when nothing is relevant, the message passes through unmodified and the agent's context is clean.

---

## Part 5: The Awareness Loop and Background Processing

### The awareness evaluator

A kernel process that evaluates incoming events against the State Store. Event-driven, not polling. Activates when data arrives with potential user relevance. Most of the time, does nothing.

When it detects something worth surfacing, it creates a proactive task with a delivery assessment: what to tell the user, and when.

The situation model (lightweight, event-driven estimate of what the user is doing) informs delivery timing. Not a full agent — a kernel function that reads events and maintains a simple state: "user is in a meeting," "user is driving," "user is winding down."

### The consolidation daemon

Runs during idle periods — when the user isn't actively interacting. Reads the event stream and does slow, thoughtful work:

**Maintenance work:**
- Extracts patterns across conversations
- Resolves fact contradictions ("user said Portland in March, mentioned moving to Seattle in June")
- Strengthens frequently-accessed memories
- Decays unused memories

**Insight generation — the creative function:**

This is where the system produces value the user didn't ask for. It looks for non-obvious connections across the user's data:

- "Your glass supplier raised prices 15% and the craft fair season starts next month — you might want to adjust your pricing."
- "I noticed you're still paying for your gym membership — about $400 since you last went. Want me to cancel it, encourage you to go, or be quiet about it?"
- "You mentioned wanting to learn pottery three separate times over the past two months. There's a studio near your Thursday route — want me to look into classes?"

These insights are queued as proactive-insight tasks and delivered at natural moments, not interrupts.

This is the "dreaming" analogy: the system uses downtime to imagine connections the user hasn't asked about. It's what makes the system feel genuinely intelligent rather than merely responsive. "Last night I realized that X might be a good idea for Y — have you thought about that?"

**Cost awareness:** Every daemon run is logged with tokens consumed, model used, and cost. If background processing exceeds expected budgets, it's visible in operational logs. Economy mode reduces daemon frequency and routes to cheaper models. The daemon should never silently burn compute — transparency is mandatory.

### The daily briefing — emergent, not imposed

The daily briefing is NOT a default feature pushed on every new user. It emerges naturally:

1. User connects calendar → agent naturally suggests: "Want me to give you a quick summary of your day each morning?"
2. User connects email → "Want me to include email highlights in your daily update?"
3. User asks about something that would benefit from regular updates → agent suggests adding it to a briefing

The briefing assembles itself from connected capabilities, suggested at natural moments. If the user never wants one, they never get one. If they'd benefit from one but haven't thought to ask, the agent offers gently when the moment is right.

This aligns with "ambient, not demanding" — the system suggests value, doesn't impose workflow.

---

## Part 6: User Profiles and Adaptive Behavior

### Strengths and weaknesses

The State Store tracks not just what the user likes but what they're good and bad at:

- "User has difficulty remembering things" → more proactive reminders, more context in notifications, birthday alerts with enough lead time to act
- "User is a world-renowned medical doctor" → trust domain expertise, don't over-explain medical terms, less alarm when scheduling surgery
- "User is not technical" → never surface technical details, frame everything in terms of their world
- "User is very cost-conscious" → default to economy tier, always mention costs, suggest savings

This profile builds from conversation over time. The agent notices: the user forgot an appointment twice → increase reminder lead time. The user consistently asks for clarification on financial terms → adjust explanations. The user never wants to see technical details → route all technical decisions to kernel defaults.

### Long-term memory architecture

The "who is John?" question illustrates the architecture:

**Event stream** (raw history): Contains every conversation mentioning John, every calendar event with John, every email involving John. Over years, this could be millions of events. This is NOT searched at query time.

**Memory projectors** (background processing): Extract facts from events and write them to the State Store. "John — met at convention (2025-06-15), had appointment (2026-02-13), phone number (124) 456-7890, discussed custom window project."

**State Store** (indexed, queryable): When user asks "who is John?", the kernel queries the State Store — fast, indexed lookup by entity name. Returns structured knowledge with provenance. The agent receives it pre-assembled: "You spoke with a John at the convention last year and had an appointment with him February 13. His number at that time was (124) 456-7890. That the guy?"

**Capability augmentation**: The kernel can also query connected capabilities (Google Calendar search, email search) to supplement or verify State Store knowledge. Both sources contribute to the answer.

**Scaling consideration**: The State Store needs proper indexing and data management. Entity records, not flat files. As the system accumulates knowledge over years, the State Store grows but remains queryable because it's structured and indexed — unlike the raw event stream, which grows linearly and is only used for replay/projection, not runtime queries.

---

## Part 7: What the User Experiences

None of the above is visible. Here's what they see.

### Day one

User messages the bot. "Hi, I need help managing my stained glass business."

System provisions workspace. Conversational agent hatches with defaults. "Hi! Tell me about your business and what takes up most of your time."

Conversation continues. The system learns: stained glass artist, craft fairs, client list, installations. Agent suggests: "Scheduling sounds like a big part of your day. Want me to connect to your calendar?" User agrees. Calendar connected.

No briefings imposed. No features pushed. Value delivered through conversation.

### Week one

User texts daily. Calendar queries, appointment additions, general questions. Agent refines — learns communication style, work hours, client naming conventions.

Agent notices: "You schedule a lot of installations. Want me to handle scheduling requests from your clients? They could message me directly." User agrees. External contact handling enabled with behavioral contract: always confirm new bookings, never share private details.

Awareness evaluator starts useful work: appointment reminders, conflict detection.

Agent offers at a natural moment: "Want me to give you a quick summary of your schedule each morning?" User says yes. First briefing capability emerges.

### Month one

Email connected. Agent triages: installation requests surfaced, spam archived, newsletters saved for evening mention. Behavioral contract refined through dozens of approve/reject signals.

"Can you make me a website?" Generative mode. Design options, hosting setup, site goes live. Registered as managed resource.

Consolidation daemon starts producing insights: "Your glass supplier raised prices — might want to review your pricing before the craft fair."

### The daily experience

The user texts when they need something and gets fast, contextual responses. Proactive alerts arrive when time-sensitive and relevant. Insights surface at natural moments. Managed resources work in the background. The user's day is supported without being interrupted.

### What the user never sees

Model names. Agent names. Task decomposition. Capability graphs. Event streams. The kernel. They see a system that knows them and works for them.

---

## Part 8: What Agents Experience

### What the kernel provides

1. **Identity** — template + user-specific refinements + soul
2. **Context** — pre-assembled, only what's relevant to the current task. Mechanical kernel state stays out.
3. **Capabilities** — tools available right now, plus awareness of what could be connected
4. **Contract** — what you're allowed to do, clearly structured
5. **Task** — what to accomplish right now

### What the kernel expects back

1. **Response** — the output (text, code, design, structured data)
2. **Tool calls** — routed through the kernel (logged, contract-enforced, error-handled)
3. **State updates** — facts learned, preferences observed, resources created. Agent surfaces; kernel persists.
4. **Escalation signals** — "I need a more capable model," "this exceeds my contract," "I need user input"

---

## Part 9: Implementation Layers

### Layer 0: What we have (Phase 1A — complete)

- Message gateway with adapter isolation (Discord, SMS ready)
- Single handler calling Claude with MCP tools
- Three-store persistence (conversation, tenant, audit)
- Shadow archive structure
- Platform-aware, capability-honest system prompts
- Tenant auto-provisioning

### Layer 1: The kernel foundation (Phase 1B — next)

**Build:**
- **Event Stream formalization** — generalize three stores into typed event stream with subscriber interface. Stores become projections.
- **Reasoning Service abstraction** — extract LLM calls from handler into kernel service. Model as parameter. Provider interface (even if one provider configured). Cost logging per call.
- **Capability Graph formalization** — extend MCPClientManager into three-tier registry with dependency awareness. System prompt builder reads live state.
- **Task Engine (minimal)** — formalize handler as reactive task executor. Zero-cost pass-through for simple tasks. Task metadata and lifecycle. Foundation for decomposition.
- **State Store abstraction** — generalize InstanceStore into richer state model. User knowledge, behavioral contracts (defaults), capability state. Clean interface for future MemOS integration.
- **Agent template structure** — define what a template contains. One or two templates (conversational, possibly coding). Seed/hatch for single user.
- **CLI for kernel operations** — start/stop, capability status, event stream inspection, state queries
- **Tenant isolation verification** — two tenants on same instance cannot access each other's state

**Design for but don't build:**
- Multi-model routing logic (build the interface, use one model)
- Awareness evaluator (design the event-driven trigger model, don't implement the evaluation loop)
- Consolidation daemon (design the idle-processing pattern, don't implement)
- Agent deliberation protocol (acknowledge the pattern, don't build the orchestration)
- Quality/cost tier UI (implement the routing hook, defer the user-facing dial)
- Daily briefing assembly (defer until multiple capabilities are connected)
- Inline annotation (design the enriched-content path in the message format, don't build the annotator)

### Layer 2: Proactive and multi-agent (Phase 2)

- Awareness evaluator with situation model and delivery scheduling
- Behavioral contract evolution from interaction signals
- Multiple agent templates, multi-step task decomposition
- Agent coordination through shared state
- Agent deliberation for complex problems
- Multi-model routing with effectiveness tracking
- Quality/cost tiers (user-facing)
- Email, research, and other core agents
- Inline contextual annotation
- Consolidation daemon with insight generation
- External contact handling with permission model
- Daily briefing (emergent from connected capabilities)

### Layer 3: Experience and ecosystem (Phase 3-4)

- Briefing surface (rich daily updates)
- Trust dashboard (behavioral contract visualization)
- Mobile app with AG-UI
- Capability marketplace (discoverable tier)
- Agent template marketplace
- Public SDK for third-party templates
- Self-extending capabilities (agents that build tools and systems)
- Managed resource ecosystem (websites, bookkeeping, legal docs)
- User strength/weakness adaptive behavior
- Long-term memory with proper entity indexing
- Intent tracking (why the user made choices, not just what they chose)

---

## Part 10: Design Decisions This Outline Encodes

| Decision | Choice | Why |
|---|---|---|
| Kernel location | Application layer, not OS level | Users are on phones, not Linux machines |
| Agent model | Ephemeral workers, not persistent processes | Kernel handles infrastructure. Agents just think. |
| Coordination mechanism | Shared state for coordination, structured conversation for deliberation | OSBuilder insight + acknowledgment that brainstorming has distinct value |
| LLM relationship | Managed resource via provider interface | Provider-agnostic. OpenRouter or direct API. Agents never call LLMs directly. |
| Model selection UX | Quality/cost tiers, no model names | The plumber doesn't know Haiku from Opus and shouldn't need to |
| Proactive architecture | Event-driven awareness evaluator + delivery scheduler | Not polling. Activates on relevant data arrival. Separates detection from timing. |
| Capability installation | Surface user-world decisions only | Money, data access, external communication. Technical details are kernel's job. |
| Background processing | Consolidation daemon with maintenance + insight generation | Not just memory cleanup — creative connection-finding. The "dreaming" function. |
| Daily briefing | Emergent, not imposed | Suggested at natural moments when capabilities make it useful. Ambient, not demanding. |
| Context boundary | Only inject what agent needs right now | Mechanical state stays in kernel. Queued notifications held until appropriate. |
| Notification timing | Detection separate from delivery | Awareness evaluator finds things. Situation model decides when to tell user. |
| User profile | Includes strengths, weaknesses, domain expertise | Adapts agent posture, reminder frequency, explanation depth |
| Long-term memory | State Store (indexed, queryable) projected from Event Stream (append-only, not queried at runtime) | Scales over years. Fast lookup. Raw history preserved for replay/audit. |
| Elegance filter | Every complexity must justify with value user would miss | If removing a feature still feels good, don't build it |
| Zero-cost path | Every primitive is a no-op for simple cases | The plumber's "what's on my schedule" is just as fast as today |
| Over-engineering guard | Layer 1 builds interfaces. Layers 2-3 fill them. | We're building the first multi-story building, not the Eiffel Tower. |

---

*v2 — incorporating owner feedback on LLM registry via providers, quality/cost tiers, emergent briefings, long-term memory scaling, event-driven situation model, context boundary discipline, capability installation framing, user profiles, notification timing, managed resources as agent-built systems, insight generation, and the elegance principle as design filter.*

*This outline is the foundation for Phase 1B specifications. The specs will scope Layer 1 — what gets built now — with specific acceptance criteria for each component, ruthlessly filtered by: does the plumber's simple text still feel instant, and does every new piece of infrastructure justify its existence with value?*
