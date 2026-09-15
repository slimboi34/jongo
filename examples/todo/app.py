"""A todo app where the database, server code and interactive UI live in one Python file.

    cd examples/todo
    jongo dev
"""
from jongo import HTTPError, Jongo, NotFound, Page, ServerError, component, css, db, effect, global_css, js, server, state
from jongo.html import *

app = Jongo(__name__, title="Jongo Todos", database="todos.sqlite3")
app.admin()  # /admin — create an account with `jongo createadmin`


# ---- data ------------------------------------------------------------------------


class Todo(db.Model):
    title = db.Text(max_length=200)
    done = db.Bool(default=False)
    created = db.DateTime(auto_now_add=True)

    class Meta:
        ordering = ["-created"]


# ---- server functions: called straight from the components below -------------------


@server
def add_todo(title: str) -> dict:
    title = title.strip()
    if not title:
        raise HTTPError(400, "Give your todo a title first")
    return Todo.create(title=title).to_dict()


@server
def toggle_todo(todo: Todo) -> dict:
    todo.done = not todo.done
    todo.save()
    return todo.to_dict()


@server
def delete_todo(todo: Todo) -> None:
    todo.delete()


@server
def clear_done() -> int:
    return Todo.filter(done=True).delete()


# ---- styles ------------------------------------------------------------------------

global_css({
    "*": {"box_sizing": "border-box"},
    "body": {
        "margin": 0,
        "background": "#f6f1e9",
        "color": "#221e1a",
        "font": "16px/1.55 ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif",
    },
    "a": {"color": "inherit"},
    "button, input": {"font": "inherit"},
})

s = css(
    shell={"max_width": 680, "margin": "0 auto", "padding": "28px 18px 80px"},
    header={"display": "flex", "align_items": "center", "justify_content": "space-between", "margin_bottom": 36},
    brand={
        "display": "flex", "align_items": "center", "gap": 10, "font_weight": 750,
        "font_size": 19, "text_decoration": "none", "letter_spacing": "-0.02em",
    },
    mark={
        "display": "grid", "place_items": "center", "width": 30, "height": 30, "border_radius": 9,
        "background": "#e4572e", "color": "white", "font_size": 15,
    },
    nav={"display": "flex", "gap": 6, "& a": {
        "text_decoration": "none", "padding": "6px 12px", "border_radius": 999, "color": "#6b635b",
        ":hover": {"background": "#ebe3d7", "color": "#221e1a"},
    }},
    hero={"margin": "0 0 22px", "& h1": {
        "font_size": "clamp(34px, 7vw, 52px)", "line_height": 1.02, "letter_spacing": "-0.04em", "margin": "0 0 10px",
    }, "& p": {"margin": 0, "color": "#6b635b"}},
    card={
        "background": "#fffdf9", "border": "1px solid #e8dfd2", "border_radius": 18,
        "box_shadow": "0 1px 0 #efe7db, 0 18px 40px -24px rgba(60, 40, 20, .35)", "overflow": "hidden",
    },
    composer={"display": "flex", "gap": 10, "padding": 14, "border_bottom": "1px solid #efe7db"},
    input={
        "flex": 1, "min_width": 0, "border": "1px solid #e0d6c8", "border_radius": 11, "padding": "11px 14px",
        "background": "white", "outline": "none", ":focus": {"border_color": "#e4572e", "box_shadow": "0 0 0 3px #fbd9cd"},
    },
    primary={
        "border": 0, "border_radius": 11, "padding": "0 18px", "background": "#221e1a", "color": "white",
        "font_weight": 600, "cursor": "pointer", ":disabled": {"opacity": 0.35, "cursor": "default"},
    },
    error={"margin": 0, "padding": "10px 16px", "background": "#fdeae4", "color": "#b53a17", "font_size": 14},
    list={"list_style": "none", "margin": 0, "padding": 0},
    empty={"padding": "38px 16px", "text_align": "center", "color": "#8d847a"},
    item={
        "display": "flex", "align_items": "center", "gap": 12, "padding": "12px 16px",
        "border_bottom": "1px solid #f1eadf", ":hover button": {"opacity": 1},
    },
    check={"width": 19, "height": 19, "accent_color": "#e4572e", "cursor": "pointer", "flex": "none"},
    title={"flex": 1, "min_width": 0, "overflow_wrap": "anywhere"},
    done={"text_decoration": "line-through", "color": "#a39a8f"},
    details={"font_size": 13, "color": "#8d847a", "text_decoration": "none", ":hover": {"color": "#e4572e"}},
    delete={
        "border": 0, "background": "none", "font_size": 20, "line_height": 1, "color": "#b9ada0",
        "cursor": "pointer", "opacity": 0.4, "padding": 4, ":hover": {"color": "#e4572e"},
    },
    footer={
        "display": "flex", "align_items": "center", "justify_content": "space-between", "gap": 10,
        "flex_wrap": "wrap", "padding": "10px 16px", "font_size": 14, "color": "#6b635b",
    },
    filters={"display": "flex", "gap": 4},
    filter={
        "border": "1px solid transparent", "background": "none", "border_radius": 999, "padding": "4px 11px",
        "cursor": "pointer", "color": "inherit", ":hover": {"border_color": "#e0d6c8"},
    },
    active={"border_color": "#221e1a !important", "color": "#221e1a"},
    ghost={"border": 0, "background": "none", "color": "inherit", "cursor": "pointer", ":hover": {"color": "#e4572e"}},
    timer={
        "display": "flex", "align_items": "center", "justify_content": "space-between", "gap": 12,
        "margin_top": 18, "padding": "16px 18px", "& strong": {
            "font": "700 34px/1 ui-monospace, SFMono-Regular, Menlo, monospace", "letter_spacing": "-0.03em",
        },
    },
    prose={"padding": "8px 24px 20px", "& code": {"background": "#f1eadf", "padding": "1px 6px", "border_radius": 6}},
)


