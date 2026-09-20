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
    sql = """SELECT w.*, t.code as trip_code, t.origin, t.destination, c.name as client_name
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
                    OR LOWER(COALESCE(w.client_order_number, '')) LIKE LOWER(?))"""
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
    después (ver el comentario largo en schema.sql, CREATE TABLE
    waybills)."""
    if not validate_csrf():
        abort(400)
    waybill = query_one("SELECT id FROM waybills WHERE id = ?", (waybill_id,))
    if waybill is None:
        abort(404)
    execute(
        "UPDATE waybills SET client_order_number = ? WHERE id = ?",
        (request.form.get("client_order_number", "").strip() or None, waybill_id),
    )
    flash("Número de pedido guardado.", "success")
    return redirect(url_for("guias.detail", waybill_id=waybill_id))


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
