"""The Jongo documentation site — itself a Jongo app.

    jongo dev docs/docs_site.py --port 8790

It renders `guide.md` and the pattern cookbook, with a client-side search box over the
whole guide. Everything on this page is Python: the markdown renderer runs on the server,
the search box compiles to JavaScript and runs in the browser.
"""

from __future__ import annotations

import re
from pathlib import Path

from jongo import Jongo, component, css, global_css, state
from jongo.html import (a, article, aside, code, div, footer, h1, h2, h3, header, input_, li,
                        main, nav, p, pre, span, strong, table, tbody, td, th, thead, tr, ul)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
GUIDE = HERE / "guide.md"
PATTERNS = REPO / "examples" / "patterns"

app = Jongo(__name__, title="Jongo — documentation")

global_css("""
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
       color: #14161a; background: #fff; }
a { color: #1a5fd0; }
@media (prefers-color-scheme: dark) {
  body { background: #0e1013; color: #e6e8ec; }
  a { color: #7fb0ff; }
}
""")

s = css(
    shell={"display": "grid", "grid_template_columns": "260px minmax(0, 1fr)", "gap": 40,
           "max_width": 1100, "margin": "0 auto", "padding": "0 20px 80px",
           "@media (max-width: 860px)": {"grid_template_columns": "1fr", "gap": 0}},
    side={"position": "sticky", "top": 0, "align_self": "start", "padding_top": 28,
          "max_height": "100vh", "overflow_y": "auto",
          "& ul": {"list_style": "none", "margin": 0, "padding": 0},
          "& li": {"margin": "2px 0"},
          "& a": {"text_decoration": "none", "font_size": 14, "display": "block",
                  "padding": "3px 8px", "border_radius": 6, "color": "inherit", "opacity": .8},
          "& a:hover": {"background": "rgba(127,127,127,.12)", "opacity": 1},
          "@media (max-width: 860px)": {"position": "static", "max_height": "none"}},
    masthead={"padding": "28px 0 8px",
              "& h1": {"margin": 0, "font_size": 22, "letter_spacing": "-.02em"},
              "& p": {"margin": "4px 0 0", "opacity": .7, "font_size": 14}},
    search={"width": "100%", "padding": "9px 12px", "border_radius": 9, "font_size": 14,
            "border": "1px solid rgba(127,127,127,.35)", "background": "transparent",
            "color": "inherit"},
    results={"margin_top": 10, "border_top": "1px solid rgba(127,127,127,.2)", "padding_top": 10},
    result={"display": "block", "padding": "7px 8px", "border_radius": 7,
            "text_decoration": "none", "color": "inherit",
            ":hover": {"background": "rgba(127,127,127,.12)"},
            "& strong": {"display": "block", "font_size": 14},
            "& span": {"font_size": 12.5, "opacity": .65}},
    body={"padding_top": 28, "min_width": 0,
          "& h2": {"margin": "42px 0 10px", "font_size": 25, "letter_spacing": "-.02em",
                   "padding_top": 12, "border_top": "1px solid rgba(127,127,127,.18)"},
          "& h3": {"margin": "26px 0 8px", "font_size": 18},
          "& p": {"margin": "12px 0"},
          "& ul": {"margin": "12px 0", "padding_left": 22},
          "& li": {"margin": "5px 0"},
          "& table": {"border_collapse": "collapse", "margin": "16px 0", "font_size": 14.5,
                      "width": "100%"},
          "& th": {"border": "1px solid rgba(127,127,127,.28)", "padding": "7px 10px",
                   "text_align": "left", "vertical_align": "top"},
          "& td": {"border": "1px solid rgba(127,127,127,.28)", "padding": "7px 10px",
                   "text_align": "left", "vertical_align": "top"},
          "& blockquote": {"margin": "16px 0", "padding": "10px 16px",
                           "border_left": "3px solid #d0a215",
                           "background": "rgba(208,162,21,.09)",
                           "border_radius": "0 8px 8px 0"}},
    code={"margin": "14px 0", "padding": "14px 16px", "border_radius": 11, "overflow_x": "auto",
          "background": "#12161c", "color": "#dbe3ee", "font_size": 13.5, "line_height": 1.55,
          "font_family": "ui-monospace, SFMono-Regular, Menlo, monospace"},
    inline={"padding": "1px 5px", "border_radius": 5, "background": "rgba(127,127,127,.16)",
            "font_size": ".92em", "font_family": "ui-monospace, SFMono-Regular, Menlo, monospace"},
    cards={"display": "grid", "gap": 14,
           "grid_template_columns": "repeat(auto-fill, minmax(250px, 1fr))"},
    card={"border": "1px solid rgba(127,127,127,.25)", "border_radius": 12, "padding": "14px 16px",
          "& h3": {"margin": "0 0 4px", "font_size": 16},
          "& p": {"margin": 0, "font_size": 14, "opacity": .78}},
    foot={"max_width": 1100, "margin": "0 auto", "padding": "24px 20px 60px", "opacity": .6,
          "font_size": 13.5},
)


