import uuid
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from app import storage
from app.auth import permission_required, validate_csrf
from app.bulk_import import (
    OIL_CHANGE_COLUMNS,
    OIL_CHANGE_EXAMPLE,
    VEHICLE_COLUMNS,
    VEHICLE_EXAMPLE,
    XLSX_MIME,
    build_import_template,
    read_import_rows,
)
from app.db import execute, get_db, query_all, query_one
from app.helpers import parse_date, parse_float, today_str

bp = Blueprint("flota", __name__, url_prefix="/flota")

DOCUMENT_ALERT_DAYS = 30
VEHICLE_DOCUMENT_FIELDS = [
    ("soat_expiry", "SOAT"),
    ("technical_review_expiry", "Revisión técnica"),
]

# Documentos escaneados de una unidad (9 sep, pedido de Braulio: "en el caso
# de tracto ... Tarjeta de propiedad, SOat, revision tecnica, MTC y poliza
# de responsabilidad civil. En el caso de las carretas tarjeta de
# propiedad, revision tecnica y mtc"). Cada tupla es
# (clave_en_url, columna_en_bd, campo_del_formulario, etiqueta,
# tipos_que_lo_necesitan, opcional). CAMION se trata igual que TRACTO
# (unidad completa que circula sola) — Braulio no lo mencionó
# explícitamente, pero no tiene sentido excluirlo de documentos que exige
# SUNAT/MTC a cualquier vehículo que no sea un remolque. Mismo criterio de
# formatos permitidos que la guía de transportista de un viaje (ver
# ALLOWED_WAYBILL_EXTENSIONS en app/routes/viajes.py): foto o PDF.
#
# "Revisión técnica especial" (9 sep, pedido de Braulio) se agregó como un
# documento APARTE de la Revisión técnica normal — no todas las unidades la
# tramitan (aplica a casos particulares, ej. conversión a GLP/GNV o
# transporte de mercancías especiales), así que es el único documento
# marcado `opcional=True`: no se resalta como faltante en ningún lado ni
# bloquea nada, simplemente está disponible para subirla si corresponde.
# Se dejó disponible para los 3 tipos de unidad (igual que Revisión
# técnica) — avisar si en la práctica solo debe aplicar a algunos.
VEHICLE_DOCUMENT_TYPES = [
    ("tarjeta-propiedad", "property_card_filename", "property_card_file", "Tarjeta de propiedad", {"CAMION", "TRACTO", "CARRETA"}, False),
    ("soat", "soat_filename", "soat_file", "SOAT", {"CAMION", "TRACTO"}, False),
    ("revision-tecnica", "technical_review_filename", "technical_review_file", "Revisión técnica", {"CAMION", "TRACTO", "CARRETA"}, False),
    ("revision-tecnica-especial", "special_technical_review_filename", "special_technical_review_file", "Revisión técnica especial", {"CAMION", "TRACTO", "CARRETA"}, True),
    ("mtc", "mtc_filename", "mtc_file", "MTC", {"CAMION", "TRACTO", "CARRETA"}, False),
    ("poliza-responsabilidad-civil", "civil_liability_policy_filename", "civil_liability_policy_file", "Póliza de responsabilidad civil", {"CAMION", "TRACTO"}, False),
]
VEHICLE_DOCUMENT_TYPES_BY_KEY = {key: t for t in VEHICLE_DOCUMENT_TYPES for key in [t[0]]}

ALLOWED_VEHICLE_DOCUMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".webp", ".heic", ".heif"}
VEHICLE_DOCUMENT_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}


