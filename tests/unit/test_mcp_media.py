from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from app.modules.assets.domain import Asset, AssetKind, AssetState
from app.modules.mcp import tools
from app.modules.mcp.tools import McpCallContext


class _AssetRepo:
    def __init__(self, asset: Asset) -> None:
        self.asset = asset

    async def get_by_id(self, asset_id: object, user_id: object) -> Asset | None:
        return self.asset if asset_id == self.asset.id and user_id == self.asset.user_id else None


class _Storage:
    def create_thumbnail_url(self, **kwargs: object) -> str:
        return f"https://storage.example/thumbnail/{kwargs['asset_id']}"

    def create_download_url(self, **kwargs: object) -> str:
        return f"https://storage.example/download/{kwargs['asset_id']}"


@pytest.mark.asyncio
async def test_final_mcp_media_contains_real_size_and_preview_urls(monkeypatch: Any) -> None:
    user_id = uuid4()
    asset = Asset(
        id=uuid4(),
        user_id=user_id,
        state=AssetState.READY,
        kind=AssetKind.IMAGE,
        storage_key="safe-test-key",
        content_type="image/png",
        size_bytes=2_901_132,
        checksum_sha256=None,
        created_at=datetime.now(UTC),
        ready_at=datetime.now(UTC),
        thumbnail_generated_at=datetime.now(UTC),
        deleted_at=None,
    )

    def repo_factory(session: object) -> _AssetRepo:
        return _AssetRepo(asset)

    monkeypatch.setattr(tools, "AssetRepository", repo_factory)
    ctx = McpCallContext(
        user_id=user_id,
        scopes=("moments.write",),
        method="mcp",
        actor_id="test-client",
        request_id="request-id",
        session=cast(Any, object()),
        object_storage=cast(Any, _Storage()),
    )

    media = await tools.build_ready_asset_media(ctx, [str(asset.id)])

    assert media == [
        {
            "assetId": str(asset.id),
            "kind": "image",
            "contentType": "image/png",
            "sizeBytes": 2_901_132,
            "thumbnailUrl": f"https://storage.example/thumbnail/{asset.id}",
            "downloadUrl": f"https://storage.example/download/{asset.id}",
        }
    ]
