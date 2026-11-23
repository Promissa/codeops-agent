"""Fail-closed admission rules for learned guidance and policy changes."""

import re
from typing import Any

from pydantic import BaseModel

from codeops.evolution.models import SafetyReport
from codeops.safety.secret_filter import SecretFilter
from codeops.trust.policy import HARD_READ_DENIES, TrustPolicy


class EvolutionSafetyGuard:
    """Scan untrusted inputs before they can become durable guidance."""

    _injection = tuple(map(lambda expression: re.compile(expression, re.I), (
        r"\b(?:ignore|disregard|override|disable|bypass|evade|skip|circumvent|turn off)\b"
        r"[^.!?\n]{0,110}\b(?:trust[ _-]?(?:policy|boundar(?:y|ies))|sandbox|bwrap|"
        r"patch[ _-]?policy|command[ _-]?policy|secret.filter(?:ing)?|safety (?:rules|gates)|"
        r"system (?:prompt|rules|instructions)|(?:previous|prior|all) instructions)",
        r"\b(?:read|open|cat|dump|extract|print|load|access|copy|mount|list|inspect|search|look (?:at|in))\b"
        r"[^.!?\n]{0,70}(?:~[ /]|\$(?:\{HOME\}|HOME\b)|/(?:home|root|etc/shadow)(?:/|\b)|"
        r"host(?:'s)? home|(?<!\w)\.(?:ssh|env|aws|gnupg)(?:\b|_))",
        r"\b(?:exfiltrate|upload|send|transmit|leak|disclose|reveal|export|share)\b"
        r"[^.!?\n]{0,70}\b(?:secrets?|credentials?|private keys?|api keys?|system prompt|environment variables)\b",
        r"\b(?:set|switch|change|use)\b[^.!?\n]{0,40}\b(?:sandbox|bwrap)\b"
        r"[^.!?\n]{0,35}\b(?:none|off|disabled|unrestricted|host mode)\b",
        r"(?:\[/?INST\]|<\|(?:system|im_start|im_end|assistant|developer)\|>|</?system>)",
    )))
    _secret_markers = re.compile(
        r"\[REDACTED\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\b(?:Bearer|Basic)\s+[A-Za-z0-9._+/=-]{16,}|"
        r"\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|"
        r"aws[_-]?secret[_-]?access[_-]?key)[\"']?\s*[:=]\s*[\"']?[^\s,}\]]+",
        re.I,
    )
    _credential_fields = {
        "password", "passwd", "apikey", "accesstoken", "clientsecret", "awssecretaccesskey",
    }

    def scan(self, *values: Any) -> SafetyReport:
        findings: set[str] = set()
        secret_filter = SecretFilter()

        def visit(value: Any) -> None:
            if isinstance(value, BaseModel):
                visit(value.model_dump(mode="json"))
            elif isinstance(value, dict):
                for key, item in value.items():
                    visit(key)
                    if re.sub(r"[^a-z]", "", str(key).lower()) in self._credential_fields and item:
                        findings.add("credential or redacted secret detected")
                    visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)
            elif isinstance(value, str):
                normalized = re.sub(r"[\x00-\x1f\u200b-\u200f\u202a-\u202e]+", " ", value)
                normalized = normalized.replace("`", "").replace("*", "")
                if (
                    not secret_filter.scan(value).passed or not secret_filter.scan(normalized).passed
                    or self._secret_markers.search(normalized)
                ):
                    findings.add("credential or redacted secret detected")
                if any(pattern.search(normalized) for pattern in self._injection):
                    findings.add("prompt injection or policy bypass instruction detected")

        for value in values:
            visit(value)
        return SafetyReport(findings=tuple(sorted(findings)))

    def require_safe(self, *values: Any) -> None:
        if report := self.scan(*values).findings:
            raise ValueError("; ".join(report))

    def policy(self, candidate: TrustPolicy, current: TrustPolicy, *, allow_network: bool = False) -> None:
        for patterns in (candidate.read_deny_globs, candidate.outbound_deny_globs):
            if not set(HARD_READ_DENIES).issubset(patterns):
                raise ValueError("Policy must retain all hard deny globs for reads and outbound access.")
        if candidate.sandbox.network_enabled and not current.sandbox.network_enabled and not allow_network:
            raise ValueError("Enabling network requires explicit --allow-network opt-in.")
        if current.sandbox.backend == "bwrap" and candidate.sandbox.backend != "bwrap":
            raise ValueError("A policy proposal cannot disable the existing sandbox.")
        # TrustPolicy rejects unknown settings, including attempts to toggle secret filtering.
