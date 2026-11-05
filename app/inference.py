"""Vultr Serverless Inference: OpenAI-compatible, so the engine's LiteLLM adapter reaches it with an
openai/ prefix on the model id. The key comes from the environment (on the VM: the systemd
EnvironmentFile) or the git-ignored .env; it is never logged, never shown, never passed to a sandbox."""

import os
import threading
import time
from typing import Any

from engine.audit import AuditLog
from engine.llm import LLM, LiteLLMClient, LLMResponse, Message

from app.clock import now
from factory.moma import read_env_file

BASE_URL = "https://api.vultrinference.com/v1"
KEY_VAR, BASE_VAR, MODEL_VAR = "VULTR_INFERENCE_API_KEY", "VULTR_INFERENCE_BASE", "VULTR_MODEL"
DEFAULT_MODEL = "glm-5.3"


def _setting(name: str, default: str = "") -> str:
    return os.environ.get(name) or read_env_file().get(name, default)


def configured() -> bool:
    return bool(_setting(KEY_VAR))


def model_id() -> str:
    return _setting(MODEL_VAR, DEFAULT_MODEL)


def client(model: str | None = None) -> LLM:
    key = _setting(KEY_VAR)
    if not key:
        raise RuntimeError(f"{KEY_VAR} is not set; see README.md, Deploy to Vultr")
    return LiteLLMClient(
        f"openai/{model or model_id()}",
        api_base=_setting(BASE_VAR, BASE_URL).rstrip("/"),
        api_key=key,
        timeout=90,
        num_retries=2,
        temperature=0.2,
    )


class AuditedLLM:
    """Writes every model turn into the run's audit chain, so the chain tells the whole story: what
    the model said and asked for, what the box ran, what the mandate decided."""

    def __init__(self, inner: LLM, audit: AuditLog, label: str,
                 lock: "threading.RLock | None" = None) -> None:
        self.inner = inner
        self.audit = audit
        self.label = label
        self.lock = lock or threading.RLock()

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        start = time.monotonic()
        response = self.inner.complete(messages, tools)  # no lock while the model thinks
        with self.lock:
            self.audit.append(
                "model_turn",
                {
                    "model": self.label,
                    "content": response.content,
                    "tool_calls": [{"name": c.name, "arguments": _short(c.arguments)}
                                   for c in response.tool_calls],
                    "latency_ms": int((time.monotonic() - start) * 1000),
                },
                now(),
            )
        return response


def _short(args: dict[str, Any]) -> dict[str, Any]:
    """The code itself is stored with the sandbox run; the model turn keeps a pointer-sized copy."""
    return {k: (v[:160] + " ...") if isinstance(v, str) and len(v) > 200 else v
            for k, v in args.items()}
