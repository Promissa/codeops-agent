"""Render the run's trust evidence as a standalone HTML document."""

from collections.abc import Iterable
from html import escape
import json
from pathlib import Path
import re
from string import Template
from typing import Any

from codeops.agents.reviewer import ReviewResult
from codeops.core.models import TaskState
from codeops.gate.effect_map import EffectMap
from codeops.gate.evidence_gate import EvidenceGateResult
from codeops.reporting.summary import determine_verdict, verification_outcome
from codeops.reporting.trace_events import TraceEvent, TraceRedactor, latest_event_detail
from codeops.tools.patch_tool import PatchSummary, PatchTool
from codeops.trust.preflight import TaskTrustContract


BOUNDARY_KINDS = {"trust_decision", "policy_check", "outbound_filter"}
_PYTHON_SYMBOL = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+(\w+)\b")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@(?: (.*))?$")


def render_trace_report(
    state: TaskState, review: ReviewResult, contract: TaskTrustContract | None,
    events: list[TraceEvent],
) -> str:
    """Render recorded facts without fetching resources or accessing the target repository."""
    verdict = determine_verdict(state, review, events, contract)
    redactor = TraceRedactor()
    safe_events = [TraceEvent.model_validate(redactor.redact(event)) for event in events]
    safe_state = TaskState.model_validate(redactor.redact(state.model_dump(mode="json", exclude={
        "patch_diff": True,
        "test_results": {"__all__": {"stdout", "stderr"}},
    })))
    safe_review = ReviewResult.model_validate(redactor.redact(review))
    safe_contract = TaskTrustContract.model_validate(redactor.redact(contract)) if contract else None
    diff, diff_status = _diff_summary(state, safe_events, redactor)
    symbols = redactor.redact(_patch_symbols(state.patch_diff or ""))
    gate_data = latest_event_detail(safe_events, "evidence_gate", "gate")
    gate = EvidenceGateResult.model_validate(gate_data) if gate_data else None
    effect_data = latest_event_detail(safe_events, "evidence_gate", "effect_map")
    effects = EffectMap.model_validate(effect_data) if effect_data else None

    boundaries = [event for event in safe_events if event.kind in BOUNDARY_KINDS]
    allowed = sum(event.status == "allowed" for event in boundaries)
    denied = sum(event.status == "denied" for event in boundaries)
    redactions = redactor.count + sum(
        event.details.get(key, 0)
        for event in safe_events
        for key in ("redactions", "trace_redactions")
        if isinstance(event.details.get(key, 0), int)
    )
    acceptance = safe_state.acceptance_contract
    cost = safe_state.cost_trace
    estimate = "Unmeasured" if cost.estimated_cost_usd is None else f"${cost.estimated_cost_usd:.6f}"
    outcomes = [verification_outcome(result) for result in state.test_results]
    observed_checks = list(zip((result.command for result in safe_state.test_results), outcomes))
    gate_metrics = [
        ("Actual net diff policy", gate.actual_diff_status),
        ("Differential transitions", " · ".join(f"{count} {status}" for status, count in gate.transitions.items())),
    ] if gate else []
    metrics = _fact_list([
        ("Lifecycle", safe_state.lifecycle_state or "Unrecorded"),
        ("Termination", safe_state.termination_reason or "Unrecorded"),
        ("Workflow status", safe_state.status),
        ("Patch", f"{diff_status} · {len(diff.changed_files)} files · {diff.changed_loc} changed lines"),
        ("Test commands", f"{outcomes.count('passed')} passed · {outcomes.count('failed')} failed · "
         f"{outcomes.count('unobserved')} unconfirmed" if outcomes else "None executed"),
        *gate_metrics,
        ("Boundary checks", f"{allowed} allowed · {denied} denied · {redactions} redactions"),
        ("Execution time", f"{cost.wall_time_seconds:.3f} s"),
        ("Model usage", f"{cost.llm_calls} calls · {cost.llm_input_tokens:,} in / {cost.llm_output_tokens:,} out"),
        ("Estimated cost", estimate),
        ("Tool activity", f"{cost.tool_calls} tool calls · {cost.codegraph_calls} CodeGraph calls"),
    ])
    findings = [*safe_review.violations, *safe_review.warnings]
    if gate:
        findings.extend(gate.violations)
        findings.extend(gate.next_steps)
    if not safe_state.evidence_matrix_path or not safe_state.intent_manifest_path:
        findings.append("Evidence matrix or intent manifest unavailable.")
    if outcomes and "passed" not in outcomes and "failed" not in outcomes:
        findings.append("Command results did not confirm that tests ran.")
    if acceptance:
        findings.extend(acceptance.ambiguity_questions)
    for event in safe_events:
        if event.kind == "error" or (event.kind == "policy_check" and event.stage == "run"):
            findings.append(str(event.details.get("message") or event.details.get("reason") or event.summary))
        elif event.kind == "patch" and event.status == "failed":
            findings.append(f"Patch application failed. {event.details.get('stderr', '')}".strip())
        elif event.kind == "policy_check" and event.stage == "patch" and event.status == "denied":
            findings.extend(event.details.get("violations", []))

    page = Template(Path(__file__).with_name("trace_page.html").read_text(encoding="utf-8"))
    return page.substitute(
        task_id=_html(safe_state.task_id),
        verdict=verdict,
        intent=_html(acceptance.summary if acceptance else "Acceptance contract unavailable"),
        expected=_html(acceptance.user_visible_after or "Unspecified" if acceptance else "Unspecified"),
        findings=_list(dict.fromkeys(findings), "No review findings recorded."),
        metrics=metrics,
        issue=_html(safe_state.issue_text or "Issue text unavailable."),
        timeline=_execution_graph(safe_events),
        impact=_impact_graph(safe_state, safe_events, diff, diff_status, symbols, observed_checks, effects),
        boundary_scope=_boundary_scope(safe_contract, safe_state.sandbox_mode),
        boundary_events=_list(
            (f"{event.status} · {event.summary}" for event in boundaries), "No boundary events recorded.",
        ),
        evolution=_evolution_entries(safe_events),
        node_options="".join(
            f'<option value="{_html(event.event_id)}">{event.seq} · {_html(event.summary)}</option>'
            for event in safe_events
        ),
        data=_safe_json({
            "run_dir": str(safe_state.run_dir), "task_id": safe_state.task_id,
            "events": [event.model_dump(mode="json") for event in safe_events],
        }),
    )


