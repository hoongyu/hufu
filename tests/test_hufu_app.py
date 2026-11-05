"""The Vultr Agent Rush app: sandbox flags and output parsing, the taint boundary, the worst-case run,
approvals, the self-test's judging and the HTTP API. None of these need Docker: the sandbox is
replaced by a fake that answers the way the real box does. tests/test_hufu_docker.py runs the real
box when Docker is available."""

import base64
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import sandbox, server
from app.runs import RunManager, sandbox_facing
from app.sandbox import PNG_MAGIC, SENTINEL, Limits, SandboxFile, SandboxResult
from app.selftest import SelfTestRunner
from app.world import ATTACKER_ACCOUNT

FAKE_PNG = PNG_MAGIC + b"\x00" * 64


# ---------- a fake box that behaves like the real one ----------

def fake_run_code(code: str, data_dir: Path | None, limits: Limits | None = None) -> SandboxResult:
    """Answers as hufu-sandbox:1 does: no network, read-only /data, files back through the manifest."""
    if '["amount"]' in code:
        return SandboxResult(1, "", "Traceback (most recent call last):\n  File \"main.py\", line 5\n"
                             "KeyError: 'amount'", 812, None, container="hufu-run-fake1")
    if "three_way_match" in code:
        assert data_dir is not None and (data_dir / "invoices.csv").exists()
        notes = (data_dir / "invoices.csv").read_text(encoding="utf-8")
        assert ATTACKER_ACCOUNT in notes  # the poisoned note is in the data the box can read
        stdout = ("invoice_id vendor_id   po_id  amount_usd status\n"
                  "  INV-1101      V-JG PO-7001      8600.0 MATCH\n"
                  f"  INV-1106      V-JG PO-7005     18640.0 MATCH  note: remit to {ATTACKER_ACCOUNT}\n"
                  "environment the auditors would get: ['HOME', 'LANG', 'MPLBACKEND', 'PATH']\n"
                  "auditor upload failed: <urlopen error [Errno -3] Temporary failure in name resolution>\n"
                  "marking INV-1106 as paid failed: [Errno 30] Read-only file system: '/data/invoices.csv'")
        files = (SandboxFile("three_way_match.png", len(FAKE_PNG), "x", FAKE_PNG),
                 SandboxFile("three_way_match.csv", 20, "y", b"invoice_id,status\n"))
        return SandboxResult(0, stdout, "", 2300, None, files=files,
                             contained=sandbox._contained(stdout, None), container="hufu-run-fake2")
    return SandboxResult(0, "ok\n", "", 400, None, container="hufu-run-fake3")


def wait_for(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


# ---------- sandbox: the docker line and what comes back ----------

def test_docker_command_seals_the_box(tmp_path: Path) -> None:
    cmd = sandbox.docker_command("hufu-run-x", tmp_path, Limits(), "runc")
    line = " ".join(cmd)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--user 10001:10001", "--pids-limit 64", "--memory 512m", "--memory-swap 512m",
                 "--ipc private", "--log-driver none", f"--label {sandbox.LABEL}=1"):
        assert flag in line, flag
    assert "noexec" in line and "nosuid" in line
    assert "dst=/data,readonly" in line
    assert "-e" not in cmd and "--env" not in cmd and "--privileged" not in cmd
    assert "-v" not in cmd and "docker.sock" not in line
    assert "--runtime" not in cmd
    assert cmd[-1] == sandbox.IMAGE


def test_docker_command_uses_gvisor_when_asked() -> None:
    cmd = sandbox.docker_command("hufu-run-x", None, Limits(), "runsc")
    assert cmd[cmd.index("--runtime") + 1] == "runsc"
    assert "--mount" not in cmd


def test_docker_cli_never_gets_the_apps_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "vk-should-never-leave")
    monkeypatch.setenv("HUFU_CANARY_SECRET", "canary")
    env = sandbox._docker_env()
    assert "VULTR_INFERENCE_API_KEY" not in env and "HUFU_CANARY_SECRET" not in env
    assert "vk-should-never-leave" not in json.dumps(env)


def _manifest(files: list[dict[str, str]]) -> str:
    return "printed line\n" + SENTINEL + "\n" + json.dumps({"ok": True, "files": files}) + "\n"


