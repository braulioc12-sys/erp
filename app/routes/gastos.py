import io
import os
import uuid

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
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
from app.helpers import parse_date, parse_float, pretty_label, today_str
from app.integrations.sunat_exchange_rate import get_rate_for_date
from app.reports import build_expenses_workbook, build_liquidacion_workbook
from app.routes.catalogos import get_catalog

bp = Blueprint("gastos", __name__, url_prefix="/gastos")

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


def _receipts_dir():
    path = os.path.join(current_app.instance_path, "receipts")
    os.makedirs(path, exist_ok=True)
    return path


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


def _compress_receipt_image(raw_bytes, dest_path):
    """Redimensiona y recomprime una foto de comprobante como JPEG, para que
    ocupe el menor espacio posible en disco (importante porque el disco
    gratuito de Render es limitado). Devuelve True si logró comprimirla y
    guardarla; False si el archivo no se pudo abrir como imagen (por
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
            img.save(dest_path, format="JPEG", quality=RECEIPT_JPEG_QUALITY, optimize=True)
        return True
    except Exception:
        return False


def _save_receipt(file_storage):
    """Guarda el comprobante adjunto (foto o PDF) y devuelve el nombre de
    archivo guardado, o None si no se envió nada válido. Las fotos se
    redimensionan y recomprimen como JPEG para ocupar el menor espacio
    posible (ver _compress_receipt_image); los PDF se guardan tal cual.
    Nota: en hosting gratuito con disco efímero (ver README, sección
    Render) estos archivos se pierden al reiniciar/redesplegar, igual que
    la base de datos SQLite."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        ext = MIME_TO_EXTENSION.get((file_storage.mimetype or "").lower())
    if not ext:
        return None

    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None

    if ext == ".pdf":
        filename = f"{uuid.uuid4().hex}.pdf"
        with open(os.path.join(_receipts_dir(), filename), "wb") as f:
            f.write(raw_bytes)
        return filename

    # Es una foto: intentamos comprimirla. Siempre se guarda como .jpg
    # porque para fotos, JPEG comprime mucho mejor que PNG/WEBP/HEIC.
    filename = f"{uuid.uuid4().hex}.jpg"
    dest_path = os.path.join(_receipts_dir(), filename)
    if _compress_receipt_image(raw_bytes, dest_path):
        return filename

    # Si no se pudo abrir como imagen (formato raro o archivo corrupto),
    # guardamos el original sin comprimir para no perder el comprobante.
    filename = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_receipts_dir(), filename), "wb") as f:
        f.write(raw_bytes)
    return filename


def budget_alerts():
    """Compara el gasto acumulado del mes en curso contra los presupuestos
    activos (por unidad o por tipo de gasto) y devuelve los que ya se
    excedieron o están a un 90% o más de su límite."""
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


def _filtered_expenses(args):
    """Aplica los filtros de tipo y rango de fechas usados por la lista y la exportación."""
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


@bp.route("")
@permission_required("gastos", "view")
def list_view():
    expenses, type_filter, from_date, to_date = _filtered_expenses(request.args)
    total = sum(e["amount"] for e in expenses)
    types = [c["name"] for c in get_catalog("expense_type")]
    return render_template(
        "gastos/list.html", expenses=expenses, types=types, type_filter=type_filter,
        from_date=from_date or "", to_date=to_date or "", total=total,
    )


@bp.route("/exportar")
@permission_required("gastos", "view")
def export_excel():
    from flask import current_app

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
        known_type_order=[c["name"] for c in get_catalog("expense_type", only_active=False)],
    )
    filename = f"gastos_{today_str()}.xlsx"
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