def _html(value: object) -> str:
    return escape(str(value), quote=True)


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True).replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")


def _list(items: Iterable[str], empty: str) -> str:
    values = "".join(f"<li>{_html(value)}</li>" for value in items)
    return f"<ul>{values}</ul>" if values else f"<p>{_html(empty)}</p>"


def _fact_list(facts: Iterable[tuple[str, str]]) -> str:
    return "<dl>" + "".join(
        f"<div><dt>{_html(label)}</dt><dd>{_html(value)}</dd></div>" for label, value in facts
    ) + "</dl>"


def _diff_summary(
    state: TaskState, events: list[TraceEvent], redactor: TraceRedactor,
) -> tuple[PatchSummary, str]:
    latest = next(
        (event for event in reversed(events) if event.kind in {"patch", "policy_check"} and "diff" in event.details),
        None,
    )
    applied = any(event.kind == "patch" and event.summary == "Apply patch" and event.status == "ok" for event in events)
    if latest:
        diff = PatchSummary.model_validate(latest.details["diff"])
        if latest.status == "denied":
            return diff, "Rejected"
        if applied:
            return diff, "Applied"
        if any(event.kind == "patch" and event.status == "failed" for event in events):
            return diff, "Apply failed"
        return diff, "Proposed"
    if state.patch_diff:
        diff = PatchTool(state.repo_path).summarize(state.patch_diff)
        return PatchSummary.model_validate(redactor.redact(diff)), "Application unrecorded"
    return PatchSummary(changed_files=[], changed_loc=0), "Unavailable"


def _svg_node(
    x: int, y: int, width: int, label: str, detail: str, status: str, event_id: str | None = None,
) -> str:
    attrs = f'class="node {_html(status)}"'
    if event_id is not None:
        attrs += f' tabindex="0" role="button" data-event-id="{_html(event_id)}" aria-label="{_html(label)}"'
    limit = (width - 24) // 7
    short_label = label if len(label) <= limit else label[:limit - 1] + "…"
    short_detail = detail if len(detail) <= limit else detail[:limit - 1] + "…"
    return (
        f'<g {attrs}><title>{_html(label)} · {_html(detail)}</title>'
        f'<rect x="{x}" y="{y}" width="{width}" height="48" rx="6"/>'
        f'<text x="{x + 12}" y="{y + 19}">{_html(short_label)}</text>'
        f'<text class="node-detail" x="{x + 12}" y="{y + 37}">{_html(short_detail)}</text></g>'
    )


