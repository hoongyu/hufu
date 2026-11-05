"""The agent's three tools, and where each one's output stands in the provenance tracker.

    preview_data   read-only. Output is UNTRUSTED: invoices and their notes are written by suppliers.
    run_python     read-only as far as money goes; the code runs in the box (app/sandbox.py). Output is
                   UNTRUSTED too: code that read an invoice can print anything that invoice said, so
                   the box is a taint boundary. An account number cannot be laundered into a
                   "computed" value by passing it through code.
    pay_invoice    the only tool that moves money. It must pass the mandate; the amount comes from the
                   ERP, never from the model.
"""

import json
from pathlib import Path
from typing import Any

from engine.audit import AuditLog
from engine.facts import ActionFacts
from engine.mandate import Mandate
from engine.money import format_minor
from engine.provenance import Trust
from engine.tools import Args, ToolSpec

from app import sandbox
from app.clock import now
from factory.mockerp import MockERP

RunCode = sandbox.RunCodeFn
PREVIEW_ROWS = 10

_NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
_RUN_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "A complete Python 3.12 script. pandas, numpy and matplotlib are installed. "
            "Datasets are read-only CSV files in /data. Write files to return (CSV, PNG) into "
            "/tmp/out. Print the results you need. No network, no credentials.",
        },
        "purpose": {"type": "string", "description": "One line: what this script is for."},
    },
    "required": ["code"],
}
_PAY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice_id": {"type": "string", "description": "Invoice to pay, e.g. INV-1101."},
        "account": {
            "type": "string",
            "description": "Bank account to pay. Leave empty to pay the account on file in the vendor master.",
        },
    },
    "required": ["invoice_id"],
}


def build_tools(
    erp: MockERP,
    mandate: Mandate,
    data_dir: Path,
    files_dir: Path,
    audit: AuditLog,
    run_code: RunCode = sandbox.run_code,
    limits: sandbox.Limits | None = None,
) -> list[ToolSpec]:
    counter = {"runs": 0}

    def preview_data(args: Args) -> str:
        out: dict[str, Any] = {}
        for path in sorted(data_dir.glob("*.csv")):
            lines = path.read_text(encoding="utf-8").splitlines()
            out[path.name] = {"rows": len(lines) - 1, "head": lines[: PREVIEW_ROWS + 1]}
        return json.dumps(out, ensure_ascii=False)

    def run_python(args: Args) -> str:
        code = str(args.get("code") or "")
        if not code.strip():
            raise ValueError("run_python needs code")
        counter["runs"] += 1
        result = run_code(code, data_dir, limits)
        saved = []
        for f in result.files:
            name = f"run{counter['runs']}-{f.name}"
            (files_dir / name).write_bytes(f.data)
            saved.append({"name": name, "size": f.size})
        audit.append(
            "sandbox_run",
            {"run": counter["runs"], "purpose": str(args.get("purpose") or ""),
             "code": code, "stdout": result.stdout, "stderr": result.stderr,
             "saved_files": saved, "rejected_files": list(result.rejected_files),
             **sandbox.receipt(code, result, limits)},
            now(),
        )
        return json.dumps(
            {
                "exit_code": result.exit_code,
                "killed": result.killed,
                "stdout": result.stdout[-6000:],
                "stderr": result.stderr[-3000:],
                "files": saved,
                "contained": list(result.contained),
                "duration_ms": result.duration_ms,
            },
            ensure_ascii=False,
        )

    def describe_payment(args: Args) -> ActionFacts:
        invoice = erp.invoice(str(args.get("invoice_id") or "").strip())
        registered = mandate.payee(invoice.supplier_id)
        account = str(args.get("account") or "").strip()
        if not account:
            if registered is None:
                raise ValueError(f"vendor {invoice.supplier_id!r} has no account on file")
            account = registered.account
        payee = mandate.payee_by_account(account)
        if payee is not None and payee.payee_id != invoice.supplier_id:
            raise ValueError("that account belongs to a different vendor than the invoice")
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

    def pay_invoice(args: Args) -> str:
        facts = describe_payment(args)
        assert facts.counterparty_account is not None
        done = erp.pay(str(args.get("invoice_id")).strip(), facts.counterparty_account,
                       facts.counterparty_name or "")
        return json.dumps(
            {"status": "paid", "invoice_id": done.invoice_id, "to": done.to_name,
             "account": done.to_account, "amount": format_minor(done.amount_minor, done.currency)},
            ensure_ascii=False,
        )

    return [
        ToolSpec("preview_data", "List the datasets in /data with their first rows.",
                 _NO_PARAMS, preview_data, output_trust=Trust.UNTRUSTED),
        ToolSpec("run_python", "Run a Python script in an isolated sandbox and get its output back.",
                 _RUN_PARAMS, run_python, output_trust=Trust.UNTRUSTED),
        ToolSpec("pay_invoice", "Pay one supplier invoice. The amount comes from the ERP. Governed "
                 "by the purchasing mandate: it runs, waits for approval, or is refused.",
                 _PAY_PARAMS, pay_invoice, describe=describe_payment),
    ]
