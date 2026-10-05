"""1 oct, pedido de Braulio ("hoy empezamos con los viajes y facturacion
reales, quiero borrar los anteriores que hicimos hasta ayer que eran de
prueba"): pantalla exclusiva de Administrador para borrar viajes y/o
facturas DE VERDAD, incluso los que ya están "ACEPTADO" por SUNAT (algo que
viajes.delete_trip()/facturacion.delete() bloquean a propósito en el uso
normal del día a día -- ver _trip_delete_block_reason()/
_invoice_delete_block_reason() -- porque ahí sí sería un comprobante
electrónico real vigente sin registro local). Ese bloqueo no tiene sentido
para los datos de PRUEBA que se cargaron antes de hoy (facturas "aceptadas"
por un sandbox/prueba de tefacturo.pe, no comprobantes reales), así que
Braulio pidió poder revisarlos uno por uno y borrarlos igual.

Exclusivo de Administrador, chequeado por ROL directamente acá (igual que
rrhh_approve() en app/routes/liquidaciones.py o purchases_authorize() en
app/routes/inventarios.py) y NO vía el catálogo de permisos por usuario
(app/permissions_catalog.py) -- a propósito, para que nunca se pueda
otorgar como "permiso específico" a alguien que no sea Administrador.

Queda en el sistema (no es un parche de un solo uso) por si hace falta
repetir una limpieza así más adelante -- Braulio lo pidió como herramienta
permanente, ver la conversación del pedido."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app.audit import log_activity
from app.auth import login_required, validate_csrf
from app.db import execute, query_all, query_one

bp = Blueprint("admin_reset", __name__, url_prefix="/admin/limpieza")


def _is_admin():
    return bool(g.user) and "ADMIN" in g.user["roles"]


def _forbid():
    flash("Solo un Administrador puede acceder a la limpieza de datos de prueba.", "error")
    return redirect(url_for("dashboard.index"))


@bp.route("")
@login_required
def list_view():
    if not _is_admin():
        return _forbid()

    # Un viaje puede tener a lo más una factura/guía real (no tiene sentido
    # facturarlo dos veces), así que el LIMIT 1 de estas subconsultas no
    # pierde información -- solo es para poder avisar en la lista si borrar
    # este viaje se "lleva de encuentro" un comprobante ya aceptado por
    # SUNAT (igual se borra: esta pantalla es justamente para saltar ese
    # bloqueo con datos de prueba).
    trips = query_all(
        """SELECT t.id, t.code, t.scheduled_date, t.status, t.issuer, t.origin, t.destination,
                  t.invoiced, c.name AS client_name,
                  (SELECT i.number FROM invoices i JOIN invoice_items ii ON ii.invoice_id = i.id
                   WHERE ii.trip_id = t.id LIMIT 1) AS invoice_number,
                  (SELECT i.sunat_status FROM invoices i JOIN invoice_items ii ON ii.invoice_id = i.id
                   WHERE ii.trip_id = t.id LIMIT 1) AS invoice_sunat_status,
                  (SELECT w.sunat_status FROM waybills w WHERE w.trip_id = t.id LIMIT 1) AS waybill_sunat_status,
                  (SELECT rt.code FROM trips rt WHERE rt.return_of_trip_id = t.id LIMIT 1) AS return_trip_code
           FROM trips t JOIN clients c ON c.id = t.client_id
           ORDER BY t.scheduled_date DESC, t.id DESC"""
    )
    invoices = query_all(
        """SELECT i.id, i.number, i.series, i.series_number, i.issue_date, i.amount, i.status,
                  i.sunat_status, i.issuer, c.name AS client_name
           FROM invoices i JOIN clients c ON c.id = i.client_id
           ORDER BY i.issue_date DESC, i.id DESC"""
    )
    return render_template("admin_reset/list.html", trips=trips, invoices=invoices)


def _force_delete_trip(trip_id):
    """Mismo borrado en cascada que viajes.delete_trip() (inspecciones,
    liquidación con su combustible/pagos, gastos, guía de remisión e ítems
    de factura), pero SIN pasar por _trip_delete_block_reason() -- acá se
    borra igual aunque el viaje tenga una factura o guía ya ACEPTADA por
    SUNAT. Devuelve el código del viaje borrado, o None si ya no existía
    (pudo borrarse como parte de otra fila de esta misma limpieza, p. ej.
    el viaje de vuelta de un viaje de ida que también se marcó)."""
    trip = query_one("SELECT code FROM trips WHERE id = ?", (trip_id,))
    if trip is None:
        return None

    # Si otro viaje lo tiene enlazado como su "ida" (return_of_trip_id),
    # se desengancha en vez de bloquear -- el borrado normal (delete_trip())
    # sí exige borrar/cancelar primero la vuelta, pero acá eso haría
    # imposible borrar ambos de una sola pasada por esta pantalla.
    execute("UPDATE trips SET return_of_trip_id = NULL WHERE return_of_trip_id = ?", (trip_id,))

    execute(
        "DELETE FROM inspection_items WHERE inspection_id IN (SELECT id FROM inspections WHERE trip_id = ?)",
        (trip_id,),
    )
    execute("DELETE FROM inspections WHERE trip_id = ?", (trip_id,))
    execute(
        "DELETE FROM fuel_entries WHERE advance_id IN (SELECT id FROM expense_advances WHERE trip_id = ?)",
        (trip_id,),
    )
    execute(
        "DELETE FROM advance_payments WHERE advance_id IN (SELECT id FROM expense_advances WHERE trip_id = ?)",
        (trip_id,),
    )
    execute("DELETE FROM expenses WHERE trip_id = ?", (trip_id,))
    execute("DELETE FROM expense_advances WHERE trip_id = ?", (trip_id,))
    execute("DELETE FROM waybills WHERE trip_id = ?", (trip_id,))
    execute("DELETE FROM trip_waybill_files WHERE trip_id = ?", (trip_id,))

    affected_invoice_ids = [
        r["invoice_id"] for r in query_all(
            "SELECT DISTINCT invoice_id FROM invoice_items WHERE trip_id = ?", (trip_id,)
        )
    ]
    execute("DELETE FROM invoice_items WHERE trip_id = ?", (trip_id,))
    for invoice_id in affected_invoice_ids:
        remaining = query_all("SELECT amount FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        if remaining:
            new_total = sum(r["amount"] for r in remaining)
            execute("UPDATE invoices SET amount = ? WHERE id = ?", (new_total, invoice_id))
        else:
            execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))

    execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    return trip["code"]


def _force_delete_invoice(invoice_id):
    """Mismo borrado que facturacion.delete() (ítems de factura + libera
    trips.invoiced), pero SIN pasar por _invoice_delete_block_reason() --
    acá se borra igual aunque sunat_status sea ACEPTADO. Devuelve el número
    de la factura borrada, o None si ya no existía (pudo borrarse sola al
    borrar su único viaje -- ver _force_delete_trip())."""
    invoice = query_one("SELECT number FROM invoices WHERE id = ?", (invoice_id,))
    if invoice is None:
        return None

    trip_ids = [
        r["trip_id"] for r in query_all(
            "SELECT trip_id FROM invoice_items WHERE invoice_id = ? AND trip_id IS NOT NULL", (invoice_id,)
        )
    ]
    execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    for trip_id in trip_ids:
        execute("UPDATE trips SET invoiced = 0 WHERE id = ?", (trip_id,))
    return invoice["number"]


@bp.route("/borrar", methods=["POST"])
@login_required
def borrar():
    if not _is_admin():
        return _forbid()
    if not validate_csrf():
        abort(400)

    # Confirmación extra a propósito, además del checkbox por fila: esta
    # pantalla salta protecciones que en el resto del sistema son
    # definitivas (factura/guía ya aceptada por SUNAT), así que un solo
    # click no alcanza -- hay que escribir la palabra de confirmación.
    if (request.form.get("confirm_text") or "").strip().upper() != "BORRAR":
        flash('Para borrar tenés que escribir "BORRAR" (en mayúsculas) en el cuadro de confirmación.', "error")
        return redirect(url_for("admin_reset.list_view"))

    trip_ids = [int(x) for x in request.form.getlist("trip_ids") if x.strip().isdigit()]
    invoice_ids = [int(x) for x in request.form.getlist("invoice_ids") if x.strip().isdigit()]

    if not trip_ids and not invoice_ids:
        flash("No marcaste ningún viaje ni factura para borrar.", "error")
        return redirect(url_for("admin_reset.list_view"))

    # Primero los viajes (su borrado en cascada ya limpia/borra la factura
    # asociada si corresponde), después las facturas que se hayan marcado
    # aparte (p. ej. una factura de prueba cuyo viaje NO se quiere borrar).
    deleted_trip_codes = [code for code in (_force_delete_trip(tid) for tid in trip_ids) if code]
    deleted_invoice_numbers = [num for num in (_force_delete_invoice(iid) for iid in invoice_ids) if num]

    log_activity(
        "admin_reset", "ELIMINAR",
        "Limpieza de datos de prueba -- viaje(s) "
        f"{', '.join(deleted_trip_codes) or '(ninguno)'} y factura(s) "
        f"{', '.join(deleted_invoice_numbers) or '(ninguna)'} borrados definitivamente, saltando el "
        "bloqueo normal de SUNAT/guía aceptada.",
    )

    flash(
        f"Se borraron {len(deleted_trip_codes)} viaje(s) y {len(deleted_invoice_numbers)} factura(s), "
        "junto con todo lo relacionado (inspecciones, liquidaciones, guías e ítems de factura).",
        "success",
    )
    return redirect(url_for("admin_reset.list_view"))
