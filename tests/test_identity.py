"""The ERP checks the agent's credential before it acts (factory/identity.py)."""

from datetime import timedelta

from engine.llm import ScriptedLLM

from factory.episodes import EPISODES, run_episodes
from factory.identity import issue_for
from factory.world import NOW, factory_mandate


def scripted(e):  # type: ignore[no-untyped-def]
    return ScriptedLLM(e.fooled_script)


def errors(run):  # type: ignore[no-untyped-def]
    return [r.payload["error"] for r in run.pep.audit.records() if r.kind == "error"]


def test_a_valid_credential_changes_nothing() -> None:
    plain = run_episodes(scripted, EPISODES)
    gated = run_episodes(scripted, EPISODES, credential="issue")
    assert [o.po_id for o in gated.erp.orders] == [o.po_id for o in plain.erp.orders]
    assert len(gated.erp.orders) == 3 and errors(gated) == []
    assert gated.erp.gate.accepted == ["create_po"] * 3  # type: ignore[union-attr]


def test_no_credential_no_order() -> None:
    run = run_episodes(scripted, EPISODES, credential=None)
    assert len(run.erp.orders) == 3  # the check is off by default
    from factory.episodes import new_run
    from factory.identity import gate_for

    gated = new_run()
    gated.erp.gate = gate_for(factory_mandate(), gated.clock, None)
    try:
        gated.erp.create_po("越州精固紧固件有限公司", "M8 螺栓", 10, 430, "CNY")
    except PermissionError as exc:
        assert "no credential" in str(exc)
    assert gated.erp.orders == []


def test_expired_forged_and_narrow_credentials_are_refused() -> None:
    mandate = factory_mandate()
    expired = issue_for(mandate, NOW - timedelta(hours=9))
    forged = issue_for(mandate, NOW).model_copy(update={"actions": frozenset({"create_po", "release_quality_hold"})})
    narrow = issue_for(mandate, NOW, actions=frozenset({"pay_invoice"}))
    for shown, why in ((expired, "not valid at this time"), (forged, "signature"), (narrow, "does not cover")):
        run = run_episodes(scripted, EPISODES, credential=shown)
        assert run.erp.orders == [], why
        # nothing executes, so the 7-day total never rises and all 6 split orders reach the ERP too: 1 + 6
        assert len(errors(run)) == 7 and all(why in e for e in errors(run)), errors(run)
