from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, today_str
from app.integrations.sunat_ose import (
    SunatOseError,
    build_client_from_config,
    build_waybill_payload,
    parse_ose_response,
)

bp = Blueprint("guias", __name__, url_prefix="/guias")


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
            """INSERT INTO waybills (trip_id, series, series_number, issue_date, weight_kg, packages,
               origin_address, destination_address, vehicle_plate, driver_document, driver_name,
               driver_license, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trip_id,
                series,
                series_number,
                parse_date(request.form.get("issue_date")) or today_str(),
                parse_float(request.form.get("weight_kg"), None),
                int(parse_float(request.form.get("packages"), 1)),
                request.form.get("origin_address", "").strip() or trip["origin"],
                request.form.get("destination_address", "").strip() or trip["destination"],
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

    return render_template("guias/form.html", trip=trip, today=today_str())


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

    client = build_client_from_config(current_app.config)
    company = {
        "ruc": current_app.config["COMPANY_RUC"],
        "name": current_app.config["COMPANY_NAME"],
        "address": current_app.config["COMPANY_ADDRESS"],
    }

    try:
        if not company["ruc"]:
            raise SunatOseError(
                "Falta configurar el RUC de tu empresa (variable de entorno COMPANY_RUC) "
                "antes de poder emitir guías electrónicas."
            )
        payload = build_waybill_payload(waybill, trip, company)
        response = client.send(payload)
        result = parse_ose_response(response)
        execute(
            """UPDATE waybills SET sunat_status=?, sunat_message=?, sunat_pdf_url=?,
               sunat_xml_url=?, sunat_cdr_url=?, sunat_sent_at=datetime('now') WHERE id=?""",
            (
                "ACEPTADO" if result["accepted"] else "RECHAZADO",
                result["message"],
                result["pdf_url"],
                result["xml_url"],
                result["cdr_url"],
                waybill_id,
            ),
        )
        if result["accepted"]:
            flash("Guía enviada y aceptada por SUNAT.", "success")
        else:
            flash(f"SUNAT/el OSE rechazó la guía: {result['message']}", "error")
    except SunatOseError as exc:
        execute(
            "UPDATE waybills SET sunat_status='ERROR', sunat_message=?, sunat_sent_at=datetime('now') WHERE id=?",
            (str(exc), waybill_id),
        )
        flash(f"No se pudo enviar la guía: {exc}", "error")

    return redirect(url_for("guias.detail", waybill_id=waybill_id))
