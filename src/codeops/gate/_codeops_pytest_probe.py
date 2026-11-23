"""Pytest plugin loaded from the CodeOps runtime to record executed test bodies."""

import json
import os
import sys

import pytest


PREFIX = "CODEOPS_PYTEST_EVIDENCE "
_cases = {}


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    root = os.getcwd() + os.sep
    lines = {}

    def trace_frame(frame, event, arg):
        if event == "line":
            lines.setdefault(frame.f_code.co_filename[len(root):], set()).add(frame.f_lineno)
        return trace_frame

    def trace_call(frame, event, arg):
        if event == "call" and frame.f_code.co_filename.startswith(root):
            return trace_frame
        return None

    previous = sys.gettrace()
    sys.settrace(trace_call)
    try:
        return (yield)
    finally:
        sys.settrace(previous)
        _cases.setdefault(item.nodeid, {})["lines"] = {
            path: sorted(numbers) for path, numbers in lines.items()
        }


def pytest_runtest_logreport(report):
    case = _cases.setdefault(report.nodeid, {})
    if report.when == "call" or report.failed or report.skipped:
        outcome = "skipped" if hasattr(report, "wasxfail") and not report.failed else report.outcome
        if case.get("status") != "failed":
            case["status"] = outcome


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(PREFIX + json.dumps(_cases, separators=(",", ":")))
