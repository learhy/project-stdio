"""Reducer registry for aggregator nodes with output_strategy: reduce.

Reducers are plain functions registered by decorator. Built-in reducers:
  majority_vote, concatenate, select_best_by, collect_all
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable

Reducer = Callable[[list[dict[str, Any]], dict[str, Any]], Any]

_registry: dict[str, Reducer] = {}


def register(name: str) -> Callable[[Reducer], Reducer]:
    def decorator(fn: Reducer) -> Reducer:
        _registry[name] = fn
        return fn
    return decorator


def get_reducer(name: str) -> Reducer | None:
    return _registry.get(name)


def list_reducers() -> list[str]:
    return sorted(_registry.keys())


@register("majority_vote")
def majority_vote(outputs: list[dict[str, Any]], config: dict[str, Any]) -> Any:
    field = config.get("field", "answer")
    values = []
    for output in outputs:
        val = _extract_field(output, field)
        if val is not None:
            values.append(val)
    if not values:
        return None
    counter = Counter(values)
    return counter.most_common(1)[0][0]


@register("concatenate")
def concatenate(outputs: list[dict[str, Any]], config: dict[str, Any]) -> Any:
    field = config.get("field", "content")
    sep = config.get("separator", "\n")
    parts = []
    for output in outputs:
        val = _extract_field(output, field)
        if val is not None:
            if isinstance(val, list):
                parts.extend(val)
            else:
                parts.append(str(val))
    if all(isinstance(p, str) for p in parts):
        return sep.join(parts)
    return parts


@register("select_best_by")
def select_best_by(outputs: list[dict[str, Any]], config: dict[str, Any]) -> Any:
    field = config.get("field", "score")
    mode = config.get("mode", "max")
    best_output = None
    best_val = None
    for output in outputs:
        val = _extract_field(output, field)
        if val is None:
            continue
        if best_val is None or (mode == "max" and val > best_val) or (mode == "min" and val < best_val):
            best_val = val
            best_output = output
    return best_output


@register("collect_all")
def collect_all(outputs: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    return list(outputs)


def _extract_field(output: dict[str, Any], field_path: str) -> Any:
    parts = field_path.split(".")
    current: Any = output
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current
