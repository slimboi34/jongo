"""JSON Schema and OpenAPI, generated from the type hints and models the app already has."""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import subprocess
import sys
import typing
import uuid
from pathlib import Path
from typing import Literal, Optional

import pytest

import jongo
from conftest import use_test_database
from jongo import Jongo, auth, db, forms, json_schema, openapi, rpc, server
from jongo.html import div
from jongo.introspect import describe
from jongo.rpc import coerce
from jongo.schema import form_schema, function_schema, model_schema
from jongo.testing import TestClient

HERE = Path(__file__).parent


@dataclasses.dataclass
class Point:
    x: int
    y: int = 0
    label: Optional[str] = None


@pytest.mark.parametrize("hint, expected", [
    (int, {"type": "integer"}),
    (float, {"type": "number"}),
    (str, {"type": "string"}),
    (bool, {"type": "boolean"}),
    (None, {"type": "null"}),
    (typing.Any, {}),
    (list[int], {"type": "array", "items": {"type": "integer"}}),
    (list, {"type": "array"}),
    (set[str], {"type": "array", "items": {"type": "string"}, "uniqueItems": True}),
    (tuple[int, str], {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}],
                       "minItems": 2, "maxItems": 2}),
    (tuple[int, ...], {"type": "array", "items": {"type": "integer"}}),
    (dict[str, float], {"type": "object", "additionalProperties": {"type": "number"}}),
    (dict, {"type": "object"}),
    (Optional[int], {"anyOf": [{"type": "integer"}, {"type": "null"}]}),
    (int | str | None, {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]}),
    (Literal["a", "b", 3], {"enum": ["a", "b", 3]}),
    (datetime.datetime, {"type": "string", "format": "date-time"}),
    (uuid.UUID, {"type": "string", "format": "uuid"}),
    (list[Optional[dict[str, list[int]]]], {"type": "array", "items": {"anyOf": [
        {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "integer"}}},
        {"type": "null"}]}}),
])
def test_type_hints_become_json_schema(hint, expected):
    assert json_schema(hint) == expected
    json.dumps(json_schema(hint))


def test_dataclasses_become_objects_with_required_fields():
    assert json_schema(Point) == {
        "type": "object", "title": "Point",
        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                       "label": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": ["x"],
    }


@pytest.fixture()
def app():
    saved_models, saved_functions = dict(db.models_registry), dict(rpc.SERVER_FUNCTIONS)
    db.models_registry.clear()
    rpc.SERVER_FUNCTIONS.clear()      # the registry is global: other test modules' functions are in it
    use_test_database()
    app = Jongo("schema_app", root=HERE, title="Shop", secret_key="k" * 40, dev=True)

    class Category(db.Model):
        name = db.Text(max_length=40)

    class Product(db.Model):
        """Something for sale."""

        name = db.Text(max_length=80)
        price = db.Float()
        status = db.Text(default="draft", choices=["draft", "live"])
        stock = db.Int(default=0)
        category = db.ForeignKey(Category, null=True, on_delete="SET NULL")
        notes = db.Text(null=True)
        added = db.DateTime(auto_now_add=True)

    Order = forms.Schema("Order", email=forms.Email(), quantity=forms.Int(min=1, max=99),
                         gift=forms.Bool(), size=forms.Choice(["s", "m", "l"], default="m"),
                         code=forms.Text(pattern=r"[A-Z]{3}\d", required=False, help="Optional promo code"))

    @server
    def add_product(request, name: str, price: float, tags: list[str] = [],
                    category: Optional[Category] = None) -> Product:
        """Add a product to the catalogue.

        Prices are in pounds.
        """
        return Product.create(name=name, price=price, category=category)

    @server(login_required=True, rate_limit="5/minute")
    def restock(request, product: Product, by: int = 1) -> dict:
        product.stock += by
        product.save()
        return product.to_dict()

    @server
    def place_order(request, order: Order) -> dict:
        return order

    @server
    def anything(*values, **options):
        return None

    @app.page("/products/<int:id>", title="Product")
    def product_page(request, id, tab: str = "details", page: int = 1):
        return div(str(id))

    @app.route("/api/products/<slug:kind>", methods=("GET", "POST"), login_required=True, rate_limit="10/minute")
    def api_products(request, kind) -> list[Product]:
        """List products of a kind."""
        return []

    @app.route("/hook", methods=("POST",), csrf=False)
    def hook(request):
        return {"ok": True}

    app.admin()
    db.migrate()
    app.t = type("T", (), dict(Product=Product, Category=Category, Order=Order, add_product=add_product,
                               restock=restock, place_order=place_order, anything=anything))
    yield app
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_models)
    rpc.SERVER_FUNCTIONS.clear()
    rpc.SERVER_FUNCTIONS.update(saved_functions)


