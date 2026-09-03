"""Conductores: datos personales y control de vencimientos (licencia de
conducir, examen médico ocupacional, y los requisitos específicos para
operar con Backus — examen de manejo, capacitación (plan de tráfico) y
escuela de conductores). Antes vivía
junto con Flota en un solo módulo con pestañas; se separaron en dos
módulos porque cada uno creció con su propio conjunto de documentos y
vencimientos a controlar.

La lista solo muestra Nombre/DNI/Estado/Documentos (1 sep, pedido de
Braulio: "que solo se vea las columnas Nombre, DNI, Estado y Documentos") —
el resto de columnas (vencimientos, teléfono, foto) se ven en el detalle de
cada conductor (driver_detail), al que se llega haciendo clic en el nombre
en la lista."""
import os
import uuid

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, send_from_directory, url_for

from app import storage
from app.auth import permission_required, validate_csrf
from app.bulk_import import DRIVER_COLUMNS, DRIVER_EXAMPLE, XLSX_MIME, build_import_template, read_import_rows
from app.db import execute, query_all, query_one
from app.helpers import compress_photo, parse_date, today_str

bp = Blueprint("conductores", __name__, url_prefix="/conductores")

# Fotos de conductores: mismo criterio que los comprobantes de gastos
# (compress_photo en app/helpers.py), pero solo imágenes (no PDF).
ALLOWED_PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}
PHOTO_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

DOCUMENT_ALERT_DAYS = 30
# (columna de vencimiento, etiqueta para mostrar en el Panel)
DRIVER_DOCUMENT_FIELDS = [
    ("license_expiry", "Licencia de conducir (brevete)"),
    ("medical_exam_expiry", "Examen médico ocupacional"),
    ("backus_driving_exam_expiry", "Examen de manejo Backus"),
    ("backus_training_expiry", "Capacitación Plan de tráfico Backus"),
    ("dds_expiry", "Escuela de conductores"),
]


def document_alerts():
    """Conductores con algún documento (licencia, examen médico, o
    requisitos Backus) que vence dentro de DOCUMENT_ALERT_DAYS días o ya
    venció, para mostrar en el Panel."""
    alerts = []
    today = today_str()
    for field, label in DRIVER_DOCUMENT_FIELDS:
        rows = query_all(
            f"""SELECT name, {field} AS expiry FROM drivers
                WHERE status = 'ACTIVO' AND {field} IS NOT NULL AND {field} != ''
                AND date({field}) <= date('now', '+{DOCUMENT_ALERT_DAYS} days')
                ORDER BY {field} ASC"""
        )
        for r in rows:
            alerts.append(
                {"name": r["name"], "document": label, "expiry": r["expiry"], "overdue": r["expiry"] < today}
            )
    alerts.sort(key=lambda a: a["expiry"])
    return alerts


def _expiring_driver_ids():
    """IDs de conductores con al menos un documento por vencer (o vencido),
    para marcarlos en la lista sin repetir la consulta de document_alerts()."""
    conditions = " OR ".join(
        f"({field} IS NOT NULL AND {field} != '' AND date({field}) <= date('now', '+{DOCUMENT_ALERT_DAYS} days'))"
        for field, _ in DRIVER_DOCUMENT_FIELDS
    )
    rows = query_all(f"SELECT id FROM drivers WHERE status = 'ACTIVO' AND ({conditions})")
    return {r["id"] for r in rows}


def _driver_fields_from_form(form):
    return (
        form.get("name", "").strip(),
        form.get("document_number", "").strip(),
        form.get("license_number", "").strip(),
        parse_date(form.get("license_expiry")),
        parse_date(form.get("medical_exam_date")),
        parse_date(form.get("medical_exam_expiry")),
        parse_date(form.get("backus_driving_exam_date")),
        parse_date(form.get("backus_driving_exam_expiry")),
        parse_date(form.get("backus_training_date")),
        parse_date(form.get("backus_training_expiry")),
        parse_date(form.get("dds_date")),
        parse_date(form.get("dds_expiry")),
        form.get("phone", "").strip(),
        form.get("status", "ACTIVO"),
    )


