from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import next_code, now_str, parse_date, parse_float, today_str
from app.routes.rutas import find_route

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

    sql = """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, d.name as driver_name,
                     d2.name as driver2_name
              FROM trips t
              JOIN clients c ON c.id = t.client_id
              LEFT JOIN vehicles v ON v.id = t.vehicle_id
              LEFT JOIN drivers d ON d.id = t.driver_id
              LEFT JOIN drivers d2 ON d2.id = t.driver2_id
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


def _active_routes():
    """Rutas activas del catálogo, para el desplegable de selección del
    formulario de viajes (ya no se escribe origen/destino a mano — pedido
    de Braulio, 28 ago: la ruta se elige de las que ya están registradas
    en Rutas). Se convierte a dicts porque sqlite3.Row no es serializable
    a JSON directamente (se usa también para la sugerencia de comisión en JS)."""
    rows = query_all(
        "SELECT id, origin, destination, default_commission_amount FROM routes WHERE active = 1 ORDER BY origin, destination"
    )
    return [dict(r) for r in rows]


def _resolve_route_selection(form, current_trip=None):
    """Resuelve la ruta elegida en el desplegable del formulario de viajes.
    Devuelve (origin, destination, route_row_o_None, error_o_None).

    `route_id` normalmente es el id de una ruta activa del catálogo. El
    valor especial "__current__" solo aparece al editar un viaje cuya
    ruta (origen/destino ya guardados) no está en el catálogo activo —
    deja la ruta tal cual estaba en vez de obligar a elegir una nueva
    sin querer."""
    route_id = (form.get("route_id") or "").strip()
    if route_id == "__current__" and current_trip is not None:
        return current_trip["origin"], current_trip["destination"], None, None
    if not route_id:
        return "", "", None, "Selecciona una ruta del catálogo (Rutas)."
    route = query_one("SELECT * FROM routes WHERE id = ? AND active = 1", (route_id,))
    if not route:
        return "", "", None, "La ruta elegida ya no está disponible — elige otra."
    return route["origin"], route["destination"], route, None


def _resolve_commission(form, origin, destination, route=None, double_driver=False, single_leg=False):
    """Si el usuario dejó vacío el campo de comisión, se usa el monto
    predeterminado de la ruta elegida (o, si no se resolvió una ruta
    directamente — caso "__current__" —, se busca por origen/destino),
    ajustado según "doble conductor" (x0.6) y "solo 1 tramo" (x0.5) —
    pedido de Braulio, 3 sep. Si ambos están marcados se combinan
    multiplicando (60% x 50% = 30%). Cuando hay doble conductor, este
    mismo monto (completo, sin repartir) se le asigna a cada uno de los
    2 conductores — ver INSERT/UPDATE en new()/edit()."""
    raw = (form.get("driver_commission") or "").strip()
    if raw:
        return parse_float(raw, 0)
    if route is None:
        route = find_route(origin, destination)
    base = route["default_commission_amount"] if route else 0
    factor = 1.0
    if double_driver:
        factor *= 0.6
    if single_leg:
        factor *= 0.5
    return base * factor


def _selected_route_id_for_edit(trip):
    """Id a preseleccionar en el desplegable al editar un viaje: el de la
    ruta activa que coincide con el origen/destino ya guardados, o
    "__current__" si esa ruta ya no está en el catálogo activo."""
    route = find_route(trip["origin"], trip["destination"])
    return str(route["id"]) if route else "__current__"


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def new():
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    vehicles = query_all("SELECT * FROM vehicles WHERE status = 'ACTIVO' ORDER BY plate")
    drivers = query_all("SELECT * FROM drivers WHERE status = 'ACTIVO' ORDER BY name")
    routes = _active_routes()

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_id = request.form.get("client_id")
        origin, destination, route, route_error = _resolve_route_selection(request.form)
        scheduled_date = parse_date(request.form.get("scheduled_date"))
        double_driver = bool(request.form.get("double_driver"))
        single_leg = bool(request.form.get("single_leg"))
        driver_id = request.form.get("driver_id") or None
        driver2_id = (request.form.get("driver2_id") or None) if double_driver else None
        errors = []
        if not client_id:
            errors.append("Selecciona un cliente.")
        if route_error:
            errors.append(route_error)
        if not scheduled_date:
            errors.append("La fecha programada no es válida.")
        if double_driver:
            if not driver2_id:
                errors.append("Selecciona el segundo conductor (viaje de doble conductor).")
            elif driver_id and driver2_id == driver_id:
                errors.append("El segundo conductor debe ser distinto del primero.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "viajes/form.html", trip=request.form, mode="new",
                clients=clients, vehicles=vehicles, drivers=drivers, routes=routes,
                selected_route_id=request.form.get("route_id", ""),
            )

        code = next_code("V", "trips")
        vehicle_id = request.form.get("vehicle_id") or None
        driver_commission = _resolve_commission(
            request.form, origin, destination, route,
            double_driver=double_driver, single_leg=single_leg,
        )
        trip_id = execute(
            """INSERT INTO trips (code, client_id, vehicle_id, driver_id, driver2_id, origin, destination,
               cargo_description, cargo_weight_kg, scheduled_date, rate, driver_commission,
               double_driver, single_leg, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                code,
                client_id,
                vehicle_id,
                driver_id,
                driver2_id,
                origin,
                destination,
                request.form.get("cargo_description", "").strip(),
                parse_float(request.form.get("cargo_weight_kg"), None),
                scheduled_date,
                parse_float(request.form.get("rate")),
                driver_commission,
                int(double_driver),
                int(single_leg),
                request.form.get("notes", "").strip(),
                None,
            ),
        )
        flash(f"Viaje {code} creado.", "success")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    return render_template(
        "viajes/form.html", trip=None, mode="new",
        clients=clients, vehicles=vehicles, drivers=drivers, routes=routes, today=today_str(),
        selected_route_id="",
    )


