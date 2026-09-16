"""Token counting helpers.

The benchmark and the indexer both need a quick, deterministic way to
estimate how many tokens a piece of text will cost the LLM. The default
is a small language-aware heuristic; if ``tiktoken`` is installed we
prefer it because it matches the *actual* tokenization of the most
common OpenAI / Anthropic models closely enough to make the savings
numbers meaningful for capacity planning.

This module is **imported by the indexer, the router, and the
benchmark**, so all three measure with the *same* definition. That is
the only way the "savings" ratio is honest.
"""

from __future__ import annotations

# Module-level cache so we only attempt to load tiktoken once.
_TOKENIZER = None
_TOKENIZER_LOADED = False
_BACKEND_NAME: str = "heuristic"


def _load_tokenizer():
    """Try to load a cl100k_base tokenizer. Returns (tokenizer, name) or (None, reason)."""
    global _TOKENIZER, _TOKENIZER_LOADED, _BACKEND_NAME
    if _TOKENIZER_LOADED:
        return _TOKENIZER, _BACKEND_NAME
    _TOKENIZER_LOADED = True
    try:
        import tiktoken  # type: ignore[import-untyped]
    except ImportError:
        _BACKEND_NAME = "heuristic (tiktoken not installed; pip install tiktoken)"
        return None, _BACKEND_NAME
    try:
        _TOKENIZER = tiktoken.get_encoding("cl100k_base")
        _BACKEND_NAME = "tiktoken/cl100k_base"
        return _TOKENIZER, _BACKEND_NAME
    except Exception as exc:  # pragma: no cover - defensive
        _BACKEND_NAME = f"heuristic (tiktoken error: {exc})"
        return None, _BACKEND_NAME


def backend() -> str:
    """Name of the active token-counting backend. Useful in benchmark output."""
    if _TOKENIZER is None and _BACKEND_NAME == "heuristic":
        _load_tokenizer()
    return _BACKEND_NAME


def count_tokens(text: str) -> int:
    """Count tokens in ``text`` using the best available backend.

    Priority:
    1. ``tiktoken`` with ``cl100k_base`` (the GPT-3.5/4 family). This
       is the same tokenizer the OpenAI API itself uses, so the
       *budget* an LLM client sets will match the *budget* we report.
    2. A language-aware heuristic that uses ``2.0`` for non-ASCII text
       and ``1.3`` for ASCII-only text. Honest enough for capacity
       *planning*, wrong enough for billing.

    The function never raises; if the input is empty, returns 0.
    """
    if not text:
        return 0
    tok, _ = _load_tokenizer()
    if tok is not None:
        # ``encode_ordinary`` skips the special tokens added by
        # ``encode``, which we don't need for length measurement.
        try:
            return len(tok.encode_ordinary(text))
        except Exception:
            pass
    # Heuristic fallback.
    has_non_ascii = any(ord(c) > 127 for c in text[:1024])
    factor = 2.0 if has_non_ascii else 1.3
    return int(len(text.split()) * factor)
