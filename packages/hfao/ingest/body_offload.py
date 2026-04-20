"""Body offload for large observation payloads.

SPEC §6.6. Any ``input`` / ``output`` longer than
``HFAO_BODY_OFFLOAD_THRESHOLD_BYTES`` (default 64 KiB) is written to a
configurable ``BodyStore`` and the Observation's inline column is replaced
with an empty string; the URI goes to ``input_ref`` / ``output_ref``.

§16 Q-6: default store is the local filesystem (``LocalBodyStore``);
the Docker/K8s shapes will plug in S3/R2/HF Bucket stores later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import msgspec
import zstandard

from hfao.schema.events import Observation

_KEY_SEP = "/"


@runtime_checkable
class BodyStore(Protocol):
    def put(
        self,
        *,
        project_id: str,
        trace_id: str,
        observation_id: str,
        field: str,
        body: str,
    ) -> str: ...

    def get(self, uri: str) -> str: ...


class LocalBodyStore:
    """Filesystem-backed ``BodyStore`` writing zstd-compressed JSON payloads.

    Keys follow SPEC §6.6:
    ``/{root}/{project_id}/{trace_id}/{observation_id}.{io}.json.zst``
    and round-trip through the ``file://`` URI scheme.
    """

    def __init__(self, root: str | Path, *, compression_level: int = 3) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._cctx = zstandard.ZstdCompressor(level=compression_level)
        self._dctx = zstandard.ZstdDecompressor()

    def put(
        self,
        *,
        project_id: str,
        trace_id: str,
        observation_id: str,
        field: str,
        body: str,
    ) -> str:
        safe_project = _sanitize(project_id)
        safe_trace = _sanitize(trace_id)
        safe_obs = _sanitize(observation_id)
        path = (
            self._root
            / safe_project
            / safe_trace
            / f"{safe_obs}.{field}.json.zst"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._cctx.compress(body.encode("utf-8")))
        return f"file://{path.resolve().as_posix()}"

    def get(self, uri: str) -> str:
        if not uri.startswith("file://"):
            raise ValueError(f"LocalBodyStore cannot read non-file URI: {uri}")
        path = Path(uri.removeprefix("file://"))
        return self._dctx.decompress(path.read_bytes()).decode("utf-8")


def _sanitize(component: str) -> str:
    """Collapse any path-traversal characters to safe tokens."""
    if not component:
        return "_"
    return component.replace("/", "_").replace("\\", "_").replace("..", "_")


class BodyOffloader:
    """Offload oversized inline bodies into the configured ``BodyStore``.

    ``threshold_bytes`` is measured on the UTF-8 encoded string. If a body
    exceeds the threshold, it's written to the store, the canonical
    column is emptied, and the URI is set on ``*_ref``.
    """

    def __init__(self, store: BodyStore, *, threshold_bytes: int) -> None:
        self._store = store
        self._threshold = threshold_bytes

    def offload(self, obs: Observation) -> Observation:
        input_val = obs.input
        output_val = obs.output
        input_ref = obs.input_ref
        output_ref = obs.output_ref

        if input_val and len(input_val.encode("utf-8")) > self._threshold:
            input_ref = self._store.put(
                project_id=obs.project_id,
                trace_id=obs.trace_id,
                observation_id=obs.observation_id,
                field="input",
                body=input_val,
            )
            input_val = ""

        if output_val and len(output_val.encode("utf-8")) > self._threshold:
            output_ref = self._store.put(
                project_id=obs.project_id,
                trace_id=obs.trace_id,
                observation_id=obs.observation_id,
                field="output",
                body=output_val,
            )
            output_val = ""

        return msgspec.structs.replace(
            obs,
            input=input_val,
            output=output_val,
            input_ref=input_ref,
            output_ref=output_ref,
        )


_ = Any  # retained for plugin-store subclasses that may widen types
_ = _KEY_SEP  # reserved for S3-style stores


__all__ = ["BodyStore", "LocalBodyStore", "BodyOffloader"]
