import importlib
import inspect
from abc import ABC
from importlib.metadata import entry_points


_stages = {}
_interfaces = {}
_builtins_loaded = False
_plugins_loaded = False
_loading = False


def _name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError('registry name must be a nonempty string')


def register_stage(name):
    _name(name)

    def register(cls):
        from .base import Stage

        if not inspect.isclass(cls) or not issubclass(cls, Stage):
            raise ValueError(f'stage {name!r} must subclass Stage')
        if name in _stages and _stages[name] is not cls:
            raise ValueError(f'stage {name!r} is already registered')
        cls.name = name
        _stages[name] = cls
        return cls

    return register


def register_interface(name):
    _name(name)

    def register(cls):
        if not inspect.isclass(cls) or not issubclass(cls, ABC) or not inspect.isabstract(cls):
            raise ValueError(f'interface {name!r} must be an abstract ABC')
        if name in _interfaces and _interfaces[name] is not cls:
            raise ValueError(f'interface {name!r} is already registered')
        _interfaces[name] = cls
        return cls

    return register


def load_builtin_and_plugins():
    global _builtins_loaded, _plugins_loaded, _loading
    if _loading:
        return
    _loading = True
    try:
        if not _builtins_loaded:
            importlib.import_module('pytest_stands.interfaces')
            importlib.import_module('pytest_stands.layers.hardware.mock_power')
            importlib.import_module('pytest_stands.layers.linux.mock_linux')
            _builtins_loaded = True
        if not _plugins_loaded:
            for plugin in entry_points(group='pytest_stands.stages'):
                registration = plugin.load()
                if callable(registration) and not inspect.isclass(registration):
                    registration()
            _plugins_loaded = True
    finally:
        _loading = False


def get_stage(name):
    load_builtin_and_plugins()
    try:
        return _stages[name]
    except (KeyError, TypeError):
        raise ValueError(f'unknown stage {name!r}; registered stages: {", ".join(_stages)}') from None


def stage_names():
    load_builtin_and_plugins()
    return tuple(_stages)


def get_interface(name):
    load_builtin_and_plugins()
    try:
        return _interfaces[name]
    except (KeyError, TypeError):
        raise ValueError(f'unknown interface {name!r}; registered interfaces: {", ".join(_interfaces)}') from None


def interface_names():
    load_builtin_and_plugins()
    return tuple(_interfaces)
