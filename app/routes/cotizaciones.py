"""Módulo Cotizaciones (1 sep) — pedido de Braulio: adjuntó una cotización
real de Harraso ("COTIZACION 111 TPP HARRASO 6 MESES.pdf") y pidió un
módulo que arme el PDF con ese mismo formato: bloque de cliente, tabla de
ítems (Código/Cant./Descripción/Unidad Medida/V.Unit/Desc.U./P.Venta/
V.Venta), totales Gravado/Exonerado/Inafecto/IGV/Descuentos/Otros Cargos/
Importe Total, monto en letras ("SON: ..."), y datos bancarios para la
transferencia.

Decisión de alcance (AskUserQuestion, 1 sep, antes de construir):
independiente de Viajes/Facturación por ahora (no se "convierte" en nada,
solo genera el PDF); el "Código" de cada línea es texto libre (no un
catálogo de servicios); la numeración sigue la real de Harraso, arranca en
QUOTATION_START_NUMBER (112, la de referencia era la N° 111); el PDF lleva
el logo de Harraso + BRMS, igual que el resto de documentos del sistema.
"""
from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import amount_to_words_pen, parse_date, parse_float, today_str
from app.integrations.sunat_ruc import get_company_for_ruc

bp = Blueprint("cotizaciones", __name__, url_prefix="/cotizaciones")

# IGV peruano (18%, vigente desde hace años) — se aplica solo sobre las
# líneas marcadas como "Gravado". No se pidió que sea configurable; si
# alguna vez cambia a nivel país, se ajusta aquí.
IGV_RATE = 0.18


def _parse_issuer(form):
    """Empresa que emite la cotización — HARRASO o BRMS (1 sep, pedido de
    Braulio: "la cotizacion debes poder elegir entre Harraso o BRMS ... ya
    que son las 2"). Cualquier valor que no sea uno de los dos cae a
    HARRASO por seguridad, en vez de fallar con un 400 por un valor de
    formulario manipulado."""
    issuer = (form.get("issuer") or "").strip().upper()
    return issuer if issuer in ("HARRASO", "BRMS") else "HARRASO"


def _next_quotation_number():
    """Sigue la numeración real de Harraso (arranca en QUOTATION_START_NUMBER,
    112 por defecto — la última cotización real que Braulio mandó como
    referencia fue la N° 111) y de ahí en más simplemente sigue subiendo."""
    row = query_one("SELECT MAX(number) as n FROM quotations")
    current_max = row["n"] if row and row["n"] is not None else None
    start = current_app.config["QUOTATION_START_NUMBER"]
    if current_max is None:
        return start
    return max(current_max + 1, start)


def _calc_line(item):
    """Calcula P.Venta y V.Venta de una línea a partir de cantidad, precio
    unitario y descuento unitario — igual que las columnas del PDF de
    referencia (P. Venta = V.Unit - Desc.U.; V. Venta = Cant. x P.Venta)."""
    quantity = item["quantity"] or 0
    unit_price = item["unit_price"] or 0
    unit_discount = item["unit_discount"] or 0
    sale_price = unit_price - unit_discount
    line_total = quantity * sale_price
    return sale_price, line_total


def _calc_totals(items, discount_total, other_charges_total):
    """Arma el bloque de totales del PDF (Gravado/Exonerado/Inafecto/IGV/
    Descuentos/Otros Cargos/Importe Total) a partir de las líneas — nunca
    se confía en totales calculados en el navegador, se recalcula siempre
    en el servidor con estos mismos datos guardados."""
    gravado = exonerado = inafecto = 0.0
    for it in items:
        _, line_total = _calc_line(it)
        if it["tax_treatment"] == "EXONERADO":
            exonerado += line_total
        elif it["tax_treatment"] == "INAFECTO":
            inafecto += line_total
        else:
            gravado += line_total
    igv = round(gravado * IGV_RATE, 2)
    total = gravado + exonerado + inafecto + igv - (discount_total or 0) + (other_charges_total or 0)
    return {
        "gravado": gravado,
        "exonerado": exonerado,
        "inafecto": inafecto,
        "igv": igv,
        "descuentos": discount_total or 0,
        "otros_cargos": other_charges_total or 0,
        "total": total,
    }


