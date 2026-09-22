import uuid

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import company_info_for_issuer, compute_detraction, next_code, parse_date, today_str
from app.integrations.sunat_ose import (
    SunatOseError,
    build_client_from_config,
    build_invoice_payload,
    is_duplicate_comprobante_error,
    parse_ose_response,
)
from app.storage import (
    local_sunat_documents_dir,
    save_sunat_document,
    sunat_document_url,
    using_s3,
)

bp = Blueprint("facturacion", __name__, url_prefix="/facturacion")


def _next_series_number(series):
    row = query_one("SELECT COUNT(*) as n FROM invoices WHERE series = ?", (series,))
    return (row["n"] if row else 0) + 1


@bp.route("")
@permission_required("facturacion", "view")
def list_view():
    status = request.args.get("status", "")
    sql = """SELECT i.*, c.name as client_name FROM invoices i
              JOIN clients c ON c.id = i.client_id WHERE 1=1"""
    params = []
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    invoices = query_all(sql, params)
    return render_template("facturacion/list.html", invoices=invoices, status=status)


def _collect_manual_items():
    """21 sep, pedido de Braulio ("aparte de facturar los viajes, tambien
    se puedan emitir facturas no relacionadas a viajes, como alquileres...
    de todo tipo"): filas libres (descripción + monto) que el propio
    formulario permite agregar/quitar con JS (ver facturacion/form.html,
    mismo patrón "+ Agregar línea" que ya usa Cotizaciones). Una fila se
    ignora en silencio si quedó vacía (usuario que le dio "+ Agregar línea"
    de más y no la llenó) -- solo se exige description Y amount > 0 cuando
    al menos uno de los dos campos de esa fila SÍ se completó, para poder
    avisar de una fila a medio llenar en vez de tragarla sin decir nada."""
    descriptions = request.form.getlist("item_description")
    amounts = request.form.getlist("item_amount")
    items = []
    incomplete = False
    for desc, amt_raw in zip(descriptions, amounts):
        desc = (desc or "").strip()
        amt_raw = (amt_raw or "").strip()
        if not desc and not amt_raw:
            continue
        try:
            amt = float(amt_raw)
        except ValueError:
            amt = 0
        if not desc or amt <= 0:
            incomplete = True
            continue
        items.append((desc, amt))
    return items, incomplete


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("facturacion", "edit")
def new():
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        client_id = request.form.get("client_id")
        trip_ids = request.form.getlist("trip_ids")
        issue_date = parse_date(request.form.get("issue_date")) or today_str()
        due_date = parse_date(request.form.get("due_date"))
        manual_items, manual_items_incomplete = _collect_manual_items()

        if not client_id:
            flash("Selecciona un cliente para facturar.", "error")
            return redirect(url_for("facturacion.new", client_id=client_id))
        if not trip_ids and not manual_items:
            flash(
                "Marca al menos un viaje o agrega al menos un ítem adicional (descripción y monto) para facturar.",
                "error",
            )
            return redirect(url_for("facturacion.new", client_id=client_id))
        if manual_items_incomplete:
            flash(
                "Hay un ítem adicional a medio llenar (le falta la descripción o el monto) — "
                "se ignoró esa línea. Revisa los ítems adicionales antes de guardar si no era esa tu intención.",
                "error",
            )

        trips = []
        if trip_ids:
            trips = query_all(
                f"""SELECT * FROM trips WHERE id IN ({','.join('?' * len(trip_ids))})
                    AND client_id = ? AND status = 'ENTREGADO' AND invoiced = 0""",
                (*trip_ids, client_id),
            )
            if not trips:
                flash("Los viajes seleccionados ya no están disponibles para facturar.", "error")
                return redirect(url_for("facturacion.new", client_id=client_id))

        # 7 sep, integración con tefacturo.pe: un comprobante electrónico se
        # emite a nombre de UN RUC, así que todos los viajes de una misma
        # factura deben ser de la misma empresa (Harraso o BRMS, ver
        # trips.issuer). Se valida aquí en vez de solo en el checkbox del
        # formulario, por si llegan viajes de ambas empresas manipulando el
        # POST a mano.
        if trips:
            issuers = {t["issuer"] for t in trips}
            if len(issuers) > 1:
                flash(
                    "Los viajes seleccionados son de empresas distintas (Harraso y BRMS); "
                    "una factura solo puede emitirse a nombre de una. Factúralos por separado.",
                    "error",
                )
                return redirect(url_for("facturacion.new", client_id=client_id))
            # 21 sep: si se marcaron viajes, la empresa emisora se toma de
            # ellos (igual que siempre) -- el <select> "Empresa que emite"
            # del formulario solo importa cuando la factura es 100% manual
            # (sin viajes), ver el else de abajo.
            issuer = issuers.pop()
        else:
            issuer = request.form.get("issuer")
            if issuer not in ("HARRASO", "BRMS"):
                issuer = "HARRASO"

        total = sum(t["rate"] for t in trips) + sum(amt for _, amt in manual_items)
        number = next_code("F", "invoices")
        series = current_app.config["INVOICE_SERIES"]
        series_number = _next_series_number(series)

        # Detracción (SPOT) — 9 sep: se calcula al crear la factura, igual
        # que el "issuer" (4% sobre el total cuando supera S/400, código de
        # bien "027" -- transporte de carga -- ver compute_detraction en
        # app/helpers.py). 21 sep, pedido de Braulio (facturas no ligadas a
        # viajes, ej. alquileres): el código "027" es específico de
        # transporte de carga y NO corresponde necesariamente a un ítem
        # manual (un alquiler de equipo, por ejemplo, tendría su propio
        # código de detracción, distinto y no confirmado). Para no arriesgar
        # marcar una detracción con el código equivocado en un comprobante
        # fiscal real, el cálculo automático solo se aplica cuando la
        # factura es 100% de viajes (sin ningún ítem manual) -- exactamente
        # el mismo comportamiento que ya existía. Con algún ítem manual
        # incluido, se guarda sin detracción y se avisa para que Braulio lo
        # revise a mano (con su contador o desde el propio portal de
        # tefacturo.pe) antes de enviarla a SUNAT.
        company = company_info_for_issuer(issuer, current_app.config)
        if manual_items:
            detraction = {"applies": False, "code": None, "percentage": None, "amount": None, "bank_account": None}
        else:
            detraction = compute_detraction(total, company)

        db = get_db()
        cur = db.execute(
            """INSERT INTO invoices (number, client_id, issue_date, due_date, amount, notes, series, series_number, issuer,
               detraction_applies, detraction_code, detraction_percentage, detraction_amount, detraction_bank_account)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                number, client_id, issue_date, due_date, total, request.form.get("notes", "").strip(), series, series_number, issuer,
                1 if detraction["applies"] else 0,
                detraction["code"],
                detraction["percentage"],
                detraction["amount"],
                detraction["bank_account"],
            ),
        )
        invoice_id = cur.lastrowid
        for t in trips:
            db.execute(
                "INSERT INTO invoice_items (invoice_id, trip_id, description, amount) VALUES (?, ?, ?, ?)",
                (invoice_id, t["id"], f"{t['code']}: {t['origin']} -> {t['destination']}", t["rate"]),
            )
            db.execute("UPDATE trips SET invoiced = 1 WHERE id = ?", (t["id"],))
        for desc, amt in manual_items:
            db.execute(
                "INSERT INTO invoice_items (invoice_id, trip_id, description, amount) VALUES (?, NULL, ?, ?)",
                (invoice_id, desc, amt),
            )
        db.commit()

        if manual_items and total > 400:
            flash(
                "Esta factura supera S/400 e incluye ítems adicionales (no solo viajes) — "
                "no se le aplicó detracción automática. Confirma si corresponde detracción "
                "(y con qué código) antes de enviarla a SUNAT.",
                "info",
            )
        flash(f"Factura {number} generada por {total:.2f}.", "success")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    selected_client = request.args.get("client_id", type=int)
    pending_trips = []
    if selected_client:
        pending_trips = query_all(
            """SELECT * FROM trips WHERE client_id = ? AND status = 'ENTREGADO' AND invoiced = 0
               ORDER BY delivered_date""",
            (selected_client,),
        )
    return render_template(
        "facturacion/form.html", clients=clients, selected_client=selected_client,
        pending_trips=pending_trips, today=today_str(),
    )


@bp.route("/<int:invoice_id>")
@permission_required("facturacion", "view")
def detail(invoice_id):
    invoice = query_one(
        """SELECT i.*, c.name as client_name, c.ruc as client_ruc FROM invoices i
           JOIN clients c ON c.id = i.client_id WHERE i.id = ?""",
        (invoice_id,),
    )
    if invoice is None:
        abort(404)
    # LEFT JOIN (no JOIN): 21 sep, un ítem manual (alquiler u otro concepto
    # sin viaje, ver new() más arriba) tiene trip_id NULL -- un JOIN normal
    # lo descartaría en silencio de esta lista.
    items = query_all(
        """SELECT ii.*, t.code as trip_code FROM invoice_items ii
           LEFT JOIN trips t ON t.id = ii.trip_id WHERE ii.invoice_id = ?""",
        (invoice_id,),
    )
    return render_template("facturacion/detail.html", invoice=invoice, items=items)


@bp.route("/<int:invoice_id>/estado", methods=["POST"])
@permission_required("facturacion", "edit")
def change_status(invoice_id):
    if not validate_csrf():
        abort(400)
    new_status = request.form.get("status")
    if new_status not in ("PENDIENTE", "PAGADA", "VENCIDA", "ANULADA"):
        abort(400)
    execute("UPDATE invoices SET status = ? WHERE id = ?", (new_status, invoice_id))
    flash("Estado de factura actualizado.", "success")
    return redirect(url_for("facturacion.detail", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/enviar-sunat", methods=["POST"])
@permission_required("facturacion", "edit")
def send_sunat(invoice_id):
    if not validate_csrf():
        abort(400)
    invoice = query_one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    if invoice is None:
        abort(404)
    client_row = query_one("SELECT * FROM clients WHERE id = ?", (invoice["client_id"],))
    items = query_all("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))

    ose_client = build_client_from_config(current_app.config, invoice["issuer"])
    company = company_info_for_issuer(invoice["issuer"], current_app.config)

    try:
        if not company["ruc"]:
            raise SunatOseError(
                f"Falta configurar el RUC de {company['name']} (variable de entorno "
                f"{'BRMS_RUC' if invoice['issuer'] == 'BRMS' else 'COMPANY_RUC'}) antes de "
                "poder emitir facturas electrónicas a su nombre."
            )
        payload = build_invoice_payload(invoice, items, client_row, company)
        ya_existia = False
        try:
            response = ose_client.emit_factura(payload)
            result = parse_ose_response(response)
        except SunatOseError as emit_exc:
            # Reenviar una factura que tefacturo.pe ya tiene registrada
            # (ej. se reintenta "Enviar a SUNAT" solo para volver a
            # descargar el PDF, tras un fallo de descarga) devuelve "el
            # comprobante ya existe" — no es un rechazo nuevo de SUNAT.
            # Confirmado en real, 8 sep, Factura F-0003: antes esto pisaba
            # el estado ACEPTADO ya guardado con ERROR, aunque SUNAT
            # siguiera teniendo la factura aceptada de verdad.
            if is_duplicate_comprobante_error(emit_exc):
                ya_existia = True
                result = {
                    "accepted": True,
                    "message": invoice["sunat_message"]
                    or "Aceptado por SUNAT (comprobante ya registrado en un envío anterior).",
                    "xml_url": invoice["sunat_xml_url"],
                    "cdr_url": invoice["sunat_cdr_url"],
                }
            else:
                raise

        pdf_filename = invoice["sunat_pdf_filename"]
        pdf_url = invoice["sunat_pdf_url"]
        if result["accepted"]:
            try:
                pdf_bytes = ose_client.get_pdf_bytes("01", invoice["series"], invoice["series_number"])
                pdf_filename = f"factura-{invoice_id}-{uuid.uuid4().hex}.pdf"
                save_sunat_document(pdf_filename, pdf_bytes)
                pdf_url = url_for("facturacion.view_sunat_pdf", invoice_id=invoice_id)
            except SunatOseError as pdf_exc:
                # La factura SÍ quedó aceptada por SUNAT — no descartar eso
                # solo porque no se pudo descargar/guardar el PDF.
                flash(f"La factura se aceptó, pero no se pudo descargar su PDF: {pdf_exc}", "error")

        execute(
            """UPDATE invoices SET sunat_status=?, sunat_message=?, sunat_pdf_url=?, sunat_pdf_filename=?,
               sunat_xml_url=?, sunat_cdr_url=?, sunat_sent_at=datetime('now') WHERE id=?""",
            (
                "ACEPTADO" if result["accepted"] else "RECHAZADO",
                result["message"],
                pdf_url,
                pdf_filename,
                result["xml_url"],
                result["cdr_url"],
                invoice_id,
            ),
        )
        if result["accepted"]:
            if ya_existia:
                flash("Esta factura ya estaba aceptada por SUNAT.", "success")
            else:
                flash("Factura enviada y aceptada por SUNAT.", "success")
        else:
            flash(f"SUNAT/tefacturo.pe rechazó la factura: {result['message']}", "error")
    except SunatOseError as exc:
        execute(
            "UPDATE invoices SET sunat_status='ERROR', sunat_message=?, sunat_sent_at=datetime('now') WHERE id=?",
            (str(exc), invoice_id),
        )
        flash(f"No se pudo enviar la factura: {exc}", "error")

    return redirect(url_for("facturacion.detail", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/pdf-sunat")
@permission_required("facturacion", "view")
def view_sunat_pdf(invoice_id):
    """Sirve el PDF real que devolvió tefacturo.pe al emitir esta factura
    (7 sep, segunda ronda) — mismo patrón que las demás descargas de
    archivos del sistema (disco local o redirect a URL firmada en S3)."""
    invoice = query_one("SELECT sunat_pdf_filename FROM invoices WHERE id = ?", (invoice_id,))
    if invoice is None or not invoice["sunat_pdf_filename"]:
        abort(404)
    if using_s3():
        return redirect(sunat_document_url(invoice["sunat_pdf_filename"]))
    return send_from_directory(local_sunat_documents_dir(), invoice["sunat_pdf_filename"])
