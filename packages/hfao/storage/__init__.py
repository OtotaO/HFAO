"""Storage backend protocol.

SPEC §6.2. The StorageBackend Protocol is the boundary between the rest of
the system and concrete hot-tier backends (DuckDB, ClickHouse). No SQL is
allowed outside this package (Appendix C rule 4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Protocol, runtime_checkable

from hfao.schema.causal import CausalEdge
from hfao.schema.events import Observation
from hfao.schema.scores import Score


@runtime_checkable
class StorageBackend(Protocol):
    def init_schema(self) -> None: ...

    def write_events(self, events: Iterable[Observation]) -> int: ...

    def write_scores(self, scores: Iterable[Score]) -> int: ...

    def write_causal_edges(self, edges: Iterable[CausalEdge]) -> int: ...

    def get_trace(self, project_id: str, trace_id: str) -> list[Observation]: ...

    def list_traces(
        self,
        project_id: str,
        *,
        where_sql: str = "1=1",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def search_traces_text(
        self, project_id: str, query: str, limit: int = 50
    ) -> list[dict[str, Any]]: ...


    def get_causal_edges(self, project_id: str, trace_id: str) -> list[CausalEdge]: ...

    def get_scores(self, project_id: str, trace_id: str) -> list[Score]: ...

    def cost_rollup(
        self,
        project_id: str,
        *,
        date_from: datetime,
        date_to: datetime,
        group_by: list[str],
    ) -> list[dict[str, Any]]: ...

    def execute_readonly_sql(self, project_id: str, sql: str) -> list[dict[str, Any]]:
        """Read-only SQL for monitor engine and console SQL playground.

        MUST enforce project_id scoping by query rewrite. MUST reject any
        statement that is not a SELECT / WITH / SHOW / DESCRIBE.
        """
        ...


__all__ = ["StorageBackend"]