def test_a_model_is_an_id_going_in_and_an_object_coming_out(app):
    Product = app.t.Product
    assert json_schema(Product) == {"type": "integer", "description": "id of a Product"}
    schema = json_schema(Product, output=True)
    assert schema == model_schema(Product)
    assert schema["title"] == "Product" and schema["description"] == "Something for sale."
    assert schema["properties"]["name"] == {"type": "string", "maxLength": 80}
    assert schema["properties"]["status"] == {"type": "string", "enum": ["draft", "live"]}
    assert schema["properties"]["category_id"] == {"anyOf": [
        {"type": "integer", "description": "id of a Category"}, {"type": "null"}]}
    assert schema["properties"]["notes"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert schema["properties"]["added"] == {"type": "string", "format": "date-time"}
    assert set(schema["required"]) == set(Product.create(name="x", price=1.0).to_dict())


def test_the_model_schema_leaves_out_what_to_dict_leaves_out():
    assert "password" not in model_schema(auth.User)["properties"]
    assert "username" in model_schema(auth.User)["properties"]
    assert "key" not in model_schema(auth.LoginSession)["properties"]


def test_a_form_schema_becomes_json_schema(app):
    assert form_schema(app.t.Order) == json_schema(app.t.Order) == {
        "type": "object", "title": "Order",
        "properties": {
            "email": {"type": "string", "format": "email", "maxLength": 254, "title": "Email"},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 99, "title": "Quantity"},
            "gift": {"type": "boolean", "title": "Gift"},
            "size": {"enum": ["s", "m", "l"], "title": "Size", "default": "m"},
            "code": {"type": "string", "pattern": "^(?:[A-Z]{3}[0-9])$", "title": "Code",
                     "description": "Optional promo code"},
        },
        "required": ["email", "quantity"],
    }


def test_a_server_functions_arguments_are_one_object(app):
    assert function_schema(app.t.add_product) == {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "price": {"type": "number"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            "category": {"anyOf": [{"type": "integer", "description": "id of a Category"}, {"type": "null"}],
                         "default": None},
        },
        "required": ["name", "price"],
        "additionalProperties": False,
    }, "request is supplied by the server and never appears"
    assert function_schema(app.t.anything) == {"type": "object", "properties": {}, "additionalProperties": True}


def test_the_schema_agrees_with_what_the_rpc_boundary_accepts(app):
    """A sample that satisfies each schema is accepted by coerce(); one that violates it is refused."""
    samples = [(int, 3, "x"), (float, 1.5, "x"), (str, "a", 3), (bool, True, "yes"), (list[int], [1, 2], [1, "a"]),
               (dict[str, int], {"a": 1}, {"a": "b"}), (Optional[int], None, "x"),
               (Literal["a", "b"], "a", "c"), (tuple[int, str], [1, "a"], [1])]
    for hint, good, bad in samples:
        coerce(good, hint)
        with pytest.raises(jongo.HTTPError):
            coerce(bad, hint)


