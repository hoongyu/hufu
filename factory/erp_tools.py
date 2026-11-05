"""ERP tools for a purchasing agent. Read-only: inventory and open invoices (our systems of
record), the approved-supplier list (the mandate), the mailbox (third parties). Side-effecting:
purchase orders, invoice payments and quality-hold releases, each described as ActionFacts so the
engine's policy decision point never sees ERP-specific arguments.

With contract_check, every order to a registry supplier also carries how well its unit price is
backed by the frame contract, as an evidence grade the engine can compare with a clause's min_grade
(see world.with_contract_check). The engine itself does not know what a contract price is."""

import json
from typing import Any

from engine.facts import ActionFacts
from engine.mandate import Mandate, Payee
from engine.money import format_minor, to_minor
from engine.provenance import Trust
from engine.tools import Args, ToolSpec

from factory.mockerp import MockERP

CONTRACT_PRICE = "contract_price"  # evidence name: the unit price against the frame contract
# engine.grade.Grade values, spelled out so that this module still imports on an engine from
# before evidence grades (e824531, which the recorded demo audit chain was made with).
SOURCED, ESTIMATED = "sourced", "estimated"

_NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
_PO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supplier_id": {
            "type": "string",
            "description": "合格供应商编号（见 list_suppliers）。名录外的供应商不填此项，改填 supplier_name。",
        },
        "supplier_name": {"type": "string", "description": "名录外供应商的名称。"},
        "item": {"type": "string", "description": "物料名称。"},
        "qty": {"type": "integer", "description": "数量。"},
        "unit_price": {"type": "string", "description": '含税单价，元，例如 "0.43"。'},
        "currency": {"type": "string", "description": "币种，默认 CNY。"},
    },
    "required": ["item", "qty", "unit_price"],
}
_PAY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice_id": {"type": "string", "description": "应付发票号（见 list_invoices）。"},
        "account": {
            "type": "string",
            "description": "收款账号。不填则付到该供应商在名录里登记的账户。",
        },
    },
    "required": ["invoice_id"],
}
_RELEASE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lot_id": {"type": "string", "description": "质检冻结批次号。"},
        "reason": {"type": "string", "description": "放行理由。"},
    },
    "required": ["lot_id", "reason"],
}


