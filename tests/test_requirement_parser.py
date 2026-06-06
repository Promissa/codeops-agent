import json

from typer.testing import CliRunner

from codeops.agents.requirement_parser import RequirementParser
from codeops.cli import app


def test_requirement_parser_produces_ready_contract():
    issue = """Bug: CSV parser crashes when a row contains trailing empty columns.

Reproduction:
parse_csv("a,b,c\\n1,2,\\n")

Expected: Should return None for trailing empty field.
Actual: IndexError.

Invariants:
- Existing NULL handling remains unchanged.

Non-goals:
- Quoted CSV support.
"""

    contract = RequirementParser().parse(issue)

    assert contract.status == "ready"
    assert contract.summary == (
        "CSV parser crashes when a row contains trailing empty columns."
    )
    assert contract.user_visible_after == (
        "Should return None for trailing empty field."
    )
    assert contract.user_visible_before == "IndexError."
    assert contract.examples[0].input == 'parse_csv("a,b,c\\n1,2,\\n")'
    assert contract.invariants == ["Existing NULL handling remains unchanged."]
    assert contract.non_goals == ["Quoted CSV support."]


def test_requirement_parser_needs_clarification_without_expected_behavior():
    issue = """Bug: CSV parser crashes.

Actual: IndexError.
"""

    contract = RequirementParser().parse(issue)

    assert contract.status == "needs_clarification"
    assert contract.user_visible_after is None
    assert contract.examples == []
    assert contract.ambiguity_questions == [
        "What user-visible behavior should this change produce?"
    ]


def test_requirement_parser_handles_github_issue_sections():
    issue = """### Issue description

Hello.
I will re-post the issue so that it can be prioritized.
HETERO GPU pipeline crashes during VLM inference.

### Step-by-step reproduction

Run the model with `HETERO:GPU.0,GPU.1`.

### Relevant log output

```shell
[GPU] clEnqueueWriteBuffer, error code: -5 CL_OUT_OF_RESOURCES
```
"""

    contract = RequirementParser().parse(issue)

    assert contract.status == "ready"
    assert contract.summary == "HETERO GPU pipeline crashes during VLM inference."
    assert contract.user_visible_after == (
        "The documented reproduction should complete without the logged failure."
    )
    assert "CL_OUT_OF_RESOURCES" in contract.user_visible_before


def test_cli_writes_acceptance_contract(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "parser.py").write_text("def parse_csv(text):\n    return []\n")
    issue = tmp_path / "issue.md"
    issue.write_text("Bug: parser issue\nExpected: parser should work\nActual: fail\n")
    out = tmp_path / ".runs" / "phase5_demo"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--issue",
            str(issue),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    task = json.loads((out / "task.json").read_text())
    contract_text = (out / "acceptance_contract.yaml").read_text()
    assert task["status"] == "reviewed"
    assert task["acceptance_contract"]["status"] == "ready"
    assert 'summary: "parser issue"' in contract_text


def test_cli_stops_before_tests_when_contract_needs_clarification(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "parser.py").write_text("def parse_csv(text):\n    return []\n")
    issue = tmp_path / "issue.md"
    issue.write_text("Bug: parser issue\nActual: fail\n")
    out = tmp_path / ".runs" / "needs_clarification_demo"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--issue",
            str(issue),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    task = json.loads((out / "task.json").read_text())
    assert task["status"] == "needs_clarification"
    assert task["test_results"] == []


def test_cli_routes_github_issue_to_source_components(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src/plugins/intel_gpu/src/runtime/ocl").mkdir(parents=True)
    (repo / "src/plugins/hetero/src").mkdir(parents=True)
    (repo / "docs/articles_en/assets/snippets").mkdir(parents=True)
    (repo / "src/openarc").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp").write_text("// gpu\n")
    (repo / "src/plugins/hetero/src/compiled_model.cpp").write_text("// hetero\n")
    (repo / "docs/articles_en/assets/snippets/main.py").write_text("def main(): pass\n")
    (repo / "src/openarc/pipeline.py").write_text("def run(): pass\n")
    (repo / "tests/test_pipeline.py").write_text("def test_pipeline(): pass\n")
    issue = tmp_path / "issue.md"
    issue.write_text(
        "### Issue description\n"
        "HETERO GPU.0,GPU.1 VLM pipeline fails.\n\n"
        "### Step-by-step reproduction\n"
        "Run with HETERO:GPU.0,GPU.1.\n\n"
        "### Relevant log output\n"
        "Exception from src\\plugins\\intel_gpu\\src\\runtime\\ocl\\ocl_memory.cpp:148:\n"
        "[GPU] clEnqueueWriteBuffer, error code: -5 CL_OUT_OF_RESOURCES\n"
        "Exception from src\\plugins\\hetero\\src\\compiled_model.cpp:36:\n"
    )
    out = tmp_path / ".runs" / "routing_demo"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--issue",
            str(issue),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    routing = json.loads((out / "issue_routing.json").read_text())
    impact = (out / "impact_envelope.yaml").read_text()
    assert "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp" in routing["files"]
    assert "src/plugins/hetero/src/compiled_model.cpp" in routing["files"]
    assert "docs/articles_en/assets/snippets/main.py" not in routing["files"]
    assert "src/plugins/intel_gpu/src/runtime/ocl/ocl_memory.cpp" in impact
    assert "tests/test_pipeline.py" not in impact