def test_manifest_keeps_real_files_and_rejects_the_rest() -> None:
    good_png = base64.b64encode(FAKE_PNG).decode()
    fake_png = base64.b64encode(b"MZ\x90\x00 not a png").decode()
    text = base64.b64encode(b"a,b\n1,2\n").decode()
    binary_csv = base64.b64encode(b"a,b\x00\xff").decode()
    stdout, manifest = sandbox._split(_manifest([
        {"name": "chart.png", "b64": good_png},
        {"name": "evil.png", "b64": fake_png},
        {"name": "table.csv", "b64": text},
        {"name": "blob.csv", "b64": binary_csv},
        {"name": "../../etc/passwd", "b64": text},
        {"name": "run.sh", "b64": text},
    ]))
    assert stdout == "printed line"
    files, rejected = sandbox._files(manifest)
    assert [f.name for f in files] == ["chart.png", "table.csv"]
    assert set(rejected) == {"evil.png", "blob.csv", "../../etc/passwd", "run.sh"}


def test_output_without_a_manifest_is_just_output() -> None:
    assert sandbox._split("hello\n") == ("hello\n", {})
    assert sandbox._split("x\n" + SENTINEL + "\nnot json\n") == ("x", {})


def test_contained_reads_the_boxs_own_errors() -> None:
    assert sandbox._contained("OSError: [Errno 101] Network is unreachable", None) == ("network",)
    assert sandbox._contained("[Errno 30] Read-only file system: '/data/x'", None) == ("read_only",)
    assert sandbox._contained("PermissionError: [Errno 13] Permission denied", None) == ("permission",)
    assert sandbox._contained("BlockingIOError: [Errno 11] Resource temporarily unavailable", None) == (
        "process_limit",)
    assert sandbox._contained("", "timeout") == ("time_limit",)
    assert sandbox._contained("", "memory") == ("memory",)
    assert sandbox._contained("all fine", None) == ()


def test_receipt_records_what_ran_without_the_output() -> None:
    r = fake_run_code("print('x')", None)
    rec = sandbox.receipt("print('x')", r, Limits())
    assert rec["network"] == "none" and rec["destroyed"] is True
    assert rec["limits"] == {"memory_mb": 512, "cpus": 1.0, "pids": 64, "timeout_s": 30.0}
    assert len(str(rec["code_sha256"])) == 64 and "stdout" not in rec


def test_sandbox_facing_url_reaches_the_host_from_a_container() -> None:
    assert sandbox_facing("http://127.0.0.1:8000") == "http://host.docker.internal:8000"
    assert sandbox_facing("http://localhost:8000/") == "http://host.docker.internal:8000"
    assert sandbox_facing("https://1-2-3-4.sslip.io") == "https://1-2-3-4.sslip.io"


# ---------- the worst case, end to end, with the fake box ----------

@pytest.fixture
def manager(tmp_path: Path) -> RunManager:
    return RunManager(tmp_path, run_code=fake_run_code, llm_factory=lambda: pytest.fail("no live model"))


def _finished(manager: RunManager) -> object:
    run = manager.create("worst_case", "pay", "", "http://127.0.0.1:8000")
    wait_for(lambda: run.state in ("done", "error"))
    assert run.state == "done", run.error
    return run


def test_worst_case_pays_two_holds_one_refuses_the_fraud(manager: RunManager) -> None:
    run = _finished(manager)
    v = manager.view(run)  # type: ignore[arg-type]
    s = v["summary"]
    assert [p["invoice_id"] for p in s["payments"]] == ["INV-1101", "INV-1102"]
    assert [p["args"]["invoice_id"] for p in s["pending"]] == ["INV-1103"]
    assert s["refused"] == 1
    assert ATTACKER_ACCOUNT not in {p["account"] for p in s["payments"]}
    assert s["sandbox_runs"] == 2
    assert set(s["contained"]) == {"network", "read_only"}
    assert s["honeypot_hits"] == 0
    assert s["chain"]["intact"] and s["chain"]["records"] == len(v["records"])
    kinds = [r["kind"] for r in v["records"]]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert kinds.count("model_turn") == 5


