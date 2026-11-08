import json
import textwrap

import pytest
import yaml


def make_project(pytester, source, selected, **changes):
    pytester.makepyfile(selected_case=textwrap.dedent(source))
    cfg = {
        "dut": {"type": "mock"},
        "stages": [
            {"layer": "hardware", "type": "mock_power"},
            {"layer": "linux", "type": "mock_linux", "params": {"file_name": "image.qcow2"}},
            {"layer": "linux", "type": "mock_service", "params": {"name": "web"}},
        ],
        "bindings": {"power": "mock_power", "system": "mock_linux", "service": "mock_service"},
        "tests": [f"selected_case.py::{name}" for name in selected],
    }
    cfg.update(changes)
    return cfg


def run_group(pytester, cfg):
    pytester.makefile(".yaml", stand=yaml.safe_dump(cfg, sort_keys=False))
    return pytester.runpytest_subprocess("--stands-config=stand.yaml", "--stands-artifacts=art", "-v")


def read_events(pytester):
    logs = sorted((pytester.path / "art").glob("*/run.log"))
    if not logs:
        return []
    return [json.loads(line) for line in logs[-1].read_text(encoding="utf-8").splitlines()]


def test_yaml_collects_outside_testpaths_and_controls_order(pytester):
    pytester.makeini("[pytest]\ntestpaths = unrelated\n")
    directory = pytester.path / "unrelated"
    directory.mkdir()
    (directory / "test_unwanted.py").write_text("raise RuntimeError('must not be collected')\n", encoding="utf-8")
    cfg = make_project(pytester, """
        def test_one(power):
            assert power.is_on()
        def test_two(service):
            assert service.is_running()
        def test_unselected():
            raise AssertionError('not selected')
    """, ["test_two", "test_one"])
    result = run_group(pytester, cfg)
    result.assert_outcomes(passed=2, deselected=1)
    starts = [event["test"] for event in read_events(pytester) if event["event"] == "test.start"]
    assert starts == ["selected_case.py::test_two", "selected_case.py::test_one"]


def test_state_is_restored_between_tests_without_repeating_setup(pytester):
    cfg = make_project(pytester, """
        def test_change(power, service, system):
            assert system.is_ready()
            power.set_on(False)
            service.stop()
            assert not power.is_on()
            assert not service.is_running()
        def test_initial_state(power, service):
            assert power.is_on()
            assert service.is_running()
    """, ["test_change", "test_initial_state"])
    result = run_group(pytester, cfg)
    result.assert_outcomes(passed=2)
    events = read_events(pytester)
    setup = [event["stage"] for event in events if event["event"] == "stage.setup.start"]
    restore = [event["stage"] for event in events if event["event"] == "stage.restore.start"]
    cleanup = [event["stage"] for event in events if event["event"] == "stage.cleanup.start"]
    assert setup == ["mock_power", "mock_linux", "mock_service"]
    assert restore == ["mock_service", "mock_linux", "mock_power"] * 2
    assert cleanup == ["mock_service", "mock_linux", "mock_power"]


def test_parametrized_test_uses_explicit_per_case_binding(pytester):
    cfg = make_project(pytester, """
        import pytest
        @pytest.mark.parametrize('expected', [True, False], ids=['main', 'aux'])
        def test_state(power, expected):
            assert power.is_on() is expected
    """, ["test_state"])
    cfg["stages"].insert(1, {"layer": "hardware", "type": "mock_aux_power", "params": {"initial_on": False}})
    cfg["test_bindings"] = {
        "selected_case.py": {"power": "mock_aux_power"},
        "selected_case.py::test_state": {"power": "mock_power"},
        "selected_case.py::test_state[aux]": {"power": "mock_aux_power"},
    }
    run_group(pytester, cfg).assert_outcomes(passed=2)