def _save_driver_photo(file_storage):
    """Guarda la foto de un conductor (comprimida vía compress_photo) y
    devuelve el nombre de archivo guardado, o None si no se subió nada
    (el campo del formulario es opcional) o el archivo no es una imagen
    reconocible."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_PHOTO_EXTENSIONS:
        ext = PHOTO_MIME_TO_EXTENSION.get((file_storage.mimetype or "").lower())
    if not ext:
        return None
    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None
    compressed = compress_photo(raw_bytes)
    if compressed is not None:
        filename = f"{uuid.uuid4().hex}.jpg"
        storage.save_driver_photo(filename, compressed)
        return filename
    # No se pudo abrir como imagen (formato raro o archivo corrupto): se
    # guarda el original sin comprimir para no perder la foto.
    filename = f"{uuid.uuid4().hex}{ext}"
    storage.save_driver_photo(filename, raw_bytes)
    return filename


@bp.route("")
@permission_required("conductores", "view")
def list_view():
    """3 sep, pedido de Braulio: mismo criterio que en Flota (ver
    flota.list_view) — "eliminar" un conductor con viajes asociados no lo
    borra de verdad (queda INACTIVO para no romper el historial de esos
    viajes), pero la lista mostraba a todos sin importar su estado, dando la
    sensación de que "se eliminó y volvió a aparecer". Ahora la lista
    principal solo muestra conductores activos por defecto; los inactivos
    quedan aparte, en ?ver=inactivas."""
    show_inactive = request.args.get("ver") == "inactivas"
    if show_inactive:
        drivers = query_all("SELECT * FROM drivers WHERE status = 'INACTIVO' ORDER BY name")
    else:
        drivers = query_all("SELECT * FROM drivers WHERE status != 'INACTIVO' ORDER BY name")
    inactive_count = query_one("SELECT COUNT(*) n FROM drivers WHERE status = 'INACTIVO'")["n"]
    return render_template(
        "conductores/list.html",
        drivers=drivers,
        expiring_ids=_expiring_driver_ids(),
        show_inactive=show_inactive,
        inactive_count=inactive_count,
    )


@bp.route("/<int:driver_id>")
@permission_required("conductores", "view")
def driver_detail(driver_id):
    driver = query_one("SELECT * FROM drivers WHERE id = ?", (driver_id,))
    if driver is None:
        abort(404)
    return render_template("conductores/detail.html", driver=driver, driver_id=driver_id)


@bp.route("/<int:driver_id>/foto")
@permission_required("conductores", "view")
def driver_photo(driver_id):
    driver = query_one("SELECT photo_filename FROM drivers WHERE id = ?", (driver_id,))
    if driver is None or not driver["photo_filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.driver_photo_url(driver["photo_filename"]))
    return send_from_directory(storage.local_photos_dir(), driver["photo_filename"])


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("conductores", "edit")
def new_driver():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre del conductor es obligatorio.", "error")
            return render_template("conductores/driver_form.html", driver=request.form, mode="new")
        fields = _driver_fields_from_form(request.form)
        photo_filename = _save_driver_photo(request.files.get("photo"))
        execute(
            """INSERT INTO drivers (name, document_number, license_number, license_expiry,
               medical_exam_date, medical_exam_expiry,
               backus_driving_exam_date, backus_driving_exam_expiry,
               backus_training_date, backus_training_expiry,
               dds_date, dds_expiry, phone, photo_filename, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*fields[:-1], photo_filename, fields[-1]),
        )
        flash("Conductor registrado.", "success")
        return redirect(url_for("conductores.list_view"))
    return render_template("conductores/driver_form.html", driver=None, mode="new")


