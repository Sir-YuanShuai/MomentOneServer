"""Moment One plugin package remains a valid single-entry MCP distribution."""

import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "moment-one"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_plugin_manifest_requires_registered_app_and_skill() -> None:
    manifest = _read_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    assert manifest["name"] == "moment-one"
    assert manifest["apps"] == "./.app.json"
    assert manifest["skills"] == "./skills/"
    assert "mcpServers" not in manifest, "The registered app already owns the MCP connection"

    app = _read_json(PLUGIN_ROOT / ".app.json")
    apps = app["apps"]
    assert isinstance(apps, dict)
    assert apps["moment-one"] == {
        "id": "asdk_app_6a7dc3dbf1648191a2e80e128a715ef8",
    }


def test_plugin_skill_has_no_scaffold_placeholders() -> None:
    skill = (PLUGIN_ROOT / "skills" / "record-with-moment-one" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "[TODO:" not in skill
    assert "idempotencyKey" in skill
    assert "createdAt" in skill
    assert "occurredAt" in skill
    assert "agent_plan" in skill
