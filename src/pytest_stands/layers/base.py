"""Этап подготовки: объект с setup и teardown и моделью параметров."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ..config.models import Layer
from ..runlog import RunLog


class StageParams(BaseModel):
    """Базовая модель параметров. Фикстура получает ровно те параметры, которые ждёт."""
    model_config = ConfigDict(extra="forbid")


class Stage(ABC):
    name: ClassVar[str]                      # заполняет @register_stage
    layer: ClassVar[Layer]
    params_model: ClassVar[type[StageParams]] = StageParams
    requires: ClassVar[tuple[str, ...]] = ()  # типы этапов, которые должны стоять раньше в конфиге

    def __init__(self, params: StageParams, log: RunLog):
        self.params = params
        self.log = log

    @abstractmethod
    def setup(self) -> None: ...

    @abstractmethod
    def teardown(self) -> None:
        """Откатить то, что изменил этот этап. Может быть пустым, но метод обязателен."""
