"""HFAO framework-integration extras.

SPEC §12.2. The OpenInference instrumentation packages cover the majority
of the semantic-convention mapping for each framework. This sub-package
ships the *extras* that sit on top — small, framework-aware helpers that
translate framework-specific identifiers (e.g. LangGraph ``thread_id``,
OpenAI Agents handoff / guardrail spans, Claude Agent pre/post-tool
hooks, smolagents CodeAgent code-action spans) into HFAO's canonical
fields so the normalizer picks them up cleanly.

Each Tier 1 module exposes ``install(tracer_provider=None)`` (idempotent,
safe when the underlying framework is not installed) and a small set of
user-callable helpers described in that module's docstring.
"""
