"""Pydantic-модели YAML-конфига. Схема пока не утверждена, это рабочий вариант."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Layer(str, Enum):
    """Слой подготовки. Порядок в конфиге: hardware, затем linux."""
    hardware = "hardware"
    linux = "linux"


LAYER_ORDER = {Layer.hardware: 0, Layer.linux: 1}


class _Strict(BaseModel):
    # Лишние поля в YAML запрещены: опечатка в имени параметра не должна молча игнорироваться
    model_config = ConfigDict(extra="forbid")


class DutConfig(_Strict):
    type: str


class StageConfig(_Strict):
    layer: Layer
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class Timeouts(_Strict):
    """Секунды. Значения по умолчанию из СРС. Пока только хранятся, принудительно не применяются."""
    command: float = 30.0
    step: float = 300.0
    cleanup: float = 60.0
    group: Optional[float] = None


class Config(_Strict):
    dut: DutConfig
    stages: list[StageConfig] = Field(min_length=1)
    tests: list[str] = Field(min_length=1)
    timeouts: Timeouts = Field(default_factory=Timeouts)

    @model_validator(mode="after")
    def _layers_in_order(self) -> "Config":
        last = -1
        for st in self.stages:
            rank = LAYER_ORDER[st.layer]
            if rank < last:
                raise ValueError(
                    f"этап '{st.type}' слоя {st.layer.value} стоит после этапа более позднего слоя: "
                    "сначала hardware, потом linux"
                )
            last = rank
        return self
