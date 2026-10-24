"""Точка входа pytest: читает конфиг, строит план, вызывает этапы как фикстуры, останавливает группу при сбое."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from .config import ConfigError, load_config
from .layers.registry import load_builtin_and_plugins
from .resolver import PlanError, PlannedStage, build_plan
from .runlog import RunLog


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("stands", "pytest-stands")
    g.addoption("--stands-config", default=None, help="путь к YAML-конфигу группы тестов")
    g.addoption("--stands-artifacts", default="artifacts", help="каталог для артефактов прогона")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("stands_config")
    if not path:
        return  # плагин установлен, но конфиг не задан: ведём себя как обычный pytest
    load_builtin_and_plugins()
    try:
        cfg = load_config(path)
        plan = build_plan(cfg)
    except (ConfigError, PlanError) as e:
        raise pytest.UsageError(f"pytest-stands: {e}") from e

    runlog = RunLog.create(config.getoption("stands_artifacts"))
    shutil.copyfile(path, runlog.directory / "config.applied.yaml")
    runtime = StandsRuntime(cfg, plan, runlog)
    config.stash_runtime = runtime  # type: ignore[attr-defined]
    config.pluginmanager.register(runtime, "stands-runtime")
    config.pluginmanager.register(runtime.fixtures_holder, "stands-stage-fixtures")


class StandsRuntime:
    """Живёт один прогон. Все хуки и динамические фикстуры одной группы."""

    def __init__(self, cfg, plan: list[PlannedStage], runlog: RunLog):
        self.cfg = cfg
        self.plan = plan
        self.log = runlog
        self.stop_reason: str | None = None
        # pytest находит фикстуры по атрибутам класса, поэтому собираем класс-держатель динамически
        ns = {pl.fixture_name: self._make_stage_fixture(pl) for pl in plan}
        self.fixtures_holder = type("_StageFixtures", (), ns)()

    # фикстуры

    def _make_stage_fixture(self, planned: PlannedStage):
        log = self.log

        def _fixture(_holder):  # pytest привязывает фикстуру к объекту-держателю, первый аргумент служебный
            stage = planned.cls(planned.params, log)
            name = planned.cls.name
            log.event("stage.setup.start", stage=name, layer=planned.cls.layer.value)
            try:
                stage.setup()
            except Exception as e:
                log.event("stage.setup.failed", stage=name, error=repr(e))
                self._cleanup(stage, name)  # этап начат: пусть откатит то, что успел изменить
                raise
            log.event("stage.setup.done", stage=name)
            yield stage
            self._cleanup(stage, name, raise_errors=True)

        return pytest.fixture(scope="session", name=planned.fixture_name)(_fixture)

    def _cleanup(self, stage, name: str, raise_errors: bool = False) -> None:
        self.log.event("stage.teardown.start", stage=name)
        try:
            stage.teardown()
        except Exception as e:
            self.log.event("stage.teardown.failed", stage=name, error=repr(e))
            self.stop(f"ошибка очистки этапа {name}")
            if raise_errors:
                raise
        else:
            self.log.event("stage.teardown.done", stage=name)

    @pytest.fixture(scope="session", autouse=True)
    def _stands_group(self, request: pytest.FixtureRequest) -> dict:
        """Поднимает этапы строго в порядке конфига. Teardown pytest делает в обратном порядке."""
        stages = {}
        for planned in self.plan:
            stages[planned.cls.name] = request.getfixturevalue(planned.fixture_name)
        return stages

    @pytest.fixture(scope="session")
    def stands(self, _stands_group: dict) -> dict:
        """Подготовленные этапы по типу. Тесты работают через них, а не создают окружение сами."""
        return _stands_group

    # остановка группы

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self.log.event("group.stopped", reason=reason)

    # хуки

    def pytest_report_header(self, config):
        return [f"stands: этапов {len(self.plan)}, артефакты {self.log.directory}"]

    def pytest_collection_modifyitems(self, session, config, items):
        wanted = self.cfg.tests
        chosen, used = [], set()
        for t in wanted:
            matched = [it for it in items
                       if it.nodeid == t or it.nodeid.startswith(t + "[") or it.nodeid.startswith(t + "::")]
            if not matched:
                raise pytest.UsageError(f"pytest-stands: тест из конфига не найден: {t}")
            for it in matched:
                if id(it) not in used:
                    used.add(id(it))
                    chosen.append(it)
        deselected = [it for it in items if id(it) not in used]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = chosen  # порядок как в YAML

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        if self.stop_reason is not None:
            self.log.event("test.skipped", test=item.nodeid, reason=self.stop_reason)
            pytest.skip(f"группа остановлена: {self.stop_reason}")
        self.log.event("test.start", test=item.nodeid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.failed:
            self.log.event("test.failed", test=item.nodeid, when=report.when)
            self.stop(f"{item.nodeid} ({report.when}) упал")
        elif report.when == "call":
            self.log.event("test.passed", test=item.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        self.log.event("session.finish", exitstatus=int(exitstatus))