def _save_vehicle_document_file(file_storage):
    """Igual que _save_waybill_file() en app/routes/viajes.py, pero
    guardando con storage.save_vehicle_document(). Devuelve el nombre
    guardado, o None si no se subió nada válido."""
    import os

    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_VEHICLE_DOCUMENT_EXTENSIONS:
        ext = VEHICLE_DOCUMENT_MIME_TO_EXTENSION.get((file_storage.mimetype or "").lower())
    if not ext:
        return None
    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None
    filename = f"{uuid.uuid4().hex}{ext}"
    storage.save_vehicle_document(filename, raw_bytes)
    return filename


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
    """3 sep, pedido de Braulio: "eliminar" una unidad con viajes asociados
    no la borra de verdad (se marca INACTIVO para no romper el historial de
    esos viajes — ver delete_vehicle), pero la lista mostraba TODAS las
    unidades sin importar su estado, así que esa unidad se quedaba ahí mismo
    con solo una etiqueta "Inactivo" — daba la sensación de "se eliminó y
    volvió a aparecer". Ahora la lista principal solo muestra unidades
    activas/en mantenimiento por defecto; las inactivas quedan aparte, en
    ?ver=inactivas, con un enlace para ir y volver.

    9 sep, pedido de Braulio: la lista también sale enumerada y se puede
    filtrar por Tipo, Estado y Modelo (query string, mismo patrón de
    filtros que viajes/list.html). Si se elige un Estado explícito, ese
    filtro manda sobre el criterio activas/inactivas de arriba (si no,
    ?ver=inactivas seguiría "peleando" con, por ejemplo, status=ACTIVO y
    nunca mostraría nada)."""
    show_inactive = request.args.get("ver") == "inactivas"
    vehicle_type = request.args.get("vehicle_type", "").strip().upper()
    status = request.args.get("status", "").strip().upper()
    model = request.args.get("model", "").strip()

    conditions = []
    params = []
    if status in ("ACTIVO", "MANTENIMIENTO", "INACTIVO"):
        conditions.append("status = ?")
        params.append(status)
    elif show_inactive:
        conditions.append("status = 'INACTIVO'")
    else:
        conditions.append("status != 'INACTIVO'")
    if vehicle_type in ("CAMION", "TRACTO", "CARRETA"):
        conditions.append("vehicle_type = ?")
        params.append(vehicle_type)
    if model:
        conditions.append("model LIKE ?")
        params.append(f"%{model}%")
    vehicles = query_all(
        f"SELECT * FROM vehicles WHERE {' AND '.join(conditions)} ORDER BY plate", tuple(params)
    )
    inactive_count = query_one("SELECT COUNT(*) n FROM vehicles WHERE status = 'INACTIVO'")["n"]
    return render_template(
        "flota/list.html",
        vehicles=vehicles,
        show_inactive=show_inactive,
        inactive_count=inactive_count,
        vehicle_type_filter=vehicle_type,
        status_filter=status,
        model_filter=model,
    )


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
    return render_template("flota/detail.html", vehicle=vehicle, document_types=VEHICLE_DOCUMENT_TYPES)


