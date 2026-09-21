"""Envío de correos vía AWS SES (Simple Email Service).

20 sep, pedido de Braulio ("como podemos hacer para que se envien alertas
automaticas a los correos?"). Se eligió AWS SES en vez de un servicio nuevo
porque el proyecto ya usa AWS para RDS/S3 (ver app/storage.py) -- misma
cuenta, mismas credenciales (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/
AWS_DEFAULT_REGION), un solo lugar donde configurar todo.

boto3 solo se importa (import perezoso) al mandar un correo de verdad,
igual que storage.py con S3 -- así el resto de la app no depende de tener
boto3 disponible si esto no se llegara a usar.

OJO -- cuenta nueva de AWS SES: por default arranca en modo "sandbox",
donde SOLO se puede mandar correo si TANTO el remitente (SES_SENDER_EMAIL)
COMO cada destinatario están verificados en la consola de SES (Verified
identities). Para mandar a cualquier destinatario sin verificarlo antes,
hay que pedir "producción" desde SES (Account dashboard -> Request
production access) -- ver README, sección "Alertas por correo (AWS SES)"."""
import os

from flask import current_app


def ses_configured():
    """True si hay al menos un remitente configurado -- no confirma que la
    cuenta de SES esté lista de verdad (verificada, fuera de sandbox,
    etc.), solo que alguien completó la variable de entorno."""
    return bool((current_app.config.get("SES_SENDER_EMAIL") or "").strip())


def _ses_client():
    import boto3

    # Mismo cuidado que _s3_client() en app/storage.py (31 ago,
    # "SignatureDoesNotMatch" en producción real): limpiar espacios/saltos
    # de línea de las credenciales en vez de dejar que boto3 las tome "tal
    # cual" del entorno.
    access_key = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    region = (os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    kwargs = {}
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    if region:
        kwargs["region_name"] = region
    return boto3.client("ses", **kwargs)


def send_email(to_addresses, subject, html_body, text_body=None):
    """Manda un correo por SES. `to_addresses` puede ser un string (uno o
    varios correos separados por coma) o una lista/tupla. Devuelve
    (True, None) si SES aceptó el envío, o (False, mensaje de error) si
    algo falló -- nunca lanza una excepción, para que send_alerts.py (o
    cualquier otro llamador) pueda seguir/loguear sin caerse."""
    sender = (current_app.config.get("SES_SENDER_EMAIL") or "").strip()
    if not sender:
        return False, "Falta configurar SES_SENDER_EMAIL (el remitente verificado en AWS SES)."

    if isinstance(to_addresses, str):
        to_list = [addr.strip() for addr in to_addresses.split(",") if addr.strip()]
    else:
        to_list = [addr.strip() for addr in (to_addresses or []) if addr and addr.strip()]
    if not to_list:
        return False, "No hay ningún destinatario configurado (ALERT_EMAIL_TO)."

    body = {"Html": {"Data": html_body, "Charset": "UTF-8"}}
    if text_body:
        body["Text"] = {"Data": text_body, "Charset": "UTF-8"}

    try:
        client = _ses_client()
        client.send_email(
            Source=sender,
            Destination={"ToAddresses": to_list},
            Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body},
        )
        return True, None
    except Exception as exc:
        return False, str(exc)
