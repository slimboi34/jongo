"""HTML elements as Python functions.

    from jongo.html import *

    div(
        h1("Hello", class_="title"),
        button("Click me", on_click=handler),
        ul([li(item) for item in items]),
    )

Positional arguments are children (strings, elements, lists). Keyword arguments
are attributes: ``class_`` becomes ``class``, ``on_click`` is an event handler,
``aria_label`` becomes ``aria-label``, and ``style`` takes a dict.
Names that clash with Python builtins get a trailing underscore: ``input_``, ``del_``.
"""
from __future__ import annotations

from .vdom import Tag, fragment, h, raw

_TAG_NAMES = """
a abbr address article aside audio b bdi bdo blockquote br button canvas caption
cite code col colgroup datalist dd details dfn dialog div dl dt em embed fieldset
figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr i iframe img ins
kbd label legend li link main mark meta meter nav noscript ol optgroup option
output p picture pre progress rp rt ruby samp script section select small source
span strong style sub summary sup table tbody td template textarea tfoot th thead
tr track u ul video wbr svg
""".split()

_RENAMED = {
    "input_": "input",
    "del_": "del",
    "map_": "map",
    "object_": "object",
    "var_": "var",
    "time_": "time",
    "data_": "data",
    "q_": "q",
    "s_": "s",
}

TAGS: dict[str, Tag] = {}
for _name in _TAG_NAMES:
    TAGS[_name] = Tag(_name)
for _py, _html_name in _RENAMED.items():
    TAGS[_py] = Tag(_html_name)

globals().update(TAGS)

__all__ = sorted(TAGS) + ["h", "raw", "fragment"]
