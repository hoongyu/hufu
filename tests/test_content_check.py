"""Content checks flag; they never decide (factory/content_check.py)."""

from factory.content_check import directives, full_accounts, mask
from factory.world import ATTACKER_ACCOUNT, PHISHING_EMAIL


def test_the_phishing_mail_is_tagged() -> None:
    kinds = {k for k, _ in directives(PHISHING_EMAIL.body)}
    assert {"对 AI 说话", "要求跳过确认"} <= kinds


def test_full_accounts_and_mask() -> None:
    assert full_accounts(f"请汇至 {ATTACKER_ACCOUNT}。") == [ATTACKER_ACCOUNT]
    assert full_accounts("发票 INV-2609-031，金额 186,400.00 元") == []
    assert mask(ATTACKER_ACCOUNT) == "****5432"
