"""LLM provider support for bounded patch generation."""

from collections.abc import Callable
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel

from codeops.core.errors import ToolError
from codeops.core.models import (
    AcceptanceContract,
    ImpactEnvelope,
    PatchPlan,
    ProjectProfile,
    RepoSketch,
    TaskRequest,
)
from codeops.agents.issue_router import IssueRoutingResult
from codeops.safety.secret_filter import SecretFilter


KIMI_BASE_URL = "https://api.moonshot.ai/v1"
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_KIMI_MODEL = "kimi-k2.6"


class LLMProviderConfig(BaseModel):
    provider: str
    model: str
    base_url: str
    api_key_env: str
    max_completion_tokens: int = 4096
    timeout_seconds: float = 60.0

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


class LLMPatchResult(BaseModel):
    patch_diff: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str
    provider: str


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


def llm_config_from_request(request: TaskRequest) -> LLMProviderConfig | None:
    provider = request.llm_provider.strip().lower()
    if provider in {"", "none", "off", "disabled"}:
        return None

    if provider in {"kimi", "moonshot"}:
        return LLMProviderConfig(
            provider="kimi",
            model=_first_value(request.llm_model, "CODEOPS_LLM_MODEL", "KIMI_MODEL")
            or DEFAULT_KIMI_MODEL,
            base_url=_first_value(
                request.llm_base_url,
                "CODEOPS_LLM_BASE_URL",
                "KIMI_BASE_URL",
            )
            or KIMI_BASE_URL,
            api_key_env=request.llm_api_key_env or "MOONSHOT_API_KEY",
            max_completion_tokens=request.llm_max_completion_tokens,
        )

    if provider in {"kimi-code", "kimi_code", "kimi-code-ai", "kimi_code_ai"}:
        return LLMProviderConfig(
            provider="kimi-code",
            model=_first_value(request.llm_model, "CODEOPS_LLM_MODEL", "KIMI_MODEL")
            or DEFAULT_KIMI_MODEL,
            base_url=_first_value(
                request.llm_base_url,
                "CODEOPS_LLM_BASE_URL",
                "KIMI_CODE_BASE_URL",
            )
            or KIMI_CODE_BASE_URL,
            api_key_env=request.llm_api_key_env or "KIMI_API_KEY",
            max_completion_tokens=request.llm_max_completion_tokens,
        )

    if provider in {"openai-compatible", "openai_compatible"}:
        base_url = _first_value(request.llm_base_url, "CODEOPS_LLM_BASE_URL")
        model = _first_value(request.llm_model, "CODEOPS_LLM_MODEL")
        if not base_url or not model:
            raise ToolError(
                "openai-compatible LLM requires llm_base_url and llm_model "
                "or CODEOPS_LLM_BASE_URL and CODEOPS_LLM_MODEL"
            )
        return LLMProviderConfig(
            provider="openai-compatible",
            model=model,
            base_url=base_url,
            api_key_env=request.llm_api_key_env or "CODEOPS_LLM_API_KEY",
            max_completion_tokens=request.llm_max_completion_tokens,
        )

    raise ToolError(f"unsupported LLM provider: {request.llm_provider}")


class OpenAICompatiblePatchProvider:
    """Generate patches through an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        config: LLMProviderConfig,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _post_json

    def generate_patch(
        self,
        repo_path: Path,
        patch_plan: PatchPlan,
        acceptance_contract: AcceptanceContract,
        impact_envelope: ImpactEnvelope,
    ) -> LLMPatchResult:
        api_key = self.config.api_key
        if not api_key:
            raise ToolError(
                f"LLM API key env var is not set: {self.config.api_key_env}"
            )

        user_prompt = _prompt(
            repo_path=repo_path,
            patch_plan=patch_plan,
            acceptance_contract=acceptance_contract,
            impact_envelope=impact_envelope,
        )
        scan = SecretFilter().scan(user_prompt)
        if not scan.passed:
            raise ToolError("secret detected in LLM prompt context")

        response = self._transport(
            _chat_completions_url(self.config.base_url),
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You generate minimal unified diffs for a code "
                            "maintenance agent. Return only a unified diff. "
                            "Do not explain. Do not edit files outside the "
                            "impact envelope."
                        ),
                    },
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "max_completion_tokens": self.config.max_completion_tokens,
            },
            self.config.timeout_seconds,
        )
        content = _response_content(response)
        patch_diff = extract_unified_diff(content)
        if not patch_diff:
            raise ToolError("LLM response did not contain a unified diff")
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        return LLMPatchResult(
            patch_diff=patch_diff,
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            model=str(response.get("model") or self.config.model),
            provider=self.config.provider,
        )


class OpenAICompatibleRoutingProvider:
    """Infer likely components through an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        config: LLMProviderConfig,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _post_json

    def enrich_routing(
        self,
        *,
        repo_path: Path,
        issue_text: str,
        project_profile: ProjectProfile,
        repo_sketch: RepoSketch,
        deterministic_result: IssueRoutingResult,
    ) -> IssueRoutingResult:
        api_key = self.config.api_key
        if not api_key:
            raise ToolError(
                f"LLM API key env var is not set: {self.config.api_key_env}"
            )
        user_prompt = _routing_prompt(
            issue_text=issue_text,
            project_profile=project_profile,
            repo_sketch=repo_sketch,
            deterministic_result=deterministic_result,
        )
        scan = SecretFilter().scan(user_prompt)
        if not scan.passed:
            raise ToolError("secret detected in LLM routing context")

        response = self._transport(
            _chat_completions_url(self.config.base_url),
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You route software issues to likely components. "
                            "Return strict JSON only. Prefer files from the "
                            "candidate list. Do not invent large refactors."
                        ),
                    },
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "max_completion_tokens": min(
                    self.config.max_completion_tokens,
                    2048,
                ),
            },
            self.config.timeout_seconds,
        )
        parsed = _json_from_content(_response_content(response))
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        return IssueRoutingResult(
            source="llm",
            components=_string_list(parsed.get("components")),
            files=_string_list(parsed.get("files")),
            tests=_string_list(parsed.get("tests")),
            query_terms=_string_list(parsed.get("query_terms")),
            candidates=deterministic_result.candidates,
            confidence=_float(parsed.get("confidence")),
            rationale=str(parsed.get("rationale", "")),
            llm_input_tokens=_int(usage.get("prompt_tokens")),
            llm_output_tokens=_int(usage.get("completion_tokens")),
            llm_calls=1,
        )