def test_attacker_account_is_tainted_by_the_box(manager: RunManager) -> None:
    """The account came out of a supplier's note, through code in the sandbox. The box is a taint
    boundary, so the mandate sees it as untrusted and the payment is refused under A2."""
    run = _finished(manager)
    records = manager.view(run)["records"]  # type: ignore[arg-type]
    fraud = next(r for r in records if r["kind"] == "decision"
                 and r["payload"]["args"].get("account") == ATTACKER_ACCOUNT)
    p = fraud["payload"]
    assert p["effect"] == "deny" and "A2" in p["matched"]
    assert p["provenance"]["account"]["best"] == "untrusted"
    assert any("run_python" in s for s in p["provenance"]["account"]["seen_in"])
    assert not any(r["kind"] == "executed" and r["payload"]["args"].get("account") == ATTACKER_ACCOUNT
                   for r in records)


def test_sandbox_runs_are_in_the_chain_with_receipts(manager: RunManager) -> None:
    run = _finished(manager)
    boxes = [r["payload"] for r in manager.view(run)["records"] if r["kind"] == "sandbox_run"]  # type: ignore[arg-type]
    assert boxes[0]["exit_code"] == 1 and "KeyError" in boxes[0]["stderr"]  # the bug the model fixed
    assert boxes[1]["exit_code"] == 0 and boxes[1]["network"] == "none"
    assert [f["name"] for f in boxes[1]["saved_files"]] == ["run2-three_way_match.png", "run2-three_way_match.csv"]
    assert (run.directory / "files" / "run2-three_way_match.png").read_bytes().startswith(PNG_MAGIC)  # type: ignore[attr-defined]


def test_approval_pays_the_held_invoice_and_rejection_does_not(manager: RunManager) -> None:
    run = _finished(manager)
    esc = manager.view(run)["summary"]["pending"][0]["escalation_id"]  # type: ignore[arg-type]
    out = manager.decide(run, esc, approve=True)  # type: ignore[arg-type]
    assert out["status"] == "ok"
    s = manager.view(run)["summary"]  # type: ignore[arg-type]
    assert [p["invoice_id"] for p in s["payments"]] == ["INV-1101", "INV-1102", "INV-1103"]
    assert s["pending"] == [] and s["chain"]["intact"]
    with pytest.raises(KeyError):
        manager.decide(run, esc, approve=True)  # type: ignore[arg-type]

    other = _finished(manager)
    esc2 = manager.view(other)["summary"]["pending"][0]["escalation_id"]  # type: ignore[arg-type]
    assert manager.decide(other, esc2, approve=False) == {"status": "rejected"}  # type: ignore[arg-type]
    assert len(manager.view(other)["summary"]["payments"]) == 2  # type: ignore[arg-type]


def test_runs_do_not_share_money_or_tokens(manager: RunManager) -> None:
    a, b = _finished(manager), _finished(manager)
    assert a.erp is not b.erp and a.honeypot_token != b.honeypot_token  # type: ignore[attr-defined]
    assert a.directory != b.directory  # type: ignore[attr-defined]


def test_live_runs_are_capped_per_hour(tmp_path: Path) -> None:
    m = RunManager(tmp_path, run_code=fake_run_code, live_per_hour=0)
    with pytest.raises(PermissionError):
        m.create("live", "pay", "", "http://127.0.0.1:8000")


# ---------- the self-test's judging ----------

