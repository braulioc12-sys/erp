from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import get_db, query_all
from app.helpers import today_str
from app.integrations.frotcom import FrotcomError, build_client_from_config

bp = Blueprint("integraciones", __name__, url_prefix="/configuracion/integraciones")


@bp.route("")
@permission_required("integraciones", "view")
def index():
    client = build_client_from_config(current_app.config)
    vehicles = query_all(
        """SELECT v.id, v.plate, v.gps_external_id, v.current_km, v.current_km_updated_at,
                  l.latitude, l.longitude, l.speed_kmh, l.recorded_at, l.updated_at as location_updated_at
           FROM vehicles v
           LEFT JOIN vehicle_locations l ON l.vehicle_id = v.id
           ORDER BY v.plate"""
    )
    return render_template(
        "integraciones/index.html", vehicles=vehicles, configured=client.is_configured(),
    )


@bp.route("/frotcom/sincronizar", methods=["POST"])
@permission_required("integraciones", "edit")
def sync_frotcom():
    if not validate_csrf():
        flash("Sesión expirada, intenta de nuevo.", "error")
        return redirect(url_for("integraciones.index"))

    client = build_client_from_config(current_app.config)
    if not client.is_configured():
        flash(
            "Frotcom no está configurado todavía. Define FROTCOM_BASE_URL, FROTCOM_USERNAME "
            "y FROTCOM_PASSWORD en las variables de entorno (ver README) y vuelve a intentar.",
            "error",
        )
        return redirect(url_for("integraciones.index"))

    try:
        positions = client.get_vehicle_positions()
    except FrotcomError as exc:
        flash(f"No se pudo sincronizar con Frotcom: {exc}", "error")
        return redirect(url_for("integraciones.index"))

    vehicles = query_all("SELECT id, gps_external_id FROM vehicles WHERE gps_external_id IS NOT NULL")
    by_external_id = {v["gps_external_id"]: v["id"] for v in vehicles}

    db = get_db()
    matched = 0
    for pos in positions:
        vehicle_id = by_external_id.get(pos["external_id"])
        if not vehicle_id:
            continue
        db.execute(
            """INSERT INTO vehicle_locations (vehicle_id, latitude, longitude, speed_kmh, heading, odometer_km, recorded_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(vehicle_id) DO UPDATE SET
                 latitude=excluded.latitude, longitude=excluded.longitude, speed_kmh=excluded.speed_kmh,
                 heading=excluded.heading, odometer_km=excluded.odometer_km, recorded_at=excluded.recorded_at,
                 updated_at=datetime('now')""",
            (
                vehicle_id, pos["latitude"], pos["longitude"], pos["speed_kmh"],
                pos["heading"], pos["odometer_km"], pos["recorded_at"],
            ),
        )
        if pos.get("odometer_km"):
            db.execute(
                "UPDATE vehicles SET current_km = ?, current_km_updated_at = ? WHERE id = ?",
                (pos["odometer_km"], today_str(), vehicle_id),
            )
        matched += 1
    db.commit()

    if matched:
        flash(f"Sincronizado: {matched} unidad(es) actualizada(s) desde Frotcom.", "success")
    else:
        flash(
            "Frotcom respondió pero no se pudo asociar ninguna posición a tus unidades. "
            "Revisa que el campo \"ID en el proveedor de GPS\" de cada unidad (en Flota) "
            "coincida exactamente con el identificador que usa Frotcom.",
            "error",
        )
    return redirect(url_for("integraciones.index"))
