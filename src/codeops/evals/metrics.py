"""Evaluation metrics."""

from pydantic import BaseModel


class EvalTaskResult(BaseModel):
    task_id: str
    solved: bool
    graph_confidence: float | None = None
    raw_file_reads: int = 0
    tool_calls: int = 0
    codegraph_calls: int = 0
    test_runs: int = 0
    estimated_cost_usd: float | None = None
    test_runtime_seconds: float = 0.0


class EvalSummary(BaseModel):
    mode: str
    task_count: int
    solve_rate: float
    avg_graph_confidence: float | None
    avg_tool_calls: float
    avg_raw_file_reads: float
    avg_test_runs: float
    estimated_cost_usd: float | None


def summarize(mode: str, results: list[EvalTaskResult]) -> EvalSummary:
    task_count = len(results)
    solve_rate = (
        sum(1 for result in results if result.solved) / task_count
        if task_count
        else 0.0
    )
    confidences = [
        result.graph_confidence
        for result in results
        if result.graph_confidence is not None
    ]
    costs = [
        result.estimated_cost_usd
        for result in results
        if result.estimated_cost_usd is not None
    ]
    return EvalSummary(
        mode=mode,
        task_count=task_count,
        solve_rate=round(solve_rate, 4),
        avg_graph_confidence=_average(confidences),
        avg_tool_calls=_average([result.tool_calls for result in results]) or 0.0,
        avg_raw_file_reads=_average([result.raw_file_reads for result in results])
        or 0.0,
        avg_test_runs=_average([result.test_runs for result in results]) or 0.0,
        estimated_cost_usd=sum(costs) if costs else None,
    )


def _average(values: list[float | int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)
