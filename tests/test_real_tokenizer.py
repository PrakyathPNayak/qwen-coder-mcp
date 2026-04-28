"""Loop 268: real-tokenizer-backed token estimation.

Verifies that when ``QWEN_REAL_TOKENIZER`` is set, ``_estimate_tokens``
uses the lazy-loaded HuggingFace tokenizer for exact counts; otherwise
it falls back to the char heuristic. Tests never actually load
``transformers`` -- they monkeypatch ``_real_tokenizer`` to return a fake.
"""
from __future__ import annotations

import sys
import pytest


class _FakeTok:
    def __init__(self, ratio: float = 2.0):
        # Pretend the tokenizer produces 1 token per `ratio` characters.
        self.ratio = ratio
        self.calls = 0

    def encode(self, text: str, add_special_tokens: bool = False):
        self.calls += 1
        n = max(1, int(len(text) / self.ratio))
        return [0] * n


class _BrokenTok:
    def encode(self, text: str, add_special_tokens: bool = False):
        raise RuntimeError("tokenizer kaboom")


class TestRealTokenizerName:
    def test_default_empty_disables_real_mode(self, monkeypatch):
        monkeypatch.delenv("QWEN_REAL_TOKENIZER", raising=False)
        from qwen_coder_mcp.qwen_client import _real_tokenizer_name
        assert _real_tokenizer_name() == ""

    def test_env_var_passthrough(self, monkeypatch):
        monkeypatch.setenv("QWEN_REAL_TOKENIZER", "Qwen/Qwen3-Next-80B")
        from qwen_coder_mcp.qwen_client import _real_tokenizer_name
        assert _real_tokenizer_name() == "Qwen/Qwen3-Next-80B"

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("QWEN_REAL_TOKENIZER", "  some/model  ")
        from qwen_coder_mcp.qwen_client import _real_tokenizer_name
        assert _real_tokenizer_name() == "some/model"


class TestEstimateTokensFallback:
    def test_empty_returns_zero(self, monkeypatch):
        monkeypatch.delenv("QWEN_REAL_TOKENIZER", raising=False)
        from qwen_coder_mcp.qwen_client import _estimate_tokens
        assert _estimate_tokens("") == 0
        assert _estimate_tokens(None) == 0  # type: ignore[arg-type]

    def test_no_env_uses_char_heuristic(self, monkeypatch):
        monkeypatch.delenv("QWEN_REAL_TOKENIZER", raising=False)
        monkeypatch.setenv("QWEN_CHARS_PER_TOKEN", "3")
        from qwen_coder_mcp.qwen_client import _estimate_tokens
        # 300 chars / 3 = 100 tokens
        assert _estimate_tokens("x" * 300) == 100

    def test_no_env_does_not_import_transformers(self, monkeypatch):
        # Module-level: don't even touch transformers if no env set.
        monkeypatch.delenv("QWEN_REAL_TOKENIZER", raising=False)
        # Pre-clear any cached transformers to be sure.
        for mod_name in list(sys.modules):
            if mod_name == "transformers" or mod_name.startswith("transformers."):
                del sys.modules[mod_name]
        from qwen_coder_mcp.qwen_client import _estimate_tokens
        _estimate_tokens("hello world this is plain text")
        assert "transformers" not in sys.modules


