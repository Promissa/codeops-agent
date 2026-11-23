"""LLM provider support for bounded patch generation."""

from collections.abc import Callable
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field

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


KIMI_BASE_URL = "https://api.moonshot.cn/v1"
KIMI_GLOBAL_BASE_URL = "https://api.moonshot.ai/v1"
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/"
KIMI_CODE_GLOBAL_BASE_URL = "https://api.kimi.ai/coding/"
DEFAULT_KIMI_MODEL = "kimi-k2.6"
DEFAULT_KIMI_CODE_MODEL = "kimi-for-coding"
ANTHROPIC_VERSION = "2023-06-01"
LLM_CONFIG_PATH = Path(".codeops/llm.yaml")
KIMI_PLATFORM_MODELS = (
    "kimi-k2.6",
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
)
KIMI_CODE_MODEL_ALIASES = {
    "kimi-2.8": DEFAULT_KIMI_CODE_MODEL,
    "kimi-k2.8": DEFAULT_KIMI_CODE_MODEL,
    "kimi-k2.8-preview": DEFAULT_KIMI_CODE_MODEL,
    "k2.8": DEFAULT_KIMI_CODE_MODEL,
    "k2.8-preview": DEFAULT_KIMI_CODE_MODEL,
}


class LLMProviderConfig(BaseModel):
    provider: str
    model: str
    base_url: str
    api_key_env: str
    api_protocol: str = "openai"
    api_key_env_aliases: list[str] = Field(default_factory=list)
    max_completion_tokens: int = 4096
    timeout_seconds: float = 60.0

    @property
    def api_key(self) -> str | None:
        for env_name in [self.api_key_env, *self.api_key_env_aliases]:
            value = os.environ.get(env_name)
            if value:
                return value
        return None

    @property
    def api_key_envs_display(self) -> str:
        names = [self.api_key_env, *self.api_key_env_aliases]
        return " or ".join(dict.fromkeys(names))


class LLMConfigFile(BaseModel):
    """Non-secret persisted harness LLM configuration (`.codeops/llm.yaml`)."""

    model_config = {"extra": "forbid"}

    provider: str = "kimi"
    model: str = DEFAULT_KIMI_MODEL
    base_url: str = KIMI_BASE_URL
    api_key_env: str = "MOONSHOT_API_KEY"
    api_protocol: str = "openai"
    max_completion_tokens: int = Field(default=4096, gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0)

    def to_yaml(self) -> str:
        return self.model_dump_json(indent=2) + "\n"

    @classmethod
    def parse(cls, contents: str) -> "LLMConfigFile":
        try:
            return cls.model_validate(json.loads(contents))
        except json.JSONDecodeError:
            try:
                import yaml
            except ImportError as exc:
                raise ToolError(
                    "Use JSON-compatible YAML in .codeops/llm.yaml."
                ) from exc
            try:
                return cls.model_validate(yaml.safe_load(contents))
            except Exception as exc:
                raise ToolError(f"Invalid .codeops/llm.yaml: {exc}") from exc
        except Exception as exc:
            raise ToolError(f"Invalid .codeops/llm.yaml: {exc}") from exc


def load_llm_config_file(repo_path: Path) -> LLMConfigFile | None:
    path = repo_path.resolve() / LLM_CONFIG_PATH
    if not path.is_file():
        return None
    return LLMConfigFile.parse(path.read_text(encoding="utf-8"))


def save_llm_config_file(repo_path: Path, config: LLMConfigFile) -> Path:
    path = repo_path.resolve() / LLM_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.to_yaml(), encoding="utf-8")
    return path


def build_llm_config_file(
    *,
    provider: str = "kimi",
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    max_completion_tokens: int = 4096,
) -> LLMConfigFile:
    resolved = llm_config_from_request(
        TaskRequest(
            repo_path=Path("."),
            issue_path=Path("."),
            out_path=Path("."),
            llm_provider=provider,
            llm_model=model,
            llm_base_url=base_url,
            llm_api_key_env=api_key_env,
            llm_max_completion_tokens=max_completion_tokens,
        )
    )
    if resolved is None:
        raise ToolError("Cannot persist disabled LLM provider 'none'.")
    return LLMConfigFile(
        provider=resolved.provider,
        model=resolved.model,
        base_url=resolved.base_url,
        api_key_env=resolved.api_key_env,
        api_protocol=resolved.api_protocol,
        max_completion_tokens=resolved.max_completion_tokens,
        timeout_seconds=resolved.timeout_seconds,
    )


