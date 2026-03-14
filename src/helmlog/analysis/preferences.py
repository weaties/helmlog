"""Preference inheritance for default analysis model (#283).

Scopes: platform → co_op → boat → user.  The most specific scope wins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helmlog.storage import Storage

# Scope hierarchy from broadest to most specific.
_SCOPES = ("platform", "co_op", "boat", "user")


async def resolve_preference(
    storage: Storage,
    user_id: int,
    co_op_id: str | None = None,
) -> str | None:
    """Walk the scope chain and return the most specific model_name, or None."""
    # Most specific first: user → boat → co_op → platform
    checks: list[tuple[str, str | None]] = [
        ("user", str(user_id)),
        ("boat", None),
    ]
    if co_op_id:
        checks.append(("co_op", co_op_id))
    checks.append(("platform", None))

    for scope, scope_id in checks:
        pref = await storage.get_analysis_preference(scope, scope_id)
        if pref is not None:
            return pref["model_name"]  # type: ignore[no-any-return]
    return None


async def set_preference(
    storage: Storage,
    scope: str,
    scope_id: str | None,
    model_name: str,
) -> None:
    """Set the preferred analysis model at the given scope."""
    if scope not in _SCOPES:
        raise ValueError(f"Invalid scope {scope!r}; must be one of {_SCOPES}")
    await storage.set_analysis_preference(scope, scope_id, model_name)
