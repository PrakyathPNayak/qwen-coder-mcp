"""Configuration loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional in some envs
    def load_dotenv(*_a, **_kw):  # type: ignore[no-redef]
        return False


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    model: str
    timeout: float
    max_tokens: int
    server_max_len: int
    loop_interval_seconds: int
    loop_max_file_bytes: int
    loop_push: bool


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None and val != "" else default


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from environment, optionally seeded by a `.env` file."""
    if env_file is None:
        env_file = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(env_file, override=False)

    return Settings(
        base_url=_env("QWEN_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/"),
        api_key=_env("QWEN_API_KEY", "EMPTY"),
        model=_env("QWEN_MODEL", "qwen3.6-27b"),
        timeout=float(_env("QWEN_TIMEOUT", "120")),
        # Default chosen so prompt+completion fits inside the serve
        # script's default max-model-len of 65536. vLLM rejects requests
        # where max_tokens > max_model_len with a VLLMValidationError,
        # so 16384 leaves ~49k tokens of prompt room. Override with
        # QWEN_MAX_TOKENS if you raised QWEN_SERVE_MAX_LEN on the server.
        # Loop 236: bumped 8192->16384 because Qwen3-Next emits long
        # <think>...</think> reasoning blocks; the prior cap was eating
        # answers mid-think and surfacing as "stops prematurely".
        max_tokens=int(_env("QWEN_MAX_TOKENS", "16384")),
        # Hard ceiling the client uses to clamp max_tokens before each
        # request. Should match the vllm --max-model-len the server was
        # started with. The default tracks scripts/serve_qwen.sh.
        server_max_len=int(_env("QWEN_SERVER_MAX_LEN", _env("QWEN_SERVE_MAX_LEN", "65536"))),
        loop_interval_seconds=int(_env("LOOP_INTERVAL_SECONDS", "45")),
        loop_max_file_bytes=int(_env("LOOP_MAX_FILE_BYTES", "60000")),
        loop_push=_env("LOOP_PUSH", "1") not in {"0", "false", "False", "no"},
    )
