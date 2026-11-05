"""The box: every piece of model-written code runs in a fresh, locked-down container that is
destroyed when the run ends.

    no network          --network none: no interface but loopback, so no egress and no DNS
    no secrets          nothing from the app's environment is passed in; the harness drops the rest
    read-only           the image's root filesystem and the data mount; only a small noexec /tmp
    no privileges       non-root user, every capability dropped, no-new-privileges
    limits              memory (no swap), CPU, process count, open files, wall-clock time
    lifecycle           --rm, a unique name and label; killed on timeout; nothing survives a run
    kernel isolation    gVisor (runsc) when HUFU_SANDBOX_RUNTIME=runsc, as on the Vultr VM

The host never trusts what comes out: stdout and stderr are capped, and the files the harness reports
are checked by type and size before anyone sees them."""

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SENTINEL = "@@HUFU-MANIFEST@@"
IMAGE = os.environ.get("HUFU_SANDBOX_IMAGE", "hufu-sandbox:1")
LABEL = "hufu.sandbox"
MAX_STDOUT, MAX_STDERR = 64_000, 16_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TEXT_TYPES = (".csv", ".json", ".txt", ".md")

# What the sandbox reported when it stopped something, matched on its own error text.
_SIGNS: tuple[tuple[str, str], ...] = (
    (r"Network is unreachable|Temporary failure in name resolution|Name or service not known"
     r"|No address associated with hostname|nodename nor servname", "network"),
    (r"Read-only file system", "read_only"),
    (r"Operation not permitted|Permission denied", "permission"),
    (r"Resource temporarily unavailable|can't start new thread", "process_limit"),
    (r"MemoryError|Cannot allocate memory", "memory"),
)


@dataclass(frozen=True)
class Limits:
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 64
    timeout_s: float = 30.0
    tmp_mb: int = 64


@dataclass(frozen=True)
class SandboxFile:
    name: str
    size: int
    sha256: str
    data: bytes


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    killed: str | None  # "timeout" or "memory" when the host or the kernel stopped the run
    files: tuple[SandboxFile, ...] = ()
    rejected_files: tuple[str, ...] = ()
    contained: tuple[str, ...] = ()  # what the box stopped, as reported by the sandbox itself
    runtime: str = "runc"
    container: str = ""
    command: tuple[str, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.killed is None


RunCodeFn = Callable[[str, Path | None, "Limits | None"], SandboxResult]


def runtime() -> str:
    return os.environ.get("HUFU_SANDBOX_RUNTIME", "").strip() or "runc"


def docker_command(name: str, data_dir: Path | None, limits: Limits, rt: str) -> list[str]:
    """The full `docker run` line. Kept a pure function so the tests can check every flag."""
    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--label", f"{LABEL}=1",
        "--network", "none",
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmp_mb}m,mode=1777",
        "--user", "10001:10001",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(limits.pids),
        "--memory", f"{limits.memory_mb}m",
        "--memory-swap", f"{limits.memory_mb}m",
        "--cpus", str(limits.cpus),
        "--ulimit", "nofile=256:256",
        "--ipc", "private",
        "--hostname", "sandbox",
        "--log-driver", "none",
    ]
    if rt != "runc":
        cmd += ["--runtime", rt]
    if data_dir is not None:
        cmd += ["--mount", f"type=bind,src={data_dir.resolve()},dst=/data,readonly"]
    return [*cmd, IMAGE]


def run_code(
    code: str, data_dir: Path | None = None, limits: Limits | None = None
) -> SandboxResult:
    limits = limits or Limits()
    rt = runtime()
    name = f"hufu-run-{secrets.token_hex(6)}"
    cmd = docker_command(name, data_dir, limits, rt)
    start = time.monotonic()
    killed: str | None = None
    try:
        proc = subprocess.run(
            cmd,
            input=code.encode("utf-8"),
            capture_output=True,
            timeout=limits.timeout_s,
            env=_docker_env(),
        )
        exit_code: int | None = proc.returncode
        out, err = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        _kill(name)
        killed, exit_code = "timeout", None
        out, err = exc.stdout or b"", exc.stderr or b""
    duration_ms = int((time.monotonic() - start) * 1000)
    stdout, manifest = _split(out.decode("utf-8", "replace"))
    stderr = err.decode("utf-8", "replace")
    if killed is None and exit_code == 137:
        killed = "memory"  # SIGKILL with no timeout from us: the kernel's OOM killer
    files, rejected = _files(manifest)
    return SandboxResult(
        exit_code=exit_code,
        stdout=_cap(stdout, MAX_STDOUT),
        stderr=_cap(stderr, MAX_STDERR),
        duration_ms=duration_ms,
        killed=killed,
        files=files,
        rejected_files=rejected,
        contained=_contained(stdout + "\n" + stderr, killed),
        runtime=rt,
        container=name,
        command=tuple(cmd),
    )


