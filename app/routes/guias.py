import json
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

from app.audit import get_creator_info, log_activity
from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
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


SUNAT_STATUS_CHOICES = ("NO_ENVIADA", "ACEPTADO", "RECHAZADO", "ERROR")


@bp.route("")
@permission_required("guias", "view")
def list_view():
    """20 sep, pedido de Braulio: mismo selector obligatorio de empresa que
    ya usan Viajes y Liquidaciones (ver el comentario largo en
    viajes.list_view) -- sin ?issuer=HARRASO|BRMS en la URL no se consulta
    ni se muestra ninguna guía, solo el selector (ver guias/list.html).
    También agrega un buscador por cliente o número de guía (y, para BRMS,
    también por número de pedido).

    20 sep, pedido de Braulio ("ver todas las guías emitidas desde el
    portal SUNAT en lo que va del año"): se suman dos filtros opcionales,
    Año (sobre issue_date, comparando solo los primeros 4 caracteres --
    funciona igual en SQLite y Postgres sin tocar `_translate` en db.py,
    a diferencia de strftime) y Estado SUNAT. "Emitida desde el portal
    SUNAT" = sunat_status = 'ACEPTADO' (aceptada por SUNAT); NO_ENVIADA
    todavía es un borrador que no se mandó. Ninguno de los dos viene
    marcado por default -- así no cambia lo que ya se veía antes (todas
    las guías de la empresa, sin filtrar) para quien solo busca una guía
    puntual."""
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template("guias/list.html", waybills=None, issuer=None)

    q = request.args.get("q", "").strip()
    anio = request.args.get("anio", "").strip()
    estado = request.args.get("estado", "").strip().upper()

    year_rows = query_all(
        """SELECT DISTINCT substr(issue_date, 1, 4) as anio FROM waybills
           WHERE issuer = ? AND issue_date IS NOT NULL AND issue_date != ''
           ORDER BY anio DESC""",
        (issuer,),
    )
    available_years = [r["anio"] for r in year_rows if r["anio"]]
    current_year = today_str()[:4]
    if current_year not in available_years:
        available_years.insert(0, current_year)

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
    if anio:
        sql += " AND substr(w.issue_date, 1, 4) = ?"
        params.append(anio)
    if estado in SUNAT_STATUS_CHOICES:
        sql += " AND w.sunat_status = ?"
        params.append(estado)
    sql += " ORDER BY w.issue_date DESC, w.id DESC"

    waybills = query_all(sql, params)
    return render_template(
        "guias/list.html",
        waybills=waybills,
        issuer=issuer,
        q=q,
        anio=anio,
        estado=estado,
        available_years=available_years,
        sunat_status_choices=SUNAT_STATUS_CHOICES,
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
        # 22 sep, registro de actividad (ver app/audit.py): "genero guias" es
        # exactamente el ejemplo que dio Braulio al pedir esto -- se registra
        # como GENERAR (no CREAR) porque es un documento que se emite, no un
        # registro cualquiera que se da de alta.
        log_activity(
            "guias", "GENERAR", f"Guía {series}-{series_number:06d} del viaje {trip['code']}",
            entity_type="guia", entity_id=waybill_id,
            entity_url=url_for("guias.detail", waybill_id=waybill_id),
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
    # 22 sep, pedido de Braulio ("que usuario... genero guias"): quién y
    # cuándo se generó, según activity_log (ver app/audit.py) -- None para
    # guías de antes de que existiera este registro.
    creator = get_creator_info("guia", waybill_id)
    return render_template(
        "guias/detail.html",
        waybill=waybill,
        needs_order_number=_client_needs_order_number(waybill["issuer"], waybill["client_name"]),
        creator=creator,
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
    waybill = query_one("SELECT trip_id, series, series_number FROM waybills WHERE id = ?", (waybill_id,))
    if waybill is None:
        abort(404)
    _set_trip_order_number(waybill["trip_id"], request.form.get("client_order_number"))
    log_activity(
        "guias", "EDITAR",
        f"Guía {waybill['series']}-{waybill['series_number']:06d}: número de pedido actualizado",
        entity_type="guia", entity_id=waybill_id,
        entity_url=url_for("guias.detail", waybill_id=waybill_id),
    )
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
    trip = query_one("SELECT id, code FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        abort(404)
    _set_trip_order_number(trip_id, request.form.get("client_order_number"))
    # 22 sep, registro de actividad: acá el registro afectado es el propio
    # viaje (trips.client_order_number), no una guía -- entity_type="viaje"
    # aunque la acción se haga desde el módulo Guías (pantalla "Enlazar
    # pedidos").
    log_activity(
        "guias", "EDITAR", f"Viaje {trip['code']}: número de pedido actualizado (enlazar pedidos)",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )
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

    # 22 sep, registro de actividad: una sola entrada para todo el lote (no
    # una por viaje) -- son varios registros a la vez, igual que cualquier
    # otra carga masiva del sistema; sin entity_type/entity_id porque no hay
    # un único registro al que apunte.
    if updates:
        log_activity(
            "guias", "EDITAR",
            f"Enlace masivo de pedidos (Excel Naviera Oriente): {len(updates)} viaje(s) actualizado(s)",
            entity_url=url_for("guias.link_orders"),
        )

    result = {"linked": len(updates), "not_found": not_found}
    return render_template("guias/link_orders_result.html", result=result)


# 20 sep, pedido de Braulio ("y si las guías fueron emitidas por
# tefacturo.pe, pero antes de que se cree harris?"): pantalla de solo
# consulta para las guías reales que Harris nunca generó porque el sistema
# no existía todavía. Se cargan desde el Excel "Lista de Guías
# Transportistas" que exporta el propio panel de tefacturo.pe (uno por
# RUC/empresa) -- ver el comentario largo en schema.sql, tabla
# sunat_waybills_history, sobre por qué esto NO crea un `trip`.

# Columnas del export de tefacturo.pe que de verdad se usan (confirmado
# contra el archivo real de Harraso, 20 sep -- el resto de columnas del
# archivo, ~63 en total, no hace falta leerlas). "NÚMERO DOCUMENTO
# RELACIONADO" también aparece en una fila de continuación (ver abajo), por
# eso se busca en ambas.
_HISTORY_REQUIRED_HEADERS = ("TIPO", "SERIE", "NUMERO", "FECHA EMISION", "RUC TRANSPORTISTA")


def _history_header_map(ws):
    """Primera fila del Excel -> {NOMBRE EN MAYÚSCULA: índice 0-based}. El
    export de tefacturo.pe repite un par de nombres de columna (p.ej.
    "NÚMERO AUTORIZACIÓN" sale dos veces) -- no importa porque esas no se
    usan acá; para las que sí interesan, el nombre es único."""
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if not header_row:
        return None, "El archivo está vacío."
    header = {}
    for idx, value in enumerate(header_row):
        name = _cell_text(value).upper()
        if name:
            header[name] = idx
    missing = [h for h in _HISTORY_REQUIRED_HEADERS if h not in header]
    if missing:
        return None, (
            "No se reconoce el formato del archivo -- faltan columnas: "
            + ", ".join(missing)
            + '. Debe ser el Excel "Lista de Guías Transportistas" tal como lo exporta tefacturo.pe.'
        )
    return header, None


# ESTADO de tefacturo.pe (columna numérica): 1 = aceptado por SUNAT, 3 =
# rechazado (en el archivo real de Harraso, casi siempre porque la
# GRE-Remitente ya traía nuestros datos y no hacía falta la de
# transportista) -- cualquier otro valor (o ninguno) se guarda como OTRO;
# "RESPUESTA DECLARACION" trae el detalle en texto para ese caso.
_HISTORY_ESTADO_MAP = {"1": "ACEPTADO", "3": "RECHAZADO"}


def _parse_tefacturo_history_excel(file_storage):
    """Lee el Excel "Lista de Guías Transportistas" de tefacturo.pe y
    devuelve (lista de dicts, None) o (None, mensaje de error).

    Cada guía ocupa su fila principal (columna TIPO llena) y, a veces, una o
    más filas de continuación inmediatamente debajo (TIPO vacío) con datos
    que no entran en una sola fila -- vistas en el archivo real: un segundo
    vehículo/tarjeta de circulación (placa de la carreta) y, más raro, un
    segundo documento relacionado. Se toma el primer vehículo de
    continuación como `trailer_plate` (mismo campo que ya usa Harris) y el
    resto de filas de continuación se guarda tal cual en `raw_extra_json`,
    por si hace falta consultarlo después."""
    try:
        wb = load_workbook(file_storage, data_only=True, read_only=True)
    except Exception:
        return None, (
            'No se pudo leer el archivo. Confirma que sea el Excel "Lista de Guías '
            "Transportistas\" tal como lo exporta tefacturo.pe."
        )
    ws = wb.worksheets[0]
    header, error = _history_header_map(ws)
    if error:
        return None, error

    def cell(row, name):
        idx = header.get(name)
        return row[idx] if idx is not None and idx < len(row) else None

    docs = []
    current = None
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row:
            continue
        tipo = _cell_text(cell(row, "TIPO"))
        if tipo:
            series = _cell_text(cell(row, "SERIE"))
            numero_raw = _cell_text(cell(row, "NUMERO"))
            fecha_raw = _cell_text(cell(row, "FECHA EMISION"))
            ruc = _cell_text(cell(row, "RUC TRANSPORTISTA"))
            if not (series and numero_raw and fecha_raw and ruc):
                current = None
                continue
            try:
                numero = int(float(numero_raw))
            except ValueError:
                current = None
                continue
            packages_raw = cell(row, "NUMERO PAQUETES TRASLADO")
            packages = None
            if packages_raw not in (None, ""):
                try:
                    packages = int(float(packages_raw))
                except (TypeError, ValueError):
                    packages = None
            current = {
                "series": series,
                "series_number": numero,
                "issue_date": fecha_raw[:10],
                "ruc_transportista": ruc,
                "sunat_status": _HISTORY_ESTADO_MAP.get(_cell_text(cell(row, "ESTADO")), "OTRO"),
                "sunat_status_detail": _cell_text(cell(row, "RESPUESTA DECLARACION")) or None,
                "client_document": _cell_text(cell(row, "NUMERO DOCUMENTO REMITENTE")) or None,
                "client_name": _cell_text(cell(row, "RAZON SOCIAL REMITENTE")) or None,
                "recipient_document": _cell_text(cell(row, "NUMERO DOCUMENTO DESTINATARIO")) or None,
                "recipient_name": _cell_text(cell(row, "RAZON SOCIAL DESTINATARIO")) or None,
                "origin_address": _cell_text(cell(row, "DIRECCION PUNTO PARTIDA")) or None,
                "origin_ubigeo": _cell_text(cell(row, "UBIGEO PUNTO PARTIDA")) or None,
                "destination_address": _cell_text(cell(row, "DIRECCION PUNTO LLEGADA")) or None,
                "destination_ubigeo": _cell_text(cell(row, "UBIGEO PUNTO LLEGADA")) or None,
                "weight_kg": parse_float(cell(row, "PESO BRUTO TOTAL TRASLADO"), None),
                "packages": packages,
                "cargo_description": _cell_text(cell(row, "DESCRIPCION TRASLADO")) or None,
                "driver_document": _cell_text(cell(row, "NÚMERO DOCUMENTO CONDUCTOR")) or None,
                "driver_name": _cell_text(cell(row, "NOMBRE COMPLETO CONDUCTOR")) or None,
                "driver_license": _cell_text(cell(row, "LICENCIA DE CONDUCIR")) or None,
                "vehicle_plate": _cell_text(cell(row, "PLACA VEHÍCULO")) or None,
                "trailer_plate": None,
                "related_document_type": _cell_text(cell(row, "TIPO DOCUMENTO RELACIONADO")) or None,
                "related_document_number": _cell_text(cell(row, "NÚMERO DOCUMENTO RELACIONADO")) or None,
                "related_document_series": _cell_text(cell(row, "SERIE DOCUMENTO RELACIONADO")) or None,
                "_extra": [],
            }
            docs.append(current)
            continue

        if current is None:
            continue
        plate = _cell_text(cell(row, "PLACA VEHÍCULO")) or None
        tarjeta = _cell_text(cell(row, "TARJETA CIRCULACIÓN")) or None
        rel_num = _cell_text(cell(row, "NÚMERO DOCUMENTO RELACIONADO")) or None
        if not (plate or tarjeta or rel_num):
            continue
        if plate and not current["trailer_plate"]:
            current["trailer_plate"] = plate
        else:
            current["_extra"].append(
                {
                    "vehicle_plate": plate,
                    "tarjeta_circulacion": tarjeta,
                    "related_document_type": _cell_text(cell(row, "TIPO DOCUMENTO RELACIONADO")) or None,
                    "related_document_number": rel_num,
                    "related_document_series": _cell_text(cell(row, "SERIE DOCUMENTO RELACIONADO")) or None,
                }
            )

    if not docs:
        return None, "No se encontró ninguna guía reconocible en el archivo."
    return docs, None


def _issuer_for_ruc(ruc):
    """HARRASO o BRMS según con qué RUC configurado (COMPANY_RUC/BRMS_RUC)
    coincida la fila del Excel -- default HARRASO si no calza con ninguno
    (mismo criterio que company_info_for_issuer en app/helpers.py)."""
    cfg = current_app.config
    if ruc and cfg.get("BRMS_RUC") and ruc == cfg.get("BRMS_RUC"):
        return "BRMS"
    return "HARRASO"


@bp.route("/historico-sunat")
@permission_required("guias", "view")
def sunat_history_list():
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template("guias/sunat_history.html", rows=None, issuer=None)

    q = request.args.get("q", "").strip()
    anio = request.args.get("anio", "").strip()
    mes = request.args.get("mes", "").strip()
    estado = request.args.get("estado", "").strip().upper()

    year_rows = query_all(
        """SELECT DISTINCT substr(issue_date, 1, 4) as anio FROM sunat_waybills_history
           WHERE issuer = ? ORDER BY anio DESC""",
        (issuer,),
    )
    available_years = [r["anio"] for r in year_rows if r["anio"]]

    sql = "SELECT * FROM sunat_waybills_history WHERE issuer = ?"
    params = [issuer]
    if q:
        sql += """ AND (LOWER(COALESCE(client_name, '')) LIKE LOWER(?)
                    OR LOWER(series || '-' || series_number) LIKE LOWER(?))"""
        params += [f"%{q}%"] * 2
    if anio:
        sql += " AND substr(issue_date, 1, 4) = ?"
        params.append(anio)
    # 20 sep, pedido de Braulio ("aparte de año también pueda haber mes"):
    # igual que el filtro de año, comparando texto (substr) en vez de
    # strftime -- funciona igual en SQLite y Postgres sin tocar `_translate`
    # en db.py. mes viene como '01'..'12' desde el <select> del template.
    if mes in (f"{n:02d}" for n in range(1, 13)):
        sql += " AND substr(issue_date, 6, 2) = ?"
        params.append(mes)
    if estado in ("ACEPTADO", "RECHAZADO", "OTRO"):
        sql += " AND sunat_status = ?"
        params.append(estado)
    sql += " ORDER BY issue_date DESC, series_number DESC"

    rows = query_all(sql, params)
    return render_template(
        "guias/sunat_history.html",
        rows=rows,
        issuer=issuer,
        q=q,
        anio=anio,
        mes=mes,
        estado=estado,
        available_years=available_years,
    )


@bp.route("/historico-sunat/cargar", methods=["POST"])
@permission_required("guias", "edit")
def sunat_history_upload():
    if not validate_csrf():
        abort(400)
    issuer = request.form.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        issuer = "HARRASO"
    file_storage = request.files.get("file")
    if not file_storage or not file_storage.filename:
        flash('Selecciona el Excel "Lista de Guías Transportistas" de tefacturo.pe.', "error")
        return redirect(url_for("guias.sunat_history_list", issuer=issuer))

    docs, file_error = _parse_tefacturo_history_excel(file_storage)
    if file_error:
        flash(file_error, "error")
        return redirect(url_for("guias.sunat_history_list", issuer=issuer))

    existing = query_all("SELECT ruc_transportista, series, series_number FROM sunat_waybills_history")
    existing_keys = {(r["ruc_transportista"], r["series"], r["series_number"]) for r in existing}

    # El archivo real de tefacturo.pe es por RUC (una sola empresa a la
    # vez), pero por si acaso se calcula la empresa fila por fila en vez de
    # asumirla una sola vez -- `detected_issuer` (para saber a dónde
    # redirigir) queda con la del primer documento del archivo.
    detected_issuer = _issuer_for_ruc(docs[0]["ruc_transportista"])
    db = get_db()
    inserted = 0
    skipped = 0
    for doc in docs:
        key = (doc["ruc_transportista"], doc["series"], doc["series_number"])
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)
        row_issuer = _issuer_for_ruc(doc["ruc_transportista"])
        db.execute(
            """INSERT OR IGNORE INTO sunat_waybills_history (
                   issuer, ruc_transportista, series, series_number, issue_date,
                   sunat_status, sunat_status_detail, client_document, client_name,
                   recipient_document, recipient_name, origin_address, origin_ubigeo,
                   destination_address, destination_ubigeo, weight_kg, packages,
                   cargo_description, driver_document, driver_name, driver_license,
                   vehicle_plate, trailer_plate, related_document_type,
                   related_document_number, related_document_series, raw_extra_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_issuer,
                doc["ruc_transportista"],
                doc["series"],
                doc["series_number"],
                doc["issue_date"],
                doc["sunat_status"],
                doc["sunat_status_detail"],
                doc["client_document"],
                doc["client_name"],
                doc["recipient_document"],
                doc["recipient_name"],
                doc["origin_address"],
                doc["origin_ubigeo"],
                doc["destination_address"],
                doc["destination_ubigeo"],
                doc["weight_kg"],
                doc["packages"],
                doc["cargo_description"],
                doc["driver_document"],
                doc["driver_name"],
                doc["driver_license"],
                doc["vehicle_plate"],
                doc["trailer_plate"],
                doc["related_document_type"],
                doc["related_document_number"],
                doc["related_document_series"],
                json.dumps(doc["_extra"], ensure_ascii=False) if doc["_extra"] else None,
            ),
        )
        inserted += 1
    db.commit()

    # 22 sep, registro de actividad: se sube un archivo (Excel de
    # tefacturo.pe), por eso SUBIR y no GENERAR -- estas guías ya existían de
    # verdad en SUNAT antes de Harris, solo se están cargando al histórico
    # (ver el comentario largo más arriba sobre sunat_waybills_history).
    if inserted:
        log_activity(
            "guias", "SUBIR",
            f"Histórico SUNAT ({detected_issuer}): {inserted} guía(s) cargada(s) desde Excel de tefacturo.pe"
            + (f", {skipped} ya existían" if skipped else ""),
            entity_url=url_for("guias.sunat_history_list", issuer=detected_issuer),
        )
        flash(
            f"Se cargaron {inserted} guía(s) nueva(s) al histórico SUNAT."
            + (f" {skipped} ya estaban cargadas (se omitieron)." if skipped else ""),
            "success",
        )
    else:
        flash(
            f"No se cargó ninguna guía nueva -- las {skipped} del archivo ya estaban en el histórico.",
            "info",
        )
    return redirect(url_for("guias.sunat_history_list", issuer=detected_issuer))


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
        xml_filename = waybill["sunat_xml_filename"]
        xml_url = waybill["sunat_xml_url"]
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
            # 24 sep, pedido de Braulio ("ya funciona genera el pdf, pero
            # para descargar el xml?") -- mismo patrón que el PDF de arriba,
            # con el mismo tipoComprobante '31' (Guía TRANSPORTISTA).
            try:
                xml_bytes = ose_client.get_xml_bytes("31", waybill["series"], waybill["series_number"])
                xml_filename = f"guia-{waybill_id}-{uuid.uuid4().hex}.xml"
                save_sunat_document(xml_filename, xml_bytes)
                xml_url = url_for("guias.view_sunat_xml", waybill_id=waybill_id)
            except SunatOseError as xml_exc:
                flash(f"La guía se aceptó, pero no se pudo descargar su XML: {xml_exc}", "error")

        execute(
            """UPDATE waybills SET sunat_status=?, sunat_message=?, sunat_pdf_url=?, sunat_pdf_filename=?,
               sunat_xml_url=?, sunat_xml_filename=?, sunat_cdr_url=?, sunat_sent_at=datetime('now') WHERE id=?""",
            (
                "ACEPTADO" if result["accepted"] else "RECHAZADO",
                result["message"],
                pdf_url,
                pdf_filename,
                xml_url,
                xml_filename,
                result["cdr_url"],
                waybill_id,
            ),
        )
        # 22 sep, registro de actividad: ENVIAR es una acción distinta de
        # GENERAR (crear la guía) -- se puede reenviar la misma guía varias
        # veces (ver "ya_existia" arriba), y cada intento queda registrado
        # con el resultado real (aceptado/rechazado).
        log_activity(
            "guias", "ENVIAR",
            f"Guía {waybill['series']}-{waybill['series_number']:06d}: envío a SUNAT → "
            + ("ACEPTADO" if result["accepted"] else "RECHAZADO"),
            entity_type="guia", entity_id=waybill_id,
            entity_url=url_for("guias.detail", waybill_id=waybill_id),
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
        log_activity(
            "guias", "ENVIAR",
            f"Guía {waybill['series']}-{waybill['series_number']:06d}: envío a SUNAT → ERROR ({exc})",
            entity_type="guia", entity_id=waybill_id,
            entity_url=url_for("guias.detail", waybill_id=waybill_id),
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


@bp.route("/<int:waybill_id>/xml-sunat")
@permission_required("guias", "view")
def view_sunat_xml(waybill_id):
    """Sirve el XML firmado real que devolvió tefacturo.pe al emitir esta
    guía (24 sep, pedido de Braulio) — mismo patrón que view_sunat_pdf()
    de arriba."""
    waybill = query_one("SELECT sunat_xml_filename FROM waybills WHERE id = ?", (waybill_id,))
    if waybill is None or not waybill["sunat_xml_filename"]:
        abort(404)
    if using_s3():
        return redirect(sunat_document_url(waybill["sunat_xml_filename"]))
    return send_from_directory(local_sunat_documents_dir(), waybill["sunat_xml_filename"])
