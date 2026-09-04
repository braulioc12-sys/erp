"""Liquidaciones: módulo único que junta lo que antes eran "Gastos" y
"Viáticos" — Braulio pidió replantearlo así (27 ago) porque en la práctica
cada liquidación contable nace de un viaje: se le da un anticipo al
conductor, se van registrando los gastos reales de ese viaje, y al cerrar
el viaje se decide manualmente cuáles de esos gastos entran en la
liquidación (antes entraban todos automático) y se liquida contra una
oficina — de ahí sale la fila Haber (el vale) + una fila Debe por cada
gasto incluido, en el formato de la "hoja resumen" de Harraso (ver
app/accounting.py).

Sigue habiendo exactamente **una liquidación por viaje** (una fila en
`expense_advances` por `trip_id`), pero ahora la asignación de gastos a esa
liquidación es explícita (checkboxes en el detalle) en vez de automática."""
import base64
import io
import os
import secrets
import uuid

from flask import Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps

from app.accounting import (
    DEFAULT_CURRENCY,
    DOCUMENT_TYPES,
    VALE_DOCUMENT_TYPE,
    office_choices,
    office_info,
    voucher_label,
)
from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import now_str, parse_date, parse_float, pretty_label, today_str
from app.integrations.sunat_exchange_rate import get_rate_for_date
from app.integrations.sunat_ruc import get_company_for_ruc
from app.reports import build_expenses_workbook, build_liquidacion_workbook
from app.routes.rutas import find_route
from app import storage

bp = Blueprint("liquidaciones", __name__, url_prefix="/liquidaciones")

ALLOWED_RECEIPT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".webp", ".heic", ".heif"}

# Las fotos de comprobantes se re-comprimen antes de guardarlas: se reducen
# a este tamaño máximo (lado más largo, en píxeles) y se recodifican como
# JPEG con esta calidad. 1600px y calidad 72 dejan el texto del comprobante
# perfectamente legible y bajan el peso del archivo entre 80% y 95% frente
# a una foto de celular sin comprimir (que suele pesar 3-8 MB).
RECEIPT_MAX_DIMENSION = 1600
RECEIPT_JPEG_QUALITY = 72

# Si el navegador no manda una extensión reconocible en el nombre del
# archivo (pasa a veces con fotos tomadas directo desde la cámara del
# celular), se usa el tipo MIME que sí manda el navegador para elegir una.
MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}


def _first_uploaded_file():
    """El formulario de gastos ofrece dos campos con el mismo nombre
    ("receipt"): uno con `capture="environment"` (abre la cámara directo en
    el celular) y otro para elegir un archivo ya existente (foto o PDF). El
    usuario solo llena uno; esto devuelve el primero que sí traiga un
    archivo."""
    for file_storage in request.files.getlist("receipt"):
        if file_storage and file_storage.filename:
            return file_storage
    return None


def _compress_receipt_image(raw_bytes):
    """Redimensiona y recomprime una foto de comprobante como JPEG, para que
    ocupe el menor espacio posible (importante en disco local; en S3 baja
    el costo de almacenamiento y de transferencia). Devuelve los bytes JPEG
    ya comprimidos, o None si el archivo no se pudo abrir como imagen (por
    ejemplo, un formato no soportado como algunos HEIC de iPhone), en cuyo
    caso el llamador debe guardar el archivo original sin tocar."""
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            # Corrige la rotación: los celulares guardan la orientación real
            # en metadatos EXIF en vez de rotar los píxeles.
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((RECEIPT_MAX_DIMENSION, RECEIPT_MAX_DIMENSION), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=RECEIPT_JPEG_QUALITY, optimize=True)
            return buffer.getvalue()
    except Exception:
        return None


def _save_receipt(file_storage):
    """Guarda el comprobante adjunto (foto o PDF) subido como multipart/form-data
    (formulario normal del navegador) y devuelve el nombre de archivo guardado,
    o None si no se envió nada válido. Delega el guardado en sí a
    _save_receipt_bytes (ver ahí el detalle de compresión/almacenamiento)."""
    if not file_storage or not file_storage.filename:
        return None
    return _save_receipt_bytes(file_storage.read(), file_storage.filename, file_storage.mimetype)


def _save_receipt_bytes(raw_bytes, filename, mimetype=None):
    """Guarda el comprobante adjunto (foto o PDF) a partir de bytes ya en
    memoria — usado tanto por _save_receipt (formulario/multipart) como por
    el intake de WhatsApp/n8n (JSON con la imagen en base64, ver
    whatsapp_intake). Devuelve el nombre de archivo guardado, o None si no
    hay nada válido que guardar. Las fotos se redimensionan y recomprimen
    como JPEG para ocupar el menor espacio posible (ver
    _compress_receipt_image); los PDF se guardan tal cual. El guardado en sí
    (disco local o Amazon S3) lo decide app/storage.py según el ambiente —
    ver README, sección "Base de datos persistente en AWS (RDS + S3)". En
    disco local (por defecto) estos archivos se pierden al
    reiniciar/redesplegar en hosting con disco efímero, igual que la base de
    datos SQLite."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        ext = MIME_TO_EXTENSION.get((mimetype or "").lower())
    if not ext:
        return None

    if not raw_bytes:
        return None

    if ext == ".pdf":
        filename = f"{uuid.uuid4().hex}.pdf"
        storage.save_receipt(filename, raw_bytes)
        return filename

    # Es una foto: intentamos comprimirla. Siempre se guarda como .jpg
    # porque para fotos, JPEG comprime mucho mejor que PNG/WEBP/HEIC.
    compressed = _compress_receipt_image(raw_bytes)
    if compressed is not None:
        filename = f"{uuid.uuid4().hex}.jpg"
        storage.save_receipt(filename, compressed)
        return filename

    # Si no se pudo abrir como imagen (formato raro o archivo corrupto),
    # guardamos el original sin comprimir para no perder el comprobante.
    filename = f"{uuid.uuid4().hex}{ext}"
    storage.save_receipt(filename, raw_bytes)
    return filename


def budget_alerts():
    """Compara el gasto acumulado del mes en curso contra los presupuestos
    activos (por unidad o por tipo de gasto) y devuelve los que ya se
    excedieron o están a un 90% o más de su límite. La usa el Panel."""
    budgets = query_all("SELECT * FROM expense_budgets WHERE active = 1")
    alerts = []
    for b in budgets:
        if b["scope_type"] == "VEHICLE":
            vehicle = query_one("SELECT plate FROM vehicles WHERE id = ?", (b["scope_value"],))
            label = f"Unidad {vehicle['plate']}" if vehicle else f"Unidad #{b['scope_value']}"
            spent = query_one(
                """SELECT COALESCE(SUM(amount), 0) total FROM expenses
                   WHERE vehicle_id = ? AND strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')""",
                (b["scope_value"],),
            )["total"]
        else:
            label = pretty_label(b["scope_value"])
            spent = query_one(
                """SELECT COALESCE(SUM(amount), 0) total FROM expenses
                   WHERE type = ? AND strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')""",
                (b["scope_value"],),
            )["total"]
        if b["monthly_amount"] <= 0:
            continue
        ratio = spent / b["monthly_amount"]
        if ratio >= 0.9:
            alerts.append(
                {
                    "label": label,
                    "spent": spent,
                    "budget": b["monthly_amount"],
                    "ratio": ratio,
                    "over": ratio >= 1,
                }
            )
    return alerts


