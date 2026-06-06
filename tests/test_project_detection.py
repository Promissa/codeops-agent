from pathlib import Path

from codeops.languages.detector import ProjectDetector


def test_project_detector_detects_python_fixture():
    profile = ProjectDetector().detect(Path("examples/fixtures/mini_data_pipeline"))

    assert profile.primary_language == "Python"
    assert profile.language_profiles == ["python"]
    assert "pyproject" in profile.build_systems
    assert "pytest" in profile.test_frameworks


def test_project_detector_detects_javascript_fixture():
    profile = ProjectDetector().detect(Path("examples/fixtures/js_vitest_project"))

    assert profile.primary_language == "JavaScript"
    assert profile.language_profiles == ["javascript"]
    assert "npm" in profile.package_managers
    assert "node:test" in profile.test_frameworks


def test_project_detector_detects_typescript_fixture():
    profile = ProjectDetector().detect(Path("examples/fixtures/ts_express_project"))

    assert profile.primary_language == "TypeScript"
    assert "JavaScript" in profile.secondary_languages
    assert "express" in profile.frameworks
    assert "tsconfig" in profile.build_systems


def test_project_detector_detects_go_fixture():
    profile = ProjectDetector().detect(Path("examples/fixtures/go_http_project"))

    assert profile.primary_language == "Go"
    assert profile.language_profiles == ["go"]
    assert "go modules" in profile.build_systems
    assert "go test" in profile.test_frameworks


def test_project_detector_detects_rust_fixture():
    profile = ProjectDetector().detect(Path("examples/fixtures/rust_cli_project"))

    assert profile.primary_language == "Rust"
    assert profile.language_profiles == ["rust"]
    assert "cargo" in profile.build_systems
    assert "cargo test" in profile.test_frameworks
