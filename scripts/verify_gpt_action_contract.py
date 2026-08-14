#!/usr/bin/env python3
"""Verify the deployed GPT attachment Action contract without credentials."""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DEFAULT_BASE_URL = "https://moment-one-api.yuanshuai.fun"


def fetch_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=15) as response:  # noqa: S310 - fixed operator URL
            if response.status != 200:
                raise RuntimeError(f"{url} returned HTTP {response.status}")
            return json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"cannot read {url}: {exc}") from exc


def verify(base_url: str) -> None:
    base_url = base_url.rstrip("/")
    health = fetch_json(f"{base_url}/healthz")
    if health.get("status") != "ok":
        raise RuntimeError("healthz did not report status=ok")

    schema = fetch_json(f"{base_url}/openapi.json")
    operation = schema["paths"]["/v1/moments/from-openai-files"]["post"]
    parameters = operation.get("parameters", [])
    if not any(
        item.get("name") == "Idempotency-Key"
        and item.get("in") == "header"
        and item.get("required") is True
        for item in parameters
    ):
        raise RuntimeError("required Idempotency-Key header is missing")

    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    request_ref = request_schema.get("$ref", "")
    request_name = request_ref.rsplit("/", 1)[-1]
    request_model = schema["components"]["schemas"][request_name]
    file_refs = request_model.get("properties", {}).get("openaiFileIdRefs")
    if file_refs is None or file_refs.get("maxItems") != 10:
        raise RuntimeError("openaiFileIdRefs is missing or has an unexpected limit")

    file_ref_name = file_refs["items"]["$ref"].rsplit("/", 1)[-1]
    required_file_fields = set(schema["components"]["schemas"][file_ref_name].get("required", []))
    expected = {"name", "id", "mime_type", "download_link"}
    if required_file_fields != expected:
        raise RuntimeError("OpenAI file reference fields do not match the GPT contract")

    print("production GPT attachment Action contract OK")


if __name__ == "__main__":
    verify(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL)
