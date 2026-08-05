"""内置记录类型注册表单元测试。

覆盖：
- bookkeeping / habit / general 类型 Schema 注册
- validate() 合法 / 非法 payload
- 未知类型 → INVALID_ARGUMENTS
"""

import pytest
from app.core.errors import ApplicationError
from app.modules.moment_types.registry import (
    BUILTIN_TYPES,
    GENERAL_TYPE,
    get_schema,
    is_builtin,
    validate,
)


def test_builtin_types_registered() -> None:
    assert "bookkeeping" in BUILTIN_TYPES
    assert "habit" in BUILTIN_TYPES
    assert len(BUILTIN_TYPES) == 2
    assert is_builtin(GENERAL_TYPE)
    assert is_builtin("bookkeeping")
    assert is_builtin("habit")
    assert not is_builtin("travel")
    assert not is_builtin("unknown-type")


def test_schemas_are_valid_json_schema_files() -> None:
    for t in BUILTIN_TYPES:
        schema = get_schema(t)
        assert schema["type"] == "object"
        assert "properties" in schema


# ---- bookkeeping ----


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": 38.5, "flow": "expense"},
        {"amount": 0, "flow": "income"},
        {"amount": 9999999, "flow": "expense", "currency": "CNY"},
        {"amount": 12, "flow": "expense", "account": "微信", "category": "餐饮"},
        # ADR-0019 扩充字段（全部可选，向后兼容）
        {
            "amount": 88,
            "flow": "expense",
            "account": "微信",
            "category": "餐饮",
            "ledger": "日常",
            "method": "微信支付",
            "countInFlow": True,
            "countInBudget": True,
            "relatedBillIds": ["f6a7b8c9-6789-abcd-def0-234567890123"],
        },
        {"amount": 5, "flow": "expense", "countInFlow": False, "countInBudget": False},
    ],
)
def test_bookkeeping_valid_payload(payload: dict) -> None:
    validate("bookkeeping", payload)  # 不应抛异常


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 缺必填 amount / flow
        {"amount": 38.5},  # 缺 flow
        {"flow": "expense"},  # 缺 amount
        {"amount": "38.5", "flow": "expense"},  # amount 类型错误
        {"amount": -1, "flow": "expense"},  # amount < 0
        {"amount": 10000000, "flow": "expense"},  # amount 超上限
        {"amount": 10, "flow": "transfer"},  # flow 枚举外
        {"amount": 10, "flow": None},  # flow 为 null
        {"amount": 10, "flow": "expense", "countInFlow": "yes"},  # 布尔字段类型错误
        {"amount": 10, "flow": "expense", "relatedBillIds": "not-array"},  # 关联账单非数组
        {"amount": 10, "flow": "expense", "relatedBillIds": ["not-a-uuid"]},  # 关联账单非 UUID
    ],
)
def test_bookkeeping_invalid_payload(payload: dict) -> None:
    with pytest.raises(ApplicationError) as exc_info:
        validate("bookkeeping", payload)
    assert exc_info.value.code == "INVALID_ARGUMENTS"
    assert exc_info.value.status_code == 400


# ---- habit ----


@pytest.mark.parametrize(
    "payload",
    [
        {"habit": "晨跑", "done": True},
        {"habit": "晨跑", "done": True},
        {"habit": "阅读", "done": False},
        {"habit": "晨跑", "done": True, "unit": "公里", "count": 5},
        {"habit": "", "done": True},  # 空字符串（Schema 仅约束长度上限）
    ],
)
def test_habit_valid_payload(payload: dict) -> None:
    validate("habit", payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 缺必填 habit / done
        {"done": True},  # 缺 habit
        {"habit": "晨跑"},  # 缺 done
        {"habit": "x" * 31, "done": True},  # habit 超 30 字
        {"habit": "晨跑", "done": "yes"},  # done 类型错误
        {"habit": "晨跑", "done": True, "count": 0},  # count < 1
        {"habit": "晨跑", "done": True, "count": 2.5},  # count 非整数
    ],
)
def test_habit_invalid_payload(payload: dict) -> None:
    with pytest.raises(ApplicationError) as exc_info:
        validate("habit", payload)
    assert exc_info.value.code == "INVALID_ARGUMENTS"


# ---- general ----


def test_general_valid_payload() -> None:
    validate(GENERAL_TYPE, {})


def test_general_rejects_non_empty_payload() -> None:
    with pytest.raises(ApplicationError) as exc_info:
        validate(GENERAL_TYPE, {"amount": 1})
    assert exc_info.value.code == "INVALID_ARGUMENTS"


# ---- 类型与 payload 类型边界 ----


def test_unknown_type_rejected() -> None:
    with pytest.raises(ApplicationError) as exc_info:
        validate("travel", {})
    assert exc_info.value.code == "INVALID_ARGUMENTS"
    assert "travel" in exc_info.value.message


def test_payload_must_be_object() -> None:
    with pytest.raises(ApplicationError) as exc_info:
        validate("bookkeeping", [1, 2, 3])
    assert exc_info.value.code == "INVALID_ARGUMENTS"


def test_payload_must_be_object_scalar() -> None:
    with pytest.raises(ApplicationError) as exc_info:
        validate("bookkeeping", 42)
    assert exc_info.value.code == "INVALID_ARGUMENTS"