# --- Liquidaciones (una por viaje): lista, anticipo, detalle con
# asignación de gastos y liquidar ---

@bp.route("")
@permission_required("liquidaciones", "view")
def list_view():
    advances = query_all(
        """SELECT a.*, t.code as trip_code, t.origin, t.destination,
                  (SELECT COALESCE(SUM(e.amount), 0) FROM expenses e WHERE e.trip_id = a.trip_id) as spent
           FROM expense_advances a
           JOIN trips t ON t.id = a.trip_id
           ORDER BY a.given_date DESC, a.id DESC"""
    )
    whatsapp_pending_count = query_one(
        "SELECT COUNT(*) n FROM whatsapp_expense_drafts WHERE status = 'PENDIENTE'"
    )["n"]
    return render_template("liquidaciones/list.html", advances=advances, whatsapp_pending_count=whatsapp_pending_count)


# 4 sep, pedido de Braulio: "cuando el operador sea BRMS deben empezar
# como B-0001, y si es harraso como H-0001" — código de la liquidación
# según la empresa operadora del viaje (trips.issuer), independiente del
# Num.Voucher contable (que se asigna al liquidar y se reinicia cada mes
# por oficina — ver _next_voucher_number). Cada empresa lleva su propio
# correlativo, que nunca se reinicia.
LIQUIDATION_CODE_PREFIXES = {"BRMS": "B", "HARRASO": "H"}


def _next_liquidation_code(issuer):
    prefix = LIQUIDATION_CODE_PREFIXES.get(issuer, "H")
    row = query_one(
        """SELECT COUNT(*) as n FROM expense_advances a
           JOIN trips t ON t.id = a.trip_id WHERE t.issuer = ?""",
        (issuer,),
    )
    n = (row["n"] if row else 0) + 1
    return f"{prefix}-{n:04d}"


