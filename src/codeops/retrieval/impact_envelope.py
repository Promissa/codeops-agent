"""ImpactEnvelope construction."""

from codeops.core.models import GraphEvidence, ImpactEnvelope, SymbolRef


HIGH_RISK_MARKERS = (
    "/auth/",
    "/billing/",
    "/payment/",
    "/security/",
    "/crypto/",
    "/migrations/",
    "/infra/",
    "/.github/workflows/",
)


class ImpactEnvelopeBuilder:
    """Build conservative edit boundaries from available evidence."""

    def build(
        self,
        *,
        target_symbols: list[SymbolRef] | None = None,
        graph_evidence: GraphEvidence | None = None,
        fallback_files: list[str] | None = None,
        affected_tests: list[str] | None = None,
        forbidden_changes: list[str] | None = None,
    ) -> ImpactEnvelope:
        symbols = target_symbols or []
        if graph_evidence is not None:
            symbols = [*symbols, *graph_evidence.target_symbols]

        affected_files = _dedupe(
            [
                symbol.path
                for symbol in symbols
                if symbol.path
            ]
            + (fallback_files or [])
        )
        graph_tests = (
            graph_evidence.affected_tests if graph_evidence is not None else []
        )
        affected_tests = _dedupe([*graph_tests, *(affected_tests or [])])
        allowed_files = _dedupe([*affected_files, *affected_tests])
        high_risk = any(_is_high_risk(path) for path in allowed_files)

        return ImpactEnvelope(
            target_symbols=_dedupe_symbols(symbols),
            allowed_files=allowed_files,
            affected_files=affected_files,
            affected_tests=affected_tests,
            forbidden_changes=forbidden_changes
            or [
                "dependency change",
                "public API signature change",
                "unrelated formatting",
            ],
            risk_level="high" if high_risk else "low",
            requires_human_approval=high_risk,
        )


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _dedupe_symbols(symbols: list[SymbolRef]) -> list[SymbolRef]:
    seen = set()
    deduped = []
    for symbol in symbols:
        key = (symbol.symbol, symbol.path, symbol.kind, symbol.line)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(symbol)
    return deduped


def _is_high_risk(path: str) -> bool:
    normalized = f"/{path.strip('/')}/"
    return any(marker in normalized for marker in HIGH_RISK_MARKERS)
