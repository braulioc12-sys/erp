from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from app.auth import permission_required, validate_csrf
from app.db import execute, get_db, query_all, query_one
from app.helpers import next_code, parse_date, today_str
from app.integrations.sunat_ose import (
    SunatOseError,
    build_client_from_config,
    build_invoice_payload,
    parse_ose_response,
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

        if not client_id or not trip_ids:
            flash("Selecciona un cliente y al menos un viaje para facturar.", "error")
            return redirect(url_for("facturacion.new", client_id=client_id))

        trips = query_all(
            f"""SELECT * FROM trips WHERE id IN ({','.join('?' * len(trip_ids))})
                AND client_id = ? AND status = 'ENTREGADO' AND invoiced = 0""",
            (*trip_ids, client_id),
        )
        if not trips:
            flash("Los viajes seleccionados ya no están disponibles para facturar.", "error")
            return redirect(url_for("facturacion.new", client_id=client_id))

        total = sum(t["rate"] for t in trips)
        number = next_code("F", "invoices")
        series = current_app.config["INVOICE_SERIES"]
        series_number = _next_series_number(series)

        db = get_db()
        cur = db.execute(
            """INSERT INTO invoices (number, client_id, issue_date, due_date, amount, notes, series, series_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (number, client_id, issue_date, due_date, total, request.form.get("notes", "").strip(), series, series_number),
        )
        invoice_id = cur.lastrowid
        for t in trips:
            db.execute(
                "INSERT INTO invoice_items (invoice_id, trip_id, description, amount) VALUES (?, ?, ?, ?)",
                (invoice_id, t["id"], f"{t['code']}: {t['origin']} -> {t['destination']}", t["rate"]),
            )
            db.execute("UPDATE trips SET invoiced = 1 WHERE id = ?", (t["id"],))
        db.commit()

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
    items = query_all(
        """SELECT ii.*, t.code as trip_code FROM invoice_items ii
           JOIN trips t ON t.id = ii.trip_id WHERE ii.invoice_id = ?""",
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

    ose_client = build_client_from_config(current_app.config)
    company = {
        "ruc": current_app.config["COMPANY_RUC"],
        "name": current_app.config["COMPANY_NAME"],
        "address": current_app.config["COMPANY_ADDRESS"],
    }

    try:
        payload = build_invoice_payload(invoice, items, client_row, company)
        response = ose_client.send(payload)
        result = parse_ose_response(response)
        execute(
            """UPDATE invoices SET sunat_status=?, sunat_message=?, sunat_pdf_url=?,
               sunat_xml_url=?, sunat_cdr_url=?, sunat_sent_at=datetime('now') WHERE id=?""",
            (
                "ACEPTADO" if result["accepted"] else "RECHAZADO",
                result["message"],
                result["pdf_url"],
                result["xml_url"],
                result["cdr_url"],
                invoice_id,
            ),
        )
        if result["accepted"]:
            flash("Factura enviada y aceptada por SUNAT.", "success")
        else:
            flash(f"SUNAT/el OSE rechazó la factura: {result['message']}", "error")
    except SunatOseError as exc:
        execute(
            "UPDATE invoices SET sunat_status='ERROR', sunat_message=?, sunat_sent_at=datetime('now') WHERE id=?",
            (str(exc), invoice_id),
        )
        flash(f"No se pudo enviar la factura: {exc}", "error")

    return redirect(url_for("facturacion.detail", invoice_id=invoice_id))
