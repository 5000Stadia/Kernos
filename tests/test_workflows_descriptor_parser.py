"""Tests for the portable workflow descriptor parser.

WORKFLOW-LOOP-PRIMITIVE C3. Pins the three loaders (YAML, JSON,
Markdown-with-frontmatter), schema-against-dataclass validation,
and the sharing-constraint enforcement at parse time.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from kernos.kernel.workflows.descriptor_parser import (
    DescriptorError,
    parse_descriptor,
)


def _yaml_workflow_text() -> str:
    return textwrap.dedent("""
        workflow_id: morning-briefing
        instance_id: inst_a
        name: Morning briefing
        version: "1.0"
        owner: owner
        bounds:
          iteration_count: 1
          wall_time_seconds: 30
        verifier:
          flavor: deterministic
          check: briefing_delivered
        action_sequence:
          - action_type: notify_user
            parameters:
              urgency: low
        trigger:
          event_type: time.tick
          predicate:
            op: AND
            operands:
              - op: eq
                path: payload.cadence
                value: daily
              - op: eq
                path: payload.local_time
                value: "08:00"
    """).lstrip()


def _json_workflow_text() -> str:
    return json.dumps({
        "workflow_id": "morning-briefing",
        "instance_id": "inst_a",
        "name": "Morning briefing",
        "version": "1.0",
        "owner": "owner",
        "bounds": {"iteration_count": 1, "wall_time_seconds": 30},
        "verifier": {"flavor": "deterministic", "check": "briefing_delivered"},
        "action_sequence": [
            {"action_type": "notify_user", "parameters": {"urgency": "low"}},
        ],
        "trigger": {
            "event_type": "time.tick",
            "predicate": {
                "op": "eq",
                "path": "payload.cadence",
                "value": "daily",
            },
        },
    })


def _markdown_workflow_text() -> str:
    return textwrap.dedent("""
        ---
        workflow_id: morning-briefing
        instance_id: inst_a
        name: Morning briefing
        version: "1.0"
        owner: owner
        bounds:
          iteration_count: 1
          wall_time_seconds: 30
        verifier:
          flavor: deterministic
          check: briefing_delivered
        action_sequence:
          - action_type: notify_user
            parameters:
              urgency: low
        trigger:
          event_type: time.tick
          predicate:
            op: eq
            path: payload.cadence
            value: daily
        ---

        # Morning briefing

        Fires daily at 8am local time. Synthesizes overnight events
        into a brief summary delivered through the primary channel.
    """).lstrip()


class TestYamlLoader:
    def test_parse_yaml(self, tmp_path):
        path = tmp_path / "morning.workflow.yaml"
        path.write_text(_yaml_workflow_text())
        wf = parse_descriptor(path)
        assert wf.workflow_id == "morning-briefing"
        assert wf.name == "Morning briefing"
        assert wf.version == "1.0"
        assert wf.bounds.iteration_count == 1
        assert wf.bounds.wall_time_seconds == 30
        assert wf.verifier.flavor == "deterministic"
        assert len(wf.action_sequence) == 1
        assert wf.action_sequence[0].action_type == "notify_user"
        assert wf.trigger is not None
        assert wf.trigger.event_type == "time.tick"
        assert wf.trigger.predicate["op"] == "AND"

    def test_yml_extension_also_supported(self, tmp_path):
        path = tmp_path / "wf.workflow.yml"
        path.write_text(_yaml_workflow_text())
        wf = parse_descriptor(path)
        assert wf.name == "Morning briefing"

    def test_yaml_parse_error(self, tmp_path):
        path = tmp_path / "broken.workflow.yaml"
        # Mismatched flow-mapping is unambiguously malformed YAML.
        path.write_text("name: {foo: bar, baz")
        with pytest.raises(DescriptorError, match="YAML"):
            parse_descriptor(path)


class TestJsonLoader:
    def test_parse_json(self, tmp_path):
        path = tmp_path / "wf.workflow.json"
        path.write_text(_json_workflow_text())
        wf = parse_descriptor(path)
        assert wf.workflow_id == "morning-briefing"
        assert wf.trigger.predicate["op"] == "eq"

    def test_json_parse_error(self, tmp_path):
        path = tmp_path / "broken.workflow.json"
        path.write_text("not-json")
        with pytest.raises(DescriptorError, match="JSON"):
            parse_descriptor(path)


class TestMarkdownLoader:
    def test_parse_markdown_with_frontmatter(self, tmp_path):
        path = tmp_path / "wf.workflow.md"
        path.write_text(_markdown_workflow_text())
        wf = parse_descriptor(path)
        assert wf.workflow_id == "morning-briefing"
        assert "Fires daily" in wf.description
        assert wf.bounds.iteration_count == 1

    def test_markdown_missing_opening_delimiter(self, tmp_path):
        path = tmp_path / "no-delim.workflow.md"
        path.write_text("# Just markdown\n\nNo frontmatter.\n")
        with pytest.raises(DescriptorError, match="frontmatter"):
            parse_descriptor(path)

    def test_markdown_missing_closing_delimiter(self, tmp_path):
        path = tmp_path / "open.workflow.md"
        path.write_text("---\nname: x\n# no closing delim\n")
        with pytest.raises(DescriptorError, match="closing"):
            parse_descriptor(path)


class TestUnsupportedExtension:
    def test_txt_extension_rejected(self, tmp_path):
        path = tmp_path / "wf.txt"
        path.write_text("anything")
        with pytest.raises(DescriptorError, match="extension"):
            parse_descriptor(path)


class TestSharingConstraint:
    """Instance-specific values must be parameterised or guarded by
    instance_local: true."""

    def _yaml_with_concrete_member_id(self) -> str:
        return textwrap.dedent("""
            workflow_id: x
            instance_id: inst_a
            name: x
            version: "1.0"
            bounds:
              iteration_count: 1
            verifier:
              flavor: deterministic
              check: x
            action_sequence:
              - action_type: notify_user
                parameters:
                  channel_id: discord-1234
            trigger:
              event_type: cc.batch.report
              predicate:
                op: exists
                path: event_id
        """).lstrip()

    def test_concrete_channel_id_rejected_when_shareable(self, tmp_path):
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(self._yaml_with_concrete_member_id())
        with pytest.raises(DescriptorError, match="instance-specific"):
            parse_descriptor(path)

    def test_parameterised_channel_id_accepted(self, tmp_path):
        # Quote the placeholder so YAML treats it as a string rather
        # than a flow mapping.
        text = self._yaml_with_concrete_member_id().replace(
            "discord-1234", '"{installer.channel_id}"',
        )
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        wf = parse_descriptor(path)
        assert (
            wf.action_sequence[0].parameters["channel_id"]
            == "{installer.channel_id}"
        )

    def test_instance_local_true_disables_sharing_check(self, tmp_path):
        text = (
            "instance_local: true\n"
            + self._yaml_with_concrete_member_id()
        )
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        wf = parse_descriptor(path)
        assert wf.instance_local is True

    def test_concrete_actor_filter_rejected(self, tmp_path):
        text = textwrap.dedent("""
            workflow_id: x
            instance_id: inst_a
            name: x
            version: "1.0"
            bounds:
              iteration_count: 1
            verifier:
              flavor: deterministic
              check: x
            action_sequence:
              - action_type: mark_state
                parameters:
                  key: x
                  value: 1
                  scope: instance
            trigger:
              event_type: cc.batch.report
              predicate:
                op: exists
                path: event_id
              actor_filter: mem_specific
        """).lstrip()
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        with pytest.raises(DescriptorError, match="instance-specific"):
            parse_descriptor(path)

    def test_concrete_member_id_in_predicate_rejected(self, tmp_path):
        text = textwrap.dedent("""
            workflow_id: x
            instance_id: inst_a
            name: x
            version: "1.0"
            bounds:
              iteration_count: 1
            verifier:
              flavor: deterministic
              check: x
            action_sequence:
              - action_type: mark_state
                parameters: {key: x, value: 1, scope: instance}
            approval_gates:
              - gate_name: g1
                pause_reason: x
                approval_event_type: user.approval
                approval_event_predicate:
                  op: actor_eq
                  value: mem_concrete
                timeout_seconds: 60
                bound_behavior_on_timeout: abort_workflow
            trigger:
              event_type: cc.batch.report
              predicate:
                op: exists
                path: event_id
        """).lstrip()
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        with pytest.raises(DescriptorError, match="instance-specific"):
            parse_descriptor(path)


class TestExpressionStringDSL:
    """C6 wires the descriptor parser to compile DSL predicates via
    trigger_compiler. The DSL form is a deterministic parse — no LLM."""

    def test_dsl_predicate_compiles_to_ast(self, tmp_path):
        text = textwrap.dedent("""
            workflow_id: x
            instance_id: inst_a
            name: x
            version: "1.0"
            bounds:
              iteration_count: 1
            verifier:
              flavor: deterministic
              check: x
            action_sequence:
              - action_type: mark_state
                parameters: {key: x, value: 1, scope: instance}
            trigger:
              event_type: cc.batch.report
              predicate: 'event.payload.kind == "report"'
        """).lstrip()
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        wf = parse_descriptor(path)
        assert wf.trigger.predicate == {
            "op": "eq", "path": "payload.kind", "value": "report",
        }

    def test_unrecognised_dsl_raises_descriptor_error(self, tmp_path):
        text = textwrap.dedent("""
            workflow_id: x
            instance_id: inst_a
            name: x
            version: "1.0"
            bounds:
              iteration_count: 1
            verifier:
              flavor: deterministic
              check: x
            action_sequence:
              - action_type: mark_state
                parameters: {key: x, value: 1, scope: instance}
            trigger:
              event_type: cc.batch.report
              predicate: 'this is plain English, not DSL'
        """).lstrip()
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        with pytest.raises(DescriptorError, match="DSL"):
            parse_descriptor(path)


class TestMissingFile:
    def test_missing_file(self, tmp_path):
        with pytest.raises(DescriptorError, match="not found"):
            parse_descriptor(tmp_path / "nope.workflow.yaml")


class TestPluralTriggers:
    """SELF-IMPROVEMENT-WORKFLOW-V1 12th amendment (Spec 6 v6.2).

    `_build_workflow` accepts either singular `trigger:` (Spec 4 legacy) or
    plural `triggers:` (production WTC path). Mixing both is rejected.
    Plural triggers leave `Workflow.trigger = None`; the descriptor.triggers
    list is compiled by WTC at production-wiring time.
    """

    def _plural_yaml(self) -> str:
        return textwrap.dedent("""
            workflow_id: self-improve
            instance_id: inst_a
            name: self_improve
            version: "1"
            owner: owner
            instance_local: true
            bounds:
              wall_time_seconds: 30
            verifier:
              flavor: deterministic
              check: terminated
            action_sequence:
              - action_type: mark_state
                parameters: {key: x, value: 1, scope: instance}
            triggers:
              - event_type: friction.pattern_frequency_threshold_exceeded
                event_selector:
                  op: AND
                  operands:
                    - {op: eq, path: instance_id, value: inst_a}
                    - {op: exists, path: payload.pattern_id}
        """).lstrip()

    def test_plural_triggers_accepted(self, tmp_path):
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(self._plural_yaml())
        wf = parse_descriptor(path)
        assert wf.trigger is None
        assert wf.workflow_id == "self-improve"

    def test_singular_trigger_still_accepted(self, tmp_path):
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(_yaml_workflow_text())
        wf = parse_descriptor(path)
        assert wf.trigger is not None
        assert wf.trigger.event_type == "time.tick"

    def test_no_trigger_accepted(self, tmp_path):
        text = textwrap.dedent("""
            workflow_id: notrig
            instance_id: inst_a
            name: notrig
            version: "1"
            owner: owner
            instance_local: true
            bounds:
              wall_time_seconds: 30
            verifier:
              flavor: deterministic
              check: terminated
            action_sequence:
              - action_type: mark_state
                parameters: {key: x, value: 1, scope: instance}
        """).lstrip()
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        wf = parse_descriptor(path)
        assert wf.trigger is None

    def test_mixing_singular_and_plural_rejected(self, tmp_path):
        text = textwrap.dedent("""
            workflow_id: mix
            instance_id: inst_a
            name: mix
            version: "1"
            owner: owner
            instance_local: true
            bounds:
              wall_time_seconds: 30
            verifier:
              flavor: deterministic
              check: terminated
            action_sequence:
              - action_type: mark_state
                parameters: {key: x, value: 1, scope: instance}
            trigger:
              event_type: time.tick
              predicate: {op: eq, path: payload.cadence, value: daily}
            triggers:
              - event_type: friction.pattern_frequency_threshold_exceeded
                event_selector: {op: eq, path: event_type, value: friction.pattern_frequency_threshold_exceeded}
        """).lstrip()
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        with pytest.raises(DescriptorError, match="both 'trigger'"):
            parse_descriptor(path)

    def test_triggers_must_be_list(self, tmp_path):
        text = textwrap.dedent("""
            workflow_id: notlist
            instance_id: inst_a
            name: notlist
            version: "1"
            owner: owner
            instance_local: true
            bounds:
              wall_time_seconds: 30
            verifier:
              flavor: deterministic
              check: terminated
            action_sequence:
              - action_type: mark_state
                parameters: {key: x, value: 1, scope: instance}
            triggers:
              event_type: oops_a_dict_not_a_list
        """).lstrip()
        path = tmp_path / "wf.workflow.yaml"
        path.write_text(text)
        with pytest.raises(DescriptorError, match="triggers must be a list"):
            parse_descriptor(path)
