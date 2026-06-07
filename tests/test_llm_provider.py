from pathlib import Path
import urllib.error
import urllib.request

import pytest

from codeops.agents.llm_provider import (
    DEFAULT_KIMI_CODE_MODEL,
    DEFAULT_KIMI_MODEL,
    KIMI_BASE_URL,
    KIMI_CODE_BASE_URL,
    LLMProviderConfig,
    OpenAICompatibleRoutingProvider,
    OpenAICompatiblePatchProvider,
    _post_json,
    extract_unified_diff,
    llm_config_from_request,
)
from codeops.agents.issue_router import IssueRoutingResult, RoutingCandidate
from codeops.core.errors import ToolError
from codeops.core.models import (
    AcceptanceContract,
    ImpactEnvelope,
    ModuleCapsule,
    PatchPlan,
    ProjectProfile,
    RepoSketch,
    SymbolRef,
    TaskRequest,
)


def test_kimi_config_defaults_to_moonshot_endpoint(monkeypatch):
    monkeypatch.delenv("CODEOPS_LLM_MODEL", raising=False)
    request = _request(llm_provider="kimi")

    config = llm_config_from_request(request)

    assert config is not None
    assert config.provider == "kimi"
    assert config.model == DEFAULT_KIMI_MODEL
    assert config.base_url == KIMI_BASE_URL
    assert config.api_key_env == "MOONSHOT_API_KEY"
    assert config.api_key_env_aliases == []


def test_kimi_code_config_defaults_to_coding_endpoint():
    request = _request(llm_provider="kimi-code")

    config = llm_config_from_request(request)

    assert config is not None
    assert config.provider == "kimi-code"
    assert config.model == DEFAULT_KIMI_CODE_MODEL
    assert config.base_url == KIMI_CODE_BASE_URL
    assert config.api_key_env == "KIMI_API_KEY"
    assert config.api_protocol == "anthropic"
    assert config.api_key_env_aliases == []


def test_kimi_code_config_reads_kimi_api_key(monkeypatch):
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-key-for-kimi")

    config = llm_config_from_request(_request(llm_provider="kimi-code"))

    assert config is not None
    assert config.api_key == "sk-test-key-for-kimi"


def test_openai_compatible_requires_explicit_base_url_and_model():
    with pytest.raises(ToolError):
        llm_config_from_request(_request(llm_provider="openai-compatible"))


