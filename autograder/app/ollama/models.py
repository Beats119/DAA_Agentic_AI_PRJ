"""Task -> model resolution from ``configs/models.yaml``.

Every agent resolves its model through :func:`model_cfg_for`, so model
names and generation settings live in exactly one place. A task missing
from the config is an explicit error — models are never silently swapped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "configs" / "models.yaml"

_KNOWN_TASKS = (
    "specification",
    "test_generation",
    "test_refinement",
    "code_review",
    "feedback",
    "feedback_check",
)

_DEFAULTS: dict[str, Any] = {
    "model": "llama3.1",
    "temperature": 0.0,
    "seed": 42,
    "timeout_s": 120.0,
    "max_tokens": 2048,
}


class ModelConfigError(ValueError):
    """Raised when the task->model configuration is missing or invalid."""


def load_models_config(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Read ``configs/models.yaml``. Returns ``{task: settings}``."""
    p = Path(path) if path else CONFIG_PATH
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ModelConfigError("PyYAML is required to read models.yaml") from exc
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ModelConfigError(f"cannot read models config {p}: {exc}") from exc
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        raise ModelConfigError(f"models config {p} has no 'tasks' mapping")
    out: dict[str, dict[str, Any]] = {}
    for task, cfg in tasks.items():
        if not isinstance(cfg, dict) or "model" not in cfg:
            raise ModelConfigError(f"task {task!r} in {p} needs a 'model' entry")
        merged = dict(_DEFAULTS)
        merged.update(cfg)
        out[task] = merged
    return out


def model_cfg_for(task: str, path: str | Path | None = None) -> dict[str, Any]:
    """Return the model settings dict for ``task``. Raises if unknown."""
    cfgs = load_models_config(path)
    if task not in cfgs:
        raise ModelConfigError(
            f"no model configured for task {task!r}; "
            f"known tasks: {sorted(cfgs)}"
        )
    return cfgs[task]
