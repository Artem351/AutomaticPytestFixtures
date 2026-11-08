import threading
import time
from types import SimpleNamespace

import pytest

from pytest_stands.errors import (
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
from pytest_stands.execution import OperationContext, StageRunner


class RecordingLog:
    def __init__(self):
        self.events = []

    def event(self, event_name, **fields):
        self.events.append((event_name, fields))


class RecordingStage:
    recovery_strategies = ("retry",)

    def __init__(self, action):
        self.action = action
        self.context = None
        self.calls = []
        self.active = 0
        self.peak_active = 0
        self.closed = 0
        self.recovery_action = None
        self.close_action = None
        self.restore_action = None
        self.cleanup_action = None
        self.exited = threading.Event()

    def operation(self):
        self.calls.append("operation")
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            return self.action(self)
        finally:
            self.active -= 1
            self.exited.set()

    def setup(self):
        return self.operation()

    def recover(self, error):
        assert self.active == 0
        self.calls.append("recover")
        if self.recovery_action is not None:
            self.recovery_action(self, error)

    def restore(self):
        assert self.active == 0
        self.calls.append("restore")
        if self.restore_action is not None:
            self.restore_action(self)

    def cleanup(self):
        assert self.active == 0
        self.calls.append("cleanup")
        if self.cleanup_action is not None:
            self.cleanup_action(self)

    def close(self):
        self.closed += 1
        if self.context is not None:
            self.context.cancel()
        if self.close_action is not None:
            self.close_action(self)


def make_runner(stage, *, command=0.04, step=0.6, cleanup=0.2, attempts=1, delay=0, group=None):
    return StageRunner(
        stage,
        SimpleNamespace(command=command, step=step, cleanup=cleanup),
        SimpleNamespace(strategy="retry" if attempts > 1 else "none", attempts=attempts, delay=delay),
        RecordingLog(),
        group_deadline=group,
    )


def raise_error(error):
    def fail(stage):
        raise error

    return fail


def test_context_cancel_interrupts_wait_without_waiting_for_deadline():
    context = OperationContext(time.monotonic() + 60)
    entered = threading.Event()
    finished = threading.Event()
    failures = []

    def wait():
        entered.set()
        try:
            context.wait(60)
        except OperationTimeout as error:
            failures.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=wait, daemon=True)
    worker.start()
    assert entered.wait(1)
    context.cancel()
    assert finished.wait(1)
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert len(failures) == 1


@pytest.mark.parametrize("phase,method", [("command", "operation"), ("setup", "setup")])
def test_cooperative_deadline_closes_and_stops_worker(phase, method):
    stage = RecordingStage(lambda stage: stage.context.wait(60))
    runner = make_runner(stage)
    started = time.monotonic()
    with pytest.raises(OperationTimeout):
        runner.call(method, phase=phase)
    assert time.monotonic() - started < 1
    assert stage.closed == 1
    assert stage.exited.is_set()
    assert stage.active == 0
    assert runner.usable


@pytest.mark.parametrize("phase,method", [("command", "operation"), ("setup", "setup")])
def test_timeout_retry_waits_for_worker_and_preserves_success(phase, method):
    expected = object()

    def action(stage):
        if stage.calls.count("operation") == 1:
            stage.context.wait(60)
        return expected

    stage = RecordingStage(action)
    runner = make_runner(stage, attempts=2)
    assert runner.call(method, phase=phase) is expected
    assert stage.calls == ["operation", "recover", "operation"]
    assert stage.closed == 1
    assert stage.peak_active == 1


def test_retry_delay_consumes_total_step_budget():
    stage = RecordingStage(raise_error(OperationError("not ready")))
    runner = make_runner(stage, command=1, step=0.04, attempts=4, delay=1)
    with pytest.raises(StepTimeout):
        runner.call("operation")
    assert stage.calls == ["operation", "recover"]
    assert stage.closed == 1
    assert stage.active == 0


def test_recovery_consumes_total_step_budget():
    stage = RecordingStage(raise_error(OperationError("not ready")))
    stage.recovery_action = lambda stage, error: stage.context.wait(60)
    runner = make_runner(stage, command=1, step=0.04, attempts=4)
    with pytest.raises(StepTimeout):
        runner.call("operation")
    assert stage.calls == ["operation", "recover"]
    assert stage.closed == 1
    assert runner.usable


def test_exhaustion_uses_total_attempts_and_preserves_last_error():
    failure = OperationError("device remains unavailable")
    stage = RecordingStage(raise_error(failure))
    runner = make_runner(stage, attempts=3)
    with pytest.raises(RecoveryError, match="3 total attempts") as caught:
        runner.call("operation")
    assert caught.value.__cause__ is failure
    assert stage.calls == ["operation", "recover", "operation", "recover", "operation"]
    assert stage.peak_active == 1


@pytest.mark.parametrize("supported,recover", [(False, True), (True, False)])
def test_retry_requires_supported_strategy_and_explicit_permission(supported, recover):
    failure = OperationError("unavailable")
    stage = RecordingStage(raise_error(failure))
    if not supported:
        stage.recovery_strategies = ()
    runner = make_runner(stage, attempts=3)
    with pytest.raises(OperationError) as caught:
        runner.call("operation", recover=recover)
    assert caught.value is failure
    assert stage.calls == ["operation"]


def test_unexpected_component_exception_keeps_original_cause_and_is_not_retried():
    failure = ValueError("invalid response")
    stage = RecordingStage(raise_error(failure))
    runner = make_runner(stage, attempts=3)
    with pytest.raises(InfrastructureError) as caught:
        runner.call("operation")
    assert caught.value.__cause__ is failure
    assert stage.calls == ["operation"]


