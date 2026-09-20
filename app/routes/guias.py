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
from openpyxl import load_workbook

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import company_info_for_issuer, parse_date, parse_float, today_str
from app.integrations.sunat_ose import (
    SunatOseError,
    build_client_from_config,
    build_waybill_payload,
    is_duplicate_comprobante_error,
    parse_ose_response,
)
from app.routes.viajes import ISSUER_CHOICES
from app.storage import (
    local_sunat_documents_dir,
    save_sunat_document,
    sunat_document_url,
    using_s3,
)
from app.ubigeo import (
    DEPARTAMENTOS,
    DEPARTAMENTOS_CON_DISTRITOS_COMPLETOS,
    DISTRITOS,
    PROVINCIAS,
    validar_ubigeo,
)

bp = Blueprint("guias", __name__, url_prefix="/guias")

# 20 sep, pedido de Braulio: guías de BRMS a Backus o Naviera Oriente van
# enlazadas a un "número de pedido" que esos clientes mandan después de
# recibir la guía, para poder facturarles -- se identifica por nombre
# porque no hay un campo/booleano propio en Clientes para marcarlo; si el
# nombre de alguno de estos dos cambia en el catálogo, o se suma un tercer
# cliente con el mismo requisito, ajustar esta lista.
ORDER_NUMBER_CLIENTS = ("backus", "naviera oriente")


def _client_needs_order_number(issuer, client_name):
    if issuer != "BRMS" or not client_name:
        return False
    name = client_name.strip().lower()
    return any(keyword in name for keyword in ORDER_NUMBER_CLIENTS)


def _set_trip_order_number(trip_id, raw_value):
    execute(
        "UPDATE trips SET client_order_number = ? WHERE id = ?",
        ((raw_value or "").strip() or None, trip_id),
    )


# 10 sep, patch 0028: catálogo de ubigeos (departamento/provincia/distrito)
# para los desplegables en cascada del formulario — se arma una sola vez acá
# y se pasa tal cual al template (ver app/ubigeo.py para el alcance real del
# catálogo de distritos, completo solo en 9 de los 25 departamentos).
UBIGEO_CATALOG = {
    "departamentos": DEPARTAMENTOS,
    "provincias": PROVINCIAS,
    "distritos": DISTRITOS,
    "completos": sorted(DEPARTAMENTOS_CON_DISTRITOS_COMPLETOS),
}

# Catálogo SUNAT de motivo de traslado, confirmado en la documentación real
# de tefacturo.pe (7 sep, segunda ronda) — se muestra tal cual en el
# desplegable del formulario.
TRANSFER_REASONS = [
    ("VENTA", "Venta"),
    ("COMPRA", "Compra"),
    ("DEVOLUCION", "Devolución"),
    ("CONSIGNACION", "Consignación"),
    ("TRASLADO_ESTABLECIMINTOS", "Traslado entre establecimientos"),
    ("EXPORTACION", "Exportación"),
    ("IMPORTACION", "Importación"),
    ("OTROS", "Otros"),
]


def _next_series_number(series):
    row = query_one("SELECT COUNT(*) as n FROM waybills WHERE series = ?", (series,))
    return (row["n"] if row else 0) + 1


