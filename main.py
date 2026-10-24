"""
Фреймворк для автоматизации pytest fixtures.
Загрузка YAML-конфигов, валидация через Pydantic, проверка зависимостей.
"""

from typing import Dict, List, Any, Optional, Type, Set
from pydantic import BaseModel, Field, field_validator, model_validator
import yaml
from pathlib import Path


# 1. Базовые модели для контрактов компонентов

class StepConfig(BaseModel):
    """
    Базовый класс для параметров шага.
    Все компоненты наследуются от него и определяют свои поля.
    """
    model_config = {"extra": "forbid"}  # Запрещаем лишние поля в YAML

    def validate_params(self, context: Dict[str, Any]) -> None:
        """
        Кастомная валидация зависимостей между параметрами.
        Переопределяется в компонентах для сложной логики.

        Args:
            context: Словарь {step_name: params} уже распарсенных шагов
        """
        pass


class ComponentContract(BaseModel):
    """
    Контракт компонента: метаданные, модель параметров, зависимости.
    Регистрируется в реестре и используется для валидации YAML.
    """
    name: str
    description: str
    params_model: Type[StepConfig]  # Pydantic-модель для параметров
    requires: List[str] = []  # Имена шагов, которые должны быть до этого
    implements: List[str] = []  # Общие интерфейсы, которые реализует

    def validate_params(self, params: Dict[str, Any], context: Dict[str, Any]) -> StepConfig:
        """
        Валидирует параметры через Pydantic-модель и кастомный метод.

        Returns:
            Инстанс модели с валидными параметрами
        """
        # Этап 1: Pydantic валидация (формат, типы, allowed_values)
        instance = self.params_model(**params)

        # Этап 2: Кастомная валидация (зависимости между параметрами)
        instance.validate_params(context)

        return instance


# 2. Реестр компонентов

class ComponentRegistry:
    """
    Глобальный реестр всех зарегистрированных компонентов.
    Компоненты регистрируются через декоратор @register_component.
    """
    _contracts: Dict[str, ComponentContract] = {}

    @classmethod
    def register(cls, contract: ComponentContract) -> None:
        """Регистрирует контракт компонента."""
        if contract.name in cls._contracts:
            raise ValueError(f"Component '{contract.name}' already registered")
        cls._contracts[contract.name] = contract

    @classmethod
    def get(cls, name: str) -> Optional[ComponentContract]:
        """Получает контракт по имени."""
        return cls._contracts.get(name)

    @classmethod
    def all_names(cls) -> List[str]:
        """Возвращает список всех зарегистрированных компонентов."""
        return list(cls._contracts.keys())

    @classmethod
    def clear(cls) -> None:
        """Очищает реестр (для тестов)."""
        cls._contracts.clear()


def register_component(
        name: str,
        description: str,
        params_model: Type[StepConfig],
        requires: List[str] = None,
        implements: List[str] = None
):
    """
    Декоратор для регистрации компонента во фреймворке.

    Example:
        @register_component(
            name="qemu_boot",
            description="Запускает QEMU",
            params_model=QemuBootParams,
            requires=[],
            implements=["platform"]
        )
        class QemuBoot:
            pass
    """

    def decorator(cls):
        contract = ComponentContract(
            name=name,
            description=description,
            params_model=params_model,
            requires=requires or [],
            implements=implements or []
        )
        ComponentRegistry.register(contract)
        cls.contract = contract
        return cls

    return decorator


# 3. Модели конфига

class Timeouts(BaseModel):
    """
    Таймауты с дефолтными значениями
    Все значения в секундах, допускаются дробные.
    """
    command: float = 30.0  # Таймаут одной команды
    step: float = 300.0  # Таймаут этапа целиком (с повторами)
    cleanup: float = 60.0  # Таймаут очистки
    group: Optional[float] = None  # Группа без ограничения


