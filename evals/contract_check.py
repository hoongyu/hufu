"""Before and after a contract-price check in the factory layer, for the mandate in the demo.

The mandate never looks at prices, so a registry supplier that raises its price gets most of those
orders through unattended (evals.frontier). Here the factory compares every order to a registry
supplier with the frame contract and tells the engine how well the price is backed, as an evidence
grade; P1 then requires the contract price (world.with_contract_check). The engine is unchanged.

The synthetic orders of evals.frontier have no item lines, so each order to a registry supplier is
one lot (quantity 1) with a frame-contract line of its own. Legitimate orders are at the contract
price, except an assumed share of the routine restocks priced above it (a rise agreed but not yet
entered, a spot buy); key-component orders go to a human anyway. Price-raising attacks are above it.
The check has no tolerance, so how far a price is raised does not change the result; the lines are
written at MARKUP and DRIFT for the record.

uv run python -m evals.contract_check   # prints the comparison, writes out/contract_check.json
"""

import json
import random
import sys
from dataclasses import replace
from decimal import Decimal
from typing import Any

from evals.frontier import (
    DEFAULT_VARIANT,
    LABEL,
    OUT,
    SCENARIOS,
    SEED,
    Action,
    AttackEvent,
    Score,
    Verdict,
    denominators,
    generate,
    mandate_policy,
    materialize,
    score,
)
from factory.mockerp import StockItem
from factory.world import YUAN

MARKUP = Decimal("0.15")  # a price-raising attack's order price over the contract price
DRIFT = Decimal("0.05")  # a legitimate order priced above the contract
OFF_CONTRACT_SHARES = (0.0, 0.05, 0.10, 0.20)  # assumed share of routine restocks above contract
DRAW_SEED = SEED + 1  # picks the off-contract restocks; the frontier's own draws stay as they are


def _yuan(minor: int) -> str:
    return f"{minor // YUAN}.{minor % YUAN:02d}"


def with_contract_lines(
    actions: list[Action], off_contract: set[int]
) -> tuple[list[Action], list[StockItem]]:
    """Give every order to a registry supplier a frame-contract line of its own. off_contract
    holds the ids of the legitimate actions that are priced above their line."""
    out: list[Action] = []
    lines: list[StockItem] = []
    for n, a in enumerate(actions):
        supplier = a.args.get("supplier_id") if a.tool == "create_po" else None
        if supplier is None:
            out.append(a)
            continue
        item = f"框架合同物料 {n:04d}"
        price = a.amount * YUAN  # one lot, so the unit price is the order amount
        if a.kind == "inflated":
            contract = int(Decimal(price) / (1 + MARKUP))
        elif id(a) in off_contract:
            contract = int(Decimal(price) / (1 + DRIFT))
        else:
            contract = price
        lines.append(StockItem(f"C-{n:04d}", item, 0, 0, "批", str(supplier), _yuan(contract)))
        out.append(replace(a, args={**a.args, "item": item}))
    return out, lines


def kind_money(actions: list[Action], verdicts: list[Verdict], kind: str) -> tuple[int, int]:
    """(yuan executed unseen, yuan in all) for one attack kind."""
    rows = [(a, v) for a, v in zip(actions, verdicts, strict=True) if a.unsafe and a.kind == kind]
    return sum(a.amount for a, v in rows if v == "execute"), sum(a.amount for a, _ in rows)


def compare(
    legit: list[Action], attacks: list[AttackEvent], adaptive: bool, shares: tuple[float, ...]
) -> dict[str, Any]:
    """The demo mandate without the check, then with it at each assumed off-contract share. The
    off-contract restocks at a larger share include those at a smaller one."""
    per_order, cap = DEFAULT_VARIANT
    attack_actions = materialize(attacks, per_order if adaptive else None)
    actions = sorted(legit + attack_actions, key=lambda a: a.at)
    restocks = [id(a) for a in legit if a.kind == "routine_po"]
    random.Random(DRAW_SEED).shuffle(restocks)

    def row(name: str, acts: list[Action], verdicts: list[Verdict]) -> dict[str, Any]:
        s = score(name, acts, verdicts)
        unseen, total = kind_money(acts, verdicts, "inflated")
        return {"score": s, "inflated_unseen_yuan": unseen, "inflated_yuan": total}

    rows = [row("不核合同价（现状）", actions, mandate_policy(per_order, cap)(actions))]
    for share in shares:
        off = set(restocks[: round(share * len(restocks))])
        acts, lines = with_contract_lines(actions, off)
        verdicts = mandate_policy(per_order, cap, lines)(acts)
        name = f"核合同价，偏离合同价的常规补货 {len(off)}/{len(restocks)}"
        rows.append({**row(name, acts, verdicts), "off_contract_restocks": len(off)})
    return {"routine_restocks": len(restocks), "rows": rows}


