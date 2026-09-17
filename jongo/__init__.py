# Jongo — a full-stack Python web framework.
# Copyright (C) 2026 Joshua Harty
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Affero General Public License (the LICENSE file, or <https://www.gnu.org/licenses/>)
# for the full terms.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jongo: a full-stack Python web framework.

Pages, server code and reactive UI in one language. Components are plain Python
that renders on the server and compiles to JavaScript for the browser.
"""
from .app import Jongo, Page
from .errors import CompileError, Forbidden, HTTPError, JongoError, NotFound, ServerError
from .http import Request, Response, json_response, redirect
from .rpc import server
from .styles import css, global_css
from .vdom import (
    VNode,
    component,
    effect,
    form_values,
    fragment,
    h,
    js,
    navigate,
    raw,
    ref,
    refresh,
    state,
)

__version__ = "0.2.3"

__all__ = [
    "Jongo", "Page", "Request", "Response", "redirect", "json_response",
    "component", "state", "effect", "ref", "navigate", "refresh", "form_values", "js",
    "server", "css", "global_css", "h", "raw", "fragment", "VNode",
    "HTTPError", "NotFound", "Forbidden", "ServerError", "CompileError", "JongoError",
]
