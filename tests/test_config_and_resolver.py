import math

import pytest
from pydantic import ValidationError

from pytest_stands.config import ConfigError, load_config
from pytest_stands.config.models import Config, RecoveryConfig, StageTimeouts, Timeouts
from pytest_stands.resolver import PlanError, build_plan


def configuration(**changes):
    values = {
        'dut': {'type': 'mock'},
        'stages': [
            {'layer': 'hardware', 'type': 'mock_power'},
            {'layer': 'hardware', 'type': 'mock_aux_power'},
            {'layer': 'linux', 'type': 'mock_linux', 'params': {'file_name': 'image'}},
            {'layer': 'linux', 'type': 'mock_service', 'params': {'name': 'application'}},
        ],
        'tests': ['tests/test_portable.py'],
        'bindings': {'power': 'mock_power', 'system': 'mock_linux', 'service': 'mock_service'},
    }
    values.update(changes)
    return values


@pytest.mark.parametrize('value', [True, False, 0, -1, math.inf, -math.inf, math.nan, '3'])
@pytest.mark.parametrize('field', ['command', 'step', 'cleanup', 'group'])
def test_timeout_values_must_be_positive_finite_numbers(value, field):
    with pytest.raises(ValidationError, match=field):
        Timeouts.model_validate({field: value})


@pytest.mark.parametrize('field', ['command', 'step', 'cleanup'])
def test_stage_timeout_none_inherits_and_boolean_is_rejected(field):
    assert getattr(StageTimeouts.model_validate({field: None}), field) is None
    with pytest.raises(ValidationError, match=field):
        StageTimeouts.model_validate({field: True})


@pytest.mark.parametrize('values', [
    {'strategy': 'none', 'attempts': 2},
    {'strategy': 'none', 'delay': 1},
    {'strategy': 'retry', 'attempts': True},
    {'strategy': 'retry', 'attempts': 0},
    {'strategy': 'retry', 'delay': math.inf},
    {'strategy': 'retry', 'delay': True},
    {'strategy': 'restart'},
])
def test_recovery_rejects_unused_or_invalid_options(values):
    with pytest.raises(ValidationError):
        RecoveryConfig.model_validate(values)


def test_plan_preserves_stage_order_and_merges_timeouts():
    values = configuration(timeouts={'command': 5, 'step': 20, 'cleanup': 9, 'group': 100})
    values['stages'][1]['timeouts'] = {'command': 2, 'step': None}
    plan = build_plan(Config.model_validate(values))
    assert [stage.name for stage in plan] == ['mock_power', 'mock_aux_power', 'mock_linux', 'mock_service']
    assert plan[1].timeouts.model_dump() == {'command': 2, 'step': 20, 'cleanup': 9, 'group': 100}
    assert plan[0].timeouts.command == 5


def test_unknown_dut_is_rejected():
    with pytest.raises(PlanError, match='dut.type.*unknown platform'):
        build_plan(configuration(dut={'type': 'physical'}))


def test_duplicate_component_is_rejected():
    values = configuration()
    values['stages'].append({'layer': 'hardware', 'type': 'mock_power'})
    with pytest.raises(PlanError, match='duplicate stage type.*mock_power'):
        build_plan(values)


def test_dependency_must_precede_component():
    values = configuration()
    values['stages'] = [values['stages'][2], values['stages'][0]]
    with pytest.raises(PlanError, match='mock_linux.*dependencies.*mock_power'):
        build_plan(values)


def test_layer_order_is_not_silently_rearranged():
    values = configuration()
    values['stages'] = [values['stages'][0], values['stages'][2], values['stages'][1]]
    with pytest.raises(PlanError, match='mock_aux_power.*layer order'):
        build_plan(values)


def test_required_component_parameter_has_component_and_field_diagnostic():
    values = configuration()
    values['stages'][2]['params'] = {}
    with pytest.raises(PlanError, match='mock_linux.*params.*file_name'):
        build_plan(values)