def _parse_items_from_form(form):
    """Lee las líneas de la cotización desde el formulario (arrays
    paralelos, una posición por fila — mismo patrón que el JS de
    'agregar/quitar línea' del formulario). Ignora filas totalmente vacías
    (código y descripción vacíos) para tolerar filas que el usuario agregó
    y no llegó a usar."""
    codes = form.getlist("item_code")
    descriptions = form.getlist("item_description")
    units = form.getlist("item_unit")
    quantities = form.getlist("item_quantity")
    unit_prices = form.getlist("item_unit_price")
    unit_discounts = form.getlist("item_unit_discount")
    tax_treatments = form.getlist("item_tax_treatment")

    items = []
    for i in range(len(descriptions)):
        description = (descriptions[i] if i < len(descriptions) else "").strip()
        code = (codes[i] if i < len(codes) else "").strip()
        if not description and not code:
            continue
        if not description:
            return None, f"La línea {i + 1} tiene código pero no descripción."
        tax_treatment = tax_treatments[i] if i < len(tax_treatments) else "GRAVADO"
        if tax_treatment not in ("GRAVADO", "EXONERADO", "INAFECTO"):
            tax_treatment = "GRAVADO"
        items.append(
            {
                "code": code or None,
                "description": description,
                "unit": (units[i] if i < len(units) else "").strip() or None,
                "quantity": parse_float(quantities[i] if i < len(quantities) else "", 0),
                "unit_price": parse_float(unit_prices[i] if i < len(unit_prices) else "", 0),
                "unit_discount": parse_float(unit_discounts[i] if i < len(unit_discounts) else "", 0),
                "tax_treatment": tax_treatment,
            }
        )
    if not items:
        return None, "Agrega al menos una línea con descripción."
    return items, None


@bp.route("/consultar-ruc")
@permission_required("cotizaciones", "edit")
def consultar_ruc():
    """Endpoint JSON que usa el formulario de cotizaciones para autocompletar
    la razón social y dirección del cliente apenas se escribe un RUC de 11
    dígitos — mismo servicio y caché que ya usa Liquidaciones para el RUC
    del proveedor (app/integrations/sunat_ruc.py), pedido por Braulio el 1
    sep ("que jale los datos de sunat como en lo hace el modulo de
    gastos"). A diferencia del endpoint de Liquidaciones, este también
    devuelve la dirección — el formulario de Cotizaciones sí tiene un campo
    de dirección del cliente (el de gastos no). Nunca devuelve error 500: si
    el servicio externo falla o el RUC no existe, responde found=false y el
    cliente se completa a mano."""
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
    return jsonify({
        "found": True,
        "razon_social": company["razon_social"],
        "estado": company["estado"],
        "direccion": company["direccion"],
    })


@bp.route("")
@permission_required("cotizaciones", "view")
def list_view():
    status = request.args.get("status", "")
    sql = "SELECT * FROM quotations WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY number DESC"
    quotations = query_all(sql, params)
    totals_by_id = {}
    for q in quotations:
        items = query_all("SELECT * FROM quotation_items WHERE quotation_id = ?", (q["id"],))
        totals_by_id[q["id"]] = _calc_totals(items, q["discount_total"], q["other_charges_total"])
    return render_template(
        "cotizaciones/list.html", quotations=quotations, status=status, totals_by_id=totals_by_id
    )


@bp.route("/nueva", methods=["GET", "POST"])
@permission_required("cotizaciones", "edit")
def new():
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_id = request.form.get("client_id") or None
        client_name = request.form.get("client_name", "").strip()
        if not client_name:
            flash("Indica el nombre/razón social del cliente.", "error")
            return redirect(url_for("cotizaciones.new"))

        items, error = _parse_items_from_form(request.form)
        if error:
            flash(error, "error")
            return redirect(url_for("cotizaciones.new"))

        db = get_db()
        number = _next_quotation_number()
        cur = db.execute(
            """INSERT INTO quotations
               (number, client_id, client_name, client_ruc, client_address, issuer, issue_date, due_date,
                currency, payment_method, payment_condition, observation,
                discount_total, other_charges_total, created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                number,
                client_id,
                client_name,
                request.form.get("client_ruc", "").strip() or None,
                request.form.get("client_address", "").strip() or None,
                _parse_issuer(request.form),
                parse_date(request.form.get("issue_date")) or today_str(),
                parse_date(request.form.get("due_date")),
                request.form.get("currency", "SOLES").strip() or "SOLES",
                request.form.get("payment_method", "").strip() or None,
                request.form.get("payment_condition", "").strip() or None,
                request.form.get("observation", "").strip() or None,
                parse_float(request.form.get("discount_total"), 0),
                parse_float(request.form.get("other_charges_total"), 0),
                g.user["id"],
            ),
        )
        quotation_id = cur.lastrowid
        for idx, it in enumerate(items):
            db.execute(
                """INSERT INTO quotation_items
                   (quotation_id, sort_order, code, description, unit, quantity, unit_price,
                    unit_discount, tax_treatment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    quotation_id, idx, it["code"], it["description"], it["unit"], it["quantity"],
                    it["unit_price"], it["unit_discount"], it["tax_treatment"],
                ),
            )
        db.commit()
        flash(f"Cotización N° {number} creada.", "success")
        return redirect(url_for("cotizaciones.detail", quotation_id=quotation_id))

    return render_template(
        "cotizaciones/form.html", clients=clients, quotation=None, items=[], today=today_str()
    )


