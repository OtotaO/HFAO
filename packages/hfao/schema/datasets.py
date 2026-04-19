from datetime import datetime
from typing import cast

from msgspec import Struct, field


class Dataset(Struct, kw_only=True):
    project_id: str
    id: str
    name: str
    description: str | None = None
    created_at: datetime


class DatasetItem(Struct, kw_only=True):
    project_id: str
    dataset_id: str
    id: str
    input: str
    expected_output: str | None = None
    metadata: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    source_trace_id: str | None = None
    source_observation_id: str | None = None
    created_at: datetime
