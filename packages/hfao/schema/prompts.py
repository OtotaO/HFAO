from datetime import datetime
from typing import Literal, cast

from msgspec import Struct, field


class PromptVersion(Struct, kw_only=True):
    project_id: str
    name: str
    version: int  # immutable, monotonic
    content: str
    config: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    type: Literal["text", "chat"]
    created_at: datetime
    created_by: str
    commit_message: str | None = None


class PromptLabel(Struct, kw_only=True):  # mutable label → version pointer
    project_id: str
    name: str
    label: str  # "production", "staging", custom
    version: int
    updated_at: datetime
