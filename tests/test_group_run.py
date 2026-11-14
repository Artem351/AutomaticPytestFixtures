"""Сквозные проверки первого этапа (на моках). Запуск pytest в подпроцессе."""
import re
import textwrap
from pathlib import Path

import pytest

CONFIG = """
dut: {{type: mock}}
stages:
  - {{layer: hardware, type: mock_power, params: {{timeout: 5, count: 2}}}}
  - {{layer: linux, type: mock_linux, params: {{file_name: image.qcow2}}}}
  - {{layer: linux, type: mock_service, params: {{name: web{extra}}}}}
tests:
  - test_group.py::test_a
  - test_group.py::test_b
  - test_group.py::test_c
"""

TESTS = """
def test_a(stands):
    assert set(stands) == {"mock_power", "mock_linux", "mock_service"}

def test_b():
    assert __B_OK__

def test_c():
    pass
"""


def run(pytester, b_ok="True", extra=""):
    pytester.makepyfile(test_group=TESTS.replace("__B_OK__", b_ok))
    pytester.makefile(".yaml", stand=CONFIG.format(extra=extra))
    result = pytester.runpytest_subprocess("--stands-config=stand.yaml", "--stands-artifacts=art", "-v")
    logs = sorted(Path(pytester.path, "art").glob("*/run.log"))
    log = logs[-1].read_text(encoding="utf-8") if logs else ""
    return result, log


def events(log):
    return [l.split(" ", 2)[1] + (" " + l.split(" ", 2)[2] if len(l.split(" ", 2)) > 2 else "") for l in log.splitlines()]


def test_group_passes_stages_run_in_config_order_and_teardown_reversed(pytester):
    result, log = run(pytester)
    result.assert_outcomes(passed=3)
    ev = events(log)
    setups = [e for e in ev if e.startswith(("mock_power.on", "mock_linux.deploy", "mock_service.start"))]
    assert [s.split()[0] for s in setups] == ["mock_power.on", "mock_linux.deploy", "mock_service.start"]
    downs = [e.split()[0] for e in ev if e.startswith(("mock_power.off", "mock_linux.cleanup", "mock_service.stop"))]
    assert downs == ["mock_service.stop", "mock_linux.cleanup", "mock_power.off"]


def test_params_from_config_reach_stage(pytester):
    _, log = run(pytester)
    assert "mock_power.on timeout=5.0 count=2" in log
    assert "mock_linux.deploy file_name=image.qcow2" in log


def test_timestamps_iso_utc_millis(pytester):
    _, log = run(pytester)
    for line in log.splitlines():
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ", line), line


def test_failed_assert_stops_group_rest_skipped_cleanup_runs(pytester):
    result, log = run(pytester, b_ok="False")
    result.assert_outcomes(passed=1, failed=1, skipped=1)
    assert "mock_power.off" in log and "mock_service.stop" in log
    assert "group.stopped" in log


def test_infrastructure_failure_is_error_rest_skipped_and_started_stages_cleaned(pytester):
    result, log = run(pytester, extra=", fail_setup: true")
    result.assert_outcomes(errors=1, skipped=2)
    ev = [e.split()[0] for e in events(log)]
    assert "mock_service.stop" in ev      # начатый этап откатил свои изменения
    assert "mock_linux.cleanup" in ev and "mock_power.off" in ev


def test_invalid_config_rejected_before_any_stage_runs(pytester):
    pytester.makepyfile(test_group=TESTS.replace("__B_OK__", "True"))
    pytester.makefile(".yaml", stand=CONFIG.format(extra=", bogus: 1"))
    result = pytester.runpytest_subprocess("--stands-config=stand.yaml", "--stands-artifacts=art")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*pytest-stands*bogus*"])
    assert not list(Path(pytester.path, "art").glob("*/run.log"))


def test_tests_selected_and_ordered_by_config(pytester):
    pytester.makepyfile(test_group=TESTS.replace("__B_OK__", "True"))
    cfg = CONFIG.format(extra="").replace("  - test_group.py::test_a\n  - test_group.py::test_b\n  - test_group.py::test_c",
                                           "  - test_group.py::test_c\n  - test_group.py::test_a")
    pytester.makefile(".yaml", stand=cfg)
    result = pytester.runpytest_subprocess("--stands-config=stand.yaml", "--stands-artifacts=art", "-v")
    result.assert_outcomes(passed=2)
    out = result.stdout.str()
    assert out.index("test_c") < out.index("test_a")
    assert "test_b" not in out.replace("test_group.py::test_b", "") or "deselected" in out


def test_without_config_plugin_is_inert(pytester):
    pytester.makepyfile("def test_x():\n    assert True\n")
    pytester.runpytest_subprocess().assert_outcomes(passed=1)