def _execution_graph(events: list[TraceEvent]) -> str:
    if not events:
        return '<svg id="execution-dag" viewBox="0 0 620 64" role="group" aria-label="No events"><text x="14" y="35">No events recorded</text></svg>'
    positions: dict[str, int] = {}
    edges = []
    nodes = []
    for index, event in enumerate(events):
        y = 12 + index * 60
        if index:
            edges.append(f'<path class="edge" d="M306 {y - 12} V{y}" marker-end="url(#timeline-arrow)"/>')
        parent = positions.get(event.parent_event_id or "")
        if parent is not None:
            edges.append(f'<path class="edge parent" d="M45 {parent} H{18 + index % 4 * 6} V{y + 24} H45"/>')
        positions[event.event_id] = y + 24
        duration = f" · {event.duration_ms:g} ms" if event.duration_ms is not None else ""
        nodes.append(_svg_node(
            45, y, 555, f"{event.seq} · {event.summary}",
            f"{event.stage} / {event.kind} · {event.status}{duration}", event.status, event.event_id,
        ))
    return (
        f'<svg id="execution-dag" viewBox="0 0 620 {12 + len(events) * 60}" role="group" aria-label="Execution timeline">'
        '<defs><marker id="timeline-arrow" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="6" '
        'markerHeight="6" orient="auto"><path d="M0 0 L8 4 L0 8 Z" class="arrow"/></marker></defs>'
        + "".join(edges + nodes) + "</svg>"
    )


def _patch_symbols(diff: str) -> dict[str, list[str]]:
    """Read Python definitions visible in changed hunks and their context."""
    symbols: dict[str, list[str]] = {}
    path = ""
    old_path = ""
    old_count = new_count = 0
    scopes: list[tuple[int, str]] = []
    for line in diff.splitlines():
        if hunk := _HUNK.match(line):
            old_count = int(hunk[1]) if hunk[1] is not None else 1
            new_count = int(hunk[2]) if hunk[2] is not None else 1
            definition = _PYTHON_SYMBOL.match(hunk[3] or "")
            scopes = [(len(definition[1]), definition[2])] if definition else []
        elif old_count or new_count:
            prefix, code = line[:1], line[1:]
            if prefix not in {" ", "+", "-"}:
                continue
            old_count -= prefix != "+"
            new_count -= prefix != "-"
            if not path.endswith(".py") or not code.strip():
                continue
            indent = len(code) - len(code.lstrip())
            while scopes and indent <= scopes[-1][0]:
                scopes.pop()
            if definition := _PYTHON_SYMBOL.match(code):
                scopes.append((indent, definition[2]))
            if prefix in {"+", "-"} and scopes:
                symbols.setdefault(path, []).append(".".join(name for _, name in scopes))
        elif line.startswith("--- "):
            old_path = line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            path = old_path if line[4:] == "/dev/null" else line[4:].removeprefix("b/")
    return {path: list(dict.fromkeys(names)) for path, names in symbols.items()}


