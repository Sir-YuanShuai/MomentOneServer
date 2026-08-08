# Vendored A2UI v0.9 schemas

Source: official A2UI repository, commit `ec97cb0`, directory `specification/v0_9/`.
Vendored on 2026-08-08 for deterministic server-side validation.

The upstream schemas are licensed under Apache-2.0. `server_to_client.json` uses
a catalog-neutral `catalog.json` reference; the runtime validator aliases the
vendored Basic Catalog to that URI exactly as the upstream conformance runner does.
