"""Bind evolution reads and finalization to the configured host repository."""

from collections.abc import Iterable
from pathlib import Path

from codeops.core.models import AcceptanceContract, TaskState
from codeops.evolution.critic import ReviewForkCritic, ReviewInput
from codeops.evolution.evaluator import guidance_key
from codeops.evolution.loader import EvolutionLoader
from codeops.evolution.models import Skill
from codeops.reporting.trace_events import TraceRecorder
from codeops.runtime.lifecycle import RunTerminated, TerminationReason


class RunEvolution:
    def __init__(self, repo: Path, trace: TraceRecorder) -> None:
        try:
            self.loader = EvolutionLoader.for_repo(repo)
        except (ValueError, OSError, RunTerminated) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "Evolution store cannot be read safely."
            raise RunTerminated(TerminationReason.POLICY_DENIED, message) from exc
        self.trace = trace
        self.skills: tuple[Skill, ...] = ()
        self.load_event: str | None = None

    def disclose(
        self, state: TaskState, contract: AcceptanceContract, files: Iterable[str],
    ) -> AcceptanceContract:
        self.skills = self.loader.load(profile=state.project_profile, issue=state.issue_text, files=files)
        self._record_load()
        return contract.model_copy(update={"evolution_guidance": {skill.name: skill.body for skill in self.skills}})

    def _record_load(self) -> None:
        if self.loader.identity is None or self.load_event is not None:
            return
        event = self.trace.record(
            "evolution", "evolution_loaded", "ok", "Load scoped procedural guidance and frozen memory",
            details={
                "skills": [{**skill.provenance(), "guidance_key": guidance_key(skill.body)} for skill in self.skills],
                "memory": self.loader.memory.model_dump(),
            },
        )
        self.load_event = event.event_id

    def finalize(self, state: TaskState) -> None:
        identity = self.loader.identity
        store = self.loader.store
        if identity is None or state.repo_path.resolve() != store.repo or state.verdict is None:
            return
        self._record_load()
        assert self.load_event is not None
        try:
            review = next(event for event in reversed(self.trace.events) if event.kind == "review")
            with store.transaction(expected=identity):
                attributed = []
                for skill in self.skills:
                    try:
                        current = store.attribute(
                            skill.name, skill.revision_hash, self.load_event, verdict=state.verdict, now=review.timestamp,
                        )
                    except (OSError, ValueError, RunTerminated):
                        continue
                    if current:
                        attributed.append(current.provenance())
                if self.skills:
                    self.trace.record(
                        "evolution", "evolution_attributed", "ok" if len(attributed) == len(self.skills) else "warning",
                        "Attribute the Evidence Gate outcome to loaded skill revisions",
                        details={"skills": attributed, "verdict": state.verdict},
                    )
            if state.verdict in {"regressed", "blocked"}:
                task = ReviewInput.model_validate(state)
                logs = tuple(text for test in state.test_results for text in (test.stdout, test.stderr))
                ReviewForkCritic(store).review(task, self.trace.events, logs=logs, expected=identity)
        except (OSError, ValueError, RunTerminated):
            self.trace.record(
                "evolution", "error", "warning", "Could not finalize evolution evidence safely",
            )