@bp.route("/<int:vehicle_id>/documentos", methods=["POST"])
@permission_required("flota", "edit")
def save_vehicle_documents(vehicle_id):
    """9 sep, pedido de Braulio: subir/actualizar los documentos escaneados
    de una unidad (ver VEHICLE_DOCUMENT_TYPES) — Tarjeta de propiedad, SOAT,
    Revisión técnica, MTC y Póliza de responsabilidad civil para
    tracto/camión; Tarjeta de propiedad, Revisión técnica y MTC para
    carretas. Solo se actualiza la columna de los documentos que trajeron
    un archivo nuevo en este envío; el resto conserva el archivo que ya
    tenía (mismo criterio que save_waybill() en app/routes/viajes.py)."""
    if not validate_csrf():
        abort(400)
    vehicle = query_one("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None:
        abort(404)
    updates = []
    params = []
    any_file_sent = False
    for key, column, form_field, label, applies_to, optional in VEHICLE_DOCUMENT_TYPES:
        if vehicle["vehicle_type"] not in applies_to:
            continue
        file_storage = request.files.get(form_field)
        if file_storage and file_storage.filename:
            any_file_sent = True
        new_filename = _save_vehicle_document_file(file_storage)
        if new_filename:
            updates.append(f"{column} = ?")
            params.append(new_filename)
    if updates:
        params.append(vehicle_id)
        execute(f"UPDATE vehicles SET {', '.join(updates)} WHERE id = ?", tuple(params))
        flash("Documentos actualizados.", "success")
    elif any_file_sent:
        flash("No se pudo guardar el archivo: use PDF, JPG, PNG, WEBP o HEIC.", "error")
    else:
        flash("No se subió ningún archivo nuevo.", "error")
    return redirect(url_for("flota.vehicle_detail", vehicle_id=vehicle_id))


@bp.route("/<int:vehicle_id>/documentos/<doc_key>")
@permission_required("flota", "view")
def vehicle_document_file(vehicle_id, doc_key):
    doc_type = VEHICLE_DOCUMENT_TYPES_BY_KEY.get(doc_key)
    if doc_type is None:
        abort(404)
    _, column, _, _, _, _ = doc_type
    vehicle = query_one(f"SELECT {column} AS filename FROM vehicles WHERE id = ?", (vehicle_id,))
    if vehicle is None or not vehicle["filename"]:
        abort(404)
    filename = vehicle["filename"]
    if storage.using_s3():
        return redirect(storage.vehicle_document_url(filename))
    return send_from_directory(storage.local_vehicle_documents_dir(), filename)


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
               soat_expiry, technical_review_expiry, current_km, current_km_updated_at, gps_external_id, owner,
               last_oil_change_km, last_oil_change_date, last_oil_change_workshop, last_oil_change_oil)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                parse_float(request.form.get("last_oil_change_km"), None),
                parse_date(request.form.get("last_oil_change_date")),
                request.form.get("last_oil_change_workshop", "").strip() or None,
                request.form.get("last_oil_change_oil", "").strip() or None,
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
               current_km=?, current_km_updated_at=?, gps_external_id=?, owner=?,
               last_oil_change_km=?, last_oil_change_date=?, last_oil_change_workshop=?, last_oil_change_oil=?
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
                parse_float(request.form.get("last_oil_change_km"), None),
                parse_date(request.form.get("last_oil_change_date")),
                request.form.get("last_oil_change_workshop", "").strip() or None,
                request.form.get("last_oil_change_oil", "").strip() or None,
                vehicle_id,
            ),
        )
        flash("Unidad actualizada.", "success")
        return redirect(url_for("flota.list_view"))
    return render_template(
        "flota/vehicle_form.html", vehicle=vehicle, mode="edit", vehicle_id=vehicle_id, owners=_vehicle_owners()
    )


# Tablas con historial "de negocio" ligado a una unidad — si tiene alguna
# fila en cualquiera de estas, no se borra de verdad al "Eliminar" (se
# marca INACTIVO en su lugar, ver delete_vehicle). Antes solo se revisaba
# "trips" — pero vehicles.id también tiene foreign key desde estas otras
# (ver schema.sql), así que un DELETE directo fallaba con un error 500 en
# Postgres (RDS sí valida las FK; SQLite local también, con PRAGMA
# foreign_keys=ON) apenas la unidad tenía, por ejemplo, una inspección o un
# neumático registrado pero ningún viaje — el caso real que reportó Braulio
# (3 sep, unidad id=4: "Internal Server Error" al eliminar).
VEHICLE_HISTORY_TABLES = ["trips", "expenses", "maintenance_records", "tires", "tire_rotations", "inspections"]

# "trips" tiene DOS columnas que apuntan a vehicles.id — vehicle_id (el
# tracto) y trailer_vehicle_id (la carreta, agregada el 3 sep con el
# rediseño de Viajes) — así que no basta con filtrar por vehicle_id: una
# carreta que solo aparece como trailer_vehicle_id en algún viaje pasaba
# _vehicle_has_history() como "sin historial" e intentaba un DELETE directo,
# violando la foreign key fk_trips_trailer_vehicle_id (500 real en
# producción, 7 sep, unidad id=57).
VEHICLE_HISTORY_EXTRA_COLUMNS = [("trips", "trailer_vehicle_id")]


def _vehicle_has_history(vehicle_id):
    if any(
        query_one(f"SELECT COUNT(*) n FROM {table} WHERE vehicle_id = ?", (vehicle_id,))["n"]
        for table in VEHICLE_HISTORY_TABLES
    ):
        return True
    return any(
        query_one(f"SELECT COUNT(*) n FROM {table} WHERE {column} = ?", (vehicle_id,))["n"]
        for table, column in VEHICLE_HISTORY_EXTRA_COLUMNS
    )


