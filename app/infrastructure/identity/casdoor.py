from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    issuer: str
    subject: str
    audience: str | None = None
    email: str | None = None
    display_name: str | None = None


class CasdoorTokenVerifier:
    """Casdoor OIDC token verification adapter.

    Signature, issuer, audience and expiry validation will be implemented once
    the deployment's discovery/JWKS configuration is provided.
    """
