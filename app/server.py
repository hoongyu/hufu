"""The web app and API. Runs on the Vultr VM behind Caddy:

    uv run uvicorn app.server:app --host 127.0.0.1 --port 8000

Settings (environment, or the git-ignored .env): VULTR_INFERENCE_API_KEY, VULTR_MODEL,
HUFU_PUBLIC_URL (https://<ip>.sslip.io), HUFU_SANDBOX_RUNTIME (runsc on the VM), HUFU_DATA_DIR,
HUFU_LIVE_RUNS_PER_HOUR."""

import contextlib
import os
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import inference, sandbox
from app.runs import RunManager, sandbox_facing
from app.scenarios import PRESETS
from app.selftest import SelfTestRunner
from app.world import CLAUSE_TEXT, ap_mandate, invoices, now_utc

STATIC = Path(__file__).parent / "static"
DATA_DIR = Path(os.environ.get("HUFU_DATA_DIR", Path(__file__).resolve().parent.parent / "var"))
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'")


class _Limiter:
    """A few run starts per visitor per ten minutes; enough for a judge, not for a script."""

    def __init__(self, per_window: int, window_s: float = 600) -> None:
        self.per_window, self.window_s = per_window, window_s
        self._seen: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            q, t = self._seen[key], time.monotonic()
            while q and q[0] < t - self.window_s:
                q.popleft()
            if len(q) >= self.per_window:
                return False
            q.append(t)
            return True


