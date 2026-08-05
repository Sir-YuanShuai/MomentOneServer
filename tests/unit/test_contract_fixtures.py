"""契约 Fixture 校验：contracts/fixtures/ 下所有 Moment fixture 必须通过
moment.v1.json（权威 Schema，ADR-0017 扩展后含 type/payload 可选字段）。

三端共享同一组 Fixture（根 AGENTS.md §关键约束），此测试防止 fixture 与
Schema 漂移。
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "moment.v1.json"
FIXTURES_DIR = REPO_ROOT / "contracts" / "fixtures"
TYPES_DIR = REPO_ROOT / "contracts" / "types"


@pytest.fixture(scope="module")
def moment_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _iter_fixture_documents() -> list[tuple[str, dict]]:
    docs: list[tuple[str, dict]] = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        if path.name == "error-cases.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "items" in data:
            docs.extend((path.name, item) for item in data["items"])
        else:
            docs.append((path.name, data))
    return docs


def test_all_moment_fixtures_pass_schema(
    moment_validator: Draft202012Validator,
) -> None:
    docs = _iter_fixture_documents()
    assert docs, "至少应有一个 Moment fixture"
    for name, doc in docs:
        errors = list(moment_validator.iter_errors(doc))
        assert not errors, f"{name}: {errors[0].message}"


def test_typed_fixtures_match_payload_schemas() -> None:
    """type != general 的 fixture，其 payload 必须通过对应 contracts/types Schema。"""
    for name, doc in _iter_fixture_documents():
        moment_type = doc.get("type", "general")
        if moment_type == "general":
            continue
        schema_path = TYPES_DIR / f"{moment_type}.schema.json"
        assert schema_path.is_file(), f"{name}: 缺少类型 Schema {schema_path.name}"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema).iter_errors(doc.get("payload", {})))
        assert not errors, f"{name}: payload 不符合 {moment_type} Schema: {errors[0].message}"


def test_builtin_type_schemas_are_valid_json_schema() -> None:
    for path in sorted(TYPES_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