def _impact_graph(
    state: TaskState, events: list[TraceEvent], diff: PatchSummary, diff_status: str,
    symbols: dict[str, list[str]], observed_checks: list[tuple[str, str]],
    effects: EffectMap | None,
) -> str:
    evidence = state.graph_evidence
    routing_paths = {
        path for event in events if event.kind == "routing" for path in event.details.get("files", [])
    }
    targets = {
        (symbol.path, symbol.symbol): ("Graph / fallback target", "inconclusive")
        for symbol in (evidence.target_symbols if evidence else [])
    }
    targets.update({(path, Path(path).stem): ("Routing target", "inconclusive") for path in sorted(routing_paths)})
    if effects:
        for ref in effects.affected_callers:
            if ref in effects.unobserved_callers:
                details = ("Unobserved caller", "inconclusive")
            elif ref in effects.failing_callers:
                details = ("Caller reached by a failing test", "failed")
            else:
                details = ("Caller exercised", "ok")
            targets[ref.path, ref.symbol] = details

    checks = dict.fromkeys(state.impact_envelope.affected_tests if state.impact_envelope else [], "skipped")
    plan = state.verification_plan
    if plan:
        for command in [*plan.acceptance_tests, *plan.affected_tests, *plan.module_tests, *plan.full_tests]:
            checks[" ".join(command.command)] = "skipped"
    for command, outcome in observed_checks:
        status = {"passed": "ok", "failed": "failed", "unobserved": "inconclusive"}[outcome]
        if checks.get(command) != "failed":
            checks[command] = status

    columns = (28, 344, 660)
    nodes = [
        f'<text x="{x}" y="24">{heading}</text>'
        for x, heading in zip(columns, ("Targets and affected callers", "Patch files and symbols", "Verification commands / targets"))
    ]
    edges = []
    patch_status = (
        "failed" if diff_status in {"Rejected", "Apply failed"} or state.verdict == "regressed"
        else "ok" if state.verdict == "verified" else "inconclusive"
    )
    file_rows = {}
    patch_y = 42
    for path in diff.changed_files:
        file_rows[path] = patch_y
        nodes.append(_svg_node(columns[1], patch_y, 288, path, diff_status, patch_status))
        patch_y += 60
        edited = [ref.symbol.rsplit(".", 1)[-1] for ref in effects.edited_symbols if ref.path == path] if effects else symbols.get(path, [])
        for symbol in dict.fromkeys(edited):
            detail = "Python hunk / definition" if path.endswith(".py") else "Edited symbol"
            nodes.append(_svg_node(columns[1] + 15, patch_y, 273, symbol, detail, patch_status))
            patch_y += 60
    for index, ((path, symbol), (origin, status)) in enumerate(targets.items()):
        y = 42 + index * 60
        label = path if origin == "Routing target" else f"{symbol} · {path}"
        nodes.append(_svg_node(columns[0], y, 288, label, origin, status))
        if path in file_rows:
            edges.append(f'<path class="edge" d="M316 {y + 24} C330 {y + 24} 330 {file_rows[path] + 24} 344 {file_rows[path] + 24}"/>')
    for index, (command, status) in enumerate(checks.items()):
        label = {"ok": "Passed", "failed": "Failed", "skipped": "Unobserved / not run",
                 "inconclusive": "Execution unconfirmed"}[status]
        y = 42 + index * 60
        nodes.append(_svg_node(columns[2], y, 288, command, label, status))
        for path in command.split():
            file_y = file_rows.get(path.split("::", 1)[0])
            if file_y is not None:
                edges.append(f'<path class="edge" d="M632 {file_y + 24} C646 {file_y + 24} 646 {y + 24} 660 {y + 24}"/>')
    for column, items in zip(columns, (targets, file_rows, checks)):
        if not items:
            nodes.append(_svg_node(column, 42, 288, "No evidence recorded", "Unobserved", "skipped"))
    height = max(patch_y, 42 + max(1, len(targets), len(checks)) * 60)
    return f'<svg id="impact-evidence-graph" viewBox="0 0 970 {height}" role="img" aria-label="Impact and evidence graph">{"".join(edges + nodes)}</svg>'


def _boundary_scope(contract: TaskTrustContract | None, sandbox_mode: str) -> str:
    if contract is None:
        return _fact_list([
            ("Local scope", "No approved workspace scope recorded"),
            ("Outbound scope", "No trust contract recorded"),
            ("Runtime", sandbox_mode),
        ])
    return _fact_list([
        ("Trust approval", "Approved" if contract.approved else "Unapproved"),
        ("Visible workspace globs", ", ".join(contract.read_allow_globs) or "None"),
        ("Local omissions", ", ".join([*contract.excluded_scopes, *contract.read_deny_globs]) or "None"),
        ("Outbound allowed globs", ", ".join(contract.outbound_allow_globs) or "None"),
        ("Outbound denied globs", ", ".join(contract.outbound_deny_globs) or "None"),
        ("Patch budget", f"{contract.write_budget.max_files_changed} files · "
         f"{contract.write_budget.max_loc_changed} changed lines"),
        ("Approved test commands", ", ".join(" ".join(argv) for argv in contract.allowed_test_commands) or "None"),
        ("Runtime", f"{contract.sandbox.backend} · " + (
            "host network namespace" if contract.sandbox.backend == "none" else
            "network enabled" if contract.sandbox.network_enabled else "private network namespace"
        )),
    ])


def _evolution_entries(events: list[TraceEvent]) -> str:
    def skills(kind: str) -> dict:
        return {
            (item["name"], item.get("revision_hash")): item
            for event in events if event.kind == kind
            for item in event.details.get("skills", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }

    attributed = skills("evolution_attributed")
    entries = []
    for key, skill in skills("evolution_loaded").items():
        stats = attributed.get(key, skill).get("evidence_stats", {})
        scope = skill.get("scope", {})
        facts = _fact_list([
            ("Owner", skill.get("owner", "Unrecorded")),
            ("Scope", " · ".join(f"{group}: {', '.join(values)}" for group, values in scope.items() if values)
             or "Issue keywords"),
            ("Source runs", ", ".join(skill.get("source_runs", [])) or "Unrecorded"),
            ("Revision", skill.get("revision_hash", "Unrecorded")),
            ("Evidence", " · ".join(f"{label.replace('_', ' ')} {count}" for label, count in stats.items())
             or "No outcomes recorded"),
        ])
        entries.append(f"<li><strong>{_html(skill['name'])}</strong>{facts}</li>")
    return "<ul>" + "".join(entries) + "</ul>" if entries else "<p>No evolved rules or skills recorded.</p>"
