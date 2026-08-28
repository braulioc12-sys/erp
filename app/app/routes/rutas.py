"""Catálogo de rutas frecuentes con un monto de viáticos predeterminado,
usado al confirmar el anticipo de gastos de viaje a un conductor."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_float

bp = Blueprint("rutas", __name__, url_prefix="/rutas")


def find_route(origin, destination):
    return query_one(
        "SELECT * FROM routes WHERE active = 1 AND origin = ? AND destination = ?",
        (origin, destination),
    )


@bp.route("")
@permission_required("rutas", "view")
def list_view():
    routes = query_all("SELECT * FROM routes ORDER BY origin, destination")
    return render_template("rutas/list.html", routes=routes)


@bp.route("/agregar", methods=["POST"])
@permission_required("rutas", "edit")
def add():
    if not validate_csrf():
        abort(400)
    origin = request.form.get("origin", "").strip()
    destination = request.form.get("destination", "").strip()
    amount = parse_float(request.form.get("default_expense_amount"), 0)
    commission = parse_float(request.form.get("default_commission_amount"), 0)
    if not origin or not destination:
        flash("Indica origen y destino.", "error")
        return redirect(url_for("rutas.list_view"))

    existing = query_one("SELECT id FROM routes WHERE origin = ? AND destination = ?", (origin, destination))
    if existing:
        execute(
            "UPDATE routes SET default_expense_amount = ?, default_commission_amount = ?, active = 1 WHERE id = ?",
            (amount, commission, existing["id"]),
        )
        flash("Ruta actualizada.", "success")
    else:
        execute(
            "INSERT INTO routes (origin, destination, default_expense_amount, default_commission_amount) VALUES (?, ?, ?, ?)",
            (origin, destination, amount, commission),
        )
        flash("Ruta agregada.", "success")
    return redirect(url_for("rutas.list_view"))


@bp.route("/<int:route_id>/alternar", methods=["POST"])
@permission_required("rutas", "edit")
def toggle(route_id):
    if not validate_csrf():
        abort(400)
    route = query_one("SELECT * FROM routes WHERE id = ?", (route_id,))
    if route is None:
        abort(404)
    execute("UPDATE routes SET active = ? WHERE id = ?", (0 if route["active"] else 1, route_id))
    flash("Actualizada." if route["active"] else "Reactivada.", "success")
    return redirect(url_for("rutas.list_view"))