def test_openai_compatible_patch_provider_posts_chat_completion(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test-key-for-kimi")
    calls = []

    def transport(url, headers, payload, timeout_seconds):
        calls.append((url, headers, payload, timeout_seconds))
        return {
            "model": "kimi-k2.6",
            "choices": [
                {
                    "message": {
                        "content": (
                            "```diff\n"
                            "diff --git a/app.py b/app.py\n"
                            "--- a/app.py\n"
                            "+++ b/app.py\n"
                            "@@ -1 +1 @@\n"
                            "-old = True\n"
                            "+new = True\n"
                            "```"
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        }

    provider = OpenAICompatiblePatchProvider(
        LLMProviderConfig(
            provider="kimi",
            model="kimi-k2.6",
            base_url=KIMI_BASE_URL,
            api_key_env="MOONSHOT_API_KEY",
        ),
        transport=transport,
    )

    result = provider.generate_patch(repo, _plan(), _contract(), _envelope())

    assert result.patch_diff.startswith("diff --git")
    assert result.input_tokens == 123
    assert result.output_tokens == 45
    assert calls[0][0] == f"{KIMI_BASE_URL}/chat/completions"
    assert calls[0][1]["Authorization"] == "Bearer sk-test-key-for-kimi"
    assert calls[0][2]["model"] == "kimi-k2.6"
    assert calls[0][2]["max_tokens"] == 4096
    assert "max_completion_tokens" not in calls[0][2]
    assert "prompt_cache_key" not in calls[0][2]
    assert "ImpactEnvelope JSON" in calls[0][2]["messages"][1]["content"]


def test_provider_blocks_secret_context(tmp_path, monkeypatch):
    repo = _repo(tmp_path, source="API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz'\n")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test-key-for-kimi")
    provider = OpenAICompatiblePatchProvider(
        LLMProviderConfig(
            provider="kimi",
            model="kimi-k2.6",
            base_url=KIMI_BASE_URL,
            api_key_env="MOONSHOT_API_KEY",
        ),
        transport=lambda *_: {},
    )

    with pytest.raises(ToolError, match="secret detected"):
        provider.generate_patch(repo, _plan(), _contract(), _envelope())


def test_kimi_code_patch_provider_posts_anthropic_messages(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-key-for-kimi")
    calls = []

    def transport(url, headers, payload, timeout_seconds):
        calls.append((url, headers, payload, timeout_seconds))
        return {
            "model": DEFAULT_KIMI_CODE_MODEL,
            "content": [
                {
                    "type": "text",
                    "text": (
                        "diff --git a/app.py b/app.py\n"
                        "--- a/app.py\n"
                        "+++ b/app.py\n"
                        "@@ -1 +1 @@\n"
                        "-old = True\n"
                        "+new = True\n"
                    ),
                }
            ],
            "usage": {"input_tokens": 30, "output_tokens": 12},
        }

    provider = OpenAICompatiblePatchProvider(
        LLMProviderConfig(
            provider="kimi-code",
            model=DEFAULT_KIMI_CODE_MODEL,
            base_url=KIMI_CODE_BASE_URL,
            api_key_env="KIMI_API_KEY",
            api_protocol="anthropic",
        ),
        transport=transport,
    )

    result = provider.generate_patch(repo, _plan(), _contract(), _envelope())

    assert result.patch_diff.startswith("diff --git")
    assert result.input_tokens == 30
    assert result.output_tokens == 12
    assert calls[0][0] == f"{KIMI_CODE_BASE_URL}v1/messages"
    assert calls[0][1]["x-api-key"] == "sk-test-key-for-kimi"
    assert calls[0][1]["anthropic-version"] == "2023-06-01"
    assert calls[0][2]["model"] == DEFAULT_KIMI_CODE_MODEL
    assert calls[0][2]["max_tokens"] == 4096
    assert "response_format" not in calls[0][2]
    assert "system" in calls[0][2]
    assert calls[0][2]["messages"][0]["role"] == "user"


def test_kimi_code_routing_provider_parses_anthropic_component_json(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-key-for-kimi")
    calls = []

    def transport(url, headers, payload, timeout_seconds):
        calls.append((url, headers, payload, timeout_seconds))
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"components":["src/plugins/intel_gpu"],'
                        '"files":["src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp"],'
                        '"tests":[],"query_terms":["ocl","intel_gpu"],'
                        '"confidence":0.87,"rationale":"log path points to ocl_memory"}'
                    ),
                }
            ],
            "usage": {"input_tokens": 50, "output_tokens": 20},
        }

    provider = OpenAICompatibleRoutingProvider(
        LLMProviderConfig(
            provider="kimi-code",
            model=DEFAULT_KIMI_CODE_MODEL,
            base_url=KIMI_CODE_BASE_URL,
            api_key_env="KIMI_API_KEY",
            api_protocol="anthropic",
        ),
        transport=transport,
    )

    result = provider.enrich_routing(
        repo_path=tmp_path,
        issue_text="GPU CL_OUT_OF_RESOURCES",
        project_profile=ProjectProfile(repo_path=tmp_path, primary_language="C++"),
        repo_sketch=RepoSketch(
            repo_root=tmp_path,
            commit=None,
            languages=["C++"],
            frameworks=[],
            entrypoints=[],
            core_modules=[
                ModuleCapsule(
                    name="intel_gpu",
                    path="src/plugins/intel_gpu",
                    responsibility="GPU plugin.",
                )
            ],
            test_commands=[],
            test_map={},
            high_risk_paths=[],
        ),
        deterministic_result=IssueRoutingResult(
            source="deterministic",
            candidates=[
                RoutingCandidate(
                    path="src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp",
                    score=100,
                )
            ],
        ),
    )

    assert result.files == ["src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp"]
    assert result.components == ["src/plugins/intel_gpu"]
    assert result.llm_input_tokens == 50
    assert calls[0][0] == f"{KIMI_CODE_BASE_URL}v1/messages"
    assert calls[0][1]["x-api-key"] == "sk-test-key-for-kimi"
    assert calls[0][1]["anthropic-version"] == "2023-06-01"
    assert calls[0][2]["model"] == DEFAULT_KIMI_CODE_MODEL
    assert calls[0][2]["max_tokens"] == 2048
    assert "max_completion_tokens" not in calls[0][2]
    assert "response_format" not in calls[0][2]
    assert "prompt_cache_key" not in calls[0][2]
    assert "Deterministic candidates JSON" in calls[0][2]["messages"][0]["content"]


def test_post_json_reports_kimi_http_error_body(monkeypatch):
    class ErrorBody:
        def read(self):
            return b'{"error":{"message":"invalid api key"}}'

        def close(self):
            return None

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://api.moonshot.cn/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=ErrorBody(),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(ToolError, match="invalid api key"):
        _post_json(
            "https://api.moonshot.cn/v1/chat/completions",
            {"Authorization": "Bearer sk-bad"},
            {"model": "kimi-k2.6", "messages": []},
            1,
        )


def test_extract_unified_diff_handles_plain_and_fenced_output():
    plain = "notes\n\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    fenced = "```diff\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n```"

    assert extract_unified_diff(plain).startswith("diff --git")
    assert extract_unified_diff(fenced).startswith("diff --git")


def _request(**updates) -> TaskRequest:
    data = {
        "repo_path": Path("examples/fixtures/mini_data_pipeline"),
        "issue_path": Path("examples/issues/csv_trailing_empty_column.md"),
        "out_path": Path(".runs/demo_csv_bug"),
    }
    data.update(updates)
    return TaskRequest(**data)


def _repo(tmp_path: Path, source: str = "old = True\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(source)
    return repo


def _contract() -> AcceptanceContract:
    return AcceptanceContract(
        requirement_id="R1",
        summary="Update app behavior.",
        user_visible_before="Old behavior.",
        user_visible_after="New behavior.",
        examples=[],
        invariants=[],
        non_goals=[],
        ambiguity_questions=[],
        status="ready",
    )


def _plan() -> PatchPlan:
    return PatchPlan(
        hypothesis="Old behavior is hardcoded.",
        edit_strategy="Change app.py only.",
        files_to_edit=["app.py"],
        tests_to_add_or_update=[],
        expected_behavior_change="New behavior.",
    )


def _envelope() -> ImpactEnvelope:
    return ImpactEnvelope(
        target_symbols=[SymbolRef(symbol="app", path="app.py")],
        allowed_files=["app.py"],
        affected_files=["app.py"],
        affected_tests=[],
        forbidden_changes=["dependency change", "public API signature change"],
        risk_level="low",
        requires_human_approval=False,
    )