@bp.route("")
@permission_required("guias", "view")
def list_view():
    """20 sep, pedido de Braulio: mismo selector obligatorio de empresa que
    ya usan Viajes y Liquidaciones (ver el comentario largo en
    viajes.list_view) -- sin ?issuer=HARRASO|BRMS en la URL no se consulta
    ni se muestra ninguna guía, solo el selector (ver guias/list.html).
    También agrega un buscador por cliente o número de guía (y, para BRMS,
    también por número de pedido)."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template("guias/list.html", waybills=None, issuer=None)

    q = request.args.get("q", "").strip()
    sql = """SELECT w.*, t.code as trip_code, t.origin, t.destination, c.name as client_name,
                    t.client_order_number as client_order_number
             FROM waybills w
             JOIN trips t ON t.id = w.trip_id
             JOIN clients c ON c.id = t.client_id
             WHERE w.issuer = ?"""
    params = [issuer]
    if q:
        # LOWER() en ambos lados (patch 0066) -- ver el comentario completo
        # en clientes.list_view(). "w.series || '-' || w.series_number" no
        # queda con el mismo cero-relleno que se muestra en pantalla
        # (V001-6 en vez de V001-000006), pero alcanza para encontrar una
        # guía por su número tal como aparece en el PDF o en la búsqueda.
        sql += """ AND (LOWER(c.name) LIKE LOWER(?) OR LOWER(w.series || '-' || w.series_number) LIKE LOWER(?)
                    OR LOWER(COALESCE(t.client_order_number, '')) LIKE LOWER(?))"""
        params += [f"%{q}%"] * 3
    sql += " ORDER BY w.issue_date DESC, w.id DESC"

    waybills = query_all(sql, params)
    return render_template(
        "guias/list.html",
        waybills=waybills,
        issuer=issuer,
        q=q,
        needs_order_number=_client_needs_order_number,
    )


@bp.route("/nueva/<int:trip_id>", methods=["GET", "POST"])
@permission_required("guias", "edit")
def new(trip_id):
    trip = query_one(
        # 15 sep, pedido de Braulio: precargar también la placa de la
        # carreta (trailer_vehicle_id) -- ver la nota larga en
        # build_waybill_payload() sobre "vehículo secundario". c.name se
        # agrega solo para mostrarle a Braulio, en el formulario, quién es
        # el remitente (el cliente del viaje) -- no se guarda en la guía.
        """SELECT t.*, v.plate as vehicle_plate, tv.plate as trailer_plate, d.name as driver_name,
                  d.document_number as driver_document, d.license_number as driver_license,
                  c.name as client_name
           FROM trips t
           LEFT JOIN vehicles v ON v.id = t.vehicle_id
           LEFT JOIN vehicles tv ON tv.id = t.trailer_vehicle_id
           LEFT JOIN drivers d ON d.id = t.driver_id
           LEFT JOIN clients c ON c.id = t.client_id
           WHERE t.id = ?""",
        (trip_id,),
    )
    if trip is None:
        abort(404)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)

        # 10 sep, patch 0028: valida los ubigeos contra el catálogo real del
        # INEI/SUNAT ANTES de guardar — evita que un código inventado (como
        # "080000", que causó un error críptico de tefacturo.pe) llegue
        # siquiera a guardarse. Ver app/ubigeo.py para el alcance exacto del
        # catálogo (completo a nivel departamento/provincia; a nivel
        # distrito solo para 9 departamentos).
        origin_ubigeo = request.form.get("origin_ubigeo", "").strip()
        destination_ubigeo = request.form.get("destination_ubigeo", "").strip()
        ubigeo_error = validar_ubigeo(origin_ubigeo, "El ubigeo de partida") or validar_ubigeo(
            destination_ubigeo, "El ubigeo de llegada"
        )
        if ubigeo_error:
            flash(ubigeo_error, "error")
            return render_template(
                "guias/form.html",
                trip=trip,
                today=today_str(),
                transfer_reasons=TRANSFER_REASONS,
                form_values=request.form,
                ubigeo_catalog=UBIGEO_CATALOG,
            )

        series = current_app.config["WAYBILL_SERIES"]
        series_number = _next_series_number(series)
        # 14 sep, patch 0029: fecha de entrega (fechaEntrega, exigida por
        # tefacturo.pe — ver la nota en build_waybill_payload) — opcional en
        # el formulario, si se deja en blanco se usa la misma fecha de
        # emisión.
        issue_date_value = parse_date(request.form.get("issue_date")) or today_str()
        delivery_date_value = parse_date(request.form.get("delivery_date")) or issue_date_value
        # 15 sep, pedido de Braulio ("hay que especificar remitente,
        # destinatario, subcontratado, pagador"): igual que vehicle_plate/
        # trailer_plate/driver_*, estos campos son editables al crear la
        # guía y opcionales -- si se dejan en blanco, build_waybill_payload()
        # usa el cliente del viaje como remitente Y destinatario (mismo
        # comportamiento que antes de este patch). El pagador por defecto es
        # DESTINATARIO (el caso más común visto en una guía real aceptada).
        payer_type = (request.form.get("payer_type") or "DESTINATARIO").strip().upper()
        if payer_type not in ("REMITENTE", "DESTINATARIO", "TERCERO"):
            payer_type = "DESTINATARIO"
        waybill_id = execute(
            """INSERT INTO waybills (trip_id, series, series_number, issuer, issue_date, delivery_date,
               weight_kg, packages,
               origin_address, destination_address, origin_ubigeo, destination_ubigeo, transfer_reason,
               vehicle_plate, trailer_plate, driver_document, driver_name, driver_license,
               recipient_ruc, recipient_name, subcontractor_ruc, subcontractor_name,
               payer_type, payer_ruc, payer_name, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trip_id,
                series,
                series_number,
                # 7 sep, integración con tefacturo.pe: la guía se emite a
                # nombre de la misma empresa que el viaje (trips.issuer,
                # Harraso o BRMS) — se copia aquí y no se recalcula después.
                trip["issuer"],
                issue_date_value,
                delivery_date_value,
                parse_float(request.form.get("weight_kg"), None),
                int(parse_float(request.form.get("packages"), 1)),
                request.form.get("origin_address", "").strip() or trip["origin"],
                request.form.get("destination_address", "").strip() or trip["destination"],
                # 7 sep, segunda ronda: campos que exige el formato real de
                # tefacturo.pe (ver app/integrations/sunat_ose.py) — sin
                # ellos, "Enviar a SUNAT" rechaza la guía con un mensaje
                # claro, pero la guía se puede crear/guardar igual.
                request.form.get("origin_ubigeo", "").strip(),
                request.form.get("destination_ubigeo", "").strip(),
                request.form.get("transfer_reason") or "OTROS",
                request.form.get("vehicle_plate", "").strip() or trip["vehicle_plate"],
                # 15 sep, pedido de Braulio: placa de la carreta (vehículo
                # secundario) -- opcional (un viaje TERCERO o un camión
                # simple no tienen carreta).
                request.form.get("trailer_plate", "").strip() or trip["trailer_plate"],
                request.form.get("driver_document", "").strip() or trip["driver_document"],
                request.form.get("driver_name", "").strip() or trip["driver_name"],
                request.form.get("driver_license", "").strip() or trip["driver_license"],
                request.form.get("recipient_ruc", "").strip(),
                request.form.get("recipient_name", "").strip(),
                request.form.get("subcontractor_ruc", "").strip(),
                # Si el viaje es TERCERO, se precarga con trips.third_party_name
                # (no hay RUC guardado ahí todavía -- se pide a mano acá).
                request.form.get("subcontractor_name", "").strip() or (trip["third_party_name"] or ""),
                payer_type,
                request.form.get("payer_ruc", "").strip(),
                request.form.get("payer_name", "").strip(),
                request.form.get("notes", "").strip(),
                None,
            ),
        )
        flash(f"Guía {series}-{series_number:06d} creada.", "success")
        return redirect(url_for("guias.detail", waybill_id=waybill_id))

    return render_template(
        "guias/form.html",
        trip=trip,
        today=today_str(),
        transfer_reasons=TRANSFER_REASONS,
        ubigeo_catalog=UBIGEO_CATALOG,
    )


@bp.route("/<int:waybill_id>")
@permission_required("guias", "view")
def detail(waybill_id):
    waybill = query_one(
        """SELECT w.*, t.code as trip_code, t.origin, t.destination, t.cargo_description,
                  t.client_order_number as client_order_number,
                  c.name as client_name
           FROM waybills w
           JOIN trips t ON t.id = w.trip_id
           JOIN clients c ON c.id = t.client_id
           WHERE w.id = ?""",
        (waybill_id,),
    )
    if waybill is None:
        abort(404)
    return render_template(
        "guias/detail.html",
        waybill=waybill,
        needs_order_number=_client_needs_order_number(waybill["issuer"], waybill["client_name"]),
    )


@bp.route("/<int:waybill_id>/pedido", methods=["POST"])
@permission_required("guias", "edit")
def save_order_number(waybill_id):
    """20 sep, pedido de Braulio: número de pedido que Backus/Naviera
    Oriente mandan después de la guía (BRMS) para poder facturarles -- se
    guarda/edita acá, aparte del formulario de creación, porque llega
    después. Se guarda en trips.client_order_number (no en waybills, ver el
    comentario largo en schema.sql junto a esa columna), así que primero
    hay que resolver el trip_id de esta guía."""
    if not validate_csrf():
        abort(400)
    waybill = query_one("SELECT trip_id FROM waybills WHERE id = ?", (waybill_id,))
    if waybill is None:
        abort(404)
    _set_trip_order_number(waybill["trip_id"], request.form.get("client_order_number"))
    flash("Número de pedido guardado.", "success")
    return redirect(url_for("guias.detail", waybill_id=waybill_id))


# --- Enlazar facturas y guías (20 sep, pedido de Braulio): pantalla propia
# de BRMS que reúne TODAS las guías con las que un viaje puede haber salido
# -- la guía electrónica propia (waybills), la guía de transportista
# (trips.carrier_waybill_number, documento propio pero de texto libre) y la
# guía del remitente cuando ya trae nuestros datos y no hace falta emitir
# una nueva (trips.shipper_waybill_number, caso "remisión") -- y permite
# enlazar el número de pedido de cada una a mano o en bloque, subiendo el
# Excel semanal que manda Naviera Oriente ("DETALLE DE VIAJE BRMS.xlsx":
# columna D "Doc. de compras" = pedido, columna G "Guía Transp." = guía,
# confirmado con Braulio).

# Valores de la columna "Guía Transp." que no son una guía real y se
# ignoran solos al importar (un viaje "sin flete real" en el sistema de
# Naviera Oriente, visto en el archivo real que compartió Braulio).
IGNORED_GUIA_VALUES = {"", "FALSO FLETE", "N/A", "-"}


def _cell_text(value):
    """openpyxl a veces trae un número como float (5900090082.0) en vez de
    int -- lo pasamos a texto sin ese ".0" para que calce con lo que
    escribiría una persona a mano."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_naviera_orders_excel(file_storage):
    """Lee el Excel semanal de Naviera Oriente y devuelve una lista de
    (fila del Excel, pedido, guía), o (None, mensaje de error).

    OJO: este archivo NO es una plantilla nuestra (a diferencia de
    app/bulk_import.py, que sí genera y espera su propio formato con
    encabezado en una fila fija) -- es un reporte de su propio sistema, con
    el bloque de encabezado ("PROVEEDOR"...) repetido cada tanto y una fila
    "Total general" al cerrar cada bloque, en vez de una sola tabla
    continua. Por eso se recorren TODAS las filas de TODAS las hojas,
    descartando encabezados/totales/filas sin guía o sin pedido, en vez de
    asumir una posición fija."""
    try:
        wb = load_workbook(file_storage, data_only=True, read_only=True)
    except Exception:
        return None, (
            "No se pudo leer el archivo. Confirma que sea el Excel (.xlsx) tal como lo manda Naviera Oriente."
        )

    rows_out = []
    for ws in wb.worksheets:
        for excel_row_num, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if not row or len(row) < 7:
                continue
            col_a = _cell_text(row[0]).upper()
            if col_a in ("PROVEEDOR", "TOTAL GENERAL"):
                continue
            pedido = _cell_text(row[3])
            guia = _cell_text(row[6])
            if not pedido or not guia or guia.upper() in IGNORED_GUIA_VALUES:
                continue
            rows_out.append((excel_row_num, pedido, guia))

    if not rows_out:
        return None, 'No se encontraron filas con "Doc. de compras" y "Guía Transp." en el archivo.'
    return rows_out, None


def _index_guia_values(pairs):
    """Arma {valor de guía en minúscula: trip_id} a partir de (valor, trip_id)
    -- si un mismo valor aparece en más de un viaje se descarta de este
    índice (ambiguo) en vez de quedarse con cualquiera de los dos al azar,
    para no enlazar un pedido al viaje equivocado."""
    index = {}
    ambiguous = set()
    for value, trip_id in pairs:
        key = (value or "").strip().lower()
        if not key:
            continue
        if key in index and index[key] != trip_id:
            ambiguous.add(key)
        else:
            index[key] = trip_id
    for key in ambiguous:
        index.pop(key, None)
    return index


def _brms_trip_guide_index():
    """Tres índices {guía en minúscula: trip_id} para los viajes de BRMS:
    por guía de transportista (texto libre), por guía electrónica propia
    (serie-número, con el mismo cero-relleno que se muestra en pantalla) y
    por guía del remitente (caso remisión). Se buscan en ese orden porque
    "Guía Transp." de Naviera Oriente suele calzar con la guía de
    transportista de texto libre, no con la electrónica."""
    trips = query_all(
        "SELECT id, carrier_waybill_number, shipper_waybill_number FROM trips WHERE issuer = 'BRMS'"
    )
    carrier_index = _index_guia_values((t["carrier_waybill_number"], t["id"]) for t in trips)
    shipper_index = _index_guia_values((t["shipper_waybill_number"], t["id"]) for t in trips)

    waybills = query_all(
        """SELECT w.trip_id, w.series, w.series_number FROM waybills w
           JOIN trips t ON t.id = w.trip_id WHERE t.issuer = 'BRMS'"""
    )
    electronic_index = _index_guia_values(
        (f"{w['series']}-{w['series_number']:06d}", w["trip_id"]) for w in waybills
    )
    return carrier_index, electronic_index, shipper_index


@bp.route("/enlazar-pedidos")
@permission_required("guias", "view")
def link_orders():
    q = request.args.get("q", "").strip()
    sql = """SELECT t.id as trip_id, t.code as trip_code, t.origin, t.destination,
                    t.client_order_number, t.carrier_waybill_number,
                    t.shipper_waybill_shows_carrier, t.shipper_waybill_number,
                    c.name as client_name,
                    w.id as waybill_id, w.series, w.series_number, w.sunat_status
             FROM trips t
             JOIN clients c ON c.id = t.client_id
             LEFT JOIN waybills w ON w.trip_id = t.id
             WHERE t.issuer = 'BRMS'
               AND (
                     w.id IS NOT NULL
                     OR t.shipper_waybill_shows_carrier = 'SI'
                     OR (t.carrier_waybill_number IS NOT NULL AND t.carrier_waybill_number != '')
                   )"""
    params = []
    if q:
        sql += """ AND (
                     LOWER(c.name) LIKE LOWER(?)
                     OR LOWER(COALESCE(t.carrier_waybill_number, '')) LIKE LOWER(?)
                     OR LOWER(COALESCE(t.shipper_waybill_number, '')) LIKE LOWER(?)
                     OR LOWER(COALESCE(t.client_order_number, '')) LIKE LOWER(?)
                     OR LOWER(COALESCE(w.series || '-' || w.series_number, '')) LIKE LOWER(?)
                   )"""
        params += [f"%{q}%"] * 5
    sql += " ORDER BY t.scheduled_date DESC, t.id DESC"

    trips = query_all(sql, params)
    return render_template(
        "guias/link_orders.html", trips=trips, q=q, needs_order_number=_client_needs_order_number
    )


@bp.route("/enlazar-pedidos/viajes/<int:trip_id>", methods=["POST"])
@permission_required("guias", "edit")
def save_trip_order_number(trip_id):
    if not validate_csrf():
        abort(400)
    trip = query_one("SELECT id FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    _set_trip_order_number(trip_id, request.form.get("client_order_number"))
    flash("Número de pedido guardado.", "success")
    q = request.form.get("q", "").strip()
    return redirect(url_for("guias.link_orders", q=q) if q else url_for("guias.link_orders"))


@bp.route("/enlazar-pedidos/cargar", methods=["POST"])
@permission_required("guias", "edit")
def link_orders_upload():
    if not validate_csrf():
        abort(400)
    file_storage = request.files.get("file")
    if not file_storage or not file_storage.filename:
        flash("Selecciona el archivo Excel de Naviera Oriente para cargar.", "error")
        return redirect(url_for("guias.link_orders"))

    rows, file_error = _parse_naviera_orders_excel(file_storage)
    if file_error:
        flash(file_error, "error")
        return redirect(url_for("guias.link_orders"))

    carrier_index, electronic_index, shipper_index = _brms_trip_guide_index()
    not_found = []
    updates = {}
    for excel_row, pedido, guia in rows:
        key = guia.strip().lower()
        trip_id = carrier_index.get(key) or electronic_index.get(key) or shipper_index.get(key)
        if trip_id is None:
            not_found.append(
                {
                    "row": excel_row,
                    "message": f'Guía "{guia}" (pedido {pedido}) no se encontró entre los viajes de BRMS.',
                }
            )
            continue
        updates[trip_id] = pedido

    for trip_id, pedido in updates.items():
        execute("UPDATE trips SET client_order_number = ? WHERE id = ?", (pedido, trip_id))

    result = {"linked": len(updates), "not_found": not_found}
    return render_template("guias/link_orders_result.html", result=result)


@bp.route("/<int:waybill_id>/enviar-sunat", methods=["POST"])
@permission_required("guias", "edit")
def send_sunat(waybill_id):
    if not validate_csrf():
        abort(400)
    waybill = query_one("SELECT * FROM waybills WHERE id = ?", (waybill_id,))
    if waybill is None:
        abort(404)
    trip = query_one("SELECT * FROM trips WHERE id = ?", (waybill["trip_id"],))
    # El "remitente"/"destinatario" del formato real de tefacturo.pe es el
    # cliente del viaje (ver la nota de "SIMPLIFICACIONES" en
    # app/integrations/sunat_ose.py) — se necesita su RUC/correo/dirección.
    client_row = query_one("SELECT * FROM clients WHERE id = ?", (trip["client_id"],))

    ose_client = build_client_from_config(current_app.config, waybill["issuer"])
    company = company_info_for_issuer(waybill["issuer"], current_app.config)

    try:
        if not company["ruc"]:
            raise SunatOseError(
                f"Falta configurar el RUC de {company['name']} (variable de entorno "
                f"{'BRMS_RUC' if waybill['issuer'] == 'BRMS' else 'COMPANY_RUC'}) antes de "
                "poder emitir guías electrónicas a su nombre."
            )
        payload = build_waybill_payload(waybill, trip, company, client_row)
        ya_existia = False
        try:
            response = ose_client.emit_guia_transportista(payload)
            result = parse_ose_response(response)
        except SunatOseError as emit_exc:
            # Mismo caso que en facturacion.send_sunat() (ver ese comentario
            # y is_duplicate_comprobante_error en sunat_ose.py): reenviar
            # una guía que tefacturo.pe ya tiene registrada no es un
            # rechazo nuevo — no hay que pisar un estado ACEPTADO ya
            # guardado con ERROR.
            if is_duplicate_comprobante_error(emit_exc):
                ya_existia = True
                result = {
                    "accepted": True,
                    "message": waybill["sunat_message"]
                    or "Aceptado por SUNAT (comprobante ya registrado en un envío anterior).",
                    "xml_url": waybill["sunat_xml_url"],
                    "cdr_url": waybill["sunat_cdr_url"],
                }
            else:
                raise

        pdf_filename = waybill["sunat_pdf_filename"]
        pdf_url = waybill["sunat_pdf_url"]
        if result["accepted"]:
            try:
                # 14 sep, patch 0033: '31' = Guía de Remisión TRANSPORTISTA
                # (Catálogo No. 01 SUNAT) — '09' es la guía REMITENTE, un
                # documento distinto que este sistema no emite. Confirmado
                # por un 404 real de tefacturo.pe ("No se encontró el tipo:
                # 09 del comprobante...") en una guía que sí quedó ACEPTADA.
                pdf_bytes = ose_client.get_pdf_bytes("31", waybill["series"], waybill["series_number"])
                pdf_filename = f"guia-{waybill_id}-{uuid.uuid4().hex}.pdf"
                save_sunat_document(pdf_filename, pdf_bytes)
                pdf_url = url_for("guias.view_sunat_pdf", waybill_id=waybill_id)
            except SunatOseError as pdf_exc:
                # La guía SÍ quedó aceptada por SUNAT — no descartar eso
                # solo porque no se pudo descargar/guardar el PDF. Se avisa
                # aparte y se puede reintentar la descarga más adelante si
                # hiciera falta (no implementado todavía: solo se descarga
                # automáticamente justo después de emitir).
                flash(f"La guía se aceptó, pero no se pudo descargar su PDF: {pdf_exc}", "error")

        execute(
            """UPDATE waybills SET sunat_status=?, sunat_message=?, sunat_pdf_url=?, sunat_pdf_filename=?,
               sunat_xml_url=?, sunat_cdr_url=?, sunat_sent_at=datetime('now') WHERE id=?""",
            (
                "ACEPTADO" if result["accepted"] else "RECHAZADO",
                result["message"],
                pdf_url,
                pdf_filename,
                result["xml_url"],
                result["cdr_url"],
                waybill_id,
            ),
        )
        if result["accepted"]:
            if ya_existia:
                flash("Esta guía ya estaba aceptada por SUNAT.", "success")
            else:
                flash("Guía enviada y aceptada por SUNAT.", "success")
        else:
            flash(f"SUNAT/tefacturo.pe rechazó la guía: {result['message']}", "error")
    except SunatOseError as exc:
        execute(
            "UPDATE waybills SET sunat_status='ERROR', sunat_message=?, sunat_sent_at=datetime('now') WHERE id=?",
            (str(exc), waybill_id),
        )
        flash(f"No se pudo enviar la guía: {exc}", "error")

    return redirect(url_for("guias.detail", waybill_id=waybill_id))


@bp.route("/<int:waybill_id>/pdf-sunat")
@permission_required("guias", "view")
def view_sunat_pdf(waybill_id):
    """Sirve el PDF real que devolvió tefacturo.pe al emitir esta guía (7
    sep, segunda ronda) — mismo patrón que las demás descargas de archivos
    del sistema (disco local o redirect a URL firmada en S3)."""
    waybill = query_one("SELECT sunat_pdf_filename FROM waybills WHERE id = ?", (waybill_id,))
    if waybill is None or not waybill["sunat_pdf_filename"]:
        abort(404)
    if using_s3():
        return redirect(sunat_document_url(waybill["sunat_pdf_filename"]))
    return send_from_directory(local_sunat_documents_dir(), waybill["sunat_pdf_filename"])
