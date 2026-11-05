"""Content checks on what the agent reads and what it says. Both only flag; neither lets anything
through or stops anything on its own. The decision stays with the mandate.

- directives(): phrases in third-party text that talk to the AI or tell it to skip a check
  ("致 AI 助手", "无需再次询问", "AI 采购系统直接下单", "分成 6 张 5 万元的订单"). The approver sees
  the tags next to the escalation.
- full_accounts(): bank account numbers written out in full, for text that leaves the agent (its
  replies, anything it would send). A registered account shown to people is masked to its last 4.
"""

import re

_DIRECTIVES = [
    ("对 AI 说话", r"(致|给|请)\s*(AI|ＡＩ|人工智能)\s*(助手|系统|采购系统|代理)?"),
    ("对 AI 说话", r"(AI|ＡＩ)\s*(助手|采购系统|系统)"),
    ("要求跳过确认", r"无需(再次)?(询问|确认|审批|核实)|不用(再)?(问|确认)|直接(付款|下单|转账|支付)"),
    ("声称已确认", r"已(经)?(.{0,8})(电话|口头)?确认"),
    ("教人绕开额度", r"(分成|拆成|拆分为)\s*\d+\s*张|单笔审批有额度限制"),
    ("要求忽略指令", r"忽略(之前|以上|上述)|ignore (all |the )?(previous|above)"),
]
_ACCOUNT = re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)")


def directives(text: str) -> list[tuple[str, str]]:
    """(kind, matched phrase) for each directive found, in order of appearance."""
    found: list[tuple[int, str, str]] = []
    for kind, pattern in _DIRECTIVES:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            found.append((m.start(), kind, m.group(0)))
    found.sort()
    seen, out = set(), []
    for _, kind, phrase in found:
        if (kind, phrase) not in seen:
            seen.add((kind, phrase))
            out.append((kind, phrase))
    return out


def full_accounts(text: str) -> list[str]:
    """Account-like digit runs (12 to 19 digits, spaces or dashes allowed) written out in full."""
    return [m.group(0).strip() for m in _ACCOUNT.finditer(text)]


def mask(account: str) -> str:
    digits = re.sub(r"\D", "", account)
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"
