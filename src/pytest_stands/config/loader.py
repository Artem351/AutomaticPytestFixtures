from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Config


class ConfigError(ValueError):
    pass


class ConfigLoader(yaml.SafeLoader):
    pass


def construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise yaml.constructor.ConstructorError(
                    'configuration mapping', node.start_mark,
                    f'duplicate key {key!r}', key_node.start_mark,
                )
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                'configuration mapping', node.start_mark,
                f'invalid mapping key: {error}', key_node.start_mark,
            ) from error
    return result


ConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)


def load_config(path: str | Path) -> Config:
    try:
        with Path(path).open(encoding='utf-8') as stream:
            data = yaml.load(stream, Loader=ConfigLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError) as error:
        raise ConfigError(f'configuration {path}: {error}') from error
    try:
        return Config.model_validate(data)
    except ValidationError as error:
        details = '; '.join(
            f'{".".join(str(part) for part in item["loc"]) or "configuration"}: {item["msg"]}'
            for item in error.errors(include_url=False)
        )
        raise ConfigError(f'configuration {path}: {details}') from error