@bp.route("/anticipo/<int:trip_id>", methods=["GET", "POST"])
@permission_required("liquidaciones", "edit")
def new_advance(trip_id):
    trip = query_one("SELECT * FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    # 4 sep, pedido de Braulio: "los viajes con terceros no deben registrar
    # liquidación, por lo tanto no tienen anticipo de viáticos, inspección
    # ni gastos de viaje" — el costo de un viaje subcontratado ya es el
    # flete acordado (third_party_rate), no algo que se liquide con
    # anticipos/gastos como un viaje de unidad propia. Chequeo en el
    # servidor (no solo ocultar el botón en viajes/detail.html) para que no
    # se pueda crear entrando directo por la URL.
    if trip["ownership"] == "TERCERO":
        flash("Los viajes con terceros no registran liquidación (el costo es el flete acordado).", "error")
        return redirect(url_for("viajes.detail", trip_id=trip_id))
    existing = query_one("SELECT id FROM expense_advances WHERE trip_id = ?", (trip_id,))
    if existing:
        flash("Este viaje ya tiene una liquidación (anticipo) registrada.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=existing["id"]))

    route = find_route(trip["origin"], trip["destination"])

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount_given"))
        if amount <= 0:
            flash("Indica un monto válido.", "error")
            return render_template("liquidaciones/advance_form.html", trip=trip, route=route, today=today_str())

        given_date = parse_date(request.form.get("given_date")) or today_str()
        notes = request.form.get("notes", "").strip()
        code = _next_liquidation_code(trip["issuer"])
        advance_id = execute(
            """INSERT INTO expense_advances (trip_id, route_id, amount_given, given_date, notes, created_by, code)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                trip_id,
                route["id"] if route else None,
                amount,
                given_date,
                notes,
                None,
                code,
            ),
        )
        # El monto inicial también queda como el primer registro en
        # advance_payments — así la liquidación arranca con su historial
        # de anticipos completo (ver _recalc_advance_total más abajo, y el
        # pedido de Braulio del 28 ago de poder dar más de un anticipo por
        # liquidación: uno al inicio del viaje, otro a mitad de camino).
        execute(
            "INSERT INTO advance_payments (advance_id, amount, payment_date, notes) VALUES (?, ?, ?, ?)",
            (advance_id, amount, given_date, notes),
        )
        flash(f"Anticipo confirmado: {trip['code']} recibió S/ {amount:.2f}. Ya puedes registrar sus gastos.", "success")
        return redirect(url_for("liquidaciones.detail", advance_id=advance_id))

    return render_template("liquidaciones/advance_form.html", trip=trip, route=route, today=today_str())


def _recalc_advance_total(advance_id):
    """`expense_advances.amount_given` sigue siendo el monto TOTAL entregado
    — se recalcula sumando advance_payments cada vez que se agrega o quita
    un anticipo, para que el resto del sistema (Resumen contable, la
    comparación contra lo gastado, etc.) siga leyendo un solo número."""
    total = query_one(
        "SELECT COALESCE(SUM(amount), 0) s FROM advance_payments WHERE advance_id = ?", (advance_id,)
    )["s"]
    execute("UPDATE expense_advances SET amount_given = ? WHERE id = ?", (total, advance_id))


@bp.route("/<int:advance_id>/anticipos", methods=["POST"])
@permission_required("liquidaciones", "edit")
def add_payment(advance_id):
    if not validate_csrf():
        abort(400)
    advance = query_one("SELECT * FROM expense_advances WHERE id = ?", (advance_id,))
    if advance is None:
        abort(404)
    if advance["status"] == "LIQUIDADO":
        flash("Esta liquidación ya está cerrada — no se pueden agregar más anticipos.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance_id))
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Indica un monto válido para el anticipo.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance_id))
    payment_date = parse_date(request.form.get("payment_date")) or today_str()
    execute(
        "INSERT INTO advance_payments (advance_id, amount, payment_date, notes) VALUES (?, ?, ?, ?)",
        (advance_id, amount, payment_date, request.form.get("notes", "").strip()),
    )
    _recalc_advance_total(advance_id)
    flash(f"Anticipo adicional registrado: S/ {amount:.2f}.", "success")
    return redirect(url_for("liquidaciones.detail", advance_id=advance_id))


@bp.route("/anticipos/<int:payment_id>/eliminar", methods=["POST"])
@permission_required("liquidaciones", "edit")
def delete_payment(payment_id):
    if not validate_csrf():
        abort(400)
    payment = query_one("SELECT * FROM advance_payments WHERE id = ?", (payment_id,))
    if payment is None:
        abort(404)
    advance = query_one("SELECT * FROM expense_advances WHERE id = ?", (payment["advance_id"],))
    if advance is None:
        abort(404)
    if advance["status"] == "LIQUIDADO":
        flash("Esta liquidación ya está cerrada.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance["id"]))
    count = query_one(
        "SELECT COUNT(*) c FROM advance_payments WHERE advance_id = ?", (advance["id"],)
    )["c"]
    if count <= 1:
        flash("Debe quedar al menos un anticipo registrado en la liquidación.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance["id"]))
    execute("DELETE FROM advance_payments WHERE id = ?", (payment_id,))
    _recalc_advance_total(advance["id"])
    flash("Anticipo eliminado.", "success")
    return redirect(url_for("liquidaciones.detail", advance_id=advance["id"]))


@bp.route("/<int:advance_id>")
@permission_required("liquidaciones", "view")
def detail(advance_id):
    advance = query_one(
        """SELECT a.*, t.code as trip_code, t.origin, t.destination, t.status as trip_status
           FROM expense_advances a JOIN trips t ON t.id = a.trip_id WHERE a.id = ?""",
        (advance_id,),
    )
    if advance is None:
        abort(404)
    expenses = query_all(
        "SELECT * FROM expenses WHERE trip_id = ? ORDER BY expense_date", (advance["trip_id"],)
    )
    payments = query_all(
        "SELECT * FROM advance_payments WHERE advance_id = ? ORDER BY payment_date, id", (advance_id,)
    )
    spent = sum(e["amount"] for e in expenses)
    difference = advance["amount_given"] - spent
    offices = office_choices()
    # 4 sep, pedido de Braulio: tabla de consumo de combustible de la ruta,
    # para comparar contra el combustible real que registre el liquidador.
    # route_id se fija al crear el anticipo (ver new_advance) — si por
    # alguna razón no quedó fijado (o la ruta se agregó al catálogo
    # después), se intenta encontrarla igual por origen/destino.
    route = None
    if advance["route_id"]:
        route = query_one("SELECT * FROM routes WHERE id = ?", (advance["route_id"],))
    if route is None:
        route = find_route(advance["origin"], advance["destination"])
    return render_template(
        "liquidaciones/detail.html", advance=advance, expenses=expenses, spent=spent, difference=difference,
        payments=payments, offices=offices, office_labels={code: info["label"] for code, info in offices},
        route=route, today=today_str(),
    )


@bp.route("/<int:advance_id>/combustible", methods=["POST"])
@permission_required("liquidaciones", "edit")
def save_fuel(advance_id):
    """4 sep, pedido de Braulio: registra el combustible real de este viaje
    contra la tabla de consumo estimado de la ruta (routes.default_fuel_amount).
    El "exceso" es un campo aparte para digitar — no se recalcula solo a
    partir de la diferencia, el liquidador lo confirma/ajusta — con su
    cuadro de observaciones al costado para justificarlo."""
    if not validate_csrf():
        abort(400)
    advance = query_one("SELECT * FROM expense_advances WHERE id = ?", (advance_id,))
    if advance is None:
        abort(404)
    if advance["status"] == "LIQUIDADO":
        flash("Esta liquidación ya está cerrada — no se puede editar el combustible.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance_id))

    fuel_actual = parse_float(request.form.get("fuel_actual"))
    fuel_excess = parse_float(request.form.get("fuel_excess"))
    fuel_notes = request.form.get("fuel_notes", "").strip()

    if fuel_actual < 0 or fuel_excess < 0:
        flash("El combustible real y el exceso no pueden ser negativos.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance_id))

    execute(
        "UPDATE expense_advances SET fuel_actual = ?, fuel_excess = ?, fuel_notes = ? WHERE id = ?",
        (fuel_actual or None, fuel_excess or None, fuel_notes or None, advance_id),
    )
    flash("Combustible registrado.", "success")
    return redirect(url_for("liquidaciones.detail", advance_id=advance_id))


def _next_voucher_number(office, month):
    """Correlativo de liquidación para esa oficina, reiniciando cada mes
    (pedido explícito de Braulio: "el Num.Voucher... se reinicia cada mes
    y empieza en 01")."""
    row = query_one(
        """SELECT COALESCE(MAX(voucher_number), 0) m FROM expense_advances
           WHERE office = ? AND strftime('%Y-%m', liquidated_at) = ?""",
        (office, month),
    )
    return (row["m"] or 0) + 1


@bp.route("/<int:advance_id>/liquidar", methods=["POST"])
@permission_required("liquidaciones", "edit")
def liquidate(advance_id):
    if not validate_csrf():
        abort(400)
    advance = query_one("SELECT * FROM expense_advances WHERE id = ?", (advance_id,))
    if advance is None:
        abort(404)
    if advance["status"] == "LIQUIDADO":
        flash("Esta liquidación ya está cerrada.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance_id))

    office = request.form.get("office")
    if office not in dict(office_choices()):
        flash("Selecciona la oficina donde se hace la liquidación.", "error")
        return redirect(url_for("liquidaciones.detail", advance_id=advance_id))

    # Asignación manual de gastos: el usuario marca, con checkboxes en el
    # detalle, cuáles de los gastos ya registrados para este viaje entran en
    # la liquidación (pedido explícito de Braulio, 27 ago — antes entraban
    # todos automático). Se guarda como el vínculo expenses.expense_advance_id.
    selected_ids = set(request.form.getlist("expense_ids", type=int))
    trip_expenses = query_all(
        "SELECT id, amount FROM expenses WHERE trip_id = ?", (advance["trip_id"],)
    )
    spent = 0.0
    for e in trip_expenses:
        included = e["id"] in selected_ids
        execute(
            "UPDATE expenses SET expense_advance_id = ? WHERE id = ?",
            (advance_id if included else None, e["id"]),
        )
        if included:
            spent += e["amount"]

    month = today_str()[:7]
    voucher_number = _next_voucher_number(office, month)

    execute(
        """UPDATE expense_advances SET status = 'LIQUIDADO', liquidated_at = datetime('now'),
           liquidated_expenses_total = ?, office = ?, voucher_number = ? WHERE id = ?""",
        (spent, office, voucher_number, advance_id),
    )
    flash("Liquidación cerrada.", "success")
    return redirect(url_for("liquidaciones.detail", advance_id=advance_id))


# --- Gastos individuales de un viaje (o de una unidad, sin viaje) ---

def _expense_concepts(only_active=True, exclude_vale=False):
    """Conceptos de gasto para el export de liquidación contable (ver
    app/accounting.py). `exclude_vale=True` quita los conceptos de tipo
    "PL" (vale/por liquidar, uno por oficina) — esos son de uso interno
    del sistema y no deben aparecer en el desplegable del formulario de
    gastos, solo se usan para armar la fila "Haber" al exportar."""
    sql = "SELECT * FROM expense_concepts WHERE 1=1"
    params = []
    if only_active:
        sql += " AND active = 1"
    if exclude_vale:
        sql += " AND document_type_code != ?"
        params.append(VALE_DOCUMENT_TYPE)
    sql += " ORDER BY sort_order, name"
    return query_all(sql, params)


def _fetch_exchange_rate(date_str):
    """Intenta obtener el tipo de cambio SUNAT para una fecha; nunca
    lanza — si falla, devuelve None y el campo queda para completar a
    mano (ver app/integrations/sunat_exchange_rate.py)."""
    try:
        rate = get_rate_for_date(
            date_str,
            base_url=current_app.config.get("DECOLECTA_BASE_URL") or None,
            token=current_app.config.get("DECOLECTA_TOKEN") or None,
        )
    except Exception:
        return None
    return rate["sell_rate"] if rate else None


@bp.route("/gastos/consultar-ruc")
@permission_required("liquidaciones", "edit")
def consultar_ruc():
    """Endpoint JSON que usa el formulario de gastos para autocompletar la
    razón social apenas se escribe un RUC de 11 dígitos (ver
    app/integrations/sunat_ruc.py). Nunca devuelve error 500: si el
    servicio externo falla o el RUC no existe, responde found=false y el
    campo se llena a mano — pedido de Braulio, 28 ago ("Si vamos a usar el
    mismo token de decolecta")."""
    ruc = request.args.get("ruc", "")
    try:
        company = get_company_for_ruc(
            ruc,
            base_url=current_app.config.get("DECOLECTA_RUC_BASE_URL") or None,
            token=current_app.config.get("DECOLECTA_TOKEN") or None,
        )
    except Exception:
        company = None
    if not company:
        return jsonify({"found": False})
    return jsonify({"found": True, "razon_social": company["razon_social"], "estado": company["estado"]})


def _expense_form_context(expense=None, preselected_trip=None):
    concepts = _expense_concepts(exclude_vale=True)
    # El viaje ya trae su propia unidad asignada (trips.vehicle_id, elegida
    # al crear el viaje) — se manda al formulario para que la Unidad se
    # auto-complete sola en vez de volver a preguntarla (pedido de Braulio,
    # 28 ago: "si el viaje ya tiene unidad, ¿para qué la vuelve a pedir?").
    # 4 sep: se excluyen los viajes con terceros — no registran liquidación
    # ni gastos de viaje (ver new_advance() más arriba).
    trips = query_all(
        "SELECT id, code, vehicle_id FROM trips WHERE status != 'CANCELADO' AND ownership != 'TERCERO' "
        "ORDER BY scheduled_date DESC"
    )
    preselected_trip_code = None
    preselected_vehicle_id = None
    preselected_vehicle_plate = None
    if preselected_trip:
        t = query_one(
            "SELECT t.code, t.vehicle_id, v.plate FROM trips t LEFT JOIN vehicles v ON v.id = t.vehicle_id "
            "WHERE t.id = ?",
            (preselected_trip,),
        )
        if t:
            preselected_trip_code = t["code"]
            preselected_vehicle_id = t["vehicle_id"]
            preselected_vehicle_plate = t["plate"]
    return {
        "trips": trips,
        "trips_json": [dict(t) for t in trips],
        "vehicles": query_all("SELECT id, plate FROM vehicles ORDER BY plate"),
        "concepts": concepts,
        "concepts_json": [dict(c) for c in concepts],
        "expense": expense,
        "preselected_trip": preselected_trip,
        "preselected_trip_code": preselected_trip_code,
        "preselected_vehicle_id": preselected_vehicle_id,
        "preselected_vehicle_plate": preselected_vehicle_plate,
        "today": today_str(),
    }


def _expense_locked(expense):
    """True si el gasto ya está vinculado a una liquidación cerrada — en
    ese caso no se debe poder eliminar (rompería el export contable ya
    cerrado de esa liquidación)."""
    if not expense or not expense["expense_advance_id"]:
        return False
    advance = query_one("SELECT status FROM expense_advances WHERE id = ?", (expense["expense_advance_id"],))
    return bool(advance and advance["status"] == "LIQUIDADO")


@bp.route("/gastos/nuevo", methods=["GET", "POST"])
@permission_required("liquidaciones", "edit")
def new_expense():
    ctx = _expense_form_context(preselected_trip=request.args.get("trip_id", type=int))

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount"))
        expense_date = parse_date(request.form.get("expense_date")) or today_str()
        # El campo "Tipo" se retiró (28 ago, pedido de Braulio: se estaba
        # pidiendo la misma clasificación dos veces). Ahora el Concepto es la
        # única clasificación del gasto, y expenses.type (columna NOT NULL,
        # usada también para agrupar en Presupuestos e Historial) se completa
        # solo con el nombre del concepto elegido.
        concept_id = request.form.get("concept_id") or None
        concept = query_one("SELECT * FROM expense_concepts WHERE id = ?", (concept_id,)) if concept_id else None
        # Si el gasto viene de una liquidación específica (link "+ Registrar
        # gasto para este viaje"), el viaje llega fijo por un campo oculto —
        # no se vuelve a preguntar (pedido de Braulio, 28 ago).
        trip_id = (request.form.get("trip_id") or "").strip() or None
        vehicle_id = request.form.get("vehicle_id") or None

        errors = []
        if not concept:
            errors.append("Selecciona un concepto de gasto válido.")
        if amount <= 0:
            errors.append("El monto debe ser mayor a cero.")
        if not trip_id and not vehicle_id:
            errors.append("Asocia el gasto a un viaje o a una unidad.")
        # 4 sep, pedido de Braulio: los viajes con terceros no registran
        # gastos de viaje — chequeo en el servidor, el desplegable de
        # _expense_form_context() ya los excluye pero esto cubre un
        # trip_id mandado a mano (ej. por el campo oculto de "+ Registrar
        # gasto para este viaje", que ahora tampoco debería mostrarse para
        # un viaje TERCERO — ver viajes/detail.html).
        if trip_id:
            trip_for_expense = query_one("SELECT ownership FROM trips WHERE id = ?", (trip_id,))
            if trip_for_expense and trip_for_expense["ownership"] == "TERCERO":
                errors.append("Los viajes con terceros no registran gastos de viaje.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("liquidaciones/expense_form.html", **{**ctx, "expense": request.form})

        expense_type = concept["name"]
        receipt_filename = _save_receipt(_first_uploaded_file())

        # El tipo de cambio se completa solo con el de SUNAT del día del
        # comprobante (Braulio: "el de sunat del dia de emision"), salvo
        # que el usuario haya escrito uno manualmente (por ejemplo porque
        # el servicio no respondió) — ver plantilla del formulario.
        manual_rate = request.form.get("exchange_rate", "").strip()
        exchange_rate = parse_float(manual_rate, None) if manual_rate else _fetch_exchange_rate(expense_date)
        if exchange_rate is None and not manual_rate:
            flash(
                "No se pudo obtener el tipo de cambio SUNAT automáticamente para esa fecha — "
                "puedes completarlo a mano si lo necesitas para la liquidación.",
                "info",
            )

        execute(
            """INSERT INTO expenses (trip_id, vehicle_id, type, amount, expense_date, description,
               receipt_filename, concept_id, document_number, due_date, provider_ruc, provider_name,
               currency, exchange_rate, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trip_id, vehicle_id, expense_type, amount, expense_date,
                request.form.get("description", "").strip(), receipt_filename, concept_id,
                request.form.get("document_number", "").strip() or None,
                expense_date,  # Fec.Ven = misma fecha de emisión (pedido explícito de Braulio)
                request.form.get("provider_ruc", "").strip() or None,
                request.form.get("provider_name", "").strip() or None,
                request.form.get("currency", DEFAULT_CURRENCY) or DEFAULT_CURRENCY,
                exchange_rate, None,
            ),
        )
        flash("Gasto registrado. Recuerda incluirlo en la liquidación del viaje cuando la cierres.", "success")
        if trip_id:
            advance = query_one("SELECT id FROM expense_advances WHERE trip_id = ?", (trip_id,))
            if advance:
                return redirect(url_for("liquidaciones.detail", advance_id=advance["id"]))
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        return redirect(url_for("liquidaciones.historial"))

    return render_template("liquidaciones/expense_form.html", **ctx)


@bp.route("/gastos/<int:expense_id>/editar", methods=["GET", "POST"])
@permission_required("liquidaciones", "edit")
def edit_expense(expense_id):
    expense = query_one("SELECT * FROM expenses WHERE id = ?", (expense_id,))
    if expense is None:
        abort(404)
    ctx = _expense_form_context(expense=expense)
    ctx["locked"] = _expense_locked(expense)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount"))
        expense_date = parse_date(request.form.get("expense_date")) or today_str()
        concept_id = request.form.get("concept_id") or None
        concept = query_one("SELECT * FROM expense_concepts WHERE id = ?", (concept_id,)) if concept_id else None
        trip_id = request.form.get("trip_id") or None
        vehicle_id = request.form.get("vehicle_id") or None

        errors = []
        if not concept:
            errors.append("Selecciona un concepto de gasto válido.")
        if amount <= 0:
            errors.append("El monto debe ser mayor a cero.")
        if not trip_id and not vehicle_id:
            errors.append("Asocia el gasto a un viaje o a una unidad.")
        # 4 sep, pedido de Braulio: ver el mismo chequeo en new_expense().
        if trip_id:
            trip_for_expense = query_one("SELECT ownership FROM trips WHERE id = ?", (trip_id,))
            if trip_for_expense and trip_for_expense["ownership"] == "TERCERO":
                errors.append("Los viajes con terceros no registran gastos de viaje.")

        if errors:
            for e in errors:
                flash(e, "error")
            merged = dict(request.form)
            merged["id"] = expense_id
            return render_template("liquidaciones/expense_form.html", **{**ctx, "expense": merged})

        expense_type = concept["name"]
        new_receipt = _save_receipt(_first_uploaded_file())
        receipt_filename = new_receipt or expense["receipt_filename"]

        manual_rate = request.form.get("exchange_rate", "").strip()
        if manual_rate:
            exchange_rate = parse_float(manual_rate, None)
        elif expense_date != expense["expense_date"] or expense["exchange_rate"] is None:
            exchange_rate = _fetch_exchange_rate(expense_date)
        else:
            exchange_rate = expense["exchange_rate"]

        execute(
            """UPDATE expenses SET trip_id = ?, vehicle_id = ?, type = ?, amount = ?, expense_date = ?,
               description = ?, receipt_filename = ?, concept_id = ?, document_number = ?, due_date = ?,
               provider_ruc = ?, provider_name = ?, currency = ?, exchange_rate = ? WHERE id = ?""",
            (
                trip_id, vehicle_id, expense_type, amount, expense_date,
                request.form.get("description", "").strip(), receipt_filename, concept_id,
                request.form.get("document_number", "").strip() or None,
                expense_date,
                request.form.get("provider_ruc", "").strip() or None,
                request.form.get("provider_name", "").strip() or None,
                request.form.get("currency", DEFAULT_CURRENCY) or DEFAULT_CURRENCY,
                exchange_rate, expense_id,
            ),
        )
        flash("Gasto actualizado.", "success")
        if trip_id:
            advance = query_one("SELECT id FROM expense_advances WHERE trip_id = ?", (trip_id,))
            if advance:
                return redirect(url_for("liquidaciones.detail", advance_id=advance["id"]))
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        return redirect(url_for("liquidaciones.historial"))

    return render_template("liquidaciones/expense_form.html", **ctx)


@bp.route("/gastos/<int:expense_id>/eliminar", methods=["POST"])
@permission_required("liquidaciones", "edit")
def delete_expense(expense_id):
    if not validate_csrf():
        abort(400)
    expense = query_one("SELECT * FROM expenses WHERE id = ?", (expense_id,))
    if expense is None:
        abort(404)
    if _expense_locked(expense):
        flash("Este gasto ya forma parte de una liquidación cerrada — no se puede eliminar.", "error")
        return redirect(request.referrer or url_for("liquidaciones.historial"))
    execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    flash("Gasto eliminado.", "success")
    return redirect(request.referrer or url_for("liquidaciones.historial"))


@bp.route("/gastos/<int:expense_id>/comprobante")
@permission_required("liquidaciones", "view")
def receipt(expense_id):
    expense = query_one("SELECT receipt_filename FROM expenses WHERE id = ?", (expense_id,))
    if expense is None or not expense["receipt_filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.receipt_url(expense["receipt_filename"]))
    return send_from_directory(storage.local_receipts_dir(), expense["receipt_filename"])


# --- Borradores de gasto desde WhatsApp (vía n8n) -----------------------
#
# Flujo (1 sep, pedido de Braulio: "quiero usar n8n para integrar Whatsapp
# a la plataforma... que tomando una foto a la factura se llene
# automaticamente los campos de datos de proveedor, monto entre otros"):
# un workflow de n8n (fuera de este repositorio — ver
# n8n/whatsapp-factura-intake.json) recibe la foto por WhatsApp Business
# API, la manda a una IA con visión para extraer los datos, y llama a
# whatsapp_intake() con esos datos + la imagen. Eso crea un borrador
# PENDIENTE (whatsapp_expense_drafts) — nunca un gasto real directo, porque
# la IA puede equivocarse en el monto o el RUC y esto es dinero real
# (confirmado con Braulio). whatsapp_review() es donde un humano revisa,
# corrige si hace falta, y recién ahí aprueba (crea la fila real en
# `expenses`, reusando el mismo INSERT que new_expense()) o rechaza.

def _n8n_token_valid():
    """Compara el token de la cabecera "X-Webhook-Token" contra
    N8N_WEBHOOK_TOKEN con comparación de tiempo constante (evita timing
    attacks). Si el servidor no tiene el token configurado, siempre
    devuelve False — el endpoint nunca queda abierto sin querer solo
    porque alguien olvidó configurar la variable de entorno."""
    configured = (current_app.config.get("N8N_WEBHOOK_TOKEN") or "").strip()
    if not configured:
        return False
    provided = request.headers.get("X-Webhook-Token", "")
    return secrets.compare_digest(configured, provided)


@bp.route("/whatsapp/intake", methods=["POST"])
def whatsapp_intake():
    """Endpoint que llama el workflow de n8n después de extraer los datos
    de una foto de factura recibida por WhatsApp. No usa sesión/login ni
    el csrf_token normal (quien llama es un servicio externo, no un
    navegador con la sesión de un usuario) — se autentica con
    N8N_WEBHOOK_TOKEN (ver _n8n_token_valid). Responde JSON siempre, nunca
    una página HTML ni una redirección, para que n8n pueda leer el
    resultado y decidir si reintentar o no.

    Acepta el body de dos formas:
    - JSON con la imagen en base64 (`image_base64` + `image_filename`) —
      la forma que usa el workflow de n8n, porque mandar JSON es más simple
      y confiable desde ahí que armar un body multipart/form-data.
    - multipart/form-data con un archivo `image` — se mantiene por si algún
      día conviene llamarlo directo con un form (ej. para pruebas manuales
      con curl -F), pero n8n no la necesita."""
    if not _n8n_token_valid():
        return jsonify({"ok": False, "error": "token inválido o no configurado"}), 401

    if request.is_json:
        data = request.get_json(silent=True) or {}
        image_b64 = data.get("image_base64") or ""
        # Por si mandan un data URL completo (data:image/jpeg;base64,xxxx)
        # en vez de solo el base64.
        if "," in image_b64 and image_b64.strip().lower().startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]
        try:
            image_bytes = base64.b64decode(image_b64, validate=False) if image_b64 else b""
        except Exception:
            return jsonify({"ok": False, "error": "image_base64 no es base64 válido"}), 400
        image_name = data.get("image_filename") or "foto.jpg"
        image_mimetype = data.get("image_mimetype") or None
        wa_message_id = (data.get("wa_message_id") or "").strip() or None
        phone = (data.get("phone") or "").strip() or None
        provider_ruc = (data.get("provider_ruc") or "").strip() or None
        provider_name = (data.get("provider_name") or "").strip() or None
        amount = parse_float(data.get("amount"), None)
        currency = (data.get("currency") or "").strip() or None
        document_number = (data.get("document_number") or "").strip() or None
        document_date = parse_date(data.get("document_date") or "")
        raw_extraction = data.get("raw_extraction") or None
        caption = (data.get("caption") or "").strip() or None
        if not image_bytes:
            return jsonify({"ok": False, "error": "falta 'image_base64'"}), 400
    else:
        image = request.files.get("image")
        if not image or not image.filename:
            return jsonify({"ok": False, "error": "falta el archivo 'image'"}), 400
        image_bytes = image.read()
        image_name = image.filename
        image_mimetype = image.mimetype
        wa_message_id = (request.form.get("wa_message_id") or "").strip() or None
        phone = (request.form.get("phone") or "").strip() or None
        provider_ruc = (request.form.get("provider_ruc") or "").strip() or None
        provider_name = (request.form.get("provider_name") or "").strip() or None
        amount = parse_float(request.form.get("amount"), None)
        currency = (request.form.get("currency") or "").strip() or None
        document_number = (request.form.get("document_number") or "").strip() or None
        document_date = parse_date(request.form.get("document_date") or "")
        raw_extraction = request.form.get("raw_extraction") or None
        caption = (request.form.get("caption") or "").strip() or None

    # Si n8n manda el id del mensaje de WhatsApp y ya se procesó antes (por
    # ejemplo, reintentó la llamada tras un timeout de red), se devuelve el
    # borrador ya creado en vez de duplicarlo.
    if wa_message_id:
        existing = query_one(
            "SELECT id, status FROM whatsapp_expense_drafts WHERE source_wa_message_id = ?",
            (wa_message_id,),
        )
        if existing:
            return jsonify({
                "ok": True, "draft_id": existing["id"], "status": existing["status"], "duplicate": True,
            })

    image_filename = _save_receipt_bytes(image_bytes, image_name, image_mimetype)
    if not image_filename:
        return jsonify({"ok": False, "error": "no se pudo leer la imagen (formato no soportado o archivo vacío)"}), 400

    draft_id = execute(
        """INSERT INTO whatsapp_expense_drafts
           (source_phone, source_wa_message_id, image_filename, extracted_provider_ruc,
            extracted_provider_name, extracted_amount, extracted_currency, extracted_document_number,
            extracted_document_date, ai_raw_response, caption)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            phone, wa_message_id, image_filename, provider_ruc, provider_name,
            amount, currency, document_number, document_date, raw_extraction, caption,
        ),
    )
    return jsonify({"ok": True, "draft_id": draft_id, "status": "PENDIENTE"})


@bp.route("/whatsapp")
@permission_required("liquidaciones", "view")
def whatsapp_list():
    status_filter = request.args.get("status", "PENDIENTE")
    sql = "SELECT * FROM whatsapp_expense_drafts WHERE 1=1"
    params = []
    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
    sql += " ORDER BY created_at DESC, id DESC"
    drafts = query_all(sql, params)
    pending_count = query_one(
        "SELECT COUNT(*) n FROM whatsapp_expense_drafts WHERE status = 'PENDIENTE'"
    )["n"]
    return render_template(
        "liquidaciones/whatsapp_list.html", drafts=drafts, status_filter=status_filter, pending_count=pending_count,
    )


@bp.route("/whatsapp/<int:draft_id>/imagen")
@permission_required("liquidaciones", "view")
def whatsapp_draft_image(draft_id):
    draft = query_one("SELECT image_filename FROM whatsapp_expense_drafts WHERE id = ?", (draft_id,))
    if draft is None or not draft["image_filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.receipt_url(draft["image_filename"]))
    return send_from_directory(storage.local_receipts_dir(), draft["image_filename"])


def _whatsapp_review_context(draft):
    concepts = _expense_concepts(exclude_vale=True)
    # 4 sep: mismo criterio que _expense_form_context() — un gasto no puede
    # asociarse a un viaje con terceros.
    trips = query_all(
        "SELECT id, code, vehicle_id FROM trips WHERE status != 'CANCELADO' AND ownership != 'TERCERO' "
        "ORDER BY scheduled_date DESC"
    )
    return {
        "draft": draft,
        "trips": trips,
        "trips_json": [dict(t) for t in trips],
        "vehicles": query_all("SELECT id, plate FROM vehicles ORDER BY plate"),
        "concepts": concepts,
        "concepts_json": [dict(c) for c in concepts],
        "today": today_str(),
    }


@bp.route("/whatsapp/<int:draft_id>", methods=["GET", "POST"])
@permission_required("liquidaciones", "edit")
def whatsapp_review(draft_id):
    draft = query_one("SELECT * FROM whatsapp_expense_drafts WHERE id = ?", (draft_id,))
    if draft is None:
        abort(404)
    if draft["status"] != "PENDIENTE":
        flash("Este borrador ya fue revisado.", "info")
        return redirect(url_for("liquidaciones.whatsapp_list"))

    ctx = _whatsapp_review_context(draft)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount"))
        expense_date = parse_date(request.form.get("expense_date")) or today_str()
        concept_id = request.form.get("concept_id") or None
        concept = query_one("SELECT * FROM expense_concepts WHERE id = ?", (concept_id,)) if concept_id else None
        trip_id = request.form.get("trip_id") or None
        vehicle_id = request.form.get("vehicle_id") or None

        errors = []
        if not concept:
            errors.append("Selecciona un concepto de gasto válido.")
        if amount <= 0:
            errors.append("El monto debe ser mayor a cero.")
        if not trip_id and not vehicle_id:
            errors.append("Asocia el gasto a un viaje o a una unidad.")
        # 4 sep, pedido de Braulio: ver el mismo chequeo en new_expense().
        if trip_id:
            trip_for_expense = query_one("SELECT ownership FROM trips WHERE id = ?", (trip_id,))
            if trip_for_expense and trip_for_expense["ownership"] == "TERCERO":
                errors.append("Los viajes con terceros no registran gastos de viaje.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("liquidaciones/whatsapp_review.html", **{**ctx, "form": request.form})

        expense_type = concept["name"]
        manual_rate = request.form.get("exchange_rate", "").strip()
        exchange_rate = parse_float(manual_rate, None) if manual_rate else _fetch_exchange_rate(expense_date)

        # El comprobante del gasto real es la misma foto que ya se guardó al
        # recibir el borrador — no hace falta volver a subirla ni duplicarla.
        expense_id = execute(
            """INSERT INTO expenses (trip_id, vehicle_id, type, amount, expense_date, description,
               receipt_filename, concept_id, document_number, due_date, provider_ruc, provider_name,
               currency, exchange_rate, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trip_id, vehicle_id, expense_type, amount, expense_date,
                request.form.get("description", "").strip(), draft["image_filename"], concept_id,
                request.form.get("document_number", "").strip() or None,
                expense_date,
                request.form.get("provider_ruc", "").strip() or None,
                request.form.get("provider_name", "").strip() or None,
                request.form.get("currency", DEFAULT_CURRENCY) or DEFAULT_CURRENCY,
                exchange_rate, g.user["id"],
            ),
        )
        execute(
            """UPDATE whatsapp_expense_drafts SET status = 'APROBADO', reviewed_by = ?, reviewed_at = ?,
               resulting_expense_id = ? WHERE id = ?""",
            (g.user["id"], now_str(), expense_id, draft_id),
        )
        flash("Gasto registrado a partir de la foto recibida por WhatsApp.", "success")
        if trip_id:
            advance = query_one("SELECT id FROM expense_advances WHERE trip_id = ?", (trip_id,))
            if advance:
                return redirect(url_for("liquidaciones.detail", advance_id=advance["id"]))
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        return redirect(url_for("liquidaciones.whatsapp_list"))

    return render_template("liquidaciones/whatsapp_review.html", **ctx)


@bp.route("/whatsapp/<int:draft_id>/rechazar", methods=["POST"])
@permission_required("liquidaciones", "edit")
def whatsapp_reject(draft_id):
    if not validate_csrf():
        abort(400)
    draft = query_one("SELECT id, status FROM whatsapp_expense_drafts WHERE id = ?", (draft_id,))
    if draft is None:
        abort(404)
    if draft["status"] != "PENDIENTE":
        flash("Este borrador ya fue revisado.", "info")
        return redirect(url_for("liquidaciones.whatsapp_list"))
    execute(
        """UPDATE whatsapp_expense_drafts SET status = 'RECHAZADO', reviewed_by = ?, reviewed_at = ?,
           rejection_reason = ? WHERE id = ?""",
        (g.user["id"], now_str(), request.form.get("rejection_reason", "").strip() or None, draft_id),
    )
    flash("Borrador descartado.", "success")
    return redirect(url_for("liquidaciones.whatsapp_list"))


# --- Historial de gastos (lista plana filtrable, para gastos sueltos de
# unidad o para revisar/exportar todo junto) y su export general a Excel ---

def _filtered_expenses(args):
    """Aplica los filtros de tipo y rango de fechas usados por el historial y su exportación."""
    type_filter = args.get("type", "")
    from_date = parse_date(args.get("from_date", ""))
    to_date = parse_date(args.get("to_date", ""))

    sql = """SELECT e.*, t.code as trip_code, v.plate as vehicle_plate
              FROM expenses e
              LEFT JOIN trips t ON t.id = e.trip_id
              LEFT JOIN vehicles v ON v.id = e.vehicle_id
              WHERE 1=1"""
    params = []
    if type_filter:
        sql += " AND e.type = ?"
        params.append(type_filter)
    if from_date:
        sql += " AND e.expense_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND e.expense_date <= ?"
        params.append(to_date)
    sql += " ORDER BY e.expense_date DESC, e.id DESC"
    expenses = query_all(sql, params)
    return expenses, type_filter, from_date, to_date


@bp.route("/historial")
@permission_required("liquidaciones", "view")
def historial():
    expenses, type_filter, from_date, to_date = _filtered_expenses(request.args)
    total = sum(e["amount"] for e in expenses)
    types = [c["name"] for c in _expense_concepts(exclude_vale=True)]
    return render_template(
        "liquidaciones/historial.html", expenses=expenses, types=types, type_filter=type_filter,
        from_date=from_date or "", to_date=to_date or "", total=total,
    )


@bp.route("/historial/exportar")
@permission_required("liquidaciones", "view")
def export_excel():
    expenses, type_filter, from_date, to_date = _filtered_expenses(request.args)

    parts = []
    parts.append(f"Tipo: {pretty_label(type_filter)}" if type_filter else "Todos los tipos")
    if from_date or to_date:
        parts.append(f"Periodo: {from_date or 'inicio'} a {to_date or 'hoy'}")
    else:
        parts.append("Todas las fechas")
    filter_description = "  ·  ".join(parts)

    buffer = build_expenses_workbook(
        expenses,
        company_name=current_app.config["COMPANY_NAME"],
        filter_description=filter_description,
        known_type_order=[c["name"] for c in _expense_concepts(only_active=False, exclude_vale=True)],
    )
    filename = f"gastos_{today_str()}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Presupuestos de gasto ---

@bp.route("/presupuestos")
@permission_required("liquidaciones", "edit")
def budgets_list():
    budgets = query_all("SELECT * FROM expense_budgets ORDER BY scope_type, scope_value")
    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    types = [c["name"] for c in _expense_concepts(only_active=False, exclude_vale=True)]
    vehicle_plates = {v["id"]: v["plate"] for v in vehicles}
    return render_template(
        "liquidaciones/budgets.html", budgets=budgets, vehicles=vehicles, types=types, vehicle_plates=vehicle_plates,
    )


@bp.route("/presupuestos/agregar", methods=["POST"])
@permission_required("liquidaciones", "edit")
def budgets_add():
    if not validate_csrf():
        abort(400)
    scope_type = request.form.get("scope_type")
    scope_value = request.form.get("scope_value", "").strip()
    amount = parse_float(request.form.get("monthly_amount"), 0)
    if scope_type not in ("VEHICLE", "TYPE") or not scope_value or amount <= 0:
        flash("Completa unidad/tipo y un monto mensual válido.", "error")
        return redirect(url_for("liquidaciones.budgets_list"))

    existing = query_one(
        "SELECT id FROM expense_budgets WHERE scope_type = ? AND scope_value = ?", (scope_type, scope_value)
    )
    if existing:
        execute(
            "UPDATE expense_budgets SET monthly_amount = ?, active = 1 WHERE id = ?",
            (amount, existing["id"]),
        )
        flash("Presupuesto actualizado.", "success")
    else:
        execute(
            "INSERT INTO expense_budgets (scope_type, scope_value, monthly_amount) VALUES (?, ?, ?)",
            (scope_type, scope_value, amount),
        )
        flash("Presupuesto agregado.", "success")
    return redirect(url_for("liquidaciones.budgets_list"))


@bp.route("/presupuestos/<int:budget_id>/alternar", methods=["POST"])
@permission_required("liquidaciones", "edit")
def budgets_toggle(budget_id):
    if not validate_csrf():
        abort(400)
    budget = query_one("SELECT * FROM expense_budgets WHERE id = ?", (budget_id,))
    if budget is None:
        abort(404)
    execute("UPDATE expense_budgets SET active = ? WHERE id = ?", (0 if budget["active"] else 1, budget_id))
    flash("Actualizado." if budget["active"] else "Reactivado.", "success")
    return redirect(url_for("liquidaciones.budgets_list"))


# --- Conceptos de gasto (cuenta contable / tipo de comprobante para el
# export de liquidación) — administrado solo por Administrador, igual que
# Catálogos, porque define códigos contables. ---

@bp.route("/conceptos")
@permission_required("catalogos", "edit")
def concepts_list():
    concepts = query_all("SELECT * FROM expense_concepts ORDER BY sort_order, name")
    return render_template(
        "liquidaciones/conceptos.html", concepts=concepts, document_types=DOCUMENT_TYPES, offices=office_choices(),
    )


@bp.route("/conceptos/agregar", methods=["POST"])
@permission_required("catalogos", "edit")
def concepts_add():
    if not validate_csrf():
        abort(400)
    name = request.form.get("name", "").strip().upper()
    account_code = request.form.get("account_code", "").strip()
    voucher_type_label = request.form.get("voucher_type_label", "").strip()
    document_type_code = request.form.get("document_type_code", "").strip()

    if not name or not account_code or not voucher_type_label or not document_type_code:
        flash("Completa nombre, cuenta contable, tipo de comprobante y tipo de documento.", "error")
        return redirect(url_for("liquidaciones.concepts_list"))

    max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM expense_concepts")["m"]
    execute(
        """INSERT INTO expense_concepts (name, account_code, voucher_type_label, document_type_code, sort_order)
           VALUES (?, ?, ?, ?, ?)""",
        (name, account_code, voucher_type_label, document_type_code, max_order + 1),
    )
    flash(f'Concepto "{name}" agregado.', "success")
    return redirect(url_for("liquidaciones.concepts_list"))


@bp.route("/conceptos/<int:concept_id>/alternar", methods=["POST"])
@permission_required("catalogos", "edit")
def concepts_toggle(concept_id):
    if not validate_csrf():
        abort(400)
    concept = query_one("SELECT * FROM expense_concepts WHERE id = ?", (concept_id,))
    if concept is None:
        abort(404)
    execute("UPDATE expense_concepts SET active = ? WHERE id = ?", (0 if concept["active"] else 1, concept_id))
    flash("Actualizado." if concept["active"] else "Reactivado.", "success")
    return redirect(url_for("liquidaciones.concepts_list"))


# --- Resumen contable exportable: junta cada liquidación (viaje) cerrada
# con los gastos que se le asignaron, en el formato EXACTO de la "hoja
# resumen" de la plantilla real de Harraso (ver app/accounting.py).
# Pensado para pegarse directo en su sistema contable. ---

def _liquidacion_rows(month, office_filter):
    """Arma las filas Haber (vale) + Debe (gastos documentados) de cada
    liquidación cerrada en el mes/oficina pedidos, en el formato de columnas
    que espera build_liquidacion_workbook."""
    sql = """SELECT a.*, t.code as trip_code, d.name as driver_name, d.document_number as driver_dni
             FROM expense_advances a
             JOIN trips t ON t.id = a.trip_id
             LEFT JOIN drivers d ON d.id = t.driver_id
             WHERE a.status = 'LIQUIDADO' AND strftime('%Y-%m', a.liquidated_at) = ?"""
    params = [month]
    if office_filter:
        sql += " AND a.office = ?"
        params.append(office_filter)
    sql += " ORDER BY a.office, a.voucher_number"
    advances = query_all(sql, params)

    rows = []
    for a in advances:
        info = office_info(a["office"]) or {}
        origen = info.get("origen_code", "")
        num_voucher = voucher_label(a["voucher_number"])
        fecha_liq = (a["liquidated_at"] or "")[:10]
        tipo_cambio_vale = _fetch_exchange_rate(a["given_date"])

        rows.append({
            "origen": origen, "num_voucher": num_voucher, "fecha_liquidacion": fecha_liq,
            "cuenta": info.get("cuenta_vale", ""), "monto_debe": None, "monto_haber": a["amount_given"],
            "moneda": DEFAULT_CURRENCY, "tipo_cambio": tipo_cambio_vale, "doc": VALE_DOCUMENT_TYPE,
            "num_doc": f"AV-{a['id']}", "fec_doc": a["given_date"], "fec_ven": a["given_date"],
            "ruc_dni": a["driver_dni"], "glosa": "DOCUMENTO POR LIQUIDAR",
            "ruc_dni2": a["driver_dni"], "razon_social": a["driver_name"],
        })

        expenses = query_all(
            """SELECT e.*, c.name as concept_name, c.account_code, c.document_type_code
               FROM expenses e LEFT JOIN expense_concepts c ON c.id = e.concept_id
               WHERE e.expense_advance_id = ? ORDER BY e.expense_date""",
            (a["id"],),
        )
        for e in expenses:
            rows.append({
                "origen": origen, "num_voucher": num_voucher, "fecha_liquidacion": fecha_liq,
                "cuenta": e["account_code"] or "", "monto_debe": e["amount"], "monto_haber": None,
                "moneda": e["currency"] or DEFAULT_CURRENCY, "tipo_cambio": e["exchange_rate"],
                "doc": e["document_type_code"] or "", "num_doc": e["document_number"],
                "fec_doc": e["expense_date"], "fec_ven": e["due_date"] or e["expense_date"],
                "ruc_dni": e["provider_ruc"], "glosa": e["concept_name"] or pretty_label(e["type"]),
                "ruc_dni2": e["provider_ruc"], "razon_social": e["provider_name"],
            })
    return rows, advances


@bp.route("/resumen")
@permission_required("liquidaciones", "view")
def resumen_view():
    month = request.args.get("month") or today_str()[:7]
    office = request.args.get("office", "")
    rows, advances = _liquidacion_rows(month, office)
    total_debe = sum(r["monto_debe"] or 0 for r in rows)
    total_haber = sum(r["monto_haber"] or 0 for r in rows)
    return render_template(
        "liquidaciones/resumen.html", rows=rows, advances=advances, month=month, office=office,
        offices=office_choices(), total_debe=total_debe, total_haber=total_haber,
    )


@bp.route("/resumen/exportar")
@permission_required("liquidaciones", "view")
def resumen_export():
    month = request.args.get("month") or today_str()[:7]
    office = request.args.get("office", "")
    rows, _ = _liquidacion_rows(month, office)

    parts = [f"Mes: {month}"]
    parts.append(f"Oficina: {office_info(office)['label']}" if office else "Todas las oficinas")
    filter_description = "  ·  ".join(parts)

    buffer = build_liquidacion_workbook(
        rows, company_name=current_app.config["COMPANY_NAME"], filter_description=filter_description,
    )
    filename = f"liquidacion_{month}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
