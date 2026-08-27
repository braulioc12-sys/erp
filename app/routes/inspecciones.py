from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.detailed_checklists import (
    CHECKLIST_LABELS,
    DETAILED_CHECKLIST_TYPES,
    HAS_ODOMETER,
    LOCATIONS,
    SPARE_TIRE_ITEM,
    TIRE_SECTION_KEY,
    VEHICLE_FIELD_LABELS,
    sections_for,
    tire_meta_for,
)
from app.helpers import next_code, parse_date, parse_float, today_str
from app.routes.catalogos import get_catalog
from app.tire_positions import get_positions

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


def _get_vehicle(vehicle_id):
    if not vehicle_id:
        return None
    return query_one("SELECT id, plate, vehicle_type, current_km FROM vehicles WHERE id = ?", (vehicle_id,))


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

    vehicles = query_all("SELECT id, plate, vehicle_type FROM vehicles ORDER BY plate")
    drivers = query_all("SELECT id, name FROM drivers WHERE status = 'ACTIVO' ORDER BY name")

    # La unidad puede venir del viaje, de un ?vehicle_id= (al elegirla en el
    # selector, que recarga la página) o del propio formulario al enviarlo.
    vehicle_id = (trip["vehicle_id"] if trip else None) or request.values.get("vehicle_id")
    vehicle = _get_vehicle(vehicle_id)
    vehicle_type = vehicle["vehicle_type"] if vehicle else None
    is_detailed = vehicle_type in DETAILED_CHECKLIST_TYPES

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        vehicle_id = request.form.get("vehicle_id") or (trip["vehicle_id"] if trip else None)
        vehicle = _get_vehicle(vehicle_id)
        if vehicle is None:
            flash("Selecciona una unidad.", "error")
            return render_template(
                "inspecciones/form.html", trip=trip, vehicles=vehicles, drivers=drivers,
                items=get_catalog("inspection_item"), today=today_str(),
            )

        if vehicle["vehicle_type"] in DETAILED_CHECKLIST_TYPES:
            return _save_detailed_inspection(trip_id, trip, vehicle)
        return _save_generic_inspection(trip_id, trip, vehicle_id)

    if is_detailed:
        return render_template(
            "inspecciones/form_checklist.html", trip=trip, vehicle=vehicle, vehicles=vehicles, drivers=drivers,
            vehicle_type=vehicle_type, checklist_label=CHECKLIST_LABELS[vehicle_type],
            vehicle_field_label=VEHICLE_FIELD_LABELS[vehicle_type], has_odometer=HAS_ODOMETER[vehicle_type],
            sections=sections_for(vehicle_type), tire_positions=get_positions(vehicle_type),
            tire_meta=tire_meta_for(vehicle_type), spare_tire_item=SPARE_TIRE_ITEM,
            locations=LOCATIONS, today=today_str(),
        )

    return render_template(
        "inspecciones/form.html", trip=trip, vehicles=vehicles, drivers=drivers,
        items=get_catalog("inspection_item"), today=today_str(), selected_vehicle_id=vehicle_id,
    )


def _save_generic_inspection(trip_id, trip, vehicle_id):
    items = get_catalog("inspection_item")
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


