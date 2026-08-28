"""Catálogos editables por el administrador: conceptos de mantenimiento,
tipos de gasto, etc. — para no tener que tocar código cada vez que se
necesita agregar una opción nueva a un desplegable."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_setting, query_all, query_one, set_setting
from app.helpers import parse_float
from app.seed_data import MECHANIC_TYPES, labor_cost_setting_key

bp = Blueprint("catalogos", __name__, url_prefix="/configuracion/catalogos")

CATEGORIES = {
    "maintenance_type": "Conceptos de mantenimiento",
    "inspection_item": "Ítems de inspección",
}


def get_catalog(category, only_active=True):
    sql = "SELECT * FROM catalog_items WHERE category = ?"
    params = [category]
    if only_active:
        sql += " AND active = 1"
    sql += " ORDER BY sort_order, name"
    return query_all(sql, params)


@bp.route("")
@permission_required("catalogos", "view")
def list_view():
    active_category = request.args.get("categoria", "maintenance_type")
    if active_category not in CATEGORIES:
        active_category = "maintenance_type"
    items = query_all(
        "SELECT * FROM catalog_items WHERE category = ? ORDER BY sort_order, name",
        (active_category,),
    )
    labor_costs = {t: get_setting(labor_cost_setting_key(t), "0") for t in MECHANIC_TYPES}
    return render_template(
        "catalogos/list.html", categories=CATEGORIES, active_category=active_category, items=items,
        labor_costs=labor_costs, mechanic_types=MECHANIC_TYPES,
    )


@bp.route("/costo-mano-obra", methods=["POST"])
@permission_required("catalogos", "edit")
def update_labor_cost():
    if not validate_csrf():
        abort(400)
    values = {}
    for t in MECHANIC_TYPES:
        value = parse_float(request.form.get(labor_cost_setting_key(t)), None)
        if value is None or value < 0:
            flash(f"Indica un costo de mano de obra por minuto válido para {t}.", "error")
            return redirect(url_for("catalogos.list_view"))
        values[t] = value
    for t, value in values.items():
        set_setting(labor_cost_setting_key(t), f"{value:.2f}")
    flash("Costos de mano de obra actualizados.", "success")
    return redirect(url_for("catalogos.list_view"))


@bp.route("/agregar", methods=["POST"])
@permission_required("catalogos", "edit")
def add_item():
    if not validate_csrf():
        abort(400)
    category = request.form.get("category")
    name = request.form.get("name", "").strip()
    if category not in CATEGORIES:
        abort(400)
    if not name:
        flash("Escribe un nombre para el nuevo concepto.", "error")
        return redirect(url_for("catalogos.list_view", categoria=category))

    existing = query_one(
        "SELECT id, active FROM catalog_items WHERE category = ? AND name = ?", (category, name)
    )
    if existing:
        if existing["active"]:
            flash("Ese concepto ya existe.", "error")
        else:
            execute("UPDATE catalog_items SET active = 1 WHERE id = ?", (existing["id"],))
            flash(f'"{name}" reactivado.', "success")
    else:
        max_order = query_one(
            "SELECT COALESCE(MAX(sort_order), -1) m FROM catalog_items WHERE category = ?", (category,)
        )["m"]
        execute(
            "INSERT INTO catalog_items (category, name, sort_order) VALUES (?, ?, ?)",
            (category, name, max_order + 1),
        )
        flash(f'"{name}" agregado.', "success")
    return redirect(url_for("catalogos.list_view", categoria=category))


@bp.route("/<int:item_id>/alternar", methods=["POST"])
@permission_required("catalogos", "edit")
def toggle_item(item_id):
    if not validate_csrf():
        abort(400)
    item = query_one("SELECT * FROM catalog_items WHERE id = ?", (item_id,))
    if item is None:
        abort(404)
    execute("UPDATE catalog_items SET active = ? WHERE id = ?", (0 if item["active"] else 1, item_id))
    flash("Actualizado." if item["active"] else "Reactivado.", "success")
    return redirect(url_for("catalogos.list_view", categoria=item["category"]))
