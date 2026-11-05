"""Check the Vultr side before recording or deploying: is the key accepted, which models are there,
does the chosen model call tools, and (with --run) does a full live run go through end to end.

    uv run python -m app.probe                 # key, model list, one tool call
    uv run python -m app.probe --model qwen3.8-27b
    uv run python -m app.probe --run           # plus one live agent run with the real sandbox

The key is read from the environment or the git-ignored .env and is never printed."""

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app import inference

ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "add_numbers",
        "description": "Add two numbers and return the sum.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
}


def model_ids(payload: dict[str, Any]) -> list[str]:
    """The OpenAI-style /models answer: {"data": [{"id": ...}, ...]}."""
    return sorted(str(m.get("id")) for m in payload.get("data", []) if isinstance(m, dict) and m.get("id"))


def list_models() -> list[str]:
    base = inference._setting(inference.BASE_VAR, inference.BASE_URL).rstrip("/")
    request = urllib.request.Request(
        base + "/models", headers={"Authorization": "Bearer " + inference._setting(inference.KEY_VAR)})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed https URL)
        return model_ids(json.loads(response.read().decode("utf-8")))


def tool_call(model: str) -> tuple[bool, float, str]:
    start = time.monotonic()
    reply = inference.client(model).complete(
        [{"role": "user", "content": "What is 1234 + 5678? Use the add_numbers tool."}], [ADD_TOOL])
    took = time.monotonic() - start
    if reply.tool_calls:
        call = reply.tool_calls[0]
        return True, took, f"{call.name}({json.dumps(call.arguments)})"
    return False, took, (reply.content or "")[:160]


def live_run(model: str) -> dict[str, Any]:
    from app import sandbox
    from app.runs import RunManager

    info = sandbox.docker_info()
    if not info.get("ready"):
        raise RuntimeError(f"the sandbox is not ready here: {info.get('error')}")
    with tempfile.TemporaryDirectory(prefix="hufu-probe-") as tmp:
        manager = RunManager(Path(tmp), llm_factory=lambda: inference.client(model), workers=1)
        run = manager.create("live", "pay", "", "http://127.0.0.1:8000")
        deadline = time.monotonic() + 600
        while run.state in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(2)
        view = manager.view(run)
    summary = view["summary"]
    return {
        "state": view["state"],
        "error": view.get("error"),
        "paid": [p["invoice_id"] for p in summary["payments"]],
        "waiting": [(e.get("args") or {}).get("invoice_id") for e in summary["pending"]],
        "refused": summary["refused"],
        "contained": summary["contained"],
        "honeypot_hits": summary["honeypot_hits"],
        "sandbox_runs": summary.get("sandbox_runs"),
        "chain": summary["chain"],
        "final_text": (view.get("final_text") or "")[:400],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=None, help=f"model id (default: {inference.model_id()})")
    parser.add_argument("--run", action="store_true", help="also run the agent once, live, with the sandbox")
    args = parser.parse_args(argv)
    model = args.model or inference.model_id()

    if not inference.configured():
        print(f"{inference.KEY_VAR} is not set. Put it in .env (git-ignored) or the environment.")
        return 2
    print(f"key: set ({inference.KEY_VAR}; not shown)")

    try:
        ids = list_models()
    except urllib.error.HTTPError as exc:
        print(f"models: HTTP {exc.code}. {'The key was refused.' if exc.code in (401, 403) else ''}")
        return 1
    except OSError as exc:
        print(f"models: cannot reach the endpoint ({exc})")
        return 1
    print(f"models: {len(ids)} available")
    for name in ids:
        print(("  * " if name == model else "    ") + name)
    if model not in ids:
        print(f"warning: {model} is not in the list; set VULTR_MODEL to one of the names above")

    ok, took, detail = tool_call(model)
    print(f"tool call with {model}: {'yes' if ok else 'NO'} in {took:.1f} s -> {detail}")
    if not ok:
        print("this model did not call the tool; try another one with --model")
        return 1

    if args.run:
        print("live run: starting (one agent run, several model calls, real sandbox) ...")
        result = live_run(model)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["state"] == "done" and result["chain"].get("intact") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
