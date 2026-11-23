"""Regex-based secret filtering."""

import re

from pydantic import BaseModel


_ASSIGNMENT_VALUE = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)"""


class SecretFinding(BaseModel):
    kind: str
    matched: str


class SecretScanResult(BaseModel):
    redacted_text: str
    findings: list[SecretFinding]

    @property
    def passed(self) -> bool:
        return not self.findings


class SecretFilter:
    """Detect and redact common secrets before artifact export."""

    PATTERNS = {
        "private_key": re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"
        ),
        "api_key_assignment": re.compile(r"(?i)\bapi[_-]key[ \t]*=[ \t]*" + _ASSIGNMENT_VALUE),
        "password_assignment": re.compile(
            r"(?i)\b(?:password|passwd)[ \t]*=[ \t]*" + _ASSIGNMENT_VALUE,
        ),
        "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "database_url": re.compile(
            r"\b(?:postgres|postgresql|mysql|mongodb)://[^\s]+",
            re.IGNORECASE,
        ),
    }

    def scan(self, text: str) -> SecretScanResult:
        redacted = text
        findings: list[SecretFinding] = []
        for kind, pattern in self.PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append(SecretFinding(kind=kind, matched=match.group(0)))
            if kind == "api_key_assignment":
                redacted = pattern.sub(self._redact_api_assignment, redacted)
            else:
                redacted = pattern.sub(f"[REDACTED {kind}]", redacted)
        return SecretScanResult(redacted_text=redacted, findings=findings)

    def _redact_api_assignment(self, match: re.Match[str]) -> str:
        kind = "openai_api_key" if self.PATTERNS["openai_api_key"].search(match[0]) else "api_key_assignment"
        return f"[REDACTED {kind}]"
