"""Runs inside the sandbox. Reads the model's code from stdin, runs it, then prints a manifest of
the files it left in /tmp/out after a sentinel line, so no host directory has to be writable.

Everything this prints is untrusted: the code can print a fake sentinel or kill the harness. The host
treats the manifest as data, caps its size and accepts only a few file types."""

import base64
import json
import os
import shutil
import sys
import traceback

SENTINEL = "@@HUFU-MANIFEST@@"
OUT = "/tmp/out"
KEEP_ENV = ("PATH", "LANG", "MPLBACKEND")
ALLOWED = (".csv", ".png", ".json", ".txt", ".md")
MAX_FILE, MAX_TOTAL = 2_000_000, 4_000_000


def _sanitize_env() -> None:
    """Only what an interpreter needs. The host passes no secrets in; this also drops what the base
    image sets (such as the GPG key id used to verify the Python release)."""
    keep = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    os.environ.clear()
    os.environ.update(keep, HOME="/tmp", MPLCONFIGDIR="/tmp/mpl", PYTHONDONTWRITEBYTECODE="1")


def _manifest(ok: bool) -> dict[str, object]:
    files, total = [], 0
    for name in sorted(os.listdir(OUT)) if os.path.isdir(OUT) else []:
        path = os.path.join(OUT, name)
        if not os.path.isfile(path) or not name.lower().endswith(ALLOWED):
            continue
        size = os.path.getsize(path)
        if size > MAX_FILE or total + size > MAX_TOTAL:
            files.append({"name": name, "size": size, "skipped": "too large"})
            continue
        total += size
        with open(path, "rb") as fh:
            files.append({"name": name, "size": size, "b64": base64.b64encode(fh.read()).decode()})
    return {"ok": ok, "files": files}


def main() -> None:
    code = sys.stdin.read()
    _sanitize_env()
    shutil.copytree("/opt/mplconfig", "/tmp/mpl", dirs_exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs("/tmp/work", exist_ok=True)
    os.chdir("/tmp/work")
    ok = True
    try:
        exec(compile(code, "main.py", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:
        ok = exc.code in (None, 0)
    except BaseException:
        ok = False
        tb = traceback.format_exc().splitlines()
        # drop the harness's own frame so the model sees a traceback of its code only
        start = next((i for i, line in enumerate(tb) if 'File "main.py"' in line), 1)
        print("Traceback (most recent call last):", *tb[start:], sep="\n", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    print("\n" + SENTINEL)
    print(json.dumps(_manifest(ok)))
    sys.stdout.flush()
    sys.exit(0 if ok else 1)  # the exit code tells the host (and the model) the script failed


if __name__ == "__main__":
    main()
