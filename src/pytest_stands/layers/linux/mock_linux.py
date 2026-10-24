from __future__ import annotations

from pydantic import Field

from ...config.models import Layer
from ..base import Stage, StageParams
from ..registry import register_stage


class MockLinuxParams(StageParams):
    file_name: str
    timeout: float = Field(30.0, gt=0)


@register_stage("mock_linux")
class MockLinux(Stage):
    """Мок развёртывания Linux из образа. Требует включённого питания."""
    layer = Layer.linux
    params_model = MockLinuxParams
    requires = ("mock_power",)

    def setup(self) -> None:
        self.log.event("mock_linux.deploy", file_name=self.params.file_name, timeout=self.params.timeout)

    def teardown(self) -> None:
        self.log.event("mock_linux.cleanup", file_name=self.params.file_name)


class MockServiceParams(StageParams):
    name: str
    fail_setup: bool = False      # для тестов: имитация окончательного сбоя инфраструктуры
    fail_teardown: bool = False


@register_stage("mock_service")
class MockService(Stage):
    """Мок сервиса на Linux. Нужен, чтобы проверять комбинации этапов и сбои."""
    layer = Layer.linux
    params_model = MockServiceParams
    requires = ("mock_linux",)

    def setup(self) -> None:
        self.log.event("mock_service.start", name=self.params.name)
        if self.params.fail_setup:
            raise RuntimeError(f"mock_service '{self.params.name}': сбой при запуске")

    def teardown(self) -> None:
        self.log.event("mock_service.stop", name=self.params.name)
        if self.params.fail_teardown:
            raise RuntimeError(f"mock_service '{self.params.name}': сбой при очистке")
