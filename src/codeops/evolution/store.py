"""Repository-local storage with atomic records and serialized host mutations."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from codeops.core.models import TrustVerdict
from codeops.evolution.guards import EvolutionSafetyGuard
from codeops.evolution.models import (
    MEMORY_LIMITS, SKILL_BODY_BYTES, FeedbackCase, MemoryName, MemorySnapshot, Proposal,
    Skill, SkillDraft, SkillHeader, check_skill_name, utc_now,
)
from codeops.runtime.workspace import open_regular
from codeops.trust.policy import POLICY_PATH, TrustPolicy

EVOLUTION_PATH = Path(".codeops/evolution")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_HEADER_BYTES = 256_000


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class EvolutionStore:
    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self.root = self.repo / EVOLUTION_PATH
        self.guard = EvolutionSafetyGuard()
        self._transaction_root: ContextVar[int | None] = ContextVar("evolution_transaction_root", default=None)

    @property
    def identity(self) -> tuple[int, int] | None:
        try:
            with self._directory(EVOLUTION_PATH) as directory:
                info = os.fstat(directory)
                return info.st_dev, info.st_ino
        except FileNotFoundError:
            return None

    @contextmanager
    def transaction(
        self, *, create: bool = False, expected: tuple[int, int] | None = None,
    ) -> Iterator[None]:
        with self._directory(EVOLUTION_PATH, create=create) as directory:
            fcntl.flock(directory, fcntl.LOCK_EX)
            info = os.fstat(directory)
            identity = (info.st_dev, info.st_ino)
            if self.identity != identity or (expected is not None and expected != identity):
                raise ValueError("Evolution store changed during the operation.")
            token = self._transaction_root.set(directory)
            try:
                yield
            finally:
                self._transaction_root.reset(token)

    @contextmanager
    def _directory(self, path: Path, *, create: bool = False) -> Iterator[int]:
        transaction_root = self._transaction_root.get()
        if transaction_root is not None and path.is_relative_to(EVOLUTION_PATH):
            directory = os.dup(transaction_root)
            path = path.relative_to(EVOLUTION_PATH)
        else:
            directory = os.open(self.repo, _DIRECTORY_FLAGS)
        try:
            for name in path.parts:
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
                os.close(directory)
                directory = child
            yield directory
        finally:
            os.close(directory)

    def _write(self, path: Path, text: str) -> None:
        with self._directory(path.parent, create=True) as directory:
            temporary = f".evolution-{uuid4().hex}"
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                    output.write(text)
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def _entries(self, path: Path) -> list[str]:
        try:
            with self._directory(path) as directory:
                return sorted(os.listdir(directory))
        except FileNotFoundError:
            return []

    @contextmanager
    def _open(self, path: Path) -> Iterator[BinaryIO]:
        with self._directory(path.parent) as directory:
            with open_regular(directory, Path(path.name)) as source:
                yield source

    def _read(self, path: Path, *, limit: int | None = None) -> str | None:
        try:
            with self._open(path) as source:
                data = source.read() if limit is None else source.read(limit + 1)
                if limit is not None and len(data) > limit:
                    raise ValueError("Evolution record exceeds its storage budget.")
                return data.decode("utf-8")
        except FileNotFoundError:
            return None

    def _skill_path(self, name: str, area: str = "skills") -> Path:
        return EVOLUTION_PATH / area / check_skill_name(name) / "SKILL.md"

    def _read_skill(self, path: Path, *, header_only: bool = False) -> SkillHeader | Skill | None:
        try:
            with self._open(path) as source:
                if source.readline(5).rstrip(b"\r\n") != b"---":
                    raise ValueError("Skill requires YAML frontmatter.")
                metadata = bytearray()
                while line := source.readline(_HEADER_BYTES + 1):
                    if line.rstrip(b"\r\n") == b"---":
                        break
                    metadata.extend(line)
                    if len(metadata) > _HEADER_BYTES:
                        raise ValueError("Skill frontmatter exceeds its storage budget.")
                else:
                    raise ValueError("Unterminated skill frontmatter.")
                header = SkillHeader.model_validate(_frontmatter(metadata.decode("utf-8")))
                self.guard.require_safe(header)
                if header_only:
                    return header
                body = source.read(SKILL_BODY_BYTES + 1)
                if len(body) > SKILL_BODY_BYTES:
                    raise ValueError("Skill body must fit within 15,000 UTF-8 bytes.")
                skill = Skill.model_validate({**header.model_dump(), "body": body.decode("utf-8")})
                self.guard.require_safe(skill.body)
                return skill.model_copy(update={"revision_hash": _revision(skill)})
        except FileNotFoundError:
            return None

    def get_skill(self, name: str, *, archived: bool = False) -> Skill | None:
        skill = self._read_skill(self._skill_path(name, "archive" if archived else "skills"))
        assert skill is None or isinstance(skill, Skill)
        if skill and skill.name != name:
            raise ValueError("Skill name does not match its directory.")
        return skill

    def index(self, *, archived: bool = False, strict: bool = True) -> tuple[SkillHeader, ...]:
        area = "archive" if archived else "skills"
        result = []
        for name in self._entries(EVOLUTION_PATH / area):
            try:
                header = self._read_skill(self._skill_path(name, area), header_only=True)
                if header and header.name == name:
                    result.append(header)
            except (ValueError, OSError):
                if strict:
                    raise
        return tuple(result)

    def _save_skill(self, skill: Skill, *, path: Path | None = None) -> Skill:
        skill = Skill.model_validate(skill.model_dump())
        self.guard.require_safe(skill)
        skill = skill.model_copy(update={"revision_hash": _revision(skill)})
        header = skill.model_dump(mode="json", exclude={"body"})
        metadata = "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in header.items())
        if len(metadata.encode("utf-8")) > _HEADER_BYTES:
            raise ValueError("Skill frontmatter exceeds its storage budget.")
        self._write(path or self._skill_path(skill.name), f"---\n{metadata}---\n{skill.body}")
        return skill

    def install(self, draft: SkillDraft, source_run: str, existing: Skill | None, *, now: datetime) -> Skill:
        # Called by approval while holding the transaction and after evaluating the fresh revision.
        skill = Skill(
            **draft.model_dump(), owner="agent", created_at=now,
            status="pinned" if existing and existing.status == "pinned" else "active",
            source_runs=tuple(dict.fromkeys([*(existing.source_runs if existing else ()), source_run])),
        )
        if existing:
            self._snapshot(existing)
        return self._save_skill(skill)

    def _snapshot(self, skill: Skill) -> None:
        revisions = EVOLUTION_PATH / "history" / skill.name
        latest = [name for name in self._entries(revisions) if name.endswith(".md") and name[:-3].isdigit()]
        sequence = int(latest[-1][:-3]) + 1 if latest else 1
        self._save_skill(skill, path=revisions / f"{sequence:012d}.md")

    def _history(self, name: str) -> Iterator[tuple[Path, Skill]]:
        directory = EVOLUTION_PATH / "history" / check_skill_name(name)
        for entry in reversed(self._entries(directory)):
            if not (entry.endswith(".md") and entry[:-3].isdigit()):
                continue
            path = directory / entry
            skill = self._read_skill(path)
            if isinstance(skill, Skill) and skill.name == name:
                yield path, skill

    def transition(
        self, name: str, action: Literal["pin", "unpin", "archive", "restore", "revert"],
    ) -> Skill:
        with self.transaction():
            current = self.get_skill(name, archived=action == "restore")
            if current is None:
                raise ValueError("Skill not found in the requested location.")
            if action == "archive":
                return self.archive(current, "Archived by reviewer.")
            if action == "restore":
                restored = current.model_copy(update={
                    "status": "active", "status_reason": "Restored by reviewer.", "restored_at": utc_now(),
                })
                return self._move_skill(restored, "archive", "skills")
            if action == "revert":
                for _, previous in self._history(name):
                    if previous.revision_hash != current.revision_hash:
                        self._snapshot(current)
                        return self.set_status(
                            previous.model_copy(update={"restored_at": utc_now()}),
                            "pinned" if current.status == "pinned" else "active",
                            "Restored prior revision by reviewer.",
                        )
                raise ValueError("No prior skill revision.")
            if action == "unpin" and current.status != "pinned":
                raise ValueError("Only a pinned skill can be unpinned.")
            return self.set_status(current, "pinned" if action == "pin" else "active", "Updated by reviewer.")

    def set_status(self, skill: Skill, status: Literal["active", "pinned", "stale", "demoted"], reason: str) -> Skill:
        return self._save_skill(skill.model_copy(update={"status": status, "status_reason": reason}))

    def archive(self, skill: Skill, reason: str) -> Skill:
        archived = skill.model_copy(update={"status": "archived", "status_reason": reason})
        return self._move_skill(archived, "skills", "archive")

    def _move_skill(self, skill: Skill, source: str, destination: str) -> Skill:
        original, target = self._skill_path(skill.name, source), self._skill_path(skill.name, destination)
        if self._read(target) is not None:
            raise ValueError("Skill already exists at the destination.")
        self._save_skill(skill, path=original)
        with self._directory(original.parent.parent) as origin:
            with self._directory(target.parent.parent, create=True) as dest:
                os.rename(skill.name, skill.name, src_dir_fd=origin, dst_dir_fd=dest)
        return skill

    def attribute(
        self, name: str, revision: str, key: str, *, verdict: TrustVerdict | None, now: datetime,
    ) -> Skill | None:
        locations = (
            (self._skill_path(name, area), self.get_skill(name, archived=area == "archive"))
            for area in ("skills", "archive")
        )
        for path, skill in locations:
            if skill and skill.revision_hash == revision:
                return self._attribute(path, skill, key, verdict, now)
        for path, skill in self._history(name):
            if skill.revision_hash == revision:
                return self._attribute(path, skill, key, verdict, now)
        return None

    def _attribute(
        self, path: Path, skill: Skill, key: str, verdict: TrustVerdict | None, now: datetime,
    ) -> Skill:
        stats = skill.evidence_stats
        if verdict is None:
            if key in skill.rejection_runs:
                return skill
            updates = {
                "rejection_runs": (*skill.rejection_runs[-255:], key),
                "evidence_stats": stats.model_copy(update={"human_rejections": stats.human_rejections + 1}),
            }
        else:
            if key in skill.outcome_runs:
                return skill
            updates = {
                "outcome_runs": (*skill.outcome_runs[-255:], key),
                "last_used_at": max(now, skill.last_used_at or now),
                "evidence_stats": stats.model_copy(update={"loads": stats.loads + 1, verdict: getattr(stats, verdict) + 1}),
            }
        return self._save_skill(skill.model_copy(update=updates), path=path)

    def memory_text(self, name: MemoryName) -> str:
        limit = MEMORY_LIMITS[name]
        try:
            text = self._read(EVOLUTION_PATH / "memory" / name, limit=4 * limit) or ""
        except ValueError:
            raise ValueError(f"{name} exceeds {limit} characters or is invalid. Curate or replace its contents.") from None
        self.check_memory(name, text)
        return text

    def memory_revision(self, name: MemoryName) -> str:
        revision = sha256()
        try:
            with self._open(EVOLUTION_PATH / "memory" / name) as source:
                while chunk := source.read(64_000):
                    revision.update(chunk)
        except FileNotFoundError:
            pass
        return revision.hexdigest()

    def check_memory(self, name: MemoryName, text: str) -> None:
        if len(text) > MEMORY_LIMITS[name]:
            raise ValueError(f"{name} exceeds {MEMORY_LIMITS[name]} characters. Curate or replace its contents.")
        self.guard.require_safe(text)

    def memory_snapshot(self) -> MemorySnapshot:
        return MemorySnapshot(memory=self.memory_text("MEMORY.md"), user=self.memory_text("USER.md"))

    def _save_memory(self, name: MemoryName, text: str) -> None:
        self.check_memory(name, text)
        self._write(EVOLUTION_PATH / "memory" / name, text)

    def policy_snapshot(self) -> tuple[TrustPolicy, str]:
        raw = self._read(POLICY_PATH, limit=256_000)
        policy = TrustPolicy.parse(raw) if raw is not None else TrustPolicy()
        return policy, digest(raw if raw is not None else "")

    def _save_policy(self, policy: TrustPolicy) -> None:
        self.guard.require_safe(policy)
        self._write(POLICY_PATH, policy.to_yaml())

    def _proposal_path(self, proposal_id: str) -> Path:
        if not proposal_id or len(proposal_id) > 80 or not proposal_id[0].isalnum() or not all(
            char.isascii() and (char.isalnum() or char in "_-") for char in proposal_id
        ):
            raise ValueError("Invalid proposal ID.")
        return EVOLUTION_PATH / "pending" / f"{proposal_id}.yaml"

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        text = self._read(self._proposal_path(proposal_id), limit=256_000)
        if text is None:
            return None
        proposal = Proposal.model_validate_json(text)
        if proposal.proposal_id != proposal_id:
            raise ValueError("Proposal ID does not match its filename.")
        return proposal

    def proposals(self) -> Iterator[Proposal]:
        for filename in self._entries(EVOLUTION_PATH / "pending"):
            if filename.endswith(".yaml") and (proposal := self.get_proposal(filename[:-5])):
                yield proposal

    def _save_proposal(self, proposal: Proposal) -> None:
        self.guard.require_safe(proposal)
        self._write(self._proposal_path(proposal.proposal_id), proposal.model_dump_json(indent=2) + "\n")

    _CORPUS = EVOLUTION_PATH / "corpus" / "feedback_cases.jsonl"

    def cases(self) -> tuple[FeedbackCase, ...]:
        raw = self._read(self._CORPUS) or ""
        self.guard.require_safe(raw)
        return tuple(FeedbackCase.model_validate_json(line) for line in raw.splitlines() if line.strip())

    def _append_case(self, case: FeedbackCase, cases: tuple[FeedbackCase, ...]) -> None:
        self.guard.require_safe(case)
        self._write(self._CORPUS, "".join(item.model_dump_json() + "\n" for item in (*cases, case)))


def _revision(skill: Skill) -> str:
    # Outcome and curation writes do not invalidate the content revision.
    return digest(skill.model_dump_json(include={"name", "description", "scope", "owner", "body"}))


def _frontmatter(text: str) -> dict:
    # Accept the generated JSON-valued mapping and the block forms used to
    # hand-author the fixed skill schema. There are no aliases or YAML tags.
    if text.lstrip().startswith("{"):
        return json.loads(text)
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    result: dict = {}
    offset = 0
    while offset < len(lines):
        line = lines[offset]
        key, separator, raw = line.partition(":")
        if not separator or not key or key != key.strip() or key in result:
            raise ValueError("Invalid skill frontmatter mapping.")
        stop = offset + 1
        while stop < len(lines) and lines[stop].startswith(" "):
            stop += 1
        nested = lines[offset + 1:stop]
        if raw.strip():
            if nested:
                raise ValueError("Unexpected block after a skill frontmatter value.")
            result[key] = _yaml_value(raw)
        elif key == "source_runs":
            result[key] = _yaml_list(nested)
        elif key in {"scope", "evidence_stats"}:
            group: dict = {}
            field = None
            for item in nested:
                member = item.lstrip()
                if member.startswith("- "):
                    if field is None or not isinstance(group[field], list):
                        raise ValueError("Unexpected item in skill frontmatter.")
                    group[field].append(_yaml_value(member[2:]))
                    continue
                field_name, delimiter, raw_value = member.partition(":")
                if not delimiter or not field_name or field_name in group:
                    raise ValueError("Invalid nested skill frontmatter mapping.")
                field = field_name
                group[field] = _yaml_value(raw_value) if raw_value.strip() else []
            result[key] = group
        else:
            if nested:
                raise ValueError("Unsupported block in skill frontmatter.")
            result[key] = None
        offset = stop
    return result


def _yaml_list(lines: list[str]) -> list:
    if any(not line.lstrip().startswith("- ") for line in lines):
        raise ValueError("Expected a list in skill frontmatter.")
    return [_yaml_value(line.lstrip()[2:]) for line in lines]


def _yaml_value(raw: str):
    value = raw.strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        if value.startswith("[") and value.endswith("]"):
            return [_yaml_value(item) for item in value[1:-1].split(",")]
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'")
        if value == "~":
            return None
        if not value or value[0] in "{}[]&*!>|\"":
            raise ValueError("Unsupported value in skill frontmatter.")
        return value
