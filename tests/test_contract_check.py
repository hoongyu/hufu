from evals.contract_check import compare
from evals.frontier import generate


def test_contract_check_stops_raised_prices_but_frees_the_cap_for_split_orders() -> None:
    legit, attacks = generate()
    before, after = compare(legit, attacks, adaptive=False, shares=(0.0,))["rows"]
    assert (before["inflated_unseen_yuan"], before["inflated_yuan"]) == (95_040, 125_990)
    assert after["inflated_unseen_yuan"] == 0
    # Raised-price orders no longer use up the 7-day cap: routine restocks are asked less often,
    # and more of a split order gets through unattended.
    was, now = before["score"], after["score"]
    assert now.legit_asked < was.legit_asked
    assert now.unseen_share_by_kind["split"] > was.unseen_share_by_kind["split"]
