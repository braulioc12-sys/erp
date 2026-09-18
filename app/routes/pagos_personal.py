"""Módulo "Pagos personal" (18 sep, pedido de Braulio):

    "creemos un modulo mas que se llame Pagos personal. En este modulo
    vamos a subir los comprobantes de pagos del personal tanto de planilla
    como recibo de honorario. Quiero que haya la opcion de que cree un
    excel con los formatos que usa la plataforma Telecredito de BCP."

Dos partes:

1. Catálogo de Personal (tabla `staff`) — nombre, documento, cargo, datos
   bancarios, y opcionalmente enlazado a un registro ya existente en
   Conductores (driver_id) para no duplicar a un chofer que también recibe
   pagos por acá (elegido por Braulio entre las opciones que se le dieron).

2. Pagos (tabla `staff_payments`) — un comprobante (boleta de planilla o
   recibo por honorarios) por persona y periodo, con su archivo adjunto.
   El tipo se elige por comprobante, no queda fijo por persona (pedido
   explícito de Braulio), y el export a Telecrédito siempre filtra por un
   solo tipo a la vez — planilla y honorarios nunca van en el mismo
   archivo.

IMPORTANTE sobre el export a Telecrédito: BCP tiene formatos DISTINTOS
según el servicio (pago de planilla/sueldos vs. pago a terceros/
honorarios), con columnas y códigos específicos de su plantilla real. El
export de acá (`export_telecredito`) genera un Excel con los datos
correctos (persona, documento, banco, cuenta/CCI, moneda, monto, concepto)
pero en un formato "borrador" — falta calzarlo exacto con la plantilla que
entrega BCP para cada servicio. Se marca así en el propio archivo hasta que
Braulio la mande."""
import os
import uuid
from datetime import datetime

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for

from app import storage
from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, today_str

bp = Blueprint("pagos_personal", __name__, url_prefix="/pagos-personal")

PAYMENT_TYPE_LABELS = {
    "PLANILLA": "Planilla",
    "RECIBO_HONORARIOS": "Recibo por honorarios",
}
DOCUMENT_TYPE_CHOICES = ["DNI", "CE", "RUC"]
CURRENCY_LABELS = {"S": "Soles", "D": "Dólares"}

ALLOWED_RECEIPT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".webp"}
RECEIPT_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


