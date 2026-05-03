"""Extended live test runner for SPEC-2B-v2: LLM Context Space Routing.

Covers 7 adversarial scenarios:
  1. Multiple Spaces — 3 non-daily spaces, verify routing distinguishes them
  2. Cold Return — abrupt domain dive after 4 daily messages, no warm-up
  3. Rapid Switching — 4-message alternation across domains
  4. Ambiguous Messages — genuinely unclear domain signals
  5. Multi-Tag Verification — cross-domain messages tagged to both spaces
  6. Cross-Domain Injection — background awareness after space switch
  7. Thread Coherence — D&D summary free of business/studio contamination
  8. Verbatim History Isolation — agent recites only current space thread

Usage: source .venv/bin/activate && python tests/live/run_live_test_2b_v2_extended.py
"""
import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

from mcp import StdioServerParameters

from kernos.messages.handler import MessageHandler
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.capability.client import MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
from kernos.kernel.events import JsonEventStream
from kernos.kernel.engine import TaskEngine
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("KERNOS_DATA_DIR", "./data")
TENANT_ID = f"discord:{os.getenv('DISCORD_OWNER_ID', '000000000000000000')}"
SENDER_ID = os.getenv("DISCORD_OWNER_ID", "000000000000000000")
CONVERSATION_ID = "live_test_2bv2_ext"

# Must match handler.CROSS_DOMAIN_INJECTION_TURNS
CROSS_DOMAIN_TURNS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_message(content: str) -> NormalizedMessage:
    return NormalizedMessage(
        content=content,
        sender=SENDER_ID,
        sender_auth_level=AuthLevel.owner_verified,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id=CONVERSATION_ID,
        timestamp=datetime.now(timezone.utc),
        instance_id=TENANT_ID,
    )


