# About Kernos

## What Kernos Is

Kernos is a personal AI agent operating system. It lives in the cloud, works 24/7, and is reachable by text message or Discord. Unlike chatbots that forget you between sessions, Kernos builds a persistent understanding of your life — your preferences, your projects, your people, your patterns — and uses that understanding to serve you better over time. It earns trust through thousands of correct small actions. Built for non-technical users: text a phone number, have an agent working for you within an hour.

## What It Can Do

- **Remembers your life across conversations.** Not just this chat — across days, weeks, months. Facts, preferences, decisions, and context persist through a compaction-driven memory system that never loses what matters.
- **Organizes context into domains automatically.** Work, personal, creative projects, health, legal, education — each gets its own space with isolated memory, files, and tools. You don't manage this. The system detects recurring topics and creates domains organically.
- **Builds tools for you.** "Track my invoices" → a working invoice tracker in seconds. "Log my calories" → a calorie logger with daily budgets. Tools are built in conversation, tested automatically, and available across all your spaces.
- **Manages your calendar, searches the web, browses pages.** Connected to Google Calendar, Brave Search, and a web browser. Creates events, checks schedules, looks things up — all from natural language.
- **Follows your rules.** Behavioral contracts you set are enforced at the infrastructure level. "Always confirm before spending money" or "don't ask follow-ups about food logging" — these are captured once and respected permanently.
- **Gets smarter over time.** Learns your procedures, detects capability gaps, proposes solutions. A 30-minute awareness cycle watches for patterns and suggests improvements.

## How It Works (Technical)

- **Cohort architecture:** Multiple specialized agents working in parallel per message — routing, analysis, knowledge selection, tool surfacing, fact extraction, all coordinated by the handler pipeline.
- **Hierarchical context spaces:** Tree structure (General → Domain → Subdomain) with scope chain inheritance. Knowledge, files, covenants, and procedures flow down the tree.
- **Universal tool catalog:** Intent-based surfacing from a catalog of all available tools. Token-budgeted window with schema-weighted LRU eviction keeps reasoning calls lean.
- **Compaction-driven memory:** Conversation logs compress into a Ledger (topic index with source references) + Living State (current operational reality). Full conversations permanently archived and retrievable.
- **Behavioral contracts:** Covenants captured from user instructions, enforced at infrastructure level. Space-scoped for domain-specific rules. The agent thinks; the kernel enforces.
- **Procedural knowledge:** Domain-specific workflows stored as `_procedures.md` files, inherited through the space tree. Covenants define behavior; procedures define processes.
- **Agentic workspace:** The agent can write code, test it, register tools, and track artifacts. Workspace-built tools become permanently available across all spaces.
- **Provider-neutral:** Works with any LLM backend (currently OpenAI Codex, designed for Anthropic Claude). No load-bearing features on any specific provider's capabilities.

## What Makes It Different

Every AI agent framework faces the same set of hard problems. Kernos solves them architecturally, not as afterthoughts.

**The memory problem.** Most agents are stateless — they forget everything between sessions. RAG bolts retrieval onto a memoryless system. Kernos has compaction-driven memory built into its core: a Living State that maintains current truth, a Ledger that indexes every past conversation, and a retrieval system that walks scope chains through hierarchical domains. The agent doesn't search for memories — it lives inside a continuous understanding.

**The context problem.** Agent frameworks dump everything into one context window and hope the LLM sorts it out. Kernos organizes context into a tree of hierarchical spaces — each with isolated memory, files, tools, and behavioral rules. A D&D campaign doesn't pollute your tax preparation. A client project doesn't leak into your health tracking. Context is structurally separated, not keyword-filtered.

**The tool problem.** Most frameworks predefine tools or require developers to write integrations. Kernos builds its own tools in conversation. "Track my invoices" → working invoice tracker in seconds. Tools register in a universal catalog, surface by intent (not keyword matching), and stay within a token budget via schema-weighted LRU eviction. The user never sees infrastructure.

**The trust problem.** Chatbots make promises they can't keep because behavioral rules exist only in the prompt. Kernos enforces behavioral contracts at the infrastructure level — the kernel gates every action, tracks covenants, and prevents violations structurally. The agent thinks; the kernel enforces. Rules survive across sessions, spaces, and tool calls.

**The user problem.** Agent frameworks are built for developers. They require API keys, configuration files, Python environments, and technical understanding. Kernos is built for non-technical users. Text a phone number. Talk naturally. The system learns, adapts, builds tools, and organizes itself around your life — no technical setup, no maintenance, no code.

**The scaling problem.** As users do more, most agent systems slow down — context windows overflow, tool lists bloat, response quality degrades. Kernos scales through compaction (conversation history compresses without losing truth), domain separation (each topic has its own focused context), and the tool window (token-budgeted with automatic eviction). More activity makes the system smarter, not slower.

## Who Built It

Built by the design review, sole design review and developer. Designed and constructed from first principles over 6 weeks. 1,600+ automated tests. Every mechanism — from the compaction system to the hierarchical context spaces to the tool-building workspace — designed, specified, implemented, and tested by one person with one vision: a second brain that works while you sleep.