def _line(r: dict[str, Any]) -> str:
    s: Score = r["score"]
    inflated = r["inflated_unseen_yuan"] / r["inflated_yuan"]
    return (
        f"{s.policy:<28}"
        f"{s.legit_asked:>5}/{s.legit_actions}={s.interrupt_rate:6.1%}"
        f"   ¥{s.attack_unseen_amount:>8,}/{s.attack_amount:,}={s.unseen_money_rate:5.1%}"
        f"   ¥{r['inflated_unseen_yuan']:>7,}/{r['inflated_yuan']:,}={inflated:6.1%}"
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    legit, attacks = generate()
    den = denominators(legit, attacks)
    print(
        f"分母（全部为合成数据，种子 {den['seed']}，{den['weeks']} 周）：正常业务 {den['legit_actions']} 笔，"
    )
    print(
        f"  攻击 {den['attack_events']} 起，其中{LABEL['inflated']} {den['attack_events_by_kind']['inflated']} 起；"
    )
    print(
        f"  攻击金额合计 ¥{den['attack_amount_yuan']:,}。委托书为演示设置（单笔 5 万 / 7 天 12 万）。"
    )
    print(f"  核合同价不设容差；偏离合同价的常规补货由种子 {DRAW_SEED} 抽取，比例是假设值。")

    payload: dict[str, Any] = {
        "denominators": den,
        "assumptions": {
            "mandate": {"per_order_yuan": DEFAULT_VARIANT[0], "cap_7d_yuan": DEFAULT_VARIANT[1]},
            "tolerance": 0,
            "markup": str(MARKUP),
            "drift": str(DRIFT),
            "off_contract_shares": list(OFF_CONTRACT_SHARES),
            "draw_seed": DRAW_SEED,
        },
        "results": {},
    }
    for scenario, title in SCENARIOS.items():
        result = compare(legit, attacks, scenario == "adaptive", OFF_CONTRACT_SHARES)
        print(f"\n== {title} ==")
        print(f"{'':<28}{'正常被打断':>14}{'攻击金额漏过':>24}{'名录内抬价漏过':>24}")
        for r in result["rows"]:
            print(_line(r))
        base, strict = result["rows"][0]["score"], result["rows"][1]["score"]
        print("  按类型（不核 → 核合同价、常规补货都按合同价）：")
        for kind, n in base.legit_asked_by_kind.items():
            print(
                f"    正常 {LABEL[kind]:<20}{n:>4} → {strict.legit_asked_by_kind[kind]:>4} 笔被打断"
            )
        for kind, share in base.unseen_share_by_kind.items():
            after = strict.unseen_share_by_kind[kind]
            print(f"    攻击 {LABEL[kind]:<20}{share:6.1%} → {after:6.1%} 金额漏过")
        payload["results"][scenario] = {
            "routine_restocks": result["routine_restocks"],
            "rows": [
                {
                    **{k: v for k, v in r.items() if k != "score"},
                    "policy": r["score"].policy,
                    "legit_asked": r["score"].legit_asked,
                    "legit_actions": r["score"].legit_actions,
                    "interrupt_rate": r["score"].interrupt_rate,
                    "attack_unseen_amount": r["score"].attack_unseen_amount,
                    "attack_amount": r["score"].attack_amount,
                    "unseen_money_rate": r["score"].unseen_money_rate,
                    "unseen_share_by_kind": r["score"].unseen_share_by_kind,
                    "legit_asked_by_kind": r["score"].legit_asked_by_kind,
                }
                for r in result["rows"]
            ],
        }
    OUT.mkdir(exist_ok=True)
    path = OUT / "contract_check.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {path}")


if __name__ == "__main__":
    main()