def _save_receipt_file(file_storage):
    """Guarda el comprobante adjunto (foto o PDF) de un pago de personal —
    mismo criterio de validación que _save_vehicle_document_file() en
    app/routes/flota.py, guardando con storage.save_staff_payment_receipt().
    Devuelve el nombre de archivo guardado, o None si no se subió nada
    válido."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        ext = RECEIPT_MIME_TO_EXTENSION.get((file_storage.mimetype or "").lower())
    if not ext:
        return None
    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None
    filename = f"{uuid.uuid4().hex}{ext}"
    storage.save_staff_payment_receipt(filename, raw_bytes)
    return filename


def get_active_staff(only_active=True):
    sql = "SELECT * FROM staff WHERE 1=1"
    if only_active:
        sql += " AND status = 'ACTIVO'"
    sql += " ORDER BY name"
    return query_all(sql)


# --- Pagos ---

def _filters_from_args(args):
    period = args.get("period") or today_str()[:7]
    staff_id = args.get("staff_id", type=int)
    payment_type = args.get("payment_type", "")
    status = args.get("status", "")
    q = args.get("q", "").strip()
    return period, staff_id, payment_type, status, q


def _filtered_payments(period, staff_id, payment_type, status, q):
    sql = """SELECT p.*, s.name as staff_name, s.document_type, s.document_number,
                    s.bank_name, s.account_number, s.cci, s.currency as staff_currency
             FROM staff_payments p JOIN staff s ON s.id = p.staff_id
             WHERE p.period = ?"""
    params = [period]
    if staff_id:
        sql += " AND p.staff_id = ?"
        params.append(staff_id)
    if payment_type in PAYMENT_TYPE_LABELS:
        sql += " AND p.payment_type = ?"
        params.append(payment_type)
    if status in ("PENDIENTE", "PAGADO"):
        sql += " AND p.status = ?"
        params.append(status)
    if q:
        sql += " AND s.name LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY s.name, p.id"
    return query_all(sql, params)


@bp.route("")
@permission_required("pagos_personal", "view")
def list_view():
    period, staff_id, payment_type, status, q = _filters_from_args(request.args)
    payments = _filtered_payments(period, staff_id, payment_type, status, q)
    total_amount = sum(p["amount"] or 0 for p in payments)
    pending_amount = sum(p["amount"] or 0 for p in payments if p["status"] == "PENDIENTE")
    return render_template(
        "pagos_personal/list.html",
        payments=payments, period=period, staff_id=staff_id, payment_type=payment_type,
        status=status, q=q, total_amount=total_amount, pending_amount=pending_amount,
        all_staff=get_active_staff(only_active=False),
        payment_type_labels=PAYMENT_TYPE_LABELS,
    )


def _payment_form_context(payment=None):
    return {
        "payment": payment,
        "all_staff": get_active_staff(),
        "payment_type_labels": PAYMENT_TYPE_LABELS,
        "today": today_str(),
        "default_period": (payment["period"] if payment else today_str()[:7]),
    }


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def new_payment():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        staff_id = request.form.get("staff_id", type=int)
        payment_type = request.form.get("payment_type", "")
        period = (request.form.get("period") or "").strip()
        errors = []
        if not staff_id or query_one("SELECT id FROM staff WHERE id = ?", (staff_id,)) is None:
            errors.append("Elige una persona del catálogo de Personal.")
        if payment_type not in PAYMENT_TYPE_LABELS:
            errors.append("Elige el tipo de comprobante (Planilla o Recibo por honorarios).")
        if not period:
            errors.append("Elige el periodo (mes) del pago.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("pagos_personal/form.html", **_payment_form_context())

        receipt_filename = _save_receipt_file(request.files.get("receipt"))
        status = "PAGADO" if request.form.get("mark_paid") else "PENDIENTE"
        payment_date = parse_date(request.form.get("payment_date")) if status == "PAGADO" else None
        if status == "PAGADO" and not payment_date:
            payment_date = today_str()

        execute(
            """INSERT INTO staff_payments
               (staff_id, payment_type, period, amount, concept, status, payment_date, receipt_filename)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                staff_id, payment_type, period, parse_float(request.form.get("amount"), 0),
                request.form.get("concept", "").strip() or None, status, payment_date, receipt_filename,
            ),
        )
        flash("Pago registrado.", "success")
        return redirect(url_for("pagos_personal.list_view", period=period))

    return render_template("pagos_personal/form.html", **_payment_form_context())


