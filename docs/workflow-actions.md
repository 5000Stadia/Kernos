# Workflow Actions

## `request_approval`

Use `request_approval` with a `gate_ref` and a matching
`approval_gates` entry. The action creates the durable approval
receipt; the workflow engine pauses on the existing gate mechanism
and resumes on `approval.decision_recorded`.

```yaml
action_sequence:
  - id: request_op_approval
    action_type: request_approval
    parameters:
      kind: git_commit_authorization
      operator_actor_id: "{idea_payload.operator_actor_id}"
      request_summary: "Commit ready: {step.draft_spec.value.spec_summary}"
      binding_payload:
        expected_parent_sha: "{step.snapshot.value.head_sha}"
      ttl_seconds: 86400
      single_use: true
      _workflow_execution_id: "{workflow.execution_id}"
      _gate_nonce: "{workflow.gate_nonce}"
    gate_ref: await_op_approval

  - id: branch_on_approval
    action_type: branch
    parameters:
      condition: "{step.request_op_approval.approval_outcome.approved}"
      branch_on_true: do_commit
      branch_on_false: surface_rejection

approval_gates:
  - gate_name: await_op_approval
    approval_event_type: approval.decision_recorded
    approval_event_predicate:
      op: eq
      path: payload.approval_id
      value: "{step.request_op_approval.value.approval_id}"
    timeout_seconds: 86400
    bound_behavior_on_timeout: abort_workflow
```

Downstream steps can read
`{step.request_op_approval.approval_outcome.decision}` for
`approved`, `rejected`, or `expired`.
