from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Config


class ConfigError(Exception):
    """Конфиг не прочитан или не прошёл проверку. Сообщение показывается пользователю как есть."""


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"файл конфига не найден: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"некорректный YAML в {p}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"конфиг {p} пуст или не является словарём")
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        lines = [f"  {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError(f"ошибки в конфиге {p}:\n" + "\n".join(lines)) from e