@bp.route("/<int:payment_id>/editar", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def edit_payment(payment_id):
    payment = query_one("SELECT * FROM staff_payments WHERE id = ?", (payment_id,))
    if payment is None:
        abort(404)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        staff_id = request.form.get("staff_id", type=int)
        payment_type = request.form.get("payment_type", "")
        period = (request.form.get("period") or "").strip()
        errors = []
        if not staff_id or query_one("SELECT id FROM staff WHERE id = ?", (staff_id,)) is None:
            errors.append("Elige una persona del catálogo de Personal.")
        if payment_type not in PAYMENT_TYPE_LABELS:
            errors.append("Elige el tipo de comprobante (Planilla o Recibo por honorarios).")
        if not period:
            errors.append("Elige el periodo (mes) del pago.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("pagos_personal/form.html", **_payment_form_context(payment))

        new_receipt = _save_receipt_file(request.files.get("receipt"))
        receipt_filename = new_receipt or payment["receipt_filename"]
        status = "PAGADO" if request.form.get("mark_paid") else "PENDIENTE"
        payment_date = parse_date(request.form.get("payment_date")) if status == "PAGADO" else None
        if status == "PAGADO" and not payment_date:
            payment_date = payment["payment_date"] or today_str()

        execute(
            """UPDATE staff_payments SET staff_id = ?, payment_type = ?, period = ?, amount = ?,
               concept = ?, status = ?, payment_date = ?, receipt_filename = ? WHERE id = ?""",
            (
                staff_id, payment_type, period, parse_float(request.form.get("amount"), 0),
                request.form.get("concept", "").strip() or None, status, payment_date, receipt_filename,
                payment_id,
            ),
        )
        flash("Pago actualizado.", "success")
        return redirect(url_for("pagos_personal.list_view", period=period))

    return render_template("pagos_personal/form.html", **_payment_form_context(payment))


@bp.route("/<int:payment_id>/eliminar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def delete_payment(payment_id):
    if not validate_csrf():
        abort(400)
    payment = query_one("SELECT period FROM staff_payments WHERE id = ?", (payment_id,))
    if payment is None:
        abort(404)
    execute("DELETE FROM staff_payments WHERE id = ?", (payment_id,))
    flash("Pago eliminado.", "success")
    return redirect(url_for("pagos_personal.list_view", period=payment["period"]))


@bp.route("/<int:payment_id>/marcar-pagado", methods=["POST"])
@permission_required("pagos_personal", "edit")
def mark_paid(payment_id):
    """Acción rápida desde el listado, sin tener que abrir el formulario
    completo — mismo criterio que otros "toggle" del sistema (ej.
    mechanics_toggle en Mantenimiento)."""
    if not validate_csrf():
        abort(400)
    payment = query_one("SELECT * FROM staff_payments WHERE id = ?", (payment_id,))
    if payment is None:
        abort(404)
    if payment["status"] == "PAGADO":
        execute("UPDATE staff_payments SET status = 'PENDIENTE', payment_date = NULL WHERE id = ?", (payment_id,))
        flash("Pago marcado como pendiente otra vez.", "success")
    else:
        execute(
            "UPDATE staff_payments SET status = 'PAGADO', payment_date = ? WHERE id = ?",
            (today_str(), payment_id),
        )
        flash("Pago marcado como pagado.", "success")
    return redirect(url_for("pagos_personal.list_view", period=payment["period"]))


@bp.route("/<int:payment_id>/comprobante")
@permission_required("pagos_personal", "view")
def payment_receipt_file(payment_id):
    payment = query_one("SELECT receipt_filename FROM staff_payments WHERE id = ?", (payment_id,))
    if payment is None or not payment["receipt_filename"]:
        abort(404)
    filename = payment["receipt_filename"]
    if storage.using_s3():
        return redirect(storage.staff_payment_receipt_url(filename))
    return send_from_directory(storage.local_staff_payment_receipts_dir(), filename)


@bp.route("/exportar")
@permission_required("pagos_personal", "view")
def export_excel():
    from app.reports import build_staff_payments_workbook

    period, staff_id, payment_type, status, q = _filters_from_args(request.args)
    payments = _filtered_payments(period, staff_id, payment_type, status, q)

    buffer = build_staff_payments_workbook(
        payments, company_name=current_app.config["COMPANY_NAME"], period=period,
        payment_type_labels=PAYMENT_TYPE_LABELS,
    )
    filename = f"pagos_personal_{period}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/exportar/telecredito")
@permission_required("pagos_personal", "edit")
def export_telecredito():
    """Genera el Excel para cargar en Telecrédito BCP — SIEMPRE de un solo
    payment_type a la vez (planilla y honorarios nunca van en el mismo
    archivo, pedido explícito de Braulio). Ver la nota grande al inicio de
    este archivo: el formato de acá es un borrador con los datos correctos
    mientras se confirma la plantilla exacta que da el banco."""
    from app.reports import build_telecredito_workbook

    period, staff_id, payment_type, status, q = _filters_from_args(request.args)
    if payment_type not in PAYMENT_TYPE_LABELS:
        flash("Para generar el archivo de Telecrédito, primero filtra por un solo tipo: Planilla o Recibo por honorarios.", "error")
        return redirect(url_for("pagos_personal.list_view", period=period, staff_id=staff_id, status=status, q=q))

    # Por defecto, solo los pendientes (los que todavía no se pagaron) —
    # si se quiere incluir los ya pagados también, se puede filtrar
    # status=PAGADO o dejarlo vacío desde la pantalla.
    effective_status = status or "PENDIENTE"
    payments = _filtered_payments(period, staff_id, payment_type, effective_status, q)

    missing_bank_data = [p for p in payments if not p["cci"]]
    if missing_bank_data:
        names = ", ".join(p["staff_name"] for p in missing_bank_data)
        flash(
            f"Estas personas no tienen CCI registrado en el catálogo de Personal, así que no se pudieron incluir: {names}.",
            "error",
        )
        payments = [p for p in payments if p["cci"]]

    if not payments:
        flash("No hay pagos con esos filtros para generar el archivo.", "error")
        return redirect(url_for("pagos_personal.list_view", period=period, staff_id=staff_id, payment_type=payment_type, status=status, q=q))

    buffer = build_telecredito_workbook(payments, payment_type=payment_type, payment_type_labels=PAYMENT_TYPE_LABELS)

    now = datetime.now().strftime("%Y%m%d%H%M")
    execute(
        f"UPDATE staff_payments SET exported_at = ? WHERE id IN ({','.join('?' * len(payments))})",
        [today_str()] + [p["id"] for p in payments],
    )

    tipo_slug = "planilla" if payment_type == "PLANILLA" else "honorarios"
    filename = f"telecredito_{tipo_slug}_{period}_{now}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Catálogo de Personal ---

@bp.route("/personal")
@permission_required("pagos_personal", "view")
def staff_list():
    show_inactive = request.args.get("ver") == "inactivos"
    q = request.args.get("q", "").strip()
    sql = "SELECT s.*, d.name as driver_name FROM staff s LEFT JOIN drivers d ON d.id = s.driver_id WHERE 1=1"
    params = []
    sql += " AND s.status = 'INACTIVO'" if show_inactive else " AND s.status = 'ACTIVO'"
    if q:
        sql += " AND (s.name LIKE ? OR s.document_number LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY s.name"
    staff = query_all(sql, params)
    return render_template("pagos_personal/staff_list.html", staff=staff, show_inactive=show_inactive, q=q)


def _staff_form_context(staff=None):
    return {
        "staff": staff,
        "drivers": query_all("SELECT id, name FROM drivers WHERE status = 'ACTIVO' ORDER BY name"),
        "document_type_choices": DOCUMENT_TYPE_CHOICES,
        "currency_labels": CURRENCY_LABELS,
    }


@bp.route("/personal/nuevo", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def staff_new():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre es obligatorio.", "error")
            return render_template("pagos_personal/staff_form.html", **_staff_form_context())
        driver_id = request.form.get("driver_id", type=int) or None
        execute(
            """INSERT INTO staff (name, document_type, document_number, position, driver_id,
               bank_name, account_number, cci, currency, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, request.form.get("document_type") or "DNI", request.form.get("document_number", "").strip() or None,
                request.form.get("position", "").strip() or None, driver_id,
                request.form.get("bank_name", "").strip() or None, request.form.get("account_number", "").strip() or None,
                request.form.get("cci", "").strip() or None, request.form.get("currency") or "S",
                request.form.get("notes", "").strip() or None,
            ),
        )
        flash("Persona agregada al catálogo.", "success")
        return redirect(url_for("pagos_personal.staff_list"))
    return render_template("pagos_personal/staff_form.html", **_staff_form_context())


@bp.route("/personal/<int:staff_id>/editar", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def staff_edit(staff_id):
    staff = query_one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if staff is None:
        abort(404)
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre es obligatorio.", "error")
            return render_template("pagos_personal/staff_form.html", **_staff_form_context(staff))
        driver_id = request.form.get("driver_id", type=int) or None
        execute(
            """UPDATE staff SET name = ?, document_type = ?, document_number = ?, position = ?, driver_id = ?,
               bank_name = ?, account_number = ?, cci = ?, currency = ?, notes = ? WHERE id = ?""",
            (
                name, request.form.get("document_type") or "DNI", request.form.get("document_number", "").strip() or None,
                request.form.get("position", "").strip() or None, driver_id,
                request.form.get("bank_name", "").strip() or None, request.form.get("account_number", "").strip() or None,
                request.form.get("cci", "").strip() or None, request.form.get("currency") or "S",
                request.form.get("notes", "").strip() or None, staff_id,
            ),
        )
        flash("Datos actualizados.", "success")
        return redirect(url_for("pagos_personal.staff_list"))
    return render_template("pagos_personal/staff_form.html", **_staff_form_context(staff))


@bp.route("/personal/<int:staff_id>/alternar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def staff_toggle(staff_id):
    if not validate_csrf():
        abort(400)
    staff = query_one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if staff is None:
        abort(404)
    new_status = "INACTIVO" if staff["status"] == "ACTIVO" else "ACTIVO"
    execute("UPDATE staff SET status = ? WHERE id = ?", (new_status, staff_id))
    flash("Actualizado." if new_status == "INACTIVO" else "Reactivado.", "success")
    return redirect(url_for("pagos_personal.staff_list"))
