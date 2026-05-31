"""Eval engine — protocols and shared helpers (SPEC §8.2).

The engine is a thin protocol-driven layer:

  * :class:`EvalContext` — what an evaluator sees for one trial.
  * :class:`Evaluator` — the protocol every built-in / user evaluator obeys.
  * :class:`EvalSpec` — name + version pair used to address an evaluator across
    process boundaries (the runner stores names + versions; resolution happens
    via :func:`registry`).
  * :func:`registry` — name → :class:`Evaluator` map for built-ins.

Built-in evaluators live in :mod:`hfao.compute.eval.builtin`; the runner lives
in :mod:`hfao.compute.eval.runner`. Calibration lives in
:mod:`hfao.compute.eval.calibration`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from msgspec import Struct

if TYPE_CHECKING:
    from hfao.schema.scores import Score


@dataclass(frozen=True)
class EvalContext:
    """One eval trial's inputs.

    ``input`` and ``expected_output`` come from the dataset item; ``output`` is
    the runtime's actual response. ``metadata`` is the dataset item's metadata
    (plus runner-injected fields like ``trace_id`` once the trace is captured).
    """

    input: Any
    output: Any
    expected_output: Any | None = None
    metadata: dict[str, Any] = field(default_factory=lambda: cast("dict[str, Any]", {}))


class EvalSpec(Struct, frozen=True, kw_only=True):
    """Lightweight evaluator reference — name + version."""

    name: str
    version: str = "v1"


@runtime_checkable
class Evaluator(Protocol):
    """Anything callable on an :class:`EvalContext` that returns a Score.

    Evaluators must be pure / side-effect-free with respect to ``ctx`` — the
    runner may call them concurrently across trials. They may read external
    state (``LLM_JUDGE`` evaluators do), but must not mutate it.
    """

    name: str
    version: str

    def __call__(self, ctx: EvalContext) -> Score: ...


# --------------------------------------------------------------------------- #
# Built-in registry — populated by :mod:`hfao.compute.eval.builtin` at import.
# --------------------------------------------------------------------------- #

_BUILTIN_REGISTRY: dict[str, Callable[[], Evaluator]] = {}


def register(name: str, factory: Callable[[], Evaluator]) -> None:
    """Register a built-in evaluator factory under ``name``.

    Factories (rather than instances) keep startup cheap when many evaluators
    pull optional dependencies (``llm_judge`` constructs the judge client at
    first use).
    """
    _BUILTIN_REGISTRY[name] = factory


def resolve(name: str) -> Evaluator:
    """Resolve a registered evaluator by ``name``.

    Importing :mod:`hfao.compute.eval.builtin` populates the standard names
    (``exact_match``, ``regex_match``, ``json_schema_match``,
    ``levenshtein_ratio``, ``llm_judge``, ``latency_p95``, ``cost_per_call``,
    ``tool_use_correct``). Custom evaluators register themselves via
    :func:`register` before the runner calls :func:`resolve`.
    """
    # Auto-import the built-ins on first use so callers don't have to remember.
    if not _BUILTIN_REGISTRY:
        # Populates _BUILTIN_REGISTRY via import-time side effect.
        import importlib

        importlib.import_module("hfao.compute.eval.builtin")

    factory = _BUILTIN_REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"unknown evaluator: {name!r}")
    return factory()


def registry() -> dict[str, Callable[[], Evaluator]]:
    """Return a copy of the current registry (read-only view)."""
    if not _BUILTIN_REGISTRY:
        import importlib

        importlib.import_module("hfao.compute.eval.builtin")

    return dict(_BUILTIN_REGISTRY)


__all__ = [
    "EvalContext",
    "EvalSpec",
    "Evaluator",
    "register",
    "registry",
    "resolve",
]
