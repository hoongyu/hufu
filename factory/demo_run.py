"""Run the four episodes and print what happened, in Chinese, straight from the audit log.

uv run python -m factory.demo_run                                   # scripted fooled model
uv run python -m factory.demo_run --live anthropic/claude-sonnet-5  # any LiteLLM model string
uv run python -m factory.demo_run --audit out/audit.jsonl           # also persist the audit chain
uv run python -m factory.demo_run --live <MoMA 模型名> --moma       # a model on China Mobile's MoMA
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from engine.audit import AuditLog, AuditRecord
from engine.llm import LLM, LiteLLMClient, ScriptedLLM

from factory import moma
from factory.episodes import Episode, Run, run_episodes
from factory.world import CLAUSE_TEXT

STATUS = {
    "ok": "已执行",
    "pending_approval": "等待人工审批",
    "denied": "拒绝",
    "rejected": "无效调用",
    "error": "出错",
}
EFFECT = {"allow": "自动执行", "ask": "问人", "deny": "拒绝"}
CATEGORY = {"standard_parts": "标准件", "packaging": "包材", "key_components": "关键部件"}
TRUST = {
    "mandate": "委托书名录",
    "system": "本方系统",
    "user": "用户输入",
    "untrusted": "第三方邮件",
    "unknown": "无出处",
}
ARG = {"account": "收款账号"}
CIRCLED = "①②③④⑤⑥⑦⑧"


def money(text: str) -> str:
    return re.sub(r"CNY ([\d,]+\.\d\d)", r"¥\1", text)


def why(text: str, tool: str = "") -> str:
    not_registered = (
        "收款账号不是名录里登记的账户" if tool == "pay_invoice" else "对方不在合格供应商名录"
    )
    rules: list[tuple[str, Any]] = [
        (r"payee is not in the registry", not_registered),
        (r"payee is in the registry", "对方在名录内"),
        (
            r"payee category '?(\w+)'? is not one of .*",
            lambda m: f"物料类别「{CATEGORY.get(m[1], m[1])}」不在授权范围",
        ),
        (r"payee category None is not one of .*", "物料类别未登记"),
        (r"amount (.+) exceeds (.+)", lambda m: f"金额 {m[1]} 超过单笔上限 {m[2]}"),
        (r"amount (.+) is below (.+)", lambda m: f"金额 {m[1]} 低于 {m[2]}"),
        (
            r"(\w+) is not from a trusted source \((\w+)\)",
            lambda m: f"{ARG.get(m[1], m[1])}的来源不可信（{TRUST.get(m[2], m[2])}）",
        ),
        (
            r"(\d+)-day total would be (.+), cap is (.+)",
            lambda m: f"{m[1]} 天累计将达 {m[2]}，超过上限 {m[3]}",
        ),
        (r"mandate is revoked or outside its validity window", "委托书已撤销或不在有效期内"),
    ]
    for pattern, repl in rules:
        m = re.fullmatch(pattern, text)
        if m:
            return money(repl(m) if callable(repl) else repl)
    return money(text)


def reason_lines(effect: str, reasons: list[str], tool: str) -> list[str]:
    """What an approver needs: the clauses that applied, and why no ALLOW clause covered it."""
    out = []
    for r in reasons:
        if m := re.fullmatch(r"(\w+) \((\w+)\) applies", r):
            if effect != "allow":
                out.append(f"{m[1]} 适用：{CLAUSE_TEXT.get(m[1], '')}")
        elif m := re.fullmatch(r"(\w+) \(allow\) does not apply: (.*)", r):
            out.append(f"{m[1]} 不能放行：{why(m[2], tool)}")
        elif r.startswith("no clause applies"):
            out.append("没有条款放行，按默认：问人")
    return out


def source(s: str) -> str:
    if s.startswith("tool:read_inbox"):
        return f"采购邮箱（审计 {s.split('#')[-1]}）"
    if s.startswith("mandate:"):
        return f"委托书名录 {s.split('#')[-1]}"
    if s.startswith("tool:"):
        return f"本方系统 {s[5:]}"
    return {"user_message": "用户输入"}.get(s, s)


def describe_decision(rec: AuditRecord) -> list[str]:
    p = rec.payload
    effect = str(p["effect"])
    facts = p["facts"]
    amount = facts.get("amount_minor")
    what = f"{p['tool']} → {facts.get('counterparty_name') or ''}"
    if amount:
        what += f"  ¥{amount / 100:,.2f}"
    head = f"{EFFECT.get(effect, effect)}"
    if effect == "allow":
        clauses = "、".join(p["matched"])
        head += f"（条款 {clauses}：{CLAUSE_TEXT.get(p['matched'][0], '')}）"
    lines = [f"  {what}", f"      裁决：{head}"]
    lines += [f"      · {r}" for r in reason_lines(effect, list(p["reasons"]), str(p["tool"]))]
    for arg, lab in p.get("provenance", {}).items():
        best = TRUST.get(lab["best"], lab["best"])
        seen = "、".join(source(s) for s in lab["seen_in"]) or "哪里都没出现过"
        lines.append(f"      · {ARG.get(arg, arg)}出处：{best}（见于 {seen}）")
    return lines


def print_run(run: Run) -> None:
    records = run.pep.audit.records()
    starts = [i for i, r in enumerate(records) if r.kind == "user_message"] + [len(records)]
    for n, (episode, result) in enumerate(run.results):
        print(f"\n━━ {CIRCLED[n]} {episode.title} ━━")
        print(f">>> {episode.prompt}")
        for rec in records[starts[n] : starts[n + 1]]:
            if rec.kind == "decision":
                print("\n".join(describe_decision(rec)))
            elif rec.kind == "rejected":
                print(f"  {rec.payload['tool']}：无效调用（{rec.payload['reason']}）")
        statuses = [STATUS.get(o.status, o.status) for name, o in result.outcomes]
        print(f"  工具调用结果：{' / '.join(statuses)}")
        print(f"  模型自称：{result.final_text}")

    erp = run.erp
    print("\n━━ 实际发生了什么 ━━")
    print(f"  已下采购单：{len(erp.orders)} 张，合计 {money(erp.spent())}")
    for po in erp.orders:
        print(
            f"    {po.po_id}  {po.supplier}  {po.item} × {po.qty:,}  ¥{po.amount_minor / 100:,.2f}"
        )
    print(f"  已付款：{len(erp.payments)} 笔")
    print(f"  等待人工审批：{len(run.pep.pending)} 项")
    print(
        f"  审计记录：{len(records)} 条，哈希链完整：{'是' if run.pep.audit.verify() is None else '否'}"
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="虎符 · 制造业采购智能体演示")
    parser.add_argument(
        "--live", metavar="MODEL", help="LiteLLM 模型串，例如 anthropic/claude-sonnet-5"
    )
    parser.add_argument("--audit", metavar="PATH", type=Path, help="把审计链写到 JSONL 文件")
    parser.add_argument(
        "--moma",
        action="store_true",
        help="--live 的模型走移动云 MoMA：接口地址和密钥取 MOMA_API_BASE、MOMA_API_KEY（环境变量或 .env）",
    )
    args = parser.parse_args()
    if args.moma and not args.live:
        parser.error("--moma 要和 --live 模型名一起用")
    model, options = args.live, {}
    if args.moma:
        model, options = moma.model_name(args.live), moma.options()

    def llm_for(episode: Episode) -> LLM:
        return LiteLLMClient(model, **options) if model else ScriptedLLM(episode.fooled_script)

    audit = None
    if args.audit:
        args.audit.parent.mkdir(parents=True, exist_ok=True)
        args.audit.unlink(missing_ok=True)
        audit = AuditLog(args.audit)
    where = "（MoMA）" if args.moma else ""
    mode = (
        f"真实模型 {args.live}{where}" if args.live else "脚本模型：邮件说什么就做什么（最坏情况）"
    )
    print(f"虎符演示 · {mode}")
    print_run(run_episodes(llm_for, audit=audit))


if __name__ == "__main__":
    main()