def receipt(code: str, result: SandboxResult, limits: Limits | None = None) -> dict[str, object]:
    """What goes into the audit chain for one run: enough to show what ran, where and how it ended,
    without storing the output twice."""
    limits = limits or Limits()
    return {
        "container": result.container,
        "image": IMAGE,
        "runtime": result.runtime,
        "network": "none",
        "limits": {
            "memory_mb": limits.memory_mb,
            "cpus": limits.cpus,
            "pids": limits.pids,
            "timeout_s": limits.timeout_s,
        },
        "code_sha256": _sha(code.encode("utf-8")),
        "stdout_sha256": _sha(result.stdout.encode("utf-8")),
        "exit_code": result.exit_code,
        "killed": result.killed,
        "duration_ms": result.duration_ms,
        "contained": list(result.contained),
        "files": [{"name": f.name, "size": f.size, "sha256": f.sha256} for f in result.files],
        "destroyed": True,  # --rm, and _kill on timeout
    }


def leftover_containers() -> list[str]:
    """Sandbox containers that still exist. Should always be empty between runs."""
    proc = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={LABEL}"],
        capture_output=True, text=True, timeout=15, env=_docker_env(),
    )
    return proc.stdout.split()


def cleanup() -> int:
    ids = leftover_containers()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=30,
                       env=_docker_env())
    return len(ids)


def docker_info() -> dict[str, object]:
    """Whether the sandbox can run here: Docker reachable, image present, which runtimes exist."""
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=15, env=_docker_env(),
        )
        if info.returncode != 0:
            return {"ready": False, "error": "docker is not reachable"}
        data = json.loads(info.stdout)
        image = subprocess.run(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=15, env=_docker_env(),
        )
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"ready": False, "error": "docker is not installed or not responding"}
    runtimes = sorted((data.get("Runtimes") or {}).keys())
    rt = runtime()
    ready = image.returncode == 0 and (rt == "runc" or rt in runtimes)
    return {
        "ready": ready,
        "server_version": data.get("ServerVersion"),
        "runtime": rt,
        "runtimes": runtimes,
        "image": IMAGE,
        "image_id": image.stdout.strip()[:19] if image.returncode == 0 else None,
        "error": None if ready else f"image {IMAGE} missing or runtime {rt} not installed",
    }


def _docker_env() -> dict[str, str]:
    """The docker CLI gets only what it needs to find the daemon, never the app's secrets."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "PROGRAMDATA", "PROGRAMFILES", "TEMP", "TMP", "DOCKER_HOST", "DOCKER_CONFIG",
            "DOCKER_CONTEXT", "XDG_RUNTIME_DIR")
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def _kill(name: str) -> None:
    subprocess.run(["docker", "kill", name], capture_output=True, timeout=30, env=_docker_env())
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, env=_docker_env())


def _split(stdout: str) -> tuple[str, dict[str, object]]:
    head, sep, tail = stdout.rpartition("\n" + SENTINEL + "\n")
    if not sep:
        return stdout, {}
    try:
        manifest = json.loads(tail.strip().splitlines()[0]) if tail.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return head, {}
    return head, manifest if isinstance(manifest, dict) else {}


def _files(manifest: dict[str, object]) -> tuple[tuple[SandboxFile, ...], tuple[str, ...]]:
    good: list[SandboxFile] = []
    bad: list[str] = []
    raw = manifest.get("files")
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        b64 = entry.get("b64")
        if not re.fullmatch(r"[\w.-]{1,80}", name) or not isinstance(b64, str):
            bad.append(name[:80])
            continue
        try:
            data = base64.b64decode(b64, validate=True)
        except ValueError:
            bad.append(name)
            continue
        lower = name.lower()
        is_png = lower.endswith(".png") and data.startswith(PNG_MAGIC)
        is_text = lower.endswith(TEXT_TYPES) and _is_text(data)
        if not (is_png or is_text):
            bad.append(name)
            continue
        good.append(SandboxFile(name, len(data), _sha(data), data))
    return tuple(good), tuple(bad)


def _is_text(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in data


def _contained(text: str, killed: str | None) -> tuple[str, ...]:
    found = [kind for pattern, kind in _SIGNS if re.search(pattern, text)]
    if killed == "timeout":
        found.append("time_limit")
    if killed == "memory" and "memory" not in found:
        found.append("memory")
    return tuple(dict.fromkeys(found))


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more characters cut]"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
