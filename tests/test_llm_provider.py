from pathlib import Path

import pytest

from codeops.agents.llm_provider import (
    DEFAULT_KIMI_MODEL,
    KIMI_BASE_URL,
    KIMI_CODE_BASE_URL,
    LLMProviderConfig,
    OpenAICompatiblePatchProvider,
    extract_unified_diff,
    llm_config_from_request,
)
from codeops.core.errors import ToolError
from codeops.core.models import AcceptanceContract, ImpactEnvelope, PatchPlan, SymbolRef, TaskRequest


def test_kimi_config_defaults_to_moonshot_endpoint(monkeypatch):
    monkeypatch.delenv("CODEOPS_LLM_MODEL", raising=False)
    request = _request(llm_provider="kimi")

    config = llm_config_from_request(request)

    assert config is not None
    assert config.provider == "kimi"
    assert config.model == DEFAULT_KIMI_MODEL
    assert config.base_url == KIMI_BASE_URL
    assert config.api_key_env == "MOONSHOT_API_KEY"


def test_kimi_code_config_defaults_to_coding_endpoint():
    request = _request(llm_provider="kimi-code")

    config = llm_config_from_request(request)

    assert config is not None
    assert config.provider == "kimi-code"
    assert config.base_url == KIMI_CODE_BASE_URL
    assert config.api_key_env == "KIMI_API_KEY"


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
