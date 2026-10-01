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

from app.auth import can, permission_required
from app.db import query_all, query_one
from app.helpers import parse_date, today_str

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
@permission_required("reportes", "view")
def viajes_por_cliente():
    redirect_resp = _require_section("viajes")
    if redirect_resp:
        return redirect_resp
    rows, from_date, to_date, issuer = _trips_by_client_rows(request.args)
    total_trips = sum(r["trip_count"] for r in rows)
    total_amount = sum(r["total_rate"] or 0 for r in rows)
    return render_template(
        "reportes/viajes_por_cliente.html", rows=rows, from_date=from_date or "",
        to_date=to_date or "", issuer=issuer, total_trips=total_trips, total_amount=total_amount,
    )


@bp.route("/viajes-por-cliente/exportar")
@permission_required("reportes", "view")
def viajes_por_cliente_export():
    redirect_resp = _require_section("viajes")
    if redirect_resp:
        return redirect_resp
    from app.reports import build_trips_by_client_workbook

    rows, from_date, to_date, issuer = _trips_by_client_rows(request.args)
    parts = []
    parts.append(f"Periodo: {from_date or 'inicio'} a {to_date or 'hoy'}" if (from_date or to_date) else "Todas las fechas")
    parts.append(f"Empresa: {'BRMS' if issuer == 'BRMS' else 'Harraso Transport'}" if issuer in ("HARRASO", "BRMS") else "Ambas empresas")
    buffer = build_trips_by_client_workbook(
        rows, company_name=current_app.config["COMPANY_NAME"], filter_description="  ·  ".join(parts),
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="viajes_por_cliente_{today_str()}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Nuevo — Finanzas: Cuentas por cobrar
# ---------------------------------------------------------------------------


def _accounts_receivable_rows(args):
    client_id = args.get("client_id", type=int)
    issuer = args.get("issuer", "").strip().upper()
    only_overdue = args.get("only_overdue") == "1"

    sql = """SELECT i.id, i.number, i.issue_date, i.due_date, i.amount, i.status, i.issuer,
                     c.name as client_name
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
        result.append(row)
    return result, client_id, issuer, only_overdue


@bp.route("/cuentas-por-cobrar")
@permission_required("reportes", "view")
def cuentas_por_cobrar():
    redirect_resp = _require_section("facturacion")
    if redirect_resp:
        return redirect_resp
    rows, client_id, issuer, only_overdue = _accounts_receivable_rows(request.args)
    clients = query_all(
        """SELECT DISTINCT c.id, c.name FROM clients c JOIN invoices i ON i.client_id = c.id
           WHERE i.status IN ('PENDIENTE', 'VENCIDA') ORDER BY c.name"""
    )
    total = sum(r["amount"] or 0 for r in rows)
    return render_template(
        "reportes/cuentas_por_cobrar.html", rows=rows, clients=clients, client_id=client_id,
        issuer=issuer, only_overdue=only_overdue, total=total,
    )


@bp.route("/cuentas-por-cobrar/exportar")
@permission_required("reportes", "view")
def cuentas_por_cobrar_export():
    redirect_resp = _require_section("facturacion")
    if redirect_resp:
        return redirect_resp
    from app.reports import build_accounts_receivable_workbook

    rows, client_id, issuer, only_overdue = _accounts_receivable_rows(request.args)
    parts = []
    parts.append("Solo vencidas" if only_overdue else "Pendientes y vencidas")
    parts.append(f"Empresa: {'BRMS' if issuer == 'BRMS' else 'Harraso Transport'}" if issuer in ("HARRASO", "BRMS") else "Ambas empresas")
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


def _maintenance_costs_rows(args):
    from_date = parse_date(args.get("from_date", ""))
    to_date = parse_date(args.get("to_date", ""))
    vehicle_id = args.get("vehicle_id", type=int)

    sql = """SELECT v.id as vehicle_id, v.plate, COUNT(*) as record_count, SUM(m.cost) as total_cost
              FROM maintenance_records m JOIN vehicles v ON v.id = m.vehicle_id
              WHERE 1=1"""
    params = []
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
@permission_required("reportes", "view")
def costos_mantenimiento():
    redirect_resp = _require_section("mantenimiento")
    if redirect_resp:
        return redirect_resp
    rows, from_date, to_date, vehicle_id = _maintenance_costs_rows(request.args)
    vehicles = query_all("SELECT id, plate FROM vehicles ORDER BY plate")
    total_cost = sum(r["total_cost"] or 0 for r in rows)
    total_records = sum(r["record_count"] for r in rows)
    return render_template(
        "reportes/costos_mantenimiento.html", rows=rows, vehicles=vehicles, vehicle_id=vehicle_id,
        from_date=from_date or "", to_date=to_date or "", total_cost=total_cost, total_records=total_records,
    )


@bp.route("/costos-mantenimiento/exportar")
@permission_required("reportes", "view")
def costos_mantenimiento_export():
    redirect_resp = _require_section("mantenimiento")
    if redirect_resp:
        return redirect_resp
    from app.reports import build_maintenance_costs_workbook

    rows, from_date, to_date, vehicle_id = _maintenance_costs_rows(request.args)
    parts = []
    parts.append(f"Periodo: {from_date or 'inicio'} a {to_date or 'hoy'}" if (from_date or to_date) else "Todas las fechas")
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
@permission_required("reportes", "view")
def pagos_por_persona():
    redirect_resp = _require_section("pagos_personal")
    if redirect_resp:
        return redirect_resp
    rows, from_period, to_period, staff_id = _staff_payments_by_person_rows(request.args)
    staff = query_all("SELECT id, name FROM staff ORDER BY name")
    total_general = sum(r["total_general"] or 0 for r in rows)
    return render_template(
        "reportes/pagos_por_persona.html", rows=rows, staff=staff, staff_id=staff_id,
        from_period=from_period, to_period=to_period, total_general=total_general,
    )


@bp.route("/pagos-por-persona/exportar")
@permission_required("reportes", "view")
def pagos_por_persona_export():
    redirect_resp = _require_section("pagos_personal")
    if redirect_resp:
        return redirect_resp
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
