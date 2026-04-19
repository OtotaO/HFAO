from datetime import datetime
from typing import Literal

from msgspec import Struct


class AnnotationQueue(Struct, kw_only=True):
    project_id: str
    id: str
    name: str
    filter_query: str  # SQL WHERE clause; auto-routes new traces
    score_schema: list[str]
    created_at: datetime


class AnnotationItem(Struct, kw_only=True):
    queue_id: str
    trace_id: str
    observation_id: str | None = None
    assigned_to: str | None = None
    status: Literal["pending", "in_progress", "completed", "skipped"]
    completed_at: datetime | None = None