def _save_detailed_inspection(trip_id, trip, vehicle):
    vehicle_type = vehicle["vehicle_type"]
    sections = sections_for(vehicle_type)
    tire_meta = tire_meta_for(vehicle_type)
    has_odometer = HAS_ODOMETER.get(vehicle_type, False)

    inspection_date = parse_date(request.form.get("inspection_date")) or today_str()
    odometer_km = parse_float(request.form.get("odometer_km"), None) if has_odometer else None
    checklist_code = next_code("CL", "inspections")

    inspection_id = execute(
        """INSERT INTO inspections (vehicle_id, trip_id, driver_id, type, inspection_date, notes,
           checklist_code, location, odometer_km, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            vehicle["id"],
            trip_id,
            request.form.get("driver_id") or (trip["driver_id"] if trip else None),
            request.form.get("type", "PRE"),
            inspection_date,
            request.form.get("notes", "").strip(),
            checklist_code,
            request.form.get("location", "").strip(),
            odometer_km,
            None,
        ),
    )

    if odometer_km is not None:
        execute(
            "UPDATE vehicles SET current_km = ?, current_km_updated_at = ? WHERE id = ?",
            (odometer_km, inspection_date, vehicle["id"]),
        )

    db = get_db()
    for section in sections:
        for idx, item_name in enumerate(section["checklist_items"]):
            prefix = f"item_{section['key']}_{idx}"
            if section["status_labels"][1] is None:
                # Sección de una sola columna (checkbox "Completo").
                status = "OK" if request.form.get(f"{prefix}_status") else "FALLA"
            else:
                status = request.form.get(f"{prefix}_status", "FALLA")
            extra_value = None
            if section["extra_field"]:
                extra_value = request.form.get(f"{prefix}_extra", "").strip() or None
            observation = request.form.get(f"{prefix}_obs", "").strip()
            db.execute(
                """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (inspection_id, item_name, status, observation, section["key"], extra_value),
            )

    def _tire_observation(field_prefix):
        observation = request.form.get(f"{field_prefix}_obs", "").strip()
        if tire_meta.get("has_pressure"):
            presion = request.form.get(f"{field_prefix}_presion", "").strip()
            if presion:
                observation = f"Presión: {presion}." + (f" {observation}" if observation else "")
        return observation

    for p in get_positions(vehicle_type):
        codigo = request.form.get(f"tire_{p['code']}_codigo", "").strip() or None
        observation = _tire_observation(f"tire_{p['code']}")
        db.execute(
            """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
               VALUES (?, ?, 'NA', ?, ?, ?)""",
            (inspection_id, p["label"], observation, TIRE_SECTION_KEY, codigo),
        )
    spare_codigo = request.form.get("tire_spare_codigo", "").strip() or None
    spare_obs = _tire_observation("tire_spare")
    db.execute(
        """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
           VALUES (?, ?, 'NA', ?, ?, ?)""",
        (inspection_id, SPARE_TIRE_ITEM, spare_obs, TIRE_SECTION_KEY, spare_codigo),
    )
    db.commit()

    flash(f"{CHECKLIST_LABELS.get(vehicle_type, 'Checklist')} {checklist_code} registrado.", "success")
    if trip_id:
        return redirect(url_for("viajes.detail", trip_id=trip_id))
    return redirect(url_for("inspecciones.detail", inspection_id=inspection_id))


@bp.route("/<int:inspection_id>")
@permission_required("inspecciones", "view")
def detail(inspection_id):
    inspection = query_one(
        """SELECT i.*, v.plate as vehicle_plate, v.vehicle_type as vehicle_type, t.code as trip_code,
                  d.name as driver_name
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

    if inspection["checklist_code"]:
        return render_template(
            "inspecciones/detail_checklist.html", inspection=inspection,
            checklist_label=CHECKLIST_LABELS.get(inspection["vehicle_type"], "Checklist"),
            has_odometer=HAS_ODOMETER.get(inspection["vehicle_type"], True),
            **_group_detailed_items(items, inspection["vehicle_type"]),
        )
    return render_template("inspecciones/detail.html", inspection=inspection, items=items)


def _group_detailed_items(items, vehicle_type):
    """Agrupa los ítems planos de `inspection_items` de vuelta por sección,
    para las plantillas del checklist detallado (detalle y PDF), sea de
    tracto o de carreta."""
    by_section = {}
    tire_rows = []
    for it in items:
        if it["section"] == TIRE_SECTION_KEY:
            tire_rows.append(it)
        else:
            by_section.setdefault(it["section"], []).append(it)

    sections = []
    for s in sections_for(vehicle_type):
        sections.append({"meta": s, "rows": by_section.get(s["key"], [])})

    spare = next((r for r in tire_rows if r["item_name"] == SPARE_TIRE_ITEM), None)
    positions = [r for r in tire_rows if r["item_name"] != SPARE_TIRE_ITEM]
    tire_meta = tire_meta_for(vehicle_type)
    return {
        "sections": sections,
        "tire_rows": positions,
        "spare_tire": spare,
        "tire_section_title": tire_meta["title"],
        "tire_section_note": tire_meta["note"],
    }


@bp.route("/<int:inspection_id>/imprimir")
@permission_required("inspecciones", "view")
def print_view(inspection_id):
    """Vista de impresión de una inspección: página independiente (sin menú
    lateral) con el logo de la empresa en el encabezado, pensada para
    imprimirse o guardarse como PDF desde el propio diálogo de impresión
    del navegador (Ctrl+P → Guardar como PDF), sin depender de ninguna
    librería de generación de PDF en el servidor."""
    inspection = query_one(
        """SELECT i.*, v.plate as vehicle_plate, v.vehicle_type as vehicle_type, v.brand as vehicle_brand,
                  v.model as vehicle_model, t.code as trip_code, d.name as driver_name
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
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M")

    if inspection["checklist_code"]:
        return render_template(
            "inspecciones/print_checklist.html", inspection=inspection, generated_at=generated_at,
            checklist_label=CHECKLIST_LABELS.get(inspection["vehicle_type"], "Checklist"),
            vehicle_field_label=VEHICLE_FIELD_LABELS.get(inspection["vehicle_type"], "Unidad"),
            has_odometer=HAS_ODOMETER.get(inspection["vehicle_type"], True),
            **_group_detailed_items(items, inspection["vehicle_type"]),
        )
    return render_template(
        "inspecciones/print.html", inspection=inspection, items=items, generated_at=generated_at,
    )
