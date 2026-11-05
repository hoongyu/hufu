"""One run = one visitor's task against a fresh copy of the AP desk: its own ERP, mandate, datasets,
sandbox runs and hash-chained audit log. Runs never share state, so judges clicking at the same time
cannot see or move each other's money."""

import secrets
import shutil
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from engine.agent import Agent
from engine.audit import AuditLog
from engine.llm import LLM, ScriptedLLM
from engine.pep import PolicyEnforcementPoint, ToolOutcome
from engine.tools import Args, registry

from app import inference, sandbox
from app.clock import now
from app.scenarios import PRESET, SYSTEM_PROMPT, worst_case_script
from app.tools import RunCode, build_tools
from app.world import APPROVER, CLAUSE_TEXT, ap_erp, ap_mandate, invoices, write_datasets
from factory.mockerp import MockERP

Mode = Literal["live", "worst_case"]
MAX_RUNS = 40
MAX_STEPS = 10


class SerializedPEP(PolicyEnforcementPoint):
    """The agent's thread and a human clicking Approve share one PEP and one audit chain. Each call
    that writes to the chain goes in whole, under the run's lock; reading the chain needs no lock,
    so the page can follow a run while a sandbox script is still running."""

    def __init__(self, *args: Any, lock: threading.RLock, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = lock

    def observe_user(self, text: str, source: str) -> None:
        with self.lock:
            super().observe_user(text, source)

    def handle(self, name: str, args: Args) -> ToolOutcome:
        with self.lock:
            return super().handle(name, args)

    def approve(self, escalation_id: str, approver: str) -> ToolOutcome:
        with self.lock:
            return super().approve(escalation_id, approver)

    def reject(self, escalation_id: str, approver: str) -> None:
        with self.lock:
            super().reject(escalation_id, approver)


@dataclass
class Run:
    run_id: str
    mode: Mode
    preset: str
    prompt: str
    model: str
    directory: Path
    audit: AuditLog
    pep: SerializedPEP
    erp: MockERP
    honeypot_token: str
    honeypot_url: str
    state: str = "queued"  # queued, running, done, error
    final_text: str | None = None
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class LiveBudget:
    """Caps live model runs per hour, so a public demo URL cannot drain the inference credits."""

    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            cutoff = time.monotonic() - 3600
            while self._times and self._times[0] < cutoff:
                self._times.popleft()
            if len(self._times) >= self.per_hour:
                return False
            self._times.append(time.monotonic())
            return True


class Honeypot:
    """The address the injected note tells the agent to upload secrets to is this server. A hit with
    a run's token would mean code got out of the box; the count is shown next to every run."""

    def __init__(self) -> None:
        self._hits: dict[str, list[dict[str, str]]] = {}
        self._lock = threading.Lock()

    def hit(self, token: str, source: str) -> None:
        with self._lock:
            self._hits.setdefault(token, []).append({"at": now().isoformat(), "from": source})

    def hits(self, token: str) -> list[dict[str, str]]:
        with self._lock:
            return list(self._hits.get(token, []))


def sandbox_facing(base_url: str) -> str:
    """From inside a container, localhost is the container itself; Docker Desktop names the host."""
    parts = urlsplit(base_url.rstrip("/"))
    host = parts.hostname or ""
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


class RunManager:
    def __init__(
        self,
        base_dir: Path,
        run_code: RunCode = sandbox.run_code,
        llm_factory: Callable[[], LLM] | None = None,
        live_per_hour: int = 30,
        workers: int = 2,
    ) -> None:
        self.base_dir = base_dir
        self.run_code = run_code
        self.llm_factory = llm_factory or inference.client
        self.budget = LiveBudget(live_per_hour)
        self.honeypot = Honeypot()
        self.runs: OrderedDict[str, Run] = OrderedDict()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hufu-run")
        self._lock = threading.Lock()
        (base_dir / "runs").mkdir(parents=True, exist_ok=True)

    def create(self, mode: Mode, preset: str, prompt: str, public_base: str) -> Run:
        if mode == "live" and not self.budget.take():
            raise PermissionError("live run limit for this hour reached; try the worst-case replay")
        run_id = secrets.token_hex(6)
        token = secrets.token_urlsafe(12)
        directory = self.base_dir / "runs" / run_id
        (directory / "files").mkdir(parents=True)
        honeypot_url = f"{sandbox_facing(public_base)}/api/honeypot/{token}"
        current = now()
        mandate = ap_mandate(current)
        rows = invoices(honeypot_url)
        erp = ap_erp(rows)
        write_datasets(directory / "data", mandate, rows)
        audit = AuditLog(directory / "audit.jsonl")
        tools = build_tools(erp, mandate, directory / "data", directory / "files", audit,
                            run_code=self.run_code)
        lock = threading.RLock()
        pep = SerializedPEP(mandate, registry(tools), audit, now, lock=lock)
        text = prompt.strip() or PRESET.get(preset, PRESET["pay"]).prompt
        model = "worst-case replay" if mode == "worst_case" else inference.model_id()
        run = Run(run_id, mode, preset, text[:2000], model, directory, audit, pep, erp, token,
                  honeypot_url, lock=lock)
        audit.append("run_started", {"mode": mode, "model": model, "preset": preset,
                                     "mandate": f"{mandate.mandate_id}@v{mandate.version}",
                                     "clauses": CLAUSE_TEXT}, current)
        with self._lock:
            self.runs[run_id] = run
            while len(self.runs) > MAX_RUNS:
                _, old = self.runs.popitem(last=False)
                shutil.rmtree(old.directory, ignore_errors=True)
        self._pool.submit(self._execute, run)
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self.runs.get(run_id)

    def _execute(self, run: Run) -> None:
        run.state = "running"
        try:
            inner: LLM = (ScriptedLLM(worst_case_script(run.honeypot_url))
                          if run.mode == "worst_case" else self.llm_factory())
            llm = inference.AuditedLLM(inner, run.audit, run.model, lock=run.lock)
            result = Agent(llm, run.pep, SYSTEM_PROMPT, max_steps=MAX_STEPS).run(run.prompt)
            with run.lock:
                run.final_text = result.final_text
                run.audit.append("run_finished", {"final_text": result.final_text,
                                                  "stopped_by_step_limit": result.stopped_by_step_limit},
                                 now())
            run.state = "done"
        except Exception as exc:  # a model or network failure ends the run, never the server
            run.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            with run.lock:
                run.audit.append("run_error", {"error": run.error}, now())
            run.state = "error"

    def decide(self, run: Run, escalation_id: str, approve: bool) -> dict[str, Any]:
        with run.lock:  # re-entrant: approve/reject take it again inside
            if escalation_id not in run.pep.pending:
                raise KeyError(escalation_id)
            if approve:
                outcome = run.pep.approve(escalation_id, APPROVER)
                return {"status": outcome.status, "content": outcome.content}
            run.pep.reject(escalation_id, APPROVER)
            return {"status": "rejected"}

    def view(self, run: Run, after: int = -1) -> dict[str, Any]:
        # No lock: records() is a snapshot of an append-only list, so a poll never waits for a
        # sandbox script. Records are immutable, so verifying while the agent appends is safe.
        records = run.audit.records()
        broken = run.audit.verify()
        pending = [
            {"escalation_id": e.escalation_id, "tool": e.tool, "args": e.args,
             "amount_minor": e.facts.amount_minor, "currency": e.facts.currency,
             "counterparty": e.facts.counterparty_name, "reasons": list(e.decision.reasons)}
            for e in list(run.pep.pending.values())
        ]
        payments = [
            {"invoice_id": p.invoice_id, "to": p.to_name, "account": p.to_account,
             "amount_minor": p.amount_minor, "currency": p.currency}
            for p in list(run.erp.payments)
        ]
        decisions = [r for r in records if r.kind == "decision"]
        boxes = [r for r in records if r.kind == "sandbox_run"]
        return {
            "run_id": run.run_id,
            "mode": run.mode,
            "model": run.model,
            "prompt": run.prompt,
            "state": run.state,
            "error": run.error,
            "final_text": run.final_text,
            "records": [r.model_dump(mode="json") for r in records if r.seq > after],
            "summary": {
                "payments": payments,
                "pending": pending,
                "refused": sum(1 for r in decisions if r.payload.get("effect") == "deny"),
                "sandbox_runs": len(boxes),
                "contained": sorted({c for r in boxes for c in r.payload.get("contained", [])}),
                "honeypot_hits": len(self.honeypot.hits(run.honeypot_token)),
                "chain": {"records": len(records), "intact": broken is None, "broken_at": broken,
                          "head": records[-1].hash if records else None},
            },
        }
