from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import next_code, parse_date, parse_float, today_str

bp = Blueprint("viajes", __name__, url_prefix="/viajes")

STATUS_FLOW = {
    "PENDIENTE": ["EN_CURSO", "CANCELADO"],
    "EN_CURSO": ["ENTREGADO", "CANCELADO"],
    "ENTREGADO": [],
    "CANCELADO": [],
}


@bp.route("")
@permission_required("viajes", "view")
def list_view():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    sql = """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, d.name as driver_name
              FROM trips t
              JOIN clients c ON c.id = t.client_id
              LEFT JOIN vehicles v ON v.id = t.vehicle_id
              LEFT JOIN drivers d ON d.id = t.driver_id
              WHERE 1=1"""
    params = []
    if status:
        sql += " AND t.status = ?"
        params.append(status)
    if q:
        sql += " AND (t.code LIKE ? OR c.name LIKE ? OR t.origin LIKE ? OR t.destination LIKE ?)"
        params += [f"%{q}%"] * 4
    sql += " ORDER BY t.scheduled_date DESC, t.id DESC"

    trips = query_all(sql, params)
    return render_template("viajes/list.html", trips=trips, status=status, q=q)


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def new():
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    vehicles = query_all("SELECT * FROM vehicles WHERE status = 'ACTIVO' ORDER BY plate")
    drivers = query_all("SELECT * FROM drivers WHERE status = 'ACTIVO' ORDER BY name")

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_id = request.form.get("client_id")
        origin = request.form.get("origin", "").strip()
        destination = request.form.get("destination", "").strip()
        scheduled_date = parse_date(request.form.get("scheduled_date"))
        errors = []
        if not client_id:
            errors.append("Selecciona un cliente.")
        if not origin or not destination:
            errors.append("Origen y destino son obligatorios.")
        if not scheduled_date:
            errors.append("La fecha programada no es válida.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "viajes/form.html", trip=request.form, mode="new",
                clients=clients, vehicles=vehicles, drivers=drivers,
            )

        code = next_code("V", "trips")
        vehicle_id = request.form.get("vehicle_id") or None
        driver_id = request.form.get("driver_id") or None
        trip_id = execute(
            """INSERT INTO trips (code, client_id, vehicle_id, driver_id, origin, destination,
               cargo_description, cargo_weight_kg, scheduled_date, rate, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                code,
                client_id,
                vehicle_id,
                driver_id,
                origin,
                destination,
                request.form.get("cargo_description", "").strip(),
                parse_float(request.form.get("cargo_weight_kg"), None),
                scheduled_date,
                parse_float(request.form.get("rate")),
                request.form.get("notes", "").strip(),
                None,
            ),
        )
        flash(f"Viaje {code} creado.", "success")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    return render_template(
        "viajes/form.html", trip=None, mode="new",
        clients=clients, vehicles=vehicles, drivers=drivers, today=today_str(),
    )


@bp.route("/<int:trip_id>/editar", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def edit(trip_id):
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    vehicles = query_all("SELECT * FROM vehicles WHERE status = 'ACTIVO' OR id = ? ORDER BY plate", (trip["vehicle_id"],))
    drivers = query_all("SELECT * FROM drivers WHERE status = 'ACTIVO' OR id = ? ORDER BY name", (trip["driver_id"],))

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        scheduled_date = parse_date(request.form.get("scheduled_date")) or trip["scheduled_date"]
        execute(
            """UPDATE trips SET client_id=?, vehicle_id=?, driver_id=?, origin=?, destination=?,
               cargo_description=?, cargo_weight_kg=?, scheduled_date=?, rate=?, notes=?
               WHERE id=?""",
            (
                request.form.get("client_id"),
                request.form.get("vehicle_id") or None,
                request.form.get("driver_id") or None,
                request.form.get("origin", "").strip(),
                request.form.get("destination", "").strip(),
                request.form.get("cargo_description", "").strip(),
                parse_float(request.form.get("cargo_weight_kg"), None),
                scheduled_date,
                parse_float(request.form.get("rate")),
                request.form.get("notes", "").strip(),
                trip_id,
            ),
        )
        flash("Viaje actualizado.", "success")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    return render_template(
        "viajes/form.html", trip=trip, mode="edit", trip_id=trip_id,
        clients=clients, vehicles=vehicles, drivers=drivers,
    )


@bp.route("/<int:trip_id>")
@permission_required("viajes", "view")
def detail(trip_id):
    trip = query_one(
        """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, d.name as driver_name
           FROM trips t
           JOIN clients c ON c.id = t.client_id
           LEFT JOIN vehicles v ON v.id = t.vehicle_id
           LEFT JOIN drivers d ON d.id = t.driver_id
           WHERE t.id = ?""",
        (trip_id,),
    )
    if trip is None:
        abort(404)
    expenses = query_all("SELECT * FROM expenses WHERE trip_id = ? ORDER BY expense_date DESC", (trip_id,))
    total_expenses = sum(e["amount"] for e in expenses)
    next_statuses = STATUS_FLOW.get(trip["status"], [])
    advance = query_one("SELECT id, status FROM expense_advances WHERE trip_id = ?", (trip_id,))
    return render_template(
        "viajes/detail.html", trip=trip, expenses=expenses,
        total_expenses=total_expenses, next_statuses=next_statuses, advance=advance,
    )


@bp.route("/<int:trip_id>/estado", methods=["POST"])
@permission_required("viajes", "edit")
def change_status(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    new_status = request.form.get("status")
    allowed = STATUS_FLOW.get(trip["status"], [])
    if new_status not in allowed:
        flash("Cambio de estado no permitido.", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    if new_status == "ENTREGADO":
        execute("UPDATE trips SET status=?, delivered_date=? WHERE id=?", (new_status, today_str(), trip_id))
    else:
        execute("UPDATE trips SET status=? WHERE id=?", (new_status, trip_id))

    flash(f"Viaje marcado como {new_status.replace('_', ' ').title()}.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))
