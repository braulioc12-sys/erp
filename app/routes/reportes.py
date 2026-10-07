"""Módulo de Reportes (30 sep, pedido de Braulio: "ahora es momento de
habilitar un Modulo de reportes"). Tras aclarar el alcance con 3 preguntas
(confirmadas por Braulio, "Ambas cosas" / las 4 áreas / pantalla con
filtros+Excel Y dashboard con gráficos), este módulo hace DOS cosas:

1. Centraliza en un solo lugar (`centro()`) los reportes que ya existían
   sueltos en otros módulos (Liquidaciones, Viajes, RRHH, Pagos personal,
   GPS) — son solo enlaces, no se duplica nada de esos módulos.
2. Suma reportes NUEVOS que no existían en ninguna parte, uno por cada área
   de negocio que pidió Braulio (Operación, Finanzas, Flota, RRHH), cada uno
   con pantalla de filtros + export a Excel (mismo patrón que ya usa el
   resto de la app, ver app/reports.py) — y un dashboard con gráficos
   (`index()`, con Chart.js vía CDN, mismo criterio ya usado para Leaflet en
   Integraciones: librería externa cargada solo en esa pantalla).

Permisos: el módulo "reportes" (ver PERMISSIONS en app/auth.py) es la
PUERTA de entrada — sin "ver" en "reportes" no se llega a ninguna ruta de
acá (permission_required en cada vista). Adentro, cada sección/reporte se
filtra ADEMÁS por el permiso del área a la que pertenece (ej. Finanzas
requiere 'facturacion'/'view', Flota requiere 'mantenimiento'/'view') vía
`_can_section()` — así un rol con "reportes" pero sin, por ejemplo,
Facturación, no ve datos de facturación acá tampoco. Esto es a propósito:
Reportes nunca debe ser una puerta trasera para ver datos de un módulo al
que no se tiene acceso directo."""
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for

from app.auth import can, permission_required, report_required
from app.db import query_all, query_one
from app.helpers import parse_date, today_str
# 30 sep, pedido de Braulio ("los reportes tienen que estar separados como
# empresa"): "Viajes por cliente", "Cuentas por cobrar" y "Costos de
# mantenimiento por unidad" pasan de un filtro OPCIONAL de empresa (con
# "Ambas empresas" por defecto) a un selector OBLIGATORIO -- mismo patrón
# (company_gate) que ya usan Viajes/Liquidaciones/Facturación/Flota/
# Mantenimiento/Neumáticos. El dashboard (index(), arriba) y "Pagos por
# persona" (más abajo) NO se tocan -- Braulio los dejó explícitamente fuera
# de este alcance.
from app.integrations.sunat_ose import IGV_RATE
from app.routes.viajes import ISSUER_CHOICES

bp = Blueprint("reportes", __name__, url_prefix="/reportes")


def _can_section(module, action="view"):
    from flask import g

    return can(g.user["roles"], module, action)


def _require_section(module, action="view"):
    """Para las vistas de reporte individuales (no el dashboard, que ya
    filtra sus propias secciones): si el usuario no tiene el permiso del
    área a la que pertenece ESE reporte puntual, lo manda de vuelta al
    Centro de reportes en vez de dejarlo pasar solo por tener 'reportes'."""
    if not _can_section(module, action):
        flash("No tienes permiso para acceder a esta sección.", "error")
        return redirect(url_for("reportes.centro"))
    return None


def _last_n_months(n):
    """Últimos `n` periodos YYYY-MM, terminando en el mes actual, en orden
    ascendente — para los gráficos de tendencia del dashboard. Se calcula en
    Python (no con SQL) para no depender de aritmética de fechas que
    funcione igual en SQLite y en Postgres."""
    months = []
    today = datetime.now().replace(day=1)
    cursor = today
    for _ in range(n):
        months.append(cursor.strftime("%Y-%m"))
        # Retrocede un mes sin arrastrar el día (ya está fijo en 1).
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return list(reversed(months))


# ---------------------------------------------------------------------------
# Dashboard (landing de /reportes)
# ---------------------------------------------------------------------------


