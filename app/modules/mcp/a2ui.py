"""A2UI over MCP negotiation, validation, and compact result-card builders.

A2UI is an optional result channel. HTML MCP Apps remain registered separately.
Clients opt in through `capabilities.experimental.a2ui`.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from mcp.server.extension import Extension
from mcp.types import (
    Annotations,
    CallToolResult,
    ClientCapabilities,
    EmbeddedResource,
    TextContent,
    TextResourceContents,
)
from referencing import Registry, Resource

A2UI_EXTENSION_ID = "a2ui"
A2UI_SERVER_EXTENSION_ID = "org.a2ui/mcp"
A2UI_VERSION = "v0.9"
A2UI_MIME_TYPE = "application/a2ui+json"
A2UI_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
_A2UI_GENERIC_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalog.json"
_SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "contracts" / "a2ui" / "v0_9"
_ALLOWED_COMPONENTS = frozenset({"Column", "Row", "Text", "Button", "Image"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class A2UISupport:
    enabled: bool
    version: str | None = None
    catalog_id: str | None = None
    capability_path: str | None = None
    reason: str | None = None


A2UI_DISABLED = A2UISupport(enabled=False, reason="client_did_not_advertise_a2ui")


class A2UIExtension(Extension):
    """Advertise A2UI generation under MCP's standard extension namespace."""

    identifier = A2UI_SERVER_EXTENSION_ID

    def settings(self) -> dict[str, Any]:
        return {
            "serverCapabilities": {
                A2UI_VERSION: {
                    "supportedCatalogIds": [A2UI_CATALOG_ID],
                    "acceptsInlineCatalogs": False,
                }
            }
        }


def negotiate_a2ui(capabilities: ClientCapabilities | None) -> A2UISupport:
    """Return enabled support only for the exact MCP extension capability path.

    Formal path:
    capabilities.experimental.a2ui.clientCapabilities["v0.9"].supportedCatalogIds
    """
    if capabilities is None or not capabilities.experimental:
        return A2UI_DISABLED
    raw = capabilities.experimental.get(A2UI_EXTENSION_ID)
    if not isinstance(raw, dict):
        return A2UISupport(enabled=False, reason="experimental.a2ui_missing")
    client_capabilities = raw.get("clientCapabilities")
    if not isinstance(client_capabilities, dict):
        return A2UISupport(enabled=False, reason="clientCapabilities_missing")
    version_settings = client_capabilities.get(A2UI_VERSION)
    if not isinstance(version_settings, dict):
        return A2UISupport(enabled=False, reason="v0.9_capability_missing")
    catalogs = version_settings.get("supportedCatalogIds")
    if not isinstance(catalogs, list) or A2UI_CATALOG_ID not in catalogs:
        return A2UISupport(enabled=False, reason="basic_catalog_not_supported")
    return A2UISupport(
        enabled=True,
        version=A2UI_VERSION,
        catalog_id=A2UI_CATALOG_ID,
        capability_path=(
            'capabilities.experimental.a2ui.clientCapabilities["v0.9"].supportedCatalogIds'
        ),
    )


class A2UIValidationError(ValueError):
    pass


def _load_validator() -> Draft202012Validator:
    server_schema = json.loads((_SCHEMA_ROOT / "json" / "server_to_client.json").read_text())
    common_schema = json.loads((_SCHEMA_ROOT / "json" / "common_types.json").read_text())
    catalog_schema = json.loads((_SCHEMA_ROOT / "catalogs" / "basic" / "catalog.json").read_text())
    catalog_alias = copy.deepcopy(catalog_schema)
    catalog_alias["$id"] = _A2UI_GENERIC_CATALOG_ID
    registry = Registry().with_resources(
        [
            (server_schema["$id"], Resource.from_contents(server_schema)),
            (common_schema["$id"], Resource.from_contents(common_schema)),
            (catalog_schema["$id"], Resource.from_contents(catalog_schema)),
            (catalog_alias["$id"], Resource.from_contents(catalog_alias)),
        ]
    )
    return Draft202012Validator(server_schema, registry=registry)


_VALIDATOR = _load_validator()


