"""Arma el mismo resumen de alertas que ya se ve en el Dashboard
(documentos de conductores/unidades por vencer, mantenimientos próximos,
presupuestos al límite, neumáticos), para reusarlo en el correo automático
-- ver send_alerts.py y app/email_sender.py.

20 sep, pedido de Braulio ("como podemos hacer para que se envien alertas
automaticas a los correos?"). A propósito reusa las mismas funciones que ya
usa app/routes/dashboard.py (document_alerts, vehicle_document_alerts,
maintenance_date_alerts, km_alerts, budget_alerts, tire_alerts) en vez de
duplicar sus consultas -- así el correo siempre muestra exactamente lo
mismo que el Panel, sin mantener dos versiones de cada alerta.

21 sep, pedido de Braulio ("...y llegar a los correos de los usuarios que
usan para entrar al sistema"): alert_recipient_emails() arma la lista de
destinatarios sola, con el correo de login de cada usuario activo -- ver
esa función más abajo."""
from flask import current_app

from app.db import query_all
from app.routes.conductores import document_alerts as driver_document_alerts
from app.routes.flota import vehicle_document_alerts
from app.routes.liquidaciones import budget_alerts
from app.routes.mantenimiento import km_alerts, maintenance_date_alerts
from app.routes.neumaticos import tire_alerts

# Orden en el que aparecen las secciones en el correo -- mismo orden que el
# Panel (ver app/templates/dashboard/index.html).
_SECTION_BUILDERS = (
    ("driver_doc", "Documentos de conductores por vencer", driver_document_alerts),
    ("vehicle_doc", "Documentos de unidades por vencer", vehicle_document_alerts),
    ("maint_date", "Mantenimientos próximos (por fecha)", maintenance_date_alerts),
    ("maint_km", "Mantenimientos próximos (por kilometraje)", km_alerts),
    ("budget", "Presupuestos al límite", budget_alerts),
    ("tire", "Neumáticos en alerta", tire_alerts),
)


def build_alert_sections():
    """Devuelve una lista de {kind, title, items} -- solo las secciones que
    de verdad tengan alguna alerta, para no mandar un correo con secciones
    vacías. `kind` decide cómo se dibuja cada fila en
    app/templates/email/alertas.html (cada tipo de alerta trae campos
    distintos: conductor, unidad, presupuesto, neumático...)."""
    sections = []
    for kind, title, builder in _SECTION_BUILDERS:
        items = builder()
        if items:
            sections.append({"kind": kind, "title": title, "items": items})
    return sections


def total_alert_count(sections):
    return sum(len(s["items"]) for s in sections)


def alert_recipient_emails():
    """21 sep, pedido de Braulio ("las alertas... tienen que llegar a los
    correos de los usuarios que usan para entrar al sistema"): el correo
    de login (users.email) de cada usuario ACTIVO -- un usuario desactivado
    ya no puede ni entrar al sistema, así que tampoco tiene sentido
    seguirle mandando alertas. Se suma, si está configurado,
    ALERT_EMAIL_TO (ver config.py) para correos extra que no son de ningún
    usuario del sistema (ej. un contador externo). Devuelve la lista sin
    duplicados, en minúscula, ordenada -- lista vacía si no hay ningún
    usuario activo con correo (no debería pasar nunca en la práctica)."""
    rows = query_all("SELECT email FROM users WHERE active = 1 AND email IS NOT NULL AND email != ''")
    emails = {r["email"].strip().lower() for r in rows if r["email"] and r["email"].strip()}

    extra = (current_app.config.get("ALERT_EMAIL_TO") or "").strip()
    if extra:
        emails |= {addr.strip().lower() for addr in extra.split(",") if addr.strip()}

    return sorted(emails)
