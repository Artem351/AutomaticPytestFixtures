from __future__ import annotations

from pydantic import Field

from ...config.models import Layer
from ..base import Stage, StageParams
from ..registry import register_stage


class MockPowerParams(StageParams):
    timeout: float = Field(30.0, gt=0)
    count: int = Field(1, ge=1)


@register_stage("mock_power")
class MockPower(Stage):
    """Мок включения питания стенда."""
    layer = Layer.hardware
    params_model = MockPowerParams

    def setup(self) -> None:
        self.log.event("mock_power.on", timeout=self.params.timeout, count=self.params.count)

    def teardown(self) -> None:
        self.log.event("mock_power.off")
