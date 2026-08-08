"""A2UI over MCP capability, schema, fixture, and fallback tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from app.modules.mcp import a2ui
from app.modules.mcp import tools as mcp_tools
from app.modules.mcp.a2ui import (
    A2UI_CATALOG_ID,
    A2UI_MIME_TYPE,
    A2UI_VERSION,
    A2UIExtension,
    A2UISupport,
    A2UIValidationError,
    build_a2ui_messages,
    build_a2ui_result,
    negotiate_a2ui,
    validate_a2ui_messages,
)
from mcp.types import ClientCapabilities, EmbeddedResource

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "a2ui"
    / "fixtures"
    / "habit-progress-tool-result.json"
)


def _capabilities(*, catalog_id: str = A2UI_CATALOG_ID) -> ClientCapabilities:
    return ClientCapabilities.model_validate(
        {
            "experimental": {
                "a2ui": {
                    "clientCapabilities": {
                        A2UI_VERSION: {
                            "supportedCatalogIds": [catalog_id],
                            "inlineCatalogs": [],
                        }
                    }
                }
            }
        }
    )


def _habit_data() -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "view": "habits",
        "timezone": "Asia/Shanghai",
        "days": 7,
        "goals": [
            {
                "id": "33333333-3333-4333-8333-333333333333",
                "name": "晨跑",
                "unit": "公里",
                "timesPerWeek": 5,
                "completedDays": 3,
                "currentStreak": 2,
            }
        ],
        "total": 1,
    }


def _messages() -> list[dict[str, Any]]:
    messages = build_a2ui_messages(
        "habit_progress", _habit_data(), surface_id="habit-progress-test"
    )
    assert messages is not None
    return messages


def test_capability_namespace_is_preserved_only_under_experimental() -> None:
    direct = ClientCapabilities.model_validate(
        {"a2ui": {"clientCapabilities": {A2UI_VERSION: {"supportedCatalogIds": [A2UI_CATALOG_ID]}}}}
    )
    assert direct.model_dump(exclude_none=True).get("a2ui") is None
    assert negotiate_a2ui(direct).enabled is False

    capabilities = _capabilities()
    dumped = capabilities.model_dump(exclude_none=True)
    assert dumped["experimental"]["a2ui"]["clientCapabilities"][A2UI_VERSION]
    support = negotiate_a2ui(capabilities)
    assert support.enabled is True
    assert support.version == A2UI_VERSION
    assert support.catalog_id == A2UI_CATALOG_ID
    assert support.capability_path == (
        'capabilities.experimental.a2ui.clientCapabilities["v0.9"].supportedCatalogIds'
    )

    advertised = A2UIExtension().settings()["serverCapabilities"][A2UI_VERSION]
    assert advertised["supportedCatalogIds"] == [A2UI_CATALOG_ID]
    assert advertised["acceptsInlineCatalogs"] is False


@pytest.mark.parametrize(
    "capabilities",
    [
        ClientCapabilities(),
        ClientCapabilities.model_validate({"experimental": {"a2ui": {}}}),
        _capabilities(catalog_id="https://example.invalid/catalog.json"),
    ],
)
def test_missing_or_unsupported_capability_disables_a2ui(
    capabilities: ClientCapabilities,
) -> None:
    assert negotiate_a2ui(capabilities).enabled is False


def test_valid_standard_messages_and_fixture() -> None:
    messages = _messages()
    validate_a2ui_messages(messages)
    assert [next(key for key in message if key != "version") for message in messages] == [
        "createSurface",
        "updateDataModel",
        "updateComponents",
    ]

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    resource = next(item for item in fixture["content"] if item["type"] == "resource")
    assert resource["resource"]["mimeType"] == A2UI_MIME_TYPE
    assert resource["annotations"]["audience"] == ["user"]
    validate_a2ui_messages(json.loads(resource["resource"]["text"]))


def test_schema_rejects_missing_root_unknown_catalog_wrong_version_and_component() -> None:
    missing_root = _messages()
    missing_root[-1]["updateComponents"]["components"] = [
        component
        for component in missing_root[-1]["updateComponents"]["components"]
        if component["id"] != "root"
    ]
    with pytest.raises(A2UIValidationError, match="root"):
        validate_a2ui_messages(missing_root)

    unknown_catalog = _messages()
    unknown_catalog[0]["createSurface"]["catalogId"] = "https://example.invalid/catalog.json"
    with pytest.raises(A2UIValidationError, match="catalogId"):
        validate_a2ui_messages(unknown_catalog)

    wrong_version = _messages()
    for message in wrong_version:
        message["version"] = "v0.9.1"
    with pytest.raises(A2UIValidationError, match="version"):
        validate_a2ui_messages(wrong_version)

    invalid_component = _messages()
    invalid_component[-1]["updateComponents"]["components"][0]["component"] = "Grid"
    with pytest.raises(A2UIValidationError):
        validate_a2ui_messages(invalid_component)


def test_validation_failure_falls_back_to_text_and_structured_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_builder(
        tool_name: str, data: dict[str, Any], *, surface_id: str
    ) -> list[dict[str, Any]]:
        del tool_name, data, surface_id
        return [
            {"version": A2UI_VERSION, "updateComponents": {"surfaceId": "bad", "components": []}}
        ]

    monkeypatch.setattr(a2ui, "build_a2ui_messages", invalid_builder)
    structured = _habit_data()
    result = build_a2ui_result(
        support=A2UISupport(
            enabled=True,
            version=A2UI_VERSION,
            catalog_id=A2UI_CATALOG_ID,
        ),
        tool_name="habit_progress",
        request_id="request-1234",
        structured_content=structured,
        text="习惯进度已更新。",
    )
    assert result.structured_content == structured
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)


@pytest.mark.asyncio
async def test_agent_plan_rejects_missing_target_and_never_recurses() -> None:
    ctx = mcp_tools.McpCallContext(
        user_id=UUID("11111111-1111-4111-8111-111111111111"),
        scopes=("moments.read", "moments.write"),
        actor_id="test-agent",
        method="oauth",
        request_id="planner-unit-test",
        session=object(),  # type: ignore[arg-type]
    )
    missing_target = await mcp_tools.agent_plan(
        ctx,
        input="查看最近的 Moment 记录",
        tool_schemas={"agent_plan": {"type": "object"}},
    )
    assert missing_target.structured_content is not None
    assert missing_target.structured_content["toolName"] == ""
    assert "没有可安全执行" in missing_target.structured_content["reply"]

    recursive = await mcp_tools.agent_plan(
        ctx,
        input="请调用 agent_plan",
        tool_schemas={"agent_plan": {"type": "object"}},
    )
    assert recursive.structured_content is not None
    assert recursive.structured_content["toolName"] == ""
