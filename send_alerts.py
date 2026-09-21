"""Manda por correo (AWS SES) el mismo resumen de alertas que se ve en el
Panel de Harris -- documentos de conductores/unidades por vencer,
mantenimientos próximos (por fecha y por kilometraje), presupuestos al
límite y neumáticos en alerta (ver app/alerts.py).

20 sep, pedido de Braulio ("como podemos hacer para que se envien alertas
automaticas a los correos?"). Pensado para correr periódicamente desde un
Cron Job de Render (uno diario y otro semanal, mismo comando, distinto
--periodo solo para que el asunto del correo lo diga) -- ver README,
sección "Alertas por correo (AWS SES)". También sirve para probarlo a mano
mientras se configura SES.

Uso:
    python send_alerts.py                    # resumen diario (default)
    python send_alerts.py --periodo semanal  # mismo resumen, asunto distinto
"""
import argparse
import sys

from flask import render_template

from app import create_app
from app.alerts import alert_recipient_emails, build_alert_sections, total_alert_count
from app.email_sender import send_email
from app.helpers import today_str


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--periodo", choices=["diario", "semanal"], default="diario")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        # 21 sep, pedido de Braulio: el destinatario ya no es un correo fijo
        # -- es el correo de login de cada usuario activo del sistema (ver
        # alert_recipient_emails() en app/alerts.py), más ALERT_EMAIL_TO si
        # hay correos extra configurados.
        to = alert_recipient_emails()
        if not to:
            print(
                "No hay ningún destinatario (ni usuarios activos con correo, ni ALERT_EMAIL_TO) -- no se manda nada.",
                file=sys.stderr,
            )
            sys.exit(1)

        sections = build_alert_sections()
        total = total_alert_count(sections)
        etiqueta = "Resumen diario" if args.periodo == "diario" else "Resumen semanal"
        subject = (
            f"Harris — {etiqueta} de alertas ({total})"
            if total
            else f"Harris — {etiqueta}: sin alertas pendientes"
        )
        html = render_template(
            "email/alertas.html", sections=sections, total=total, periodo=args.periodo, today=today_str()
        )

        ok, error = send_email(to, subject, html)
        if ok:
            print(f"Correo de alertas enviado a {', '.join(to)} ({total} alerta(s), periodo {args.periodo}).")
        else:
            print(f"No se pudo enviar el correo de alertas: {error}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
