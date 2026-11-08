from __future__ import annotations

from pathlib import Path
from time import monotonic

import pytest
import yaml

from .config import ConfigError, load_config
from .config.models import Config
from .errors import CleanupError, GroupTimeout, InfrastructureError, OperationError, RestoreError
from .execution import StageRunner
from .layers.registry import get_interface, interface_names, load_builtin_and_plugins
from .resolver import PlanError, PlannedStage, build_plan, validate_bindings
from .runlog import RunLog


_RUNTIME = pytest.StashKey["StandsRuntime"]()


def pytest_addoption(parser: pytest.Parser) -> None:
    options = parser.getgroup("stands", "pytest-stands")
    options.addoption("--stands-config", help="YAML-конфиг одной группы тестов")
    options.addoption("--stands-artifacts", default="artifacts", help="каталог журналов прогонов")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("stands_config")
    if path is None:
        return
    try:
        load_builtin_and_plugins()
        cfg = load_config(path)
        plan = build_plan(cfg)
        base = config.inipath.parent if config.inipath is not None else config.invocation_params.dir
        selectors = [normalize_selector(value, config.rootpath, base) for value in cfg.tests]
        overrides = {}
        for key, value in cfg.test_bindings.items():
            selector = normalize_selector(key, config.rootpath, base)
            if selector in overrides:
                raise ValueError(f"повторяющийся селектор test_bindings: {selector}")
            overrides[selector] = value
    except (ConfigError, PlanError, ValueError) as error:
        raise pytest.UsageError(f"pytest-stands: {error}") from error
    files = dict.fromkeys(selector.split("::", 1)[0] for selector in selectors)
    config.args[:] = [str(config.rootpath / file) for file in files]
    try:
        log = RunLog.create(config.getoption("stands_artifacts"))
        applied = yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
        (log.directory / "config.applied.yaml").write_text(applied, encoding="utf-8")
    except OSError as error:
        raise pytest.UsageError(f"pytest-stands: не удалось сохранить конфиг и журнал: {error}") from error
    runtime = StandsRuntime(cfg, plan, log, selectors, overrides)
    config.stash[_RUNTIME] = runtime
    config.pluginmanager.register(runtime, "stands-runtime")
    config.pluginmanager.register(runtime.fixtures, "stands-interfaces")


def normalize_selector(value: str, root: Path, base: Path) -> str:
    file, separator, test = value.replace("\\", "/").partition("::")
    path = Path(file)
    if not path.is_absolute():
        path = base / path
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"путь теста должен находиться внутри {root}: {value}") from error
    return relative + (separator + test if separator else "")


def matches(nodeid: str, selector: str) -> bool:
    return nodeid == selector or nodeid.startswith(selector + "[") or nodeid.startswith(selector + "::")


