"""Run directory path helpers."""

from dataclasses import dataclass
from pathlib import Path


ARTIFACT_FILENAMES = {
    "task": "task.json",
    "trust_contract": "trust_contract.yaml",
    "repo_sketch": "repo_sketch.md",
    "repo_sketch_json": "repo_sketch.json",
    "issue_routing": "issue_routing.json",
    "acceptance_contract": "acceptance_contract.yaml",
    "graph_evidence": "graph_evidence.json",
    "impact_envelope": "impact_envelope.yaml",
    "patch_plan": "patch_plan.yaml",
    "verification_plan": "verification_plan.yaml",
    "patch": "patch.diff",
    "test_results": "test_results.json",
    "workspace_diff": "workspace_diff.json",
    "effect_map": "effect_map.json",
    "differential_results": "differential_results.json",
    "evidence_gate": "evidence_gate.json",
    "evidence_matrix": "evidence_matrix.md",
    "intent_manifest": "intent_manifest.md",
    "cost_trace": "cost_trace.json",
    "final_report": "final_report.md",
    "trace_events": "trace_events.jsonl",
    "trace_report": "trace_report.html",
}


@dataclass(frozen=True)
class RunPaths:
    """Paths for one `.runs/<task_id>/` artifact directory."""

    task_id: str
    root: Path = Path(".runs")

    @classmethod
    def from_run_dir(cls, run_dir: Path) -> "RunPaths":
        return cls(task_id=run_dir.name, root=run_dir.parent)

    @property
    def run_dir(self) -> Path:
        return self.root / self.task_id

    def ensure(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return self.run_dir

    def artifact_path(self, artifact_name: str) -> Path:
        filename = ARTIFACT_FILENAMES.get(artifact_name, artifact_name)
        return self.run_dir / filename

    @property
    def trace_events_path(self) -> Path:
        return self.artifact_path("trace_events")

    @property
    def trace_report_path(self) -> Path:
        return self.artifact_path("trace_report")