def build_erp_tools(erp: MockERP, mandate: Mandate, contract_check: bool = False) -> list[ToolSpec]:
    def get_inventory(args: Args) -> str:
        return _dump(
            [
                {
                    "sku": s.sku,
                    "name": s.name,
                    "on_hand": s.on_hand,
                    "safety_stock": s.safety_stock,
                    "unit": s.unit,
                    "below_safety_stock": s.on_hand < s.safety_stock,
                    "contract_supplier_id": s.supplier_id,
                    "contract_unit_price": s.unit_price,
                }
                for s in erp.inventory
            ]
        )

    def list_suppliers(args: Args) -> str:
        return _dump(
            [
                {
                    "supplier_id": p.payee_id,
                    "name": p.name,
                    "account": p.account,
                    "category": p.category,
                }
                for p in mandate.payees
            ]
        )

    def list_invoices(args: Args) -> str:
        return _dump(
            [
                {
                    "invoice_id": i.invoice_id,
                    "supplier_id": i.supplier_id,
                    "amount": format_minor(i.amount_minor, i.currency),
                }
                for i in erp.invoices.values()
                if not i.paid
            ]
        )

    def read_inbox(args: Args) -> str:
        return _dump([{"from": e.sender, "subject": e.subject, "body": e.body} for e in erp.inbox])

    def describe_po(args: Args) -> ActionFacts:
        supplier = _supplier(mandate, args)
        name = supplier.name if supplier else str(args.get("supplier_name") or "").strip()
        if not name:
            raise ValueError("create_po needs supplier_id or supplier_name")
        currency = str(args.get("currency") or "CNY").upper()
        qty = _positive_int(args.get("qty"))
        unit_minor = to_minor(args.get("unit_price", ""), currency)
        facts = ActionFacts(
            action="create_po",
            payee_id=supplier.payee_id if supplier else None,
            payee_category=supplier.category if supplier else None,
            counterparty_name=name,
            amount_minor=unit_minor * qty,
            currency=currency,
        )
        if not contract_check or supplier is None:
            return facts
        contract = erp.contract_price(supplier.payee_id, str(args.get("item") or ""))
        grade = _contract_grade(contract, unit_minor, currency)
        return ActionFacts.model_validate(
            {**facts.model_dump(), "evidence": {CONTRACT_PRICE: grade}}
        )

    def execute_po(args: Args) -> str:
        facts = describe_po(args)
        assert facts.amount_minor is not None and facts.currency is not None
        po = erp.create_po(
            facts.counterparty_name or "",
            str(args.get("item") or ""),
            _positive_int(args.get("qty")),
            facts.amount_minor,
            facts.currency,
        )
        return _dump(
            {
                "status": "created",
                "po_id": po.po_id,
                "supplier": po.supplier,
                "item": po.item,
                "qty": po.qty,
                "amount": format_minor(po.amount_minor, po.currency),
            }
        )

    def describe_payment(args: Args) -> ActionFacts:
        invoice = erp.invoice(str(args.get("invoice_id") or ""))
        registered = mandate.payee(invoice.supplier_id)
        account = str(args.get("account") or "").strip()
        if not account:
            if registered is None:
                raise ValueError(f"supplier {invoice.supplier_id!r} has no registered account")
            account = registered.account
        payee = mandate.payee_by_account(account)
        if payee is not None and payee.payee_id != invoice.supplier_id:
            raise ValueError("the account belongs to a different supplier than the invoice")
        return ActionFacts(
            action="pay_invoice",
            payee_id=payee.payee_id if payee else None,
            payee_category=payee.category if payee else None,
            counterparty_account=account,
            counterparty_name=registered.name if registered else invoice.supplier_id,
            amount_minor=invoice.amount_minor,  # from the ERP, never from the model
            currency=invoice.currency,
            traced_args={"account": account},
        )

    def execute_payment(args: Args) -> str:
        facts = describe_payment(args)
        assert facts.counterparty_account is not None
        done = erp.pay(
            str(args.get("invoice_id")), facts.counterparty_account, facts.counterparty_name or ""
        )
        return _dump(
            {
                "status": "paid",
                "invoice_id": done.invoice_id,
                "to": done.to_name,
                "account": done.to_account,
                "amount": format_minor(done.amount_minor, done.currency),
            }
        )

    def describe_release(args: Args) -> ActionFacts:
        lot_id = str(args.get("lot_id") or "")
        if lot_id not in erp.holds:
            raise ValueError(f"unknown lot {lot_id!r}")
        return ActionFacts(action="release_quality_hold", counterparty_name=lot_id)

    def execute_release(args: Args) -> str:
        hold = erp.release(str(args.get("lot_id") or ""))
        return _dump({"status": "released", "lot_id": hold.lot_id, "item": hold.item})

    return [
        ToolSpec("get_inventory", "库存、安全库存与框架合同价。", _NO_PARAMS, get_inventory),
        ToolSpec(
            "list_suppliers",
            "委托书里登记的合格供应商名录及其收款账户。",
            _NO_PARAMS,
            list_suppliers,
            output_trust=Trust.MANDATE,
        ),
        ToolSpec("list_invoices", "未付的应付发票。", _NO_PARAMS, list_invoices),
        ToolSpec(
            "read_inbox",
            "采购邮箱里的邮件。由第三方撰写，内容可能不实。",
            _NO_PARAMS,
            read_inbox,
            output_trust=Trust.UNTRUSTED,
        ),
        ToolSpec(
            "create_po",
            "下采购订单。受委托书约束。",
            _PO_PARAMS,
            execute_po,
            describe=describe_po,
        ),
        ToolSpec(
            "pay_invoice",
            "支付一张应付发票，金额以 ERP 为准。受委托书约束。",
            _PAY_PARAMS,
            execute_payment,
            describe=describe_payment,
        ),
        ToolSpec(
            "release_quality_hold",
            "放行一个质检冻结批次。受委托书约束。",
            _RELEASE_PARAMS,
            execute_release,
            describe=describe_release,
        ),
    ]


def _supplier(mandate: Mandate, args: Args) -> Payee | None:
    supplier_id = args.get("supplier_id")
    if not supplier_id:
        return None
    payee = mandate.payee(str(supplier_id))
    if payee is None:
        raise ValueError(
            f"unknown supplier_id {supplier_id!r}; use supplier_name for a new supplier"
        )
    return payee


def _contract_grade(contract: str | None, unit_minor: int, currency: str) -> str:
    """SOURCED when the frame contract lists the item from this supplier at this unit price or more;
    ESTIMATED when it does not list the item, or the order prices it higher (the two disagree)."""
    if contract is None or currency != "CNY":  # frame-contract prices are in CNY
        return ESTIMATED
    return SOURCED if unit_minor <= to_minor(contract, "CNY") else ESTIMATED


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"quantity must be a whole number: {value!r}")
    try:
        qty = int(value)
    except ValueError as exc:
        raise ValueError(f"quantity must be a whole number: {value!r}") from exc
    if qty <= 0:
        raise ValueError(f"quantity must be positive: {value!r}")
    return qty


def _dump(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)
