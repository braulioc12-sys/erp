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

Sobre el export a Telecrédito (18 sep, 2da y 3ra ronda): BCP arma el
archivo de texto de ancho fijo de la Planilla de Haberes, con checksum
obligatorio, y ese MISMO formato sirve tanto para Planilla como para
Recibo por honorarios — lo único que cambia entre los dos es el "Subtipo
de planilla" de la cabecera (corrección de Braulio tras un primer intento
fallido con un formato "Proveedores" distinto — ver la nota grande al
inicio de app/telecredito.py). `telecredito_configure()` pide los datos de
la cabecera (cuenta de cargo, fecha, referencia) y `telecredito_generate()`
arma el .txt exacto con `app/telecredito.py` — verificado byte a byte
contra un archivo real de Planilla que mandó Braulio antes de darlo por
bueno (ver la nota grande al inicio de ese módulo).

3. Constancias de pago (tabla `payment_vouchers`, 18 sep 4ta ronda —
   "quiero que el menu de Pagos personal este agrupado por año y luego
   mes, y una vez que se entra a cada mes pueda ver pdfs de constancias de
   pago antiguas, asi mismo para los meses de ahora en adelante quiero
   poder subir las constancias que me brindara cada banco"): un archivador
   por año -> mes de los comprobantes que da cada BANCO de que un lote de
   Telecrédito se procesó (distinto de receipt_filename en staff_payments,
   que es la boleta/recibo de cada persona). `constancias_years()` /
   `constancias_months()` / `constancias_month_detail()` arman la
   navegación; `constancias_upload()` sube uno o más archivos sueltos a un
   mes puntual; `constancias_import_zip()` es la carga inicial masiva —
   acepta el mismo .zip con carpetas "AÑO/MES AÑO/archivo" que ya venía
   usando Braulio para guardar esto a mano, y clasifica cada archivo por
   año/mes con app/payment_vouchers_import.py (ver ese módulo para el
   detalle de las reglas y su verificación contra un zip real de 314
   archivos).

4. Plantilla de honorarios (tabla `honorarios_template_items`, 18 sep,
   6ta ronda — "tengo un excel con nombres y numeros de cuenta que quiero
   que sea la plantilla default con montos que se paga cada mes de
   recibos por honorario, quiero que cada mes se use esta por default y
   se editen los montos, agreguen o borren personas. Luego seleccione de
   esta plantilla a quienes les voy a pagar y se cree el archivo masivo
   de telecredito, los que ya se crearon que se marquen como pagados."):
   lista reusable de personas de RECIBO_HONORARIOS con su monto por
   defecto — `honorarios_plantilla()` la administra (agregar/editar
   monto/activar-desactivar/quitar) y también es, en la misma pantalla,
   la pantalla mensual (7ma ronda, mismo día — pedido de Braulio de
   agregar ahí mismo filtro por concepto, buscador por nombre, columna de
   N° de comprobante, selección + generar archivo de Telecrédito y marca
   de pagado sin salir de la plantilla): ofrece la plantilla activa,
   filtrable por nombre/concepto, con casillas + montos + N° de
   comprobante editables para ESE mes (sin tocar el default guardado) —
   `_honorarios_month_rows()` arma esas filas y las reusa tanto para
   mostrarlas (GET) como para saber, en `honorarios_plantilla_guardar_mes()`
   (POST), exactamente qué filas están dentro del filtro actual, así un
   filtro puesto nunca borra ni toca el pago de alguien que quedó fuera
   de la vista por el filtro. Ese mismo POST, según el botón que se use,
   guarda nomás (crea/actualiza/quita `staff_payments` según lo marcado)
   o de una vez redirige a `telecredito_configure()` ya con la lista
   exacta de pagos elegidos (`payment_ids`) para generar y descargar el
   archivo sin salir del flujo — al volver a la Plantilla (mismo periodo),
   lo que se acaba de exportar aparece marcado como "Pagado" y bloqueado
   para editar. `honorarios_plantilla_import()` carga la plantilla en
   bloque desde un Excel (motor genérico de app/bulk_import.py,
   HONORARIOS_TEMPLATE_COLUMNS) creando o actualizando personas en el
   Catálogo de Personal según haga falta. Ese mismo pedido original
   también hizo que `telecredito_generate()` marque como PAGADO los
   pagos que incluye en el archivo, en vez de dejarlos pendientes — y
   ahora acepta opcionalmente `payment_ids` para limitarse a una
   selección puntual en vez de "todo lo pendiente del periodo/filtros".

   Además, `staff_import_txt()` (sección "Catálogo de Personal" más abajo)
   lee uno o más .txt de Telecrédito YA GENERADOS (de este sistema o del
   propio banco) y completa el Catálogo de Personal con los datos
   bancarios de cada persona que encuentra — ver app/telecredito_txt_import.py
   (lee exactamente las mismas posiciones que escribe app/telecredito.py,
   verificado contra un archivo real)."""
import io
import os
import uuid
import zipfile
from datetime import datetime

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, send_from_directory, url_for

from app import storage
from app.auth import permission_required, validate_csrf
from app.bulk_import import (
    HONORARIOS_TEMPLATE_COLUMNS,
    HONORARIOS_TEMPLATE_EXAMPLE,
    XLSX_MIME,
    build_import_template,
    read_import_rows,
)
from app.db import execute, query_all, query_one
from app.helpers import now_str, parse_date, parse_float, today_str
from app.payment_vouchers_import import MONTH_LABELS, classify_zip_entry
from app.telecredito_txt_import import parse_txt_file

bp = Blueprint("pagos_personal", __name__, url_prefix="/pagos-personal")

PAYMENT_TYPE_LABELS = {
    "PLANILLA": "Planilla",
    "RECIBO_HONORARIOS": "Recibo por honorarios",
}
DOCUMENT_TYPE_CHOICES = ["DNI", "CE", "RUC"]
CURRENCY_LABELS = {"S": "Soles", "D": "Dólares"}
ACCOUNT_TYPE_LABELS = {"AHORROS": "Ahorros", "CORRIENTE": "Corriente", "MAESTRA": "Maestra"}

ALLOWED_RECEIPT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".webp"}
RECEIPT_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

# Las constancias que da cada banco no son solo PDFs — el archivo real que
# mandó Braulio para la carga inicial trae también .txt (el propio archivo
# de Telecrédito ya generado), .xlsx (reportes de pendientes/interbancarios)
# y .docx — por eso esta lista es más amplia que ALLOWED_RECEIPT_EXTENSIONS.
ALLOWED_VOUCHER_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp",
    ".txt", ".xlsx", ".xls", ".docx", ".doc", ".csv",
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


def _filtered_payments(period, staff_id, payment_type, status, q, payment_ids=None):
    """payment_ids (18 sep, 7ma ronda — flujo de "seleccionar de la
    Plantilla de honorarios y generar el archivo") acota el resultado a
    esa lista puntual de IDs de staff_payments en vez de "todo lo que
    calce con los filtros" — usado cuando el archivo de Telecrédito se
    genera desde una selección puntual en vez de desde la lista general."""
    sql = """SELECT p.*, s.name as staff_name, s.document_type, s.document_number,
                    s.bank_name, s.account_number, s.account_type, s.cci,
                    s.currency as staff_currency, s.company as staff_company
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
    if payment_ids:
        sql += f" AND p.id IN ({','.join('?' * len(payment_ids))})"
        params.extend(payment_ids)
    sql += " ORDER BY s.name, p.id"
    return query_all(sql, params)


@bp.route("")
@permission_required("pagos_personal", "view")
def hub():
    """19 sep (reorganización del menú de Pagos personal, pedido de
    Braulio: "Primero debe ser una pantalla para seleccionar si se quiere
    hacer pago de planilla, recibos por honorarios o consultar
    constancias"): pantalla de entrada con las 3 opciones — Planilla
    (`planilla_placeholder()`, todavía "en construcción"), Recibos por
    honorarios (`honorarios_plantilla()`, ver su docstring) y Constancias
    (`constancias_years()`). Reemplaza a la antigua lista general
    (mezclaba Planilla y Honorarios en una sola tabla, con filtros de
    tipo/estado/periodo) que vivía en esta misma URL."""
    return render_template("pagos_personal/hub.html")


@bp.route("/planilla")
@permission_required("pagos_personal", "view")
def planilla_placeholder():
    """"Cuando se ingrese a planilla por mientras dejarlo que diga en
    construccion, luego haremos este modulo" (pedido de Braulio, 19 sep).
    Los datos y las rutas de Planilla (new_payment/edit_payment/etc, más
    abajo) siguen intactos para cuando se retome este módulo — nomás no
    hay ningún link hacia ellas desde acá todavía."""
    return render_template("pagos_personal/planilla_placeholder.html")


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
        return redirect(url_for("pagos_personal.planilla_placeholder"))

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
        return redirect(url_for("pagos_personal.planilla_placeholder"))

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
    return redirect(url_for("pagos_personal.planilla_placeholder"))


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
    return redirect(url_for("pagos_personal.planilla_placeholder"))


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


@bp.route("/exportar/telecredito/configurar")
@permission_required("pagos_personal", "edit")
def telecredito_configure():
    """Paso previo a generar el archivo de Telecrédito: se eligen los
    datos que van en la cabecera (cuenta de cargo, fecha de proceso,
    referencia, y para Planilla el subtipo) antes de armar el .txt — ver
    telecredito_generate() y app/telecredito.py. 18 sep, 2da ronda: antes
    de esto el botón generaba directo un Excel "borrador"; ahora que se
    tiene la ficha real de BCP y un archivo de ejemplo de Braulio, se
    genera el .txt exacto que pide el banco."""
    from app.telecredito import DEFAULT_SUBTIPO_HONORARIOS, DEFAULT_SUBTIPO_PLANILLA, SUBTIPO_PLANILLA_CHOICES

    period, staff_id, payment_type, status, q = _filters_from_args(request.args)
    # payment_ids/origin (18 sep, 7ma ronda): cuando se llega acá desde la
    # Plantilla de honorarios con una selección puntual de personas, en vez
    # de "todo lo pendiente del periodo/filtros" — ver honorarios_plantilla_guardar_mes().
    payment_ids = request.args.getlist("payment_ids", type=int)
    origin = request.args.get("origin", "")
    plantilla_q = request.args.get("plantilla_q", "")
    plantilla_concept = request.args.get("plantilla_concept", "")
    plantilla_status = request.args.get("plantilla_status", "")

    def _back_to_origin():
        if origin == "honorarios_plantilla":
            return redirect(url_for(
                "pagos_personal.honorarios_plantilla", period=period, q=plantilla_q,
                concept=plantilla_concept, status=plantilla_status,
            ))
        return redirect(url_for("pagos_personal.hub"))

    if payment_type not in PAYMENT_TYPE_LABELS:
        flash("Para generar el archivo de Telecrédito, primero filtra por un solo tipo: Planilla o Recibo por honorarios.", "error")
        return _back_to_origin()

    # Por defecto, solo los pendientes (los que todavía no se pagaron) —
    # si se quiere incluir los ya pagados también, se puede filtrar
    # status=PAGADO o dejarlo vacío desde la pantalla.
    effective_status = status or "PENDIENTE"
    payments = _filtered_payments(period, staff_id, payment_type, effective_status, q, payment_ids=payment_ids or None)
    if not payments:
        flash("No hay pagos con esos filtros para generar el archivo.", "error")
        return _back_to_origin()

    accounts = query_all("SELECT * FROM company_bank_accounts WHERE active = 1 ORDER BY company_name, sort_order")
    if not accounts:
        flash("Todavía no hay ninguna cuenta de cargo registrada — agrégala primero en Catálogos > Bancos.", "error")
        return redirect(url_for("catalogos.bancos_list"))

    back_url = (
        url_for(
            "pagos_personal.honorarios_plantilla", period=period, q=plantilla_q,
            concept=plantilla_concept, status=plantilla_status,
        )
        if origin == "honorarios_plantilla"
        else url_for("pagos_personal.hub")
    )

    return render_template(
        "pagos_personal/telecredito_configure.html",
        payments=payments, total_amount=sum(p["amount"] or 0 for p in payments), count=len(payments),
        period=period, staff_id=staff_id, payment_type=payment_type, status=status, q=q,
        payment_type_labels=PAYMENT_TYPE_LABELS, accounts=accounts,
        subtipo_choices=SUBTIPO_PLANILLA_CHOICES, default_subtipo=DEFAULT_SUBTIPO_PLANILLA,
        subtipo_honorarios=DEFAULT_SUBTIPO_HONORARIOS,
        default_reference=f"{PAYMENT_TYPE_LABELS[payment_type].upper()} {period}", today=today_str(),
        payment_ids=payment_ids, origin=origin, plantilla_q=plantilla_q, plantilla_concept=plantilla_concept,
        plantilla_status=plantilla_status, back_url=back_url,
    )


@bp.route("/exportar/telecredito/generar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def telecredito_generate():
    """Arma y descarga el .txt de Telecrédito con los datos elegidos en
    telecredito_configure(). Excluye (con aviso, sin bloquear el resto)
    cualquier pago cuya persona no tenga cuenta/CCI, cuya moneda no
    coincida con la cuenta de cargo elegida (el banco exige que todo el
    archivo sea de una sola moneda), o cuyo tipo de documento no sea válido
    para este servicio (Planilla no admite RUC).

    18 sep, 6ta ronda: por un tiempo esto marcaba de una vez como PAGADO
    los pagos incluidos al generar el archivo. 19 sep (reorganización del
    menú, pedido de Braulio: "una vez que se paguen enlazar la constancia
    de pago de manera manual para que figuren como pagados") — se
    revirtió: generar el archivo ya NO marca como pagado por sí solo,
    solo dejar constancia (con `exported_at`) de que se generó. El pago
    recién pasa a PAGADO cuando se enlaza a mano la constancia real que
    emite el banco — ver honorarios_plantilla_guardar_mes() (action
    "enlazar") y honorarios_plantilla_desenlazar_constancia()."""
    from app.telecredito import (
        DEFAULT_SUBTIPO_HONORARIOS,
        DEFAULT_SUBTIPO_PLANILLA,
        DOCUMENT_TYPE_CODES_HABERES,
        abono_bank_fields,
        build_haberes_txt,
    )

    if not validate_csrf():
        abort(400)

    period, staff_id, payment_type, status, q = _filters_from_args(request.form)
    payment_ids = request.form.getlist("payment_ids", type=int)
    origin = request.form.get("origin", "")
    plantilla_q = request.form.get("plantilla_q", "")
    plantilla_concept = request.form.get("plantilla_concept", "")
    plantilla_status = request.form.get("plantilla_status", "")
    if payment_type not in PAYMENT_TYPE_LABELS:
        abort(400)
    redirect_to_configure = lambda: redirect(url_for(
        "pagos_personal.telecredito_configure", period=period, staff_id=staff_id,
        payment_type=payment_type, status=status, q=q,
        payment_ids=payment_ids, origin=origin, plantilla_q=plantilla_q, plantilla_concept=plantilla_concept,
        plantilla_status=plantilla_status,
    ))

    effective_status = status or "PENDIENTE"
    payments = _filtered_payments(period, staff_id, payment_type, effective_status, q, payment_ids=payment_ids or None)
    if not payments:
        flash("No hay pagos con esos filtros para generar el archivo.", "error")
        return redirect_to_configure()

    account_id = request.form.get("cuenta_cargo_id", type=int)
    cuenta_cargo = query_one("SELECT * FROM company_bank_accounts WHERE id = ? AND active = 1", (account_id,))
    if cuenta_cargo is None:
        flash("Elige una cuenta de cargo válida.", "error")
        return redirect_to_configure()

    fecha_proceso = request.form.get("fecha_proceso") or today_str()
    if fecha_proceso < today_str():
        flash("La fecha de proceso no puede ser anterior a hoy — lo exige el banco.", "error")
        return redirect_to_configure()
    referencia_planilla = (
        request.form.get("referencia_planilla", "").strip()
        or f"{PAYMENT_TYPE_LABELS[payment_type].upper()} {period}"
    )

    # Mismo formato de archivo (Planilla de Haberes) para los dos tipos de
    # pago — ver app/telecredito.py — así que el documento del beneficiario
    # siempre queda limitado a DNI/CE, sin RUC, en ambos casos.
    valid_doc_codes = DOCUMENT_TYPE_CODES_HABERES

    excluded_bank, excluded_currency, excluded_doc = [], [], []
    prepared = []
    for row in payments:
        p = dict(row)
        if p["staff_currency"] != cuenta_cargo["currency"]:
            excluded_currency.append(p["staff_name"])
            continue
        if p["document_type"] not in valid_doc_codes:
            excluded_doc.append(p["staff_name"])
            continue
        bank_type, bank_value, is_interbank = abono_bank_fields(p["cci"], p["account_number"], p["account_type"])
        if not bank_value:
            excluded_bank.append(p["staff_name"])
            continue
        p["bank_type"], p["bank_value"], p["bank_is_interbank"] = bank_type, bank_value, is_interbank
        prepared.append(p)

    if excluded_bank:
        flash(f"Sin cuenta ni CCI registrado en el catálogo de Personal, no se pudieron incluir: {', '.join(excluded_bank)}.", "error")
    if excluded_currency:
        moneda = CURRENCY_LABELS.get(cuenta_cargo["currency"], cuenta_cargo["currency"])
        flash(f"Su moneda no coincide con la cuenta de cargo elegida ({moneda}), no se pudieron incluir: {', '.join(excluded_currency)}.", "error")
    if excluded_doc:
        flash(f"Su tipo de documento no es válido para este servicio de Telecrédito, no se pudieron incluir: {', '.join(excluded_doc)}.", "error")

    if not prepared:
        flash("No quedó ningún pago para incluir en el archivo — revisa los avisos de arriba.", "error")
        return redirect_to_configure()

    default_company_name = cuenta_cargo["company_name"]
    if payment_type == "PLANILLA":
        subtipo = request.form.get("subtipo_planilla") or DEFAULT_SUBTIPO_PLANILLA
        default_concept = "PAGO DE HABERES"
        tipo_slug = "haberes"
    else:
        # Recibo por honorarios: mismo archivo que Planilla, con el
        # subtipo fijo en "4" (Cuarta categoría) — corrección de Braulio,
        # ver app/telecredito.py.
        subtipo = DEFAULT_SUBTIPO_HONORARIOS
        default_concept = "PAGO HONORARIOS"
        tipo_slug = "honorarios"

    content = build_haberes_txt(
        prepared, cuenta_cargo=cuenta_cargo, fecha_proceso=fecha_proceso,
        subtipo_planilla=subtipo, referencia_planilla=referencia_planilla,
        default_company_name=default_company_name, default_concept=default_concept,
    )

    now = datetime.now().strftime("%Y%m%d%H%M")
    execute(
        f"""UPDATE staff_payments SET exported_at = ? WHERE id IN ({','.join('?' * len(prepared))})""",
        [today_str()] + [p["id"] for p in prepared],
    )

    filename = f"telecredito_{tipo_slug}_{period}_{now}.txt"
    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Catálogo de Personal ---

_STAFF_BANK_FIELDS = ("document_type", "document_number", "bank_name", "account_number", "account_type", "cci", "currency")


def _match_staff(name, document_number=None):
    """Empareja con una persona ya existente en el Catálogo de Personal —
    por documento si se dio uno (más confiable), si no por nombre exacto
    sin distinguir mayúsculas. Usado por los 2 importadores masivos
    (Excel de la plantilla de honorarios y .txt de Telecrédito) para no
    duplicar a alguien que ya está cargado."""
    doc = (document_number or "").strip()
    if doc:
        row = query_one("SELECT * FROM staff WHERE document_number = ?", (doc,))
        if row:
            return row
    return query_one("SELECT * FROM staff WHERE LOWER(name) = LOWER(?)", (name.strip(),))


def _upsert_staff_bank_data(name, **fields):
    """Crea a la persona en Personal si no existe (emparejando por
    document_number o nombre — ver _match_staff), o actualiza sus datos
    bancarios si ya existe. Solo pisa un campo si `fields` trae un valor
    no vacío para él — así una fila incompleta del Excel/.txt nunca borra
    un dato bueno que la persona ya tenía cargado. `fields` acepta
    cualquiera de _STAFF_BANK_FIELDS. Devuelve (staff_id, created: bool)."""
    clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items() if k in _STAFF_BANK_FIELDS}
    clean = {k: v for k, v in clean.items() if v}
    existing = _match_staff(name, clean.get("document_number"))
    if existing:
        if clean:
            sets = ", ".join(f"{k} = ?" for k in clean)
            execute(f"UPDATE staff SET {sets} WHERE id = ?", list(clean.values()) + [existing["id"]])
        return existing["id"], False
    new_id = execute(
        """INSERT INTO staff (name, document_type, document_number, bank_name, account_number,
           account_type, cci, currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name.strip(), clean.get("document_type") or "DNI", clean.get("document_number"),
            clean.get("bank_name"), clean.get("account_number"), clean.get("account_type") or "AHORROS",
            clean.get("cci"), clean.get("currency") or "S",
        ),
    )
    return new_id, True


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
        "account_type_labels": ACCOUNT_TYPE_LABELS,
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
        account_type = request.form.get("account_type") or "AHORROS"
        if account_type not in ACCOUNT_TYPE_LABELS:
            account_type = "AHORROS"
        execute(
            """INSERT INTO staff (name, document_type, document_number, position, company, driver_id,
               bank_name, account_number, account_type, cci, currency, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, request.form.get("document_type") or "DNI", request.form.get("document_number", "").strip() or None,
                request.form.get("position", "").strip() or None, request.form.get("company", "").strip() or None, driver_id,
                request.form.get("bank_name", "").strip() or None, request.form.get("account_number", "").strip() or None,
                account_type, request.form.get("cci", "").strip() or None, request.form.get("currency") or "S",
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
        account_type = request.form.get("account_type") or "AHORROS"
        if account_type not in ACCOUNT_TYPE_LABELS:
            account_type = "AHORROS"
        execute(
            """UPDATE staff SET name = ?, document_type = ?, document_number = ?, position = ?, company = ?, driver_id = ?,
               bank_name = ?, account_number = ?, account_type = ?, cci = ?, currency = ?, notes = ? WHERE id = ?""",
            (
                name, request.form.get("document_type") or "DNI", request.form.get("document_number", "").strip() or None,
                request.form.get("position", "").strip() or None, request.form.get("company", "").strip() or None, driver_id,
                request.form.get("bank_name", "").strip() or None, request.form.get("account_number", "").strip() or None,
                account_type, request.form.get("cci", "").strip() or None, request.form.get("currency") or "S",
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


@bp.route("/personal/importar-txt", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def staff_import_txt():
    """18 sep, 6ta ronda (pedido de Braulio: "Si te adjunto varios .txt
    que usa telecredito y ya sabes como usa su estructura, puedes agregar
    los datos del personal?"): lee uno o más .txt de Telecrédito YA
    GENERADOS (de este sistema o del propio banco, de Planilla u
    Honorarios — el formato es el mismo desde el patch 0054) y completa el
    Catálogo de Personal con los datos bancarios de cada fila de pago que
    encuentra — ver app/telecredito_txt_import.py para el detalle exacto
    de qué posiciones lee (mismas que escribe app/telecredito.py,
    verificado contra un archivo real). A propósito NO toca "empresa" de
    cada persona ni ningún monto — ver el docstring de ese módulo para el
    motivo.

    También acepta un .zip con muchos .txt adentro (Braulio adjuntó un
    archivador histórico de varios años con esta misma estructura de
    carpetas por año/mes que ya usaba a mano) — se extraen y procesan
    todos los .txt que haya dentro, en cualquier subcarpeta, y se ignora
    en silencio cualquier otro tipo de archivo que venga mezclado (PDFs,
    Excel, etc. — es común que compartan la misma carpeta)."""
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        uploaded = [f for f in request.files.getlist("archivos") if f and f.filename]
        if not uploaded:
            flash("Elige al menos un archivo .txt (o un .zip con varios adentro).", "error")
            return redirect(url_for("pagos_personal.staff_import_txt"))

        # (label, raw_bytes) por cada .txt a procesar — expandiendo cualquier .zip.
        txt_entries = []
        errors = []
        for file_storage in uploaded:
            fname = file_storage.filename
            lower = fname.lower()
            if lower.endswith(".txt"):
                txt_entries.append((fname, file_storage.read()))
            elif lower.endswith(".zip"):
                raw_zip = file_storage.read()
                try:
                    zf = zipfile.ZipFile(io.BytesIO(raw_zip))
                except zipfile.BadZipFile:
                    errors.append({"row": fname, "message": "No se pudo leer el .zip — revisa que el archivo no esté dañado."})
                    continue
                for name in zf.namelist():
                    if name.endswith("/") or "/__MACOSX/" in name or name.startswith("__MACOSX/"):
                        continue
                    base = name.rsplit("/", 1)[-1]
                    if base.startswith(".") or not base.lower().endswith(".txt"):
                        continue  # .DS_Store y otros archivos que no son .txt se ignoran sin avisar
                    raw_bytes = zf.read(name)
                    if raw_bytes:
                        txt_entries.append((name, raw_bytes))
            else:
                errors.append({"row": fname, "message": "No es un archivo .txt ni .zip, se ignoró."})

        created, updated, skipped = 0, 0, []
        for label, raw in txt_entries:
            header_info, rows, warnings = parse_txt_file(raw)
            for w in warnings:
                errors.append({"row": label, "message": w})
            seen_in_file = set()
            for row in rows:
                key = row.get("document_number") or row["name"].strip().lower()
                if key in seen_in_file:
                    skipped.append({"row": f"{label}:{row['line']}", "message": f"{row['name']} repetido dentro del mismo archivo; ya se había procesado antes."})
                    continue
                seen_in_file.add(key)
                bank_fields = {
                    "document_type": row["document_type"],
                    "document_number": row["document_number"],
                    "account_type": row["account_type"],
                    "currency": row["currency"],
                }
                if row["is_cci"]:
                    bank_fields["cci"] = row["bank_value"]
                else:
                    bank_fields["account_number"] = row["bank_value"]
                staff_id, was_created = _upsert_staff_bank_data(row["name"], **bank_fields)
                if was_created:
                    created += 1
                else:
                    updated += 1

        result = {"created": created, "updated": updated, "skipped": skipped, "errors": errors}
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("pagos_personal.staff_list"), retry_url=url_for("pagos_personal.staff_import_txt"),
        )

    return render_template("pagos_personal/staff_import_txt.html")


# --- Constancias de pago (archivador por año -> mes) ---

def _voucher_extension(file_storage):
    ext = os.path.splitext(file_storage.filename)[1].lower()
    return ext if ext in ALLOWED_VOUCHER_EXTENSIONS else None


def _save_voucher_file(file_storage):
    """Igual que _save_receipt_file(), pero validando contra
    ALLOWED_VOUCHER_EXTENSIONS (más amplia) y guardando con
    storage.save_payment_voucher(). Devuelve el nombre guardado, o None si
    no se subió nada válido."""
    if not file_storage or not file_storage.filename:
        return None
    ext = _voucher_extension(file_storage)
    if not ext:
        return None
    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None
    filename = f"{uuid.uuid4().hex}{ext}"
    storage.save_payment_voucher(filename, raw_bytes)
    return filename


def _period_str(year, month):
    return f"{year:04d}-{month:02d}"


def _voucher_period_context(period):
    """company_bank_accounts activas + payment_type_labels, usados por el
    formulario de subida (para etiquetar opcionalmente de qué cuenta/tipo
    es cada constancia)."""
    return {
        "accounts": query_all("SELECT * FROM company_bank_accounts WHERE active = 1 ORDER BY company_name, sort_order"),
        "payment_type_labels": PAYMENT_TYPE_LABELS,
    }


@bp.route("/constancias")
@permission_required("pagos_personal", "view")
def constancias_years():
    rows = query_all(
        "SELECT substr(period, 1, 4) AS year, COUNT(*) AS n FROM payment_vouchers WHERE deleted_at IS NULL GROUP BY year ORDER BY year DESC"
    )
    years = {int(r["year"]): r["n"] for r in rows}
    current_year = int(today_str()[:4])
    years.setdefault(current_year, 0)
    years_sorted = sorted(years.items(), key=lambda kv: kv[0], reverse=True)
    return render_template("pagos_personal/constancias_years.html", years=years_sorted, current_year=current_year)


@bp.route("/constancias/importar-zip", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def constancias_import_zip():
    """Carga inicial masiva: acepta un .zip con la misma organización de
    carpetas por año/mes que ya usaba Braulio a mano ("2025/junio
    2025/archivo.pdf", o variantes — ver app/payment_vouchers_import.py
    para el detalle de qué formas de carpeta/nombre reconoce). Cada
    archivo que sí se pueda ubicar en un año/mes se guarda y queda
    disponible en su mes correspondiente; los que no se puedan clasificar
    se listan al final para subirlos a mano desde el mes que corresponda."""
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        file_storage = request.files.get("archivo_zip")
        if not file_storage or not file_storage.filename:
            flash("Elige un archivo .zip para importar.", "error")
            return redirect(url_for("pagos_personal.constancias_import_zip"))
        if not file_storage.filename.lower().endswith(".zip"):
            flash("El archivo tiene que ser un .zip.", "error")
            return redirect(url_for("pagos_personal.constancias_import_zip"))

        raw = file_storage.read()
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            flash("No se pudo leer el .zip — revisa que el archivo no esté dañado.", "error")
            return redirect(url_for("pagos_personal.constancias_import_zip"))

        imported = 0
        skipped_unclassified = []
        skipped_extension = []
        for name in zf.namelist():
            if name.endswith("/") or "/__MACOSX/" in name or name.startswith("__MACOSX/"):
                continue
            base = name.rsplit("/", 1)[-1]
            if base.startswith("."):
                continue  # .DS_Store y similares
            ext = os.path.splitext(base)[1].lower()
            if ext not in ALLOWED_VOUCHER_EXTENSIONS:
                skipped_extension.append(name)
                continue
            year, month = classify_zip_entry(name)
            if not year or not month:
                skipped_unclassified.append(name)
                continue
            raw_bytes = zf.read(name)
            if not raw_bytes:
                continue
            stored_filename = f"{uuid.uuid4().hex}{ext}"
            storage.save_payment_voucher(stored_filename, raw_bytes)
            execute(
                """INSERT INTO payment_vouchers (period, filename, original_filename, uploaded_by)
                   VALUES (?, ?, ?, ?)""",
                (_period_str(year, month), stored_filename, base, g.user["id"]),
            )
            imported += 1

        flash(f"Se importaron {imported} archivo(s) al archivador de constancias.", "success")
        if skipped_unclassified:
            flash(
                f"{len(skipped_unclassified)} archivo(s) no se pudieron ubicar en un año/mes y no se importaron — "
                f"súbelos a mano desde el mes que corresponda: {', '.join(skipped_unclassified[:15])}"
                + (f" y {len(skipped_unclassified) - 15} más." if len(skipped_unclassified) > 15 else "."),
                "error",
            )
        if skipped_extension:
            flash(
                f"{len(skipped_extension)} archivo(s) con un tipo no admitido no se importaron: "
                f"{', '.join(skipped_extension[:15])}" + (f" y {len(skipped_extension) - 15} más." if len(skipped_extension) > 15 else "."),
                "error",
            )
        return redirect(url_for("pagos_personal.constancias_years"))

    return render_template("pagos_personal/constancias_import.html")


@bp.route("/constancias/<int:year>")
@permission_required("pagos_personal", "view")
def constancias_months(year):
    rows = query_all(
        "SELECT substr(period, 6, 2) AS month, COUNT(*) AS n FROM payment_vouchers WHERE substr(period, 1, 4) = ? AND deleted_at IS NULL GROUP BY month",
        (f"{year:04d}",),
    )
    counts = {int(r["month"]): r["n"] for r in rows}
    months = [(m, MONTH_LABELS[m], counts.get(m, 0)) for m in range(1, 13)]
    return render_template("pagos_personal/constancias_months.html", year=year, months=months)


@bp.route("/constancias/<int:year>/<int:month>")
@permission_required("pagos_personal", "view")
def constancias_month_detail(year, month):
    if month < 1 or month > 12:
        abort(404)
    period = _period_str(year, month)
    files = query_all(
        """SELECT v.*, a.company_name, a.bank_name, a.alias
           FROM payment_vouchers v LEFT JOIN company_bank_accounts a ON a.id = v.bank_account_id
           WHERE v.period = ? AND v.deleted_at IS NULL ORDER BY v.created_at DESC, v.id DESC""",
        (period,),
    )
    return render_template(
        "pagos_personal/constancias_month_detail.html",
        year=year, month=month, month_label=MONTH_LABELS[month], period=period, files=files,
        **_voucher_period_context(period),
    )


@bp.route("/constancias/<int:year>/<int:month>/subir", methods=["POST"])
@permission_required("pagos_personal", "edit")
def constancias_upload(year, month):
    if not validate_csrf():
        abort(400)
    if month < 1 or month > 12:
        abort(404)
    period = _period_str(year, month)
    bank_account_id = request.form.get("bank_account_id", type=int) or None
    payment_type = request.form.get("payment_type") or None
    if payment_type not in PAYMENT_TYPE_LABELS:
        payment_type = None
    label = request.form.get("label", "").strip() or None

    files = [f for f in request.files.getlist("archivos") if f and f.filename]
    if not files:
        flash("Elige al menos un archivo para subir.", "error")
        return redirect(url_for("pagos_personal.constancias_month_detail", year=year, month=month))

    saved, rejected = 0, []
    for file_storage in files:
        filename = _save_voucher_file(file_storage)
        if not filename:
            rejected.append(file_storage.filename)
            continue
        execute(
            """INSERT INTO payment_vouchers (period, bank_account_id, payment_type, label, filename, original_filename, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (period, bank_account_id, payment_type, label, filename, file_storage.filename, g.user["id"]),
        )
        saved += 1

    if saved:
        flash(f"Se subieron {saved} archivo(s).", "success")
    if rejected:
        flash(f"No se pudieron subir (tipo de archivo no admitido): {', '.join(rejected)}.", "error")
    return redirect(url_for("pagos_personal.constancias_month_detail", year=year, month=month))


@bp.route("/constancias/archivo/<int:voucher_id>")
@permission_required("pagos_personal", "view")
def constancias_file(voucher_id):
    voucher = query_one("SELECT filename FROM payment_vouchers WHERE id = ?", (voucher_id,))
    if voucher is None:
        abort(404)
    filename = voucher["filename"]
    if storage.using_s3():
        return redirect(storage.payment_voucher_url(filename))
    return send_from_directory(storage.local_payment_vouchers_dir(), filename)


@bp.route("/constancias/archivo/<int:voucher_id>/eliminar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def constancias_delete(voucher_id):
    """18 sep, 5ta ronda (pedido de Braulio: "hay manera que se envie
    primero a una papelera antes que se elimine de la base de datos ...
    y saber que usuario lo hizo?"): esto ya NO borra la fila — la marca
    como eliminada (deleted_at/deleted_by) y desaparece de la vista normal
    del mes, pero se puede recuperar desde la Papelera
    (constancias_papelera()/constancias_restore()) sabiendo quién y
    cuándo la mandó ahí. El archivo en sí (disco/S3) tampoco se borra."""
    if not validate_csrf():
        abort(400)
    voucher = query_one("SELECT period FROM payment_vouchers WHERE id = ? AND deleted_at IS NULL", (voucher_id,))
    if voucher is None:
        abort(404)
    year, month = voucher["period"].split("-")
    execute(
        "UPDATE payment_vouchers SET deleted_at = ?, deleted_by = ? WHERE id = ?",
        (now_str(), g.user["id"], voucher_id),
    )
    flash("Constancia enviada a la papelera.", "success")
    return redirect(url_for("pagos_personal.constancias_month_detail", year=int(year), month=int(month)))


@bp.route("/constancias/papelera")
@permission_required("pagos_personal", "edit")
def constancias_papelera():
    """Lista todo lo enviado a la papelera (de cualquier año/mes), más
    reciente primero, con quién lo eliminó y cuándo — para recuperarlo por
    error o confirmar que ya no hace falta."""
    files = query_all(
        """SELECT v.*, u.name AS deleted_by_name
           FROM payment_vouchers v LEFT JOIN users u ON u.id = v.deleted_by
           WHERE v.deleted_at IS NOT NULL ORDER BY v.deleted_at DESC, v.id DESC"""
    )
    return render_template("pagos_personal/constancias_papelera.html", files=files)


@bp.route("/constancias/archivo/<int:voucher_id>/restaurar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def constancias_restore(voucher_id):
    if not validate_csrf():
        abort(400)
    voucher = query_one("SELECT id FROM payment_vouchers WHERE id = ? AND deleted_at IS NOT NULL", (voucher_id,))
    if voucher is None:
        abort(404)
    execute("UPDATE payment_vouchers SET deleted_at = NULL, deleted_by = NULL WHERE id = ?", (voucher_id,))
    flash("Constancia restaurada.", "success")
    return redirect(url_for("pagos_personal.constancias_papelera"))


# --- Plantilla de honorarios (lista reusable mes a mes) ---

def _honorarios_template_items():
    return query_all(
        """SELECT t.*, s.name AS staff_name, s.document_type, s.document_number,
                  s.bank_name, s.account_number, s.cci, s.currency, s.status AS staff_status
           FROM honorarios_template_items t JOIN staff s ON s.id = t.staff_id
           ORDER BY t.sort_order, s.name"""
    )


def _honorarios_month_rows(period, q="", concept_filter="", status_filter=""):
    """Arma las filas de la plantilla activa para UN periodo puntual —
    monto/concepto/N° de comprobante de ESE mes (si ya hay un
    staff_payments para esa persona+periodo) o el default de la plantilla
    si todavía no se generó nada, más la constancia enlazada si ya la
    tiene. Se usa tanto para mostrar la pantalla (GET honorarios_plantilla)
    como, con los MISMOS q/concept_filter/status_filter, para saber en el
    POST (honorarios_plantilla_guardar_mes) exactamente qué filas estaban
    a la vista — así un filtro puesto nunca crea, actualiza ni borra el
    pago de alguien que quedó fuera de la vista por el filtro, solo de
    quien sí se ve y se desmarca a propósito.

    status_filter "PENDIENTE" incluye tanto lo que ya tiene un pago
    PENDIENTE como lo que todavía no tiene ningún pago generado ese mes
    (en los dos casos, "pendiente" en el sentido de "todavía no pagado")."""
    items = _honorarios_template_items()
    existing_payments = {
        p["staff_id"]: p for p in query_all(
            "SELECT * FROM staff_payments WHERE period = ? AND payment_type = 'RECIBO_HONORARIOS'", (period,)
        )
    }
    voucher_ids = {p["payment_voucher_id"] for p in existing_payments.values() if p["payment_voucher_id"]}
    vouchers = {}
    if voucher_ids:
        vouchers = {
            v["id"]: v for v in query_all(
                f"SELECT * FROM payment_vouchers WHERE id IN ({','.join('?' * len(voucher_ids))})", list(voucher_ids)
            )
        }
    q_lower = q.strip().lower()
    concept_lower = concept_filter.strip().lower()
    rows = []
    for item in items:
        if not item["active"]:
            continue
        payment = existing_payments.get(item["staff_id"])
        effective_concept = (payment["concept"] if payment else item["default_concept"]) or ""
        if q_lower and q_lower not in item["staff_name"].lower():
            continue
        if concept_lower and concept_lower not in effective_concept.lower():
            continue
        row_status = payment["status"] if payment else None
        if status_filter == "PENDIENTE" and row_status == "PAGADO":
            continue
        if status_filter == "PAGADO" and row_status != "PAGADO":
            continue
        rows.append({
            "item": item,
            "payment": payment,
            "amount": payment["amount"] if payment else item["default_amount"],
            "concept": effective_concept,
            "receipt_number": (payment["receipt_number"] if payment else "") or "",
            "locked": payment is not None and payment["status"] == "PAGADO",
            "voucher": vouchers.get(payment["payment_voucher_id"]) if payment and payment["payment_voucher_id"] else None,
            "exported": bool(payment and payment["exported_at"] and payment["status"] == "PENDIENTE"),
        })
    return rows


@bp.route("/honorarios/plantilla")
@permission_required("pagos_personal", "edit")
def honorarios_plantilla():
    period = request.args.get("period") or today_str()[:7]
    q = request.args.get("q", "").strip()
    concept_filter = request.args.get("concept", "").strip()
    status_filter = request.args.get("status", "").strip()

    items = _honorarios_template_items()
    template_staff_ids = {i["staff_id"] for i in items}
    available_staff = [s for s in get_active_staff() if s["id"] not in template_staff_ids]
    all_concepts = sorted({
        (i["default_concept"] or "").strip() for i in items if (i["default_concept"] or "").strip()
    })
    month_rows = _honorarios_month_rows(period, q, concept_filter, status_filter)
    period_vouchers = query_all(
        "SELECT * FROM payment_vouchers WHERE period = ? AND deleted_at IS NULL ORDER BY created_at DESC", (period,)
    )

    return render_template(
        "pagos_personal/honorarios_plantilla.html",
        items=items, available_staff=available_staff, currency_labels=CURRENCY_LABELS,
        period=period, q=q, concept_filter=concept_filter, status_filter=status_filter, all_concepts=all_concepts,
        month_rows=month_rows, period_vouchers=period_vouchers, today=today_str(),
    )


@bp.route("/honorarios/plantilla/mes", methods=["POST"])
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_guardar_mes():
    """Guarda los pagos de RECIBO_HONORARIOS del periodo elegido según lo
    marcado/editado en la pantalla (crea/actualiza/quita), y según el
    botón que se usó se queda en la Plantilla ("guardar"), redirige a
    telecredito_configure() con la selección exacta de pagos para armar y
    descargar el archivo ("generar" — ver el docstring grande al inicio
    del módulo, sección 4), o enlaza una constancia ya subida a los pagos
    seleccionados y recién ahí los marca PAGADO ("enlazar" — pedido de
    Braulio, 19 sep: "una vez que se paguen enlazar la constancia de pago
    de manera manual para que figuren como pagados"). Solo toca las filas
    que estaban dentro del filtro (q/concept/status) que tenía puesto la
    pantalla — ver _honorarios_month_rows()."""
    if not validate_csrf():
        abort(400)
    period = request.form.get("period") or today_str()[:7]
    q = request.form.get("q", "")
    concept_filter = request.form.get("concept", "")
    status_filter = request.form.get("status", "")
    action = request.form.get("action") or "guardar"

    def _back():
        return redirect(url_for("pagos_personal.honorarios_plantilla", period=period, q=q, concept=concept_filter, status=status_filter))

    rows = _honorarios_month_rows(period, q, concept_filter, status_filter)
    checked_ids = {int(v) for v in request.form.getlist("incluir")}
    created, updated, removed = 0, 0, 0
    synced_payment_ids = []

    for row in rows:
        item = row["item"]
        existing = row["payment"]
        if item["id"] not in checked_ids:
            if existing and existing["status"] == "PENDIENTE":
                execute("DELETE FROM staff_payments WHERE id = ?", (existing["id"],))
                removed += 1
            continue
        if existing and existing["status"] == "PAGADO":
            continue  # ya pagado, no se vuelve a tocar ni a incluir en un nuevo archivo/enlace
        amount = parse_float(request.form.get(f"amount_{item['id']}"), item["default_amount"])
        concept = (request.form.get(f"concept_{item['id']}", "") or "").strip() or item["default_concept"]
        receipt_number = (request.form.get(f"receipt_{item['id']}", "") or "").strip() or None
        if existing:
            execute(
                "UPDATE staff_payments SET amount = ?, concept = ?, receipt_number = ? WHERE id = ?",
                (amount, concept, receipt_number, existing["id"]),
            )
            updated += 1
            synced_payment_ids.append(existing["id"])
        else:
            new_id = execute(
                """INSERT INTO staff_payments (staff_id, payment_type, period, amount, concept, receipt_number, status)
                   VALUES (?, 'RECIBO_HONORARIOS', ?, ?, ?, ?, 'PENDIENTE')""",
                (item["staff_id"], period, amount, concept, receipt_number),
            )
            created += 1
            synced_payment_ids.append(new_id)

    if action == "generar":
        if not synced_payment_ids:
            flash("Elige al menos una persona (todavía sin pagar) para generar el archivo de Telecrédito.", "error")
            return _back()
        return redirect(url_for(
            "pagos_personal.telecredito_configure", period=period, payment_type="RECIBO_HONORARIOS",
            payment_ids=synced_payment_ids, origin="honorarios_plantilla",
            plantilla_q=q, plantilla_concept=concept_filter, plantilla_status=status_filter,
        ))

    if action == "enlazar":
        if not synced_payment_ids:
            flash("Elige al menos una persona (todavía sin pagar) para enlazar la constancia.", "error")
            return _back()
        voucher_id = request.form.get("voucher_id", type=int)
        voucher = query_one("SELECT id FROM payment_vouchers WHERE id = ? AND deleted_at IS NULL", (voucher_id,)) if voucher_id else None
        if not voucher:
            flash("Elige una constancia válida para enlazar — si todavía no la subiste, hazlo primero desde Constancias.", "error")
            return _back()
        payment_date = request.form.get("payment_date") or today_str()
        execute(
            f"""UPDATE staff_payments SET status = 'PAGADO', payment_voucher_id = ?, payment_date = ?
                WHERE id IN ({','.join('?' * len(synced_payment_ids))})""",
            [voucher_id, payment_date] + synced_payment_ids,
        )
        flash(f"Constancia enlazada a {len(synced_payment_ids)} pago(s) — quedaron marcados como pagados.", "success")
        return _back()

    flash(f"Listo: {created} pago(s) nuevo(s), {updated} actualizado(s), {removed} quitado(s).", "success")
    return _back()


@bp.route("/honorarios/plantilla/<int:payment_id>/desenlazar-constancia", methods=["POST"])
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_desenlazar_constancia(payment_id):
    """Revierte un enlace hecho por error: vuelve el pago a PENDIENTE y le
    quita la constancia y la fecha de pago — no borra la constancia en sí,
    solo la desenlaza de este pago puntual."""
    if not validate_csrf():
        abort(400)
    payment = query_one("SELECT * FROM staff_payments WHERE id = ? AND payment_type = 'RECIBO_HONORARIOS'", (payment_id,))
    if payment is None:
        abort(404)
    execute(
        "UPDATE staff_payments SET status = 'PENDIENTE', payment_voucher_id = NULL, payment_date = NULL WHERE id = ?",
        (payment_id,),
    )
    flash("Constancia desenlazada — el pago volvió a quedar pendiente.", "success")
    return redirect(url_for("pagos_personal.honorarios_plantilla", period=payment["period"]))


@bp.route("/honorarios/plantilla/agregar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_add():
    if not validate_csrf():
        abort(400)
    staff_id = request.form.get("staff_id", type=int)
    staff = query_one("SELECT id FROM staff WHERE id = ?", (staff_id,))
    if not staff:
        flash("Elige una persona válida del catálogo de Personal.", "error")
        return redirect(url_for("pagos_personal.honorarios_plantilla"))
    if query_one("SELECT id FROM honorarios_template_items WHERE staff_id = ?", (staff_id,)):
        flash("Esa persona ya está en la plantilla.", "error")
        return redirect(url_for("pagos_personal.honorarios_plantilla"))
    amount = parse_float(request.form.get("default_amount"), 0)
    concept = request.form.get("default_concept", "").strip() or None
    max_order = query_one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM honorarios_template_items")["m"]
    execute(
        "INSERT INTO honorarios_template_items (staff_id, default_amount, default_concept, sort_order) VALUES (?, ?, ?, ?)",
        (staff_id, amount, concept, max_order + 1),
    )
    flash("Persona agregada a la plantilla.", "success")
    return redirect(url_for("pagos_personal.honorarios_plantilla"))


@bp.route("/honorarios/plantilla/<int:item_id>/editar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_edit(item_id):
    if not validate_csrf():
        abort(400)
    item = query_one("SELECT id FROM honorarios_template_items WHERE id = ?", (item_id,))
    if item is None:
        abort(404)
    amount = parse_float(request.form.get("default_amount"), 0)
    concept = request.form.get("default_concept", "").strip() or None
    execute(
        "UPDATE honorarios_template_items SET default_amount = ?, default_concept = ? WHERE id = ?",
        (amount, concept, item_id),
    )
    flash("Plantilla actualizada.", "success")
    return redirect(url_for("pagos_personal.honorarios_plantilla"))


@bp.route("/honorarios/plantilla/<int:item_id>/alternar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_toggle(item_id):
    if not validate_csrf():
        abort(400)
    item = query_one("SELECT active FROM honorarios_template_items WHERE id = ?", (item_id,))
    if item is None:
        abort(404)
    execute("UPDATE honorarios_template_items SET active = ? WHERE id = ?", (0 if item["active"] else 1, item_id))
    flash("Actualizado.", "success")
    return redirect(url_for("pagos_personal.honorarios_plantilla"))


@bp.route("/honorarios/plantilla/<int:item_id>/quitar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_remove(item_id):
    """Quita a la persona de la plantilla (no la elimina del Catálogo de
    Personal, ni toca ningún pago ya registrado — solo deja de ofrecerse
    por defecto cada mes)."""
    if not validate_csrf():
        abort(400)
    execute("DELETE FROM honorarios_template_items WHERE id = ?", (item_id,))
    flash("Quitada de la plantilla.", "success")
    return redirect(url_for("pagos_personal.honorarios_plantilla"))


@bp.route("/honorarios/plantilla/importar/plantilla")
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_template():
    buffer = build_import_template("Plantilla de honorarios", HONORARIOS_TEMPLATE_COLUMNS, HONORARIOS_TEMPLATE_EXAMPLE)
    return Response(
        buffer.getvalue(), mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_honorarios.xlsx"'},
    )


def _apply_honorarios_template_import(rows, example_skips):
    created, updated, errors = 0, 0, []
    skipped = [
        {"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."}
        for r in example_skips
    ]
    max_order = query_one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM honorarios_template_items")["m"]
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})
        name = (row.get("name") or "").strip()
        if not name:
            errors.append({"row": n, "message": "Falta el nombre; la fila no se importó."})
            continue
        staff_id, staff_created = _upsert_staff_bank_data(
            name,
            document_type=row.get("document_type"), document_number=row.get("document_number"),
            bank_name=row.get("bank_name"), account_number=row.get("account_number"),
            cci=row.get("cci"), account_type=row.get("account_type"), currency=row.get("currency"),
        )
        existing_item = query_one("SELECT id FROM honorarios_template_items WHERE staff_id = ?", (staff_id,))
        amount = row.get("default_amount") or 0
        concept = row.get("default_concept") or None
        if existing_item:
            execute(
                "UPDATE honorarios_template_items SET default_amount = ?, default_concept = ?, active = 1 WHERE id = ?",
                (amount, concept, existing_item["id"]),
            )
        else:
            max_order += 1
            execute(
                "INSERT INTO honorarios_template_items (staff_id, default_amount, default_concept, sort_order) VALUES (?, ?, ?, ?)",
                (staff_id, amount, concept, max_order),
            )
        if staff_created:
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


@bp.route("/honorarios/plantilla/importar", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def honorarios_plantilla_import():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), HONORARIOS_TEMPLATE_COLUMNS, HONORARIOS_TEMPLATE_EXAMPLE)
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("pagos_personal.honorarios_plantilla_import"))
        result = _apply_honorarios_template_import(rows, example_skips)
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("pagos_personal.honorarios_plantilla"), retry_url=url_for("pagos_personal.honorarios_plantilla_import"),
        )
    return render_template(
        "import_form.html", title="Importar plantilla de honorarios", module_label="la plantilla de honorarios",
        template_url=url_for("pagos_personal.honorarios_plantilla_template"), upload_url=url_for("pagos_personal.honorarios_plantilla_import"),
        back_url=url_for("pagos_personal.honorarios_plantilla"), columns=HONORARIOS_TEMPLATE_COLUMNS,
    )


@bp.route("/honorarios/generar", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def honorarios_generar():
    """18 sep, 7ma ronda: esta pantalla separada se fusionó dentro de
    honorarios_plantilla() (que ahora ya trae, en la misma vista, el
    filtro por nombre/concepto, casillas + monto + N° de comprobante del
    mes y el botón para generar el archivo de Telecrédito — ver el
    docstring grande al inicio del módulo). Se deja esta ruta como
    redirección para no romper accesos directos/marcadores viejos."""
    period = request.args.get("period") or request.form.get("period") or today_str()[:7]
    return redirect(url_for("pagos_personal.honorarios_plantilla", period=period))
