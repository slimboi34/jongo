"""Tests that kill mutants which survived the 0.7.0 regression suite.

Mutation testing of 0.7.0 changed one line at a time (only the lines the release changed: 422
mutants over 11 files) and ran the release's tests. Each test here pins behaviour a surviving,
non-equivalent mutant changed: it fails on that mutant and passes on the code as shipped.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess

import pytest

from jongo import auth
from jongo.compiler.bundle import Bundler, runtime_source
from jongo.http import Request

NODE = shutil.which("node")


# compiler/transpile.py: `_is_num(right_node) and right_node.value != 0` compiles a literal
# non-zero divisor to a bare "/". The parity suite divided by a *variable* zero only, so the
# mutant `== 0` (5 / 0 compiled to Infinity) survived. The Pow guard beside it is checked too.
@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("expr,expected", [
    ("5 / 0", "ZeroDivisionError"),
    ("5 % 0", "ZeroDivisionError"),
    ("5 // 0", "ZeroDivisionError"),
    ("2 ** -1", "0.5"),          # a literal negative exponent must use $pow, not a bare **
])
def test_literal_zero_divisor_and_negative_power_match_cpython(expr, expected, tmp_path):
    path = tmp_path / "m_killer.py"
    path.write_text(f"def f():\n    return {expr}\n")
    spec = importlib.util.spec_from_file_location("m_killer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bundler = Bundler(roots=[tmp_path])
    name = bundler.resolve("f", module.f)
    units = "\n".join(f"const {u.name} = {u.code};" for u in bundler.order)
    script = ('(function(){"use strict";\n' + runtime_source() + "\n" + units
              + f"\ntry {{ console.log(JSON.stringify({{ok: $repr({name}())}})); }} "
              + "catch (e) { console.log(JSON.stringify({err: String(e && e.name)})); }\n})();")
    proc = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=60)
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == ({"err": expected} if expected == "ZeroDivisionError" else {"ok": expected}), (expr, out)


# auth.py: `_checkable()` decides whether verify_password really hashes against a stored value
# (part of the constant-time login). Nothing pinned that the algorithm must match or that one
# iteration is enough, so `and` -> `or` and `>=` -> `>` both survived.
def test_checkable_requires_matching_algorithm_and_positive_iterations():
    assert auth._checkable(f"{auth.ALGORITHM}$480000$c2FsdHNhbHQ$aGFzaGhhc2g") is True
    assert auth._checkable("bcrypt$480000$c2FsdHNhbHQ$aGFzaGhhc2g") is False   # kills and -> or
    assert auth._checkable(f"{auth.ALGORITHM}$1$c2FsdHNhbHQ$aGFzaGhhc2g") is True  # kills >= -> >
    assert auth._checkable(f"{auth.ALGORITHM}$0$c2FsdHNhbHQ$aGFzaGhhc2g") is False


# http.py: max_form_fields / max_form_parts = 1000. No test sat on the exact boundary, so +-1
# survived.
def _urlencoded(n):
    return "&".join(f"f{i}=v" for i in range(n)).encode()


def _multipart(n):
    body = b"".join(b'--B\r\nContent-Disposition: form-data; name="f%d"\r\n\r\nv\r\n' % i for i in range(n))
    return body + b"--B--\r\n"


def _request(body, content_type):
    return Request({"REQUEST_METHOD": "POST", "PATH_INFO": "/x", "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)})


FORMS = [("application/x-www-form-urlencoded", _urlencoded), ("multipart/form-data; boundary=B", _multipart)]


@pytest.mark.parametrize("content_type,build", FORMS, ids=["urlencoded", "multipart"])
def test_form_cap_accepts_exactly_the_limit(content_type, build):
    assert len(_request(build(1000), content_type).form) == 1000


@pytest.mark.parametrize("content_type,build", FORMS, ids=["urlencoded", "multipart"])
def test_form_cap_refuses_one_over_the_limit(content_type, build):
    with pytest.raises(Exception) as info:
        _request(build(1001), content_type).form
    assert getattr(info.value, "status", None) == 413


# http.py: keep_blank_values=True. Flipping it drops empty fields (an untouched text input),
# and no 0.7.0 test posted one.
def test_urlencoded_keeps_blank_values():
    form = _request(b"a=&b=x", "application/x-www-form-urlencoded").form
    assert form.get("a") == "" and form.get("b") == "x"
