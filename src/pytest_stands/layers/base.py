from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import Field, StrictBool, StrictInt

from ..config.models import Layer, NonnegativeNumber, StrictModel
from ..errors import CleanupError, InfrastructureError, OperationError, RestoreError

if TYPE_CHECKING:
    from ..execution import OperationContext


class StageParams(StrictModel):
    pass


class Stage(ABC):
    name: ClassVar[str] = ''
    layer: ClassVar[Layer]
    platforms: ClassVar[tuple[str, ...]] = ()
    provides: ClassVar[tuple[str, ...]] = ()
    requires: ClassVar[tuple[str, ...]] = ()
    recovery_strategies: ClassVar[tuple[str, ...]] = ('none',)
    params_model: ClassVar[type[StageParams]] = StageParams

    def __init__(self, params: StageParams, log):
        self.params = params
        self.log = log
        self._context: OperationContext | None = None

    @property
    def context(self) -> 'OperationContext':
        if self._context is None:
            raise InfrastructureError(f'component {self.name}: no active operation context')
        return self._context

    @context.setter
    def context(self, context: 'OperationContext') -> None:
        self._context = context

    @abstractmethod
    def setup(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def restore(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def cleanup(self) -> None:
        raise NotImplementedError

    def recover(self, error: Exception) -> None:
        raise OperationError(f'component {self.name} does not support recovery') from error

    def close(self) -> None:
        context = self._context
        if context is not None:
            context.cancel()


class MockParams(StageParams):
    setup_delay: NonnegativeNumber = 0.0
    command_delay: NonnegativeNumber = 0.0
    restore_delay: NonnegativeNumber = 0.0
    cleanup_delay: NonnegativeNumber = 0.0
    fail_setup_attempts: Annotated[StrictInt, Field(ge=0)] = 0
    fail_restore: StrictBool = False
    fail_cleanup: StrictBool = False


class MockStage(Stage):
    platforms = ('mock',)
    recovery_strategies = ('none', 'retry')
    params_model = MockParams

    def __init__(self, params: MockParams, log):
        super().__init__(params, log)
        self._setup_attempts = 0
        self._active = False

    def _event(self, event, **fields):
        self.log.event(event, component=self.name, **fields)

    def _setup(self):
        self.context.wait(self.params.setup_delay)
        self._setup_attempts += 1
        self._event('setup_attempt', attempt=self._setup_attempts)
        if self._setup_attempts <= self.params.fail_setup_attempts:
            raise OperationError(f'component {self.name}: configured setup failure on attempt {self._setup_attempts}')
        self._active = True

    def _command(self):
        self.context.wait(self.params.command_delay)
        if not self._active:
            raise OperationError(f'component {self.name}: component is not set up')

    def _restore(self):
        self.context.wait(self.params.restore_delay)
        if self.params.fail_restore:
            raise RestoreError(f'component {self.name}: configured restore failure')
        if not self._active:
            raise RestoreError(f'component {self.name}: cannot restore an inactive component')

    def _cleanup(self):
        self.context.wait(self.params.cleanup_delay)
        self._active = False
        if self.params.fail_cleanup:
            raise CleanupError(f'component {self.name}: configured cleanup failure')

    def recover(self, error: Exception) -> None:
        self.context.check()
        self._event('recovery', error=str(error), setup_attempts=self._setup_attempts)
