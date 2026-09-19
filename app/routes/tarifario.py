"""Tarifario: tarifas por cliente y ruta (15 sep, pedido de Braulio: "un
menu que se llame tarifario, este agrupado por clientes... Aparte de estos
2 clientes [Backus/Lindley] quiero que dejes la opcion de poder agregar
luego manualmente otros"). Reutiliza el catálogo de Clientes que ya existe
(app/routes/clientes.py) -- una ruta de tarifario siempre pertenece a un
cliente real (`clients.id`), así que agregar "otro cliente" es simplemente
crear un cliente nuevo ahí y después agregarle rutas/tarifas acá.

Cada ruta (origen/destino) de un cliente puede tener una o más tarifas con
su propia etiqueta y monto (tariff_items) -- mismo patrón de "encabezado +
líneas" que ya usa Cotizaciones (quotations/quotation_items), con el mismo
truco de arrays paralelos en el formulario (item_label[]/item_amount[]) y
JS para agregar/quitar filas. Backus trae 2 tarifas por ruta ("PT +
ENVASES"/"PT + VACIO"); Lindley trae 1 ("Tarifa"); un cliente nuevo puede
tener cualquier cantidad -- no está hardcodeado a 1 o 2."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_float

bp = Blueprint("tarifario", __name__, url_prefix="/tarifario")


def _parse_items_from_form(form):
    """Lee las líneas de tarifa del formulario (arrays paralelos
    item_label/item_amount, mismo patrón que _parse_items_from_form en
    cotizaciones.py). Ignora filas totalmente vacías (para tolerar filas
    que el usuario agregó con el botón y no llegó a usar)."""
    labels = form.getlist("item_label")
    amounts = form.getlist("item_amount")
    items = []
    for i in range(len(labels)):
        label = (labels[i] if i < len(labels) else "").strip()
        amount_raw = amounts[i] if i < len(amounts) else ""
        if not label and not str(amount_raw).strip():
            continue
        if not label:
            return None, f"La línea {i + 1} tiene un monto pero no una etiqueta (ej. 'PT + ENVASES')."
        items.append({"label": label, "amount": parse_float(amount_raw, 0)})
    if not items:
        return None, "Agrega al menos una tarifa (etiqueta + monto) para esta ruta."
    return items, None


def _save_items(tariff_route_id, items):
    for order, item in enumerate(items):
        execute(
            "INSERT INTO tariff_items (tariff_route_id, label, amount, sort_order) VALUES (?, ?, ?, ?)",
            (tariff_route_id, item["label"], item["amount"], order),
        )


@bp.route("")
@permission_required("tarifario", "view")
def list_view():
    q = request.args.get("q", "").strip()
    sql = """SELECT r.*, c.name as client_name
              FROM tariff_routes r JOIN clients c ON c.id = r.client_id
              WHERE r.active = 1"""
    params = []
    if q:
        # LOWER() en ambos lados (patch 0066) -- ver el comentario completo en
        # clientes.list_view().
        sql += " AND (LOWER(c.name) LIKE LOWER(?) OR LOWER(r.origin) LIKE LOWER(?) OR LOWER(r.destination) LIKE LOWER(?))"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    sql += " ORDER BY c.name, r.origin, r.destination"
    routes = query_all(sql, params)

    items_by_route = {}
    if routes:
        route_ids = [r["id"] for r in routes]
        placeholders = ",".join("?" for _ in route_ids)
        all_items = query_all(
            f"SELECT * FROM tariff_items WHERE tariff_route_id IN ({placeholders}) ORDER BY tariff_route_id, sort_order, id",
            route_ids,
        )
        for it in all_items:
            items_by_route.setdefault(it["tariff_route_id"], []).append(it)

    # Agrupa por cliente, conservando el orden alfabético ya aplicado en el
    # SQL (ORDER BY c.name) -- un dict normal de Python mantiene el orden
    # de inserción, así que no hace falta ordenar de nuevo acá.
    groups = {}
    for r in routes:
        group = groups.setdefault(r["client_id"], {"client_name": r["client_name"], "routes": []})
        # "tariff_items", no "items" -- un dict de Jinja resuelve ".items"
        # al método builtin dict.items ANTES que a una clave con ese
        # nombre (a diferencia de un sqlite3.Row, que no tiene ese
        # método), así que "entry.items" en el template rompería.
        group["routes"].append({"route": r, "tariff_items": items_by_route.get(r["id"], [])})

    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    return render_template("tarifario/list.html", groups=groups.values(), q=q, clients=clients)


@bp.route("/nueva", methods=["GET", "POST"])
@permission_required("tarifario", "edit")
def new():
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_id = request.form.get("client_id") or None
        origin = request.form.get("origin", "").strip()
        destination = request.form.get("destination", "").strip()
        notes = request.form.get("notes", "").strip()
        errors = []
        if not client_id:
            errors.append("Selecciona un cliente (o créalo primero en Clientes).")
        if not origin:
            errors.append("Indica el origen.")
        if not destination:
            errors.append("Indica el destino.")
        items, item_error = _parse_items_from_form(request.form)
        if item_error:
            errors.append(item_error)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "tarifario/form.html", clients=clients, route=request.form, items=[], mode="new"
            )
        route_id = execute(
            "INSERT INTO tariff_routes (client_id, origin, destination, notes) VALUES (?, ?, ?, ?)",
            (client_id, origin, destination, notes),
        )
        _save_items(route_id, items)
        flash("Ruta y tarifa agregadas al tarifario.", "success")
        return redirect(url_for("tarifario.list_view"))
    return render_template("tarifario/form.html", clients=clients, route=None, items=[], mode="new")


@bp.route("/<int:route_id>/editar", methods=["GET", "POST"])
@permission_required("tarifario", "edit")
def edit(route_id):
    route = query_one("SELECT * FROM tariff_routes WHERE id = ?", (route_id,))
    if route is None:
        abort(404)
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_id = request.form.get("client_id") or None
        origin = request.form.get("origin", "").strip()
        destination = request.form.get("destination", "").strip()
        notes = request.form.get("notes", "").strip()
        errors = []
        if not client_id:
            errors.append("Selecciona un cliente.")
        if not origin:
            errors.append("Indica el origen.")
        if not destination:
            errors.append("Indica el destino.")
        items, item_error = _parse_items_from_form(request.form)
        if item_error:
            errors.append(item_error)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "tarifario/form.html", clients=clients, route=request.form, items=[], mode="edit", route_id=route_id
            )
        execute(
            "UPDATE tariff_routes SET client_id=?, origin=?, destination=?, notes=? WHERE id=?",
            (client_id, origin, destination, notes, route_id),
        )
        # Reemplaza las líneas por completo (igual que en la edición de
        # ítems de Cotizaciones) -- más simple que hacer un diff y evita
        # ítems huérfanos si se quitó una fila.
        execute("DELETE FROM tariff_items WHERE tariff_route_id = ?", (route_id,))
        _save_items(route_id, items)
        flash("Tarifa actualizada.", "success")
        return redirect(url_for("tarifario.list_view"))

    existing_items = query_all(
        "SELECT * FROM tariff_items WHERE tariff_route_id = ? ORDER BY sort_order, id", (route_id,)
    )
    return render_template(
        "tarifario/form.html", clients=clients, route=route, items=existing_items, mode="edit", route_id=route_id
    )


@bp.route("/<int:route_id>/eliminar", methods=["POST"])
@permission_required("tarifario", "edit")
def delete(route_id):
    if not validate_csrf():
        abort(400)
    route = query_one("SELECT id FROM tariff_routes WHERE id = ?", (route_id,))
    if route is None:
        abort(404)
    # Tarifario es solo un catálogo de referencia (no lo usa ningún
    # documento fiscal todavía), así que se puede borrar directo -- sin el
    # chequeo de "en uso" que sí tiene clientes.delete().
    execute("DELETE FROM tariff_items WHERE tariff_route_id = ?", (route_id,))
    execute("DELETE FROM tariff_routes WHERE id = ?", (route_id,))
    flash("Ruta eliminada del tarifario.", "success")
    return redirect(url_for("tarifario.list_view"))