def test_extra_component_parameter_is_rejected():
    values = configuration()
    values['stages'][0]['params'] = {'unexpected': 1}
    with pytest.raises(PlanError, match='mock_power.*unexpected.*Extra inputs'):
        build_plan(values)


@pytest.mark.parametrize('bindings, diagnostic', [
    ({'power': 'absent'}, 'not configured'),
    ({'power': 'mock_service'}, 'does not provide'),
    ({'unknown': 'mock_power'}, 'unknown interface'),
])
def test_binding_targets_are_validated_before_setup(bindings, diagnostic):
    with pytest.raises(PlanError, match=diagnostic):
        build_plan(configuration(bindings=bindings))


def test_per_test_bindings_identify_invalid_selector_target():
    values = configuration(test_bindings={'tests/test_portable.py::test_state': {'power': 'mock_linux'}})
    with pytest.raises(PlanError, match='test_bindings.*test_state.*mock_linux'):
        build_plan(values)


def test_yaml_extra_key_reports_field(tmp_path):
    path = tmp_path / 'invalid.yaml'
    path.write_text('dut: {type: mock}\nstages: [{layer: hardware, type: mock_power}]\ntests: [test_portable.py]\nunexpected: true\n', encoding='utf-8')
    with pytest.raises(ConfigError, match='unexpected.*Extra inputs'):
        load_config(path)


def test_yaml_duplicate_key_is_not_silently_overwritten(tmp_path):
    path = tmp_path / 'duplicate.yaml'
    path.write_text('dut: {type: mock, type: other}\n', encoding='utf-8')
    with pytest.raises(ConfigError, match='duplicate key.*type'):
        load_config(path)


def test_third_party_registration_supports_new_interface_and_platform(monkeypatch):
    from abc import ABC, abstractmethod

    from pytest_stands.layers.hardware.mock_power import MockPower
    from pytest_stands.layers.registry import register_interface, register_stage
    from pytest_stands.layers import registry

    registry.load_builtin_and_plugins()
    monkeypatch.setattr(registry, '_stages', dict(registry._stages))
    monkeypatch.setattr(registry, '_interfaces', dict(registry._interfaces))

    @register_interface('extension_status')
    class StatusControl(ABC):
        @abstractmethod
        def status(self) -> bool:
            raise NotImplementedError

    @register_stage('extension_power')
    class ExtensionPower(MockPower, StatusControl):
        platforms = ('extension_platform',)
        provides = ('power', 'extension_status')
        recovery_strategies = ('none',)

        def status(self) -> bool:
            return self.is_on()

    values = configuration(
        dut={'type': 'extension_platform'},
        stages=[{'layer': 'hardware', 'type': 'extension_power'}],
        bindings={'extension_status': 'extension_power'},
    )
    assert build_plan(values)[0].cls is ExtensionPower
    values['dut'] = {'type': 'mock'}
    with pytest.raises(PlanError, match='extension_power.*incompatible.*mock'):
        build_plan(values)
    values['dut'] = {'type': 'extension_platform'}
    values['stages'][0]['recovery'] = {'strategy': 'retry', 'attempts': 2}
    with pytest.raises(PlanError, match='extension_power.*unsupported strategy'):
        build_plan(values)


@pytest.mark.parametrize('params, field', [
    ({'setup_delay': True}, 'setup_delay'),
    ({'command_delay': -1}, 'command_delay'),
    ({'restore_delay': math.inf}, 'restore_delay'),
    ({'cleanup_delay': math.nan}, 'cleanup_delay'),
    ({'fail_setup_attempts': True}, 'fail_setup_attempts'),
    ({'initial_on': 1}, 'initial_on'),
])
def test_mock_controls_are_validated_before_setup(params, field):
    values = configuration()
    values['stages'][0]['params'] = params
    with pytest.raises(PlanError, match=f'mock_power.*{field}'):
        build_plan(values)