def resolve_llm_request_overrides(
    repo_path: Path,
    *,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_base_url: str | None = None,
    llm_api_key_env: str | None = None,
    llm_max_completion_tokens: int | None = None,
) -> dict[str, Any]:
    """Merge CLI flags > repo `.codeops/llm.yaml` > `CODEOPS_LLM_PROVIDER` > disabled."""
    stored = load_llm_config_file(repo_path)
    if llm_provider is not None:
        normalized = llm_provider.strip().lower()
        if normalized in {"", "none", "off", "disabled"}:
            return {
                "llm_provider": "none",
                "llm_model": llm_model,
                "llm_base_url": llm_base_url,
                "llm_api_key_env": llm_api_key_env,
                "llm_max_completion_tokens": llm_max_completion_tokens or 4096,
            }
        provider = llm_provider
        use_stored = stored is not None and stored.provider == _canonical_provider(provider)
    elif stored is not None:
        provider = stored.provider
        use_stored = True
    else:
        provider = os.environ.get("CODEOPS_LLM_PROVIDER", "").strip() or "none"
        use_stored = False

    return {
        "llm_provider": provider,
        "llm_model": llm_model if llm_model is not None else (stored.model if use_stored and stored else None),
        "llm_base_url": llm_base_url if llm_base_url is not None else (stored.base_url if use_stored and stored else None),
        "llm_api_key_env": llm_api_key_env if llm_api_key_env is not None else (stored.api_key_env if use_stored and stored else None),
        "llm_max_completion_tokens": (
            llm_max_completion_tokens
            if llm_max_completion_tokens is not None
            else (stored.max_completion_tokens if use_stored and stored else 4096)
        ),
    }


def _normalize_kimi_code_model(model: str) -> str:
    return KIMI_CODE_MODEL_ALIASES.get(model.strip().lower(), model)