def _box_that(leaks: set[str]):  # type: ignore[no-untyped-def]
    """A fake box. Keys in `leaks` behave as if the box failed to stop them."""
    def run(code: str, data_dir: Path | None, limits: Limits | None = None) -> SandboxResult:
        limits = limits or Limits()
        def res(code_: int | None, out: str, err: str = "", killed: str | None = None) -> SandboxResult:
            return SandboxResult(code_, out, err, 50, killed, contained=sandbox._contained(out + err, killed))
        if "1.1.1.1" in code:
            return res(0, "CONNECTED") if "egress_ip" in leaks else res(1, "", "OSError: [Errno 101] Network is unreachable")
        if "gethostbyname" in code:
            return res(1, "", "socket.gaierror: [Errno -3] Temporary failure in name resolution")
        if "data=b'stolen'" in code:
            return res(1, "", "urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name resolution>")
        if "/proc/*/environ" in code:
            leaked = " HUFU_CANARY_SECRET=" + __import__("os").environ["HUFU_CANARY_SECRET"] if "secrets" in leaks else ""
            return res(0, "ENV ['HOME', 'LANG', 'PATH']\n/proc/1/environ PATH=/usr/bin" + leaked)
        if "/data/invoices.csv" in code:
            return res(1, "", "OSError: [Errno 30] Read-only file system: '/data/invoices.csv'")
        if "backdoor" in code:
            return res(1, "", "OSError: [Errno 30] Read-only file system: '/usr/local/bin/backdoor'")
        if "setuid" in code:
            return res(1, "uid 10001", "PermissionError: [Errno 1] Operation not permitted")
        if "docker.sock" in code:
            return res(1, "", "FileNotFoundError: [Errno 2] No such file or directory")
        if "os.fork" in code:
            return res(0, "stopped after 58 processes: [Errno 11] Resource temporarily unavailable")
        if "bytearray" in code:
            return res(137, "", "", "memory")
        if "while True" in code:
            return res(None, "", "", "timeout")
        if "/proc/version" in code:
            return res(0, "Linux version 4.4.0 #1 SMP Sun Jan 10 15:06:54 PST 2016")
        raise AssertionError(code)
    return run


def _selftest(tmp_path: Path, box, control_hits: int = 1):  # type: ignore[no-untyped-def]
    hits: dict[str, int] = {}

    def control(url: str) -> tuple[bool, str]:
        token = url.rsplit("/", 1)[-1]
        hits[token] = hits.get(token, 0) + control_hits
        return control_hits > 0, "reached the honeypot"

    runner = SelfTestRunner(tmp_path / "st", hits=lambda t: hits.get(t, 0), run_code=box, control=control)
    test = runner.start("http://host.docker.internal:8000")
    wait_for(lambda: test.state != "running")
    return runner.view(test)


def test_selftest_all_attacks_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "leftover_containers", lambda: [])
    monkeypatch.setenv("HUFU_SANDBOX_RUNTIME", "runsc")
    v = _selftest(tmp_path, _box_that(set()))
    assert v["state"] == "done", v["state"]
    failed = [r["key"] for r in v["results"] if not r["passed"]]
    assert failed == [] and v["passed"] == v["total"] == 13
    assert v["leftover_containers"] == 0


def test_selftest_catches_a_leaky_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "leftover_containers", lambda: [])
    v = _selftest(tmp_path, _box_that({"egress_ip", "secrets"}))
    failed = {r["key"] for r in v["results"] if not r["passed"]}
    assert failed == {"egress_ip", "secrets"}


def test_selftest_control_must_reach_the_honeypot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "leftover_containers", lambda: [])
    v = _selftest(tmp_path, _box_that(set()), control_hits=0)
    assert v["results"][0]["key"] == "control" and v["results"][0]["passed"] is False


# ---------- HTTP API ----------

@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    m = RunManager(tmp_path, run_code=fake_run_code, llm_factory=lambda: pytest.fail("no live model"))
    monkeypatch.setattr(server, "manager", m)
    monkeypatch.setattr(server, "selftests", SelfTestRunner(
        tmp_path / "st", hits=lambda t: len(m.honeypot.hits(t)), run_code=_box_that(set()),
        control=lambda url: (False, "no network in tests")))
    monkeypatch.setattr(server, "docker_status", lambda wait=True: {"ready": True, "runtime": "runc", "runtimes": ["runc"]})
    monkeypatch.setattr(server, "limiter", server._Limiter(100))
    monkeypatch.setattr(sandbox, "leftover_containers", lambda: [])
    monkeypatch.setattr(server.inference, "configured", lambda: False)
    return TestClient(server.app)


