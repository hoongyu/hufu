"""Where the address and key of the MoMA model service come from: the environment, or else the
project's .env file (git-ignored). The key is handed to LiteLLM and never printed.

    MOMA_API_BASE=https://.../v1     # as the MoMA console shows it, up to /v1
    MOMA_API_KEY=...
"""

import os
from pathlib import Path
from typing import Any

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
BASE, KEY = "MOMA_API_BASE", "MOMA_API_KEY"


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """NAME=value lines; blank lines, comments and a UTF-8 byte-order mark are ignored."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def options(path: Path = ENV_FILE) -> dict[str, Any]:
    """api_base and api_key for LiteLLM. The environment wins over the file; when something is
    missing, the error names it and never contains the key."""
    file = read_env_file(path)
    values = {name: os.environ.get(name) or file.get(name, "") for name in (BASE, KEY)}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"缺 {'、'.join(missing)}：照 README 用 PowerShell 存进 {path}")
    return {"api_base": values[BASE].rstrip("/"), "api_key": values[KEY]}


def model_name(name: str) -> str:
    """LiteLLM sends a model to an OpenAI-compatible address only with the openai/ prefix, and
    MoMA model ids may themselves contain a slash."""
    return name if name.startswith("openai/") else f"openai/{name}"