class DUTConfig(BaseModel):
    """Описание тестируемой системы (DUT)."""
    platform: str  # Например, "qemu"
    base_image: str  # Путь к базовому образу


class StepDefinition(BaseModel):
    """
    Определение шага в YAML.
    Может быть строкой (без параметров) или словарем (с параметрами).
    """
    name: str
    params: Dict[str, Any] = {}


class Config(BaseModel):
    """
    Модель конфига.
    """
    dut: DUTConfig
    steps: List[StepDefinition]
    tests: List[str]
    timeouts: Timeouts = Field(default_factory=Timeouts)

    @field_validator('steps', mode='before')
    @classmethod
    def parse_steps(cls, v):
        """
        Парсит шаги из YAML формата.
        Поддерживает оба формата:
          - qemu_boot              (строка)
          - console_login: {...}   (словарь с одним ключом)
        """
        result = []
        for item in v:
            if isinstance(item, str):
                # Формат: - qemu_boot
                result.append(StepDefinition(name=item))
            elif isinstance(item, dict):
                # Формат: - console_login: {password_env: ...}
                if len(item) != 1:
                    raise ValueError(
                        f"Step must have exactly one key, got: {item}"
                    )
                name, params = next(iter(item.items()))
                result.append(StepDefinition(name=name, params=params or {}))
            else:
                raise ValueError(f"Invalid step format: {item}")
        return result


# 4. Валидатор конфига

class ConfigValidationError(Exception):
    """Ошибка валидации конфига с сообщением."""
    pass


class ConfigValidator:
    """
    Валидирует конфиг до запуска тестов.
    Проверяет: существование компонентов, параметры, зависимости.
    """

    @staticmethod
    def validate(config: Config) -> None:
        """
        Полная валидация конфига.
        Вызывается до любых действий с DUT.
        """
        ConfigValidator._validate_steps_exist(config)
        ConfigValidator._validate_step_params(config)
        ConfigValidator._validate_dependencies_graph(config)

    @staticmethod
    def _validate_steps_exist(config: Config) -> None:
        """Проверяет, что все шаги есть в реестре компонентов."""
        for step in config.steps:
            contract = ComponentRegistry.get(step.name)
            if not contract:
                available = ComponentRegistry.all_names()
                raise ConfigValidationError(
                    f"Step '{step.name}' not found in registry.\n"
                    f"Available components: {available}\n"
                    f"To add a new component, see UC-3-1 in SRS."
                )

    @staticmethod
    def _validate_step_params(config: Config) -> None:
        """
        Валидирует параметры каждого шага через его контракт.
        Строит контекст для кастомной валидации зависимостей.
        """
        context = {}  # {step_name: params} для кастомной валидации

        for step in config.steps:
            contract = ComponentRegistry.get(step.name)
            try:
                # Валидируем параметры через Pydantic + кастомный метод
                contract.validate_params(step.params, context)
                # Добавляем в контекст для следующих шагов
                context[step.name] = step.params
            except Exception as e:
                raise ConfigValidationError(
                    f"Invalid parameters for step '{step.name}':\n{e}"
                )

    @staticmethod
    def _validate_dependencies_graph(config: Config) -> None:
        """
        Строит граф зависимостей и проверяет топологический порядок.
        Если шаг A требует шаг B, то B должен идти до A в списке.
        """
        # Строим граф: step_name -> set of required steps
        graph: Dict[str, Set[str]] = {}
        step_names = [step.name for step in config.steps]

        for step in config.steps:
            contract = ComponentRegistry.get(step.name)
            graph[step.name] = set(contract.requires)

        # Проверка 1: Все зависимости существуют в конфиге
        for step_name, requires in graph.items():
            for req in requires:
                if req not in step_names:
                    raise ConfigValidationError(
                        f"Step '{step_name}' requires '{req}', "
                        f"but '{req}' is not in the steps list.\n"
                        f"Add '{req}' to the steps before '{step_name}'."
                    )

        # Проверка 2: Топологический порядок (зависимости идут до зависимых)
        visited: Set[str] = set()
        for step in config.steps:
            # Все зависимости текущего шага должны быть уже посещены
            for req in graph[step.name]:
                if req not in visited:
                    raise ConfigValidationError(
                        f"Dependency order violation:\n"
                        f"Step '{step.name}' requires '{req}', "
                        f"but '{req}' must come before '{step.name}' "
                        f"in the steps list.\n"
                        f"Current order: {step_names}"
                    )
            visited.add(step.name)


