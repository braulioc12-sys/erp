import logging
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

# 22 sep, registro de actividad (ver app/audit.py): log_activity() para
# saber quién generó/editó/envió cada factura, get_creator_info() para el
# "Creado por" de facturacion/detail.html (mismo patrón que app/routes/viajes.py).
from app.audit import get_creator_info, log_activity
from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import (
    company_info_for_issuer,
    compute_detraction,
    get_detraction_goods_catalog,
    get_detraction_goods_codes,
    get_detraction_tefacturo_code,
    next_code,
    parse_date,
    parse_float,
    today_str,
)
from app.integrations.ai_vision import AiVisionError, extract_invoice_rows_from_image
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

# 28 sep, bug real en producción (Braulio, factura #14): "Enviar a SUNAT"
# reventó con la página genérica "Internal Server Error" de Flask/Werkzeug
# en vez de un flash claro. Causa: send_sunat() de abajo solo tenía
# `except SunatOseError` -- cualquier otro tipo de excepción (un error de
# red que urllib no envuelve como URLError/HTTPError, un dato inesperado en
# la factura, un bug nuestro) se escapaba sin capturar y Flask no tiene
# forma de mostrar algo útil para eso salvo la página genérica, que además
# no deja ver el traceback real (Render solo se lo muestra a Braulio así,
# nunca el log del servidor). Se agregó un segundo `except Exception` en
# send_sunat() (ver más abajo) que: deja la factura en estado ERROR igual
# que un SunatOseError, muestra un flash con el tipo/mensaje real del error
# (para que Braulio lo pueda copiar y mandar directo, sin tener que ir a
# buscar en los logs de Render), y además loguea el traceback completo acá
# (logger.exception) por si hace falta revisarlo con más detalle. Mismo
# patrón ya usado en este proyecto para no dejar que un error inesperado
# tire abajo la request entera (ver app/audit.py, app/scheduler.py,
# app/routes/integraciones.py).
logger = logging.getLogger(__name__)


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


def _match_client_by_name(name):
    """28 sep, "Facturar desde imagen": intenta calzar el nombre que la IA
    leyó en la columna "Sociedad" del screenshot con un cliente YA
    registrado. Solo se da por encontrado un match exacto (sin importar
    mayúsculas) o, si no hay exacto, un ÚNICO candidato por coincidencia
    parcial -- con cero o más de un candidato se devuelve None y
    from_image_confirm() lo deja sin seleccionar para que Braulio elija (o
    cree el cliente) a mano: nunca se adivina el cliente de una factura
    real."""
    if not name:
        return None
    exact = query_one("SELECT id FROM clients WHERE active = 1 AND LOWER(name) = LOWER(?)", (name,))
    if exact:
        return exact["id"]
    partial = query_all("SELECT id FROM clients WHERE active = 1 AND LOWER(name) LIKE LOWER(?)", (f"%{name}%",))
    if len(partial) == 1:
        return partial[0]["id"]
    return None


