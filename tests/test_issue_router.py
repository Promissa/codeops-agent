from pathlib import Path

from codeops.agents.issue_router import IssueRouter
from codeops.core.models import ModuleCapsule, ProjectProfile, RepoSketch


def test_issue_router_prefers_runtime_gpu_components_from_logs(tmp_path: Path):
    _write(tmp_path, "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp")
    _write(tmp_path, "src/plugins/intel_gpu/src/plugin/program_builder.cpp")
    _write(tmp_path, "src/plugins/hetero/src/compiled_model.cpp")
    _write(tmp_path, "docs/articles_en/assets/snippets/main.py")
    issue = """### Issue description
HETERO GPU.0,GPU.1 VLM pipeline fails.

### Relevant log output
Exception from src\\plugins\\intel_gpu\\src\\runtime\\ocl\\ocl_memory.cpp:148:
[GPU] clEnqueueWriteBuffer, error code: -5 CL_OUT_OF_RESOURCES
Exception from src\\plugins\\hetero\\src\\compiled_model.cpp:36:
"""

    result = IssueRouter().route(
        repo_path=tmp_path,
        issue_text=issue,
        project_profile=ProjectProfile(repo_path=tmp_path, primary_language="C++"),
        repo_sketch=_sketch(tmp_path),
        graph_files=[],
    )

    assert "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp" in result.files
    assert "src/plugins/hetero/src/compiled_model.cpp" in result.files
    assert "docs/articles_en/assets/snippets/main.py" not in result.files
    assert "src/plugins/intel_gpu" in result.components
    assert result.confidence >= 0.55


def test_issue_router_accepts_llm_refinement_with_safe_existing_file(tmp_path: Path):
    _write(tmp_path, "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp")
    provider = FakeRoutingProvider()

    result = IssueRouter().route(
        repo_path=tmp_path,
        issue_text="GPU CL_OUT_OF_RESOURCES",
        project_profile=ProjectProfile(repo_path=tmp_path, primary_language="C++"),
        repo_sketch=_sketch(tmp_path),
        graph_files=[],
        provider=provider,
    )

    assert result.source == "llm"
    assert result.files == ["src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp"]
    assert result.llm_calls == 1


def test_issue_router_ignores_weak_test_matches(tmp_path: Path):
    _write(tmp_path, "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp")
    _write(tmp_path, "src/plugins/auto/tests/functional/async_device_test.cpp")
    issue = (
        "Exception from src\\plugins\\intel_gpu\\src\\runtime\\ocl\\ocl_memory.cpp:148\n"
        "device sync failed while running HETERO GPU pipeline"
    )

    result = IssueRouter().route(
        repo_path=tmp_path,
        issue_text=issue,
        project_profile=ProjectProfile(repo_path=tmp_path, primary_language="C++"),
        repo_sketch=_sketch(tmp_path),
        graph_files=[],
    )

    assert result.files == ["src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp"]
    assert result.tests == []


def test_issue_router_keeps_direct_test_log_path(tmp_path: Path):
    test_path = "src/plugins/auto/tests/functional/async_device_test.cpp"
    _write(tmp_path, test_path)
    issue = f"Failure reproduced in {test_path}"

    result = IssueRouter().route(
        repo_path=tmp_path,
        issue_text=issue,
        project_profile=ProjectProfile(repo_path=tmp_path, primary_language="C++"),
        repo_sketch=_sketch(tmp_path),
        graph_files=[],
    )

    assert result.tests == [test_path]


def test_issue_router_prioritizes_plugin_logs_over_core_wrappers(tmp_path: Path):
    _write(tmp_path, "src/core/src/runtime/tensor.cpp")
    _write(tmp_path, "src/inference/src/cpp/core.cpp")
    _write(tmp_path, "src/plugins/hetero/src/compiled_model.cpp")
    _write(tmp_path, "src/plugins/intel_gpu/src/plugin/program_builder.cpp")
    _write(tmp_path, "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp")
    issue = """HETERO:GPU.0,GPU.1 PIPELINE_PARALLEL fails.
RuntimeError: Exception from src\\core\\src\\runtime\\tensor.cpp:97:
Exception from src\\plugins\\intel_gpu\\src\\runtime\\ocl\\ocl_memory.cpp:148:
[GPU] clEnqueueWriteBuffer, error code: -5 CL_OUT_OF_RESOURCES
Exception from src\\inference\\src\\cpp\\core.cpp:120:
Exception from src\\plugins\\hetero\\src\\compiled_model.cpp:36:
Check 'false' failed at src\\plugins\\intel_gpu\\src\\plugin\\program_builder.cpp:168:
Exception from src\\plugins\\intel_gpu\\src\\runtime\\ocl\\ocl_memory.cpp:582:
[GPU] clWaitForEvents, error code: -14 CL_EXEC_STATUS_ERROR_FOR_EVENTS_IN_WAIT_LIST
"""

    result = IssueRouter().route(
        repo_path=tmp_path,
        issue_text=issue,
        project_profile=ProjectProfile(repo_path=tmp_path, primary_language="C++"),
        repo_sketch=_sketch(tmp_path),
        graph_files=[],
    )

    assert "src/plugins/hetero/src/compiled_model.cpp" in result.files
    assert "src/plugins/intel_gpu/src/plugin/program_builder.cpp" in result.files
    assert "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp" in result.files
    assert "src/core/src/runtime/tensor.cpp" not in result.files


def _write(root: Path, rel_path: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// source\n")


def _sketch(repo_path: Path) -> RepoSketch:
    return RepoSketch(
        repo_root=repo_path,
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
    )


class FakeRoutingProvider:
    def enrich_routing(self, **kwargs):
        deterministic = kwargs["deterministic_result"]
        return deterministic.model_copy(
            update={
                "source": "llm",
                "files": ["src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp"],
                "components": ["src/plugins/intel_gpu"],
                "query_terms": ["intel_gpu", "ocl"],
                "confidence": 0.9,
                "llm_calls": 1,
            }
        )
