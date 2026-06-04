"""Regex-based secret filtering."""

import re

from pydantic import BaseModel


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
        "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        "private_key": re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"
        ),
        "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "database_url": re.compile(
            r"\b(?:postgres|postgresql|mysql|mongodb)://[^\s]+",
            re.IGNORECASE,
        ),
        "password_assignment": re.compile(
            r"(?i)\b(password|passwd)\s*=\s*[^\s]+"
        ),
    }

    def scan(self, text: str) -> SecretScanResult:
        redacted = text
        findings: list[SecretFinding] = []
        for kind, pattern in self.PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append(SecretFinding(kind=kind, matched=match.group(0)))
            redacted = pattern.sub(f"[REDACTED {kind}]", redacted)
        return SecretScanResult(redacted_text=redacted, findings=findings)