def _public_base(request: Request) -> str:
    return os.environ.get("HUFU_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


def _client(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


manager = RunManager(DATA_DIR, live_per_hour=int(os.environ.get("HUFU_LIVE_RUNS_PER_HOUR", "30")))
selftests = SelfTestRunner(DATA_DIR / "selftest-data",
                           hits=lambda token: len(manager.honeypot.hits(token)))
limiter = _Limiter(int(os.environ.get("HUFU_RUNS_PER_VISITOR", "12")))
_docker_cache: dict[str, Any] = {"at": 0.0, "info": None}
_docker_refresh = threading.Lock()
CHECKING: dict[str, Any] = {"ready": False, "checking": True, "error": "checking the sandbox"}


def _refresh_docker() -> None:
    if not _docker_refresh.acquire(blocking=False):
        return  # a check is already running
    try:
        _docker_cache.update(at=time.monotonic(), info=sandbox.docker_info())
    finally:
        _docker_refresh.release()


def docker_status(wait: bool = True) -> dict[str, Any]:
    """`docker info` can take seconds. The page asks with wait=False and gets the cached answer
    (refreshed in the background every 30 s, or 5 s after a failure); starting a run waits for a real answer."""
    info: dict[str, Any] | None = _docker_cache["info"]
    # a failure is retried soon: Docker Desktop can take a while to answer right after it wakes up
    stale = time.monotonic() - _docker_cache["at"] > (30 if info and info.get("ready") else 5)
    if info is None and wait:
        with _docker_refresh:
            if _docker_cache["info"] is None:
                _docker_cache.update(at=time.monotonic(), info=sandbox.docker_info())
        return dict(_docker_cache["info"])
    if info is None or stale:
        threading.Thread(target=_refresh_docker, daemon=True).start()
    return dict(info) if info is not None else dict(CHECKING)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    def warm_up() -> None:
        with contextlib.suppress(Exception):  # docker missing: the status endpoint says so
            sandbox.cleanup()  # no sandbox outlives a restart
        _refresh_docker()

    threading.Thread(target=warm_up, daemon=True).start()
    yield


app = FastAPI(title="Hufu", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = CSP
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {
        "inference": {"provider": "Vultr Serverless Inference", "base_url": inference.BASE_URL,
                      "model": inference.model_id(), "configured": inference.configured()},
        "sandbox": docker_status(wait=False),
        "host": os.environ.get("HUFU_HOST_LABEL", "local development"),
        "presets": [{"key": p.key, "title": p.title, "prompt": p.prompt} for p in PRESETS],
        "clauses": CLAUSE_TEXT,
    }


@app.get("/api/datasets")
def datasets() -> dict[str, Any]:
    """What the agent will see, as a visitor can inspect it before running anything."""
    mandate = ap_mandate(now_utc())
    rows = invoices("https://<this server>/api/honeypot/<per-run token>")
    return {
        "vendor_master": [[p.payee_id, p.name, p.account, p.category] for p in mandate.payees],
        "invoices": [[r.invoice_id, r.vendor_id, r.po_id, r.item, r.qty, r.unit_price, r.amount,
                      r.note] for r in rows],
    }


class RunRequest(BaseModel):
    mode: Literal["live", "worst_case"] = "worst_case"
    preset: str = "pay"
    prompt: str = Field(default="", max_length=2000)


@app.post("/api/runs")
def create_run(body: RunRequest, request: Request) -> dict[str, Any]:
    if not docker_status().get("ready"):
        raise HTTPException(503, "the sandbox is not available on this server")
    if body.mode == "live" and not inference.configured():
        raise HTTPException(503, "Vultr Serverless Inference is not configured on this server")
    if body.mode == "worst_case" and body.preset != "pay":
        raise HTTPException(400, "the worst-case replay exists for the three-way match task only")
    if not limiter.allow(_client(request)):
        raise HTTPException(429, "too many runs from this address; wait a few minutes")
    try:
        run = manager.create(body.mode, body.preset, body.prompt, _public_base(request))
    except PermissionError as exc:
        raise HTTPException(429, str(exc)) from exc
    return {"run_id": run.run_id}


def _run_or_404(run_id: str):  # type: ignore[no-untyped-def]
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run (runs are kept for a limited time)")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, after: int = -1) -> dict[str, Any]:
    return manager.view(_run_or_404(run_id), after)


@app.post("/api/runs/{run_id}/escalations/{escalation_id}/{verdict}")
def decide(run_id: str, escalation_id: str, verdict: Literal["approve", "reject"]) -> dict[str, Any]:
    run = _run_or_404(run_id)
    try:
        return manager.decide(run, escalation_id, verdict == "approve")
    except KeyError as exc:
        raise HTTPException(404, "no such pending approval") from exc


@app.get("/api/runs/{run_id}/files/{name}")
def run_file(run_id: str, name: str) -> Response:
    run = _run_or_404(run_id)
    if not re.fullmatch(r"run\d+-[\w.-]{1,80}", name):
        raise HTTPException(404)
    path = run.directory / "files" / name
    if not path.is_file():
        raise HTTPException(404)
    if name.lower().endswith(".png"):
        return FileResponse(path, media_type="image/png")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/audit.jsonl")
def audit_file(run_id: str) -> PlainTextResponse:
    run = _run_or_404(run_id)
    text = (run.directory / "audit.jsonl").read_text(encoding="utf-8")
    return PlainTextResponse(text, headers={
        "Content-Disposition": f'attachment; filename="hufu-audit-{run_id}.jsonl"'})


@app.post("/api/selftest")
def start_selftest(request: Request) -> dict[str, Any]:
    if not docker_status().get("ready"):
        raise HTTPException(503, "the sandbox is not available on this server")
    if not limiter.allow("selftest:" + _client(request)):
        raise HTTPException(429, "too many self-tests from this address; wait a few minutes")
    try:
        test = selftests.start(sandbox_facing(_public_base(request)))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"test_id": test.test_id}


@app.get("/api/selftest/{test_id}")
def get_selftest(test_id: str) -> dict[str, Any]:
    test = selftests.tests.get(test_id)
    if test is None:
        raise HTTPException(404)
    return selftests.view(test)


@app.api_route("/api/honeypot/{token}", methods=["GET", "POST", "PUT"])
async def honeypot(token: str, request: Request) -> Response:
    """Anything that arrives here with a run's token got out of a box it should not have."""
    await request.body()
    manager.honeypot.hit(token[:64], _client(request))
    return JSONResponse({"received": True}, status_code=200)