def _canonical_provider(name: str) -> str:
    lowered = name.strip().lower()
    if lowered in {
        "kimi",
        "moonshot",
        "moonshotai",
        "moonshotai-cn",
        "kimi-platform",
        "kimi-platform-cn",
        "kimi-platform-global",
        "kimi-global",
    }:
        return "kimi"
    if lowered in {
        "kimi-code",
        "kimi_code",
        "kimi-code-ai",
        "kimi_code_ai",
        "kimi-code-plan-cn",
        "kimi-code-plan-global",
        "kimi-code-global",
    }:
        return "kimi-code"
    if lowered in {"openai-compatible", "openai_compatible"}:
        return "openai-compatible"
    return lowered


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

    if provider in {
        "kimi",
        "moonshot",
        "moonshotai-cn",
        "kimi-platform",
        "kimi-platform-cn",
    }:
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

    if provider in {"moonshotai", "kimi-global", "kimi-platform-global"}:
        return LLMProviderConfig(
            provider="kimi",
            model=_first_value(request.llm_model, "CODEOPS_LLM_MODEL", "KIMI_MODEL")
            or DEFAULT_KIMI_MODEL,
            base_url=_first_value(
                request.llm_base_url,
                "CODEOPS_LLM_BASE_URL",
                "KIMI_BASE_URL",
            )
            or KIMI_GLOBAL_BASE_URL,
            api_key_env=request.llm_api_key_env or "MOONSHOT_API_KEY",
            max_completion_tokens=request.llm_max_completion_tokens,
        )

    if provider in {
        "kimi-code",
        "kimi_code",
        "kimi-code-ai",
        "kimi_code_ai",
        "kimi-code-plan-cn",
    }:
        raw_model = (
            _first_value(
                request.llm_model,
                "CODEOPS_LLM_MODEL",
                "KIMI_CODE_MODEL",
                "KIMI_MODEL",
            )
            or DEFAULT_KIMI_CODE_MODEL
        )
        return LLMProviderConfig(
            provider="kimi-code",
            model=_normalize_kimi_code_model(raw_model),
            base_url=_first_value(
                request.llm_base_url,
                "CODEOPS_LLM_BASE_URL",
                "KIMI_CODE_BASE_URL",
            )
            or KIMI_CODE_BASE_URL,
            api_key_env=request.llm_api_key_env or "KIMI_API_KEY",
            api_protocol=_first_value(
                None,
                "CODEOPS_LLM_API_PROTOCOL",
                "KIMI_CODE_API_PROTOCOL",
            )
            or "anthropic",
            max_completion_tokens=request.llm_max_completion_tokens,
        )

    if provider in {"kimi-code-plan-global", "kimi-code-global"}:
        raw_model = (
            _first_value(
                request.llm_model,
                "CODEOPS_LLM_MODEL",
                "KIMI_CODE_MODEL",
                "KIMI_MODEL",
            )
            or DEFAULT_KIMI_CODE_MODEL
        )
        return LLMProviderConfig(
            provider="kimi-code",
            model=_normalize_kimi_code_model(raw_model),
            base_url=_first_value(
                request.llm_base_url,
                "CODEOPS_LLM_BASE_URL",
                "KIMI_CODE_BASE_URL",
            )
            or KIMI_CODE_GLOBAL_BASE_URL,
            api_key_env=request.llm_api_key_env or "KIMI_API_KEY",
            api_protocol=_first_value(
                None,
                "CODEOPS_LLM_API_PROTOCOL",
                "KIMI_CODE_API_PROTOCOL",
            )
            or "anthropic",
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
                "LLM API key env var is not set: "
                f"{self.config.api_key_envs_display}"
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

        url, headers, payload = _chat_request(
            config=self.config,
            api_key=api_key,
            messages=[
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
            max_completion_tokens=self.config.max_completion_tokens,
        )
        response = self._transport(
            url,
            headers,
            payload,
            self.config.timeout_seconds,
        )
        content = _response_content(response)
        patch_diff = extract_unified_diff(content)
        if not patch_diff:
            raise ToolError("LLM response did not contain a unified diff")
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        return LLMPatchResult(
            patch_diff=patch_diff,
            input_tokens=_input_tokens(usage),
            output_tokens=_output_tokens(usage),
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
                "LLM API key env var is not set: "
                f"{self.config.api_key_envs_display}"
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

        url, headers, payload = _chat_request(
            config=self.config,
            api_key=api_key,
            messages=[
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
            max_completion_tokens=min(
                self.config.max_completion_tokens,
                2048,
            ),
            response_format={"type": "json_object"},
        )
        response = self._transport(
            url,
            headers,
            payload,
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
            llm_input_tokens=_input_tokens(usage),
            llm_output_tokens=_output_tokens(usage),
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

    # Run-local evolution guidance is outside the approved outbound file scope.
    outbound_fields = {"evolution_guidance"}
    return "\n\n".join(
        [
            "Generate a minimal patch for this AcceptanceContract and ImpactEnvelope.",
            "Return only unified diff text.",
            "Do not add dependencies, do not change build files, and do not change public APIs unless explicitly allowed.",
            f"AcceptanceContract JSON:\n{acceptance_contract.model_dump_json(indent=2, exclude=outbound_fields)}",
            f"PatchPlan JSON:\n{patch_plan.model_dump_json(indent=2, exclude=outbound_fields)}",
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


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }


def _chat_request(
    *,
    config: LLMProviderConfig,
    api_key: str,
    messages: list[dict[str, str]],
    max_completion_tokens: int,
    response_format: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    if config.api_protocol == "anthropic":
        return (
            _anthropic_messages_url(config.base_url),
            _anthropic_headers(api_key),
            _anthropic_payload(
                config=config,
                messages=messages,
                max_completion_tokens=max_completion_tokens,
            ),
        )
    return (
        _chat_completions_url(config.base_url),
        _headers(api_key),
        _chat_payload(
            config=config,
            messages=messages,
            max_completion_tokens=max_completion_tokens,
            response_format=response_format,
        ),
    )


def _chat_payload(
    *,
    config: LLMProviderConfig,
    messages: list[dict[str, str]],
    max_completion_tokens: int,
    response_format: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": 0,
    }
    if config.provider in {"kimi", "kimi-code"}:
        payload["max_tokens"] = max_completion_tokens
    else:
        payload["max_completion_tokens"] = max_completion_tokens
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def _anthropic_payload(
    *,
    config: LLMProviderConfig,
    messages: list[dict[str, str]],
    max_completion_tokens: int,
) -> dict[str, Any]:
    system_parts = []
    conversation = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            system_parts.append(content)
        elif role in {"user", "assistant"}:
            conversation.append({"role": role, "content": content})
    if not conversation:
        conversation.append({"role": "user", "content": ""})
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": conversation,
        "max_tokens": max_completion_tokens,
        "temperature": 0,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    return payload


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
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        raise ToolError(f"LLM API request failed: {detail}") from exc
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


def _anthropic_messages_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/messages"
    return normalized + "/v1/messages"


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best effort diagnostics.
        body = ""
    if not body:
        return f"HTTP {exc.code} {exc.reason}"
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return f"HTTP {exc.code} {exc.reason}: {body[:500]}"
    return f"HTTP {exc.code} {exc.reason}: {json.dumps(parsed, ensure_ascii=False)}"


def _response_content(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type", "text") == "text"
        ]
        if text_parts:
            return "\n".join(text_parts)

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


def _input_tokens(usage: dict[str, Any]) -> int:
    return _int(usage.get("prompt_tokens") or usage.get("input_tokens"))


def _output_tokens(usage: dict[str, Any]) -> int:
    return _int(usage.get("completion_tokens") or usage.get("output_tokens"))


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
