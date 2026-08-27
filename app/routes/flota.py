from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, today_str

bp = Blueprint("flota", __name__, url_prefix="/flota")

DOCUMENT_ALERT_DAYS = 30
VEHICLE_DOCUMENT_FIELDS = [
    ("soat_expiry", "SOAT"),
    ("technical_review_expiry", "Revisión técnica"),
]


def vehicle_document_alerts():
    """Unidades cuyo SOAT o Revisión Técnica vence dentro de
    DOCUMENT_ALERT_DAYS días (o ya venció), para el Panel."""
    alerts = []
    for field, label in VEHICLE_DOCUMENT_FIELDS:
        rows = query_all(
            f"""SELECT plate, {field} AS expiry FROM vehicles
                WHERE status != 'INACTIVO' AND {field} IS NOT NULL AND {field} != ''
                AND date({field}) <= date('now', '+{DOCUMENT_ALERT_DAYS} days')
                ORDER BY {field} ASC"""
        )
        today = today_str()
        for r in rows:
            alerts.append(
                {"plate": r["plate"], "document": label, "expiry": r["expiry"], "overdue": r["expiry"] < today}
            )
    alerts.sort(key=lambda a: a["expiry"])
    return alerts


@bp.route("")
@permission_required("flota", "view")
def list_view():
    vehicles = query_all("SELECT * FROM vehicles ORDER BY plate")
    return render_template("flota/list.html", vehicles=vehicles)


# --- Vehículos ---

@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("flota", "edit")
def new_vehicle():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        plate = request.form.get("plate", "").strip().upper()
        if not plate:
            flash("La placa es obligatoria.", "error")
            return render_template("flota/vehicle_form.html", vehicle=request.form, mode="new")
        existing = query_one("SELECT id FROM vehicles WHERE plate = ?", (plate,))
        if existing:
            flash("Ya existe una unidad con esa placa.", "error")
            return render_template("flota/vehicle_form.html", vehicle=request.form, mode="new")
        execute(
            """INSERT INTO vehicles (plate, brand, model, capacity_kg, status, vehicle_type, notes,
               soat_expiry, technical_review_expiry, current_km, current_km_updated_at, gps_external_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plate,
                request.form.get("brand", "").strip(),
                request.form.get("model", "").strip(),
                request.form.get("capacity_kg") or None,
                request.form.get("status", "ACTIVO"),
                request.form.get("vehicle_type", "CAMION"),
                request.form.get("notes", "").strip(),
                parse_date(request.form.get("soat_expiry")),
                parse_date(request.form.get("technical_review_expiry")),
                parse_float(request.form.get("current_km"), None),
                today_str() if request.form.get("current_km") else None,
                request.form.get("gps_external_id", "").strip() or None,
            ),
        )
        flash("Unidad registrada.", "success")
        return redirect(url_for("flota.list_view"))
    return render_template("flota/vehicle_form.html", vehicle=None, mode="new")


@bp.route("/<int:vehicle_id>/editar", methods=["GET", "POST"])
@permission_required("flota", "edit")
def edit_vehicle(vehicle_id):
    vehicle = query_one("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        new_km = parse_float(request.form.get("current_km"), None)
        km_changed = new_km is not None and new_km != vehicle["current_km"]
        execute(
            """UPDATE vehicles SET plate=?, brand=?, model=?, capacity_kg=?, status=?, vehicle_type=?, notes=?,
               soat_expiry=?, technical_review_expiry=?,
               current_km=?, current_km_updated_at=?, gps_external_id=?
               WHERE id=?""",
            (
                request.form.get("plate", "").strip().upper(),
                request.form.get("brand", "").strip(),
                request.form.get("model", "").strip(),
                request.form.get("capacity_kg") or None,
                request.form.get("status", "ACTIVO"),
                request.form.get("vehicle_type", "CAMION"),
                request.form.get("notes", "").strip(),
                parse_date(request.form.get("soat_expiry")),
                parse_date(request.form.get("technical_review_expiry")),
                new_km,
                today_str() if km_changed else vehicle["current_km_updated_at"],
                request.form.get("gps_external_id", "").strip() or None,
                vehicle_id,
            ),
        )
        flash("Unidad actualizada.", "success")
        return redirect(url_for("flota.list_view"))
    return render_template("flota/vehicle_form.html", vehicle=vehicle, mode="edit", vehicle_id=vehicle_id)


@bp.route("/<int:vehicle_id>/eliminar", methods=["POST"])
@permission_required("flota", "edit")
def delete_vehicle(vehicle_id):
    if not validate_csrf():
        abort(400)
    in_use = query_one("SELECT COUNT(*) n FROM trips WHERE vehicle_id = ?", (vehicle_id,))["n"]
    if in_use:
        execute("UPDATE vehicles SET status = 'INACTIVO' WHERE id = ?", (vehicle_id,))
        flash("La unidad tiene viajes asociados; se marcó como inactiva.", "success")
    else:
        execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        flash("Unidad eliminada.", "success")
    return redirect(url_for("flota.list_view"))
