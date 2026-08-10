from typing import Final

LOGIN_PROVIDER_FIELDS: Final = ("github", "gitee", "google", "qq")
LOGIN_PROVIDER_PROPERTY_NAMES: Final = {
    "github": "GitHub",
    "gitee": "Gitee",
    "google": "Google",
    "qq": "QQ",
}


def normalize_login_provider(provider: str | None) -> str | None:
    if not provider:
        return None
    normalized = provider.lower()
    return normalized if normalized in LOGIN_PROVIDER_FIELDS else None


def has_casdoor_provider_link(casdoor_user: dict[str, object], provider: str) -> bool:
    field = normalize_login_provider(provider)
    if field is None:
        return False
    provider_name = LOGIN_PROVIDER_PROPERTY_NAMES[field]
    raw_props = casdoor_user.get("properties")
    props = raw_props if isinstance(raw_props, dict) else {}
    return bool(
        casdoor_user.get(field)
        or props.get(f"oauth_{provider_name}_id")
        or props.get(f"oauth_{provider_name}_accessToken")
    )


def login_providers_from_casdoor(casdoor_user: dict[str, object]) -> list[dict[str, str]]:
    raw_props = casdoor_user.get("properties")
    props = raw_props if isinstance(raw_props, dict) else {}
    providers: list[dict[str, str]] = []
    for field in LOGIN_PROVIDER_FIELDS:
        if has_casdoor_provider_link(casdoor_user, field):
            provider_name = LOGIN_PROVIDER_PROPERTY_NAMES[field]
            display = props.get(f"oauth_{provider_name}_displayName")
            providers.append(
                {
                    "provider": field,
                    "handle": str(display) if display else str(casdoor_user.get(field) or ""),
                }
            )
    return providers


__all__ = [
    "LOGIN_PROVIDER_FIELDS",
    "LOGIN_PROVIDER_PROPERTY_NAMES",
    "has_casdoor_provider_link",
    "login_providers_from_casdoor",
    "normalize_login_provider",
]
