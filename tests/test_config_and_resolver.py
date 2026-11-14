import pytest

from pytest_stands.config import ConfigError, load_config
from pytest_stands.layers.registry import load_builtin_and_plugins
from pytest_stands.resolver import PlanError, build_plan

load_builtin_and_plugins()

BASE = """
dut: {type: mock}
stages:
  - {layer: hardware, type: mock_power, params: {timeout: 5, count: 2}}
  - {layer: linux, type: mock_linux, params: {file_name: image.qcow2}}
tests: [tests/test_x.py::test_a]
"""


def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def plan_of(tmp_path, text):
    return build_plan(load_config(write(tmp_path, text)))


def test_valid_config_builds_plan_in_config_order(tmp_path):
    plan = plan_of(tmp_path, BASE)
    assert [p.cls.name for p in plan] == ["mock_power", "mock_linux"]
    assert plan[0].params.count == 2


def test_extra_param_rejected(tmp_path):
    with pytest.raises(PlanError, match="mock_power.*count|extra|Extra"):
        plan_of(tmp_path, BASE.replace("count: 2", "count: 2, colour: red"))


def test_missing_required_param_rejected(tmp_path):
    with pytest.raises(PlanError, match="file_name"):
        plan_of(tmp_path, BASE.replace("params: {file_name: image.qcow2}", "params: {}"))


def test_wrong_param_type_rejected(tmp_path):
    with pytest.raises(PlanError, match="count"):
        plan_of(tmp_path, BASE.replace("count: 2", "count: many"))


def test_dependency_must_come_first(tmp_path):
    text = """
dut: {type: mock}
stages:
  - {layer: linux, type: mock_linux, params: {file_name: a}}
tests: [tests/test_x.py::test_a]
"""
    with pytest.raises(PlanError, match="требует 'mock_power'"):
        plan_of(tmp_path, text)


def test_unknown_stage_lists_available(tmp_path):
    with pytest.raises(PlanError, match="mock_power"):
        plan_of(tmp_path, BASE.replace("type: mock_power", "type: nope"))


def test_layer_order_hardware_before_linux(tmp_path):
    text = """
dut: {type: mock}
stages:
  - {layer: linux, type: mock_linux, params: {file_name: a}}
  - {layer: hardware, type: mock_power}
tests: [tests/test_x.py::test_a]
"""
    with pytest.raises(ConfigError, match="сначала hardware"):
        load_config(write(tmp_path, text))


def test_all_errors_reported_at_once(tmp_path):
    text = BASE.replace("count: 2", "count: many").replace("params: {file_name: image.qcow2}", "params: {}")
    with pytest.raises(PlanError) as e:
        plan_of(tmp_path, text)
    assert "count" in str(e.value) and "file_name" in str(e.value)