@bp.route("/<int:driver_id>/editar", methods=["GET", "POST"])
@permission_required("conductores", "edit")
def edit_driver(driver_id):
    driver = query_one("SELECT * FROM drivers WHERE id = ?", (driver_id,))
    if driver is None:
        abort(404)
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        fields = _driver_fields_from_form(request.form)
        # Subir una foto nueva reemplaza la anterior; si no se sube nada,
        # se conserva la que ya tenía (el campo del formulario es opcional
        # tanto al crear como al editar).
        new_photo = _save_driver_photo(request.files.get("photo"))
        photo_filename = new_photo if new_photo else driver["photo_filename"]
        execute(
            """UPDATE drivers SET name=?, document_number=?, license_number=?, license_expiry=?,
               medical_exam_date=?, medical_exam_expiry=?,
               backus_driving_exam_date=?, backus_driving_exam_expiry=?,
               backus_training_date=?, backus_training_expiry=?,
               dds_date=?, dds_expiry=?, phone=?, photo_filename=?, status=?
               WHERE id=?""",
            (*fields[:-1], photo_filename, fields[-1], driver_id),
        )
        flash("Conductor actualizado.", "success")
        return redirect(url_for("conductores.list_view"))
    return render_template("conductores/driver_form.html", driver=driver, mode="edit", driver_id=driver_id)


@bp.route("/<int:driver_id>/eliminar", methods=["POST"])
@permission_required("conductores", "edit")
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
    return redirect(url_for("conductores.list_view"))


# --- Importación masiva desde Excel (30 ago, pedido de Braulio) ---

@bp.route("/importar/plantilla")
@permission_required("conductores", "edit")
def import_template():
    buffer = build_import_template("Conductores", DRIVER_COLUMNS, DRIVER_EXAMPLE)
    return Response(
        buffer.getvalue(),
        mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_conductores.xlsx"'},
    )


def _driver_fields_from_row(row):
    return (
        row.get("name") or "",
        row.get("document_number") or "",
        row.get("license_number") or "",
        row.get("license_expiry"),
        row.get("medical_exam_date"),
        row.get("medical_exam_expiry"),
        row.get("backus_driving_exam_date"),
        row.get("backus_driving_exam_expiry"),
        row.get("backus_training_date"),
        row.get("backus_training_expiry"),
        row.get("dds_date"),
        row.get("dds_expiry"),
        row.get("phone") or "",
        row.get("status") or "ACTIVO",
    )


def _apply_driver_import(rows, example_skips):
    created, errors = 0, []
    skipped = [
        {"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."}
        for r in example_skips
    ]
    seen = set()
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})
        name = (row.get("name") or "").strip()
        if not name:
            errors.append({"row": n, "message": "Falta el nombre; la fila no se importó."})
            continue
        document_number = (row.get("document_number") or "").strip()
        dedup_key = document_number.lower() if document_number else f"name:{name.lower()}"
        if dedup_key in seen:
            skipped.append({"row": n, "message": f"{name} está repetido dentro del archivo; ya se había importado antes."})
            continue
        existing = None
        if document_number:
            existing = query_one("SELECT id FROM drivers WHERE document_number = ?", (document_number,))
        if existing is None:
            existing = query_one("SELECT id FROM drivers WHERE lower(name) = lower(?)", (name,))
        if existing:
            reason = f"DNI {document_number}" if document_number else "el nombre"
            skipped.append({"row": n, "message": f"Ya existe un conductor con {reason} igual ({name}); no se modificó."})
            continue
        seen.add(dedup_key)
        execute(
            """INSERT INTO drivers (name, document_number, license_number, license_expiry,
               medical_exam_date, medical_exam_expiry,
               backus_driving_exam_date, backus_driving_exam_expiry,
               backus_training_date, backus_training_expiry,
               dds_date, dds_expiry, phone, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _driver_fields_from_row(row),
        )
        created += 1
    return {"created": created, "updated": 0, "skipped": skipped, "errors": errors}


@bp.route("/importar", methods=["GET", "POST"])
@permission_required("conductores", "edit")
def import_drivers():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), DRIVER_COLUMNS, DRIVER_EXAMPLE)
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("conductores.import_drivers"))
        result = _apply_driver_import(rows, example_skips)
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("conductores.list_view"), retry_url=url_for("conductores.import_drivers"),
        )
    return render_template(
        "import_form.html", title="Importar conductores", module_label="los conductores",
        template_url=url_for("conductores.import_template"), upload_url=url_for("conductores.import_drivers"),
        back_url=url_for("conductores.list_view"), columns=DRIVER_COLUMNS,
    )