@bp.route("")
@permission_required("reportes", "view")
def index():
    months = _last_n_months(6)
    month_labels = [f"{m[5:7]}/{m[2:4]}" for m in months]

    revenue_chart = None
    top_clients_chart = None
    invoices_kpi = None
    if _can_section("facturacion"):
        by_month = {
            r["ym"]: r["total"]
            for r in query_all(
                "SELECT strftime('%Y-%m', issue_date) as ym, SUM(amount) as total FROM invoices "
                "WHERE status != 'ANULADA' GROUP BY ym"
            )
        }
        revenue_chart = {
            "labels": month_labels,
            "data": [round(by_month.get(m, 0) or 0, 2) for m in months],
        }
        top_clients = query_all(
            """SELECT c.name as client_name, SUM(i.amount) as total
               FROM invoices i JOIN clients c ON c.id = i.client_id
               WHERE i.status != 'ANULADA' AND strftime('%Y-%m', i.issue_date) >= ?
               GROUP BY c.id, c.name ORDER BY total DESC LIMIT 8""",
            (months[0],),
        )
        top_clients_chart = {
            "labels": [r["client_name"] for r in top_clients],
            "data": [round(r["total"] or 0, 2) for r in top_clients],
        }
        pending = query_one(
            "SELECT COUNT(*) as n, COALESCE(SUM(amount), 0) as total FROM invoices WHERE status IN ('PENDIENTE', 'VENCIDA')"
        )
        invoices_kpi = {"count": pending["n"], "total": pending["total"]}

    trip_status_chart = None
    trips_kpi = None
    if _can_section("viajes"):
        current_month = months[-1]
        by_status = query_all(
            "SELECT status, COUNT(*) as n FROM trips WHERE strftime('%Y-%m', scheduled_date) = ? GROUP BY status",
            (current_month,),
        )
        status_labels = {
            "PENDIENTE": "Pendiente", "EN_CURSO": "En curso",
            "ENTREGADO": "Entregado", "CANCELADO": "Cancelado",
        }
        trip_status_chart = {
            "labels": [status_labels.get(r["status"], r["status"]) for r in by_status],
            "data": [r["n"] for r in by_status],
        }
        trips_kpi = {"count": sum(r["n"] for r in by_status)}

    maintenance_chart = None
    maintenance_kpi = None
    if _can_section("mantenimiento"):
        by_month = {
            r["ym"]: r["total"]
            for r in query_all(
                "SELECT strftime('%Y-%m', maintenance_date) as ym, SUM(cost) as total "
                "FROM maintenance_records GROUP BY ym"
            )
        }
        maintenance_chart = {
            "labels": month_labels,
            "data": [round(by_month.get(m, 0) or 0, 2) for m in months],
        }
        maintenance_kpi = {"total_month": round(by_month.get(months[-1], 0) or 0, 2)}

    return render_template(
        "reportes/dashboard.html",
        revenue_chart=revenue_chart,
        top_clients_chart=top_clients_chart,
        trip_status_chart=trip_status_chart,
        maintenance_chart=maintenance_chart,
        invoices_kpi=invoices_kpi,
        trips_kpi=trips_kpi,
        maintenance_kpi=maintenance_kpi,
    )


@bp.route("/centro")
@permission_required("reportes", "view")
def centro():
    """Centro de reportes: enlaces agrupados por área — a la izquierda lo
    que ya existía suelto en otros módulos (no se duplica nada, son las
    mismas rutas de siempre), a la derecha los reportes nuevos de este
    módulo. Cada card se filtra por el permiso del área correspondiente,
    igual que las secciones del dashboard."""
    return render_template("reportes/centro.html")


# ---------------------------------------------------------------------------
# Nuevo — Operación: Viajes por cliente
# ---------------------------------------------------------------------------


def _trips_by_client_rows(args):
    from_date = parse_date(args.get("from_date", ""))
    to_date = parse_date(args.get("to_date", ""))
    issuer = args.get("issuer", "").strip().upper()

    sql = """SELECT c.id as client_id, c.name as client_name, COUNT(*) as trip_count,
                     SUM(t.rate) as total_rate
              FROM trips t JOIN clients c ON c.id = t.client_id
              WHERE t.status != 'CANCELADO'"""
    params = []
    if from_date:
        sql += " AND t.scheduled_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND t.scheduled_date <= ?"
        params.append(to_date)
    if issuer in ("HARRASO", "BRMS"):
        sql += " AND t.issuer = ?"
        params.append(issuer)
    sql += " GROUP BY c.id, c.name ORDER BY total_rate DESC"
    rows = query_all(sql, params)
    return rows, from_date, to_date, issuer