def test_the_openapi_document_covers_functions_pages_and_routes(app):
    doc = openapi(app, version="1.4.0", server_url="https://shop.example")
    json.dumps(doc)
    assert doc["openapi"] == "3.1.0" and doc["info"] == {**doc["info"], "title": "Shop", "version": "1.4.0"}
    assert doc["servers"] == [{"url": "https://shop.example"}]
    assert set(doc["paths"]) == {
        f"/_jongo/rpc/{app.t.add_product.id}", f"/_jongo/rpc/{app.t.restock.id}",
        f"/_jongo/rpc/{app.t.place_order.id}", f"/_jongo/rpc/{app.t.anything.id}",
        "/products/{id}", "/api/products/{kind}", "/hook",
    }, "the framework's own routes and the admin stay out"

    add = doc["paths"][f"/_jongo/rpc/{app.t.add_product.id}"]["post"]
    assert add["operationId"] == "add_product" and add["summary"] == "Add a product to the catalogue."
    assert add["description"] == "Prices are in pounds."
    body = add["requestBody"]["content"]["application/json"]["schema"]
    # 0.7.0: the browser sends positional args, so neither key is required by itself; the
    # anyOf lists each way of covering name and price (all by name, mixed, all by position).
    assert "required" not in body["properties"]["kwargs"] and "required" not in body
    assert [shape["required"] for shape in body["anyOf"]] == [["kwargs"], ["args", "kwargs"], ["args"]]
    assert body["anyOf"][0]["properties"]["kwargs"]["required"] == ["name", "price"]
    assert body["properties"]["args"]["maxItems"] == 4 and "name, price, tags, category" in \
        body["properties"]["args"]["description"]
    ok = add["responses"]["200"]["content"]["application/json"]["schema"]["anyOf"][0]
    assert ok["properties"]["ok"] == {"$ref": "#/components/schemas/Product"}
    assert "404" in add["responses"], "a model argument can 404"
    assert add["security"] == [{"csrf": []}]

    restock = doc["paths"][f"/_jongo/rpc/{app.t.restock.id}"]["post"]
    assert {"401", "404", "429"} <= set(restock["responses"])
    assert restock["x-jongo"] == {"id": app.t.restock.id, "login_required": True, "admin_required": False,
                                  "refresh": False, "rate_limit": "5/minute"}
    assert restock["security"] == [{"csrf": [], "session": []}]

    order = doc["paths"][f"/_jongo/rpc/{app.t.place_order.id}"]["post"]
    assert order["requestBody"]["content"]["application/json"]["schema"]["properties"]["kwargs"]["properties"][
        "order"]["title"] == "Order"

    page = doc["paths"]["/products/{id}"]["get"]
    assert page["tags"] == ["pages"] and page["summary"] == "Product"
    assert page["parameters"] == [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
        {"name": "tab", "in": "query", "required": False, "schema": {"type": "string", "default": "details"}},
        {"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "default": 1}},
    ]
    api = doc["paths"]["/api/products/{kind}"]
    assert set(api) == {"get", "post"} and api["get"]["operationId"] == "api_products_get"
    assert api["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "type": "array", "items": {"$ref": "#/components/schemas/Product"}}
    assert api["get"]["security"] == [{"session": []}] and api["post"]["security"] == [{"csrf": [], "session": []}]
    assert "429" in api["get"]["responses"] and "401" in api["get"]["responses"]
    assert "security" not in doc["paths"]["/hook"]["post"], "csrf=False and public: nothing to send"

    assert set(doc["components"]["schemas"]) == {"Error", "Product"}
    assert doc["components"]["schemas"]["Product"] == model_schema(app.t.Product)
    ids = [op["operationId"] for path in doc["paths"].values() for op in path.values()]
    assert len(ids) == len(set(ids)), "operationIds must be unique"


def test_every_ref_in_the_document_resolves(app):
    doc = openapi(app)

    def refs(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref":
                    yield value
                else:
                    yield from refs(value)
        elif isinstance(node, list):
            for item in node:
                yield from refs(item)

    found = set(refs(doc))
    assert found
    for ref in found:
        target = doc
        for part in ref.lstrip("#/").split("/"):
            target = target[part]
        assert isinstance(target, dict)


def test_the_error_schema_matches_a_real_error_response(app):
    client = TestClient(app)
    error = client.post(f"/_jongo/rpc/{app.t.add_product.id}", json={"kwargs": {"name": 3}}).json()["error"]
    schema = openapi(app)["components"]["schemas"]["Error"]["properties"]["error"]
    assert set(schema["required"]) <= set(error) and set(error) <= set(schema["properties"])


def test_the_document_is_served_by_the_running_app_and_locked_in_production(app, monkeypatch):
    client = TestClient(app)
    served = client.get("/_jongo/openapi.json")
    assert served.status == 200 and served.json()["paths"].keys() == openapi(app)["paths"].keys()
    app.dev = False
    refused = client.get("/_jongo/openapi.json")
    assert refused.status == 403 and refused.json()["error"]["code"] == "J508"
    monkeypatch.setenv("JONGO_STATUS_TOKEN", "t" * 20)
    assert client.get("/_jongo/openapi.json", headers={"Authorization": "Bearer " + "t" * 20}).status == 200


def test_describe_carries_input_schemas_rate_limits_and_form_schemas(app):
    d = describe(app)
    json.dumps(d)
    by_name = {f["name"]: f for f in d["server_functions"] if f["file"] == "test_schema.py"}
    assert by_name["add_product"]["input_schema"] == function_schema(app.t.add_product)
    assert by_name["restock"]["rate_limit"] == "5/minute" and by_name["add_product"]["rate_limit"] is None
    assert by_name["place_order"]["signature"] == "place_order(request, order: Order) -> dict"
    assert by_name["place_order"]["params"] == [{"name": "order", "type": "Order", "schema": "Order"}]
    order = [s for s in d["schemas"] if s["name"] == "Order" and s["file"] == "test_schema.py"][-1]
    assert order["used_by"] == ["place_order"] and order["line"]
    assert [f["name"] for f in order["fields"]] == ["email", "quantity", "gift", "size", "code"]
    assert order["fields"][1] == {"name": "quantity", "type": "int", "required": True, "label": "Quantity",
                                  "min": 1, "max": 99}
    route = [r for r in d["routes"] if r["path"] == "/api/products/<slug:kind>"][0]
    assert route["rate_limit"] == "10/minute"
    text = jongo.context(app)
    assert "## Form schemas" in text and "### `Order`" in text and "used by `place_order`" in text
    assert "limited to 5/minute" in text and "`jongo mcp`" in text and "`jongo openapi`" in text


APP = '''
from jongo import Jongo, db, server
app = Jongo(__name__, title="CLI shop", database="shop.sqlite3")

class Item(db.Model):
    name = db.Text(max_length=50)

@server
def add_item(name: str, quantity: int = 1) -> Item:
    """Add an item."""
    return Item.create(name=name)
'''


def run_cli(tmp_path, *args):
    env = {**os.environ, "PYTHONPATH": str(HERE.parent), "JONGO_TRACEBACK": "0"}
    env.pop("JONGO_DEV", None)
    return subprocess.run([sys.executable, "-m", "jongo", *args], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=60)


def test_jongo_openapi_prints_or_writes_the_document(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    out = run_cli(tmp_path, "openapi", "--api-version", "2.0.0", "--server", "https://x.example")
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc["info"]["title"] == "CLI shop" and doc["info"]["version"] == "2.0.0"
    assert doc["servers"] == [{"url": "https://x.example"}]
    assert "/_jongo/rpc/app.add_item" in doc["paths"] and "Item" in doc["components"]["schemas"]
    written = run_cli(tmp_path, "openapi", "-o", "openapi.json")
    assert written.returncode == 0 and "wrote openapi.json" in written.stdout
    assert json.loads((tmp_path / "openapi.json").read_text())["openapi"] == "3.1.0"
    assert not (tmp_path / "shop.sqlite3").exists(), "describing the app must not create its database"
    help_text = run_cli(tmp_path, "openapi", "--help").stdout
    assert "OpenAPI 3.1" in help_text and "--api-version" in help_text