def _expense_form_context(expense=None, preselected_trip=None):
    concepts = _expense_concepts(exclude_vale=True)
    return {
        "trips": query_all("SELECT id, code FROM trips WHERE status != 'CANCELADO' ORDER BY scheduled_date DESC"),
        "vehicles": query_all("SELECT id, plate FROM vehicles ORDER BY plate"),
        "types": [c["name"] for c in get_catalog("expense_type")],
        "concepts": concepts,
        "concepts_json": [dict(c) for c in concepts],
        "expense": expense,
        "preselected_trip": preselected_trip,
        "today": today_str(),
    }


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("gastos", "edit")
def new():
    ctx = _expense_form_context(preselected_trip=request.args.get("trip_id", type=int))

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount"))
        expense_date = parse_date(request.form.get("expense_date")) or today_str()
        expense_type = request.form.get("type")
        trip_id = request.form.get("trip_id") or None
        vehicle_id = request.form.get("vehicle_id") or None
        concept_id = request.form.get("concept_id") or None

        errors = []
        if expense_type not in ctx["types"]:
            errors.append("Selecciona un tipo de gasto válido.")
        if amount <= 0:
            errors.append("El monto debe ser mayor a cero.")
        if not trip_id and not vehicle_id:
            errors.append("Asocia el gasto a un viaje o a una unidad.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("gastos/form.html", **{**ctx, "expense": request.form})

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
        flash("Gasto registrado.", "success")
        if trip_id:
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        return redirect(url_for("gastos.list_view"))

    return render_template("gastos/form.html", **ctx)


@bp.route("/<int:expense_id>/editar", methods=["GET", "POST"])
@permission_required("gastos", "edit")
def edit(expense_id):
    expense = query_one("SELECT * FROM expenses WHERE id = ?", (expense_id,))
    if expense is None:
        abort(404)
    ctx = _expense_form_context(expense=expense)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        amount = parse_float(request.form.get("amount"))
        expense_date = parse_date(request.form.get("expense_date")) or today_str()
        expense_type = request.form.get("type")
        trip_id = request.form.get("trip_id") or None
        vehicle_id = request.form.get("vehicle_id") or None
        concept_id = request.form.get("concept_id") or None

        errors = []
        if expense_type not in ctx["types"]:
            errors.append("Selecciona un tipo de gasto válido.")
        if amount <= 0:
            errors.append("El monto debe ser mayor a cero.")
        if not trip_id and not vehicle_id:
            errors.append("Asocia el gasto a un viaje o a una unidad.")

        if errors:
            for e in errors:
                flash(e, "error")
            merged = dict(request.form)
            merged["id"] = expense_id
            return render_template("gastos/form.html", **{**ctx, "expense": merged})

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
            return redirect(url_for("viajes.detail", trip_id=trip_id))
        return redirect(url_for("gastos.list_view"))

    return render_template("gastos/form.html", **ctx)


@bp.route("/<int:expense_id>/eliminar", methods=["POST"])
@permission_required("gastos", "edit")
def delete(expense_id):
    if not validate_csrf():
        abort(400)
    execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    flash("Gasto eliminado.", "success")
    return redirect(url_for("gastos.list_view"))


@bp.route("/<int:expense_id>/comprobante")
@permission_required("gastos", "view")
def receipt(expense_id):
    expense = query_one("SELECT receipt_filename FROM expenses WHERE id = ?", (expense_id,))
    if expense is None or not expense["receipt_filename"]:
        abort(404)
    return send_from_directory(_receipts_dir(), expense["receipt_filename"])


# --- Presupuestos de gasto ---

@bp.route("/presupuestos")
@permission_required("gastos", "edit")
def budgets_list():
    budgets = query_all("SELECT * FROM expense_budgets ORDER BY scope_type, scope_value")
    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    types = [c["name"] for c in get_catalog("expense_type", only_active=False)]
    vehicle_plates = {v["id"]: v["plate"] for v in vehicles}
    return render_template(
        "gastos/budgets.html", budgets=budgets, vehicles=vehicles, types=types, vehicle_plates=vehicle_plates,
    )


@bp.route("/presupuestos/agregar", methods=["POST"])
@permission_required("gastos", "edit")
def budgets_add():
    if not validate_csrf():
        abort(400)
    scope_type = request.form.get("scope_type")
    scope_value = request.form.get("scope_value", "").strip()
    amount = parse_float(request.form.get("monthly_amount"), 0)
    if scope_type not in ("VEHICLE", "TYPE") or not scope_value or amount <= 0:
        flash("Completa unidad/tipo y un monto mensual válido.", "error")
        return redirect(url_for("gastos.budgets_list"))

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
    return redirect(url_for("gastos.budgets_list"))


@bp.route("/presupuestos/<int:budget_id>/alternar", methods=["POST"])
@permission_required("gastos", "edit")
def budgets_toggle(budget_id):
    if not validate_csrf():
        abort(400)
    budget = query_one("SELECT * FROM expense_budgets WHERE id = ?", (budget_id,))
    if budget is None:
        abort(404)
    execute("UPDATE expense_budgets SET active = ? WHERE id = ?", (0 if budget["active"] else 1, budget_id))
    flash("Actualizado." if budget["active"] else "Reactivado.", "success")
    return redirect(url_for("gastos.budgets_list"))


# --- Conceptos de gasto (cuenta contable / tipo de comprobante para el
# export de liquidación) — administrado solo por Administrador, igual que
# Catálogos, porque define códigos contables. ---

@bp.route("/conceptos")
@permission_required("catalogos", "edit")
def concepts_list():
    concepts = query_all("SELECT * FROM expense_concepts ORDER BY sort_order, name")
    return render_template(
        "gastos/conceptos.html", concepts=concepts, document_types=DOCUMENT_TYPES, offices=office_choices(),
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
        return redirect(url_for("gastos.concepts_list"))

    max_order = query_one("SELECT COALESCE(MAX(sort_order), -1) m FROM expense_concepts")["m"]
    execute(
        """INSERT INTO expense_concepts (name, account_code, voucher_type_label, document_type_code, sort_order)
           VALUES (?, ?, ?, ?, ?)""",
        (name, account_code, voucher_type_label, document_type_code, max_order + 1),
    )
    flash(f'Concepto "{name}" agregado.', "success")
    return redirect(url_for("gastos.concepts_list"))


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
    return redirect(url_for("gastos.concepts_list"))


# --- Liquidación contable exportable: junta cada anticipo de viáticos ya
# liquidado con los gastos documentados que se le vincularon, en el
# formato EXACTO de la "hoja resumen" de la plantilla real de Harraso
# (ver app/accounting.py). Pensado para pegarse directo en su sistema
# contable — por eso es un export aparte del reporte general de Gastos. ---

def _liquidacion_rows(month, office_filter):
    """Arma las filas Haber (vale) + Debe (gastos documentados) de cada
    anticipo liquidado en el mes/oficina pedidos, en el formato de columnas
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


@bp.route("/liquidacion")
@permission_required("gastos", "view")
def liquidacion_view():
    month = request.args.get("month") or today_str()[:7]
    office = request.args.get("office", "")
    rows, advances = _liquidacion_rows(month, office)
    total_debe = sum(r["monto_debe"] or 0 for r in rows)
    total_haber = sum(r["monto_haber"] or 0 for r in rows)
    return render_template(
        "gastos/liquidacion.html", rows=rows, advances=advances, month=month, office=office,
        offices=office_choices(), total_debe=total_debe, total_haber=total_haber,
    )


@bp.route("/liquidacion/exportar")
@permission_required("gastos", "view")
def liquidacion_export():
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