@bp.route("/viajes-por-cliente")
@report_required("viajes_por_cliente")
def viajes_por_cliente():
    # 30 sep: selector obligatorio de empresa (ya no existe la opción "Ambas
    # empresas") -- sin ?issuer=HARRASO|BRMS en la URL solo se muestra el
    # selector, mismo patrón que viajes.list_view()/flota.list_view().
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template(
            "reportes/viajes_por_cliente.html", rows=None, issuer=None, from_date="",
            to_date="", total_trips=0, total_amount=0,
        )
    rows, from_date, to_date, issuer = _trips_by_client_rows(request.args)
    total_trips = sum(r["trip_count"] for r in rows)
    total_amount = sum(r["total_rate"] or 0 for r in rows)
    return render_template(
        "reportes/viajes_por_cliente.html", rows=rows, from_date=from_date or "",
        to_date=to_date or "", issuer=issuer, total_trips=total_trips, total_amount=total_amount,
    )


@bp.route("/viajes-por-cliente/exportar")
@report_required("viajes_por_cliente")
def viajes_por_cliente_export():
    # 30 sep: el botón "Exportar a Excel" de la pantalla gateada arriba
    # siempre manda ?issuer=... -- esto es solo defensa contra un link
    # armado a mano sin empresa.
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige una empresa para exportar este reporte.", "error")
        return redirect(url_for("reportes.viajes_por_cliente"))
    from app.reports import build_trips_by_client_workbook

    rows, from_date, to_date, issuer = _trips_by_client_rows(request.args)
    parts = []
    parts.append(f"Periodo: {from_date or 'inicio'} a {to_date or 'hoy'}" if (from_date or to_date) else "Todas las fechas")
    parts.append(f"Empresa: {'BRMS' if issuer == 'BRMS' else 'Harraso Transport'}")
    buffer = build_trips_by_client_workbook(
        rows, company_name=current_app.config["COMPANY_NAME"], filter_description="  ·  ".join(parts),
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="viajes_por_cliente_{today_str()}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Nuevo — Operación: Viajes pendientes de facturar
# ---------------------------------------------------------------------------

PENDING_TRIP_STATUS_LABELS = {"ENTREGADO": "Entregado", "EN_CURSO": "En curso"}


def _pending_billing_rows(args):
    """7 oct, pedido de Braulio ("en reportes hay que incluir los viajes
    pendientes de facturar, que incluya los entregados y los que están en
    curso"): viajes de la empresa elegida que todavía NO se facturan
    (trips.invoiced = 0) y están ENTREGADOS o EN CURSO. Un viaje de un
    cliente que se factura a otra razón social (clients.billing_client_id,
    ej. Honda -> A&S) trae también "Se factura a"."""
    issuer = args.get("issuer", "").strip().upper()
    client_id = args.get("client_id", type=int)
    status = args.get("status", "").strip().upper()
    from_date = parse_date(args.get("from_date", ""))
    to_date = parse_date(args.get("to_date", ""))

    sql = """SELECT t.id, t.code, t.status, t.scheduled_date, t.delivered_date, t.origin,
                    t.destination, t.rate, t.issuer, c.name as client_name,
                    COALESCE(bc.name, c.name) as bill_to_name,
                    v.plate as plate, d.name as driver_name
             FROM trips t
             JOIN clients c ON c.id = t.client_id
             LEFT JOIN clients bc ON bc.id = c.billing_client_id
             LEFT JOIN vehicles v ON v.id = t.vehicle_id
             LEFT JOIN drivers d ON d.id = t.driver_id
             WHERE t.invoiced = 0 AND t.status IN ('ENTREGADO', 'EN_CURSO')"""
    params = []
    if issuer in ("HARRASO", "BRMS"):
        sql += " AND t.issuer = ?"
        params.append(issuer)
    if status in PENDING_TRIP_STATUS_LABELS:
        sql += " AND t.status = ?"
        params.append(status)
    if client_id:
        sql += " AND (c.id = ? OR c.billing_client_id = ?)"
        params.extend([client_id, client_id])
    if from_date:
        sql += " AND t.scheduled_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND t.scheduled_date <= ?"
        params.append(to_date)
    sql += " ORDER BY t.status ASC, t.delivered_date IS NULL, t.delivered_date ASC, t.scheduled_date ASC, t.id ASC"
    rows = query_all(sql, params)

    today = datetime.now().date()
    igv_exonerado = issuer == "BRMS"
    result = []
    for r in rows:
        row = dict(r)
        days = None
        if row["status"] == "ENTREGADO" and row["delivered_date"]:
            try:
                days = (today - datetime.strptime(row["delivered_date"], "%Y-%m-%d").date()).days
            except ValueError:
                days = None
        row["days_pending"] = days
        rate = float(row["rate"] or 0)
        # La tarifa del viaje es SIN IGV en Harraso (ver Generar factura); BRMS
        # está exonerada y su tarifa es el total.
        row["rate_with_igv"] = rate if igv_exonerado else round(rate * (1 + IGV_RATE), 2)
        result.append(row)
    return result, issuer, client_id, status, from_date, to_date


def _pending_billing_filter_description(issuer, status, from_date, to_date):
    parts = [f"Empresa: {'BRMS' if issuer == 'BRMS' else 'Harraso Transport'}"]
    parts.append(f"Estado: {PENDING_TRIP_STATUS_LABELS[status]}" if status in PENDING_TRIP_STATUS_LABELS else "Entregados y en curso")
    if from_date or to_date:
        parts.append(f"Programados: {from_date or 'inicio'} a {to_date or 'hoy'}")
    return "  ·  ".join(parts)


@bp.route("/viajes-pendientes-facturar")
@report_required("viajes_pendientes_facturar")
def viajes_pendientes_facturar():
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template("reportes/viajes_pendientes_facturar.html", rows=None, issuer=None)
    rows, issuer, client_id, status, from_date, to_date = _pending_billing_rows(request.args)
    clients = query_all("SELECT id, name FROM clients ORDER BY name")
    total = round(sum(float(r["rate"] or 0) for r in rows), 2)
    total_with_igv = round(sum(r["rate_with_igv"] for r in rows), 2)
    return render_template(
        "reportes/viajes_pendientes_facturar.html", rows=rows, issuer=issuer, clients=clients,
        client_id=client_id, status=status, from_date=from_date or "", to_date=to_date or "",
        status_labels=PENDING_TRIP_STATUS_LABELS, total=total, total_with_igv=total_with_igv,
        count_delivered=sum(1 for r in rows if r["status"] == "ENTREGADO"),
        count_in_progress=sum(1 for r in rows if r["status"] == "EN_CURSO"),
    )


@bp.route("/viajes-pendientes-facturar/exportar")
@report_required("viajes_pendientes_facturar")
def viajes_pendientes_facturar_export():
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige una empresa para exportar este reporte.", "error")
        return redirect(url_for("reportes.viajes_pendientes_facturar"))
    from app.reports import build_pending_billing_workbook

    rows, issuer, _client_id, status, from_date, to_date = _pending_billing_rows(request.args)
    buffer = build_pending_billing_workbook(
        rows, company_name=current_app.config["COMPANY_NAME"], issuer=issuer,
        filter_description=_pending_billing_filter_description(issuer, status, from_date, to_date),
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="viajes_pendientes_facturar_{today_str()}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Nuevo — Finanzas: Cuentas por cobrar
# ---------------------------------------------------------------------------


def _accounts_receivable_rows(args):
    client_id = args.get("client_id", type=int)
    issuer = args.get("issuer", "").strip().upper()
    only_overdue = args.get("only_overdue") == "1"

    # 7 oct: se descuentan los adelantos recibidos (invoice_advances) -- "Saldo"
    # es lo que realmente falta cobrar de la factura.
    sql = """SELECT i.id, i.number, i.issue_date, i.due_date, i.amount, i.status, i.issuer,
                     c.name as client_name,
                     (SELECT COALESCE(SUM(a.amount), 0) FROM invoice_advances a WHERE a.invoice_id = i.id) as advances_total
              FROM invoices i JOIN clients c ON c.id = i.client_id
              WHERE i.status IN ('PENDIENTE', 'VENCIDA')"""
    params = []
    if client_id:
        sql += " AND c.id = ?"
        params.append(client_id)
    if issuer in ("HARRASO", "BRMS"):
        sql += " AND i.issuer = ?"
        params.append(issuer)
    sql += " ORDER BY i.due_date IS NULL, i.due_date ASC, i.id ASC"
    rows = query_all(sql, params)

    today = datetime.now().date()
    result = []
    for r in rows:
        row = dict(r)
        days_overdue = None
        if row["due_date"]:
            try:
                due = datetime.strptime(row["due_date"], "%Y-%m-%d").date()
                days_overdue = (today - due).days
            except ValueError:
                days_overdue = None
        if only_overdue and not (days_overdue and days_overdue > 0):
            continue
        row["days_overdue"] = days_overdue
        row["balance"] = round(float(row["amount"] or 0) - float(row["advances_total"] or 0), 2)
        result.append(row)
    return result, client_id, issuer, only_overdue


@bp.route("/cuentas-por-cobrar")
@report_required("cuentas_por_cobrar")
def cuentas_por_cobrar():
    # 30 sep: selector obligatorio de empresa (ya no existe "Ambas empresas").
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template(
            "reportes/cuentas_por_cobrar.html", rows=None, clients=[], client_id=None,
            issuer=None, only_overdue=False, total=0,
        )
    rows, client_id, issuer, only_overdue = _accounts_receivable_rows(request.args)
    clients = query_all(
        """SELECT DISTINCT c.id, c.name FROM clients c JOIN invoices i ON i.client_id = c.id
           WHERE i.status IN ('PENDIENTE', 'VENCIDA') ORDER BY c.name"""
    )
    total = round(sum(r["balance"] for r in rows), 2)
    return render_template(
        "reportes/cuentas_por_cobrar.html", rows=rows, clients=clients, client_id=client_id,
        issuer=issuer, only_overdue=only_overdue, total=total,
    )


@bp.route("/cuentas-por-cobrar/exportar")
@report_required("cuentas_por_cobrar")
def cuentas_por_cobrar_export():
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige una empresa para exportar este reporte.", "error")
        return redirect(url_for("reportes.cuentas_por_cobrar"))
    from app.reports import build_accounts_receivable_workbook

    rows, client_id, issuer, only_overdue = _accounts_receivable_rows(request.args)
    parts = []
    parts.append("Solo vencidas" if only_overdue else "Pendientes y vencidas")
    parts.append(f"Empresa: {'BRMS' if issuer == 'BRMS' else 'Harraso Transport'}")
    buffer = build_accounts_receivable_workbook(
        rows, company_name=current_app.config["COMPANY_NAME"], filter_description="  ·  ".join(parts),
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="cuentas_por_cobrar_{today_str()}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Nuevo — Flota: Costos de mantenimiento por unidad
# ---------------------------------------------------------------------------


def _maintenance_costs_rows(args, issuer):
    from_date = parse_date(args.get("from_date", ""))
    to_date = parse_date(args.get("to_date", ""))
    vehicle_id = args.get("vehicle_id", type=int)

    # 30 sep: "v.issuer = ?" acá (no solo en el <select> de unidades) para
    # que, si alguien arma a mano una URL con vehicle_id de la OTRA empresa,
    # la fila simplemente no aparezca -- mismo criterio que
    # mantenimiento.new() valida el vehicle_id posteado.
    sql = """SELECT v.id as vehicle_id, v.plate, COUNT(*) as record_count, SUM(m.cost) as total_cost
              FROM maintenance_records m JOIN vehicles v ON v.id = m.vehicle_id
              WHERE v.issuer = ?"""
    params = [issuer]
    if from_date:
        sql += " AND m.maintenance_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND m.maintenance_date <= ?"
        params.append(to_date)
    if vehicle_id:
        sql += " AND v.id = ?"
        params.append(vehicle_id)
    sql += " GROUP BY v.id, v.plate ORDER BY total_cost DESC"
    rows = query_all(sql, params)
    return rows, from_date, to_date, vehicle_id


@bp.route("/costos-mantenimiento")
@report_required("costos_mantenimiento")
def costos_mantenimiento():
    # 30 sep: selector obligatorio de empresa, ahora que vehicles.issuer
    # existe (ver flota.list_view()/mantenimiento.list_view()) -- este
    # reporte es, en el fondo, un reporte de Mantenimiento, así que queda
    # gateado igual que ese módulo.
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        return render_template(
            "reportes/costos_mantenimiento.html", rows=None, vehicles=[], vehicle_id=None, issuer=None,
            from_date="", to_date="", total_cost=0, total_records=0,
        )
    rows, from_date, to_date, vehicle_id = _maintenance_costs_rows(request.args, issuer)
    vehicles = query_all("SELECT id, plate FROM vehicles WHERE issuer = ? ORDER BY plate", (issuer,))
    total_cost = sum(r["total_cost"] or 0 for r in rows)
    total_records = sum(r["record_count"] for r in rows)
    return render_template(
        "reportes/costos_mantenimiento.html", rows=rows, vehicles=vehicles, vehicle_id=vehicle_id, issuer=issuer,
        from_date=from_date or "", to_date=to_date or "", total_cost=total_cost, total_records=total_records,
    )


@bp.route("/costos-mantenimiento/exportar")
@report_required("costos_mantenimiento")
def costos_mantenimiento_export():
    issuer = request.args.get("issuer", "").strip().upper()
    if issuer not in ISSUER_CHOICES:
        flash("Elige una empresa para exportar este reporte.", "error")
        return redirect(url_for("reportes.costos_mantenimiento"))
    from app.reports import build_maintenance_costs_workbook

    rows, from_date, to_date, vehicle_id = _maintenance_costs_rows(request.args, issuer)
    parts = []
    parts.append(f"Periodo: {from_date or 'inicio'} a {to_date or 'hoy'}" if (from_date or to_date) else "Todas las fechas")
    parts.append(f"Empresa: {'BRMS' if issuer == 'BRMS' else 'Harraso Transport'}")
    buffer = build_maintenance_costs_workbook(
        rows, company_name=current_app.config["COMPANY_NAME"], filter_description="  ·  ".join(parts),
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="costos_mantenimiento_{today_str()}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Nuevo — RRHH: Pagos por persona
# ---------------------------------------------------------------------------


def _staff_payments_by_person_rows(args):
    from_period = args.get("from_period", "").strip()
    to_period = args.get("to_period", "").strip()
    staff_id = args.get("staff_id", type=int)

    sql = """SELECT s.id as staff_id, s.name as staff_name,
                     SUM(CASE WHEN p.payment_type = 'PLANILLA' THEN p.amount ELSE 0 END) as total_planilla,
                     SUM(CASE WHEN p.payment_type = 'RECIBO_HONORARIOS' THEN p.amount ELSE 0 END) as total_honorarios,
                     SUM(p.amount) as total_general, COUNT(*) as payment_count
              FROM staff_payments p JOIN staff s ON s.id = p.staff_id
              WHERE 1=1"""
    params = []
    if from_period:
        sql += " AND p.period >= ?"
        params.append(from_period)
    if to_period:
        sql += " AND p.period <= ?"
        params.append(to_period)
    if staff_id:
        sql += " AND s.id = ?"
        params.append(staff_id)
    sql += " GROUP BY s.id, s.name ORDER BY total_general DESC"
    rows = query_all(sql, params)
    return rows, from_period, to_period, staff_id


@bp.route("/pagos-por-persona")
@report_required("pagos_por_persona")
def pagos_por_persona():
    rows, from_period, to_period, staff_id = _staff_payments_by_person_rows(request.args)
    staff = query_all("SELECT id, name FROM staff ORDER BY name")
    total_general = sum(r["total_general"] or 0 for r in rows)
    return render_template(
        "reportes/pagos_por_persona.html", rows=rows, staff=staff, staff_id=staff_id,
        from_period=from_period, to_period=to_period, total_general=total_general,
    )


@bp.route("/pagos-por-persona/exportar")
@report_required("pagos_por_persona")
def pagos_por_persona_export():
    from app.reports import build_staff_payments_by_person_workbook

    rows, from_period, to_period, staff_id = _staff_payments_by_person_rows(request.args)
    parts = [f"Periodo: {from_period or 'inicio'} a {to_period or 'hoy'}" if (from_period or to_period) else "Todos los periodos"]
    buffer = build_staff_payments_by_person_workbook(
        rows, company_name=current_app.config["COMPANY_NAME"], filter_description="  ·  ".join(parts),
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="pagos_por_persona_{today_str()}.xlsx"'},
    )