# -- a small markdown renderer -----------------------------------------------------------

_INLINE = re.compile(r"(`[^`]+`|\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*)")


def inline(text: str):
    """Render `code`, [links](...) and **bold**; everything else is plain text."""
    out = []
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("`") and piece.endswith("`"):
            out.append(code(piece[1:-1], class_=s.inline))
        elif piece.startswith("**") and piece.endswith("**"):
            out.append(strong(piece[2:-2]))
        elif piece.startswith("[") and "](" in piece:
            label, _, href = piece[1:-1].partition("](")
            out.append(a(label, href=_rewrite(href)))
        else:
            out.append(piece)
    return out


def _rewrite(href: str) -> str:
    """Point in-repo links at this site or at GitHub."""
    if href.startswith("#") or href.startswith("http"):
        return href
    if "examples/patterns" in href:
        name = href.rsplit("/", 1)[-1]
        return f"/patterns/{name[:-3]}" if name.endswith(".py") else "/patterns"
    return "https://github.com/slimboi34/jongo/blob/main/" + href.lstrip("./")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse(markdown: str) -> list[dict]:
    """Markdown -> a list of blocks. Enough of it for this guide, and no more."""
    blocks: list[dict] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            body, index = [], index + 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks.append({"kind": "code", "text": "\n".join(body)})
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            blocks.append({"kind": f"h{level}", "text": line[level:].strip()})
        elif line.startswith("|"):
            rows, index2 = [], index
            while index2 < len(lines) and lines[index2].startswith("|"):
                rows.append([cell.strip() for cell in lines[index2].strip("|").split("|")])
                index2 += 1
            index = index2 - 1
            blocks.append({"kind": "table", "rows": [r for r in rows if not _is_rule(r)]})
        elif line.startswith("> "):
            quote, index2 = [], index
            while index2 < len(lines) and lines[index2].startswith(">"):
                quote.append(lines[index2].lstrip("> ").rstrip())
                index2 += 1
            index = index2 - 1
            blocks.append({"kind": "quote", "text": " ".join(quote)})
        elif re.match(r"^\s*[-*] ", line):
            items, index2 = [], index
            while index2 < len(lines) and (re.match(r"^\s*[-*] ", lines[index2])
                                           or (items and lines[index2].startswith("  "))):
                if re.match(r"^\s*[-*] ", lines[index2]):
                    items.append(re.sub(r"^\s*[-*] ", "", lines[index2]))
                else:
                    items[-1] += " " + lines[index2].strip()
                index2 += 1
            index = index2 - 1
            blocks.append({"kind": "list", "items": items})
        elif line.startswith("---") and line.strip("-") == "":
            pass
        elif line.strip():
            para, index2 = [], index
            while index2 < len(lines) and lines[index2].strip() and not _starts_block(lines[index2]):
                para.append(lines[index2].strip())
                index2 += 1
            index = index2 - 1
            blocks.append({"kind": "p", "text": " ".join(para)})
        index += 1
    return blocks


def _is_rule(cells: list[str]) -> bool:
    return all(set(cell) <= set("-: ") and cell for cell in cells)


def _starts_block(line: str) -> bool:
    return line.startswith(("#", "```", "|", "> ")) or bool(re.match(r"^\s*[-*] ", line))


