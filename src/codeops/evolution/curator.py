"""Age unused skills and demote revisions associated with poor outcomes."""

from datetime import datetime, timedelta

from codeops.evolution.models import utc_now
from codeops.evolution.store import EvolutionStore


class EvolutionCurator:
    def __init__(self, store: EvolutionStore) -> None:
        self.store = store

    def curate(self, *, now: datetime | None = None) -> dict[str, str]:
        now = now or utc_now()
        changes = {}
        if self.store.identity is None:
            return changes
        with self.store.transaction():
            for header in self.store.index():
                if header.status == "pinned":
                    continue
                age = now - max(
                    header.created_at, header.last_used_at or header.created_at,
                    header.restored_at or header.created_at,
                )
                stats = header.evidence_stats
                adverse = stats.regressed + stats.human_rejections
                skill = None
                if age >= timedelta(days=90) or (
                    header.status == "active" and (
                        age >= timedelta(days=30)
                        or (header.owner == "agent" and stats.loads >= 3 and adverse * 2 >= stats.loads)
                    )
                ):
                    skill = self.store.get_skill(header.name)
                if skill is None:
                    continue
                if age >= timedelta(days=90):
                    updated = self.store.archive(skill, "Unused for at least 90 days.")
                elif header.owner == "agent" and stats.loads >= 3 and adverse * 2 >= stats.loads:
                    updated = self.store.set_status(
                        skill, "demoted",
                        f"{stats.regressed} regressed outcomes and {stats.human_rejections} "
                        f"human rejections across {stats.loads} loads.",
                    )
                else:
                    updated = self.store.set_status(skill, "stale", "Unused for at least 30 days.")
                changes[updated.name] = updated.status
        return changes
