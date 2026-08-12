"""A minimal shim for building the ES query DSL (replacing elasticsearch_dsl's Q / Search).

The repositories use elasticsearch_dsl only to **build a query dict** (execution goes through the swappable client method surface),
and the subset they use is: ``Q(name, **params)`` / ``Q(raw_dict)`` / ``.to_dict()``,
``Search(using=, index=).query(...).filter(...).sort(...)[:size].to_dict()``。

This shim reproduces that ``to_dict()`` shape (checked case by case against the real library), which removes elasticsearch-dsl
from the default dependencies (it becomes the ``[es]`` extra) while leaving the repository code untouched.
"""

from __future__ import annotations

from typing import Any


def _to_dict(obj: Any) -> Any:
    if isinstance(obj, Q):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(v) for v in obj]
    return obj


class Q:
    """One query clause. ``Q("term", f=v)`` -> ``{"term": {"f": v}}``; ``Q(raw_dict)`` -> as is."""

    def __init__(self, name_or_dict: str | dict[str, Any], **params: Any) -> None:
        if isinstance(name_or_dict, dict):
            self._d: dict[str, Any] = name_or_dict
        else:
            self._d = {name_or_dict: params}

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self._d)


class Search:
    """A chained query builder reproducing the to_dict shape of elasticsearch_dsl.Search.

    Note: the real library returns a copy at every step (immutable); this one mutates in place and returns self. The repositories
    all use it linearly as ``s = s.query(...)`` / ``s = s[:n]``, so the behaviour is equivalent.
    """

    def __init__(self, using: Any = None, index: str | None = None) -> None:
        self._using = using
        self._index = index
        self._must: list[Q] = []
        self._filter: list[Q] = []
        self._sort: list[dict[str, Any]] = []
        self._size: int | None = None

    def query(self, *args: Any, **params: Any) -> Search:
        # The first positional argument is the query type (such as "match" / "multi_match") or an already built Q; *args
        # receives it rather than a named parameter, so it cannot clash with a field name (which may itself be name or query).
        head = args[0] if args else None
        self._must.append(head if isinstance(head, Q) else Q(head, **params))
        return self

    def filter(self, *args: Any, **params: Any) -> Search:
        head = args[0] if args else None
        if isinstance(head, Q):
            self._filter.append(head)
        elif isinstance(head, dict):
            self._filter.append(Q(head))
        else:
            self._filter.append(Q(head, **params))
        return self

    def sort(self, *keys: str) -> Search:
        for k in keys:
            if k.startswith("-"):
                self._sort.append({k[1:]: {"order": "desc"}})
            else:
                self._sort.append({k: {"order": "asc"}})
        return self

    def __getitem__(self, item: slice | int) -> Search:
        if isinstance(item, slice) and item.stop is not None:
            self._size = int(item.stop)
        return self

    def to_dict(self) -> dict[str, Any]:
        must = [q.to_dict() for q in self._must]
        filt = [q.to_dict() for q in self._filter]
        out: dict[str, Any] = {}
        if filt or len(must) > 1:
            bool_body: dict[str, Any] = {}
            if must:
                bool_body["must"] = must
            if filt:
                bool_body["filter"] = filt
            out["query"] = {"bool": bool_body}
        elif must:
            out["query"] = must[0]
        if self._size is not None:
            out["size"] = self._size
        if self._sort:
            out["sort"] = self._sort
        return out


__all__ = ["Q", "Search"]