@bp.route("/desde-imagen")
@permission_required("facturacion", "edit")
def from_image_upload():
    """28 sep, pedido de Braulio ("quiero que yo subiendo una imagen crees
    la factura como la 13"): pantalla para subir el screenshot del portal
    del cliente (columnas "Documento de compra"/OC, "Número HES",
    "Sociedad" e "Imp. recepcionado") -- ver app/integrations/ai_vision.py
    para la extracción con IA y from_image_confirm() más abajo para la
    revisión obligatoria antes de crear la factura de verdad."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para facturar desde una imagen.", "error")
        return redirect(url_for("facturacion.list_view"))
    return render_template("facturacion/from_image_upload.html", issuer=issuer)


@bp.route("/desde-imagen/extraer", methods=["POST"])
@permission_required("facturacion", "edit")
def from_image_extract():
    """Lee la imagen subida con IA (ver app/integrations/ai_vision.py) y
    manda a la pantalla de confirmación con lo que se pudo extraer -- nunca
    crea la factura directo desde acá."""
    if not validate_csrf():
        abort(400)
    issuer = request.form.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para facturar desde una imagen.", "error")
        return redirect(url_for("facturacion.list_view"))

    file_storage = request.files.get("image")
    if not file_storage or not file_storage.filename:
        flash("Sube una imagen (screenshot) para poder leerla.", "error")
        return redirect(url_for("facturacion.from_image_upload", issuer=issuer))
    raw_bytes = file_storage.read()
    if not raw_bytes:
        flash("La imagen llegó vacía — intenta subirla de nuevo.", "error")
        return redirect(url_for("facturacion.from_image_upload", issuer=issuer))

    try:
        rows = extract_invoice_rows_from_image(
            raw_bytes, file_storage.mimetype, current_app.config.get("ANTHROPIC_API_KEY", "")
        )
    except AiVisionError as exc:
        flash(f"No se pudo leer la imagen: {exc}", "error")
        return redirect(url_for("facturacion.from_image_upload", issuer=issuer))

    # 28 sep, pedido de Braulio (compartió una captura con 3 filas -- "si
    # subo una imagen asi, y solo quiero facturar la del medio, como seria?
    # Siempre es solo una factura por pedido a la vez, no se puede poner
    # todos en una sola factura"): con una sola fila se sigue yendo directo
    # a la confirmación (como antes); con varias, primero hay que elegir
    # CUÁL -- nunca se combinan filas distintas en una sola factura.
    if len(rows) == 1:
        return redirect(_confirm_url(issuer, rows[0]))
    return render_template("facturacion/from_image_pick_row.html", issuer=issuer, rows=rows)


def _confirm_url(issuer, row):
    return url_for(
        "facturacion.from_image_confirm",
        issuer=issuer,
        numero_oc=row["numero_oc"] or "",
        numero_hes=row["numero_hes"] or "",
        sociedad=row["sociedad"] or "",
        monto=row["monto"] if row["monto"] is not None else "",
    )


@bp.route("/desde-imagen/confirmar")
@permission_required("facturacion", "edit")
def from_image_confirm():
    """Pantalla de revisión: muestra lo que se extrajo de la imagen (de la
    fila elegida, si el screenshot tenía varias -- ver from_image_extract()
    y from_image_pick_row.html) ya precargado en un formulario editable --
    el único dato que llega SIEMPRE vacío es la fecha de vencimiento
    (pedido explícito de Braulio: "el unico campo que debe quedar sin
    completar debe ser el de fecha de vencimiento... y yo poner
    manualmente la fecha", ya que todas estas facturas son a crédito).
    Nada se guarda en la base de datos hasta que se confirme en
    from_image_create().

    El cliente se calza por nombre (ver _match_client_by_name()) siempre
    acá, a partir de "sociedad" -- no antes, en from_image_extract() -- así
    funciona igual sin importar de dónde se llegue: extracción directa (una
    sola fila), el selector de filas, o el "volver" de
    from_image_new_client() después de crear un cliente nuevo (que además
    ya calza exacto, porque se creó con ese mismo nombre)."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para facturar desde una imagen.", "error")
        return redirect(url_for("facturacion.list_view"))
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    sociedad = request.args.get("sociedad", "")
    # Si se llega con un client_id explícito (volviendo de crear un cliente
    # nuevo en from_image_new_client(), cuyo nombre pudo haberse editado en
    # ese mini-formulario y ya no calzar con "sociedad" tal cual la leyó la
    # IA), se respeta ESE cliente sin volver a adivinar por nombre.
    client_id = request.args.get("client_id", type=int)
    if client_id is None:
        client_id = _match_client_by_name(sociedad)
    return render_template(
        "facturacion/from_image_confirm.html",
        issuer=issuer,
        clients=clients,
        client_id=client_id,
        numero_oc=request.args.get("numero_oc", ""),
        numero_hes=request.args.get("numero_hes", ""),
        sociedad=sociedad,
        monto=request.args.get("monto", ""),
        today=today_str(),
    )