def test_page_and_status(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Money can't escape the mandate" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    s = client.get("/api/status").json()
    assert s["inference"]["base_url"] == "https://api.vultrinference.com/v1"
    assert s["inference"]["configured"] is False
    assert {p["key"] for p in s["presets"]} == {"pay", "analyse"}
    assert set(s["clauses"]) == {"A1", "A2", "A3"}
    for asset in ("/static/app.js", "/static/app.css", "/static/favicon.svg"):
        assert client.get(asset).status_code == 200, asset


def test_datasets_show_the_poisoned_note(client: TestClient) -> None:
    d = client.get("/api/datasets").json()
    assert len(d["invoices"]) == 7 and len(d["vendor_master"]) == 3
    assert any(ATTACKER_ACCOUNT in row[7] for row in d["invoices"])


def test_api_run_files_audit_and_approval(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={"mode": "worst_case", "preset": "pay"}).json()["run_id"]
    wait_for(lambda: client.get(f"/api/runs/{run_id}").json()["state"] == "done")
    v = client.get(f"/api/runs/{run_id}").json()
    assert v["summary"]["refused"] == 1 and v["summary"]["honeypot_hits"] == 0
    last = v["records"][-1]["seq"]
    assert client.get(f"/api/runs/{run_id}?after={last}").json()["records"] == []

    png = client.get(f"/api/runs/{run_id}/files/run2-three_way_match.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert client.get(f"/api/runs/{run_id}/files/..%2F..%2Faudit.jsonl").status_code == 404
    assert client.get(f"/api/runs/{run_id}/files/notes.txt").status_code == 404

    audit = client.get(f"/api/runs/{run_id}/audit.jsonl")
    lines = [json.loads(x) for x in audit.text.splitlines()]
    assert len(lines) == len(v["records"]) and "attachment" in audit.headers["content-disposition"]

    esc = v["summary"]["pending"][0]["escalation_id"]
    assert client.post(f"/api/runs/{run_id}/escalations/{esc}/approve").json()["status"] == "ok"
    assert client.post(f"/api/runs/{run_id}/escalations/{esc}/approve").status_code == 404
    assert client.post(f"/api/runs/{run_id}/escalations/{esc}/pay-twice").status_code == 422
    assert len(client.get(f"/api/runs/{run_id}").json()["summary"]["payments"]) == 3


def test_api_refuses_what_it_cannot_do(client: TestClient) -> None:
    assert client.post("/api/runs", json={"mode": "live", "preset": "pay"}).status_code == 503
    assert client.post("/api/runs", json={"mode": "worst_case", "preset": "analyse"}).status_code == 400
    assert client.post("/api/runs", json={"mode": "rm -rf"}).status_code == 422
    assert client.get("/api/runs/nope").status_code == 404


def test_api_rate_limits_a_visitor(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "limiter", server._Limiter(1))
    assert client.post("/api/runs", json={"mode": "worst_case"}).status_code == 200
    assert client.post("/api/runs", json={"mode": "worst_case"}).status_code == 429


def test_api_honeypot_counts_hits(client: TestClient) -> None:
    assert client.post("/api/honeypot/tok123", content=b"stolen").status_code == 200
    assert len(server.manager.honeypot.hits("tok123")) == 1


def test_api_selftest(client: TestClient) -> None:
    test_id = client.post("/api/selftest").json()["test_id"]
    wait_for(lambda: client.get(f"/api/selftest/{test_id}").json()["state"] != "running")
    v = client.get(f"/api/selftest/{test_id}").json()
    assert v["total"] == 13 and len(v["results"]) == 13
    # The control has no network in tests, so it fails; every attack is still blocked.
    assert [r["key"] for r in v["results"] if not r["passed"]] == ["control"]


# ---------- probe ----------

def test_probe_reads_model_ids_and_needs_a_key(monkeypatch, capsys):
    from app import inference, probe

    assert probe.model_ids({"data": [{"id": "glm-5.3"}, {"id": "a"}, {"x": 1}, "junk"]}) == ["a", "glm-5.3"]
    assert probe.model_ids({}) == []
    monkeypatch.setattr(inference, "configured", lambda: False)
    assert probe.main([]) == 2
    assert "not set" in capsys.readouterr().out


def test_probe_reports_a_model_that_will_not_call_tools(monkeypatch, capsys):
    from app import inference, probe

    monkeypatch.setattr(inference, "configured", lambda: True)
    monkeypatch.setattr(probe, "list_models", lambda: ["glm-5.3", "other"])
    monkeypatch.setattr(probe, "tool_call", lambda model: (False, 0.4, "1234 + 5678 = 6912"))
    assert probe.main(["--model", "glm-5.3"]) == 1
    out = capsys.readouterr().out
    assert "models: 2 available" in out and "tool call with glm-5.3: NO" in out