def validate_a2ui_messages(messages: list[dict[str, Any]]) -> None:
    if not messages:
        raise A2UIValidationError("A2UI messages cannot be empty")
    surface_id: str | None = None
    created = False
    root_seen = False
    for index, message in enumerate(messages):
        errors = sorted(_VALIDATOR.iter_errors(message), key=lambda error: list(error.path))
        if errors:
            raise A2UIValidationError(f"message[{index}]: {errors[0].message}")
        if message.get("version") != A2UI_VERSION:
            raise A2UIValidationError(f"message[{index}]: unsupported A2UI version")
        if "createSurface" in message:
            if created:
                raise A2UIValidationError("createSurface may only appear once")
            payload = message["createSurface"]
            if payload.get("catalogId") != A2UI_CATALOG_ID:
                raise A2UIValidationError("unknown A2UI catalogId")
            surface_id = payload["surfaceId"]
            created = True
        else:
            payload = next(
                value
                for key, value in message.items()
                if key in {"updateComponents", "updateDataModel", "deleteSurface"}
            )
            if not created or payload.get("surfaceId") != surface_id:
                raise A2UIValidationError("surface must be created before updates")
        if "updateComponents" in message:
            components = message["updateComponents"]["components"]
            if any(
                component.get("component") not in _ALLOWED_COMPONENTS for component in components
            ):
                raise A2UIValidationError("component is outside the allowed compact-card subset")
            root_seen = root_seen or any(component.get("id") == "root" for component in components)
    if not root_seen:
        raise A2UIValidationError("updateComponents must define root")


def create_a2ui_embedded_resource(
    messages: list[dict[str, Any]], *, surface_id: str
) -> EmbeddedResource:
    return EmbeddedResource(
        resource=TextResourceContents(
            uri=f"a2ui://moment-one/{surface_id}",
            mime_type=A2UI_MIME_TYPE,
            text=json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
        ),
        annotations=Annotations(audience=["user"], priority=1.0),
    )


def build_a2ui_result(
    *,
    support: A2UISupport,
    tool_name: str,
    request_id: str,
    structured_content: dict[str, Any],
    text: str,
) -> CallToolResult:
    content: list[Any] = [TextContent(type="text", text=text)]
    surface_id = f"{tool_name.replace('_', '-')}-{request_id[:8]}"
    validation_result = "not_requested"
    if support.enabled:
        try:
            messages = build_a2ui_messages(tool_name, structured_content, surface_id=surface_id)
            if messages is not None:
                validate_a2ui_messages(messages)
                content.append(create_a2ui_embedded_resource(messages, surface_id=surface_id))
                validation_result = "valid"
            else:
                validation_result = "no_card_builder"
        except (A2UIValidationError, TypeError, ValueError) as exc:
            validation_result = "invalid_fallback_to_text"
            logger.warning(
                "a2ui_validation_failed",
                extra={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "surface_id": surface_id,
                    "a2ui_version": A2UI_VERSION,
                    "catalog_id": A2UI_CATALOG_ID,
                    "validation_result": validation_result,
                    "error_type": type(exc).__name__,
                },
            )
    logger.info(
        "a2ui_result_built",
        extra={
            "request_id": request_id,
            "tool_name": tool_name,
            "surface_id": surface_id,
            "a2ui_version": A2UI_VERSION,
            "catalog_id": A2UI_CATALOG_ID,
            "validation_result": validation_result,
        },
    )
    return CallToolResult(content=content, structured_content=structured_content)


def _text(component_id: str, value: str | dict[str, str], variant: str = "body") -> dict[str, Any]:
    return {"id": component_id, "component": "Text", "text": value, "variant": variant}


def _envelopes(
    surface_id: str, *, data: dict[str, Any], components: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "version": A2UI_VERSION,
            "createSurface": {"surfaceId": surface_id, "catalogId": A2UI_CATALOG_ID},
        },
        {
            "version": A2UI_VERSION,
            "updateDataModel": {"surfaceId": surface_id, "path": "/", "value": data},
        },
        {
            "version": A2UI_VERSION,
            "updateComponents": {"surfaceId": surface_id, "components": components},
        },
    ]


