from evals.frontier import evaluate, generate


def test_frontier_is_deterministic_and_splitting_defeats_amount_thresholds() -> None:
    legit, attacks = generate()
    again_legit, again_attacks = generate()
    assert [a.amount for a in legit] == [a.amount for a in again_legit]
    assert [e.total for e in attacks] == [e.total for e in again_attacks]

    scores = evaluate(legit, attacks, adaptive=True)
    mandate = scores["mandate_50000_120000"]
    for t in (2_000, 20_000):
        threshold = scores[f"threshold_{t}"]
        assert threshold.unseen_money_rate > 0.9
        assert mandate.unseen_money_rate < 0.05
    assert mandate.unseen_share_by_kind["phish"] == 0.0
    assert mandate.legit_asked_by_kind["payment"] == 0