def render(blocks: list[dict]):
    out = []
    for block in blocks:
        kind = block["kind"]
        if kind == "h1":
            out.append(h1(block["text"]))
        elif kind == "h2":
            out.append(h2(*inline(block["text"]), id=slug(block["text"])))
        elif kind in ("h3", "h4"):
            out.append(h3(*inline(block["text"]), id=slug(block["text"])))
        elif kind == "p":
            out.append(p(*inline(block["text"])))
        elif kind == "quote":
            from jongo.html import blockquote
            out.append(blockquote(*inline(block["text"])))
        elif kind == "code":
            out.append(pre(code(block["text"]), class_=s.code))
        elif kind == "list":
            out.append(ul([li(*inline(item)) for item in block["items"]]))
        elif kind == "table" and block["rows"]:
            head, *rest = block["rows"]
            out.append(table(
                thead(tr([th(*inline(cell)) for cell in head])),
                tbody([tr([td(*inline(cell)) for cell in row]) for row in rest]),
            ))
    return out


# -- the index that search runs over -----------------------------------------------------


def build_index(blocks: list[dict]) -> list[dict]:
    """One entry per section: its heading, its anchor and its text."""
    entries: list[dict] = []
    for block in blocks:
        if block["kind"] in ("h2", "h3"):
            entries.append({"title": block["text"], "anchor": slug(block["text"]), "text": ""})
        elif entries and block["kind"] in ("p", "list", "quote", "code"):
            extra = block.get("text") or " ".join(block.get("items", []))
            entries[-1]["text"] = (entries[-1]["text"] + " " + extra)[:600]
    return entries


@component
def Search(entries):
    """Filter the guide in the browser — no round trip, no search service."""
    query = state("")

    def matches():
        text = query.value.strip().lower()
        if len(text) < 2:
            return []
        found = []
        for entry in entries:
            haystack = (entry["title"] + " " + entry["text"]).lower()
            if text in haystack:
                found.append(entry)
        return found[:8]

    results = matches()
    return div(
        input_(class_=s.search, placeholder="Search the guide", value=query.value,
               on_input=lambda e: query.set(e.target.value)),
        div(
            [a(strong(entry["title"]), span(entry["text"][:90] + "…"),
               href="#" + entry["anchor"], class_=s.result, key=entry["anchor"])
             for entry in results],
            class_=s.results,
        ) if results else (
            div(p("No match."), class_=s.results) if len(query.value.strip()) >= 2 else None
        ),
    )


# -- pages --------------------------------------------------------------------------------


def guide_blocks():
    return parse(GUIDE.read_text())


def shell(body, blocks):
    headings = [b for b in blocks if b["kind"] == "h2"]
    return div(
        header(
            div(h1("Jongo"), p("Documentation — and this page is a Jongo app."),
                class_=s.masthead),
            class_=s.shell, style={"padding_bottom": 0},
        ),
        div(
            aside(
                Search(entries=build_index(blocks)),
                nav(ul([li(a(b["text"], href="#" + slug(b["text"]))) for b in headings])),
                ul([li(a("Patterns", href="/patterns")),
                    li(a("GitHub", href="https://github.com/slimboi34/jongo"))]),
                class_=s.side,
            ),
            main(article(*body, class_=s.body)),
            class_=s.shell,
        ),
        footer(p("© 2026 Joshua Harty · AGPL-3.0-or-later"), class_=s.foot),
    )


@app.page("/")
def home():
    blocks = guide_blocks()
    return shell(render(blocks), blocks)


@app.page("/patterns")
def patterns():
    blocks = guide_blocks()
    cards = []
    for path in sorted(PATTERNS.glob("*.py")):
        summary = _summary(path)
        cards.append(div(h3(a(path.stem.replace("_", " "), href=f"/patterns/{path.stem}")),
                         p(summary), class_=s.card, key=path.stem))
    return shell([h1("Patterns"),
                  p("Complete, runnable apps. Each one is exercised by the test suite."),
                  div(cards, class_=s.cards)], blocks)


@app.page("/patterns/<name>")
def pattern(name: str):
    blocks = guide_blocks()
    path = PATTERNS / f"{name}.py"
    if not path.is_file() or path.parent != PATTERNS:
        return shell([h1("Not found"), p("No such pattern."), a("Back", href="/patterns")], blocks)
    return shell([h1(name.replace("_", " ")),
                  p(a("← all patterns", href="/patterns")),
                  pre(code(path.read_text()), class_=s.code)], blocks)


def _summary(path: Path) -> str:
    """The first line of the module docstring."""
    text = path.read_text()
    if text.startswith('"""'):
        return text[3:].split("\n", 1)[0].strip()
    return path.stem