@bp.route("/desde-imagen/cliente-nuevo", methods=["POST"])
@permission_required("facturacion", "edit")
def from_image_new_client():
    """Mismo criterio que quick_new_client() más arriba (crear un cliente
    sin perder lo ya completado), pero para volver a la pantalla de
    confirmación de "Facturar desde imagen" en vez de a new() -- por eso
    los datos ya extraídos (OC, HES, Sociedad, monto) viajan como campos
    ocultos en el mini-formulario de facturacion/from_image_confirm.html y
    se reenvían acá para no perderlos al crear el cliente."""
    if not validate_csrf():
        abort(400)
    issuer = request.form.get("issuer", "").strip().upper()
    carry = {
        "numero_oc": request.form.get("numero_oc", ""),
        "numero_hes": request.form.get("numero_hes", ""),
        "sociedad": request.form.get("sociedad", ""),
        "monto": request.form.get("monto", ""),
    }
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para facturar desde una imagen.", "error")
        return redirect(url_for("facturacion.list_view"))
    name = request.form.get("name", "").strip()
    if not name:
        flash("El nombre del cliente nuevo es obligatorio.", "error")
        return redirect(url_for("facturacion.from_image_confirm", issuer=issuer, **carry))
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
    return redirect(url_for("facturacion.from_image_confirm", issuer=issuer, client_id=client_id, **carry))


