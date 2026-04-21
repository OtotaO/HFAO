"""PII redaction for ingested observation bodies.

SPEC §6.5. Two layers:

1. Always-on regex sweep over the built-in pattern set (emails, phone
   numbers, credit cards, SSN, AWS keys, Anthropic/OpenAI API keys, IP
   addresses). Matches are replaced with ``[REDACTED:{kind}]`` or, if
   ``hash_only=True``, with ``sha256:{first12}`` — the latter is useful
   for correlating repeated PII occurrences without storing the value.
2. Optional Presidio pass (``pip install hfao[presidio]``). Off by default;
   configurable entity list. Falls back silently if presidio isn't
   installed and ``use_presidio=True`` — a warning is emitted once.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, cast

import msgspec
from msgspec import Struct, field

from hfao.schema.events import Observation

log = logging.getLogger(__name__)

_DEFAULT_KINDS: frozenset[str] = frozenset(
    {"email", "phone", "cc", "ssn", "aws_key", "anthropic_key", "openai_key"}
)


def _default_kinds() -> set[str]:
    return set(_DEFAULT_KINDS)


def _default_presidio_entities() -> list[str]:
    return ["PERSON", "LOCATION"]


class RedactionConfig(Struct, kw_only=True):
    enabled: bool = True
    builtin_kinds: set[str] = field(default_factory=_default_kinds)
    use_presidio: bool = False
    presidio_entities: list[str] = field(default_factory=_default_presidio_entities)
    hash_only: bool = False


# Compiled regex registry. Order matters — specific patterns first so they
# don't get eaten by more general ones (API keys before generic IPs, etc.).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-api03-[A-Za-z0-9_-]{93,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("cc", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
    ("phone", re.compile(r"\+[1-9]\d{6,14}\b")),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


_presidio_warned = False


class _Repl:
    """Callable wrapper that binds a kind at construction time.

    Using a class instead of a closure avoids the loop-variable binding
    pitfall (ruff B023) without sacrificing readability.
    """

    __slots__ = ("_handler", "_kind")

    def __init__(self, handler: Any, kind: str) -> None:
        self._handler = handler
        self._kind = kind

    def __call__(self, m: re.Match[str]) -> str:
        result: str = self._handler(m.group(0), self._kind)
        return result


class Redactor:
    def __init__(self, config: RedactionConfig | None = None) -> None:
        self.config = config or RedactionConfig()
        self._presidio: Any | None = None
        if self.config.use_presidio:
            self._presidio = _load_presidio()

    def redact_text(self, text: str) -> str:
        if not self.config.enabled or not text:
            return text
        out = text
        for kind, pat in _PATTERNS:
            if kind not in self.config.builtin_kinds:
                continue
            handler = self._sub_cc if kind == "cc" else self._replacement
            out = pat.sub(_Repl(handler, kind), out)
        if self._presidio is not None:
            out = self._presidio_redact(out)
        return out

    def redact_observation(self, obs: Observation) -> Observation:
        if not self.config.enabled:
            return obs
        return msgspec.structs.replace(
            obs,
            input=self.redact_text(obs.input) if obs.input else obs.input,
            output=self.redact_text(obs.output) if obs.output else obs.output,
            status_message=self.redact_text(obs.status_message)
            if obs.status_message
            else obs.status_message,
        )

    # ---- internals ----

    def _replacement(self, value: str, kind: str) -> str:
        if self.config.hash_only:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            return f"sha256:{digest}"
        return f"[REDACTED:{kind}]"

    def _sub_cc(self, candidate: str, kind: str) -> str:
        digits = re.sub(r"[ -]", "", candidate)
        if not _luhn_ok(digits):
            return candidate  # not a valid card number; keep as-is
        return self._replacement(candidate, kind)

    def _presidio_redact(self, text: str) -> str:
        presidio: Any = self._presidio
        if presidio is None:
            return text
        try:
            results = presidio["analyzer"].analyze(
                text=text, entities=self.config.presidio_entities, language="en"
            )
            anonymized = presidio["anonymizer"].anonymize(text=text, analyzer_results=results)
            return str(anonymized.text)
        except Exception as e:  # noqa: BLE001
            log.warning("presidio redaction failed; returning pre-presidio text: %s", e)
            return text


def _luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _load_presidio() -> Any | None:
    # presidio has no pyright stubs; widen at the dynamic-import boundary.
    global _presidio_warned
    try:
        presidio_analyzer: Any = __import__("presidio_analyzer")
        presidio_anonymizer: Any = __import__("presidio_anonymizer")
    except ImportError:
        if not _presidio_warned:
            log.warning(
                "use_presidio=True but presidio is not installed; "
                "install `hfao[presidio]` to enable. Falling back to regex-only."
            )
            _presidio_warned = True
        return None
    return {
        "analyzer": presidio_analyzer.AnalyzerEngine(),
        "anonymizer": presidio_anonymizer.AnonymizerEngine(),
    }


_ = cast  # silence unused-import when redactor skips the cast path


__all__ = ["RedactionConfig", "Redactor"]
