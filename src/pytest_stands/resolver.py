"""Резолвер: из конфига строит план этапов. Все проверки до любых действий с DUT."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from .config.models import Config
from .layers.base import Stage, StageParams
from .layers.registry import get_stage, stage_names


class PlanError(Exception):
    """План невозможен. В сообщении все найденные проблемы сразу."""


@dataclass(frozen=True)
class PlannedStage:
    index: int
    cls: type[Stage]
    params: StageParams

    @property
    def fixture_name(self) -> str:
        return f"stage_{self.index}_{self.cls.name}"


def build_plan(config: Config) -> list[PlannedStage]:
    """Порядок = порядок в конфиге. Зависимости (requires) только проверяются, порядок не меняется."""
    errors: list[str] = []
    plan: list[PlannedStage] = []
    seen: set[str] = set()

    for i, st in enumerate(config.stages):
        cls = get_stage(st.type)
        if cls is None:
            errors.append(f"этап #{i} '{st.type}': такого этапа нет. Доступны: {stage_names()}")
            continue
        if cls.layer != st.layer:
            errors.append(f"этап #{i} '{st.type}': это слой {cls.layer.value}, а в конфиге указан {st.layer.value}")
        try:
            params = cls.params_model.model_validate(st.params)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"]) or "params"
                errors.append(f"этап #{i} '{st.type}', параметр {loc}: {err['msg']}")
            params = None
        for req in cls.requires:
            if req not in seen:
                errors.append(f"этап #{i} '{st.type}' требует '{req}' раньше в списке этапов")
        seen.add(st.type)
        if params is not None:
            plan.append(PlannedStage(index=i, cls=cls, params=params))

    if errors:
        raise PlanError("\n".join(errors))
    return plan
