# Research: Novel Architectures in KERNOS

**[📄 Read the full report (PDF, 20 pages) →](KERNOS-Novel-Architectures-Report.pdf)**

A code-verified survey of the mechanisms KERNOS contributes beyond the mid-2026
landscape of agentic harnesses — and an honest accounting of which of its
elements are commonplace. Prepared 21 July 2026.

## The question

*Which of KERNOS's architectural elements are genuinely novel in the current
landscape of agentic harnesses, and which are commonplace?* Commodity machinery
— MCP tool access, platform adapters, SQLite persistence, provider fallback
chains, schedulers, sandboxed code execution, event logs — is explicitly
excluded from the findings.

## Method

Three independent verification tracks, reconciled:

1. **Claims extraction** — the full documentation corpus (`DECISIONS.md`,
   `TECHNICAL-ARCHITECTURE.md`, `DESIGN-PRINCIPLES.md`, `docs/architecture/`)
   read to enumerate every claimed mechanism.
2. **Code verification** — three inspection passes traced each claim to source
   with file-and-line evidence, actual constants and formulas, and reported
   discrepancies where code and docs disagree (four found; all disclosed in §6
   of the report rather than smoothed over).
3. **Adversarial landscape survey** — two research passes over ~50 systems and
   papers (Letta/MemGPT, Mem0, Zep/Graphiti, LangGraph/LangMem, CrewAI,
   AutoGen, Anthropic/OpenAI/Google native memory, SICA, Darwin-Gödel Machine,
   MOSS, AlphaEvolve, NeMo Guardrails, Operator, Devin, Character.AI, Replika,
   and the 2025–2026 research literature) with instructions to **refute**
   novelty wherever possible. Two refutations succeeded and are honored in the
   findings.

## Findings at a glance

| § | Element | Assessment |
| --- | --- | --- |
| 4.1 | Automatic hierarchical context spaces | **Novel** — the survey's single clearest gap |
| 4.2 | Compaction as a multi-harvest metabolic boundary | Novel as integration |
| 4.3 | Dual-strength (Bjork) memory | Rare — one niche contemporaneous precedent |
| 4.4 | The Cognitive UI (rendered, not accumulated) | Novel as formalization |
| 4.5 | The Quiet Cohort | Novel as discipline |
| 4.6 | "User intent is authorization" + covenants in the dispatch path | **Novel** — no formalized equivalent found |
| 4.7 | The governed self-improvement stack | **Novel** — boot-guard rollback, DB-enforced recursion bounds, constitutional-file boundaries, diff-hash-bound approvals each individually absent from every surveyed system |
| 4.8 | The friction loop (automatic detection + upfront approval) | **Novel** — the landscape splits this combination apart everywhere |
| 4.9 | Per-member emergent identity + relationship-mediated disclosure | **Novel** — no production precedent |
| 4.10 | Token-budgeted tool surfacing with promotion | Novel mechanism |
| 4.11 | The 24 Escalation, narration-audited completion, typed failure-as-message, plain-English self-test | Original framings, minor |

## Reading notes

- Negative findings ("no system found that…") are claims about a bounded,
  good-faith adversarial search — not proofs of absence. §6 of the report
  states the search's known blind spots.
- The report discloses every documentation-vs-code discrepancy the
  verification found, including two that weaken KERNOS's own documented
  claims. Novelty judgments were downgraded wherever the adversarial pass
  located precedent, however niche.
- For the design intent behind the mechanisms surveyed here, see
  [`docs/DESIGN-PRINCIPLES.md`](../DESIGN-PRINCIPLES.md) — 17 named, portable
  patterns derived from live operation.
