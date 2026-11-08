from pydantic import StrictBool

from ...config.models import Layer, Name
from ...interfaces import ServiceControl, SystemInfo
from ..base import MockParams, MockStage
from ..registry import register_stage


class MockLinuxParams(MockParams):
    file_name: Name


@register_stage('mock_linux')
class MockLinux(MockStage, SystemInfo):
    layer = Layer.linux
    provides = ('system',)
    requires = ('mock_power',)
    params_model = MockLinuxParams

    def __init__(self, params: MockLinuxParams, log):
        super().__init__(params, log)
        self._ready = False

    def setup(self) -> None:
        self._setup()
        self._ready = True
        self._event('system_ready', file_name=self.params.file_name)

    def is_ready(self) -> bool:
        self._command()
        return self._ready

    def restore(self) -> None:
        self._restore()
        self._ready = True
        self._event('system_restored', ready=True)

    def cleanup(self) -> None:
        try:
            self._cleanup()
        finally:
            if not self._active:
                self._ready = False
                self._event('system_released')

    def recover(self, error: Exception) -> None:
        super().recover(error)
        self._ready = self._active
        self._event('system_recovered', ready=self._ready)


class MockServiceParams(MockParams):
    name: Name = 'service'
    initial_running: StrictBool = True


@register_stage('mock_service')
class MockService(MockStage, ServiceControl):
    layer = Layer.linux
    provides = ('service',)
    requires = ('mock_linux',)
    params_model = MockServiceParams

    def __init__(self, params: MockServiceParams, log):
        super().__init__(params, log)
        self._running = None

    def setup(self) -> None:
        self._setup()
        self._running = self.params.initial_running
        self._event('service_setup', service=self.params.name, running=self._running)

    def is_running(self) -> bool:
        self._command()
        return self._running

    def start(self) -> None:
        self._command()
        self._running = True
        self._event('service_started', service=self.params.name)

    def stop(self) -> None:
        self._command()
        self._running = False
        self._event('service_stopped', service=self.params.name)

    def restore(self) -> None:
        self._restore()
        self._running = self.params.initial_running
        self._event('service_restored', service=self.params.name, running=self._running)

    def cleanup(self) -> None:
        try:
            self._cleanup()
        finally:
            if not self._active:
                self._running = None
                self._event('service_released', service=self.params.name)

    def recover(self, error: Exception) -> None:
        super().recover(error)
        self._running = self.params.initial_running if self._active else None
        self._event('service_recovered', service=self.params.name, running=self._running)