@bp.route("/<int:trip_id>/editar", methods=["GET", "POST"])
@permission_required("viajes", "edit")
def edit(trip_id):
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    vehicles = query_all("SELECT * FROM vehicles WHERE status = 'ACTIVO' OR id = ? ORDER BY plate", (trip["vehicle_id"],))
    drivers = query_all(
        "SELECT * FROM drivers WHERE status = 'ACTIVO' OR id = ? OR id = ? ORDER BY name",
        (trip["driver_id"], trip["driver2_id"]),
    )
    routes = _active_routes()

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        scheduled_date = parse_date(request.form.get("scheduled_date")) or trip["scheduled_date"]
        origin, destination, route, route_error = _resolve_route_selection(request.form, current_trip=trip)
        double_driver = bool(request.form.get("double_driver"))
        single_leg = bool(request.form.get("single_leg"))
        driver_id = request.form.get("driver_id") or None
        driver2_id = (request.form.get("driver2_id") or None) if double_driver else None
        errors = []
        if route_error:
            errors.append(route_error)
        if double_driver:
            if not driver2_id:
                errors.append("Selecciona el segundo conductor (viaje de doble conductor).")
            elif driver_id and driver2_id == driver_id:
                errors.append("El segundo conductor debe ser distinto del primero.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "viajes/form.html", trip=trip, mode="edit", trip_id=trip_id,
                clients=clients, vehicles=vehicles, drivers=drivers, routes=routes,
                selected_route_id=request.form.get("route_id", ""),
            )
        driver_commission = _resolve_commission(
            request.form, origin, destination, route,
            double_driver=double_driver, single_leg=single_leg,
        )
        execute(
            """UPDATE trips SET client_id=?, vehicle_id=?, driver_id=?, driver2_id=?, origin=?, destination=?,
               cargo_description=?, cargo_weight_kg=?, scheduled_date=?, rate=?, driver_commission=?,
               double_driver=?, single_leg=?, notes=?
               WHERE id=?""",
            (
                request.form.get("client_id"),
                request.form.get("vehicle_id") or None,
                driver_id,
                driver2_id,
                origin,
                destination,
                request.form.get("cargo_description", "").strip(),
                parse_float(request.form.get("cargo_weight_kg"), None),
                scheduled_date,
                parse_float(request.form.get("rate")),
                driver_commission,
                int(double_driver),
                int(single_leg),
                request.form.get("notes", "").strip(),
                trip_id,
            ),
        )
        flash("Viaje actualizado.", "success")
        return redirect(url_for("viajes.detail", trip_id=trip_id))

    return render_template(
        "viajes/form.html", trip=trip, mode="edit", trip_id=trip_id,
        clients=clients, vehicles=vehicles, drivers=drivers, routes=routes,
        selected_route_id=_selected_route_id_for_edit(trip),
    )