@bp.route("/desde-imagen/crear", methods=["POST"])
@permission_required("facturacion", "edit")
def from_image_create():
    """Crea la factura de verdad con lo confirmado en
    facturacion/from_image_confirm.html: un único ítem manual (sin viaje)
    con la descripción fija que pidió Braulio ("POR EL SERVICIO DE
    TRANSPORTE DE CERVEZA Y ENVASES SEGUN OC ... HES ..."). Mismo patrón de
    INSERT que new() más arriba, simplificado (un solo ítem, sin viajes):
    detracción se calcula automático solo para Harraso (BRMS nunca aplica,
    igual que en new()) y no se ofrece confirmarla manualmente acá -- si
    hiciera falta, se ajusta después desde el detalle de la factura, igual
    que cualquier otra."""
    if not validate_csrf():
        abort(400)
    issuer = request.form.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para facturar desde una imagen.", "error")
        return redirect(url_for("facturacion.list_view"))

    client_id = request.form.get("client_id")
    numero_oc = request.form.get("numero_oc", "").strip()
    numero_hes = request.form.get("numero_hes", "").strip()
    monto = parse_float(request.form.get("monto"), None)
    issue_date = parse_date(request.form.get("issue_date")) or today_str()
    due_date = parse_date(request.form.get("due_date"))
    sociedad = request.form.get("sociedad", "")

    def _back_to_confirm():
        return redirect(
            url_for(
                "facturacion.from_image_confirm", issuer=issuer, client_id=client_id or "",
                numero_oc=numero_oc, numero_hes=numero_hes, sociedad=sociedad,
                monto=request.form.get("monto", ""),
            )
        )

    if not client_id:
        flash("Selecciona o crea el cliente antes de generar la factura.", "error")
        return _back_to_confirm()
    if not numero_oc or not numero_hes:
        flash("Completa el número de OC y el número de HES.", "error")
        return _back_to_confirm()
    if not monto or monto <= 0:
        flash("Ingresa un monto válido.", "error")
        return _back_to_confirm()

    description = f"POR EL SERVICIO DE TRANSPORTE DE CERVEZA Y ENVASES SEGUN OC {numero_oc} HES {numero_hes}"
    company = company_info_for_issuer(issuer, current_app.config)
    # Mismo cálculo automático de detracción que ya usa new() para
    # facturas 100% de viajes (código 027, 4% sobre S/400) -- BRMS nunca
    # aplica (ver company_info_for_issuer/comentario en new() más arriba).
    detraction = (
        compute_detraction(monto, company)
        if issuer == "HARRASO"
        else {"applies": False, "code": None, "percentage": None, "amount": None, "bank_account": None}
    )

    number = next_code("F", "invoices")
    series = current_app.config["INVOICE_SERIES"]
    series_number = _next_series_number(series)

    db = get_db()
    cur = db.execute(
        """INSERT INTO invoices (number, client_id, issue_date, due_date, amount, notes, series, series_number, issuer,
           detraction_applies, detraction_code, detraction_percentage, detraction_amount, detraction_bank_account)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            number, client_id, issue_date, due_date, monto, "", series, series_number, issuer,
            1 if detraction["applies"] else 0,
            detraction["code"], detraction["percentage"], detraction["amount"], detraction["bank_account"],
        ),
    )
    invoice_id = cur.lastrowid
    db.execute(
        "INSERT INTO invoice_items (invoice_id, trip_id, description, amount, quantity) VALUES (?, NULL, ?, ?, 1)",
        (invoice_id, description, monto),
    )
    db.commit()

    client_row = query_one("SELECT name FROM clients WHERE id = ?", (client_id,))
    log_activity(
        "facturacion", "CREAR",
        f"Factura {number} — {client_row['name'] if client_row else ''} — S/{monto:.2f} "
        f"(desde imagen, OC {numero_oc} HES {numero_hes})",
        entity_type="factura", entity_id=invoice_id,
        entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
    )
    if not due_date:
        flash(
            f"Factura {number} generada por {monto:.2f} — es a crédito, no olvides poner la fecha "
            "de vencimiento desde el detalle antes de enviarla a SUNAT.",
            "info",
        )
    else:
        flash(f"Factura {number} generada por {monto:.2f}.", "success")
    return redirect(url_for("facturacion.detail", invoice_id=invoice_id))


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

        # 22 sep, registro de actividad (ver app/audit.py): quién generó esta
        # factura -- el nombre del cliente se toma de la lista `clients` ya
        # cargada arriba (evita una consulta aparte solo para el label).
        client_name = next((c["name"] for c in clients if str(c["id"]) == str(client_id)), "")
        log_activity(
            "facturacion", "CREAR", f"Factura {number} — {client_name} — S/{total:.2f}",
            entity_type="factura", entity_id=invoice_id,
            entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
        )

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
    # 22 sep, pedido de Braulio ("que usuario creo... la factura"): quién y
    # cuándo se generó, según activity_log (ver app/audit.py) -- None para
    # facturas de antes de que existiera este registro.
    creator = get_creator_info("factura", invoice_id)
    # 23 sep: si esta factura tiene detracción, ¿ya se puede reportar a
    # SUNAT de verdad (ver build_invoice_payload en app/integrations/
    # sunat_ose.py), o todavía falta cargarle a este concepto su código de
    # tefacturo.pe en Catálogos → Conceptos de detracción?
    detraction_tefacturo_code = (
        get_detraction_tefacturo_code(invoice["detraction_code"]) if invoice["detraction_applies"] else None
    )
    return render_template(
        "facturacion/detail.html", invoice=invoice, items=items,
        default_detraction_account=company.get("bank_nacion_detraction_account", ""),
        detraction_goods_catalog=get_detraction_goods_catalog(),
        detraction_goods_codes=get_detraction_goods_codes(),
        creator=creator, detraction_tefacturo_code=detraction_tefacturo_code,
    )


def _invoice_is_locked(invoice):
    """23 sep, pedido de Braulio ("si una factura aun no ha sido enviada a
    sunat se deberia poder editar todos los campos"): una vez que SUNAT
    ACEPTÓ el comprobante, ya no se puede tocar nada de su contenido (el
    comprobante electrónico real ya quedó emitido con esos datos) — solo
    hasta ahí se podía editar la detracción (update_detraction, sin este
    chequeo) y nada más. Una factura ANULADA tampoco se edita. NO_ENVIADA,
    ERROR y RECHAZADO sí se pueden editar (es justamente el caso de este
    pedido: corregir un dato — ej. la fecha de emisión — que hizo que
    tefacturo.pe la rechace, sin tener que anularla y crear una nueva)."""
    return invoice["sunat_status"] == "ACEPTADO" or invoice["status"] == "ANULADA"


def _invoice_lock_reason(invoice):
    if invoice["sunat_status"] == "ACEPTADO":
        return "Esta factura ya fue aceptada por SUNAT — el comprobante electrónico ya se emitió así, no se puede editar."
    if invoice["status"] == "ANULADA":
        return "Esta factura está anulada — no se puede editar."
    return None


@bp.route("/<int:invoice_id>/editar", methods=["GET", "POST"])
@permission_required("facturacion", "edit")
def edit(invoice_id):
    invoice = query_one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    if invoice is None:
        abort(404)
    if _invoice_is_locked(invoice):
        flash(_invoice_lock_reason(invoice), "error")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    items = query_all(
        """SELECT ii.*, t.code as trip_code FROM invoice_items ii
           LEFT JOIN trips t ON t.id = ii.trip_id WHERE ii.invoice_id = ? ORDER BY ii.id""",
        (invoice_id,),
    )

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        # Red de seguridad: si la factura se envió y fue aceptada justo
        # entre que se abrió este formulario y se guardó, no se aplica el
        # cambio igual.
        if _invoice_is_locked(invoice):
            flash(_invoice_lock_reason(invoice), "error")
            return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

        client_id = request.form.get("client_id")
        if not client_id:
            flash("Selecciona un cliente para la factura.", "error")
            return redirect(url_for("facturacion.edit", invoice_id=invoice_id))
        issue_date = parse_date(request.form.get("issue_date")) or invoice["issue_date"]
        due_date = parse_date(request.form.get("due_date"))
        notes = request.form.get("notes", "").strip()

        # Ítems ya existentes: se pueden editar (descripción/cantidad/monto)
        # o marcar para eliminar (checkbox "existing_item_delete", con el id
        # del ítem como valor). Si el ítem eliminado venía de un viaje, el
        # viaje vuelve a quedar disponible para facturarse en otra factura
        # (mismo criterio que "Anular" en Viajes: invoiced solo se prende al
        # facturar, así que se apaga al sacarlo de esta factura).
        existing_ids = request.form.getlist("existing_item_id")
        existing_descriptions = request.form.getlist("existing_item_description")
        existing_quantities = request.form.getlist("existing_item_quantity")
        existing_amounts = request.form.getlist("existing_item_amount")
        delete_ids = set(request.form.getlist("existing_item_delete"))
        items_by_id = {str(it["id"]): it for it in items}

        kept_rows = []  # (id, description, quantity, amount, trip_id)
        for item_id, desc, qty_raw, amt_raw in zip_longest(
            existing_ids, existing_descriptions, existing_quantities, existing_amounts, fillvalue=""
        ):
            if not item_id or item_id not in items_by_id:
                continue
            if item_id in delete_ids:
                continue
            desc = (desc or "").strip()
            try:
                qty = float(qty_raw) if qty_raw else 1.0
            except ValueError:
                qty = 0
            try:
                amt = float(amt_raw)
            except ValueError:
                amt = 0
            if not desc or amt <= 0 or qty <= 0:
                flash(
                    f"El ítem \"{items_by_id[item_id]['description']}\" quedó con datos inválidos "
                    "(descripción, cantidad o monto) — revísalo, no se guardó ningún cambio.",
                    "error",
                )
                return redirect(url_for("facturacion.edit", invoice_id=invoice_id))
            kept_rows.append((item_id, desc, qty, amt, items_by_id[item_id]["trip_id"]))

        new_manual_items, manual_items_incomplete = _collect_manual_items()
        if manual_items_incomplete:
            flash(
                "Hay una línea nueva a medio llenar (le falta la descripción o el monto) — "
                "se ignoró esa línea. Revísala antes de guardar si no era esa tu intención.",
                "error",
            )

        if not kept_rows and not new_manual_items:
            flash("Una factura no puede quedar sin ítems — no se guardó ningún cambio.", "error")
            return redirect(url_for("facturacion.edit", invoice_id=invoice_id))

        total = sum(amt for _id, _d, _q, amt, _t in kept_rows) + sum(
            line_total for _desc, _qty, _unit, line_total in new_manual_items
        )

        db = get_db()
        for item_id, desc, qty, amt, _trip_id in kept_rows:
            db.execute(
                "UPDATE invoice_items SET description = ?, quantity = ?, amount = ? WHERE id = ?",
                (desc, qty, amt, item_id),
            )
        for item_id_str, item in items_by_id.items():
            if item_id_str in delete_ids:
                db.execute("DELETE FROM invoice_items WHERE id = ?", (item["id"],))
                if item["trip_id"]:
                    db.execute("UPDATE trips SET invoiced = 0 WHERE id = ?", (item["trip_id"],))
        for desc, qty, _unit_amt, line_total in new_manual_items:
            db.execute(
                "INSERT INTO invoice_items (invoice_id, trip_id, description, amount, quantity) VALUES (?, NULL, ?, ?, ?)",
                (invoice_id, desc, line_total, qty),
            )
        db.execute(
            "UPDATE invoices SET client_id = ?, issue_date = ?, due_date = ?, notes = ?, amount = ? WHERE id = ?",
            (client_id, issue_date, due_date, notes, total, invoice_id),
        )
        db.commit()

        log_activity(
            "facturacion", "EDITAR", f"Factura {invoice['number']}: datos editados (total S/{total:.2f})",
            entity_type="factura", entity_id=invoice_id,
            entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
        )
        # 23 sep: si el total cambió y la factura ya tenía detracción
        # confirmada, el monto detraído guardado (calculado sobre el total
        # VIEJO) puede haber quedado desactualizado -- se avisa en vez de
        # recalcularlo solo (podría pisar un monto que el contador ya había
        # ajustado a mano, ver update_detraction()).
        if invoice["detraction_applies"] and round(total, 2) != round(invoice["amount"], 2):
            flash(
                "El total de la factura cambió — revisa el monto de la detracción (más abajo en el detalle), "
                "puede que ya no corresponda al nuevo total.",
                "info",
            )
        flash("Factura actualizada.", "success")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    return render_template(
        "facturacion/edit.html", invoice=invoice, items=items, clients=clients,
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
    # 23 sep: mismo candado que edit() -- una factura ya ACEPTADA por SUNAT
    # o ANULADA no se toca más, ni siquiera la detracción.
    if _invoice_is_locked(invoice):
        flash(_invoice_lock_reason(invoice), "error")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    if request.form.get("applies") != "on":
        execute(
            """UPDATE invoices SET detraction_applies=0, detraction_code=NULL,
               detraction_percentage=NULL, detraction_amount=NULL, detraction_bank_account=NULL
               WHERE id=?""",
            (invoice_id,),
        )
        # 22 sep, registro de actividad (ver app/audit.py).
        log_activity(
            "facturacion", "EDITAR", f"Factura {invoice['number']}: se quitó la detracción",
            entity_type="factura", entity_id=invoice_id,
            entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
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
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "facturacion", "EDITAR",
        f"Factura {invoice['number']}: detracción código {code} ({percentage:.2f}%) — S/{amount:.2f}",
        entity_type="factura", entity_id=invoice_id,
        entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
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
    invoice = query_one("SELECT number, status FROM invoices WHERE id = ?", (invoice_id,))
    if invoice is None:
        abort(404)
    execute("UPDATE invoices SET status = ? WHERE id = ?", (new_status, invoice_id))
    # 22 sep, registro de actividad (ver app/audit.py).
    log_activity(
        "facturacion", "ESTADO", f"Factura {invoice['number']}: {invoice['status']} → {new_status}",
        entity_type="factura", entity_id=invoice_id,
        entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
    )
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
        xml_filename = invoice["sunat_xml_filename"]
        xml_url = invoice["sunat_xml_url"]
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
            # 24 sep, pedido de Braulio ("ya funciona genera el pdf, pero
            # para descargar el xml?"): mismo patrón que el PDF de arriba,
            # ver get_xml_bytes() en app/integrations/sunat_ose.py. Un
            # fallo acá tampoco debe pisar el estado ACEPTADO ya logrado.
            try:
                xml_bytes = ose_client.get_xml_bytes("01", invoice["series"], invoice["series_number"])
                xml_filename = f"factura-{invoice_id}-{uuid.uuid4().hex}.xml"
                save_sunat_document(xml_filename, xml_bytes)
                xml_url = url_for("facturacion.view_sunat_xml", invoice_id=invoice_id)
            except SunatOseError as xml_exc:
                flash(f"La factura se aceptó, pero no se pudo descargar su XML: {xml_exc}", "error")

        execute(
            """UPDATE invoices SET sunat_status=?, sunat_message=?, sunat_pdf_url=?, sunat_pdf_filename=?,
               sunat_xml_url=?, sunat_xml_filename=?, sunat_cdr_url=?, sunat_sent_at=datetime('now') WHERE id=?""",
            (
                "ACEPTADO" if result["accepted"] else "RECHAZADO",
                result["message"],
                pdf_url,
                pdf_filename,
                xml_url,
                xml_filename,
                result["cdr_url"],
                invoice_id,
            ),
        )
        # 22 sep, registro de actividad (ver app/audit.py): "ENVIAR" cubre
        # tanto el caso aceptado como el rechazado por SUNAT/tefacturo.pe --
        # en ambos casos el usuario sí ejecutó la acción de enviar, y el
        # resultado (aceptado/rechazado) ya queda en el propio label.
        log_activity(
            "facturacion", "ENVIAR",
            f"Factura {invoice['number']} enviada a SUNAT — {'ACEPTADO' if result['accepted'] else 'RECHAZADO'}",
            entity_type="factura", entity_id=invoice_id,
            entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
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
        # 22 sep, registro de actividad (ver app/audit.py): también se deja
        # constancia del intento fallido (error de conexión/config, no un
        # rechazo de SUNAT) -- Braulio quiere saber quién intentó enviarla,
        # no solo los envíos que sí llegaron a SUNAT.
        log_activity(
            "facturacion", "ENVIAR", f"Factura {invoice['number']}: error al enviar a SUNAT — {exc}",
            entity_type="factura", entity_id=invoice_id,
            entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
        )
        flash(f"No se pudo enviar la factura: {exc}", "error")
    except Exception as exc:
        # 28 sep, ver la nota grande junto a `logger = logging.getLogger(...)`
        # más arriba en este archivo: sin esto, cualquier excepción que no
        # sea SunatOseError (un bug nuestro, una respuesta de red que
        # urllib no envuelve como URLError/HTTPError, etc.) tiraba la página
        # genérica "Internal Server Error" de Flask, sin dejar rastro visible
        # para Braulio ni forma de saber qué pasó de verdad. Con esto: la
        # factura queda en ERROR (igual que un SunatOseError), el flash
        # muestra el tipo y mensaje reales del error para que Braulio lo
        # pueda copiar y mandar directo, y el traceback completo queda en el
        # log del servidor (logger.exception) por si hace falta más detalle.
        logger.exception(
            "Error inesperado al enviar la factura #%s a SUNAT", invoice_id
        )
        execute(
            "UPDATE invoices SET sunat_status='ERROR', sunat_message=?, sunat_sent_at=datetime('now') WHERE id=?",
            (f"Error interno inesperado: {type(exc).__name__}: {exc}", invoice_id),
        )
        log_activity(
            "facturacion", "ENVIAR",
            f"Factura {invoice['number']}: error interno inesperado al enviar a SUNAT — "
            f"{type(exc).__name__}: {exc}",
            entity_type="factura", entity_id=invoice_id,
            entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
        )
        flash(
            "No se pudo enviar la factura por un error interno inesperado (no fue un rechazo de "
            f"SUNAT): {type(exc).__name__}: {exc} — copia este mensaje y mándamelo para revisarlo.",
            "error",
        )

    return redirect(url_for("facturacion.detail", invoice_id=invoice_id))


# 29 sep, pedido de Braulio ("que solo el administrador pueda borrar...
# facturas"): antes no existía ninguna forma de borrar una factura del
# todo (solo Anular, que es un cambio de estado -- ver change_status()).
# Acción "delete" propia, que por defecto solo tiene Administrador (ver
# PERMISSIONS en app/auth.py y el comentario en app/permissions_catalog.py).
# Braulio confirmó (29 sep) que una factura ya ACEPTADA por SUNAT NO se
# debe poder borrar -- quedaría un comprobante electrónico real vigente
# sin ningún registro local, y un hueco en la numeración F001 -- para esas
# solo queda Anular, que ya existe.
def _invoice_delete_block_reason(invoice):
    if invoice["sunat_status"] == "ACEPTADO":
        return (
            "Esta factura ya fue aceptada por SUNAT — no se puede borrar del todo (quedaría un "
            "comprobante electrónico vigente sin registro local). Usa \"Anular\" en su lugar."
        )
    return None


@bp.route("/<int:invoice_id>/eliminar", methods=["POST"])
@permission_required("facturacion", "delete")
def delete(invoice_id):
    if not validate_csrf():
        abort(400)
    invoice = query_one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    if invoice is None:
        abort(404)
    reason = _invoice_delete_block_reason(invoice)
    if reason:
        flash(reason, "error")
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    # Los viajes facturados en esta factura vuelven a quedar disponibles
    # para facturarse en otra (mismo criterio que al quitar un ítem desde
    # editar factura -- ver edit()).
    trip_ids = [r["trip_id"] for r in query_all(
        "SELECT trip_id FROM invoice_items WHERE invoice_id = ? AND trip_id IS NOT NULL", (invoice_id,)
    )]
    execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    for trip_id in trip_ids:
        execute("UPDATE trips SET invoiced = 0 WHERE id = ?", (trip_id,))
    log_activity(
        "facturacion", "ELIMINAR", f"Factura {invoice['number']}",
        entity_type="factura", entity_id=invoice_id,
    )
    flash("Factura eliminada.", "success")
    return redirect(url_for("facturacion.list_view"))


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


@bp.route("/<int:invoice_id>/xml-sunat")
@permission_required("facturacion", "view")
def view_sunat_xml(invoice_id):
    """Sirve el XML firmado real que devolvió tefacturo.pe al emitir esta
    factura (24 sep, pedido de Braulio) — mismo patrón que view_sunat_pdf()
    de arriba, salvo que este SÍ se descarga como archivo en vez de
    abrirse en una pestaña nueva (24 sep, segundo pedido: "cuando haga
    click quiero que se descargue, no que se abra en otra ventana del
    explorador") — el XML firmado no es algo que uno quiera "ver" en el
    navegador como el PDF, es un archivo para guardar/mandarle a alguien
    (el contador, SUNAT, etc.). Se descarga con el nombre de serie/número
    real (ej. "F001-000009.xml") en vez del nombre interno con el uuid."""
    invoice = query_one(
        "SELECT sunat_xml_filename, series, series_number FROM invoices WHERE id = ?", (invoice_id,)
    )
    if invoice is None or not invoice["sunat_xml_filename"]:
        abort(404)
    download_name = f"{invoice['series']}-{invoice['series_number']:06d}.xml"
    if using_s3():
        return redirect(
            sunat_document_url(invoice["sunat_xml_filename"], as_attachment=True, download_name=download_name)
        )
    return send_from_directory(
        local_sunat_documents_dir(),
        invoice["sunat_xml_filename"],
        as_attachment=True,
        download_name=download_name,
    )
