from flask import Blueprint, render_template

from app.auth import permission_required
from app.db import query_all, query_one
from app.helpers import today_str
from app.routes.conductores import document_alerts as driver_document_alerts
from app.routes.flota import vehicle_document_alerts
from app.routes.gastos import budget_alerts
from app.routes.mantenimiento import km_alerts
from app.routes.neumaticos import tire_alerts

bp = Blueprint("dashboard", __name__, url_prefix="/")


@bp.route("dashboard")
@permission_required("dashboard", "view")
def index():
    active_trips = query_one(
        "SELECT COUNT(*) n FROM trips WHERE status IN ('PENDIENTE', 'EN_CURSO')"
    )["n"]

    revenue_month = query_one(
        """SELECT COALESCE(SUM(rate), 0) total FROM trips
           WHERE status = 'ENTREGADO' AND strftime('%Y-%m', delivered_date) = strftime('%Y-%m', 'now')"""
    )["total"]

    pending_invoices = query_one(
        "SELECT COUNT(*) n, COALESCE(SUM(amount), 0) total FROM invoices WHERE status IN ('PENDIENTE', 'VENCIDA')"
    )

    vehicles_maintenance = query_one(
        "SELECT COUNT(*) n FROM vehicles WHERE status = 'MANTENIMIENTO'"
    )["n"]

    total_vehicles = query_one("SELECT COUNT(*) n FROM vehicles")["n"]

    recent_trips = query_all(
        """SELECT t.*, c.name as client_name, v.plate as vehicle_plate, d.name as driver_name
           FROM trips t
           JOIN clients c ON c.id = t.client_id
           LEFT JOIN vehicles v ON v.id = t.vehicle_id
           LEFT JOIN drivers d ON d.id = t.driver_id
           ORDER BY t.created_at DESC LIMIT 8"""
    )

    # Alertas: mantenimientos próximos (documentos de conductores y
    # unidades se resuelven en sus propios módulos, ver imports arriba)
    maintenance_alerts = query_all(
        """SELECT v.plate, m.next_due_date FROM maintenance_records m
           JOIN vehicles v ON v.id = m.vehicle_id
           WHERE m.next_due_date IS NOT NULL AND m.next_due_date != ''
           AND date(m.next_due_date) <= date('now', '+30 days')
           ORDER BY m.next_due_date ASC"""
    )

    return render_template(
        "dashboard/index.html",
        active_trips=active_trips,
        revenue_month=revenue_month,
        pending_invoices=pending_invoices,
        vehicles_maintenance=vehicles_maintenance,
        total_vehicles=total_vehicles,
        recent_trips=recent_trips,
        driver_document_alerts=driver_document_alerts(),
        vehicle_document_alerts=vehicle_document_alerts(),
        maintenance_alerts=maintenance_alerts,
        km_alerts=km_alerts(),
        budget_alerts=budget_alerts(),
        tire_alerts=tire_alerts(),
        today=today_str(),
    )
