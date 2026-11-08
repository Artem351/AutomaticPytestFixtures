import math
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator


def positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('must be a positive finite number, not a boolean or string')
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError('must be a positive finite number') from None
    if not math.isfinite(result) or result <= 0:
        raise ValueError('must be a positive finite number')
    return result


def nonnegative_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('must be a nonnegative finite number, not a boolean or string')
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError('must be a nonnegative finite number') from None
    if not math.isfinite(result) or result < 0:
        raise ValueError('must be a nonnegative finite number')
    return result


def nonempty_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('must be a nonempty string')
    return value


PositiveNumber = Annotated[float, BeforeValidator(positive_number)]
NonnegativeNumber = Annotated[float, BeforeValidator(nonnegative_number)]
Name = Annotated[StrictStr, BeforeValidator(nonempty_name)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Layer(str, Enum):
    hardware = 'hardware'
    linux = 'linux'


class Timeouts(StrictModel):
    command: PositiveNumber = 30.0
    step: PositiveNumber = 300.0
    cleanup: PositiveNumber = 60.0
    group: PositiveNumber | None = None


class StageTimeouts(StrictModel):
    command: PositiveNumber | None = None
    step: PositiveNumber | None = None
    cleanup: PositiveNumber | None = None


class RecoveryConfig(StrictModel):
    strategy: Literal['none', 'retry'] = 'none'
    attempts: Annotated[StrictInt, Field(ge=1)] = 1
    delay: NonnegativeNumber = 0.0

    @model_validator(mode='after')
    def validate_unused_options(self):
        if self.strategy == 'none' and (self.attempts != 1 or self.delay != 0):
            raise ValueError('recovery strategy none requires attempts=1 and delay=0')
        return self


class DutConfig(StrictModel):
    type: Name


class StageConfig(StrictModel):
    layer: Layer
    type: Name
    params: dict[str, object] = Field(default_factory=dict)
    timeouts: StageTimeouts = Field(default_factory=StageTimeouts)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)


class Config(StrictModel):
    dut: DutConfig
    stages: Annotated[list[StageConfig], Field(min_length=1)]
    tests: Annotated[list[Name], Field(min_length=1)]
    bindings: dict[Name, Name] = Field(default_factory=dict)
    test_bindings: dict[Name, dict[Name, Name]] = Field(default_factory=dict)
    timeouts: Timeouts = Field(default_factory=Timeouts)

    @field_validator('stages')
    @classmethod
    def unique_stages(cls, stages):
        names = set()
        for stage in stages:
            if stage.type in names:
                raise ValueError(f'duplicate stage type {stage.type!r}')
            names.add(stage.type)
        return stages
