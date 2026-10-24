"""Реестр этапов. Расширение: декоратор @register_stage или entry point группы pytest_stands.stages."""
from __future__ import annotations

import inspect
from importlib.metadata import entry_points

from .base import Stage

_STAGES: dict[str, type[Stage]] = {}
ENTRY_POINT_GROUP = "pytest_stands.stages"


def register_stage(name: str):
    def deco(cls: type[Stage]) -> type[Stage]:
        if name in _STAGES and _STAGES[name] is not cls:
            raise ValueError(f"этап '{name}' уже зарегистрирован")
        cls.name = name
        _STAGES[name] = cls
        return cls
    return deco


def get_stage(name: str) -> type[Stage] | None:
    return _STAGES.get(name)


def stage_names() -> list[str]:
    return sorted(_STAGES)


def load_builtin_and_plugins() -> None:
    """Импорт встроенных этапов и этапов из сторонних пакетов."""
    from .hardware import mock_power  # noqa: F401
    from .linux import mock_linux  # noqa: F401

    for ep in entry_points(group=ENTRY_POINT_GROUP):
        obj = ep.load()
        if inspect.isclass(obj) and issubclass(obj, Stage) and getattr(obj, "name", None) not in _STAGES:
            register_stage(ep.name)(obj)
