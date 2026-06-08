"""③ ownership-aware redaction (2026-06-08): you always have full permission to
your OWN information. The cross-member redaction invariant blanket-redacted the
covenant cohort's output — but that cohort member-filters to the recipient, so
its content is the recipient's own rules, never another member's. Surfacing your
own rules back to you bricked context-routing turns. The redaction check now
exempts a Restricted CohortOutput whose owner_member_id == the recipient.
"""
import pytest

from kernos.kernel.integration.briefing import CohortOutput, Restricted, Public

# Reuse the runner test harness factory.
from tests.test_integration_runner import _make_runner

SECRET = "the-user-has-a-rule-do-not-message-after-10pm-this-is-restricted"


def _runner():
    runner, _ = _make_runner()
    return runner


def _restricted(owner: str) -> CohortOutput:
    return CohortOutput(
        cohort_id="covenant",
        cohort_run_id="cr-1",
        output={"rules": [SECRET]},
        visibility=Restricted(reason="covenant_set"),
        owner_member_id=owner,
    )


def test_cohort_output_accepts_owner_member_id():
    co = _restricted("mem_abc")
    assert co.owner_member_id == "mem_abc"
    assert CohortOutput(cohort_id="c", cohort_run_id="r",
                        output={}).owner_member_id == ""  # default


def test_own_content_quoted_to_owner_is_exempt():
    # recipient == owner: quoting their own rule into the directive is NOT a
    # leak — must not raise.
    r = _runner()
    r._check_redaction_invariant(
        relevant=(), filtered=(), directive=f"Be mindful: {SECRET}",
        cohort_outputs=(_restricted("mem_self"),),
        requesting_member_id="mem_self",
    )  # no exception = pass


def test_other_members_content_still_redacted():
    # owner != recipient: genuine cross-member leak → must raise.
    from kernos.kernel.integration.briefing import BriefingValidationError
    r = _runner()
    with pytest.raises(BriefingValidationError):
        r._check_redaction_invariant(
            relevant=(), filtered=(), directive=f"Be mindful: {SECRET}",
            cohort_outputs=(_restricted("mem_OTHER"),),
            requesting_member_id="mem_self",
        )


def test_unowned_restricted_content_still_redacted():
    # empty owner = no ownership asserted → safe default is still redacted.
    from kernos.kernel.integration.briefing import BriefingValidationError
    r = _runner()
    with pytest.raises(BriefingValidationError):
        r._check_redaction_invariant(
            relevant=(), filtered=(), directive=f"Be mindful: {SECRET}",
            cohort_outputs=(_restricted(""),),
            requesting_member_id="mem_self",
        )


def test_covenant_cohort_stamps_owner_member_id():
    # The covenant cohort marks its self-scoped output with the recipient as
    # owner so the exemption applies.
    import inspect
    from kernos.kernel.cohorts import covenant_cohort
    src = inspect.getsource(covenant_cohort)
    assert src.count("owner_member_id=ctx.member_id") >= 2  # both return sites
