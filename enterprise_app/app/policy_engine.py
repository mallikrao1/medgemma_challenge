import json
from typing import Any

from sqlalchemy.orm import Session

from .models import PolicyRule, User


def _get_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _evaluate_condition(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    field = str(condition.get("field") or "").strip()
    op = str(condition.get("op") or "eq").lower().strip()
    target = condition.get("value")
    if not field:
        return False

    value = _get_path(context, field)
    if op == "eq":
        return value == target
    if op == "neq":
        return value != target
    if op == "lt":
        return value is not None and target is not None and value < target
    if op == "lte":
        return value is not None and target is not None and value <= target
    if op == "gt":
        return value is not None and target is not None and value > target
    if op == "gte":
        return value is not None and target is not None and value >= target
    if op == "contains":
        if isinstance(value, str):
            return str(target or "") in value
        if isinstance(value, list):
            return target in value
        return False
    return False


def evaluate_policies(
    db: Session,
    actor: User,
    action: str,
    context: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    rules = (
        db.query(PolicyRule)
        .filter(
            PolicyRule.tenant_id == actor.tenant_id,
            PolicyRule.target_action == action,
            PolicyRule.is_active.is_(True),
        )
        .all()
    )

    triggered: list[dict[str, Any]] = []
    for rule in rules:
        try:
            condition = json.loads(rule.condition_json or "{}")
        except json.JSONDecodeError:
            continue
        if _evaluate_condition(condition, context):
            triggered.append(
                {
                    "rule_id": rule.id,
                    "rule_name": rule.name,
                    "effect": rule.effect,
                    "condition": condition,
                }
            )

    denied = any(item.get("effect") == "deny" for item in triggered)
    return (not denied), triggered

