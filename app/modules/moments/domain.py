from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class MomentCategory(StrEnum):
    EXPERIENCE = "experience"
    HABIT = "habit"
    TRAVEL = "travel"
    FOOD = "food"
    GROWTH = "growth"
    EMOTION = "emotion"


class LocationSource(StrEnum):
    DEVICE = "device"
    USER = "user"
    MCP = "mcp"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MomentLocation:
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    source: LocationSource = LocationSource.UNKNOWN


@dataclass(frozen=True, slots=True)
class MomentEmotion:
    label: str = ""
    valence: float | None = None
    arousal: float | None = None


@dataclass(frozen=True, slots=True)
class Moment:
    id: UUID
    user_id: UUID
    title: str
    description: str | None
    voice_input: str | None
    ai_summary: str | None
    category: MomentCategory
    tags: tuple[str, ...]
    occurred_at: datetime
    timezone: str
    revision: int
    created_at: datetime
    updated_at: datetime
    location: MomentLocation | None = None
    emotion: MomentEmotion | None = None
    deleted_at: datetime | None = None
