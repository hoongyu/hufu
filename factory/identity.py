"""The ERP as a counterparty that checks the agent's credential before it acts.

The mandate decides whether an action may happen; the credential (engine.identity: which agent acts
for which principal, under which mandate version, for which actions, until when, HMAC-signed) is what
the system on the other side checks before it accepts the request. A call the mandate allowed is
still refused here if the agent shows no credential, an expired one, a forged one, or one that does
not cover the action. Off by default, so the demo and its archived audit chain are unchanged.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.identity import AgentCredential, check, issue
from engine.mandate import Mandate

# the purchasing agent may order and pay; releasing a quality hold is not its job
AGENT_ACTIONS = frozenset({"create_po", "pay_invoice"})
TTL = timedelta(hours=8)
DEMO_KEY = b"hufu-demo-erp-shared-key"  # demo only; a deployment keeps the key in a secret store


class Refused(PermissionError):
    """The counterparty would not accept the request; the enforcement point records it as an error."""


@dataclass
class CredentialGate:
    key: bytes
    principal: str
    now: Callable[[], datetime]
    credential: AgentCredential | None
    refused: list[tuple[str, str]] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)

    def __call__(self, action: str) -> None:
        reason = (
            "no credential"
            if self.credential is None
            else check(self.credential, self.key, self.now(), action, self.principal)
        )
        if reason is not None:
            self.refused.append((action, reason))
            raise Refused(f"ERP refused {action}: {reason}")
        self.accepted.append(action)


def issue_for(
    mandate: Mandate, now: datetime, actions: frozenset[str] = AGENT_ACTIONS, ttl: timedelta = TTL
) -> AgentCredential:
    return issue(mandate, actions, DEMO_KEY, now, ttl)


def gate_for(mandate: Mandate, now: Callable[[], datetime], credential: AgentCredential | None) -> CredentialGate:
    return CredentialGate(DEMO_KEY, mandate.principal, now, credential)
