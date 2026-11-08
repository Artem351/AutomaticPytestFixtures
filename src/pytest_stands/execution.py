from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import (
    CleanupError,
    CleanupTimeout,
    GroupTimeout,
    InfrastructureError,
    OperationError,
    OperationTimeout,
    RecoveryError,
    RestoreError,
    StepTimeout,
)

if TYPE_CHECKING:
    from .config.models import RecoveryConfig, Timeouts


class OperationContext:
    def __init__(self, deadline: float):
        self.deadline = deadline
        self._cancelled = threading.Event()

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        if self._cancelled.is_set():
            raise OperationTimeout("Operation was cancelled")
        if self.remaining <= 0:
            raise OperationTimeout("Operation deadline expired")

    def cancel(self) -> None:
        self._cancelled.set()

    def wait(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Wait duration must be finite and nonnegative")
        self.check()
        self._cancelled.wait(min(seconds, self.remaining))
        self.check()


@dataclass(frozen=True)
class _Deadline:
    when: float
    error: type[OperationTimeout]
    message: str

    def exception(self) -> OperationTimeout:
        return self.error(self.message)


@dataclass
class _Outcome:
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    finished: float = 0.0


class StageRunner:
    cancellation_grace = 0.25

    def __init__(
        self,
        stage: Any,
        timeouts: Timeouts,
        recovery: RecoveryConfig,
        log: Any,
        group_deadline: float | None = None,
    ):
        self.stage = stage
        self.timeouts = timeouts
        self.recovery = recovery
        self.log = log
        self.group_deadline = group_deadline
        self._poisoned = False
        self._lock = threading.Lock()
        self._context: OperationContext | None = None

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    @property
    def usable(self) -> bool:
        return not self._poisoned

    def call(
        self,
        method_name: str,
        *args: Any,
        phase: str = "command",
        recover: bool = True,
        **kwargs: Any,
    ) -> Any:
        if phase not in {"setup", "command", "restore", "cleanup"}:
            raise ValueError(f"Unknown execution phase: {phase}")
        if not self._lock.acquire(blocking=False):
            raise InfrastructureError("Stage runner already has an active call")
        try:
            if self.poisoned:
                raise InfrastructureError("Stage runner is poisoned and cannot be reused")
            try:
                if phase in {"restore", "cleanup"}:
                    deadline = _Deadline(
                        time.monotonic() + self.timeouts.cleanup,
                        CleanupTimeout,
                        f"{phase.capitalize()} exceeded its cleanup budget",
                    )
                    return self._invoke(method_name, args, kwargs, deadline)
                return self._run_attempts(method_name, args, kwargs, recover)
            except Exception as error:
                if phase == "restore" and not isinstance(error, (RestoreError, CleanupTimeout)):
                    raise RestoreError(f"Restore failed: {error}") from error
                if phase == "cleanup" and not isinstance(error, (CleanupError, CleanupTimeout)):
                    raise CleanupError(f"Cleanup failed: {error}") from error
                raise
        finally:
            self._lock.release()

    def _total_deadline(self) -> _Deadline:
        deadlines = []
        if self.group_deadline is not None:
            deadlines.append(_Deadline(self.group_deadline, GroupTimeout, "Group budget expired"))
        deadlines.append(
            _Deadline(time.monotonic() + self.timeouts.step, StepTimeout, "Step budget expired")
        )
        return min(deadlines, key=lambda deadline: deadline.when)

    def _attempt_deadline(self, total: _Deadline) -> _Deadline:
        command = _Deadline(
            time.monotonic() + self.timeouts.command,
            OperationTimeout,
            "Command attempt exceeded its deadline",
        )
        return min((total, command), key=lambda deadline: deadline.when)

    def _run_attempts(
        self, method_name: str, args: tuple[Any, ...], kwargs: dict[str, Any], recover: bool
    ) -> Any:
        total = self._total_deadline()
        retry = (
            recover
            and self.recovery.strategy == "retry"
            and "retry" in self.stage.recovery_strategies
        )
        attempts = self.recovery.attempts if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                return self._invoke(method_name, args, kwargs, self._attempt_deadline(total))
            except OperationError as error:
                if self.poisoned or not retry or isinstance(error, (StepTimeout, GroupTimeout)):
                    raise
                if attempt == attempts:
                    raise RecoveryError(
                        f"{method_name} failed after {attempts} total attempts: {error}"
                    ) from error
                self.log.event(
                    "operation_retry",
                    method=method_name,
                    attempt=attempt,
                    attempts=attempts,
                    error=str(error),
                )
                try:
                    self._invoke("recover", (error,), {}, self._attempt_deadline(total))
                except (StepTimeout, GroupTimeout):
                    raise
                except Exception as recovery_error:
                    raise RecoveryError(
                        f"Recovery after {method_name} attempt {attempt} failed: {recovery_error}"
                    ) from recovery_error
                self._retry_delay(total)
        raise InfrastructureError("Recovery attempts must be positive")

    def _retry_delay(self, deadline: _Deadline) -> None:
        context = OperationContext(deadline.when)
        try:
            context.wait(self.recovery.delay)
        except OperationTimeout as cause:
            error = deadline.exception()
            self._cancel_and_close(None, context, error)
            raise error from cause
        except BaseException as error:
            self._cancel_and_close(None, context, error)
            raise

    def _invoke(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        deadline: _Deadline,
    ) -> Any:
        context = OperationContext(deadline.when)
        self._context = context
        outcome = _Outcome()

        def run() -> None:
            try:
                self.stage.context = context
                context.check()
                result = getattr(self.stage, method_name)(*args, **kwargs)
                context.check()
                outcome.result = result
            except BaseException as error:
                outcome.error = error
            finally:
                outcome.finished = time.monotonic()
                outcome.done.set()

        if context.remaining <= 0:
            error = deadline.exception()
            self._cancel_and_close(None, context, error)
            raise error
        worker = threading.Thread(target=run, name=f"stage-{method_name}", daemon=True)
        stopping = False
        try:
            worker.start()
            completed = outcome.done.wait(context.remaining)
            if not completed or outcome.finished > deadline.when:
                error = deadline.exception()
                stopping = True
                self._cancel_and_close(worker, context, error)
                if outcome.error is not None:
                    raise error from outcome.error
                raise error
            worker.join(timeout=max(0.0, deadline.when - time.monotonic()))
            if worker.is_alive():
                error = deadline.exception()
                stopping = True
                self._cancel_and_close(worker, context, error)
                raise error
            if outcome.error is not None:
                if isinstance(outcome.error, OperationTimeout):
                    stopping = True
                    self._cancel_and_close(worker, context, outcome.error)
                if isinstance(outcome.error, InfrastructureError):
                    raise outcome.error
                if not isinstance(outcome.error, Exception):
                    raise outcome.error
                raise InfrastructureError(f"{method_name} failed: {outcome.error}") from outcome.error
            return outcome.result
        except BaseException as error:
            if not stopping and (worker.is_alive() or not isinstance(error, Exception)):
                self._cancel_and_close(worker, context, error)
            raise

    def _cancel_and_close(
        self,
        worker: threading.Thread | None,
        context: OperationContext,
        cause: BaseException,
    ) -> None:
        context.cancel()
        if self._context is not None:
            self._context.cancel()
        closed = _Outcome()

        def close() -> None:
            try:
                self.stage.close()
            except BaseException as error:
                closed.error = error
            finally:
                closed.done.set()

        closer = threading.Thread(target=close, name="stage-close", daemon=True)
        try:
            closer.start()
        except BaseException as error:
            self._poisoned = True
            if isinstance(error, Exception):
                raise InfrastructureError("Could not supervise close; stage runner is poisoned") from error
            raise
        stop_deadline = time.monotonic() + min(self.cancellation_grace, self.timeouts.cleanup)
        interrupted: BaseException | None = None
        try:
            if worker is not None:
                worker.join(timeout=max(0.0, stop_deadline - time.monotonic()))
            closer.join(timeout=max(0.0, stop_deadline - time.monotonic()))
        except BaseException as error:
            interrupted = error
        active = (worker is not None and worker.is_alive()) or closer.is_alive()
        if active or closed.error is not None:
            self._poisoned = True
        if interrupted is not None:
            self._poisoned = True
            raise interrupted
        if self.poisoned:
            if not isinstance(cause, Exception):
                raise cause
            if closed.error is not None and not isinstance(closed.error, Exception):
                raise closed.error
            detail = "cancellation or close did not finish" if active else f"close failed: {closed.error}"
            error = InfrastructureError(f"{cause}; {detail}; stage runner is poisoned")
            raise error from (closed.error if closed.error is not None else cause)
