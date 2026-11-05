"""The audit chain checked from outside the run: load the audit file demo_run wrote, then change one digit
of the amount in the first executed purchase order (record 3, PO-0001) in a copy and load that again.

uv run python -m factory.demo_run --audit out/audit.jsonl
uv run python -m evals.audit_check out/audit.jsonl
"""

import json
import re
import sys
import tempfile
from pathlib import Path

from engine.audit import AuditLog


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "out/audit.jsonl")
    records = AuditLog(path).records()
    print(f"加载 {path.as_posix()}：{len(records)} 条记录，逐条重算哈希，全部对得上")
    print(f"  第 0 条 {records[0].hash[:16]}…　第 {len(records) - 1} 条 {records[-1].hash[:16]}…")

    target = next(r for r in records if r.kind == "executed" and r.payload.get("tool") == "create_po")
    rows = path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(rows[target.seq])
    forged["payload"]["result"] = forged["payload"]["result"].replace("8,600.00", "86,000.00")
    rows[target.seq] = json.dumps(forged, ensure_ascii=False)
    print(f"篡改副本：第 {target.seq} 条（create_po 的执行结果）金额 8,600.00 改成 86,000.00，哈希字段不动")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        copy.write_text("\n".join(rows) + "\n", encoding="utf-8")
        try:
            AuditLog(copy)
        except ValueError as exc:
            m = re.search(r"seq (\d+)", str(exc))
            print(f"加载篡改副本：失败，从第 {m[1] if m else '?'} 条起哈希对不上（{str(exc).replace(tmp, '…')}）")
            return
    sys.exit("加载篡改副本：没有发现篡改")


if __name__ == "__main__":
    main()
