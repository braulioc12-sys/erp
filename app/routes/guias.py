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
    parse_ose_response,
)
from app.storage import (
    local_sunat_documents_dir,
    save_sunat_document,
    sunat_document_url,
    using_s3,
)

bp = Blueprint("guias", __name__, url_prefix="/guias")

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
    waybills = query_all(
        """SELECT w.*, t.code as trip_code, t.origin, t.destination, c.name as client_name
           FROM waybills w
           JOIN trips t ON t.id = w.trip_id
           JOIN clients c ON c.id = t.client_id
           ORDER BY w.issue_date DESC, w.id DESC"""
    )
    return render_template("guias/list.html", waybills=waybills)


@bp.route("/nueva/<int:trip_id>", methods=["GET", "POST"])
@permission_required("guias", "edit")
def new(trip_id):
    trip = query_one(
        """SELECT t.*, v.plate as vehicle_plate, d.name as driver_name,
                  d.document_number as driver_document, d.license_number as driver_license
           FROM trips t
           LEFT JOIN vehicles v ON v.id = t.vehicle_id
           LEFT JOIN drivers d ON d.id = t.driver_id
           WHERE t.id = ?""",
        (trip_id,),
    )
    if trip is None:
        abort(404)

    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        series = current_app.config["WAYBILL_SERIES"]
        series_number = _next_series_number(series)
        waybill_id = execute(
            """INSERT INTO waybills (trip_id, series, series_number, issuer, issue_date, weight_kg, packages,
               origin_address, destination_address, origin_ubigeo, destination_ubigeo, transfer_reason,
               vehicle_plate, driver_document, driver_name, driver_license, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trip_id,
                series,
                series_number,
                # 7 sep, integración con tefacturo.pe: la guía se emite a
                # nombre de la misma empresa que el viaje (trips.issuer,
                # Harraso o BRMS) — se copia aquí y no se recalcula después.
                trip["issuer"],
                parse_date(request.form.get("issue_date")) or today_str(),
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
                request.form.get("driver_document", "").strip() or trip["driver_document"],
                request.form.get("driver_name", "").strip() or trip["driver_name"],
                request.form.get("driver_license", "").strip() or trip["driver_license"],
                request.form.get("notes", "").strip(),
                None,
            ),
        )
        flash(f"Guía {series}-{series_number:06d} creada.", "success")
        return redirect(url_for("guias.detail", waybill_id=waybill_id))

    return render_template(
        "guias/form.html", trip=trip, today=today_str(), transfer_reasons=TRANSFER_REASONS
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
    return render_template("guias/detail.html", waybill=waybill)


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
        response = ose_client.emit_guia_transportista(payload)
        result = parse_ose_response(response)

        pdf_filename = waybill["sunat_pdf_filename"]
        pdf_url = waybill["sunat_pdf_url"]
        if result["accepted"]:
            try:
                pdf_bytes = ose_client.get_pdf_bytes("09", waybill["series"], waybill["series_number"])
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
