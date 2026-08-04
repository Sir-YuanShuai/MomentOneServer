from app.infrastructure.binding_codes.generator import generate_binding_code


def test_generate_binding_code_format() -> None:
    code = generate_binding_code(24)
    assert code.startswith("BIND-")
    assert len(code) == len("BIND-") + 24


def test_generate_binding_code_uniqueness() -> None:
    codes = {generate_binding_code(24) for _ in range(100)}
    assert len(codes) == 100


def test_generate_binding_code_custom_length() -> None:
    code = generate_binding_code(12)
    assert code.startswith("BIND-")
    assert len(code) == len("BIND-") + 12
