from abc import ABC, abstractmethod

from .layers.registry import register_interface


@register_interface('power')
class PowerControl(ABC):
    @abstractmethod
    def is_on(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_on(self, on: bool) -> None:
        raise NotImplementedError


@register_interface('system')
class SystemInfo(ABC):
    @abstractmethod
    def is_ready(self) -> bool:
        raise NotImplementedError


@register_interface('service')
class ServiceControl(ABC):
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError
