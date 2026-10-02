import io
import logging
import os
import uuid
import zipfile
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
    IGV_RATE,
    SunatOseError,
    build_client_from_config,
    build_credit_note_payload,
    build_invoice_payload,
    is_duplicate_comprobante_error,
    parse_ose_response,
)
from app.integrations.pdf_text import extract_pdf_text, pdf_text_matches_invoice
from app.integrations.sunat_ruc import get_company_for_ruc
from app.integrations.sunat_xml import SunatXmlError, parse_invoice_xml
from app.routes.viajes import ISSUER_CHOICES
from app.storage import (
    local_sunat_documents_dir,
    save_sunat_document,
    sunat_document_url,
    using_s3,
)

bp = Blueprint("facturacion", __name__, url_prefix="/facturacion")

# 30 sep, "Cargar factura ya emitida (SUNAT)" -- ver manual_upload() y
# manual_zip() más abajo: extensiones válidas para el PDF/XML del
# comprobante que Braulio ya descargó del portal de SUNAT.
ALLOWED_MANUAL_PDF_EXTENSIONS = {".pdf"}
ALLOWED_MANUAL_XML_EXTENSIONS = {".xml"}

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
    """29 sep: mismo bug de conteo que next_code() en app/helpers.py (ver su
    comentario) — acá no hay un UNIQUE en series_number que lo haga saltar
    como error, así que un correlativo repetido pasaría silencioso (SUNAT sí
    lo rechazaría al enviar el comprobante). Una factura sí se puede borrar
    (ver facturacion.delete()/viajes.py al desvincular un viaje facturado),
    así que el conteo puede bajar sin que el número más alto ya usado deje
    de existir. MAX en vez de COUNT, igual que next_code()."""
    row = query_one(
        "SELECT MAX(series_number) as n FROM invoices WHERE series = ?", (series,)
    )
    return (row["n"] if row and row["n"] is not None else 0) + 1


# 1 oct, nueva feature "Notas de crédito" (pedido de Braulio: "Hay que
# incluir en facturacion la emision de notas de credito"). Estos 7 valores
# son los únicos que confirma la documentación real de tefacturo.pe
# (https://api.tefacturo.pe/doc/integracion/docs/api/nota-credito/,
# verificada con dos fetches independientes) -- ver la nota larga en
# app/schema.sql junto a credit_notes.reason_code, y build_credit_note_payload()
# en app/integrations/sunat_ose.py para el payload real que arma cada uno.
CREDIT_NOTE_REASONS = [
    ("ANULACION_OPERACION", "Anulación total de la operación"),
    ("ANULACION_ERROR_RUC", "Anulación por error en el RUC del cliente"),
    ("CORRECCION_DESCRIPCION", "Corrección por error en la descripción"),
    ("DESCUENTO_GLOBAL", "Descuento global"),
    ("DESCUENTO_ITEM", "Descuento por ítem"),
    ("DEVOLUCION_TOTAL", "Devolución total"),
    ("DEVOLUCION_ITEM", "Devolución por ítem"),
]
CREDIT_NOTE_REASON_LABELS = dict(CREDIT_NOTE_REASONS)
# Motivos que son, por definición, una anulación del 100% de la factura --
# new_credit_note() de más abajo los trata distinto: fuerza incluir todos
# los ítems originales por su monto completo y no deja editar montos
# parciales (mezclar "anulación total" con un monto recortado a mano no
# tendría sentido ante SUNAT).
CREDIT_NOTE_FULL_REASONS = {"ANULACION_OPERACION", "ANULACION_ERROR_RUC"}


def _next_credit_note_series_number(series):
    """Mismo patrón MAX (no COUNT) que _next_series_number() de arriba --
    ver su comentario: una nota de crédito también se puede borrar/quedar
    mal creada, así que contar filas en vez de buscar el máximo ya usado
    repetiría un correlativo."""
    row = query_one(
        "SELECT MAX(series_number) as n FROM credit_notes WHERE series = ?", (series,)
    )
    return (row["n"] if row and row["n"] is not None else 0) + 1


def _credit_note_available_amount(invoice):
    """Cuánto de esta factura todavía se puede acreditar: su monto total
    menos lo que ya cubren notas de crédito existentes para ella que no
    fueron RECHAZADAS por SUNAT (una rechazada nunca llegó a existir ante
    SUNAT de verdad, así que no "gasta" nada del saldo disponible)."""
    row = query_one(
        "SELECT COALESCE(SUM(amount), 0) as used FROM credit_notes "
        "WHERE invoice_id = ? AND sunat_status != 'RECHAZADO'",
        (invoice["id"],),
    )
    used = row["used"] if row and row["used"] is not None else 0
    return round(invoice["amount"] - used, 2)


@bp.route("")
@permission_required("facturacion", "view")
def list_view():
    """22 sep, pedido de Braulio ("en facturacion la primera pantalla debe
    ser elegir Harraso o BRMS"): mismo patrón obligatorio (sin una opción
    "Todas") ya usado en viajes.list_view/liquidaciones.list_view — ver el
    comentario en viajes.py. Antes esta lista mezclaba facturas de ambas
    empresas con una columna "Empresa"; ahora, al elegir la empresa acá, esa
    elección se lleva también a "Generar factura" (ver new() más abajo),
    donde ya no hace falta volver a preguntarla.

    30 sep, pedido de Braulio ("agregues aca una columna que se llame
    numero sunat... y que tambien haya la opcion de buscar"): `q` busca por
    cliente, por el número interno de Harris o por la serie-número real de
    SUNAT — mismo patrón (LOWER() en ambos lados, "series || '-' ||
    series_number" sin el cero-relleno que sí se muestra en pantalla, ver
    facturacion/list.html) ya usado en guias.list_view()/clientes.list_view()
    (patch 0066)."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template("facturacion/list.html", invoices=None, issuer=None, status="", q="", pending_e001=0)

    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    sql = """SELECT i.*, c.name as client_name FROM invoices i
              JOIN clients c ON c.id = i.client_id WHERE i.issuer = ?"""
    params = [issuer]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if q:
        sql += """ AND (LOWER(c.name) LIKE LOWER(?) OR LOWER(i.number) LIKE LOWER(?)
                    OR LOWER(i.series || '-' || i.series_number) LIKE LOWER(?))"""
        params += [f"%{q}%"] * 3
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    invoices = query_all(sql, params)
    # 1 oct, pedido de Braulio ("todas las facturas e001... ya todas han
    # sido cobradas, hay que cambiar su status"): para mostrar el botón de
    # marcar-pagadas-en-lote (ver brms_e001_mark_paid() más arriba) solo
    # cuando BRMS tiene al menos una E001 pendiente -- si ya se usó una vez
    # y no quedan más, el botón deja de aparecer solo (no hace falta que
    # Braulio adivine si ya no hay nada por hacer).
    pending_e001 = 0
    if issuer == "BRMS":
        row = query_one(
            "SELECT COUNT(*) as n FROM invoices WHERE issuer = 'BRMS' AND series = 'E001' "
            "AND status NOT IN ('PAGADA', 'ANULADA')"
        )
        pending_e001 = row["n"] if row else 0
    return render_template(
        "facturacion/list.html", invoices=invoices, status=status, issuer=issuer, q=q,
        pending_e001=pending_e001,
    )


@bp.route("/brms-e001/marcar-pagadas", methods=["POST"])
@permission_required("facturacion", "edit")
def brms_e001_mark_paid():
    """1 oct, pedido de Braulio ("en la facturacion de BRMS, todas las
    facturas e001-, es decir las que se emitieron anteriormente en otro
    sistema ya todas han sido cobradas, hay que cambiar su status"): arreglo
    puntual de datos, no un flujo recurrente. Las facturas con serie "E001"
    son las que BRMS emitió en su sistema anterior (antes de que existiera
    este ERP) y se cargaron acá con "Cargar factura ya emitida (SUNAT)" (ver
    manual_create() más arriba), preservando su serie-número real tal cual
    -- nunca se les asignó un correlativo nuevo (ver el comentario de 30 sep
    en manual_create()). Braulio confirma que TODAS esas ya fueron cobradas
    en su momento, así que este botón las pone en PAGADA de una sola vez en
    vez de una por una con "Cambiar estado" (change_status() más arriba).

    Alcance deliberadamente acotado para no tocar nada que no se pidió:
    - issuer='BRMS' AND series='E001' únicamente -- ni las F001 nuevas de
      BRMS (facturadas desde este ERP) ni ninguna factura de Harraso.
    - Excluye las que ya están en PAGADA (nada que cambiar, la deja igual
      -- este botón es idempotente, se puede apretar de nuevo sin efecto
      si ya no queda ninguna pendiente) y las ANULADA (una factura anulada
      marcada como pagada sería contradictorio -- si alguna E001 quedó
      anulada por error, se corrige a mano, no con este botón masivo).
    Mismo permiso que change_status() (facturacion/edit) porque es
    conceptualmente la misma acción (cambiar el estado de una factura),
    solo que aplicada en lote a las que ya califican."""
    if not validate_csrf():
        abort(400)
    rows = query_all(
        "SELECT id, number FROM invoices WHERE issuer = 'BRMS' AND series = 'E001' "
        "AND status NOT IN ('PAGADA', 'ANULADA')"
    )
    if not rows:
        flash("No hay facturas E001 de BRMS pendientes de marcar como pagadas.", "info")
        return redirect(url_for("facturacion.list_view", issuer="BRMS"))
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    execute(f"UPDATE invoices SET status = 'PAGADA' WHERE id IN ({placeholders})", ids)
    numbers = ", ".join(r["number"] for r in rows)
    log_activity(
        "facturacion", "ESTADO",
        f"{len(rows)} factura(s) E001 de BRMS marcadas como PAGADA en lote ({numbers})",
        entity_type="factura",
    )
    flash(f"{len(rows)} factura(s) E001 de BRMS marcadas como pagadas.", "success")
    return redirect(url_for("facturacion.list_view", issuer="BRMS"))


def _collect_manual_items(issuer=None):
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

    1 oct, pedido de Braulio ("en el caso de harraso, cuando se ingrese el
    monto que sea sin igv y luego abajo muestres cuanto es el igv y el
    total"): invoice_items.amount sigue significando lo mismo de siempre en
    TODO el resto del sistema (el monto TOTAL de la línea, YA CON IGV --
    así lo asume build_invoice_payload()/_split_igv() en
    app/integrations/sunat_ose.py, el cálculo de detracción, los reportes,
    etc.) -- lo único que cambia es qué escribe Braulio en el campo "P.
    unitario" para una factura de Harraso: el precio SIN IGV, no el precio
    final. Por eso acá, cuando `issuer == "HARRASO"`, el precio unitario
    escrito se "sube" un 18% antes de multiplicarlo por la cantidad -- el
    valor que queda guardado en invoice_items.amount termina siendo
    exactamente el mismo que si Braulio hubiera escrito el precio CON IGV
    directamente, así que nada aguas abajo (SUNAT, detracción, reportes)
    se entera de este cambio ni hace falta tocarlo. BRMS no cambia en nada
    (exonerada de IGV, ver igv_exonerado en company_info_for_issuer) --
    issuer=None (valor por defecto) tampoco convierte nada, para no romper
    el resto de los llamadores de esta función (from_image_create(),
    manual_create(), etc.) que no pasan por este flujo.

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
        if issuer == "HARRASO":
            amt = round(amt * (1 + IGV_RATE), 2)
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


def _match_client_by_ruc(ruc):
    """30 sep, "Cargar factura ya emitida (SUNAT)": a diferencia de
    _match_client_by_name() (que calza por nombre porque es lo único que la
    IA lee de un screenshot), acá el XML del comprobante SÍ trae el RUC del
    cliente -- un dato mucho más confiable que el nombre para encontrar (o
    descartar) un cliente ya registrado, así que se intenta primero."""
    if not ruc:
        return None
    row = query_one("SELECT id FROM clients WHERE active = 1 AND ruc = ?", (ruc,))
    return row["id"] if row else None


def _save_manual_document_file(file_storage, allowed_extensions):
    """Guarda el PDF/XML de una factura cargada manualmente (ver
    manual_extract() más abajo) con storage.save_sunat_document() -- mismo
    mecanismo/carpeta que ya usa send_sunat() para el PDF/XML que devuelve
    tefacturo.pe (así ambos casos se sirven/descargan igual desde
    view_sunat_pdf()/view_sunat_xml()). Devuelve (nombre_guardado,
    raw_bytes), o (None, None) si no se subió nada o la extensión no es
    válida -- se devuelven también los bytes ya leídos (en vez de nada más
    el nombre) para poder parsear el XML enseguida sin tener que releerlo
    de storage (que en modo S3 no lo tendría en disco local)."""
    if not file_storage or not file_storage.filename:
        return None, None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in allowed_extensions:
        return None, None
    raw_bytes = file_storage.read()
    if not raw_bytes:
        return None, None
    filename = f"{uuid.uuid4().hex}{ext}"
    save_sunat_document(filename, raw_bytes)
    return filename, raw_bytes


@bp.route("/cargar-manual")
@permission_required("facturacion", "edit")
def manual_upload():
    """30 sep, pedido de Braulio ("quiero subir de manera manual, o en un
    zip todas las facturas que antes he emitido como BRMS desde el portal
    sunat. quiero poder descargarlas tambien desde harris"): primera
    pantalla para cargar UNA factura que ya se emitió de verdad, directo
    desde el portal de SUNAT (fuera de este ERP y de la integración con
    tefacturo.pe) -- ver manual_extract()/manual_confirm()/manual_create()
    más abajo para el resto del flujo, y manual_zip() para cargar varias a
    la vez con un .zip. Mismo patrón obligatorio de elegir empresa antes
    (ver el comentario en new()) que el resto de Facturación."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para cargar la factura.", "error")
        return redirect(url_for("facturacion.list_view"))
    return render_template("facturacion/manual_upload.html", issuer=issuer)


@bp.route("/cargar-manual/extraer", methods=["POST"])
@permission_required("facturacion", "edit")
def manual_extract():
    """Guarda el/los archivo(s) subidos (ya, acá mismo -- a diferencia de
    "Facturar desde imagen", que solo pasa datos de texto entre pantallas,
    acá hay que pasar también un PDF/XML reales entre esta pantalla y la de
    confirmación, y un <input type=file> nunca se puede precargar por
    seguridad del navegador -- así que se guardan de una vez con
    storage.save_sunat_document() y de ahí en adelante solo viaja el NOMBRE
    ya guardado). Si el XML viene y se puede leer (ver
    app/integrations/sunat_xml.py), se usa para precargar cliente, serie,
    número, fecha y monto en la pantalla de confirmación -- si no viene, o
    no se pudo leer, esos campos quedan en blanco para llenarlos a mano
    (pero el PDF ya subido NO se pierde)."""
    if not validate_csrf():
        abort(400)
    issuer = request.form.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para cargar la factura.", "error")
        return redirect(url_for("facturacion.list_view"))

    xml_file_storage = request.files.get("xml")
    xml_was_attempted = bool(xml_file_storage and xml_file_storage.filename)

    pdf_filename, _pdf_bytes = _save_manual_document_file(request.files.get("pdf"), ALLOWED_MANUAL_PDF_EXTENSIONS)
    if not pdf_filename:
        flash("Sube el PDF de la factura (formato .pdf) para poder cargarla.", "error")
        return redirect(url_for("facturacion.manual_upload", issuer=issuer))
    xml_filename, xml_bytes = _save_manual_document_file(xml_file_storage, ALLOWED_MANUAL_XML_EXTENSIONS)

    extracted = {}
    if xml_was_attempted and not xml_filename:
        flash("El archivo XML no tiene extensión .xml válida — se ignoró (el PDF sí se guardó).", "error")
    elif xml_bytes:
        try:
            extracted = parse_invoice_xml(xml_bytes)
        except SunatXmlError as exc:
            flash(f"No se pudo leer el XML: {exc} — completa los datos a mano.", "error")
            extracted = {}

    client_id = _match_client_by_ruc(extracted.get("customer_ruc")) or _match_client_by_name(
        extracted.get("customer_name")
    )
    return redirect(
        url_for(
            "facturacion.manual_confirm",
            issuer=issuer,
            pdf_filename=pdf_filename,
            xml_filename=xml_filename or "",
            client_id=client_id or "",
            customer_name=extracted.get("customer_name") or "",
            customer_ruc=extracted.get("customer_ruc") or "",
            series=extracted.get("series") or "",
            correlativo=extracted.get("correlativo") or "",
            issue_date=parse_date(extracted.get("issue_date")) or "",
            monto=extracted.get("total_amount") if extracted.get("total_amount") is not None else "",
        )
    )


@bp.route("/cargar-manual/confirmar")
@permission_required("facturacion", "edit")
def manual_confirm():
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para cargar la factura.", "error")
        return redirect(url_for("facturacion.list_view"))
    clients = query_all("SELECT * FROM clients WHERE active = 1 ORDER BY name")
    return render_template(
        "facturacion/manual_upload_confirm.html",
        issuer=issuer, clients=clients,
        pdf_filename=request.args.get("pdf_filename", ""),
        xml_filename=request.args.get("xml_filename", ""),
        client_id=request.args.get("client_id", type=int),
        customer_name=request.args.get("customer_name", ""),
        customer_ruc=request.args.get("customer_ruc", ""),
        series=request.args.get("series", "") or current_app.config["INVOICE_SERIES"],
        correlativo=request.args.get("correlativo", ""),
        issue_date=request.args.get("issue_date") or today_str(),
        monto=request.args.get("monto", ""),
        today=today_str(),
    )


@bp.route("/cargar-manual/cliente-nuevo", methods=["POST"])
@permission_required("facturacion", "edit")
def manual_new_client():
    """Igual que from_image_new_client() más arriba, pero para volver a la
    pantalla de confirmación de "Cargar factura ya emitida" sin perder lo ya
    extraído/subido -- viaja todo como campos ocultos en el mini-formulario
    de facturacion/manual_upload_confirm.html."""
    if not validate_csrf():
        abort(400)
    issuer = request.form.get("issuer", "").strip().upper()
    carry = {
        "pdf_filename": request.form.get("pdf_filename", ""),
        "xml_filename": request.form.get("xml_filename", ""),
        "series": request.form.get("series", ""),
        "correlativo": request.form.get("correlativo", ""),
        "issue_date": request.form.get("issue_date", ""),
        "monto": request.form.get("monto", ""),
    }
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para cargar la factura.", "error")
        return redirect(url_for("facturacion.list_view"))
    name = request.form.get("name", "").strip()
    if not name:
        flash("El nombre del cliente nuevo es obligatorio.", "error")
        return redirect(url_for("facturacion.manual_confirm", issuer=issuer, **carry))
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
    flash(f"Cliente '{name}' creado — ya puedes usarlo para esta factura.", "success")
    return redirect(url_for("facturacion.manual_confirm", issuer=issuer, client_id=client_id, **carry))


@bp.route("/cargar-manual/crear", methods=["POST"])
@permission_required("facturacion", "edit")
def manual_create():
    """Crea de verdad el registro de una factura que Braulio YA emitió
    directo desde el portal de SUNAT (fuera de este ERP): a diferencia de
    new()/from_image_create() (que arman un comprobante nuevo y lo mandan a
    SUNAT vía tefacturo.pe), acá el comprobante YA existe y ya fue aceptado
    -- se guarda con sunat_status='ACEPTADO' directo (nunca 'NO_ENVIADA') y
    el PDF/XML ya subidos quedan enlazados exactamente igual que si
    "Enviar a SUNAT" los hubiera descargado (ver send_sunat() más abajo) --
    así facturacion/detail.html los muestra para descargar sin ningún
    cambio de plantilla, y el botón "Enviar a SUNAT" no aparece (ya está
    aceptada). serie/número se toman tal cual los escribió/confirmó Braulio
    (el correlativo REAL del comprobante ya emitido, no uno generado por
    next_code()/_next_series_number() -- esos siguen sirviendo para la
    PRÓXIMA factura nueva gracias al MAX() en vez de COUNT(*), ver el
    comentario en _next_series_number())."""
    if not validate_csrf():
        abort(400)
    issuer = request.form.get("issuer", "").strip().upper()
    pdf_filename = request.form.get("pdf_filename", "").strip()
    xml_filename = request.form.get("xml_filename", "").strip()

    def _back_to_confirm():
        return redirect(
            url_for(
                "facturacion.manual_confirm", issuer=issuer, pdf_filename=pdf_filename, xml_filename=xml_filename,
                client_id=request.form.get("client_id", ""), series=request.form.get("series", ""),
                correlativo=request.form.get("correlativo", ""), issue_date=request.form.get("issue_date", ""),
                monto=request.form.get("monto", ""),
            )
        )

    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para cargar la factura.", "error")
        return redirect(url_for("facturacion.list_view"))
    if not pdf_filename:
        flash("Falta el PDF de la factura — vuelve a empezar la carga.", "error")
        return redirect(url_for("facturacion.manual_upload", issuer=issuer))

    client_id = request.form.get("client_id")
    series = request.form.get("series", "").strip().upper()
    correlativo_raw = request.form.get("correlativo", "").strip()
    issue_date = parse_date(request.form.get("issue_date")) or today_str()
    due_date = parse_date(request.form.get("due_date"))
    monto = parse_float(request.form.get("monto"), None)

    if not client_id:
        flash("Selecciona o crea el cliente antes de guardar la factura.", "error")
        return _back_to_confirm()
    if not series or not correlativo_raw:
        flash("Completa la serie y el número del comprobante (ej. F001-123).", "error")
        return _back_to_confirm()
    try:
        correlativo = int(correlativo_raw)
    except ValueError:
        flash("El número del comprobante debe ser numérico (ej. 123 para F001-123).", "error")
        return _back_to_confirm()
    if not monto or monto <= 0:
        flash("Ingresa un monto válido.", "error")
        return _back_to_confirm()

    existing = query_one(
        "SELECT id, number FROM invoices WHERE issuer = ? AND series = ? AND series_number = ?",
        (issuer, series, correlativo),
    )
    if existing:
        flash(
            f"Ya existe una factura con {series}-{correlativo:06d} (Factura {existing['number']}) — "
            "revisa si ya la habías cargado.",
            "error",
        )
        return _back_to_confirm()

    # 30 sep, pedido de Braulio ("el nombre de la factura tiene que
    # mantenerse con el del archivo (E001-00495) y no asignarle un nombre
    # nuevo"): antes acá se llamaba a next_code("F", ...) igual que para una
    # factura NUEVA de verdad (ver new() más abajo) -- eso le asignaba a una
    # factura ya emitida por SUNAT un número interno "F-XXXX" inventado
    # (ej. "F-0220" para lo que en realidad es "E001-000349"), Y de paso
    # hacía avanzar el contador de next_code() para las facturas F-XXXX
    # reales de Harris (que cuentan el MAX ya usado con ese prefijo -- ver
    # su comentario en app/helpers.py), corriendo el próximo número real
    # más adelante de lo que debía. Ahora el "number" de una factura cargada
    # a mano ES literalmente su serie-número real de SUNAT, tal cual
    # aparece en el archivo -- así no inventa un nombre nuevo y no le quita
    # ningún número a la numeración de Harris.
    #
    # "number" tiene una restricción UNIQUE global (no separada por
    # empresa, a diferencia de series+series_number de arriba, que sí lo
    # está) -- coincidiría solo si OTRA empresa (Harraso vs BRMS) ya hubiera
    # cargado a mano un comprobante con la misma serie-número exacta, algo
    # posible en teoría (las series "F001"/"E001" son genéricas) aunque
    # las dos empresas facturan real y por separado en SUNAT. Se revisa
    # aparte (sin filtrar por empresa, a diferencia del chequeo de arriba)
    # para avisar con un mensaje claro en vez de reventar con un error de
    # base de datos.
    number = f"{series}-{correlativo:06d}"
    number_clash = query_one("SELECT id, issuer FROM invoices WHERE number = ?", (number,))
    if number_clash:
        flash(
            f"Ya existe una factura con el número {number} cargada para "
            f"{'BRMS' if number_clash['issuer'] == 'BRMS' else 'Harraso Transport'} — "
            "si es la misma empresa, revisa si ya la habías cargado; si es la otra empresa, "
            "avísanos porque este caso no está previsto.",
            "error",
        )
        return _back_to_confirm()
    db = get_db()
    cur = db.execute(
        """INSERT INTO invoices (number, client_id, issue_date, due_date, amount, notes, series, series_number,
           issuer, sunat_status, sunat_message, sunat_pdf_filename, sunat_xml_filename, sunat_sent_at, manual_upload)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACEPTADO', ?, ?, ?, datetime('now'), 1)""",
        (
            number, client_id, issue_date, due_date, monto, request.form.get("notes", "").strip(),
            series, correlativo, issuer,
            "Factura cargada manualmente — emitida directamente desde el portal de SUNAT (fuera de Harris).",
            pdf_filename, xml_filename or None,
        ),
    )
    invoice_id = cur.lastrowid
    db.execute(
        "INSERT INTO invoice_items (invoice_id, trip_id, description, amount, quantity) VALUES (?, NULL, ?, ?, 1)",
        (invoice_id, "Factura cargada manualmente (emitida desde el portal SUNAT)", monto),
    )
    # sunat_pdf_url/sunat_xml_url: mismo criterio que send_sunat() -- ruta
    # interna que sirve el archivo (view_sunat_pdf/view_sunat_xml ya resuelven
    # disco local vs S3 solos), recién ahora que ya existe invoice_id.
    db.execute(
        "UPDATE invoices SET sunat_pdf_url = ?, sunat_xml_url = ? WHERE id = ?",
        (
            url_for("facturacion.view_sunat_pdf", invoice_id=invoice_id),
            url_for("facturacion.view_sunat_xml", invoice_id=invoice_id) if xml_filename else None,
            invoice_id,
        ),
    )
    db.commit()

    client_row = query_one("SELECT name FROM clients WHERE id = ?", (client_id,))
    log_activity(
        "facturacion", "CREAR",
        f"Factura {number} ({series}-{correlativo:06d}) — {client_row['name'] if client_row else ''} — "
        f"S/{monto:.2f} (cargada manualmente desde SUNAT)",
        entity_type="factura", entity_id=invoice_id,
        entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
    )
    flash(f"Factura {number} ({series}-{correlativo:06d}) cargada por S/{monto:.2f}.", "success")
    return redirect(url_for("facturacion.detail", invoice_id=invoice_id))


_MANUAL_ZIP_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB, ver _iter_zip_leaf_files()


def _iter_zip_leaf_files(raw_bytes, _depth=0, _seen_total=None):
    """30 sep, segunda vuelta de "Cargar factura ya emitida (SUNAT)":
    algunos de los archivos que baja el portal de SUNAT son ellos mismos un
    .zip (Braulio compartió una captura con varias "Compressed (zipped)
    Folder" junto a PDFs/XMLs sueltos) -- en vez de exigirle que los
    desempaquete todos a mano antes de subir el .zip final, esto los abre
    también, recursivamente (hasta 3 niveles, para no quedar en un loop con
    un .zip que se contenga a sí mismo por error). Devuelve una lista plana
    de (nombre_de_archivo, bytes) para cada archivo real encontrado
    (ignora carpetas y basura de sistema como __MACOSX/.DS_Store).

    _seen_total acumula el total de bytes ya descomprimidos entre todas las
    llamadas recursivas -- sin este límite, un .zip pequeño que contenga
    zips anidados especialmente armados podría descomprimir muchísimo más
    de lo que pesa el archivo subido ("zip bomb")."""
    if _seen_total is None:
        _seen_total = [0]
    leaves = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename)
            if not base or base.startswith(".") or "__MACOSX" in info.filename:
                continue
            try:
                data = zf.read(info.filename)
            except RuntimeError as exc:
                # 30 sep, tras un 500 real en producción: zf.read() de un
                # archivo protegido con contraseña dentro del zip lanza
                # RuntimeError (no zipfile.BadZipFile) -- sin este chequeo,
                # eso se colaba crudo hasta tirar abajo toda la carga.
                raise ValueError(
                    f"El archivo \"{base}\" dentro del zip está protegido con contraseña — quítale la "
                    "contraseña (o exclúyelo del zip) y vuelve a subirlo."
                ) from exc
            except NotImplementedError as exc:
                raise ValueError(
                    f"El archivo \"{base}\" dentro del zip usa un método de compresión no soportado."
                ) from exc
            _seen_total[0] += len(data)
            if _seen_total[0] > _MANUAL_ZIP_MAX_TOTAL_BYTES:
                raise ValueError(
                    "El .zip (incluyendo lo que tiene adentro) pesa demasiado una vez descomprimido "
                    "— súbelo en partes más chicas."
                )
            if base.lower().endswith(".zip") and _depth < 3:
                leaves.extend(_iter_zip_leaf_files(data, _depth + 1, _seen_total))
            else:
                leaves.append((base, data))
    return leaves


@bp.route("/cargar-manual/zip", methods=["GET", "POST"])
@permission_required("facturacion", "edit")
def manual_zip():
    """30 sep, pedido de Braulio ("o en un zip todas las facturas..."):
    carga MUCHAS facturas ya emitidas de una sola vez. A diferencia de la
    carga individual (manual_upload()/manual_confirm()), acá no hay pantalla
    de revisión por factura -- se necesita el XML de cada una (el PDF solo
    no alcanza para saber cliente/monto/serie-número de forma confiable) y
    los datos se toman tal cual vienen ahí; el resultado (creadas/omitidas)
    se muestra al final para que Braulio revise qué faltó.

    30 sep, segunda vuelta -- Braulio compartió una captura de su carpeta
    de descargas: el PDF y el XML de una misma factura NO comparten nombre
    de archivo para nada (los nombra distinto el portal/navegador). La
    primera versión de esto calzaba por nombre de archivo -- ya no sirve.
    Ahora se calzan por CONTENIDO: se junta primero TODO el XML que se
    pueda leer (cada uno ya trae su serie-número exacta, ver
    parse_invoice_xml) y luego, para cada uno, se busca entre los PDF del
    zip cuál menciona esa misma serie-número en su texto (ver
    app/integrations/pdf_text.py) -- sin importar cómo se llame el
    archivo. De paso, algunos de los archivos que baja el portal de SUNAT
    son ellos mismos un .zip (ej. "Compressed Folder" en la captura de
    Braulio) -- _iter_zip_leaf_files() los abre también, recursivamente."""
    issuer = request.args.get("issuer", "").strip().upper() or request.form.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige primero la empresa (Harraso o BRMS) para cargar facturas.", "error")
        return redirect(url_for("facturacion.list_view"))

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        try:
            file_storage = request.files.get("zip")
            if not file_storage or not file_storage.filename:
                flash("Sube un archivo .zip con las facturas (PDF + XML de cada una).", "error")
                return redirect(url_for("facturacion.manual_zip", issuer=issuer))
            raw_bytes = file_storage.read()
            if not raw_bytes or not zipfile.is_zipfile(io.BytesIO(raw_bytes)):
                flash("El archivo subido no es un .zip válido.", "error")
                return redirect(url_for("facturacion.manual_zip", issuer=issuer))

            try:
                leaves = _iter_zip_leaf_files(raw_bytes)
            except zipfile.BadZipFile:
                flash("El .zip tiene un archivo comprimido dentro que no se pudo abrir.", "error")
                return redirect(url_for("facturacion.manual_zip", issuer=issuer))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("facturacion.manual_zip", issuer=issuer))
            if not leaves:
                flash("El .zip está vacío o no tiene archivos reconocibles.", "error")
                return redirect(url_for("facturacion.manual_zip", issuer=issuer))

            # Junta primero TODO el XML que se pueda leer como comprobante --
            # un .xml que no lo sea (ej. la Constancia de Recepción/CDR que
            # también entrega SUNAT junto al comprobante) simplemente no entra
            # a la lista, sin reportarse como error: no es una factura que
            # Braulio esperaba cargar. Guarda también los bytes originales
            # (para save_sunat_document() más abajo) y el nombre (solo para
            # mostrarlo en el resultado si hiciera falta).
            xml_candidates = []
            pdf_candidates = []
            for filename, data in leaves:
                ext = os.path.splitext(filename)[1].lower()
                if ext == ".xml":
                    try:
                        extracted = parse_invoice_xml(data)
                    except SunatXmlError:
                        continue
                    xml_candidates.append({"filename": filename, "bytes": data, "extracted": extracted})
                elif ext == ".pdf":
                    pdf_candidates.append({"filename": filename, "bytes": data, "text": extract_pdf_text(data)})

            if not xml_candidates:
                flash(
                    "No se encontró ningún XML de comprobante reconocible dentro del .zip "
                    "(¿subiste el XML de cada factura, no solo el CDR?).",
                    "error",
                )
                return redirect(url_for("facturacion.manual_zip", issuer=issuer))

            created, errors = [], []
            used_pdfs = set()
            company = company_info_for_issuer(issuer, current_app.config)
            for cand in xml_candidates:
                extracted = cand["extracted"]
                xml_bytes = cand["bytes"]
                label = f"{extracted['series']}-{extracted['correlativo']:06d}"

                pdf_match = None
                for idx, pdf in enumerate(pdf_candidates):
                    if idx in used_pdfs:
                        continue
                    if pdf_text_matches_invoice(pdf["text"], extracted["series"], extracted["correlativo"]):
                        pdf_match = idx
                        break
                if pdf_match is None:
                    errors.append({
                        "row": label,
                        "message": (
                            f"No se encontró, entre los PDF del zip, uno que mencione \"{label}\" — "
                            "súbela individual desde \"Cargar factura ya emitida\"."
                        ),
                    })
                    continue
                pdf_bytes = pdf_candidates[pdf_match]["bytes"]
                used_pdfs.add(pdf_match)

                if (
                    company.get("ruc")
                    and extracted.get("supplier_ruc")
                    and extracted["supplier_ruc"] != company["ruc"]
                ):
                    errors.append({
                        "row": label,
                        "message": (
                            f"El RUC emisor del XML ({extracted['supplier_ruc']}) no es el de "
                            f"{company['name']} ({company['ruc']}) — revisa que estés en la empresa correcta."
                        ),
                    })
                    continue

                existing = query_one(
                    "SELECT number FROM invoices WHERE issuer = ? AND series = ? AND series_number = ?",
                    (issuer, extracted["series"], extracted["correlativo"]),
                )
                if existing:
                    errors.append({
                        "row": label,
                        "message": f"Ya existe como Factura {existing['number']} — no se volvió a cargar.",
                    })
                    continue

                # 30 sep, mismo arreglo que manual_create() (ver su
                # comentario grande): el "number" de una factura cargada a
                # mano ES su serie-número real de SUNAT tal cual, no un
                # "F-XXXX" inventado con next_code() -- eso antes corría de
                # más el contador de las facturas F-XXXX reales de Harris.
                # Se revisa la colisión ANTES de crear cliente nuevo o subir
                # los archivos a S3, para no dejar trabajo a medias si esta
                # factura no se va a poder cargar.
                number = f"{extracted['series']}-{extracted['correlativo']:06d}"
                number_clash = query_one("SELECT id, issuer FROM invoices WHERE number = ?", (number,))
                if number_clash:
                    errors.append({
                        "row": label,
                        "message": (
                            f"Ya existe una factura con el número {number} cargada para "
                            f"{'BRMS' if number_clash['issuer'] == 'BRMS' else 'Harraso Transport'} "
                            "— no se volvió a cargar."
                        ),
                    })
                    continue

                client_id = _match_client_by_ruc(extracted.get("customer_ruc")) or _match_client_by_name(
                    extracted.get("customer_name")
                )
                new_client = False
                if not client_id:
                    client_id = execute(
                        "INSERT INTO clients (name, ruc) VALUES (?, ?)",
                        (extracted["customer_name"], extracted.get("customer_ruc") or ""),
                    )
                    new_client = True

                pdf_filename = f"{uuid.uuid4().hex}.pdf"
                xml_filename = f"{uuid.uuid4().hex}.xml"
                save_sunat_document(pdf_filename, pdf_bytes)
                save_sunat_document(xml_filename, xml_bytes)

                issue_date = parse_date(extracted.get("issue_date")) or today_str()
                invoice_id = execute(
                    """INSERT INTO invoices (number, client_id, issue_date, amount, notes, series, series_number,
                       issuer, sunat_status, sunat_message, sunat_pdf_filename, sunat_xml_filename, sunat_sent_at,
                       manual_upload)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACEPTADO', ?, ?, ?, datetime('now'), 1)""",
                    (
                        number, client_id, issue_date, extracted["total_amount"], "",
                        extracted["series"], extracted["correlativo"], issuer,
                        "Factura cargada manualmente — emitida directamente desde el portal de SUNAT "
                        "(fuera de Harris), importada desde un .zip.",
                        pdf_filename, xml_filename,
                    ),
                )
                execute(
                    "INSERT INTO invoice_items (invoice_id, trip_id, description, amount, quantity) VALUES (?, NULL, ?, ?, 1)",
                    (invoice_id, "Factura cargada manualmente (emitida desde el portal SUNAT)", extracted["total_amount"]),
                )
                execute(
                    "UPDATE invoices SET sunat_pdf_url = ?, sunat_xml_url = ? WHERE id = ?",
                    (
                        url_for("facturacion.view_sunat_pdf", invoice_id=invoice_id),
                        url_for("facturacion.view_sunat_xml", invoice_id=invoice_id),
                        invoice_id,
                    ),
                )
                client_row = query_one("SELECT name FROM clients WHERE id = ?", (client_id,))
                log_activity(
                    "facturacion", "CREAR",
                    f"Factura {number} ({label}) — {client_row['name'] if client_row else ''} — "
                    f"S/{extracted['total_amount']:.2f} (cargada manualmente desde SUNAT, importada desde .zip)",
                    entity_type="factura", entity_id=invoice_id,
                    entity_url=url_for("facturacion.detail", invoice_id=invoice_id),
                )
                created.append({
                    "invoice_id": invoice_id, "number": number,
                    "series": extracted["series"], "correlativo": extracted["correlativo"],
                    "client_name": client_row["name"] if client_row else "",
                    "amount": extracted["total_amount"], "new_client": new_client,
                })

            unmatched_pdfs = len(pdf_candidates) - len(used_pdfs)
            return render_template(
                "facturacion/manual_upload_zip_result.html", issuer=issuer, created=created, errors=errors,
                unmatched_pdfs=unmatched_pdfs,
            )
        except Exception as exc:
            # 30 sep, tras un 500 real en producción ("Error handling request
            # /facturacion/cargar-manual/zip", sin más detalle en el log de Render):
            # mismo patrón ya usado en este proyecto para no dejar que un error
            # inesperado (un archivo del zip con algo que ningún caso de arriba
            # anticipó) tire abajo la carga completa con un 500 crudo -- ver el
            # comentario grande junto a "logger = logging.getLogger(...)" al inicio
            # de este archivo. El flash muestra el tipo/mensaje real del error para
            # que Braulio lo pueda copiar y mandar directo.
            logger.exception("Error inesperado al cargar facturas por zip (issuer=%s)", issuer)
            flash(
                f"Ocurrió un error inesperado al procesar el zip: {type(exc).__name__}: {exc} "
                "— copia este mensaje y avísame si el problema sigue.",
                "error",
            )
            return redirect(url_for("facturacion.manual_zip", issuer=issuer))

    return render_template("facturacion/manual_upload_zip.html", issuer=issuer)


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

    number = next_code("F", "invoices", code_column="number")
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
        # 1 oct, pedido de Braulio ("en ambas empresas hay que poder
        # seleccionar la moneda (soles o dolares)") -- mismo criterio que
        # quotations.currency (Cotizaciones): sin CHECK acá, se valida acá
        # mismo contra los dos únicos valores válidos.
        currency = (request.form.get("currency") or "SOLES").strip().upper()
        if currency not in ("SOLES", "DOLARES"):
            currency = "SOLES"
        manual_items, manual_items_incomplete = _collect_manual_items(issuer)

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
        number = next_code("F", "invoices", code_column="number")
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
               currency, detraction_applies, detraction_code, detraction_percentage, detraction_amount, detraction_bank_account)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                number, client_id, issue_date, due_date, total, request.form.get("notes", "").strip(), series, series_number, issuer,
                currency,
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
    # 1 oct, feature "Notas de crédito": panel "Notas de crédito emitidas"
    # más abajo en el template, solo visible cuando ya existe alguna.
    credit_notes = query_all(
        "SELECT * FROM credit_notes WHERE invoice_id = ? ORDER BY id DESC", (invoice_id,)
    )
    return render_template(
        "facturacion/detail.html", invoice=invoice, items=items,
        default_detraction_account=company.get("bank_nacion_detraction_account", ""),
        detraction_goods_catalog=get_detraction_goods_catalog(),
        detraction_goods_codes=get_detraction_goods_codes(),
        creator=creator, detraction_tefacturo_code=detraction_tefacturo_code,
        credit_notes=credit_notes, credit_note_reason_labels=CREDIT_NOTE_REASON_LABELS,
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

        # 1 oct: issuer=invoice["issuer"] -- mismo criterio de "P. unitario
        # sin IGV para Harraso" que new() (ver la nota larga en
        # _collect_manual_items()), para que una línea nueva agregada desde
        # Editar factura no quede con un monto distinto del que hubiera
        # tenido si se agregaba desde Generar factura.
        new_manual_items, manual_items_incomplete = _collect_manual_items(invoice["issuer"])
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
    archivos del sistema (disco local o redirect a URL firmada en S3).

    30 sep, pedido de Braulio ("cuando se cree el archivo y se descargue,
    tanto el pdf como el xml que el nombre del archivo sea la factura + su
    extension"): mismo nombre de serie/número real que ya usa
    view_sunat_xml() de abajo (ej. "F001-000009.pdf") en vez del nombre
    interno con el uuid — sigue abriéndose en una pestaña nueva como
    siempre, solo cambia el nombre sugerido si el usuario lo guarda."""
    invoice = query_one(
        "SELECT sunat_pdf_filename, series, series_number FROM invoices WHERE id = ?", (invoice_id,)
    )
    if invoice is None or not invoice["sunat_pdf_filename"]:
        abort(404)
    download_name = f"{invoice['series']}-{invoice['series_number']:06d}.pdf"
    if using_s3():
        return redirect(sunat_document_url(invoice["sunat_pdf_filename"], download_name=download_name))
    return send_from_directory(
        local_sunat_documents_dir(), invoice["sunat_pdf_filename"], download_name=download_name
    )


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


# ============================================================================
# Notas de crédito (1 oct, pedido de Braulio: "Hay que incluir en
# facturacion la emision de notas de credito"). Corrigen/anulan ante SUNAT
# una factura YA ACEPTADA -- el botón "Anular" de más arriba (change_status())
# NO se toca para nada: sigue siendo la forma de anular localmente una
# factura que nunca llegó a enviarse a SUNAT (o que SUNAT rechazó), a pedido
# expreso de Braulio de dejar ambos flujos completamente separados. Ver la
# nota larga en app/schema.sql junto a CREATE TABLE credit_notes para el
# resto del contexto, y build_credit_note_payload() en
# app/integrations/sunat_ose.py para el payload real.
# ============================================================================


@bp.route("/<int:invoice_id>/notas-credito/nueva", methods=["GET", "POST"])
@permission_required("facturacion", "edit")
def new_credit_note(invoice_id):
    """Solo se ofrece para una factura que SUNAT ya ACEPTÓ -- no tendría
    sentido "corregir ante SUNAT" algo que SUNAT nunca llegó a aceptar (para
    esos casos sigue estando "Anular", que no se toca para nada por esta
    feature)."""
    invoice = query_one(
        """SELECT i.*, c.name as client_name, c.ruc as client_ruc FROM invoices i
           JOIN clients c ON c.id = i.client_id WHERE i.id = ?""",
        (invoice_id,),
    )
    if invoice is None:
        abort(404)
    if invoice["sunat_status"] != "ACEPTADO":
        flash(
            "Solo se puede emitir una nota de crédito de una factura ya ACEPTADA por SUNAT. "
            "Si esta factura nunca se envió (o fue rechazada), usa \"Anular\" en su lugar.",
            "error",
        )
        return redirect(url_for("facturacion.detail", invoice_id=invoice_id))

    items = query_all(
        """SELECT ii.*, t.code as trip_code FROM invoice_items ii
           LEFT JOIN trips t ON t.id = ii.trip_id WHERE ii.invoice_id = ?""",
        (invoice_id,),
    )
    available = _credit_note_available_amount(invoice)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        reason_code = request.form.get("reason_code")
        if reason_code not in CREDIT_NOTE_REASON_LABELS:
            flash("Elige un motivo válido.", "error")
            return redirect(url_for("facturacion.new_credit_note", invoice_id=invoice_id))
        reason_note = (request.form.get("reason_note") or "").strip()
        issue_date = parse_date(request.form.get("issue_date")) or today_str()
        is_full = reason_code in CREDIT_NOTE_FULL_REASONS

        cn_items = []
        if is_full:
            # Anulación total (ANULACION_OPERACION/ANULACION_ERROR_RUC):
            # todos los ítems de la factura original, por su monto completo
            # -- no se ofrecen montos parciales para estos dos motivos (ver
            # CREDIT_NOTE_FULL_REASONS más arriba).
            for it in items:
                cn_items.append({
                    "invoice_item_id": it["id"],
                    "description": it["description"],
                    "quantity": it["quantity"],
                    "amount": it["amount"],
                })
        else:
            # Parcial (pedido explícito de Braulio: "También parciales (por
            # ítem o monto)"): se eligen ítems por checkbox, cada uno con su
            # monto editable (recortado al monto original del ítem, para no
            # poder acreditar más de lo que ese ítem vale en la factura), más
            # una línea libre opcional para montos sin ítem de origen (ej.
            # un descuento global).
            for it in items:
                if request.form.get(f"item_{it['id']}"):
                    amount = parse_float(request.form.get(f"amount_{it['id']}"), default=it["amount"])
                    amount = min(amount, it["amount"])
                    if amount > 0:
                        cn_items.append({
                            "invoice_item_id": it["id"],
                            "description": it["description"],
                            "quantity": it["quantity"],
                            "amount": round(amount, 2),
                        })
            extra_desc = (request.form.get("extra_description") or "").strip()
            extra_amount = parse_float(request.form.get("extra_amount"), default=0.0)
            if extra_desc and extra_amount > 0:
                cn_items.append({
                    "invoice_item_id": None,
                    "description": extra_desc,
                    "quantity": 1,
                    "amount": round(extra_amount, 2),
                })

        total = round(sum(it["amount"] for it in cn_items), 2)
        if not cn_items or total <= 0:
            flash(
                "Esta nota de crédito no tiene ítems ni monto -- elige al menos un ítem o completa "
                "el monto adicional.",
                "error",
            )
            return redirect(url_for("facturacion.new_credit_note", invoice_id=invoice_id))
        if total > available + 0.01:
            flash(
                f"El monto de la nota de crédito ({total:.2f}) supera lo disponible para acreditar de "
                f"esta factura ({available:.2f}, considerando notas de crédito previas).",
                "error",
            )
            return redirect(url_for("facturacion.new_credit_note", invoice_id=invoice_id))
        if not invoice["client_ruc"]:
            flash(
                f"El cliente '{invoice['client_name']}' no tiene RUC registrado -- una nota de "
                "crédito electrónica requiere el RUC del cliente.",
                "error",
            )
            return redirect(url_for("facturacion.new_credit_note", invoice_id=invoice_id))

        series = current_app.config["CREDIT_NOTE_SERIES"]
        series_number = _next_credit_note_series_number(series)
        number = next_code("NC", "credit_notes", code_column="number")
        credit_note_id = execute(
            """INSERT INTO credit_notes
               (number, invoice_id, issue_date, reason_code, reason_note, amount, currency,
                issuer, series, series_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                number, invoice_id, issue_date, reason_code, reason_note, total,
                invoice["currency"], invoice["issuer"], series, series_number,
            ),
        )
        for it in cn_items:
            execute(
                """INSERT INTO credit_note_items (credit_note_id, invoice_item_id, description, quantity, amount)
                   VALUES (?, ?, ?, ?, ?)""",
                (credit_note_id, it["invoice_item_id"], it["description"], it["quantity"], it["amount"]),
            )
        log_activity(
            "facturacion", "CREAR_NC", f"Nota de crédito {number} de la factura {invoice['number']}",
            entity_type="nota_credito", entity_id=credit_note_id,
            entity_url=url_for("facturacion.credit_note_detail", credit_note_id=credit_note_id),
        )
        flash(f"Nota de crédito {number} creada. Ahora puedes enviarla a SUNAT.", "success")
        return redirect(url_for("facturacion.credit_note_detail", credit_note_id=credit_note_id))

    return render_template(
        "facturacion/credit_note_new.html", invoice=invoice, items=items,
        available=available, reasons=CREDIT_NOTE_REASONS, full_reasons=CREDIT_NOTE_FULL_REASONS,
        today=today_str(),
    )


@bp.route("/notas-credito/<int:credit_note_id>")
@permission_required("facturacion", "view")
def credit_note_detail(credit_note_id):
    credit_note = query_one(
        """SELECT cn.*, i.number as invoice_number, i.id as invoice_id,
               c.name as client_name, c.ruc as client_ruc
           FROM credit_notes cn
           JOIN invoices i ON i.id = cn.invoice_id
           JOIN clients c ON c.id = i.client_id
           WHERE cn.id = ?""",
        (credit_note_id,),
    )
    if credit_note is None:
        abort(404)
    items = query_all(
        "SELECT * FROM credit_note_items WHERE credit_note_id = ?", (credit_note_id,)
    )
    return render_template(
        "facturacion/credit_note_detail.html", credit_note=credit_note, items=items,
        reason_label=CREDIT_NOTE_REASON_LABELS.get(credit_note["reason_code"], credit_note["reason_code"]),
    )


@bp.route("/notas-credito/<int:credit_note_id>/enviar-sunat", methods=["POST"])
@permission_required("facturacion", "edit")
def send_credit_note_sunat(credit_note_id):
    """Mismo patrón que send_sunat() de más arriba (para la factura) --
    incluyendo el doble `except` (SunatOseError + Exception genérica) por el
    mismo motivo real de producción documentado en el comentario grande
    junto a `logger = logging.getLogger(...)` al inicio de este archivo."""
    if not validate_csrf():
        abort(400)
    credit_note = query_one("SELECT * FROM credit_notes WHERE id = ?", (credit_note_id,))
    if credit_note is None:
        abort(404)
    invoice = query_one("SELECT * FROM invoices WHERE id = ?", (credit_note["invoice_id"],))
    client_row = query_one("SELECT * FROM clients WHERE id = ?", (invoice["client_id"],))
    items = query_all("SELECT * FROM credit_note_items WHERE credit_note_id = ?", (credit_note_id,))

    ose_client = build_client_from_config(current_app.config, credit_note["issuer"])
    company = company_info_for_issuer(credit_note["issuer"], current_app.config)

    try:
        if not company["ruc"]:
            raise SunatOseError(
                f"Falta configurar el RUC de {company['name']} (variable de entorno "
                f"{'BRMS_RUC' if credit_note['issuer'] == 'BRMS' else 'COMPANY_RUC'}) antes de "
                "poder emitir notas de crédito electrónicas a su nombre."
            )
        payload = build_credit_note_payload(credit_note, items, invoice, client_row, company)
        ya_existia = False
        try:
            response = ose_client.emit_nota_credito(payload)
            result = parse_ose_response(response)
        except SunatOseError as emit_exc:
            if is_duplicate_comprobante_error(emit_exc):
                ya_existia = True
                result = {
                    "accepted": True,
                    "message": credit_note["sunat_message"]
                    or "Aceptado por SUNAT (comprobante ya registrado en un envío anterior).",
                    "xml_url": credit_note["sunat_xml_url"],
                    "cdr_url": credit_note["sunat_cdr_url"],
                }
            else:
                raise

        pdf_filename = credit_note["sunat_pdf_filename"]
        pdf_url = credit_note["sunat_pdf_url"]
        xml_filename = credit_note["sunat_xml_filename"]
        xml_url = credit_note["sunat_xml_url"]
        if result["accepted"]:
            # "07" = Nota de Crédito Electrónica en el Catálogo No. 01 de
            # SUNAT (el mismo catálogo ya usado en este archivo para "01"
            # factura/"03" boleta -- este código SÍ es un estándar público
            # de SUNAT, no una suposición propia de tefacturo.pe como las
            # que este proyecto evita: la documentación de tefacturo.pe para
            # consultarPdf/consultarXml no da un ejemplo específico por tipo
            # de comprobante, pero el parámetro es justamente ese código de
            # Catálogo 01 en los otros dos usos ya confirmados de este mismo
            # archivo).
            try:
                pdf_bytes = ose_client.get_pdf_bytes("07", credit_note["series"], credit_note["series_number"])
                pdf_filename = f"nota-credito-{credit_note_id}-{uuid.uuid4().hex}.pdf"
                save_sunat_document(pdf_filename, pdf_bytes)
                pdf_url = url_for("facturacion.view_credit_note_pdf", credit_note_id=credit_note_id)
            except SunatOseError as pdf_exc:
                flash(f"La nota de crédito se aceptó, pero no se pudo descargar su PDF: {pdf_exc}", "error")
            try:
                xml_bytes = ose_client.get_xml_bytes("07", credit_note["series"], credit_note["series_number"])
                xml_filename = f"nota-credito-{credit_note_id}-{uuid.uuid4().hex}.xml"
                save_sunat_document(xml_filename, xml_bytes)
                xml_url = url_for("facturacion.view_credit_note_xml", credit_note_id=credit_note_id)
            except SunatOseError as xml_exc:
                flash(f"La nota de crédito se aceptó, pero no se pudo descargar su XML: {xml_exc}", "error")

        execute(
            """UPDATE credit_notes SET sunat_status=?, sunat_message=?, sunat_pdf_url=?, sunat_pdf_filename=?,
               sunat_xml_url=?, sunat_xml_filename=?, sunat_cdr_url=?, sunat_sent_at=datetime('now') WHERE id=?""",
            (
                "ACEPTADO" if result["accepted"] else "RECHAZADO",
                result["message"],
                pdf_url,
                pdf_filename,
                xml_url,
                xml_filename,
                result["cdr_url"],
                credit_note_id,
            ),
        )
        log_activity(
            "facturacion", "ENVIAR_NC",
            f"Nota de crédito {credit_note['number']} enviada a SUNAT — "
            f"{'ACEPTADO' if result['accepted'] else 'RECHAZADO'}",
            entity_type="nota_credito", entity_id=credit_note_id,
            entity_url=url_for("facturacion.credit_note_detail", credit_note_id=credit_note_id),
        )
        if result["accepted"]:
            if ya_existia:
                flash("Esta nota de crédito ya estaba aceptada por SUNAT.", "success")
            else:
                flash("Nota de crédito enviada y aceptada por SUNAT.", "success")
        else:
            flash(f"SUNAT/tefacturo.pe rechazó la nota de crédito: {result['message']}", "error")
    except SunatOseError as exc:
        execute(
            "UPDATE credit_notes SET sunat_status='ERROR', sunat_message=?, sunat_sent_at=datetime('now') WHERE id=?",
            (str(exc), credit_note_id),
        )
        log_activity(
            "facturacion", "ENVIAR_NC",
            f"Nota de crédito {credit_note['number']}: error al enviar a SUNAT — {exc}",
            entity_type="nota_credito", entity_id=credit_note_id,
            entity_url=url_for("facturacion.credit_note_detail", credit_note_id=credit_note_id),
        )
        flash(f"No se pudo enviar la nota de crédito: {exc}", "error")
    except Exception as exc:
        logger.exception(
            "Error inesperado al enviar la nota de crédito #%s a SUNAT", credit_note_id
        )
        execute(
            "UPDATE credit_notes SET sunat_status='ERROR', sunat_message=?, sunat_sent_at=datetime('now') WHERE id=?",
            (f"Error interno inesperado: {type(exc).__name__}: {exc}", credit_note_id),
        )
        log_activity(
            "facturacion", "ENVIAR_NC",
            f"Nota de crédito {credit_note['number']}: error interno inesperado al enviar a SUNAT — "
            f"{type(exc).__name__}: {exc}",
            entity_type="nota_credito", entity_id=credit_note_id,
            entity_url=url_for("facturacion.credit_note_detail", credit_note_id=credit_note_id),
        )
        flash(
            "No se pudo enviar la nota de crédito por un error interno inesperado (no fue un rechazo de "
            f"SUNAT): {type(exc).__name__}: {exc} — copia este mensaje y mándamelo para revisarlo.",
            "error",
        )

    return redirect(url_for("facturacion.credit_note_detail", credit_note_id=credit_note_id))


@bp.route("/notas-credito/<int:credit_note_id>/pdf-sunat")
@permission_required("facturacion", "view")
def view_credit_note_pdf(credit_note_id):
    """Mismo patrón que view_sunat_pdf() de más arriba (para la factura)."""
    credit_note = query_one(
        "SELECT sunat_pdf_filename, series, series_number FROM credit_notes WHERE id = ?",
        (credit_note_id,),
    )
    if credit_note is None or not credit_note["sunat_pdf_filename"]:
        abort(404)
    download_name = f"{credit_note['series']}-{credit_note['series_number']:06d}.pdf"
    if using_s3():
        return redirect(sunat_document_url(credit_note["sunat_pdf_filename"], download_name=download_name))
    return send_from_directory(
        local_sunat_documents_dir(), credit_note["sunat_pdf_filename"], download_name=download_name
    )


@bp.route("/notas-credito/<int:credit_note_id>/xml-sunat")
@permission_required("facturacion", "view")
def view_credit_note_xml(credit_note_id):
    """Mismo patrón que view_sunat_xml() de más arriba (para la factura)."""
    credit_note = query_one(
        "SELECT sunat_xml_filename, series, series_number FROM credit_notes WHERE id = ?",
        (credit_note_id,),
    )
    if credit_note is None or not credit_note["sunat_xml_filename"]:
        abort(404)
    download_name = f"{credit_note['series']}-{credit_note['series_number']:06d}.xml"
    if using_s3():
        return redirect(
            sunat_document_url(credit_note["sunat_xml_filename"], as_attachment=True, download_name=download_name)
        )
    return send_from_directory(
        local_sunat_documents_dir(),
        credit_note["sunat_xml_filename"],
        as_attachment=True,
        download_name=download_name,
    )