class StandsRuntime:
    def __init__(self, cfg: Config, plan: list[PlannedStage], log: RunLog,
                 selectors: list[str], overrides: dict[str, dict[str, str]]):
        self.cfg = cfg
        self.plan = plan
        self.log = log
        self.selectors = selectors
        self.overrides = overrides
        self.stop_reason: str | None = None
        self.group_deadline: float | None = None
        self.runners: dict[str, StageRunner] = {}
        self.started: list[tuple[str, StageRunner]] = []
        self.item_bindings: dict[str, dict[str, str]] = {}
        namespace = {name: self._interface_fixture(name) for name in interface_names()}
        self.fixtures = type("StandInterfaces", (), namespace)()

    def _interface_fixture(self, name: str):
        def interface_fixture(holder, request: pytest.FixtureRequest):
            binding = self.item_bindings[request.node.nodeid].get(name)
            if binding is None:
                raise OperationError(f"интерфейс '{name}' не имеет явной привязки для {request.node.nodeid}")
            interface = get_interface(name)
            if interface is None:
                raise OperationError(f"интерфейс '{name}' не зарегистрирован")
            runner = self.runners[binding]
            methods = {method: self._proxy_method(runner, method) for method in interface.__abstractmethods__}
            proxy = type(f"{interface.__name__}Proxy", (interface,), methods)
            return proxy()

        return pytest.fixture(name=name)(interface_fixture)

    @staticmethod
    def _proxy_method(runner: StageRunner, method: str):
        def invoke(proxy, *args, **kwargs):
            return runner.call(method, *args, **kwargs)

        invoke.__name__ = method
        return invoke

    @pytest.fixture(scope="session", autouse=True)
    def _stands_group(self):
        if self.cfg.timeouts.group is not None:
            self.group_deadline = monotonic() + self.cfg.timeouts.group
        preparation_error: BaseException | None = None
        try:
            for planned in self.plan:
                name = planned.cls.name
                try:
                    stage = planned.cls(planned.params, self.log)
                except Exception as error:
                    raise OperationError(f"не удалось создать компонент '{name}': {error}") from error
                runner = StageRunner(stage, planned.timeouts, planned.recovery, self.log,
                                     group_deadline=self.group_deadline)
                self.runners[name] = runner
                self.started.append((name, runner))
                self.log.event("stage.setup.start", stage=name)
                runner.call("setup", phase="setup")
                self.log.event("stage.setup.done", stage=name)
            yield
        except BaseException as error:
            preparation_error = error
            raise
        finally:
            errors = self._finish_stages("cleanup")
            if errors and preparation_error is None:
                self._raise_lifecycle_error("cleanup", errors)

    @pytest.fixture(autouse=True)
    def _stands_test(self, _stands_group):
        try:
            yield
        finally:
            expired = self.group_deadline is not None and monotonic() >= self.group_deadline
            errors = self._finish_stages("restore")
            if errors:
                self._raise_lifecycle_error("restore", errors)
            if expired:
                self.stop("исчерпан тайм-аут группы")
                raise GroupTimeout("исчерпан тайм-аут группы")

    def _finish_stages(self, method: str) -> list[tuple[str, BaseException]]:
        errors = []
        for name, runner in reversed(self.started):
            self.log.event(f"stage.{method}.start", stage=name)
            try:
                runner.call(method, phase=method, recover=False)
            except BaseException as error:
                errors.append((name, error))
                self.log.event(f"stage.{method}.failed", stage=name,
                               error_type=type(error).__name__, error=str(error))
                self.stop(f"ошибка {method} компонента '{name}'")
            else:
                self.log.event(f"stage.{method}.done", stage=name)
        return errors

    @staticmethod
    def _raise_lifecycle_error(method: str, errors: list[tuple[str, BaseException]]) -> None:
        details = "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors)
        error_type = RestoreError if method == "restore" else CleanupError
        raise error_type(details) from errors[0][1]

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self.log.event("group.stopped", reason=reason)

    def pytest_report_header(self):
        return f"stands: компонентов {len(self.plan)}, артефакты {self.log.directory}"

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        chosen = []
        used = set()
        for selector in self.selectors:
            found = [item for item in items if matches(item.nodeid, selector)]
            if not found:
                raise pytest.UsageError(f"pytest-stands: тест из конфига не найден: {selector}")
            for item in found:
                if item.nodeid not in used:
                    used.add(item.nodeid)
                    chosen.append(item)
        for selector in self.overrides:
            if not any(matches(item.nodeid, selector) for item in chosen):
                raise pytest.UsageError(f"pytest-stands: привязки заданы для невыбранного теста: {selector}")
        required_interfaces = set(interface_names())
        errors = []
        for item in chosen:
            bindings = dict(self.cfg.bindings)
            applicable = [(selector, values) for selector, values in self.overrides.items()
                          if matches(item.nodeid, selector)]
            for selector, values in sorted(applicable, key=lambda entry: len(entry[0])):
                bindings.update(values)
            try:
                validate_bindings(bindings, self.plan)
            except PlanError as error:
                errors.append(f"{item.nodeid}: {error}")
            needed = required_interfaces.intersection(getattr(item, "fixturenames", ()))
            for name in sorted(needed - bindings.keys()):
                errors.append(f"{item.nodeid}: интерфейс '{name}' не имеет явной привязки")
            self.item_bindings[item.nodeid] = bindings
        if errors:
            raise pytest.UsageError("pytest-stands:\n" + "\n".join(errors))
        deselected = [item for item in items if item.nodeid not in used]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = chosen

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        if self.stop_reason is not None:
            pytest.skip(f"группа остановлена: {self.stop_reason}")
        if self.group_deadline is not None and monotonic() >= self.group_deadline:
            raise GroupTimeout("исчерпан тайм-аут группы")
        self.log.event("test.start", test=item.nodeid)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call: pytest.CallInfo):
        outcome = yield
        report = outcome.get_result()
        infrastructure = call.excinfo is not None and isinstance(call.excinfo.value, InfrastructureError)
        if infrastructure:
            report.stands_infrastructure = True
            if report.skipped and hasattr(report, "wasxfail"):
                report.outcome = "failed"
                report.longrepr = item.repr_failure(call.excinfo)
                delattr(report, "wasxfail")
        if report.failed:
            error_type = type(call.excinfo.value).__name__ if call.excinfo is not None else None
            self.log.event("test.error" if infrastructure or report.when != "call" else "test.failed",
                           test=item.nodeid, phase=report.when, error_type=error_type)
            self.stop(f"{item.nodeid}: {error_type or 'сбой'} ({report.when})")
        elif report.skipped:
            event = "test.xfailed" if hasattr(report, "wasxfail") else "test.skipped"
            self.log.event(event, test=item.nodeid, phase=report.when,
                           reason=str(getattr(report, "wasxfail", report.longrepr)))
        elif report.when == "call" and report.passed:
            event = "test.xpassed" if hasattr(report, "wasxfail") else "test.passed"
            self.log.event(event, test=item.nodeid)

    @pytest.hookimpl(tryfirst=True)
    def pytest_report_teststatus(self, report: pytest.TestReport):
        if report.failed and getattr(report, "stands_infrastructure", False):
            return "error", "E", "ERROR"
        return None

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.log.event("session.finish", exitstatus=int(exitstatus))
