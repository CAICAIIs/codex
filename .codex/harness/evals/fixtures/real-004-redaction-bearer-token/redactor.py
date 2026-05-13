import re


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]+"),
]


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