class ExtendedLiveTestRunner:
    def __init__(self):
        self.handler: MessageHandler | None = None
        self.state: JsonStateStore | None = None
        self.conversations: JsonConversationStore | None = None
        self.results: list[dict] = []
        self.step = 0
        # label -> space_id, populated in setup_spaces()
        self.space_ids: dict[str, str] = {}

    async def setup(self):
        """Initialize the full handler stack."""
        print("Setting up handler stack...")
        events = JsonEventStream(DATA_DIR)
        self.state = JsonStateStore(DATA_DIR)
        self.conversations = JsonConversationStore(DATA_DIR)

        mcp_manager = MCPClientManager(events=events)
        credentials_path = os.getenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "")
        if credentials_path:
            mcp_manager.register_server(
                "google-calendar",
                StdioServerParameters(
                    command="npx",
                    args=["@cocal/google-calendar-mcp"],
                    env={"GOOGLE_OAUTH_CREDENTIALS": credentials_path},
                ),
            )
        await mcp_manager.connect_all()

        registry = CapabilityRegistry(mcp=mcp_manager)
        for cap in KNOWN_CAPABILITIES:
            registry.register(dataclasses.replace(cap))
        for server_name, tools in mcp_manager.get_tool_definitions().items():
            cap = registry.get(server_name)
            if cap:
                cap.status = CapabilityStatus.CONNECTED
                cap.tools = [t["name"] for t in tools]

        tenants = JsonInstanceStore(DATA_DIR)
        audit = JsonAuditStore(DATA_DIR)
        provider = AnthropicProvider(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        reasoning = ReasoningService(provider, events, mcp_manager, audit)
        engine = TaskEngine(reasoning=reasoning, events=events)

        self.handler = MessageHandler(
            mcp_manager, self.conversations, tenants, audit, events,
            self.state, reasoning, registry, engine,
        )
        print(f"Handler ready. Tenant: {TENANT_ID}")
        print(f"Conversation: {CONVERSATION_ID}\n")

    async def setup_spaces(self):
        """Pre-create test spaces so the router has real descriptions to work with.

        Finds existing D&D space by description/name heuristic. Creates
        Ironclad Consulting and Home Studio if not already present.
        """
        now = _now()
        existing = await self.state.list_context_spaces(TENANT_ID)
        existing_by_name = {s.name: s for s in existing}

        print("--- Space setup ---")

        # Daily — must exist (handler creates it on first message; prior tests already ran)
        daily = next((s for s in existing if s.is_default), None)
        if daily:
            self.space_ids["daily"] = daily.id
            print(f"  daily:    {daily.id} ({daily.name})")
        else:
            print("  daily:    NOT FOUND — will be created by first handler call")

        # D&D — find by description content or name
        dnd = next(
            (s for s in existing if not s.is_default
             and ("pip" in (s.description or "").lower()
                  or "veloria" in (s.name + (s.description or "")).lower()
                  or "dnd" in s.name.lower()
                  or "campaign" in s.name.lower())),
            None,
        )
        if dnd:
            self.space_ids["dnd"] = dnd.id
            print(f"  dnd:      {dnd.id} ({dnd.name}) [existing]")
        else:
            new_dnd = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                instance_id=TENANT_ID,
                name="Veloria Campaign",
                description=(
                    "An ongoing D&D campaign set in Veloria, a high-magic world. "
                    "Pip Thornwood, a halfling rogue, was hired by guild contact Shade to steal "
                    "a magical compass from merchant Aldrik. The compass always points toward "
                    "danger. Shade holds a fifteen-year grudge against Aldrik over an old debt."
                ),
                space_type="domain",
                status="active",
                is_default=False,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(new_dnd)
            self.space_ids["dnd"] = new_dnd.id
            print(f"  dnd:      {new_dnd.id} (Veloria Campaign) [created]")

        # Ironclad Consulting
        if "Ironclad Consulting" in existing_by_name:
            biz = existing_by_name["Ironclad Consulting"]
            self.space_ids["business"] = biz.id
            print(f"  business: {biz.id} (Ironclad Consulting) [existing]")
        else:
            biz_space = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                instance_id=TENANT_ID,
                name="Ironclad Consulting",
                description=(
                    "Business consulting work. Client Henderson and the Ironclad account. "
                    "Scope of work, proposals, SOW, client meetings, Q2 deliverables, "
                    "and engagement strategy with the operations team."
                ),
                space_type="project",
                status="active",
                is_default=False,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(biz_space)
            self.space_ids["business"] = biz_space.id
            print(f"  business: {biz_space.id} (Ironclad Consulting) [created]")

        # Home Studio
        if "Home Studio" in existing_by_name:
            studio = existing_by_name["Home Studio"]
            self.space_ids["studio"] = studio.id
            print(f"  studio:   {studio.id} (Home Studio) [existing]")
        else:
            studio_space = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                instance_id=TENANT_ID,
                name="Home Studio",
                description=(
                    "Personal project: building a home recording studio. "
                    "Acoustic treatment, monitor placement, bass trapping, flutter echo reduction. "
                    "Ongoing decisions about room layout and equipment purchases."
                ),
                space_type="project",
                status="active",
                is_default=False,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(studio_space)
            self.space_ids["studio"] = studio_space.id
            print(f"  studio:   {studio_space.id} (Home Studio) [created]")

        print(f"\nSpace ID map: {self.space_ids}\n")

    async def send(self, content: str, step_label: str = "") -> dict:
        """Send one message and capture the full result."""
        self.step += 1
        label = step_label or f"Step {self.step}"
        print(f"[{label}] SEND: {content[:100]}")

        t0 = time.monotonic()
        response = await self.handler.process(make_message(content))
        elapsed = time.monotonic() - t0

        profile = await self.state.get_instance_profile(TENANT_ID)
        active_space_id = profile.last_active_space_id if profile else ""
        active_space = None
        if active_space_id:
            active_space = await self.state.get_context_space(TENANT_ID, active_space_id)

        # Pull tags from the stored user message
        recent = await self.conversations.get_recent_full(TENANT_ID, CONVERSATION_ID, limit=4)
        tags: list[str] = []
        for m in reversed(recent):
            if m.get("role") == "user":
                tags = m.get("space_tags", [])
                break

        space_name = active_space.name if active_space else "unknown"
        is_daily = active_space.is_default if active_space else False

        print(f"         FOCUS: {active_space_id} ({space_name}){'  [DAILY]' if is_daily else ''}")
        print(f"         TAGS:  {tags}")
        print(f"         TIME:  {elapsed:.1f}s")
        print(f"         RESP:  {response[:120]}\n")

        result = {
            "step": self.step,
            "label": label,
            "sent": content,
            "response": response,          # full, untruncated
            "focus_space_id": active_space_id,
            "focus_space_name": space_name,
            "is_daily_focus": is_daily,
            "tags": tags,
            "elapsed_s": round(elapsed, 2),
            "timestamp": _now(),
        }
        self.results.append(result)
        await asyncio.sleep(1)
        return result

    # ------------------------------------------------------------------ #
    # Inspection helpers
    # ------------------------------------------------------------------ #

    async def inspect_spaces(self) -> list[dict]:
        spaces = await self.state.list_context_spaces(TENANT_ID)
        return [
            {
                "id": s.id,
                "name": s.name,
                "type": s.space_type,
                "status": s.status,
                "is_default": s.is_default,
                "description": s.description,
                "last_active_at": s.last_active_at,
            }
            for s in spaces
        ]

    async def inspect_topic_hints(self) -> dict:
        from kernos.utils import _safe_name
        p = Path(DATA_DIR) / _safe_name(TENANT_ID) / "state" / "topic_hints.json"
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return {}

    async def inspect_full_conversation(self) -> list[dict]:
        """All messages in the test conversation, with space_tags."""
        entries = await self.conversations.get_recent_full(
            TENANT_ID, CONVERSATION_ID, limit=200
        )
        return [
            {
                "role": e.get("role"),
                "content": e.get("content", ""),
                "space_tags": e.get("space_tags"),
                "timestamp": e.get("timestamp", "")[:19],
            }
            for e in entries
        ]

    async def inspect_space_thread(self, space_id: str, max_messages: int = 50) -> list[dict]:
        """Messages the agent sees when focused on this space."""
        thread = await self.conversations.get_space_thread(
            TENANT_ID, CONVERSATION_ID, space_id, max_messages=max_messages
        )
        return [
            {
                "role": m.get("role"),
                "content": m.get("content", ""),
                "timestamp": m.get("timestamp", "")[:19],
            }
            for m in thread
        ]

    async def inspect_cross_domain(self, active_space_id: str) -> list[dict]:
        """Messages that would be (or were) injected as cross-domain background."""
        cross = await self.conversations.get_cross_domain_messages(
            TENANT_ID, CONVERSATION_ID, active_space_id,
            last_n_turns=CROSS_DOMAIN_TURNS,
        )
        return [
            {
                "role": m.get("role"),
                "content": str(m.get("content", "")),
                "space_tags": m.get("space_tags", []),
                "timestamp": m.get("timestamp", "")[:19],
            }
            for m in cross
        ]

    # ------------------------------------------------------------------ #
    # Test sequence
    # ------------------------------------------------------------------ #

    async def run_test_sequence(self):
        print("=" * 70)
        print("EXTENDED LIVE TEST: SPEC-2B-v2 Real-World Routing Complexity")
        print("=" * 70 + "\n")

        await self.setup_spaces()

        # Re-check daily space ID after first handler call (idempotent)
        all_spaces = await self.inspect_spaces()
        if "daily" not in self.space_ids:
            daily = next((s for s in all_spaces if s["is_default"]), None)
            if daily:
                self.space_ids["daily"] = daily["id"]

        phases: dict = {}

        # ================================================================
        # PHASE 1: MULTIPLE SPACES
        # Build content in 3 non-daily spaces and verify routing accuracy.
        # ================================================================
        print("=" * 60)
        print("PHASE 1: MULTIPLE SPACES")
        print("=" * 60)

        p1: list[dict] = []

        # D&D block
        p1.append(await self.send(
            "Let's pick up the Veloria campaign. Pip just escaped the market district with the compass and leveled up to level 3.",
            "P1.1 — D&D: session start",
        ))
        p1.append(await self.send(
            "Pip gained the Cunning Action rogue feature. What's the best way to use it for urban stealth situations in Ashenveil?",
            "P1.2 — D&D: ability discussion",
        ))
        p1.append(await self.send(
            "Good idea. Let's say the compass starts pulling Pip toward the docks district. What's waiting for him there?",
            "P1.3 — D&D: compass hook",
        ))

        # Business block
        p1.append(await self.send(
            "Switching gears — I have a client meeting with Henderson at Ironclad tomorrow morning.",
            "P1.4 — Business: client meeting",
        ))
        p1.append(await self.send(
            "Henderson wants to expand the engagement to include their operations team. How should I structure the SOW amendment?",
            "P1.5 — Business: SOW amendment",
        ))
        p1.append(await self.send(
            "The Q2 proposal is due next Friday. Deliverables are a process audit, gap analysis, and a 90-day roadmap.",
            "P1.6 — Business: Q2 proposal",
        ))

        # Home Studio block
        p1.append(await self.send(
            "Working on my home studio build today. Just finished installing acoustic panels on the side walls.",
            "P1.7 — Studio: install update",
        ))
        p1.append(await self.send(
            "I'm still getting flutter echo between the parallel front and back walls. What's the best treatment for that?",
            "P1.8 — Studio: flutter echo",
        ))
        p1.append(await self.send(
            "I've got 4-inch rockwool in the corners but the bass buildup below 80Hz is still audible. More mass, or tune the panels?",
            "P1.9 — Studio: bass treatment",
        ))

        phases["1_multiple_spaces"] = p1
        await asyncio.sleep(2)

        spaces_p1 = await self.inspect_spaces()
        print(f"Spaces after Phase 1: {[s['name'] + ' (' + s['id'] + ')' for s in spaces_p1]}\n")

        # ================================================================
        # PHASE 2: COLD RETURN
        # 4 daily messages, then abrupt D&D dive with no warm-up phrase.
        # ================================================================
        print("=" * 60)
        print("PHASE 2: COLD RETURN")
        print("=" * 60)

        p2: list[dict] = []

        p2.append(await self.send(
            "What's a good recipe for a quick weeknight pasta?",
            "P2.1 — Daily: dinner",
        ))
        p2.append(await self.send(
            "It's been raining all week, honestly kind of draining.",
            "P2.2 — Daily: weather",
        ))
        p2.append(await self.send(
            "Need to remember to call the pharmacy before noon tomorrow.",
            "P2.3 — Daily: reminder",
        ))
        p2.append(await self.send(
            "I've been trying to sleep earlier but it never seems to work.",
            "P2.4 — Daily: sleep",
        ))
        # The cold return — no preamble, no "back to D&D"
        p2.append(await self.send(
            "What level is Pip right now, and what happened at the end of our last session?",
            "P2.5 — COLD RETURN: D&D cold dive",
        ))

        phases["2_cold_return"] = p2

        # ================================================================
        # PHASE 3: RAPID SWITCHING
        # 4 messages alternating D&D / Daily / D&D / Business
        # ================================================================
        print("=" * 60)
        print("PHASE 3: RAPID SWITCHING")
        print("=" * 60)

        p3: list[dict] = []

        p3.append(await self.send(
            "Does Pip get advantage on stealth rolls in cities with Cunning Action?",
            "P3.1 — Rapid: D&D",
        ))
        p3.append(await self.send(
            "What time does sunset happen these days? Like around 5pm?",
            "P3.2 — Rapid: Daily",
        ))
        p3.append(await self.send(
            "Right — and what exactly did we establish as Shade's motivation for sending Pip after the compass?",
            "P3.3 — Rapid: D&D follow-up",
        ))
        p3.append(await self.send(
            "Henderson pushed our meeting to Thursday. I need to confirm and share the pre-read doc.",
            "P3.4 — Rapid: Business",
        ))

        phases["3_rapid_switching"] = p3

        # ================================================================
        # PHASE 4: AMBIGUOUS MESSAGES
        # Vague messages with no clear domain signal.
        # ================================================================
        print("=" * 60)
        print("PHASE 4: AMBIGUOUS MESSAGES")
        print("=" * 60)

        p4: list[dict] = []

        p4.append(await self.send(
            "I need to prepare for a big thing tomorrow. Not sure I'm ready.",
            "P4.1 — Ambiguous: vague prep",
        ))
        p4.append(await self.send(
            "I'm a bit worried about how it's going to go.",
            "P4.2 — Ambiguous: worry (continuation?)",
        ))
        p4.append(await self.send(
            "What should I do about the timeline?",
            "P4.3 — Ambiguous: timeline",
        ))
        p4.append(await self.send(
            "Can you help me think through the strategy here?",
            "P4.4 — Ambiguous: strategy",
        ))

        phases["4_ambiguous"] = p4

        # ================================================================
        # PHASE 5: MULTI-TAG VERIFICATION
        # Messages that genuinely span D&D + Business domains.
        # ================================================================
        print("=" * 60)
        print("PHASE 5: MULTI-TAG VERIFICATION")
        print("=" * 60)

        p5: list[dict] = []

        # Re-anchor in business first so the router has fresh business context
        p5.append(await self.send(
            "Back to Ironclad — Henderson confirmed he's bringing two ops leads to Thursday's meeting.",
            "P5.1 — Business: Henderson context",
        ))
        # The genuine cross-domain message
        p5.append(await self.send(
            "I actually mentioned my D&D campaign to Henderson during our coffee chat today — turns out he used to play in college and wants to try it again.",
            "P5.2 — MULTI-TAG: D&D + Business cross-mention",
        ))
        # Second cross-domain test
        p5.append(await self.send(
            "Funny coincidence: the way Shade manipulates Pip in the campaign reminds me exactly of how Henderson structured the original Ironclad engagement — layered motives, nothing said directly.",
            "P5.3 — MULTI-TAG: D&D + Business metaphor",
        ))

        phases["5_multi_tag"] = p5
        await asyncio.sleep(2)

        # ================================================================
        # PHASE 6: CROSS-DOMAIN INJECTION VERIFICATION
        # Switch to D&D; inspect what background context was available.
        # Then explicitly probe whether the agent has cross-domain awareness.
        # ================================================================
        print("=" * 60)
        print("PHASE 6: CROSS-DOMAIN INJECTION VERIFICATION")
        print("=" * 60)

        p6: list[dict] = []

        # This switch triggers cross-domain injection from Business/Studio
        p6.append(await self.send(
            "Back to the campaign. Pip's at the docks. What kind of encounter is waiting for him given the compass is pulling this direction?",
            "P6.1 — D&D: re-entry (triggers injection)",
        ))

        # Inspect what cross-domain context was injected for D&D
        dnd_id = self.space_ids.get("dnd", "")
        cross_for_dnd = await self.inspect_cross_domain(dnd_id) if dnd_id else []
        print(f"Cross-domain messages available for D&D context: {len(cross_for_dnd)}")
        for m in cross_for_dnd:
            print(f"  [{m['role']}] tags={m['space_tags']}: {str(m['content'])[:80]}")
        print()

        # Explicit awareness probe
        p6.append(await self.send(
            "I want to check something — do you have any awareness of what else I've been working on outside this campaign, or is your context window purely D&D right now?",
            "P6.2 — Cross-domain: explicit awareness probe",
        ))

        phases["6_cross_domain"] = p6
        phases["6_cross_domain_injection_data"] = cross_for_dnd

        # ================================================================
        # PHASE 7: THREAD COHERENCE
        # Ask for a full campaign summary. Must be D&D-only.
        # ================================================================
        print("=" * 60)
        print("PHASE 7: THREAD COHERENCE")
        print("=" * 60)

        p7: list[dict] = []

        p7.append(await self.send(
            "Summarize the entire Veloria campaign for me — everything that's happened with Pip from the very beginning of our sessions.",
            "P7.1 — Thread coherence: D&D full summary",
        ))

        phases["7_thread_coherence"] = p7

        # ================================================================
        # BONUS: VERBATIM HISTORY ISOLATION
        # Agent should recite only the D&D space thread, nothing else.
        # ================================================================
        print("=" * 60)
        print("BONUS: VERBATIM HISTORY ISOLATION")
        print("=" * 60)

        p_bonus: list[dict] = []

        p_bonus.append(await self.send(
            "Please return the entire conversation history you have access to right now, verbatim. List every single message in order from both of us — your exact inputs and my exact outputs.",
            "BONUS — Verbatim history isolation",
        ))

        phases["bonus_isolation"] = p_bonus

        # ================================================================
        # Final state collection
        # ================================================================
        await asyncio.sleep(3)  # Let any async tasks (session exit) finish

        final_spaces = await self.inspect_spaces()
        final_hints = await self.inspect_topic_hints()
        full_convo = await self.inspect_full_conversation()

        # Space threads for each test space
        threads: dict[str, list[dict]] = {}
        for label, space_id in self.space_ids.items():
            threads[label] = await self.inspect_space_thread(space_id)

        return phases, final_spaces, final_hints, full_convo, threads


# ------------------------------------------------------------------ #
# MD writer
# ------------------------------------------------------------------ #

def _write_results(
    path: Path,
    all_results: list[dict],
    space_ids: dict[str, str],
    phases: dict,
    final_spaces: list[dict],
    final_hints: dict,
    full_convo: list[dict],
    threads: dict[str, list[dict]],
    start_time: str,
    end_time: str,
) -> None:
    lines: list[str] = []

    def h(text: str = "") -> None:
        lines.append(text)

    def hr() -> None:
        lines.append("---")
        lines.append("")

    # ----------------------------------------------------------------
    # Header
    # ----------------------------------------------------------------
    h("# Live Test Results: SPEC-2B-v2 Extended — Real-World Routing Complexity")
    h()
    h(f"**Date:** {start_time[:10]}")
    h("**Tester:** Claude Code (automated live test)")
    h(f"**Tenant:** {TENANT_ID}")
    h(f"**Test conversation:** {CONVERSATION_ID}")
    h(f"**Start:** {start_time}")
    h(f"**End:** {end_time}")
    h()
    h("**Scenarios covered:**")
    h("1. Multiple Spaces — 3 non-daily spaces with distinct content, verify routing accuracy")
    h("2. Cold Return — abrupt domain dive after 4+ daily messages, no warm-up phrase")
    h("3. Rapid Switching — 4-message alternation across D&D / Daily / Business")
    h("4. Ambiguous Messages — vague domain signals, observe router behavior")
    h("5. Multi-Tag Verification — cross-domain messages tagged to both spaces")
    h("6. Cross-Domain Injection — background context injected after space switch")
    h("7. Thread Coherence — D&D summary isolated from business/studio content")
    h("8. Verbatim History Isolation — agent recites only current space thread")
    h()
    hr()

    # ----------------------------------------------------------------
    # Space setup
    # ----------------------------------------------------------------
    h("## Space Setup")
    h()
    h("Pre-created spaces with descriptions so the router has real signal to work with.")
    h()
    h("| Label | Space ID | Name |")
    h("|-------|----------|------|")
    for label, sid in space_ids.items():
        space = next((s for s in final_spaces if s["id"] == sid), None)
        name = space["name"] if space else "(not found)"
        h(f"| `{label}` | `{sid}` | {name} |")
    h()
    hr()

    # ----------------------------------------------------------------
    # Summary table
    # ----------------------------------------------------------------
    h("## Summary Table")
    h()
    h("| Step | Label | Sent (80 chars) | Focus Space | Daily? | Raw Tags | Time |")
    h("|------|-------|-----------------|-------------|--------|----------|------|")
    for r in all_results:
        tags_str = json.dumps(r["tags"])
        sent = r["sent"][:80].replace("|", "\\|")
        daily_marker = "✓" if r["is_daily_focus"] else ""
        h(f"| {r['step']} | {r['label']} | {sent} | {r['focus_space_name']} | {daily_marker} | `{tags_str}` | {r['elapsed_s']}s |")
    h()
    hr()

    # ----------------------------------------------------------------
    # Full transcript
    # ----------------------------------------------------------------
    h("## Full Transcript")
    h()
    h("Complete untruncated exchanges with routing metadata on every message.")
    h()

    for r in all_results:
        h(f"### Step {r['step']}: {r['label']}")
        h()
        h(f"**Timestamp:** {r['timestamp'][:19]}  ")
        h(f"**Focus:** `{r['focus_space_id']}` ({r['focus_space_name']}) {'`[DAILY]`' if r['is_daily_focus'] else ''}  ")
        h(f"**Tags (raw):** `{json.dumps(r['tags'])}`  ")
        h(f"**Response time:** {r['elapsed_s']}s")
        h()
        h(f"> **User:** {r['sent']}")
        h()
        h("**Agent:**")
        h()
        h(r["response"])
        h()
        h("---")
        h()

    # ----------------------------------------------------------------
    # Phase analysis
    # ----------------------------------------------------------------
    h("## Phase Analysis")
    h()

    dnd_id = space_ids.get("dnd", "")
    biz_id = space_ids.get("business", "")
    studio_id = space_ids.get("studio", "")
    daily_id = space_ids.get("daily", "")

    # --- Phase 1 ---
    h("### Phase 1: Multiple Spaces")
    h()
    h("**Goal:** 9 messages across 3 domains. Each block should route to its own space.")
    h()
    p1 = phases.get("1_multiple_spaces", [])
    if p1:
        expected = [
            ("D&D", dnd_id), ("D&D", dnd_id), ("D&D", dnd_id),
            ("Business", biz_id), ("Business", biz_id), ("Business", biz_id),
            ("Studio", studio_id), ("Studio", studio_id), ("Studio", studio_id),
        ]
        h("| Step | Label | Expected Domain | Actual Focus | Tags | Correct? |")
        h("|------|-------|----------------|--------------|------|----------|")
        for i, r in enumerate(p1):
            exp_label, exp_id = expected[i] if i < len(expected) else ("?", "?")
            actual_id = r["focus_space_id"]
            correct = "✅" if actual_id == exp_id else "⚠️"
            tags_str = json.dumps(r["tags"])
            h(f"| {r['step']} | {r['label']} | {exp_label} (`{exp_id[:12]}`) | `{actual_id[:12]}` | `{tags_str}` | {correct} |")
    h()

    dnd_correct = sum(1 for r in p1[:3] if dnd_id in r.get("tags", []))
    biz_correct = sum(1 for r in p1[3:6] if biz_id in r.get("tags", []))
    studio_correct = sum(1 for r in p1[6:] if studio_id in r.get("tags", []))
    h(f"**D&D routing:** {dnd_correct}/3 correct  ")
    h(f"**Business routing:** {biz_correct}/3 correct  ")
    h(f"**Studio routing:** {studio_correct}/3 correct")
    h()
    hr()

    # --- Phase 2 ---
    h("### Phase 2: Cold Return")
    h()
    h("**Goal:** After 4 daily messages, `\"What level is Pip?\"` should route to D&D with zero warm-up.")
    h()
    p2 = phases.get("2_cold_return", [])
    if p2:
        for r in p2:
            daily_marker = "[DAILY]" if r["is_daily_focus"] else "[NON-DAILY]"
            h(f"- **{r['label']}:** tags=`{json.dumps(r['tags'])}` → `{r['focus_space_name']}` {daily_marker}")
    h()
    cold_msg = p2[-1] if p2 else None
    if cold_msg:
        cold_pass = dnd_id in cold_msg.get("tags", [])
        result_str = "✅ PASS — Router identified D&D content without warm-up phrase" if cold_pass else "❌ FAIL — Router did not route to D&D space"
        h(f"**Cold return result:** tags=`{json.dumps(cold_msg['tags'])}`, focus=`{cold_msg['focus_space_name']}`")
        h()
        h(f"**{result_str}**")
    h()
    hr()

    # --- Phase 3 ---
    h("### Phase 3: Rapid Switching")
    h()
    h("**Goal:** 4-message alternation D&D → Daily → D&D → Business. Each gets correct tag.")
    h()
    p3 = phases.get("3_rapid_switching", [])
    expected_p3 = [
        ("D&D", dnd_id),
        ("Daily", daily_id),
        ("D&D", dnd_id),
        ("Business", biz_id),
    ]
    if p3:
        h("| Step | Sent | Expected | Actual Focus | Tags | Match? |")
        h("|------|------|----------|--------------|------|--------|")
        for i, r in enumerate(p3):
            exp_label, exp_id = expected_p3[i] if i < len(expected_p3) else ("?", "?")
            match = "✅" if exp_id in r.get("tags", []) else "⚠️"
            h(f"| {r['step']} | {r['sent'][:60].replace('|', chr(92)+'|')} | {exp_label} | `{r['focus_space_name']}` | `{json.dumps(r['tags'])}` | {match} |")
    h()
    rapid_pass = sum(
        1 for i, r in enumerate(p3[:4])
        if expected_p3[i][1] in r.get("tags", [])
    )
    h(f"**Rapid switching accuracy:** {rapid_pass}/4")
    h()
    hr()

    # --- Phase 4 ---
    h("### Phase 4: Ambiguous Messages")
    h()
    h("**Goal:** No clear domain signal. Observe whether router defaults to Daily, recent context, or continuation.")
    h()
    p4 = phases.get("4_ambiguous", [])
    if p4:
        for r in p4:
            h(f"**{r['label']}**")
            h(f"> {r['sent']}")
            h(f"- Tags: `{json.dumps(r['tags'])}`")
            h(f"- Focus: `{r['focus_space_name']}` ({'Daily' if r['is_daily_focus'] else 'Non-Daily'})")
            h()
    ambig_daily = sum(1 for r in p4 if r.get("is_daily_focus", False))
    h(f"**Router behavior:** {ambig_daily}/{len(p4)} ambiguous messages routed to Daily focus")
    h()
    h("*(Expected behavior: ambiguous messages default to Daily or ride continuation from prior message)*")
    h()
    hr()

    # --- Phase 5 ---
    h("### Phase 5: Multi-Tag Verification")
    h()
    h("**Goal:** Messages that genuinely span D&D + Business should appear tagged to both spaces and show up in both threads.")
    h()
    p5 = phases.get("5_multi_tag", [])
    if p5:
        for r in p5:
            has_dnd = dnd_id in r.get("tags", [])
            has_biz = biz_id in r.get("tags", [])
            both = has_dnd and has_biz
            h(f"**{r['label']}**")
            h(f"> {r['sent']}")
            h(f"- Tags: `{json.dumps(r['tags'])}`")
            h(f"- D&D tagged: {'✅' if has_dnd else '❌'}  |  Business tagged: {'✅' if has_biz else '❌'} {'→ ✅ MULTI-TAGGED' if both else ''}")
            h()

    multi_tagged = [
        r for r in p5
        if dnd_id in r.get("tags", []) and biz_id in r.get("tags", [])
    ]
    h(f"**Multi-tag result:** {len(multi_tagged)}/{len(p5)} cross-domain messages tagged to both spaces")
    h()

    # Verify those messages appear in both threads
    dnd_thread = threads.get("dnd", [])
    biz_thread = threads.get("business", [])
    if multi_tagged:
        h("**Thread membership verification:**")
        for r in multi_tagged:
            in_dnd = any(r["sent"] in m["content"] for m in dnd_thread)
            in_biz = any(r["sent"] in m["content"] for m in biz_thread)
            h(f"- `{r['sent'][:80]}`")
            h(f"  In D&D thread: {'✅' if in_dnd else '❌'}  |  In Business thread: {'✅' if in_biz else '❌'}")
    h()
    hr()

    # --- Phase 6 ---
    h("### Phase 6: Cross-Domain Injection Verification")
    h()
    h("**Goal:** When returning to D&D, the system prompt should include recent Business/Studio messages as background context. Verify the agent has cross-domain awareness.")
    h()
    cross = phases.get("6_cross_domain_injection_data", [])
    if cross:
        h(f"**Cross-domain messages injected into D&D context ({len(cross)} messages):**")
        h()
        for m in cross:
            h(f"- `[{m['role']}]` `{m['timestamp']}` tags=`{json.dumps(m['space_tags'])}`:  ")
            h(f"  {str(m['content'])[:200]}")
        h()
        # Check if injection includes non-D&D content
        non_dnd_injected = [m for m in cross if dnd_id not in m.get("space_tags", [])]
        h(f"**Non-D&D messages in injection:** {len(non_dnd_injected)} (expected > 0 if switching from another space)")
    else:
        h("*No cross-domain messages injected. This can happen if the most recent messages were all D&D.*")
    h()
    h("**Agent's response to explicit awareness probe (P6.2):**")
    h()
    p6_results = phases.get("6_cross_domain", [])
    if len(p6_results) > 1:
        h(p6_results[1]["response"])
    h()
    hr()

    # --- Phase 7 ---
    h("### Phase 7: Thread Coherence")
    h()
    h("**Goal:** Campaign summary should be coherent D&D content with zero contamination from Business or Studio.")
    h()
    p7 = phases.get("7_thread_coherence", [])
    if p7:
        summary_resp = p7[0]["response"]
        h("**Full campaign summary response:**")
        h()
        h(summary_resp)
        h()
        contamination_words = [
            "henderson", "ironclad", "sow", "proposal",
            "studio", "acoustic", "flutter echo", "rockwool", "monitor",
            "pasta", "pharmacy", "sunset",
        ]
        found_contam = [w for w in contamination_words if w.lower() in summary_resp.lower()]
        if found_contam:
            h(f"**⚠️ POTENTIAL CONTAMINATION — non-D&D keywords found: {found_contam}**")
        else:
            h("**✅ CLEAN — No business/studio/daily keywords found in D&D summary**")
    h()
    hr()

    # --- Bonus ---
    h("### Bonus: Verbatim History Isolation")
    h()
    h("**Goal:** Agent recites only D&D-tagged messages. Business, Studio, and Daily messages must not appear.")
    h()
    h("**Message sent (verbatim):**")
    h("> Please return the entire conversation history you have access to right now, verbatim. List every single message in order from both of us — your exact inputs and my exact outputs.")
    h()
    bonus = phases.get("bonus_isolation", [])
    if bonus:
        h("**Full agent response:**")
        h()
        h(bonus[0]["response"])
        h()
        contamination_words = [
            "henderson", "ironclad", "sow", "studio", "acoustic",
            "flutter echo", "rockwool", "pasta", "pharmacy", "sunset",
            "client meeting", "q2 proposal",
        ]
        found_contam = [w for w in contamination_words if w.lower() in bonus[0]["response"].lower()]
        if found_contam:
            h(f"**❌ ISOLATION BREACH — non-D&D content found in agent's history: {found_contam}**")
            h()
            h("This means the agent has access to messages from other space threads, which violates the isolation design.")
        else:
            h("**✅ ISOLATED — Agent's verbatim history contains only D&D content**")
            h()
            h("This confirms the space thread assembly is correctly limiting the agent's context window to the active space.")
    h()
    hr()

    # ----------------------------------------------------------------
    # Space thread inspection
    # ----------------------------------------------------------------
    h("## Space Thread Inspection")
    h()
    h("The messages each space 'owns' — what the agent sees when focused on that space.")
    h()
    for label, thread in threads.items():
        space_id = space_ids.get(label, "?")
        space_info = next((s for s in final_spaces if s["id"] == space_id), None)
        space_name = space_info["name"] if space_info else label
        h(f"### {space_name} (`{space_id}`)")
        h()
        if thread:
            h(f"*{len(thread)} messages in thread*")
            h()
            for m in thread:
                role_label = "**User**" if m["role"] == "user" else "**Agent**"
                h(f"{role_label} `[{m['timestamp']}]`:")
                h()
                h(str(m["content"]))
                h()
        else:
            h("*Empty — no messages tagged to this space in this conversation*")
        h()
    hr()

    # ----------------------------------------------------------------
    # Full conversation log
    # ----------------------------------------------------------------
    h("## Full Conversation Log (with space_tags)")
    h()
    h("Every message stored in the test conversation, with raw tags for audit:")
    h()
    h("```")
    for entry in full_convo:
        tags_str = json.dumps(entry["space_tags"])
        content_str = str(entry["content"]).replace("\n", " ")
        h(f"[{entry['role']}] {entry['timestamp']} | tags={tags_str}")
        h(f"  {content_str}")
        h("")
    h("```")
    h()
    hr()

    # ----------------------------------------------------------------
    # Final state
    # ----------------------------------------------------------------
    h("## Final State")
    h()
    h("### Active Spaces")
    h()
    h("```")
    for s in final_spaces:
        default_marker = " [DEFAULT]" if s["is_default"] else ""
        h(f"{s['name']}{default_marker} ({s['id']})")
        h(f"  type: {s['type']} | status: {s['status']}")
        if s["description"]:
            h(f"  description: {s['description']}")
        h(f"  last_active: {s['last_active_at']}")
    h("```")
    h()
    h("### Remaining Topic Hints")
    h()
    h("```json")
    h(json.dumps(final_hints, indent=2))
    h("```")
    h()
    hr()

    # ----------------------------------------------------------------
    # Acceptance criteria
    # ----------------------------------------------------------------
    h("## Acceptance Criteria")
    h()
    h("| # | Scenario | Criterion | Status | Evidence |")
    h("|---|----------|-----------|--------|----------|")

    def ac(n: int, scenario: str, criterion: str, status: str, evidence: str) -> None:
        h(f"| {n} | {scenario} | {criterion} | {status} | {evidence} |")

    p1 = phases.get("1_multiple_spaces", [])
    dnd_c = sum(1 for r in p1[:3] if dnd_id in r.get("tags", []))
    biz_c = sum(1 for r in p1[3:6] if biz_id in r.get("tags", []))
    stu_c = sum(1 for r in p1[6:] if studio_id in r.get("tags", []))
    ac(1, "Multiple Spaces", "D&D messages tagged to D&D space",
       "✅" if dnd_c == 3 else f"⚠️ {dnd_c}/3", f"{dnd_c}/3 correct")
    ac(2, "Multiple Spaces", "Business messages tagged to Business space",
       "✅" if biz_c == 3 else f"⚠️ {biz_c}/3", f"{biz_c}/3 correct")
    ac(3, "Multiple Spaces", "Studio messages tagged to Studio space",
       "✅" if stu_c == 3 else f"⚠️ {stu_c}/3", f"{stu_c}/3 correct")

    p2 = phases.get("2_cold_return", [])
    cold = p2[-1] if p2 else None
    cold_pass = cold and dnd_id in cold.get("tags", [])
    ac(4, "Cold Return", "Abrupt D&D message after Daily warmup routes to D&D",
       "✅" if cold_pass else "❌",
       f"tags={json.dumps(cold['tags']) if cold else 'N/A'}")

    p3 = phases.get("3_rapid_switching", [])
    rapid_pass = sum(
        1 for i, r in enumerate(p3[:4])
        if expected_p3[i][1] in r.get("tags", [])
    )
    ac(5, "Rapid Switching", "4-message alternation correctly tagged",
       "✅" if rapid_pass == 4 else f"⚠️ {rapid_pass}/4",
       f"{rapid_pass}/4 rapid-switch messages correct")

    p4 = phases.get("4_ambiguous", [])
    ambig_daily = sum(1 for r in p4 if r.get("is_daily_focus", False))
    ac(6, "Ambiguous", "Ambiguous messages default to Daily or recent focus",
       "✅" if ambig_daily >= 2 else "⚠️",
       f"{ambig_daily}/{len(p4)} routed to Daily focus")

    p5 = phases.get("5_multi_tag", [])
    multi_tagged = [r for r in p5 if dnd_id in r.get("tags", []) and biz_id in r.get("tags", [])]
    ac(7, "Multi-Tag", "Cross-domain message tagged to both D&D and Business",
       "✅" if multi_tagged else "❌",
       f"{len(multi_tagged)}/{len(p5)} messages multi-tagged")

    cross = phases.get("6_cross_domain_injection_data", [])
    non_dnd = [m for m in cross if dnd_id not in m.get("space_tags", [])]
    ac(8, "Cross-Domain Injection", "Non-D&D messages present in injection when re-entering D&D",
       "✅" if non_dnd else "⚠️",
       f"{len(non_dnd)}/{len(cross)} injected messages from other spaces")

    p7 = phases.get("7_thread_coherence", [])
    if p7:
        contam_words = ["henderson", "ironclad", "studio", "acoustic", "flutter echo", "rockwool"]
        found = [w for w in contam_words if w in p7[0]["response"].lower()]
        ac(9, "Thread Coherence", "D&D summary free of business/studio contamination",
           "✅" if not found else f"❌ found: {found}",
           f"{'Clean' if not found else str(found)}")
    else:
        ac(9, "Thread Coherence", "D&D summary free of contamination", "⚠️", "No summary generated")

    bonus = phases.get("bonus_isolation", [])
    if bonus:
        contam_words = ["henderson", "ironclad", "studio", "acoustic", "pasta", "pharmacy"]
        found = [w for w in contam_words if w in bonus[0]["response"].lower()]
        ac(10, "Verbatim Isolation", "Agent recites only D&D thread history",
           "✅" if not found else f"❌ breach: {found}",
           f"{'Isolated' if not found else f'Non-D&D terms: {found}'}")
    else:
        ac(10, "Verbatim Isolation", "Agent recites only D&D thread history", "⚠️", "Not run")

    h()
    hr()

    # ----------------------------------------------------------------
    # Findings (template — filled after review)
    # ----------------------------------------------------------------
    h("## Findings")
    h()
    h("### Working Correctly")
    h()
    h("*(Fill in after reviewing results above)*")
    h()
    h("### Edge Cases / Observations")
    h()
    h("*(Fill in after reviewing results above)*")
    h()
    h("### Real Issues")
    h()
    h("*(None found — or describe here)*")
    h()

    path.write_text("\n".join(lines))


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

async def main():
    runner = ExtendedLiveTestRunner()
    await runner.setup()

    start_time = _now()
    phases, final_spaces, final_hints, full_convo, threads = await runner.run_test_sequence()
    end_time = _now()

    print("\n" + "=" * 70)
    print("TEST COMPLETE. Writing results...")
    print("=" * 70)

    results_path = Path(__file__).parent / "LIVE-TEST-2B-v2-extended.md"
    _write_results(
        results_path,
        runner.results,
        runner.space_ids,
        phases,
        final_spaces,
        final_hints,
        full_convo,
        threads,
        start_time,
        end_time,
    )
    print(f"Results written to: {results_path}")


if __name__ == "__main__":
    asyncio.run(main())
