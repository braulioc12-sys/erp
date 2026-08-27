from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_float, today_str

bp = Blueprint("flota", __name__, url_prefix="/flota")


@bp.route("")
@permission_required("flota", "view")
def list_view():
    tab = request.args.get("tab", "vehiculos")
    vehicles = query_all("SELECT * FROM vehicles ORDER BY plate")
    drivers = query_all("SELECT * FROM drivers ORDER BY name")
    return render_template("flota/list.html", vehicles=vehicles, drivers=drivers, tab=tab)


# --- Vehículos ---

@bp.route("/vehiculos/nuevo", methods=["GET", "POST"])
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
            """INSERT INTO vehicles (plate, brand, model, capacity_kg, status, notes, current_km, current_km_updated_at, gps_external_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plate,
                request.form.get("brand", "").strip(),
                request.form.get("model", "").strip(),
                request.form.get("capacity_kg") or None,
                request.form.get("status", "ACTIVO"),
                request.form.get("notes", "").strip(),
                parse_float(request.form.get("current_km"), None),
                today_str() if request.form.get("current_km") else None,
                request.form.get("gps_external_id", "").strip() or None,
            ),
        )
        flash("Unidad registrada.", "success")
        return redirect(url_for("flota.list_view", tab="vehiculos"))
    return render_template("flota/vehicle_form.html", vehicle=None, mode="new")


@bp.route("/vehiculos/<int:vehicle_id>/editar", methods=["GET", "POST"])
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
            """UPDATE vehicles SET plate=?, brand=?, model=?, capacity_kg=?, status=?, notes=?,
               current_km=?, current_km_updated_at=?, gps_external_id=?
               WHERE id=?""",
            (
                request.form.get("plate", "").strip().upper(),
                request.form.get("brand", "").strip(),
                request.form.get("model", "").strip(),
                request.form.get("capacity_kg") or None,
                request.form.get("status", "ACTIVO"),
                request.form.get("notes", "").strip(),
                new_km,
                today_str() if km_changed else vehicle["current_km_updated_at"],
                request.form.get("gps_external_id", "").strip() or None,
                vehicle_id,
            ),
        )
        flash("Unidad actualizada.", "success")
        return redirect(url_for("flota.list_view", tab="vehiculos"))
    return render_template("flota/vehicle_form.html", vehicle=vehicle, mode="edit", vehicle_id=vehicle_id)


@bp.route("/vehiculos/<int:vehicle_id>/eliminar", methods=["POST"])
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
    return redirect(url_for("flota.list_view", tab="vehiculos"))


# --- Conductores ---

@bp.route("/conductores/nuevo", methods=["GET", "POST"])
@permission_required("flota", "edit")
def new_driver():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre del conductor es obligatorio.", "error")
            return render_template("flota/driver_form.html", driver=request.form, mode="new")
        execute(
            """INSERT INTO drivers (name, document_number, license_number, license_expiry, phone, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                name,
                request.form.get("document_number", "").strip(),
                request.form.get("license_number", "").strip(),
                request.form.get("license_expiry") or None,
                request.form.get("phone", "").strip(),
                request.form.get("status", "ACTIVO"),
            ),
        )
        flash("Conductor registrado.", "success")
        return redirect(url_for("flota.list_view", tab="conductores"))
    return render_template("flota/driver_form.html", driver=None, mode="new")


@bp.route("/conductores/<int:driver_id>/editar", methods=["GET", "POST"])
@permission_required("flota", "edit")
def edit_driver(driver_id):
    driver = query_one("SELECT * FROM drivers WHERE id = ?", (driver_id,))
    if driver is None:
        abort(404)
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        execute(
            """UPDATE drivers SET name=?, document_number=?, license_number=?, license_expiry=?, phone=?, status=?
               WHERE id=?""",
            (
                request.form.get("name", "").strip(),
                request.form.get("document_number", "").strip(),
                request.form.get("license_number", "").strip(),
                request.form.get("license_expiry") or None,
                request.form.get("phone", "").strip(),
                request.form.get("status", "ACTIVO"),
                driver_id,
            ),
        )
        flash("Conductor actualizado.", "success")
        return redirect(url_for("flota.list_view", tab="conductores"))
    return render_template("flota/driver_form.html", driver=driver, mode="edit", driver_id=driver_id)


@bp.route("/conductores/<int:driver_id>/eliminar", methods=["POST"])
@permission_required("flota", "edit")
def delete_driver(driver_id):
    if not validate_csrf():
        abort(400)
    in_use = query_one("SELECT COUNT(*) n FROM trips WHERE driver_id = ?", (driver_id,))["n"]
    if in_use:
        execute("UPDATE drivers SET status = 'INACTIVO' WHERE id = ?", (driver_id,))
        flash("El conductor tiene viajes asociados; se marcó como inactivo.", "success")
    else:
        execute("DELETE FROM drivers WHERE id = ?", (driver_id,))
        flash("Conductor eliminado.", "success")
    return redirect(url_for("flota.list_view", tab="conductores"))