class TestEstimateTokensWithRealTokenizer:
    def test_uses_fake_tokenizer_when_env_set(self, monkeypatch):
        monkeypatch.setenv("QWEN_REAL_TOKENIZER", "fake/model")
        from qwen_coder_mcp import qwen_client
        fake = _FakeTok(ratio=2.0)
        monkeypatch.setattr(qwen_client, "_real_tokenizer", lambda name: fake)
        # 100 chars at 2 chars/tok = 50 tokens, NOT the char heuristic's value.
        assert qwen_client._estimate_tokens("y" * 100) == 50
        assert fake.calls == 1

    def test_falls_back_when_tokenizer_returns_none(self, monkeypatch):
        monkeypatch.setenv("QWEN_REAL_TOKENIZER", "fake/model")
        monkeypatch.setenv("QWEN_CHARS_PER_TOKEN", "4")
        from qwen_coder_mcp import qwen_client
        monkeypatch.setattr(qwen_client, "_real_tokenizer", lambda name: None)
        # Should fall back to char heuristic (400/4 = 100).
        assert qwen_client._estimate_tokens("z" * 400) == 100

    def test_falls_back_when_encode_raises(self, monkeypatch):
        monkeypatch.setenv("QWEN_REAL_TOKENIZER", "fake/model")
        monkeypatch.setenv("QWEN_CHARS_PER_TOKEN", "4")
        from qwen_coder_mcp import qwen_client
        monkeypatch.setattr(qwen_client, "_real_tokenizer", lambda name: _BrokenTok())
        # Must not raise; fallback to char heuristic (400/4 = 100).
        assert qwen_client._estimate_tokens("z" * 400) == 100

    def test_minimum_one_token_for_nonempty(self, monkeypatch):
        monkeypatch.setenv("QWEN_REAL_TOKENIZER", "fake/model")
        from qwen_coder_mcp import qwen_client

        class _ZeroTok:
            def encode(self, text, add_special_tokens=False):
                return []

        monkeypatch.setattr(qwen_client, "_real_tokenizer", lambda name: _ZeroTok())
        # Even if tokenizer returns empty list, non-empty input yields >=1.
        assert qwen_client._estimate_tokens("a") == 1


class TestRealTokenizerLoader:
    def test_returns_none_for_empty_name(self):
        from qwen_coder_mcp.qwen_client import _real_tokenizer
        assert _real_tokenizer("") is None

    def test_returns_none_when_transformers_missing(self, monkeypatch):
        # Force ImportError for transformers.
        from qwen_coder_mcp import qwen_client
        # Clear lru_cache so this call isn't satisfied from a prior load.
        qwen_client._real_tokenizer.cache_clear()
        # Inject a fake transformers module that raises on AutoTokenizer access.
        import types
        fake_mod = types.ModuleType("transformers")

        def _bad(*a, **kw):
            raise RuntimeError("nope")

        class _FakeAuto:
            from_pretrained = staticmethod(_bad)

        fake_mod.AutoTokenizer = _FakeAuto
        monkeypatch.setitem(sys.modules, "transformers", fake_mod)
        assert qwen_client._real_tokenizer("nonexistent/model-xyz") is None
        qwen_client._real_tokenizer.cache_clear()

    def test_cached_per_name(self, monkeypatch):
        # Two calls with the same name should hit the lru_cache.
        from qwen_coder_mcp import qwen_client
        qwen_client._real_tokenizer.cache_clear()

        load_count = {"n": 0}

        class _Sentinel:
            pass

        sentinel = _Sentinel()
        import types
        fake_mod = types.ModuleType("transformers")

        class _FakeAuto:
            @staticmethod
            def from_pretrained(name, trust_remote_code=False):
                load_count["n"] += 1
                return sentinel

        fake_mod.AutoTokenizer = _FakeAuto
        monkeypatch.setitem(sys.modules, "transformers", fake_mod)
        a = qwen_client._real_tokenizer("foo/bar")
        b = qwen_client._real_tokenizer("foo/bar")
        assert a is sentinel and b is sentinel
        assert load_count["n"] == 1
        qwen_client._real_tokenizer.cache_clear()


class TestReadmeAndDocs:
    def test_env_var_documented_or_at_least_referenceable(self):
        # Soft check: the constant should exist and be callable; full
        # README docs can come in a follow-up. Lock the public API name.
        from qwen_coder_mcp.qwen_client import _real_tokenizer_name, _real_tokenizer
        assert callable(_real_tokenizer_name)
        assert callable(_real_tokenizer)

    def test_readme_documents_real_tokenizer_knob(self):
        # Loop 271: README must mention the env var so operators
        # can discover it. Locks against accidental doc drift.
        from pathlib import Path
        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        assert "`QWEN_REAL_TOKENIZER`" in text
        assert "transformers" in text.lower()