@bp.route("/<int:trip_id>")
@permission_required("viajes", "view")
def detail(trip_id):
    trip = query_one(
        """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, d.name as driver_name,
                  d2.name as driver2_name
           FROM trips t
           JOIN clients c ON c.id = t.client_id
           LEFT JOIN vehicles v ON v.id = t.vehicle_id
           LEFT JOIN drivers d ON d.id = t.driver_id
           LEFT JOIN drivers d2 ON d2.id = t.driver2_id
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
        # 31 ago: además de la fecha (ya existía), se guarda el momento
        # exacto de inicio/fin real del viaje — es lo que necesita el futuro
        # reporte de cumplimiento de hoja de ruta para saber qué tramo del
        # historial de GPS corresponde a este viaje.
        execute(
            "UPDATE trips SET status=?, delivered_date=?, actual_end_at=? WHERE id=?",
            (new_status, today_str(), now_str(), trip_id),
        )
    elif new_status == "EN_CURSO":
        execute(
            "UPDATE trips SET status=?, actual_start_at=COALESCE(actual_start_at, ?) WHERE id=?",
            (new_status, now_str(), trip_id),
        )
    else:
        execute("UPDATE trips SET status=? WHERE id=?", (new_status, trip_id))

    flash(f"Viaje marcado como {new_status.replace('_', ' ').title()}.", "success")
    return redirect(url_for("viajes.detail", trip_id=trip_id))


def _commissions_by_driver(month):
    """Agrupa, para un mes (YYYY-MM), cuántos viajes hizo cada conductor a
    cada ruta y cuál fue su comisión total. Excluye viajes cancelados.

    En un viaje de "doble conductor" (double_driver=1), driver_commission ya
    trae el monto completo que le corresponde a CADA conductor (pedido de
    Braulio, 3 sep: "cada conductor recibe el 60% completo", no se reparte) —
    por eso el segundo conductor se agrega con un UNION ALL que suma ese
    mismo monto otra vez, no la mitad."""
    rows = query_all(
        """SELECT driver_id, driver_name, origin, destination,
                  COUNT(*) as trip_count, SUM(driver_commission) as route_commission
           FROM (
               SELECT d.id as driver_id, d.name as driver_name, t.origin, t.destination,
                      t.driver_commission as driver_commission
               FROM trips t
               JOIN drivers d ON d.id = t.driver_id
               WHERE strftime('%Y-%m', t.scheduled_date) = ? AND t.status != 'CANCELADO'
               UNION ALL
               SELECT d2.id as driver_id, d2.name as driver_name, t.origin, t.destination,
                      t.driver_commission as driver_commission
               FROM trips t
               JOIN drivers d2 ON d2.id = t.driver2_id
               WHERE t.double_driver = 1 AND strftime('%Y-%m', t.scheduled_date) = ? AND t.status != 'CANCELADO'
           ) combined
           GROUP BY driver_id, origin, destination
           ORDER BY driver_name, origin, destination""",
        (month, month),
    )
    by_driver = {}
    for r in rows:
        entry = by_driver.setdefault(
            r["driver_id"],
            {"driver_name": r["driver_name"], "routes": [], "trip_count": 0, "total_commission": 0.0},
        )
        entry["routes"].append(r)
        entry["trip_count"] += r["trip_count"]
        entry["total_commission"] += r["route_commission"] or 0.0
    return list(by_driver.values())


@bp.route("/comisiones")
@permission_required("viajes", "view")
def commissions_report():
    month = request.args.get("month") or today_str()[:7]
    drivers = _commissions_by_driver(month)
    grand_total = sum(d["total_commission"] for d in drivers)
    grand_trips = sum(d["trip_count"] for d in drivers)
    return render_template(
        "viajes/commissions.html", month=month, drivers=drivers,
        grand_total=grand_total, grand_trips=grand_trips,
    )


@bp.route("/comisiones/exportar")
@permission_required("viajes", "view")
def commissions_export():
    from flask import current_app

    from app.reports import build_commissions_workbook

    month = request.args.get("month") or today_str()[:7]
    drivers = _commissions_by_driver(month)
    buffer = build_commissions_workbook(drivers, company_name=current_app.config["COMPANY_NAME"], month=month)
    filename = f"comisiones_{month}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