# 5. Загрузчик конфига

def load_config(path: str) -> Config:
    """
    Загружает и валидирует конфиг из YAML файла.

    Args:
        path: Путь к YAML файлу

    Returns:
        Валидный инстанс Config

    Raises:
        ConfigValidationError: Если конфиг невалиден
        FileNotFoundError: Если файл не найден
    """
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(yaml_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    if not data:
        raise ConfigValidationError("Config file is empty")

    # Парсим YAML в Pydantic модель
    config = Config(**data)

    # Валидируем через ConfigValidator
    ConfigValidator.validate(config)

    return config


# 6. Примеры компонентов

# Пример 1: qemu_boot
class QemuBootParams(StepConfig):
    image: str
    memory: int = 1024
    cpus: int = 1


@register_component(
    name="qemu_boot",
    description="Запускает QEMU виртуальную машину",
    params_model=QemuBootParams,
    requires=[],
    implements=["platform"]
)
class QemuBoot:
    pass


# Пример 2: console_login
class ConsoleLoginParams(StepConfig):
    password_env: str  # Ссылка на переменную окружения
    username: str = "root"

    def validate_params(self, context: Dict[str, Any]) -> None:
        """
        Кастомная валидация:
        - Проверяем формат password_env (должна быть строка)
        - Физическую проверку переменной окружения откладываем на runtime
        """
        if not isinstance(self.password_env, str):
            raise ValueError("password_env must be a string (env var name)")

        # Пример зависимости: если username != "root", проверяем наличие sudo
        if self.username != "root" and "qemu_boot" in context:
            # Можно добавить сложную логику здесь
            pass


@register_component(
    name="console_login",
    description="Вход в консоль по логину и паролю",
    params_model=ConsoleLoginParams,
    requires=["qemu_boot"],
    implements=["transport"]
)
class ConsoleLogin:
    pass


# Пример 3: shell
class ShellParams(StepConfig):
    timeout: float = 30.0
    prompt: str = r"\$ "


@register_component(
    name="shell",
    description="Открывает shell поверх транспорта",
    params_model=ShellParams,
    requires=["console_login"],
    implements=["shell"]
)
class Shell:
    pass


# 7. Пример использования

if __name__ == "__main__":
    # Пример YAML конфига
    yaml_content = """
dut:
  platform: qemu
  base_image: images/ubuntu-22.04.qcow2

steps:
  - qemu_boot:
      image: images/ubuntu-22.04.qcow2
      memory: 2048
  - console_login:
      password_env: DUT_PASSWORD
      username: root
  - shell:
      timeout: 60.0

tests:
  - tests/test_network.py::test_ping
  - tests/test_disk.py::test_mount

timeouts:
  command: 30
  step: 300
  cleanup: 60
"""

    # Сохраняем в файл
    with open("test_config.yaml", "w") as f:
        f.write(yaml_content)

    # Загружаем и валидируем
    try:
        config = load_config("test_config.yaml")
        print("✓ Config loaded successfully!")
        print(f"  Platform: {config.dut.platform}")
        print(f"  Steps: {[s.name for s in config.steps]}")
        print(f"  Tests: {len(config.tests)}")
        print(f"  Timeouts: command={config.timeouts.command}s, step={config.timeouts.step}s")
    except ConfigValidationError as e:
        print(f"✗ Config validation failed:\n{e}")
    except Exception as e:
        print(f"✗ Error: {e}")