def test_interface_can_be_bound_only_for_one_test(pytester):
    cfg = make_project(pytester, """
        def test_state(power):
            assert not power.is_on()
    """, ["test_state"], bindings={})
    cfg["stages"].insert(1, {"layer": "hardware", "type": "mock_aux_power", "params": {"initial_on": False}})
    cfg["test_bindings"] = {"selected_case.py::test_state": {"power": "mock_aux_power"}}
    run_group(pytester, cfg).assert_outcomes(passed=1)


def test_missing_binding_in_transitive_fixture_is_rejected_before_setup(pytester):
    cfg = make_project(pytester, """
        import pytest
        @pytest.fixture
        def switched_off(power):
            power.set_on(False)
            return power
        def test_state(switched_off):
            assert not switched_off.is_on()
    """, ["test_state"], bindings={})
    result = run_group(pytester, cfg)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "power" in result.stderr.str()
    assert not any(event["event"] == "stage.setup.start" for event in read_events(pytester))


def test_override_for_unselected_test_is_rejected_before_setup(pytester):
    cfg = make_project(pytester, """
        def test_state(power):
            assert power.is_on()
    """, ["test_state"], test_bindings={"selected_case.py::test_typo": {"power": "mock_power"}})
    result = run_group(pytester, cfg)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "test_typo" in result.stderr.str()
    assert not any(event["event"] == "stage.setup.start" for event in read_events(pytester))


def test_failed_assert_stops_group_but_restores_and_cleans_up(pytester):
    cfg = make_project(pytester, """
        def test_failure(power):
            power.set_on(False)
            assert power.is_on()
        def test_remaining(power):
            assert power.is_on()
    """, ["test_failure", "test_remaining"])
    run_group(pytester, cfg).assert_outcomes(failed=1, skipped=1)
    events = read_events(pytester)
    assert [event["stage"] for event in events if event["event"] == "stage.restore.done"] == [
        "mock_service", "mock_linux", "mock_power"
    ]
    assert [event["stage"] for event in events if event["event"] == "stage.cleanup.done"] == [
        "mock_service", "mock_linux", "mock_power"
    ]
    assert (pytester.path / "art").is_dir()
    assert list((pytester.path / "art").glob("*/config.applied.yaml"))


def test_command_timeout_is_error_not_assertion_failure(pytester):
    cfg = make_project(pytester, """
        def test_timeout(power):
            assert power.is_on()
        def test_remaining(power):
            assert power.is_on()
    """, ["test_timeout", "test_remaining"],
        timeouts={"command": 0.05, "step": 1, "cleanup": 1})
    cfg["stages"][0]["params"] = {"command_delay": 1}
    result = run_group(pytester, cfg)
    result.assert_outcomes(errors=1, skipped=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "OperationTimeout" in result.stdout.str()
    events = read_events(pytester)
    assert any(event["event"] == "test.error" and event["phase"] == "call" for event in events)
    assert not any(event["event"] == "test.passed" for event in events)


def test_xfail_cannot_hide_infrastructure_timeout(pytester):
    cfg = make_project(pytester, """
        import pytest
        @pytest.mark.xfail(reason='application bug')
        def test_timeout(power):
            assert power.is_on()
        def test_remaining(power):
            assert power.is_on()
    """, ["test_timeout", "test_remaining"],
        timeouts={"command": 0.05, "step": 1, "cleanup": 1})
    cfg["stages"][0]["params"] = {"command_delay": 1}
    result = run_group(pytester, cfg)
    result.assert_outcomes(errors=1, skipped=1)
    assert "OperationTimeout" in result.stdout.str()
    assert any(event["event"] == "test.error" for event in read_events(pytester))


def test_restore_failure_stops_remaining_tests_and_cleanup_continues(pytester):
    cfg = make_project(pytester, """
        def test_first(power):
            assert power.is_on()
        def test_remaining(power):
            assert power.is_on()
    """, ["test_first", "test_remaining"])
    cfg["stages"][0]["params"] = {"fail_restore": True}
    result = run_group(pytester, cfg)
    result.assert_outcomes(passed=1, errors=1, skipped=1)
    assert "RestoreError" in result.stdout.str()
    assert [event["stage"] for event in read_events(pytester) if event["event"] == "stage.cleanup.done"] == [
        "mock_service", "mock_linux", "mock_power"
    ]


def test_cleanup_failure_does_not_prevent_other_components_cleanup(pytester):
    cfg = make_project(pytester, """
        def test_state(power):
            assert power.is_on()
    """, ["test_state"])
    cfg["stages"][2]["params"]["fail_cleanup"] = True
    result = run_group(pytester, cfg)
    result.assert_outcomes(passed=1, errors=1)
    assert "CleanupError" in result.stdout.str()
    assert [event["stage"] for event in read_events(pytester) if event["event"] == "stage.cleanup.done"] == [
        "mock_linux", "mock_power"
    ]


def test_setup_recovery_succeeds_within_configured_attempts(pytester):
    cfg = make_project(pytester, """
        def test_state(power):
            assert power.is_on()
    """, ["test_state"])
    cfg["stages"][0]["params"] = {"fail_setup_attempts": 2}
    cfg["stages"][0]["recovery"] = {"strategy": "retry", "attempts": 3}
    run_group(pytester, cfg).assert_outcomes(passed=1)


def test_exhausted_setup_recovery_cleans_only_started_components(pytester):
    cfg = make_project(pytester, """
        def test_first(power):
            assert power.is_on()
        def test_remaining(power):
            assert power.is_on()
    """, ["test_first", "test_remaining"])
    cfg["stages"][0]["params"] = {"fail_setup_attempts": 3}
    cfg["stages"][0]["recovery"] = {"strategy": "retry", "attempts": 2}
    run_group(pytester, cfg).assert_outcomes(errors=1, skipped=1)
    assert [event["stage"] for event in read_events(pytester) if event["event"] == "stage.cleanup.start"] == ["mock_power"]


def test_skip_xfail_and_xpass_are_not_logged_as_passed(pytester):
    cfg = make_project(pytester, """
        import pytest
        def test_skip():
            pytest.skip('not applicable')
        @pytest.mark.xfail(reason='known issue')
        def test_xfail(power):
            power.set_on(False)
            assert power.is_on()
        @pytest.mark.xfail(reason='already fixed', strict=False)
        def test_xpass(power):
            assert power.is_on()
        def test_pass(power):
            assert power.is_on()
    """, ["test_skip", "test_xfail", "test_xpass", "test_pass"])
    run_group(pytester, cfg).assert_outcomes(skipped=1, xfailed=1, xpassed=1, passed=1)
    results = {event["test"]: event["event"] for event in read_events(pytester)
               if event["event"] in {"test.skipped", "test.xfailed", "test.xpassed", "test.passed"}}
    assert results == {
        "selected_case.py::test_skip": "test.skipped",
        "selected_case.py::test_xfail": "test.xfailed",
        "selected_case.py::test_xpass": "test.xpassed",
        "selected_case.py::test_pass": "test.passed",
    }


def test_without_config_plugin_does_not_provide_interface_fixtures(pytester):
    pytester.makepyfile("def test_state(power):\n    assert power.is_on()\n")
    result = pytester.runpytest_subprocess("--stands-artifacts=art")
    result.assert_outcomes(errors=1)
    assert "fixture 'power' not found" in result.stdout.str()
    assert not (pytester.path / "art").exists()


def test_external_config_keeps_paths_relative_to_project(pytester):
    cfg = make_project(pytester, """
        def test_state(power):
            assert power.is_on()
    """, ["test_state"])
    path = pytester.path.parent / f"{pytester.path.name}-stand.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    result = pytester.runpytest_subprocess(f"--stands-config={path}", "--stands-artifacts=art")
    result.assert_outcomes(passed=1)
