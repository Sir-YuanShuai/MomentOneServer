"""内置记录类型注册表。

类型 Schema 是 JSON Schema 文件，位于 `contracts/types/`（仓库根），
是 MCP 动态生成工具的 Schema 来源（后续阶段）。本模块负责：

- 注册内置类型（bookkeeping / habit / general 兜底）
- `validate(type, payload)`：按类型 Schema 校验 payload，失败抛 INVALID_ARGUMENTS
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.core.errors import ApplicationError

# general = 通用自由记录（现有 Moment 语义），payload 恒为 {}，无需 Schema
GENERAL_TYPE = "general"
BUILTIN_TYPES: tuple[str, ...] = ("bookkeeping", "habit")

_CONTRACTS_TYPES_DIR = Path(__file__).resolve().parents[3] / "contracts" / "types"


@lru_cache(maxsize=8)
def get_schema(moment_type: str) -> dict[str, Any]:
    """读取内置类型的 JSON Schema 文件；未知类型抛 INVALID_ARGUMENTS。"""
    if moment_type == GENERAL_TYPE:
        # general 兜底：payload 必须为空对象
        return {"type": "object", "required": [], "properties": {}}
    if moment_type not in BUILTIN_TYPES:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message=(
                f"未知的记录类型：{moment_type}。"
                f"可用类型：{', '.join((*BUILTIN_TYPES, GENERAL_TYPE))}。"
            ),
            status_code=400,
            details={"type": moment_type, "availableTypes": [*BUILTIN_TYPES, GENERAL_TYPE]},
        )
    schema_path = _CONTRACTS_TYPES_DIR / f"{moment_type}.schema.json"
    if not schema_path.is_file():
        raise ApplicationError(
            code="INTERNAL_ERROR",
            message=f"内置类型 {moment_type} 缺少 Schema 文件。",
            status_code=500,
        )
    with schema_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def is_builtin(moment_type: str) -> bool:
    return moment_type in (*BUILTIN_TYPES, GENERAL_TYPE)


def validate(moment_type: str, payload: Any) -> None:
    """校验 payload 是否符合类型 Schema。

    - 类型不存在 → INVALID_ARGUMENTS
    - payload 不是对象 → INVALID_ARGUMENTS
    - payload 不符合 Schema（必填、枚举、类型、金额上限等）→ INVALID_ARGUMENTS

    演进约束（D6）：类型 Schema 只做向后兼容新增，不做破坏性变更。
    """
    if not is_builtin(moment_type):
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message=(
                f"未知的记录类型：{moment_type}。"
                f"可用类型：{', '.join((*BUILTIN_TYPES, GENERAL_TYPE))}。"
            ),
            status_code=400,
            details={"type": moment_type, "availableTypes": [*BUILTIN_TYPES, GENERAL_TYPE]},
        )
    if not isinstance(payload, dict):
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="payload 必须是 JSON 对象。",
            status_code=400,
            details={"type": moment_type},
        )

    if moment_type == GENERAL_TYPE:
        if payload:
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="general 类型的 payload 必须为空对象 {}。",
                status_code=400,
                details={"type": moment_type},
            )
        return

    schema = get_schema(moment_type)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.path) or "$"
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message=f"payload 不符合 {moment_type} 类型 Schema：{first.message}",
            status_code=400,
            details={
                "type": moment_type,
                "path": path,
                "errors": [e.message for e in errors],
            },
        )


__all__ = [
    "BUILTIN_TYPES",
    "GENERAL_TYPE",
    "get_schema",
    "is_builtin",
    "validate",
]
