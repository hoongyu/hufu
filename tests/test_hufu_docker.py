"""The real box: these run hufu-sandbox:1 under Docker and are skipped when Docker or the image is not
available. Build the image first:  docker build -t hufu-sandbox:1 sandbox/"""

import os
from pathlib import Path

import pytest

from app import sandbox
from app.sandbox import Limits
from app.selftest import SelfTestRunner

_info = sandbox.docker_info()
pytestmark = pytest.mark.skipif(not _info.get("ready"), reason=f"sandbox unavailable: {_info.get('error')}")


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    (d / "invoices.csv").write_text("invoice_id,amount_usd\nINV-1,10.50\nINV-2,4.25\n", encoding="utf-8")
    (d / "invoices.csv").chmod(0o644)
    d.chmod(0o755)
    return d


def test_runs_pandas_and_returns_files(data: Path) -> None:
    code = ("import pandas as pd, matplotlib.pyplot as plt\n"
            "df = pd.read_csv('/data/invoices.csv')\n"
            "print('TOTAL', df.amount_usd.sum())\n"
            "df.to_csv('/tmp/out/copy.csv', index=False)\n"
            "df.plot.bar(x='invoice_id', y='amount_usd'); plt.savefig('/tmp/out/chart.png')\n")
    r = sandbox.run_code(code, data)
    assert r.ok, r.stderr
    assert "TOTAL 14.75" in r.stdout
    assert sorted(f.name for f in r.files) == ["chart.png", "copy.csv"]
    assert next(f for f in r.files if f.name == "chart.png").data.startswith(sandbox.PNG_MAGIC)


def test_errors_come_back_for_the_model_to_fix(data: Path) -> None:
    r = sandbox.run_code("import pandas as pd\npd.read_csv('/data/invoices.csv')['amount']\n", data)
    assert r.exit_code == 1 and "KeyError" in r.stderr and 'File "main.py"' in r.stderr


def test_no_network_no_writes_no_secrets(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "vk-test-should-not-leak-1234567890")
    code = ("import os, socket\n"
            "print(sorted(os.environ.items()))\n"
            "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=3)\n    print('NET-OPEN')\n"
            "except OSError as e:\n    print('net:', e)\n"
            "try:\n    open('/data/invoices.csv', 'a').write('x')\n    print('WROTE')\n"
            "except OSError as e:\n    print('data:', e)\n")
    r = sandbox.run_code(code, data)
    assert "NET-OPEN" not in r.stdout and "WROTE" not in r.stdout
    assert "vk-test-should-not-leak" not in r.stdout
    assert {"network", "read_only"} <= set(r.contained)
    assert (data / "invoices.csv").read_text(encoding="utf-8").count("\n") == 3


def test_time_limit_kills_and_removes_the_container() -> None:
    r = sandbox.run_code("while True:\n    pass\n", None, Limits(timeout_s=4))
    assert r.killed == "timeout" and "time_limit" in r.contained
    assert r.container not in " ".join(sandbox.leftover_containers())


def test_selftest_blocks_every_attack(tmp_path: Path) -> None:
    runner = SelfTestRunner(tmp_path / "st", hits=lambda token: 0,
                            control=lambda url: (True, "control skipped in tests"))
    test = runner.start("http://host.docker.internal:9")
    import time
    end = time.monotonic() + 240
    while test.state == "running" and time.monotonic() < end:
        time.sleep(0.5)
    v = runner.view(test)
    assert v["state"] == "done", v["state"]
    failed = {r["key"]: r["evidence"] for r in v["results"] if r["key"] != "control" and not r["passed"]}
    assert failed == {}
    assert v["leftover_containers"] == 0
    assert os.environ.get("HUFU_CANARY_SECRET")  # the canary was set in this process all along