def test_recovery_failure_keeps_original_cause():
    failure = ValueError("recovery failed")
    stage = RecordingStage(raise_error(OperationError("unavailable")))

    def recover(stage, error):
        raise failure

    stage.recovery_action = recover
    runner = make_runner(stage, attempts=3)
    with pytest.raises(RecoveryError) as caught:
        runner.call("operation")
    assert caught.value.__cause__.__cause__ is failure
    assert stage.calls == ["operation", "recover"]


@pytest.mark.parametrize("phase,error_type", [("restore", RestoreError), ("cleanup", CleanupError)])
def test_phase_errors_are_explicit_and_preserve_original_failure(phase, error_type):
    failure = OperationError("lifecycle failure")
    stage = RecordingStage(lambda stage: None)
    setattr(stage, f"{phase}_action", raise_error(failure))
    runner = make_runner(stage, attempts=3)
    with pytest.raises(error_type) as caught:
        runner.call(phase, phase=phase)
    assert caught.value.__cause__ is failure
    assert stage.calls == [phase]


@pytest.mark.parametrize("phase", ["restore", "cleanup"])
def test_lifecycle_uses_separate_budget_after_expired_group(phase):
    stage = RecordingStage(lambda stage: None)
    runner = make_runner(stage, group=time.monotonic() - 1)
    with pytest.raises(GroupTimeout):
        runner.call("operation")
    assert stage.calls == []
    runner.call(phase, phase=phase)
    assert stage.calls == [phase]
    assert runner.usable


@pytest.mark.parametrize("phase", ["restore", "cleanup"])
def test_lifecycle_deadline_is_typed_and_never_retried(phase):
    stage = RecordingStage(lambda stage: None)
    setattr(stage, f"{phase}_action", lambda stage: stage.context.wait(60))
    runner = make_runner(stage, cleanup=0.04, attempts=3)
    with pytest.raises(CleanupTimeout):
        runner.call(phase, phase=phase)
    assert stage.calls == [phase]
    assert stage.closed == 1
    assert runner.usable


def test_noncooperative_component_poison_prevents_retry_and_cleanup_overlap():
    release = threading.Event()
    stage = RecordingStage(lambda stage: release.wait(60))
    runner = make_runner(stage, command=0.03, cleanup=0.04, attempts=3)
    try:
        with pytest.raises(InfrastructureError, match="poisoned"):
            runner.call("operation")
        assert runner.poisoned
        assert not runner.usable
        assert stage.calls == ["operation"]
        with pytest.raises(InfrastructureError, match="poisoned"):
            runner.call("cleanup", phase="cleanup")
        assert stage.calls == ["operation"]
    finally:
        release.set()
        assert stage.exited.wait(1)


def test_hanging_close_is_bounded_and_poisons_runner():
    release = threading.Event()
    closed = threading.Event()
    stage = RecordingStage(lambda stage: stage.context.wait(60))

    def close(stage):
        try:
            release.wait(60)
        finally:
            closed.set()

    stage.close_action = close
    runner = make_runner(stage, command=0.03, cleanup=0.04)
    started = time.monotonic()
    try:
        with pytest.raises(InfrastructureError, match="poisoned"):
            runner.call("operation")
        assert time.monotonic() - started < 1
        assert runner.poisoned
        assert stage.exited.is_set()
    finally:
        release.set()
        assert closed.wait(1)


def test_late_success_after_cancellation_is_still_timeout():
    expected = object()

    def action(stage):
        try:
            stage.context.wait(60)
        except OperationTimeout:
            return expected

    stage = RecordingStage(action)
    runner = make_runner(stage)
    with pytest.raises(OperationTimeout):
        runner.call("operation")
    assert stage.closed == 1
    assert stage.active == 0


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_component_base_exception_reaches_parent_with_safe_cleanup(exception):
    failure = exception("interrupted")
    stage = RecordingStage(raise_error(failure))
    runner = make_runner(stage)
    with pytest.raises(exception) as caught:
        runner.call("operation")
    assert caught.value is failure
    assert stage.active == 0
    runner.call("cleanup", phase="cleanup")
    assert stage.calls == ["operation", "cleanup"]


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_parent_interruption_cancels_worker_before_cleanup(monkeypatch, exception):
    entered = threading.Event()

    def action(stage):
        entered.set()
        stage.context.wait(60)

    stage = RecordingStage(action)
    runner = make_runner(stage, command=1)
    failure = exception("parent interrupted")
    original_wait = threading.Event.wait
    main_thread = threading.current_thread()
    delivered = False

    def interrupt_wait(event, timeout=None):
        nonlocal delivered
        if threading.current_thread() is main_thread and timeout is not None and not delivered:
            assert original_wait(entered, 1)
            delivered = True
            raise failure
        return original_wait(event, timeout)

    monkeypatch.setattr(threading.Event, "wait", interrupt_wait)
    with pytest.raises(exception) as caught:
        runner.call("operation")
    assert caught.value is failure
    assert delivered
    assert stage.closed == 1
    assert stage.active == 0
    assert runner.usable
    runner.call("cleanup", phase="cleanup")
    assert stage.calls == ["operation", "cleanup"]


@pytest.mark.parametrize("error_type", [InfrastructureError, OperationError, RestoreError])
def test_component_infrastructure_error_keeps_its_exact_type_and_identity(error_type):
    failure = error_type("component diagnostic")
    stage = RecordingStage(raise_error(failure))
    runner = make_runner(stage)
    with pytest.raises(error_type) as caught:
        runner.call("operation")
    assert caught.value is failure
    assert type(caught.value) is error_type
    assert stage.calls == ["operation"]
