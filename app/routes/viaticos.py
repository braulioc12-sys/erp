"""Anticipo de viáticos (gastos de viaje) entregado al conductor, y su
liquidación posterior contra los gastos reales registrados del viaje.
Usa el mismo permiso que Gastos (módulo "gastos"): quien puede gestionar
gastos, puede gestionar anticipos y liquidaciones."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, today_str
from app.routes.rutas import find_route

bp = Blueprint("viaticos", __name__, url_prefix="/viaticos")


@bp.route("")
@permission_required("gastos", "view")
def list_view():
    advances = query_all(
        """SELECT a.*, t.code as trip_code, t.origin, t.destination,
                  (SELECT COALESCE(SUM(e.amount), 0) FROM expenses e WHERE e.trip_id = a.trip_id) as spent
           FROM expense_advances a
           JOIN trips t ON t.id = a.trip_id
           ORDER BY a.given_date DESC, a.id DESC"""
    )
    return render_template("viaticos/list.html", advances=advances)


@bp.route("/nuevo/<int:trip_id>", methods=["GET", "POST"])
@permission_required("gastos", "edit")
def new(trip_id):
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    existing = query_one("SELECT id FROM expense_advances WHERE trip_id = ?", (trip_id,))
    if existing:
        flash("Este viaje ya tiene un anticipo de viáticos registrado.", "error")
        return redirect(url_for("viaticos.detail", advance_id=existing["id"]))

    route = find_route(trip["origin"], trip["destination"])

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount_given"))
        if amount <= 0:
            flash("Indica un monto válido.", "error")
            return render_template("viaticos/form.html", trip=trip, route=route, today=today_str())

        advance_id = execute(
            """INSERT INTO expense_advances (trip_id, route_id, amount_given, given_date, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                trip_id,
                route["id"] if route else None,
                amount,
                parse_date(request.form.get("given_date")) or today_str(),
                request.form.get("notes", "").strip(),
                None,
            ),
        )
        flash(f"Anticipo de viáticos confirmado: {trip['code']} recibió S/ {amount:.2f}.", "success")
        return redirect(url_for("viaticos.detail", advance_id=advance_id))

    return render_template("viaticos/form.html", trip=trip, route=route, today=today_str())


@bp.route("/<int:advance_id>")
@permission_required("gastos", "view")
def detail(advance_id):
    advance = query_one(
        """SELECT a.*, t.code as trip_code, t.origin, t.destination, t.status as trip_status
           FROM expense_advances a JOIN trips t ON t.id = a.trip_id WHERE a.id = ?""",
        (advance_id,),
    )
    if advance is None:
        abort(404)
    expenses = query_all(
        "SELECT * FROM expenses WHERE trip_id = ? ORDER BY expense_date", (advance["trip_id"],)
    )
    spent = sum(e["amount"] for e in expenses)
    difference = advance["amount_given"] - spent
    return render_template("viaticos/detail.html", advance=advance, expenses=expenses, spent=spent, difference=difference)


@bp.route("/<int:advance_id>/liquidar", methods=["POST"])
@permission_required("gastos", "edit")
def liquidate(advance_id):
    if not validate_csrf():
        abort(400)
    advance = query_one("SELECT * FROM expense_advances WHERE id = ?", (advance_id,))
    if advance is None:
        abort(404)
    spent = query_one(
        "SELECT COALESCE(SUM(amount), 0) total FROM expenses WHERE trip_id = ?", (advance["trip_id"],)
    )["total"]
    execute(
        """UPDATE expense_advances SET status = 'LIQUIDADO', liquidated_at = datetime('now'),
           liquidated_expenses_total = ? WHERE id = ?""",
        (spent, advance_id),
    )
    flash("Anticipo liquidado.", "success")
    return redirect(url_for("viaticos.detail", advance_id=advance_id))
