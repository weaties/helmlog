"""Preference inheritance for default visualization plugins (#286).

Scopes: platform -> co_op -> boat -> user.  The most specific scope wins.
Same 4-level inheritance pattern as analysis preferences.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helmlog.storage import Storage

# Scope hierarchy from broadest to most specific.
_SCOPES = ("platform", "co_op", "boat", "user")


async def resolve_viz_preference(
    storage: Storage,
    user_id: int,
    co_op_id: str | None = None,
) -> list[str] | None:
    """Walk the scope chain and return the most specific plugin_names list, or None."""
    import json  # noqa: PLC0415

    # Most specific first: user -> boat -> co_op -> platform
    checks: list[tuple[str, str | None]] = [
        ("user", str(user_id)),
        ("boat", None),
    ]
    if co_op_id:
        checks.append(("co_op", co_op_id))
    checks.append(("platform", None))

    for scope, scope_id in checks:
        pref = await storage.get_viz_preference(scope, scope_id)
        if pref is not None:
            try:
                return json.loads(pref["plugin_names"])  # type: ignore[no-any-return]
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
    return None


async def set_viz_preference(
    storage: Storage,
    scope: str,
    scope_id: str | None,
    plugin_names: list[str],
) -> None:
    """Set the preferred visualization plugins at the given scope."""
    import json  # noqa: PLC0415

    if scope not in _SCOPES:
        raise ValueError(f"Invalid scope {scope!r}; must be one of {_SCOPES}")
    names_json = json.dumps(plugin_names)
    await storage.set_viz_preference(scope, scope_id, names_json)