@bp.route("/<int:quotation_id>")
@permission_required("cotizaciones", "view")
def detail(quotation_id):
    quotation = query_one("SELECT * FROM quotations WHERE id = ?", (quotation_id,))
    if quotation is None:
        abort(404)
    items = [dict(it) for it in query_all(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (quotation_id,)
    )]
    for it in items:
        sale_price, line_total = _calc_line(it)
        it["sale_price"] = sale_price
        it["line_total"] = line_total
    totals = _calc_totals(items, quotation["discount_total"], quotation["other_charges_total"])
    return render_template(
        "cotizaciones/detail.html", quotation=quotation, items=items, totals=totals
    )


@bp.route("/<int:quotation_id>/editar", methods=["GET", "POST"])
@permission_required("cotizaciones", "edit")
def edit(quotation_id):
    quotation = query_one("SELECT * FROM quotations WHERE id = ?", (quotation_id,))
    if quotation is None:
        abort(404)
    if quotation["status"] != "BORRADOR":
        flash("Solo se puede editar una cotización mientras está en Borrador.", "error")
        return redirect(url_for("cotizaciones.detail", quotation_id=quotation_id))

    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_name = request.form.get("client_name", "").strip()
        if not client_name:
            flash("Indica el nombre/razón social del cliente.", "error")
            return redirect(url_for("cotizaciones.edit", quotation_id=quotation_id))

        items, error = _parse_items_from_form(request.form)
        if error:
            flash(error, "error")
            return redirect(url_for("cotizaciones.edit", quotation_id=quotation_id))

        db = get_db()
        db.execute(
            """UPDATE quotations SET client_id=?, client_name=?, client_ruc=?, client_address=?,
               issuer=?, issue_date=?, due_date=?, currency=?, payment_method=?, payment_condition=?,
               observation=?, discount_total=?, other_charges_total=? WHERE id=?""",
            (
                request.form.get("client_id") or None,
                client_name,
                request.form.get("client_ruc", "").strip() or None,
                request.form.get("client_address", "").strip() or None,
                _parse_issuer(request.form),
                parse_date(request.form.get("issue_date")) or today_str(),
                parse_date(request.form.get("due_date")),
                request.form.get("currency", "SOLES").strip() or "SOLES",
                request.form.get("payment_method", "").strip() or None,
                request.form.get("payment_condition", "").strip() or None,
                request.form.get("observation", "").strip() or None,
                parse_float(request.form.get("discount_total"), 0),
                parse_float(request.form.get("other_charges_total"), 0),
                quotation_id,
            ),
        )
        db.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (quotation_id,))
        for idx, it in enumerate(items):
            db.execute(
                """INSERT INTO quotation_items
                   (quotation_id, sort_order, code, description, unit, quantity, unit_price,
                    unit_discount, tax_treatment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    quotation_id, idx, it["code"], it["description"], it["unit"], it["quantity"],
                    it["unit_price"], it["unit_discount"], it["tax_treatment"],
                ),
            )
        db.commit()
        flash("Cotización actualizada.", "success")
        return redirect(url_for("cotizaciones.detail", quotation_id=quotation_id))

    items = query_all(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (quotation_id,)
    )
    return render_template(
        "cotizaciones/form.html", clients=clients, quotation=quotation, items=items, today=today_str()
    )


@bp.route("/<int:quotation_id>/eliminar", methods=["POST"])
@permission_required("cotizaciones", "edit")
def delete(quotation_id):
    if not validate_csrf():
        abort(400)
    quotation = query_one("SELECT * FROM quotations WHERE id = ?", (quotation_id,))
    if quotation is None:
        abort(404)
    if quotation["status"] != "BORRADOR":
        flash("Solo se puede eliminar una cotización mientras está en Borrador.", "error")
        return redirect(url_for("cotizaciones.detail", quotation_id=quotation_id))
    db = get_db()
    db.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (quotation_id,))
    db.execute("DELETE FROM quotations WHERE id = ?", (quotation_id,))
    db.commit()
    flash("Cotización eliminada.", "success")
    return redirect(url_for("cotizaciones.list_view"))


@bp.route("/<int:quotation_id>/estado", methods=["POST"])
@permission_required("cotizaciones", "edit")
def change_status(quotation_id):
    if not validate_csrf():
        abort(400)
    new_status = request.form.get("status")
    if new_status not in ("BORRADOR", "ENVIADA", "ACEPTADA", "RECHAZADA"):
        abort(400)
    quotation = query_one("SELECT * FROM quotations WHERE id = ?", (quotation_id,))
    if quotation is None:
        abort(404)
    execute("UPDATE quotations SET status = ? WHERE id = ?", (new_status, quotation_id))
    flash("Estado de la cotización actualizado.", "success")
    return redirect(url_for("cotizaciones.detail", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/pdf")
@permission_required("cotizaciones", "view")
def pdf(quotation_id):
    quotation = query_one("SELECT * FROM quotations WHERE id = ?", (quotation_id,))
    if quotation is None:
        abort(404)
    items = [dict(it) for it in query_all(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (quotation_id,)
    )]
    for it in items:
        sale_price, line_total = _calc_line(it)
        it["sale_price"] = sale_price
        it["line_total"] = line_total
    totals = _calc_totals(items, quotation["discount_total"], quotation["other_charges_total"])
    amount_words = amount_to_words_pen(totals["total"])
    cfg = current_app.config

    # Datos de la empresa emisora — Harraso o BRMS (1 sep, pedido de
    # Braulio: "la cotizacion debes poder elegir entre Harraso o BRMS ...
    # ya que son las 2"). El correo/teléfono se comparten entre ambas
    # (confirmado por Braulio); el RUC y la dirección de BRMS son propios
    # (BRMS_RUC/BRMS_ADDRESS, ver config.py — AJUSTAR si quedaron vacíos).
    # BRMS solo muestra una cuenta bancaria (BRMS_BANK_ACCOUNT), sin Banco
    # de la Nación ni cuenta de ahorro — a diferencia de Harraso, que
    # muestra las 3.
    if quotation["issuer"] == "BRMS":
        # Sin "S.A.C." fijo a propósito (a diferencia de Harraso, donde sí
        # se confirmó) — la razón social legal completa de BRMS no se
        # confirmó todavía. AJUSTAR en app/templates/cotizaciones/pdf.html
        # si hace falta agregarle el tipo societario.
        company_name = "BRMS"
        company_legal_suffix = ""
        company_ruc = cfg["BRMS_RUC"]
        company_address = cfg["BRMS_ADDRESS"]
        bank_accounts = [
            {"bank": "Banco de Crédito del Perú (BCP)", "label": "Cuenta en Soles", "account": cfg["BRMS_BANK_ACCOUNT"]},
        ]
    else:
        company_name = cfg["COMPANY_NAME"]
        company_legal_suffix = "S.A.C."
        company_ruc = cfg["COMPANY_RUC"]
        company_address = cfg["COMPANY_ADDRESS"]
        bank_accounts = [
            {"bank": "Banco de la Nación", "label": "Cuenta Detracción en Soles", "account": cfg["COMPANY_BANK_NACION_ACCOUNT"], "cci": cfg["COMPANY_BANK_NACION_CCI"]},
            {"bank": "Banco de Crédito del Perú", "label": "Cta Ahorro en Soles", "account": cfg["COMPANY_BANK_BCP_SAVINGS_ACCOUNT"], "cci": cfg["COMPANY_BANK_BCP_SAVINGS_CCI"]},
            {"bank": "Banco de Crédito del Perú", "label": "Cta Cte. en Soles", "account": cfg["COMPANY_BANK_BCP_CHECKING_ACCOUNT"]},
        ]

    # Se agrupa por banco ANTES de pasarlo a la plantilla (en vez de hacerlo
    # con {% set %} dentro de un {% for %} en Jinja, que no acumula estado
    # entre iteraciones) — así el PDF muestra el nombre del banco una sola
    # vez seguido de sus líneas, igual que el documento de referencia.
    bank_groups = []
    for acc in bank_accounts:
        if bank_groups and bank_groups[-1]["bank"] == acc["bank"]:
            bank_groups[-1]["lines"].append(acc)
        else:
            bank_groups.append({"bank": acc["bank"], "lines": [acc]})

    return render_template(
        "cotizaciones/pdf.html",
        quotation=quotation,
        items=items,
        totals=totals,
        amount_words=amount_words,
        company_name=company_name,
        company_legal_suffix=company_legal_suffix,
        company_ruc=company_ruc,
        company_address=company_address,
        company_email=cfg["COMPANY_EMAIL"],
        company_phone=cfg["COMPANY_PHONE"],
        bank_groups=bank_groups,
        generated_at=today_str(),
    )
