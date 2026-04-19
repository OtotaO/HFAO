# CLAUDE.md

This file is auto-loaded at the start of every Claude Code session in this
repository. Read it before doing anything else.

## The one rule that outranks everything

**SPEC.md is the single source of truth for HFAO v1.**

**§15.3 rule 2 — No spec deviation without a §16 entry first.**

If any part of the task is genuinely ambiguous against SPEC.md — silent gap,
two sections that disagree, an answer that would require a judgement call not
already made in the document — you **stop**. You do not improvise. You do not
"pick a reasonable default." You do not infer intent from chat context.

You:

1. **Stop** the work in progress. Do not write code, do not run migrations, do
   not modify SPEC.md beyond §16.
2. **Append** the question to SPEC.md §16 (Open Questions) as a new row with a
   proposed default and a decision deadline. Commit with
   `docs(spec): open question — <short slug>`.
3. **Wait** for a human to resolve it. When the answer arrives, append it to
   §16 with a date stamp and a one-line rationale — **do not** rewrite the
   original question (preserve the audit trail per §16's own instruction).
4. **Resume** only after the answer is recorded.

This rule protects eight months of strategic work from being silently
rewritten. Treat it as non-negotiable.

## What counts as "deviation"

- Changing a schema field name, type, or nullability vs. §4.
- Choosing a different library than the ones named in §11.2, §12.2, or
  Appendix A.
- Skipping, reordering, or merging commits from §15.2.
- Changing default values in Appendix A env vars.
- Introducing a new top-level dependency not justified by the commit body
  (§15.3 rule 7).
- Adding a new top-level directory beyond §3.
- Advancing to the next module's commit before its §14 acceptance tests pass
  green (§15.3 rule 3).
- Labeling an `LLM_JUDGE`-only causal edge as a "cause" rather than a
  "hypothesis" (§8.1, Appendix C rule 2).
- Emitting a proprietary wire format instead of OTel / OpenInference
  (Appendix C rule 1).
- Putting SQL anywhere outside `packages/hfao/storage/` (Appendix C rule 4).

When in doubt, it is deviation. File the §16 entry.

## What does **not** count as deviation

- Internal refactors that don't change the external contract of a module.
- Comments, docstrings, typo fixes.
- Adding tests beyond the §14 minimum.
- Tightening types (e.g., replacing `Any` at a non-OTLP boundary with a
  concrete type).

## Hard procedural rules (from §15.3, reproduced so they can't be missed)

1. One commit per line in §15.2. Conventional Commits format. Reference the
   spec section in the commit body.
2. **No spec deviation without a §16 entry first.** *(This file exists to
   enforce this rule.)*
3. Acceptance tests pass before the next commit. Never push red.
4. Every PR runs the full AC suite. No squash-merging across module
   boundaries.
5. `pyright --strict` clean for `packages/hfao/`. `tsc --strict` clean for
   `apps/console/`.
6. No `# type: ignore` without a comment. No `Any` outside the OTLP boundary.
7. No new top-level dependencies without justification in the commit body.

## If SPEC.md is not yet present in the repo

SPEC.md v1.0.0 is authoritative even before it is committed. If you are asked
to do HFAO work and SPEC.md is missing from the tree, stop and ask the human
to commit it — do not proceed from memory or chat history.
