import inspect
from dataclasses import dataclass

from pydantic import ValidationError

from .config.models import Config, Layer, RecoveryConfig, Timeouts
from .layers.base import Stage, StageParams
from .layers.registry import get_interface, get_stage, load_builtin_and_plugins, stage_names


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class PlannedStage:
    index: int
    cls: type[Stage]
    params: StageParams
    timeouts: Timeouts
    recovery: RecoveryConfig

    @property
    def name(self) -> str:
        return self.cls.name


def _validation_details(error):
    return '; '.join(
        f'{".".join(str(part) for part in item["loc"]) or "configuration"}: {item["msg"]}'
        for item in error.errors(include_url=False)
    )


def _names(value, field, component):
    if not isinstance(value, tuple) or any(not isinstance(name, str) or not name.strip() for name in value):
        raise PlanError(f'component {component!r}: {field} must be a tuple of nonempty names')
    if len(value) != len(set(value)):
        raise PlanError(f'component {component!r}: duplicate name in {field}')
    return value


def _stage_contract(cls):
    name = cls.name
    if inspect.isabstract(cls):
        raise PlanError(f'component {name!r}: stage has unimplemented abstract methods')
    if not isinstance(cls.params_model, type) or not issubclass(cls.params_model, StageParams):
        raise PlanError(f'component {name!r}: params_model must subclass StageParams')
    for field in ('platforms', 'provides', 'requires', 'recovery_strategies'):
        _names(getattr(cls, field), field, name)
    if not cls.platforms:
        raise PlanError(f'component {name!r}: platforms must not be empty')
    for interface in cls.provides:
        try:
            interface_cls = get_interface(interface)
        except ValueError as error:
            raise PlanError(f'component {name!r}: provides: {error}') from error
        if not issubclass(cls, interface_cls):
            raise PlanError(f'component {name!r}: provides {interface!r} but does not implement its interface')


def validate_bindings(bindings: dict[str, str], plan: list[PlannedStage]) -> None:
    if not isinstance(bindings, dict):
        raise PlanError('bindings must be a mapping of interface names to component names')
    targets = {stage.name: stage for stage in plan}
    for interface, target in bindings.items():
        if not isinstance(interface, str) or not interface.strip():
            raise PlanError('bindings: interface name must be a nonempty string')
        if not isinstance(target, str) or not target.strip():
            raise PlanError(f'bindings.{interface}: target must be a nonempty component name')
        try:
            interface_cls = get_interface(interface)
        except ValueError as error:
            raise PlanError(f'bindings.{interface}: {error}') from error
        if target not in targets:
            raise PlanError(f'bindings.{interface}: target component {target!r} is not configured')
        cls = targets[target].cls
        if interface not in cls.provides or not issubclass(cls, interface_cls):
            raise PlanError(f'bindings.{interface}: component {target!r} does not provide and implement interface {interface!r}')


def build_plan(cfg: Config) -> list[PlannedStage]:
    try:
        cfg = Config.model_validate(cfg.model_dump() if isinstance(cfg, Config) else cfg)
    except ValidationError as error:
        raise PlanError(_validation_details(error)) from error
    except (ValueError, TypeError, RecursionError) as error:
        raise PlanError(f'configuration: {error}') from error
    try:
        load_builtin_and_plugins()
        registered = [get_stage(name) for name in stage_names()]
    except (ValueError, ImportError, TypeError) as error:
        raise PlanError(f'component registry: {error}') from error
    platforms = {
        platform
        for cls in registered
        for platform in _names(cls.platforms, 'platforms', cls.name)
    }
    if cfg.dut.type not in platforms:
        raise PlanError(f'dut.type: unknown platform {cfg.dut.type!r}; supported platforms: {", ".join(sorted(platforms))}')
    plan = []
    seen = set()
    layer_order = {Layer.hardware: 0, Layer.linux: 1}
    previous_layer = -1
    for index, configured in enumerate(cfg.stages):
        name = configured.type
        prefix = f'stages.{index} component {name!r}'
        if name in seen:
            raise PlanError(f'{prefix}: duplicate stage type')
        try:
            cls = get_stage(name)
        except ValueError as error:
            raise PlanError(f'{prefix}: {error}') from error
        _stage_contract(cls)
        try:
            declared_layer = Layer(cls.layer)
        except (AttributeError, ValueError, TypeError) as error:
            raise PlanError(f'{prefix}: component declares an invalid layer') from error
        if configured.layer != declared_layer:
            raise PlanError(f'{prefix}: layer {configured.layer.value!r} does not match component layer {declared_layer.value!r}')
        if layer_order[configured.layer] < previous_layer:
            raise PlanError(f'{prefix}: layer order requires hardware before linux; YAML order is preserved')
        if cfg.dut.type not in cls.platforms:
            raise PlanError(f'{prefix}: incompatible with dut.type {cfg.dut.type!r}; supported platforms: {", ".join(cls.platforms)}')
        missing = [required for required in cls.requires if required not in seen]
        if missing:
            raise PlanError(f'{prefix}: dependencies must be configured earlier: {", ".join(missing)}')
        if configured.recovery.strategy not in cls.recovery_strategies:
            raise PlanError(f'{prefix}.recovery.strategy: unsupported strategy {configured.recovery.strategy!r}')
        if configured.recovery.strategy == 'retry' and cls.recover is Stage.recover:
            raise PlanError(f'{prefix}.recovery.strategy: retry requires an implemented recover method')
        try:
            params = cls.params_model.model_validate(configured.params)
        except ValidationError as error:
            raise PlanError(f'{prefix}.params: {_validation_details(error)}') from error
        overrides = configured.timeouts.model_dump(exclude_none=True)
        timeouts = Timeouts.model_validate({**cfg.timeouts.model_dump(), **overrides})
        plan.append(PlannedStage(index, cls, params, timeouts, configured.recovery))
        seen.add(name)
        previous_layer = layer_order[configured.layer]
    validate_bindings(cfg.bindings, plan)
    for selector, bindings in cfg.test_bindings.items():
        try:
            validate_bindings(bindings, plan)
        except PlanError as error:
            raise PlanError(f'test_bindings.{selector}: {error}') from error
    return plan
