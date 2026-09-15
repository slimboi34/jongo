"""An admin site generated from your models.

    app = Jongo(__name__, database="db.sqlite3")
    app.admin()            # -> /admin, for users with is_admin=True

Create the first admin with ``jongo createadmin``.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime

from . import auth, db
from .app import Page
from .errors import NotFound
from .html import *
from .http import CSRF_FIELD, redirect
from .styles import css

PER_PAGE = 50

S = css(
    shell={
        "display": "grid", "grid_template_columns": "240px 1fr", "min_height": "100vh",
        "font": "14px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif", "color": "#1d1b20", "background": "#f7f6f3",
        "@media (max-width: 760px)": {"grid_template_columns": "1fr"},
    },
    side={
        "background": "#1c1a1f", "color": "#d8d3dd", "padding": "22px 14px",
        "& a": {"display": "block", "color": "inherit", "text_decoration": "none", "padding": "7px 10px", "border_radius": 8},
        "& a:hover": {"background": "#2c2831", "color": "white"},
    },
    brand={"font_weight": 700, "color": "white", "font_size": 16, "padding": "0 10px 18px", "display": "flex", "gap": 8},
    mark={"color": "#e4572e"},
    here={"background": "#2c2831", "color": "white !important"},
    section_label={"text_transform": "uppercase", "font_size": 11, "letter_spacing": ".08em", "color": "#857e8c",
                   "padding": "14px 10px 6px"},
    main={"padding": "26px clamp(16px, 4vw, 44px) 60px", "min_width": 0},
    top={"display": "flex", "justify_content": "space-between", "align_items": "center", "gap": 12, "flex_wrap": "wrap",
         "margin_bottom": 22, "& h1": {"margin": 0, "font_size": 26, "letter_spacing": "-.02em"}},
    user={"display": "flex", "gap": 10, "align_items": "center", "color": "#6f6a74"},
    card={"background": "white", "border": "1px solid #e6e2dc", "border_radius": 12, "overflow": "hidden"},
    grid={"display": "grid", "grid_template_columns": "repeat(auto-fill, minmax(210px, 1fr))", "gap": 14},
    stat={"display": "block", "padding": 18, "text_decoration": "none", "color": "inherit",
          ":hover": {"border_color": "#e4572e"}, "& strong": {"display": "block", "font_size": 28, "letter_spacing": "-.02em"}},
    toolbar={"display": "flex", "gap": 8, "flex_wrap": "wrap", "margin_bottom": 14},
    table_wrap={"overflow_x": "auto"},
    table={"width": "100%", "border_collapse": "collapse", "& th, & td": {
        "text_align": "left", "padding": "10px 14px", "border_bottom": "1px solid #efece7", "white_space": "nowrap",
    }, "& th": {"font_size": 12, "color": "#6f6a74", "font_weight": 600, "background": "#fbfaf8"},
        "& th a": {"color": "inherit"}, "& tr:hover td": {"background": "#fdfbf7"}, "& td a": {"color": "#c2410c", "font_weight": 600}},
    input={"padding": "8px 11px", "border": "1px solid #dcd6ce", "border_radius": 8, "font": "inherit", "background": "white",
           "min_width": 0, ":focus": {"outline": "2px solid #fbd0c0", "border_color": "#e4572e"}},
    button={"padding": "8px 14px", "border": "1px solid #1d1b20", "border_radius": 8, "background": "#1d1b20",
            "color": "white", "font": "inherit", "font_weight": 600, "cursor": "pointer", "text_decoration": "none",
            "display": "inline-block"},
    secondary={"background": "white !important", "color": "#1d1b20 !important", "border_color": "#dcd6ce !important"},
    danger={"background": "#b42318 !important", "border_color": "#b42318 !important"},
    link_button={"background": "none", "border": 0, "color": "#6f6a74", "cursor": "pointer", "font": "inherit", "padding": 0,
                 ":hover": {"color": "#1d1b20"}},
    form={"padding": 22, "display": "grid", "gap": 16, "max_width": 640},
    field={"display": "grid", "gap": 5, "& label": {"font_weight": 600}, "& small": {"color": "#8a8490"},
           "& textarea, & select": {"font": "inherit"}},
    error={"color": "#b42318", "margin": 0, "font_size": 13},
    flash={"background": "#ecfdf3", "border": "1px solid #abefc6", "color": "#067647", "padding": "10px 14px",
           "border_radius": 10, "margin_bottom": 16},
    muted={"color": "#8a8490"},
    pager={"display": "flex", "gap": 10, "align_items": "center", "padding": "12px 14px", "color": "#6f6a74"},
    login={"min_height": "100vh", "display": "grid", "place_items": "center", "background": "#1c1a1f",
           "font": "14px/1.5 ui-sans-serif, system-ui, sans-serif", "padding": 16},
)


def _display(value) -> str:
    if value is None:
        return "—"
    if value is True:
        return "✓"
    if value is False:
        return "✗"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value)[:60]
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


class Admin:
    def __init__(self, app, path="/admin", *, models=None, title="Jongo admin"):
        self.app = app
        self.path = path.rstrip("/")
        self.title = title
        self._models = models
        self._register()

    # -- helpers --------------------------------------------------------------------

    @property
    def models(self):
        models = self._models if self._models is not None else db.models_registry.values()
        return sorted(models, key=lambda m: m._meta.name)

    def slug(self, model) -> str:
        return model._meta.name.lower()

    def model_for(self, slug: str):
        for model in self.models:
            if self.slug(model) == slug.lower():
                return model
        raise NotFound(f"No model called {slug!r}")

    def url(self, *parts) -> str:
        return "/".join([self.path, *(str(p) for p in parts)])

    def denied(self, request):
        if not getattr(request.user, "is_admin", False):
            return redirect(f"{self.path}/login?next={urllib.parse.quote(request.full_path)}", 302)
        return None

    def flash(self, request, message: str):
        request.session["_flash"] = message

    def csrf(self, request):
        return input_(type="hidden", name=CSRF_FIELD, value=request.csrf_token)

    def shell(self, request, heading: str, *content, actions=None, current=None) -> Page:
        message = request.session.pop("_flash") if "_flash" in request.session else None
        side = aside(
            div(span("◆", class_=S.mark), self.title, class_=S.brand),
            a("Dashboard", href=self.path, class_={S.here: current is None}),
            div("Models", class_=S.section_label),
            [
                a(model._meta.verbose_name_plural.capitalize(), href=self.url(self.slug(model)),
                  class_={S.here: current is model})
                for model in self.models
            ],
            div("Site", class_=S.section_label),
            a("View site ↗", href="/", data_reload=True),
            class_=S.side,
        )
        top = div(
            h1(heading),
            div(
                actions,
                span(str(request.user)),
                form(self.csrf(request), button("Log out", class_=S.link_button), method="post",
                     action=self.url("logout")),
                class_=S.user,
            ),
            class_=S.top,
        )
        body = main(top, message and div(message, class_=S.flash), *content, class_=S.main)
        return Page(div(side, body, class_=S.shell), title=f"{heading} · {self.title}")

    # -- forms ------------------------------------------------------------------------

    def editable_fields(self, model):
        return [f for f in model._meta.fields if f.editable]

    def control(self, field, value):
        name = field.name
        attrs = {"id": f"f-{name}", "name": name, "class_": S.input}
        text = "" if value is None else str(value)
        if field.kind == "bool":
            return input_(type="checkbox", checked=value not in (None, False, "", "false", "0"), id=f"f-{name}", name=name)
        if field.choices:
            options = [] if field.required else [option("—", value="")]
            options += [option(label, value=str(v), selected=str(v) == text) for v, label in field.choices]
            return select(options, **attrs)
        if field.kind == "fk":
            options = [option("—", value="")] if field.null else []
            options += [
                option(str(obj), value=str(obj.pk), selected=str(obj.pk) == text)
                for obj in field.related_model.all()[:500]
            ]
            return select(options, **attrs)
        if field.kind in ("int", "float"):
            return input_(type="number", value=text, step="1" if field.kind == "int" else "any", **attrs)
        if field.kind == "datetime":
            if isinstance(value, datetime):
                text = value.strftime("%Y-%m-%dT%H:%M")
            return input_(type="datetime-local", value=text, **attrs)
        if field.kind == "date":
            return input_(type="date", value=text, **attrs)
        if field.kind == "json":
            if not isinstance(value, str):
                text = json.dumps(value, indent=2) if value is not None else ""
            return textarea(value=text, rows=6, **attrs)
        if getattr(field, "max_length", None) is None:
            return textarea(value=text, rows=4, **attrs)
        return input_(type="email" if "email" in name else "text", value=text, maxlength=field.max_length, **attrs)

    def form_view(self, request, model, obj, values, errors):
        is_user = model is auth.User
        fields = []
        for field in self.editable_fields(model):
            fields.append(
                div(
                    field.kind == "bool" and label(self.control(field, values.get(field.name)), " ", field.label)
                    or [label(field.label, for_=f"f-{field.name}"), self.control(field, values.get(field.name))],
                    field.help and small(field.help),
                    errors.get(field.name) and p(errors[field.name], class_=S.error),
                    class_=S.field,
                )
            )
        if is_user:
            fields.append(div(
                label("New password" if obj.pk else "Password", for_="f-password"),
                input_(type="password", id="f-password", name="_password", autocomplete="new-password", class_=S.input),
                small("Leave blank to keep the current password") if obj.pk else None,
                class_=S.field,
            ))
        readonly = [f for f in model._meta.fields if not f.editable and f.name != "password" and obj.pk]
        return div(
            errors.get("__all__") and p(errors["__all__"], class_=S.error),
            form(
                self.csrf(request),
                fields,
                readonly and div([p(strong(f.label, ": "), _display(getattr(obj, f.name))) for f in readonly],
                                 class_=S.muted),
                div(
                    button("Save", class_=S.button),
                    " ",
                    a("Cancel", href=self.url(self.slug(model)), class_=[S.button, S.secondary]),
                    obj.pk and [" ", a("Delete", href=self.url(self.slug(model), obj.pk, "delete"),
                                       class_=[S.button, S.danger])],
                ),
                method="post",
                class_=S.form,
            ),
            class_=S.card,
        )

    def save_from_form(self, request, model, obj):
        errors = {}
        for field in self.editable_fields(model):
            try:
                setattr(obj, field.attname, field.clean(request.form.get(field.name)))
            except db.ValidationError as exc:
                errors[field.name] = getattr(exc, "message", None) or str(exc)
        new_password = request.form.get("_password")
        if model is auth.User:
            if new_password:
                obj.set_password(new_password)
            elif not obj.pk:
                errors["_password"] = errors["__all__"] = "Set a password for the new user"
        if not errors:
            try:
                obj.save()
            except db.ValidationError as exc:
                errors.update(exc.errors)
        return errors

    # -- routes -------------------------------------------------------------------------

    def _register(self):
        app, admin = self.app, self

        @app.page(f"{self.path}/login", layout=False, title="Log in")
        def admin_login(request, next: str = ""):
            return admin.login_page(request, next)

        @app.post(f"{self.path}/login")
        def admin_login_post(request):
            username = request.form.get("username", "")
            user = auth.authenticate(username, request.form.get("password", ""))
            destination = request.form.get("next") or admin.path
            if not destination.startswith("/") or destination.startswith("//"):
                destination = admin.path
            if user is None or not user.is_admin:
                return app.render_page(request, admin.login_page(request, destination, "Wrong username or password, or not an admin."))
            auth.login(request, user)
            return redirect(destination)

        @app.post(f"{self.path}/logout")
        def admin_logout(request):
            auth.logout(request)
            return redirect(f"{admin.path}/login")

        @app.page(self.path, layout=False)
        def admin_dashboard(request):
            return admin.denied(request) or admin.shell(
                request,
                "Dashboard",
                div(
                    [
                        a(span(m._meta.verbose_name_plural.capitalize(), class_=S.muted), strong(str(m.objects.count())),
                          href=admin.url(admin.slug(m)), class_=[S.card, S.stat])
                        for m in admin.models
                    ],
                    class_=S.grid,
                ),
            )

        @app.page(f"{self.path}/<model>", layout=False)
        def admin_list(request, model: str, q: str = "", page: int = 1, o: str = ""):
            return admin.denied(request) or admin.list_page(request, admin.model_for(model), q, page, o)

        @app.page(f"{self.path}/<model>/new", layout=False)
        def admin_new(request, model: str):
            if denied := admin.denied(request):
                return denied
            cls = admin.model_for(model)
            obj = cls()
            values = {f.name: getattr(obj, f.attname, None) for f in admin.editable_fields(cls)}
            return admin.shell(request, f"New {cls._meta.verbose_name}", admin.form_view(request, cls, obj, values, {}),
                               current=cls)

        @app.post(f"{self.path}/<model>/new")
        def admin_create(request, model: str):
            if denied := admin.denied(request):
                return denied
            cls = admin.model_for(model)
            obj = cls()
            errors = admin.save_from_form(request, cls, obj)
            if errors:
                page = admin.shell(request, f"New {cls._meta.verbose_name}",
                                   admin.form_view(request, cls, obj, dict(request.form), errors), current=cls)
                page.status = 400
                return app.render_page(request, page)
            admin.flash(request, f"Created {obj}.")
            return redirect(admin.url(model))

        @app.page(f"{self.path}/<model>/<id>", layout=False)
        def admin_edit(request, model: str, id: int):
            if denied := admin.denied(request):
                return denied
            cls = admin.model_for(model)
            obj = cls.filter(id=id).first() or admin._missing(cls, id)
            values = {f.name: getattr(obj, f.attname) for f in admin.editable_fields(cls)}
            return admin.shell(request, str(obj), admin.form_view(request, cls, obj, values, {}), current=cls)

        @app.post(f"{self.path}/<model>/<id>")
        def admin_update(request, model: str, id: int):
            if denied := admin.denied(request):
                return denied
            cls = admin.model_for(model)
            obj = cls.filter(id=id).first() or admin._missing(cls, id)
            errors = admin.save_from_form(request, cls, obj)
            if errors:
                page = admin.shell(request, str(obj), admin.form_view(request, cls, obj, dict(request.form), errors),
                                   current=cls)
                page.status = 400
                return app.render_page(request, page)
            admin.flash(request, f"Saved {obj}.")
            return redirect(admin.url(model))

        @app.page(f"{self.path}/<model>/<id>/delete", layout=False)
        def admin_confirm_delete(request, model: str, id: int):
            if denied := admin.denied(request):
                return denied
            cls = admin.model_for(model)
            obj = cls.filter(id=id).first() or admin._missing(cls, id)
            return admin.shell(
                request,
                f"Delete {obj}?",
                div(
                    form(
                        p("This can't be undone."),
                        admin.csrf(request),
                        button("Delete", class_=[S.button, S.danger]),
                        " ",
                        a("Cancel", href=admin.url(model, id), class_=[S.button, S.secondary]),
                        method="post",
                        class_=S.form,
                    ),
                    class_=S.card,
                ),
                current=cls,
            )

        @app.post(f"{self.path}/<model>/<id>/delete")
        def admin_delete(request, model: str, id: int):
            if denied := admin.denied(request):
                return denied
            cls = admin.model_for(model)
            obj = cls.filter(id=id).first() or admin._missing(cls, id)
            label = str(obj)
            obj.delete()
            admin.flash(request, f"Deleted {label}.")
            return redirect(admin.url(model))

    @staticmethod
    def _missing(model, pk):
        raise NotFound(f"{model._meta.verbose_name.capitalize()} #{pk} doesn't exist")

    # -- pages ----------------------------------------------------------------------------

    def login_page(self, request, next_url="", error=None):
        return Page(
            div(
                form(
                    div(span("◆", class_=S.mark), self.title, class_=S.brand, style={"color": "#1d1b20", "padding": 0}),
                    error and p(error, class_=S.error),
                    self.csrf(request),
                    input_(type="hidden", name="next", value=next_url),
                    div(label("Username", for_="u"), input_(id="u", name="username", autofocus=True, class_=S.input),
                        class_=S.field),
                    div(label("Password", for_="p"), input_(id="p", name="password", type="password", class_=S.input),
                        class_=S.field),
                    button("Log in", class_=S.button),
                    method="post",
                    action=self.url("login"),
                    class_=[S.card, S.form],
                    style={"width": "min(360px, 100%)"},
                ),
                class_=S.login,
            ),
            title=f"Log in · {self.title}",
            status=400 if error else 200,
        )

    def list_page(self, request, model, q, page, order):
        meta = model._meta
        fields = [f for f in meta.fields if f.kind != "json" and f.name != "password"][:5]
        queryset = model.objects.all()
        text_fields = [f.name for f in meta.fields if f.kind == "text" and f.name != "password"]
        if q and text_fields:
            queryset = queryset.search(q, text_fields)
        sortable = {f.name for f in fields} | {"id"}
        if order.lstrip("-") in sortable:
            queryset = queryset.order_by(order)
        total = queryset.count()
        page = max(1, page)
        rows = list(queryset[(page - 1) * PER_PAGE : page * PER_PAGE])
        slug = self.slug(model)

        def sort_link(field):
            new = field.name if order != field.name else f"-{field.name}"
            arrow = " ↑" if order == field.name else " ↓" if order == f"-{field.name}" else ""
            query = urllib.parse.urlencode({k: v for k, v in (("q", q), ("o", new)) if v})
            return a(field.label + arrow, href=f"{self.url(slug)}?{query}")

        def page_link(number, text):
            query = urllib.parse.urlencode({k: v for k, v in (("q", q), ("o", order), ("page", number)) if v})
            return a(text, href=f"{self.url(slug)}?{query}")

        table_ = table(
            thead(tr(th(meta.verbose_name.capitalize()), [th(sort_link(f)) for f in fields])),
            tbody(
                [
                    tr(
                        td(a(_display(str(obj)), href=self.url(slug, obj.pk))),
                        [td(_display(getattr(obj, f.name))) for f in fields],
                        key=obj.pk,
                    )
                    for obj in rows
                ]
                or tr(td(f"No {meta.verbose_name_plural} yet." if not q else "Nothing matches.",
                         colspan=len(fields) + 1, class_=S.muted))
            ),
            class_=S.table,
        )
        pages = max(1, -(-total // PER_PAGE))
        return self.shell(
            request,
            meta.verbose_name_plural.capitalize(),
            form(
                input_(name="q", value=q, placeholder=f"Search {meta.verbose_name_plural}", class_=S.input,
                       type="search", disabled=not text_fields),
                button("Search", class_=[S.button, S.secondary]),
                method="get",
                class_=S.toolbar,
            ),
            div(
                div(table_, class_=S.table_wrap),
                div(
                    f"{total} {meta.verbose_name if total == 1 else meta.verbose_name_plural}",
                    page > 1 and page_link(page - 1, "← Previous"),
                    pages > 1 and span(f"Page {page} of {pages}"),
                    page < pages and page_link(page + 1, "Next →"),
                    class_=S.pager,
                ),
                class_=S.card,
            ),
            actions=a(f"Add {meta.verbose_name}", href=self.url(slug, "new"), class_=S.button),
            current=model,
        )


def install(app, path="/admin", *, models=None, title="Jongo admin") -> Admin:
    return Admin(app, path, models=models, title=title)
