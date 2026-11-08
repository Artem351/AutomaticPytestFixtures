from pydantic import StrictBool

from ...config.models import Layer
from ...errors import OperationError
from ...interfaces import PowerControl
from ..base import MockParams, MockStage
from ..registry import register_stage


class MockPowerParams(MockParams):
    initial_on: StrictBool = True


@register_stage('mock_power')
class MockPower(MockStage, PowerControl):
    layer = Layer.hardware
    provides = ('power',)
    params_model = MockPowerParams

    def __init__(self, params: MockPowerParams, log):
        super().__init__(params, log)
        self._on = None

    def setup(self) -> None:
        self._setup()
        self._on = self.params.initial_on
        self._event('power_setup', on=self._on)

    def is_on(self) -> bool:
        self._command()
        return self._on

    def set_on(self, on: bool) -> None:
        self._command()
        if not isinstance(on, bool):
            raise OperationError(f'component {self.name}: on must be a boolean')
        self._on = on
        self._event('power_changed', on=on)

    def restore(self) -> None:
        self._restore()
        self._on = self.params.initial_on
        self._event('power_restored', on=self._on)

    def cleanup(self) -> None:
        try:
            self._cleanup()
        finally:
            if not self._active:
                self._on = None
                self._event('power_released')

    def recover(self, error: Exception) -> None:
        super().recover(error)
        self._on = self.params.initial_on if self._active else None
        self._event('power_recovered', on=self._on)


@register_stage('mock_aux_power')
class MockAuxPower(MockPower):
    pass