def extract_unified_diff(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    marker = "diff --git "
    if marker in stripped:
        return stripped[stripped.index(marker) :].rstrip() + "\n"
    if stripped.startswith("--- ") and "\n+++ " in stripped:
        return stripped.rstrip() + "\n"
    return ""


def _prompt(
    *,
    repo_path: Path,
    patch_plan: PatchPlan,
    acceptance_contract: AcceptanceContract,
    impact_envelope: ImpactEnvelope,
) -> str:
    files = _context_files(patch_plan, impact_envelope)
    context_parts = []
    for rel_path in files:
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        context_parts.append(
            "\n".join(
                [
                    f"### file: {rel_path}",
                    path.read_text(encoding="utf-8", errors="replace")[:20000],
                ]
            )
        )

    return "\n\n".join(
        [
            "Generate a minimal patch for this AcceptanceContract and ImpactEnvelope.",
            "Return only unified diff text.",
            "Do not add dependencies, do not change build files, and do not change public APIs unless explicitly allowed.",
            f"AcceptanceContract JSON:\n{acceptance_contract.model_dump_json(indent=2)}",
            f"PatchPlan JSON:\n{patch_plan.model_dump_json(indent=2)}",
            f"ImpactEnvelope JSON:\n{impact_envelope.model_dump_json(indent=2)}",
            "Allowed file contents:",
            "\n\n".join(context_parts) or "No readable allowed files were found.",
        ]
    )


def _context_files(
    patch_plan: PatchPlan,
    impact_envelope: ImpactEnvelope,
) -> list[str]:
    planned = [*patch_plan.files_to_edit, *patch_plan.tests_to_add_or_update]
    allowed = set(impact_envelope.allowed_files)
    files = [path for path in planned if path in allowed]
    if files:
        return list(dict.fromkeys(files))
    return list(dict.fromkeys(impact_envelope.allowed_files))


def _routing_prompt(
    *,
    issue_text: str,
    project_profile: ProjectProfile,
    repo_sketch: RepoSketch,
    deterministic_result: IssueRoutingResult,
) -> str:
    candidates = [
        {
            "path": candidate.path,
            "score": candidate.score,
            "reasons": candidate.reasons[:4],
        }
        for candidate in deterministic_result.candidates[:80]
    ]
    modules = [
        {
            "name": module.name,
            "path": module.path,
            "symbols": module.public_symbols[:10],
            "risk": module.risk,
        }
        for module in repo_sketch.core_modules[:80]
    ]
    return "\n\n".join(
        [
            "Infer likely suspected components/files for this issue.",
            "Return JSON with keys: components, files, tests, query_terms, confidence, rationale.",
            "Files must be repository-relative paths. Prefer candidate paths when possible.",
            f"ProjectProfile JSON:\n{project_profile.model_dump_json(indent=2)}",
            f"RepoSketch modules JSON:\n{json.dumps(modules, indent=2)}",
            f"Deterministic candidates JSON:\n{json.dumps(candidates, indent=2)}",
            f"Issue text:\n{issue_text[:20000]}",
        ]
    )


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise ToolError(f"LLM API request failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ToolError("LLM API returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ToolError("LLM API returned unexpected JSON")
    return parsed


def _chat_completions_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _response_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ToolError("LLM API response did not include choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ToolError("LLM API response choice was invalid")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ToolError("LLM API response did not include a message")
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        )
    return ""


def _json_from_content(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ToolError("LLM routing response did not contain valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ToolError("LLM routing response JSON must be an object")
    return parsed


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _float(value: Any) -> float:
    if isinstance(value, int | float):
        return max(0.0, min(1.0, float(value)))
    return 0.0


def _first_value(explicit: str | None, *env_names: str) -> str | None:
    if explicit:
        return explicit
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            return value
    return None


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0
