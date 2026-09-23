import uuid
from itertools import zip_longest

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import (
    company_info_for_issuer,
    compute_detraction,
    get_detraction_goods_catalog,
    get_detraction_goods_codes,
    next_code,
    parse_date,
    parse_float,
    today_str,
)
from app.integrations.sunat_ose import (
    SunatOseError,
    build_client_from_config,
    build_invoice_payload,
    is_duplicate_comprobante_error,
    parse_ose_response,
)
from app.integrations.sunat_ruc import get_company_for_ruc
from app.routes.viajes import ISSUER_CHOICES
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
    """22 sep, pedido de Braulio ("en facturacion la primera pantalla debe
    ser elegir Harraso o BRMS"): mismo patrón obligatorio (sin una opción
    "Todas") ya usado en viajes.list_view/liquidaciones.list_view — ver el
    comentario en viajes.py. Antes esta lista mezclaba facturas de ambas
    empresas con una columna "Empresa"; ahora, al elegir la empresa acá, esa
    elección se lleva también a "Generar factura" (ver new() más abajo),
    donde ya no hace falta volver a preguntarla."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template("facturacion/list.html", invoices=None, issuer=None, status="")

    status = request.args.get("status", "")
    sql = """SELECT i.*, c.name as client_name FROM invoices i
              JOIN clients c ON c.id = i.client_id WHERE i.issuer = ?"""
    params = [issuer]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    invoices = query_all(sql, params)
    return render_template("facturacion/list.html", invoices=invoices, status=status, issuer=issuer)


def _collect_manual_items():
    """21 sep, pedido de Braulio ("aparte de facturar los viajes, tambien
    se puedan emitir facturas no relacionadas a viajes, como alquileres...
    de todo tipo"): filas libres (cantidad + descripción + precio unitario)
    que el propio formulario permite agregar/quitar con JS (ver
    facturacion/form.html, mismo patrón "+ Agregar línea" que ya usa
    Cotizaciones). Una fila se ignora en silencio si quedó vacía (usuario
    que le dio "+ Agregar línea" de más y no la llenó) -- solo se exige
    description Y amount > 0 cuando al menos uno de los campos de esa fila
    SÍ se completó, para poder avisar de una fila a medio llenar en vez de
    tragarla sin decir nada.

    22 sep, pedido de Braulio ("a la hora de agregar un item debe salir
    cantidad, descripcion y monto" -- ver la captura que compartió del
    formulario de tefacturo.pe, que separa Cantidad y Valor Unitario):
    "amount" pasa a ser el PRECIO UNITARIO que se escribe en el formulario,
    y el monto de la línea (lo que se guarda en invoice_items.amount y lo
    que se suma al total de la factura) es cantidad × amount. Con
    cantidad=1 (el valor por defecto si se deja vacío) el comportamiento es
    IDÉNTICO al de antes -- una factura ya generada con una sola fila nunca
    cambia de monto por este cambio.

    Devuelve una lista de (description, quantity, unit_amount, line_total)."""
    descriptions = request.form.getlist("item_description")
    quantities = request.form.getlist("item_quantity")
    amounts = request.form.getlist("item_amount")
    items = []
    incomplete = False
    # zip_longest (no zip): "item_quantity" es un campo nuevo (22 sep) -- si
    # llegara una fila sin ese campo (formulario viejo en caché, JS que no
    # cargó), un zip() normal recortaría TODAS las filas a la lista más
    # corta y se perderían ítems en silencio. Con fillvalue="" simplemente
    # se asume cantidad 1 para esa fila (ver más abajo), nunca se descarta.
    for desc, qty_raw, amt_raw in zip_longest(descriptions, quantities, amounts, fillvalue=""):
        desc = (desc or "").strip()
        qty_raw = (qty_raw or "").strip()
        amt_raw = (amt_raw or "").strip()
        if not desc and not qty_raw and not amt_raw:
            continue
        try:
            amt = float(amt_raw)
        except ValueError:
            amt = 0
        try:
            qty = float(qty_raw) if qty_raw else 1.0
        except ValueError:
            qty = 0
        if not desc or amt <= 0 or qty <= 0:
            incomplete = True
            continue
        items.append((desc, qty, amt, round(qty * amt, 2)))
    return items, incomplete


@bp.route("/clientes/consultar-ruc")
@permission_required("facturacion", "edit")
def consultar_ruc():
    """22 sep, pedido de Braulio ("debe haber la opcion de generar cliente
    nuevo y se pueda emitir una factura de cliente que no este
    registrado"): autocompleta razón social/dirección al escribir un RUC de
    11 dígitos en el formulario de "cliente nuevo" de Facturación (ver
    facturacion/form.html) -- mismo servicio y caché que ya usa Cotizaciones
    (consultar_ruc() en app/routes/cotizaciones.py) y Liquidaciones. No se
    reusa directamente el endpoint de Cotizaciones a propósito: queda con
    permission_required("cotizaciones", "edit"), y Contabilidad (el único
    rol, junto con Admin, que tiene "facturacion" edit) no siempre tiene por
    qué tener también "cotizaciones" -- hoy la tiene, pero depender de eso
    sería un acoplamiento frágil entre dos módulos que no tienen por qué
    variar juntos. Nunca devuelve error 500: si el servicio externo falla o
    el RUC no existe, responde found=false y el cliente se completa a mano."""
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
        "direccion": company.get("direccion") or "",
    })


@bp.route("/clientes/nuevo", methods=["POST"])
@permission_required("facturacion", "edit")
def quick_new_client():
    """22 sep, pedido de Braulio ("en este menu, debe haber la opcion de
    generar cliente nuevo y se peuda emitir una factura de cliente que no
    este registrado"): crea un cliente sin salir de "Generar factura" (ver
    el formulario colapsable "+ Registrar cliente nuevo" en
    facturacion/form.html) y redirige de vuelta ya con ese cliente
    seleccionado -- como un cliente recién creado nunca tiene viajes
    pendientes, la pantalla le mostrará directo la sección de "Ítems
    adicionales" (ver new() más abajo) para facturarlo igual.

    Gateado por el permiso de Facturación (no el de Clientes) a propósito:
    Contabilidad -- el único rol, junto con Admin, con "facturacion" edit --
    solo tiene "clientes" en modo "view" (ver PERMISSIONS en app/auth.py), y
    es justo quien necesita esto para poder facturar a un cliente nuevo sin
    depender de que alguien más lo dé de alta primero en Clientes."""
    if not validate_csrf():
        abort(400)
    # 22 sep: la empresa ya se eligió al entrar a "Generar factura" (ver
    # new() más abajo) -- el formulario de "+ Registrar cliente nuevo"
    # manda ese mismo issuer en un campo oculto para no perderlo al volver.
    issuer = request.form.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        issuer = None
    name = request.form.get("name", "").strip()
    if not name:
        flash("El nombre del cliente nuevo es obligatorio.", "error")
        return redirect(url_for("facturacion.new", issuer=issuer) if issuer else url_for("facturacion.list_view"))
    client_id = execute(
        "INSERT INTO clients (name, ruc, phone, email, address) VALUES (?, ?, ?, ?, ?)",
        (
            name,
            request.form.get("ruc", "").strip(),
            request.form.get("phone", "").strip(),
            request.form.get("email", "").strip(),
            request.form.get("address", "").strip(),
        ),
    )
    flash(f"Cliente '{name}' creado — ya puedes facturarle.", "success")
    if not issuer:
        return redirect(url_for("facturacion.list_view"))
    return redirect(url_for("facturacion.new", issuer=issuer, client_id=client_id))


@bp.route("/nuevo", methods=["GET", "POST"])
@permission_required("facturacion", "edit")
def new():
    """22 sep, pedido de Braulio ("en facturacion la primera pantalla debe
    ser elegir Harraso o BRMS. Luego cuando se genera factura ya no debe
    salir ese campo de empresa que emite"): la empresa emisora ya no se
    elige dentro de este formulario (el <select> "Empresa que emite" se
    quitó de facturacion/form.html) -- se elige ANTES, en Facturación →
    lista (ver list_view()), y este endpoint la recibe como ?issuer=... y
    la mantiene fija en un campo oculto durante todo el formulario. Sin un
    issuer válido (alguien entra directo a /facturacion/nuevo sin pasar por
    la lista) se manda de vuelta a elegir empresa, igual que el resto de
    módulos con este mismo patrón (viajes, liquidaciones)."""
    if request.method == "POST":
        issuer = request.form.get("issuer", "").strip().upper()
    else:
        issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para generar la factura.", "error")
        return redirect(url_for("facturacion.list_view"))

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
            return redirect(url_for("facturacion.new", issuer=issuer, client_id=client_id))
        if not trip_ids and not manual_items:
            flash(
                "Marca al menos un viaje o agrega al menos un ítem adicional (descripción y monto) para facturar.",
                "error",
            )
            return redirect(url_for("facturacion.new", issuer=issuer, client_id=client_id))
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
                return redirect(url_for("facturacion.new", issuer=issuer, client_id=client_id))

        # 7 sep, integración con tefacturo.pe: un comprobante electrónico se
        # emite a nombre de UN RUC, así que todos los viajes de una misma
        # factura deben ser de la misma empresa (Harraso o BRMS, ver
        # trips.issuer). 22 sep: ahora la empresa se fija ANTES (arriba, vía
        # ?issuer=...) y "Viajes entregados pendientes de facturar" ya solo
        # muestra los de esa empresa (ver el GET más abajo) -- esta
        # validación queda como red de seguridad por si alguien manipula el
        # POST a mano con viajes de la empresa equivocada.
        if trips:
            issuers = {t["issuer"] for t in trips}
            if len(issuers) > 1 or issuer not in issuers:
                flash(
                    "Los viajes seleccionados no son de la empresa elegida (Harraso/BRMS); "
                    "una factura solo puede emitirse a nombre de una. Factúralos por separado.",
                    "error",
                )
                return redirect(url_for("facturacion.new", issuer=issuer, client_id=client_id))

        total = sum(t["rate"] for t in trips) + sum(line_total for _, _, _, line_total in manual_items)
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
        # el mismo comportamiento que ya existía.
        #
        # 22 sep, pedido de Braulio ("como confirmo la detraccion?" +
        # captura del formulario de tefacturo.pe, que sí tiene un switch de
        # "Detracción"): con algún ítem manual incluido, ahora se puede
        # confirmar la detracción de una vez en esta misma pantalla (mismos
        # campos y misma validación que update_detraction() en
        # facturacion/detail.html) -- se elige el bien de una lista (ver
        # get_detraction_goods_catalog() en app/helpers.py, que lee de la
        # tabla `detraction_concepts` -- editable desde Catálogos) que
        # completa sola el código y el porcentaje; el monto se calcula solo
        # si se deja en blanco. Si NO se marca el switch, o el bien elegido no trae
        # código/porcentaje válidos, se guarda sin detracción -- igual que
        # siempre, se puede confirmar después desde el detalle de la
        # factura.
        #
        # 22 sep, mismo pedido ("recuerda que solo Harraso emite con
        # detraccion, BRMS no"): para una factura de BRMS no se ofrece nada
        # de esto -- ni este bloque manual ni el cálculo automático de abajo
        # -- sin importar el monto ni los ítems que tenga.
        company = company_info_for_issuer(issuer, current_app.config)
        if issuer != "HARRASO":
            detraction = {"applies": False, "code": None, "percentage": None, "amount": None, "bank_account": None}
        elif manual_items:
            if request.form.get("detraction_applies") == "on":
                det_code = request.form.get("detraction_code", "").strip()
                det_percentage = parse_float(request.form.get("detraction_percentage"), default=0.0)
                det_bank_account = request.form.get("detraction_bank_account", "").strip()
                if det_code and det_percentage > 0:
                    det_amount_raw = request.form.get("detraction_amount", "").strip()
                    det_amount = parse_float(det_amount_raw) if det_amount_raw else round(total * det_percentage / 100, 2)
                    detraction = {
                        "applies": True, "code": det_code, "percentage": det_percentage,
                        "amount": det_amount, "bank_account": det_bank_account,
                    }
                else:
                    flash(
                        "Marcaste que esta factura tiene detracción, pero no elegiste un bien válido de la "
                        "lista — se generó SIN detracción. Confírmala desde el detalle de la factura.",
                        "error",
                    )
                    detraction = {"applies": False, "code": None, "percentage": None, "amount": None, "bank_account": None}
            else:
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
        for desc, qty, _unit_amt, line_total in manual_items:
            db.execute(
                "INSERT INTO invoice_items (invoice_id, trip_id, description, amount, quantity) VALUES (?, NULL, ?, ?, ?)",
                (invoice_id, desc, line_total, qty),
            )
        db.commit()

        if issuer == "HARRASO" and manual_items and total > 400 and not detraction["applies"]:
            flash(
                "Esta factura supera S/400 e incluye ítems adicionales (no solo viajes) — "
                "no se le aplicó detracción. Confirma si corresponde detracción (y con qué código) "
                "antes de enviarla a SUNAT.",
                "info",
            )
        flash(f"Factura {number} generada por {total:.2f}.", "success")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    selected_client = request.args.get("client_id", type=int)
    pending_trips = []
    if selected_client:
        # 22 sep: solo viajes de la empresa ya elegida arriba (issuer) --
        # antes se mostraban los del cliente sin importar la empresa, y el
        # aviso de "factúralos por separado" cubría el resto; ahora que la
        # empresa se fija primero, ni siquiera aparecen los de la otra.
        pending_trips = query_all(
            """SELECT * FROM trips WHERE client_id = ? AND issuer = ? AND status = 'ENTREGADO' AND invoiced = 0
               ORDER BY delivered_date""",
            (selected_client, issuer),
        )
    company = company_info_for_issuer(issuer, current_app.config)
    return render_template(
        "facturacion/form.html", clients=clients, selected_client=selected_client,
        pending_trips=pending_trips, today=today_str(), issuer=issuer,
        default_detraction_account=company.get("bank_nacion_detraction_account", ""),
        detraction_goods_catalog=get_detraction_goods_catalog(),
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
    # 22 sep, pedido de Braulio ("como confirmo la detraccion?"): cuando la
    # factura tiene ítems manuales (ver new() más arriba), el sistema NO
    # aplica detracción automática -- se necesita un campo para
    # confirmarla/editarla a mano (ver update_detraction() abajo). Se
    # sugiere la cuenta del Banco de la Nación de la empresa emisora como
    # punto de partida, editable por si esta factura puntual usa otra.
    company = company_info_for_issuer(invoice["issuer"], current_app.config)
    return render_template(
        "facturacion/detail.html", invoice=invoice, items=items,
        default_detraction_account=company.get("bank_nacion_detraction_account", ""),
        detraction_goods_catalog=get_detraction_goods_catalog(),
        detraction_goods_codes=get_detraction_goods_codes(),
    )


@bp.route("/<int:invoice_id>/detraccion", methods=["POST"])
@permission_required("facturacion", "edit")
def update_detraction(invoice_id):
    """22 sep, pedido de Braulio ("como confirmo la detraccion?"): edición
    manual de la detracción de UNA factura -- necesaria porque el cálculo
    automático (compute_detraction() en app/helpers.py) solo aplica al
    código "027" (transporte de carga) y se desactiva por completo apenas
    la factura tiene algún ítem manual (alquileres, gestión, etc. -- ver
    new()), ya que esos servicios pueden estar sujetos a un código y
    porcentaje de detracción TOTALMENTE DISTINTO (o no estarlo en
    absoluto) según el Anexo de SUNAT, y este sistema no tiene forma de
    determinarlo solo. Braulio (o su contador) confirma acá el código y
    porcentaje correctos; el monto se puede dejar en blanco para que se
    calcule solo a partir del porcentaje, o ingresarlo a mano si difiere.

    Desmarcar "aplica" limpia los 4 campos de detracción de la factura
    (vuelve a quedar como si nunca se le hubiera aplicado)."""
    if not validate_csrf():
        abort(400)
    invoice = query_one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    if invoice is None:
        abort(404)

    if request.form.get("applies") != "on":
        execute(
            """UPDATE invoices SET detraction_applies=0, detraction_code=NULL,
               detraction_percentage=NULL, detraction_amount=NULL, detraction_bank_account=NULL
               WHERE id=?""",
            (invoice_id,),
        )
        flash("Se quitó la detracción de esta factura.", "success")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    # 22 sep, pedido de Braulio ("recuerda que solo Harraso emite con
    # detraccion, BRMS no"): el formulario de facturacion/detail.html ya no
    # ofrece esta sección para una factura de BRMS (ver el `{% if
    # invoice.issuer == 'HARRASO' %}` ahí) -- este chequeo es solo la red de
    # seguridad por si alguien manda el POST a mano igual. Sí se permite
    # siempre desmarcar "aplica" (el bloque de arriba), por si una factura
    # de BRMS quedó con detracción de antes de esta regla y hay que
    # corregirla.
    if invoice["issuer"] != "HARRASO":
        flash("BRMS no emite comprobantes con detracción — no se puede aplicar aquí.", "error")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    code = request.form.get("code", "").strip()
    percentage = parse_float(request.form.get("percentage"), default=0.0)
    bank_account = request.form.get("bank_account", "").strip()
    if not code or percentage <= 0:
        flash("Ingresa el código de detracción y un porcentaje mayor a 0.", "error")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    amount_raw = request.form.get("amount", "").strip()
    amount = parse_float(amount_raw) if amount_raw else round(invoice["amount"] * percentage / 100, 2)

    execute(
        """UPDATE invoices SET detraction_applies=1, detraction_code=?, detraction_percentage=?,
           detraction_amount=?, detraction_bank_account=? WHERE id=?""",
        (code, percentage, amount, bank_account, invoice_id),
    )
    flash("Detracción actualizada.", "success")
    return redirect(url_for("facturacion.detail", invoice_id=invoice_id))


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
