"""绑定会话码生成器。"""

import secrets


def generate_binding_code(length: int = 24) -> str:
    """生成 BIND- 前缀 + 指定长度随机字符的绑定码。

    使用 secrets.token_urlsafe 生成 URL 安全的随机串，截取到指定长度。
    """
    # token_urlsafe(n) 返回约 4n/3 个字符，多取一些再截断
    raw = secrets.token_urlsafe(length)
    return f"BIND-{raw[:length]}"