# ---- components ----------------------------------------------------------------------


@app.layout
@component
def Layout(children):
    return div(
        header(
            a(span("◆", class_=s.mark), "Jongo Todos", href="/", class_=s.brand),
            nav(a("Todos", href="/"), a("About", href="/about"), class_=s.nav),
            class_=s.header,
        ),
        main(children),
        class_=s.shell,
    )


@component
def TodoItem(todo, on_toggle, on_delete):
    return li(
        input_(type="checkbox", checked=todo["done"], on_change=lambda e: on_toggle(todo), class_=s.check,
               aria_label=f"Mark {todo['title']} done"),
        span(todo["title"], class_=[s.title, {s.done: todo["done"]}]),
        a("details", href=f"/todos/{todo['id']}", class_=s.details),
        button("×", on_click=lambda e: on_delete(todo), class_=s.delete, aria_label="Delete"),
        class_=s.item,
    )


@component
def TodoApp(initial):
    todos = state(initial)
    draft = state("")
    show = state("all")
    error = state(None)

    remaining = len([t for t in todos.value if not t["done"]])

    def update_title():
        js.document.title = f"{remaining} left · Jongo Todos"

    effect(update_title, [remaining])

    async def add(event):
        try:
            todo = await add_todo(draft.value)
        except ServerError as exc:
            error.value = str(exc)
            return
        todos.value = [todo] + todos.value
        draft.value = ""
        error.value = None

    async def toggle(todo):
        updated = await toggle_todo(todo["id"])
        todos.value = [updated if t["id"] == todo["id"] else t for t in todos.value]

    async def remove(todo):
        todos.value = [t for t in todos.value if t["id"] != todo["id"]]
        await delete_todo(todo["id"])

    async def clear():
        await clear_done()
        todos.value = [t for t in todos.value if not t["done"]]

    visible = [t for t in todos.value if show.value == "all" or (show.value == "done") == t["done"]]

    return section(
        form(
            input_(
                value=draft.value,
                placeholder="What needs doing?",
                on_input=lambda e: draft.set(e.target.value),
                class_=s.input,
                aria_label="New todo",
            ),
            button("Add", class_=s.primary, disabled=not draft.value.strip()),
            on_submit=add,
            class_=s.composer,
        ),
        error.value and p(error.value, class_=s.error),
        ul([TodoItem(todo=t, on_toggle=toggle, on_delete=remove, key=t["id"]) for t in visible], class_=s.list)
        if visible
        else p("Nothing here yet." if show.value == "all" else f"No {show.value} todos.", class_=s.empty),
        footer(
            span(f"{remaining} left"),
            div(
                [
                    button(name.title(), on_click=lambda e, name=name: show.set(name),
                           class_=[s.filter, {s.active: show.value == name}])
                    for name in ["all", "active", "done"]
                ],
                class_=s.filters,
            ),
            button("Clear done", on_click=lambda e: clear(), class_=s.ghost),
            class_=s.footer,
        ),
        class_=s.card,
    )


@component
def FocusTimer(minutes=25):
    seconds = state(minutes * 60)
    running = state(False)

    def run():
        if not running.value:
            return None
        timer = js.setInterval(lambda: seconds.update(lambda left: max(0, left - 1)), 1000)
        return lambda: js.clearInterval(timer)

    effect(run, [running.value])
    mins, secs = divmod(seconds.value, 60)

    return div(
        strong(f"{mins:02d}:{secs:02d}"),
        div(
            button("Pause" if running.value else "Start focus", on_click=lambda e: running.set(not running.value),
                   class_=s.primary, style={"padding": "10px 16px"}),
            button("Reset", on_click=lambda e: seconds.set(minutes * 60), class_=s.ghost, style={"margin_left": 8}),
        ),
        class_=[s.card, s.timer],
    )


# ---- pages ------------------------------------------------------------------------------


@app.page("/")
def home():
    return div(
        div(h1("Get it done."), p("Everything on this page — UI, server calls, database — is one Python file."),
            class_=s.hero),
        TodoApp(initial=Todo.all()),
    )


@app.page("/todos/<id>")
def todo_page(id: int):
    todo = Todo.filter(id=id).first()
    if todo is None:
        raise NotFound("That todo doesn't exist (maybe you deleted it?)")
    return Page(
        div(
            div(p(a("← All todos", href="/")), h1(todo.title),
                p(f"{'Done' if todo.done else 'Not done yet'} · added {todo.created:%b %d, %Y at %H:%M} UTC"),
                class_=s.hero),
            FocusTimer(minutes=25),
        ),
        title=todo.title,
    )


@app.page("/about", title="About · Jongo Todos")
def about():
    return div(
        div(h1("One language."), p("A tiny tour of what powers this app."), class_=s.hero),
        div(
            ul(
                li(code("@component"), " functions render on the server, then compile to JavaScript and hydrate."),
                li(code("state()"), " and ", code("effect()"), " make them reactive in the browser."),
                li(code("@server"), " functions are called from event handlers with ", code("await"),
                   " — arguments are type-checked, ", code("todo: Todo"), " loads the row."),
                li("Links switch pages without a full reload, and the layout keeps its state."),
            ),
            class_=[s.card, s.prose],
        ),
    )
