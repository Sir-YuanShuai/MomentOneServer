from dataclasses import dataclass, field


def _empty_details() -> dict[str, object]:
    return {}


@dataclass(slots=True)
class ApplicationError(Exception):
    code: str
    message: str
    status_code: int = 400
    details: dict[str, object] = field(default_factory=_empty_details)
