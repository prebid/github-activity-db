"""GitHub's documented GraphQL point cost, computed from a document's text.

GitHub: "Add up the number of requests needed to fulfill each unique connection in the call.
Assume every request will reach the first or last argument limits. Divide the number by 100 and
round the result to the nearest whole number to get the final aggregate rate limit score. The
minimum score is 1." A connection nested in another needs one request per parent node, so its
requests are the product of the enclosing connections' ``first``.

This parses only the GraphQL the source layer writes: fields with aliases and arguments,
selections, inline fragments, and named fragments.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r"\.\.\.|[{}():,!\[\]$]|[A-Za-z_][A-Za-z0-9_]*|-?\d+|\"(?:[^\"\\]|\\.)*\"")


@dataclass
class _Field:
    name: str
    first: int | None
    children: list[_Field | str] = field(default_factory=list)  # str: a fragment spread


def cost(document: str) -> int:
    """The point cost GitHub's formula gives ``document``."""
    tokens = _TOKEN.findall(document)
    fragments: dict[str, list[_Field | str]] = {}
    operation: list[_Field | str] = []
    position = 0
    while position < len(tokens):
        if tokens[position] == "fragment":
            name = tokens[position + 1]
            position = tokens.index("{", position)
            selection, position = _selection(tokens, position)
            fragments[name] = selection
        elif tokens[position] == "query":
            position = _skip_to_selection(tokens, position)
            operation, position = _selection(tokens, position)
        else:
            position += 1
    requests = _requests(operation, fragments, parents=1)
    return max(1, math.floor(requests / 100 + 0.5))


def _requests(
    selection: list[_Field | str], fragments: dict[str, list[_Field | str]], parents: int
) -> int:
    total = 0
    for child in selection:
        if isinstance(child, str):
            total += _requests(fragments[child], fragments, parents)
        elif child.first is not None:
            total += parents
            total += _requests(child.children, fragments, parents * child.first)
        else:
            total += _requests(child.children, fragments, parents)
    return total


def _skip_to_selection(tokens: list[str], position: int) -> int:
    depth = 0
    while True:
        token = tokens[position]
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif token == "{" and depth == 0:
            return position
        position += 1


def _selection(tokens: list[str], position: int) -> tuple[list[_Field | str], int]:
    """Parse ``{ ... }`` starting at ``tokens[position] == "{"``; return it and the next index."""
    assert tokens[position] == "{"
    position += 1
    items: list[_Field | str] = []
    while tokens[position] != "}":
        token = tokens[position]
        if token == "...":
            if tokens[position + 1] == "on":
                position = tokens.index("{", position)
                inline, position = _selection(tokens, position)
                items.append(_Field(name="inline", first=None, children=inline))
            else:
                items.append(tokens[position + 1])
                position += 2
            continue
        name = token
        position += 1
        if tokens[position] == ":":
            name = tokens[position + 1]
            position += 2
        first = None
        if tokens[position] == "(":
            first, position = _arguments(tokens, position)
        children: list[_Field | str] = []
        if tokens[position] == "{":
            children, position = _selection(tokens, position)
        items.append(_Field(name=name, first=first, children=children))
    return items, position + 1


def _arguments(tokens: list[str], position: int) -> tuple[int | None, int]:
    depth = 0
    first = None
    while True:
        token = tokens[position]
        if token in "([{":
            depth += 1
        elif token in ")]}":
            depth -= 1
            if depth == 0:
                return first, position + 1
        elif token in ("first", "last") and depth == 1 and tokens[position + 1] == ":":
            first = int(tokens[position + 2])
        position += 1
