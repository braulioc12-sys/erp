from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.bulk_import import VEHICLE_COLUMNS, VEHICLE_EXAMPLE, XLSX_MIME, build_import_template, read_import_rows
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


def _vehicle_owners():
    return query_all(
        "SELECT * FROM catalog_items WHERE category = 'vehicle_owner' AND active = 1 ORDER BY sort_order, name"
    )


@bp.route("")
@permission_required("flota", "view")
def list_view():
    vehicles = query_all("SELECT * FROM vehicles ORDER BY plate")
    return render_template("flota/list.html", vehicles=vehicles)


@bp.route("/<int:vehicle_id>")
@permission_required("flota", "view")
def vehicle_detail(vehicle_id):
    """2 sep, pedido de Braulio: lista principal de Flota vuelta compacta
    (mismo patrón ya usado en Conductores) — el resto de los datos de cada
    unidad, incluido el kilometraje actual (ahora alimentado por GPS, ver
    integraciones Frotcom), se ven acá en el detalle."""
    vehicle = query_one("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    return render_template("flota/detail.html", vehicle=vehicle)


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
            return render_template("flota/vehicle_form.html", vehicle=request.form, mode="new", owners=_vehicle_owners())
        existing = query_one("SELECT id FROM vehicles WHERE plate = ?", (plate,))
        if existing:
            flash("Ya existe una unidad con esa placa.", "error")
            return render_template("flota/vehicle_form.html", vehicle=request.form, mode="new", owners=_vehicle_owners())
        execute(
            """INSERT INTO vehicles (plate, brand, model, capacity_kg, status, vehicle_type, notes,
               soat_expiry, technical_review_expiry, current_km, current_km_updated_at, gps_external_id, owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                request.form.get("owner", "").strip() or None,
            ),
        )
        flash("Unidad registrada.", "success")
        return redirect(url_for("flota.list_view"))
    return render_template("flota/vehicle_form.html", vehicle=None, mode="new", owners=_vehicle_owners())


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
               current_km=?, current_km_updated_at=?, gps_external_id=?, owner=?
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
                request.form.get("owner", "").strip() or None,
                vehicle_id,
            ),
        )
        flash("Unidad actualizada.", "success")
        return redirect(url_for("flota.list_view"))
    return render_template(
        "flota/vehicle_form.html", vehicle=vehicle, mode="edit", vehicle_id=vehicle_id, owners=_vehicle_owners()
    )


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


# --- Importación masiva desde Excel (30 ago, pedido de Braulio) ---

@bp.route("/importar/plantilla")
@permission_required("flota", "edit")
def import_template():
    buffer = build_import_template("Flota (unidades)", VEHICLE_COLUMNS, VEHICLE_EXAMPLE)
    return Response(
        buffer.getvalue(),
        mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_flota.xlsx"'},
    )


def _apply_vehicle_import(rows, example_skips):
    created, updated, errors = 0, 0, []
    skipped = [
        {"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."}
        for r in example_skips
    ]
    seen_plates = set()
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})
        plate = (row.get("plate") or "").strip().upper()
        if not plate:
            errors.append({"row": n, "message": "Falta la placa; la fila no se importó."})
            continue
        if plate in seen_plates:
            skipped.append({"row": n, "message": f"Placa {plate} repetida dentro del archivo; ya se había importado antes."})
            continue
        gps_external_id = (row.get("gps_external_id") or "").strip() or None
        existing = query_one("SELECT id FROM vehicles WHERE plate = ?", (plate,))
        if existing:
            # 31 ago, pedido de Braulio: al re-importar la flota real ya
            # cargada, no se crea de nuevo ni se toca el resto de la fila —
            # pero si trae "ID en el proveedor de GPS" (para completar el
            # mapeo con Frotcom en bloque) sí se actualiza ese campo puntual.
            if gps_external_id:
                execute(
                    "UPDATE vehicles SET gps_external_id = ? WHERE id = ?",
                    (gps_external_id, existing["id"]),
                )
                seen_plates.add(plate)
                updated += 1
            else:
                skipped.append({"row": n, "message": f"La placa {plate} ya existe en Flota; no se modificó."})
            continue
        seen_plates.add(plate)
        execute(
            """INSERT INTO vehicles (plate, brand, model, capacity_kg, status, vehicle_type, notes,
               soat_expiry, technical_review_expiry, current_km, current_km_updated_at, gps_external_id, owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plate,
                row.get("brand") or "",
                row.get("model") or "",
                row.get("capacity_kg"),
                row.get("status") or "ACTIVO",
                row.get("vehicle_type") or "CAMION",
                row.get("notes") or "",
                row.get("soat_expiry"),
                row.get("technical_review_expiry"),
                row.get("current_km"),
                today_str() if row.get("current_km") is not None else None,
                gps_external_id,
                row.get("owner") or None,
            ),
        )
        created += 1
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


@bp.route("/importar", methods=["GET", "POST"])
@permission_required("flota", "edit")
def import_vehicles():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), VEHICLE_COLUMNS, VEHICLE_EXAMPLE)
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("flota.import_vehicles"))
        result = _apply_vehicle_import(rows, example_skips)
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("flota.list_view"), retry_url=url_for("flota.import_vehicles"),
        )
    return render_template(
        "import_form.html", title="Importar unidades", module_label="las unidades de Flota",
        template_url=url_for("flota.import_template"), upload_url=url_for("flota.import_vehicles"),
        back_url=url_for("flota.list_view"), columns=VEHICLE_COLUMNS,
    )