@bp.route("/<int:vehicle_id>/eliminar", methods=["POST"])
@permission_required("flota", "edit")
def delete_vehicle(vehicle_id):
    if not validate_csrf():
        abort(400)
    if _vehicle_has_history(vehicle_id):
        execute("UPDATE vehicles SET status = 'INACTIVO' WHERE id = ?", (vehicle_id,))
        flash(
            "La unidad tiene historial asociado (viajes, gastos, mantenimiento, neumáticos o "
            "inspecciones); se marcó como inactiva para no perder ese historial.",
            "success",
        )
    else:
        # Sin historial "de negocio", pero puede tener datos de rastreo GPS
        # (vehicle_locations/vehicle_location_history/vehicle_trips) — esos
        # no tienen valor propio sin la unidad, así que se borran junto con
        # ella en vez de bloquear el borrado por esto.
        db = get_db()
        db.execute("DELETE FROM vehicle_locations WHERE vehicle_id = ?", (vehicle_id,))
        db.execute("DELETE FROM vehicle_location_history WHERE vehicle_id = ?", (vehicle_id,))
        db.execute("DELETE FROM vehicle_trips WHERE vehicle_id = ?", (vehicle_id,))
        db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        db.commit()
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


# --- Importación masiva de últimos cambios de aceite (3 sep, pedido de
# Braulio) — módulo aparte de la importación general de Flota de arriba en
# vez de extender _apply_vehicle_import(): esta SOLO actualiza unidades que
# ya existen (nunca crea una unidad nueva, a diferencia de la importación
# general) y actualiza 4 campos puntuales sin tocar el resto de la unidad,
# así que mezclarla con _apply_vehicle_import() habría complicado esa
# función y además cambiado su comportamiento para otros casos futuros que
# no tienen que ver con aceite. "Observación" (lo que pidió Braulio agregar
# junto con esta carga) no es una columna de esta plantilla: es el campo
# "notes" que ya existe en Flota (se llena a mano, como hasta ahora, desde
# el formulario de la unidad).

@bp.route("/importar-aceite/plantilla")
@permission_required("flota", "edit")
def import_oil_changes_template():
    buffer = build_import_template("Flota — últimos cambios de aceite", OIL_CHANGE_COLUMNS, OIL_CHANGE_EXAMPLE)
    return Response(
        buffer.getvalue(),
        mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_cambios_aceite.xlsx"'},
    )


def _apply_oil_change_import(rows, example_skips):
    updated, errors = 0, []
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
        existing = query_one("SELECT id FROM vehicles WHERE plate = ?", (plate,))
        if not existing:
            # Pedido explícito de Braulio: esta importación no crea
            # unidades nuevas, solo actualiza las que ya existen en Flota.
            skipped.append({"row": n, "message": f"La placa {plate} no está registrada en Flota; no se creó (esta carga solo actualiza unidades existentes)."})
            continue
        seen_plates.add(plate)
        execute(
            """UPDATE vehicles SET last_oil_change_km=?, last_oil_change_date=?,
               last_oil_change_workshop=?, last_oil_change_oil=? WHERE id=?""",
            (
                row.get("oil_change_km"),
                row.get("oil_change_date"),
                row.get("workshop") or None,
                row.get("oil_type") or None,
                existing["id"],
            ),
        )
        updated += 1
    return {"created": 0, "updated": updated, "skipped": skipped, "errors": errors}


@bp.route("/importar-aceite", methods=["GET", "POST"])
@permission_required("flota", "edit")
def import_oil_changes():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(
            request.files.get("file"), OIL_CHANGE_COLUMNS, OIL_CHANGE_EXAMPLE
        )
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("flota.import_oil_changes"))
        result = _apply_oil_change_import(rows, example_skips)
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("flota.list_view"), retry_url=url_for("flota.import_oil_changes"),
        )
    return render_template(
        "import_form.html", title="Importar últimos cambios de aceite",
        module_label="los últimos cambios de aceite de Flota",
        template_url=url_for("flota.import_oil_changes_template"), upload_url=url_for("flota.import_oil_changes"),
        back_url=url_for("flota.list_view"), columns=OIL_CHANGE_COLUMNS,
    )
