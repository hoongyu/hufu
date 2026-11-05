"""How long the enforcement point takes per action: the four episodes (scripted model) replayed many
times, timing each call that changes something (describe the action, trace its parameters, decide,
write the audit record, execute when allowed). Model latency is not included. One process, one
thread, on whatever machine runs it; the machine is written into the output.

uv run python -m evals.latency    # writes out/latency.json
"""

import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

from engine.llm import ScriptedLLM

from factory.episodes import run_episodes

OUT = Path(__file__).resolve().parent.parent / "out" / "latency.json"
SIDE_EFFECTS = {"create_po", "pay_invoice", "release_quality_hold"}
RUNS = 300


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    samples: list[float] = []
    import engine.pep as pep_module

    original = pep_module.PolicyEnforcementPoint.handle

    def timed(self, name, args):  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        out = original(self, name, args)
        if name in SIDE_EFFECTS:
            samples.append((time.perf_counter() - start) * 1000)
        return out

    pep_module.PolicyEnforcementPoint.handle = timed  # type: ignore[method-assign]
    try:
        run_episodes(lambda e: ScriptedLLM(e.fooled_script))  # warm-up, not counted
        samples.clear()
        for _ in range(RUNS):
            run_episodes(lambda e: ScriptedLLM(e.fooled_script))
    finally:
        pep_module.PolicyEnforcementPoint.handle = original  # type: ignore[method-assign]
    samples.sort()
    p99 = samples[min(len(samples) - 1, int(round(0.99 * len(samples))) - 1)]
    result = {
        "samples": len(samples),
        "runs": RUNS,
        "median_ms": round(statistics.median(samples), 3),
        "p99_ms": round(p99, 3),
        "max_ms": round(samples[-1], 3),
        "machine": f"{platform.processor() or platform.machine()}, {os.cpu_count()} logical CPUs, {platform.system()} {platform.release()}",
        "python": platform.python_version(),
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