def _simple_rows_card(
    surface_id: str,
    *,
    heading: str,
    meta: str,
    rows: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    data: dict[str, Any] = {"heading": heading, "meta": meta}
    children = ["heading", "meta"]
    components: list[dict[str, Any]] = [
        {"id": "root", "component": "Column", "children": children},
        _text("heading", {"path": "/heading"}, "h3"),
        _text("meta", {"path": "/meta"}, "caption"),
    ]
    for index, (title, detail) in enumerate(rows[:3]):
        data[f"row{index}Title"] = title
        data[f"row{index}Detail"] = detail
        row_id = f"row{index}"
        title_id = f"row{index}Title"
        detail_id = f"row{index}Detail"
        children.append(row_id)
        components.extend(
            [
                {
                    "id": row_id,
                    "component": "Row",
                    "children": [title_id, detail_id],
                    "justify": "spaceBetween",
                    "align": "center",
                },
                _text(title_id, {"path": f"/{title_id}"}, "body"),
                _text(detail_id, {"path": f"/{detail_id}"}, "caption"),
            ]
        )
    return _envelopes(surface_id, data=data, components=components)


def build_a2ui_messages(
    tool_name: str, data: dict[str, Any], *, surface_id: str
) -> list[dict[str, Any]] | None:
    if tool_name == "bookkeeping_summary":
        categories = data.get("byCategory") or []
        rows = [
            (str(item.get("category", "未分类")), f"¥{item.get('amount', 0)}")
            for item in categories
        ]
        metrics = [
            ("支出", f"¥{data.get('expense', 0)}"),
            ("收入", f"¥{data.get('income', 0)}"),
            ("结余", f"¥{data.get('balance', 0)}"),
        ]
        model: dict[str, Any] = {
            "heading": "收支概览",
            "meta": f"{data.get('period', 'period')} · {data.get('count', 0)} 笔",
        }
        components: list[dict[str, Any]] = [
            {
                "id": "root",
                "component": "Column",
                "children": ["heading", "meta", "metrics"],
            },
            _text("heading", {"path": "/heading"}, "h3"),
            _text("meta", {"path": "/meta"}, "caption"),
            {"id": "metrics", "component": "Row", "children": [], "justify": "spaceBetween"},
        ]
        metric_children: list[str] = []
        for index, (label, value) in enumerate(metrics):
            column = f"metric{index}"
            value_id = f"metric{index}Value"
            label_id = f"metric{index}Label"
            model[value_id] = value
            model[label_id] = label
            metric_children.append(column)
            components.extend(
                [
                    {
                        "id": column,
                        "component": "Column",
                        "children": [value_id, label_id],
                        "align": "center",
                    },
                    _text(value_id, {"path": f"/{value_id}"}, "h4"),
                    _text(label_id, {"path": f"/{label_id}"}, "caption"),
                ]
            )
        components[3]["children"] = metric_children
        category_children: list[str] = []
        for index, (title, detail) in enumerate(rows[:3]):
            row_id = f"category{index}"
            title_id = f"category{index}Title"
            detail_id = f"category{index}Detail"
            model[title_id], model[detail_id] = title, detail
            category_children.append(row_id)
            components.extend(
                [
                    {
                        "id": row_id,
                        "component": "Row",
                        "children": [title_id, detail_id],
                        "justify": "spaceBetween",
                    },
                    _text(title_id, {"path": f"/{title_id}"}),
                    _text(detail_id, {"path": f"/{detail_id}"}, "caption"),
                ]
            )
        components[0]["children"].extend(category_children)
        return _envelopes(surface_id, data=model, components=components)

    if tool_name == "bookkeeping_create":
        sign = "+" if data.get("flow") == "income" else "-"
        return _simple_rows_card(
            surface_id,
            heading="记账成功",
            meta=str(data.get("occurredAt", "")),
            rows=[
                (
                    str(data.get("category") or data.get("merchant") or "记账"),
                    f"{sign}¥{data.get('amount', 0)}",
                ),
                ("流向", "收入" if data.get("flow") == "income" else "支出"),
            ],
        )

    if tool_name == "bookkeeping_list":
        items = data.get("items") or []
        return _simple_rows_card(
            surface_id,
            heading="账目明细",
            meta=f"共 {data.get('total', len(items))} 笔",
            rows=[
                (
                    str(item.get("title", "记账")),
                    f"{'+' if item.get('flow') == 'income' else '-'}¥{item.get('amount', 0)}",
                )
                for item in items
            ],
        )

    if tool_name in {"moments_list", "moments_search", "reviews_daily"}:
        items = data.get("highlights") or data.get("items") or []
        return _simple_rows_card(
            surface_id,
            heading=(f"“{data.get('query')}”的结果" if data.get("query") else "Moment 查询结果"),
            meta=f"共 {data.get('count', data.get('total', len(items)))} 条",
            rows=[
                (str(item.get("title", "Moment")), str(item.get("occurredAt", "")))
                for item in items
            ],
        )

    if tool_name == "moments_get":
        return _simple_rows_card(
            surface_id,
            heading=str(data.get("title", "Moment")),
            meta=str(data.get("occurredAt", "")),
            rows=[
                ("分类", str(data.get("category", ""))),
                ("摘要", str(data.get("description") or data.get("type") or "")),
            ],
        )

    if tool_name == "habit_progress":
        goals = data.get("goals") or []
        return _simple_rows_card(
            surface_id,
            heading="习惯进度",
            meta=f"最近 {data.get('days', 7)} 天 · {len(goals)} 个目标",
            rows=[
                (
                    str(goal.get("name", "习惯")),
                    (
                        f"{goal.get('completedDays', 0)}/"
                        f"{goal.get('timesPerWeek') or data.get('days', 7)} 次"
                    ),
                )
                for goal in goals
            ],
        )

    if tool_name == "habit_checkin_create":
        goal = data.get("goal") or {}
        checkin = data.get("checkin") or {}
        payload = checkin.get("payload") or {}
        return _simple_rows_card(
            surface_id,
            heading="打卡成功",
            meta=str(checkin.get("occurredAt", "")),
            rows=[
                (
                    str(goal.get("name", "习惯")),
                    f"{payload.get('count', 1)} {goal.get('unit') or '次'}",
                ),
                ("状态", "已完成"),
            ],
        )
    return None


def build_text_summary(tool_name: str, data: dict[str, Any]) -> str:
    if tool_name == "bookkeeping_summary":
        return (
            f"统计结果：支出 ¥{data.get('expense', 0)}，收入 ¥{data.get('income', 0)}，"
            f"结余 ¥{data.get('balance', 0)}，共 {data.get('count', 0)} 笔。"
        )
    if tool_name == "bookkeeping_create":
        return (
            f"记账成功：{'收入' if data.get('flow') == 'income' else '支出'} "
            f"¥{data.get('amount', 0)}，分类 {data.get('category') or '未分类'}。"
        )
    if tool_name == "bookkeeping_list":
        return f"共找到 {data.get('total', 0)} 笔账目，返回前 {len(data.get('items') or [])} 笔。"
    if tool_name in {"moments_list", "moments_search", "reviews_daily"}:
        items = data.get("highlights") or data.get("items") or []
        count = data.get("count", data.get("total", len(items)))
        return f"共找到 {count} 条 Moment，返回前 {len(items)} 条。"
    if tool_name == "moments_get":
        return f"Moment：{data.get('title', '未命名')}。{data.get('description') or ''}".strip()
    if tool_name == "habit_progress":
        goals = data.get("goals") or []
        if not goals:
            return "当前还没有习惯目标。"
        first = goals[0]
        return (
            f"{first.get('name', '习惯')}最近 {data.get('days', 7)} 天完成 "
            f"{first.get('completedDays', 0)} 次，连续 {first.get('currentStreak', 0)} 天。"
        )
    if tool_name == "habit_checkin_create":
        goal = data.get("goal") or {}
        return f"{goal.get('name', '习惯')}打卡成功。"
    return json.dumps(data, ensure_ascii=False, indent=2)
