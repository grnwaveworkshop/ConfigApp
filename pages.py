"""Page layout for the config UI - turns the flat param list into navigable
pages, entirely derived from the key strings that already cross the wire.
No per-project curation table: the firmware's parameter table has no notion
of sections, but keys are consistently dotted/namespaced, so:

  - the *first* dotted component is the page ("sbus", "hero", "m", "lift", ...)
  - if a page's params have more than one distinct *second* component
    (e.g. "m.headR.*" vs "m.tRing.*"), it gets sub-tabs by that component;
    otherwise it's shown as one flat table.

This means a new param, or even a whole new key prefix, appended to any
project's ConfigParams.def shows up correctly with no changes here - and it's
the same reason this file needs no per-robot profile at all.
"""
from __future__ import annotations


def page_for_key(key: str) -> str:
    """Map a config key to its page name via the first dotted component."""
    return key.split(".")[0]


def group_params(params: list) -> dict[str, list]:
    """Bucket ParamInfo objects into pages, preserving id order within each."""
    out: dict[str, list] = {}
    for info in params:
        out.setdefault(page_for_key(info.key), []).append(info)
    return out


def ordered_pages(grouped: dict[str, list]) -> list[str]:
    """Pages in alphabetical order, omitting empty ones."""
    return sorted(name for name, params in grouped.items() if params)


def subgroup_of(key: str) -> str | None:
    """Second dotted component, e.g. 'm.headR.address' -> 'headR'. None for a
    key with no third component (e.g. 'lift.tolerance')."""
    parts = key.split(".")
    return parts[1] if len(parts) > 2 else None


def has_subgroups(params: list) -> bool:
    """True if this page's params naturally split into >1 sub-tab."""
    subs = {subgroup_of(p.key) for p in params}
    subs.discard(None)
    return len(subs) > 1


def subgroups(params: list) -> tuple[list, dict[str, list]]:
    """Split a page into (params with no subgroup, {subgroup: [params...]})."""
    glob: list = []
    per: dict[str, list] = {}
    for info in params:
        sub = subgroup_of(info.key)
        if sub is None:
            glob.append(info)
        else:
            per.setdefault(sub, []).append(info)
    return glob, per
