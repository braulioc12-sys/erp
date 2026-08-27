from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import parse_date, today_str
from app.routes.catalogos import get_catalog

bp = Blueprint("inspecciones", __name__, url_prefix="/inspecciones")


@bp.route("")
@permission_required("inspecciones", "view")
def list_view():
    inspections = query_all(
        """SELECT i.*, v.plate as vehicle_plate, t.code as trip_code, d.name as driver_name,
                  (SELECT COUNT(*) FROM inspection_items ii WHERE ii.inspection_id = i.id AND ii.status = 'FALLA') as n_fails
           FROM inspections i
           JOIN vehicles v ON v.id = i.vehicle_id
           LEFT JOIN trips t ON t.id = i.trip_id
           LEFT JOIN drivers d ON d.id = i.driver_id
           ORDER BY i.inspection_date DESC, i.id DESC"""
    )
    return render_template("inspecciones/list.html", inspections=inspections)


@bp.route("/nueva", methods=["GET", "POST"])
@bp.route("/nueva/<int:trip_id>", methods=["GET", "POST"])
@permission_required("inspecciones", "edit")
def new(trip_id=None):
    trip = None
    if trip_id:
        trip = query_one(
            """SELECT t.*, v.id as vehicle_id, v.plate as vehicle_plate, d.id as driver_id, d.name as driver_name
               FROM trips t
               LEFT JOIN vehicles v ON v.id = t.vehicle_id
               LEFT JOIN drivers d ON d.id = t.driver_id
               WHERE t.id = ?""",
            (trip_id,),
        )
        if trip is None:
            abort(404)

    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    drivers = query_all("SELECT id, name FROM drivers WHERE status = 'ACTIVO' ORDER BY name")
    items = get_catalog("inspection_item")

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        vehicle_id = request.form.get("vehicle_id") or (trip["vehicle_id"] if trip else None)
        if not vehicle_id:
            flash("Selecciona una unidad.", "error")
            return render_template("inspecciones/form.html", trip=trip, vehicles=vehicles, drivers=drivers, items=items, today=today_str())

        inspection_id = execute(
            """INSERT INTO inspections (vehicle_id, trip_id, driver_id, type, inspection_date, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                vehicle_id,
                trip_id,
                request.form.get("driver_id") or (trip["driver_id"] if trip else None),
                request.form.get("type", "PRE"),
                parse_date(request.form.get("inspection_date")) or today_str(),
                request.form.get("notes", "").strip(),
                None,
            ),
        )

        db = get_db()
        for item in items:
            status = request.form.get(f"item_{item['id']}_status", "NA")
            observation = request.form.get(f"item_{item['id']}_observation", "").strip()
            db.execute(
                "INSERT INTO inspection_items (inspection_id, item_name, status, observation) VALUES (?, ?, ?, ?)",
                (inspection_id, item["name"], status, observation),
            )
        db.commit()

        flash("Inspección registrada.", "success")
        if trip_id:
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        return redirect(url_for("inspecciones.detail", inspection_id=inspection_id))

    return render_template(
        "inspecciones/form.html", trip=trip, vehicles=vehicles, drivers=drivers, items=items, today=today_str(),
    )


@bp.route("/<int:inspection_id>")
@permission_required("inspecciones", "view")
def detail(inspection_id):
    inspection = query_one(
        """SELECT i.*, v.plate as vehicle_plate, t.code as trip_code, d.name as driver_name
           FROM inspections i
           JOIN vehicles v ON v.id = i.vehicle_id
           LEFT JOIN trips t ON t.id = i.trip_id
           LEFT JOIN drivers d ON d.id = i.driver_id
           WHERE i.id = ?""",
        (inspection_id,),
    )
    if inspection is None:
        abort(404)
    items = query_all("SELECT * FROM inspection_items WHERE inspection_id = ?", (inspection_id,))
    return render_template("inspecciones/detail.html", inspection=inspection, items=items)


@bp.route("/<int:inspection_id>/imprimir")
@permission_required("inspecciones", "view")
def print_view(inspection_id):
    """Vista de impresión de una inspección: página independiente (sin menú
    lateral) con el logo de la empresa en el encabezado, pensada para
    imprimirse o guardarse como PDF desde el propio diálogo de impresión
    del navegador (Ctrl+P → Guardar como PDF), sin depender de ninguna
    librería de generación de PDF en el servidor."""
    inspection = query_one(
        """SELECT i.*, v.plate as vehicle_plate, v.brand as vehicle_brand, v.model as vehicle_model,
                  t.code as trip_code, d.name as driver_name
           FROM inspections i
           JOIN vehicles v ON v.id = i.vehicle_id
           LEFT JOIN trips t ON t.id = i.trip_id
           LEFT JOIN drivers d ON d.id = i.driver_id
           WHERE i.id = ?""",
        (inspection_id,),
    )
    if inspection is None:
        abort(404)
    items = query_all("SELECT * FROM inspection_items WHERE inspection_id = ?", (inspection_id,))
    from datetime import datetime

    return render_template(
        "inspecciones/print.html", inspection=inspection, items=items,
        generated_at=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )
