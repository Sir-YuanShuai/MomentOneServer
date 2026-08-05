from dataclasses import dataclass, field
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


class ProvenanceSource(StrEnum):
    ROKID = "rokid"
    MOBILE = "mobile"
    WEB = "web"
    AGENT = "agent"
    MCP = "mcp"
    IMPORT = "import"


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
class MomentProvenance:
    """Moment 来源链，创建后不可篡改。与 moment.v1.json 对齐。"""

    source: ProvenanceSource
    device_id: str | None = None
    client_id: str | None = None
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    external_id: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"source": self.source.value}
        if self.device_id is not None:
            d["deviceId"] = self.device_id
        if self.client_id is not None:
            d["clientId"] = self.client_id
        if self.mcp_server_id is not None:
            d["mcpServerId"] = self.mcp_server_id
        if self.mcp_tool_name is not None:
            d["mcpToolName"] = self.mcp_tool_name
        if self.external_id is not None:
            d["externalId"] = self.external_id
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> "MomentProvenance | None":
        if not data:
            return None
        return cls(
            source=ProvenanceSource(data.get("source", "web")),
            device_id=data.get("deviceId") or data.get("device_id"),
            client_id=data.get("clientId") or data.get("client_id"),
            mcp_server_id=data.get("mcpServerId") or data.get("mcp_server_id"),
            mcp_tool_name=data.get("mcpToolName") or data.get("mcp_tool_name"),
            external_id=data.get("externalId") or data.get("external_id"),
        )


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
    provenance: MomentProvenance | None = None
    deleted_at: datetime | None = None
    # 记录类型（D2/D3）：moment_type 默认 "general"，payload 默认 {}
    moment_type: str = "general"
    payload: dict = field(default_factory=dict)
    # 通用描述维度（ADR-0019）：人物 / 事件，均可留空
    persons: tuple[str, ...] = ()
    event: str | None = None
