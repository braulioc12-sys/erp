"""Almacenamiento de archivos subidos por los usuarios — hoy solo los
comprobantes de gastos de Liquidaciones (ver app/routes/liquidaciones.py).

Dos modos, elegidos por la variable de entorno AWS_S3_BUCKET (ver README,
sección "Base de datos persistente en AWS (RDS + S3)"):

- Si NO está seteada (por defecto — desarrollo local, y producción en
  Render con disco efímero como hasta ahora): los archivos se guardan en
  disco, en la carpeta "receipts" dentro de instance/. Se pierden en cada
  redeploy de Render, exactamente igual que antes de este cambio.
- Si está seteada (producción real, ej. Amazon S3): los archivos se suben a
  ese bucket, y se sirven mediante URLs firmadas de corta duración (5
  minutos) — el bucket es privado, así que nadie puede descargar un
  comprobante sin antes pasar por el chequeo de permisos de la aplicación
  (la ruta que pide la URL firmada ya verificó `permission_required` antes
  de llamar aquí).

boto3 solo se importa (import perezoso) cuando el modo S3 está realmente
activo, para no exigir esa dependencia en desarrollo local."""
import mimetypes
import os

from flask import current_app


def using_s3():
    return bool(current_app.config.get("AWS_S3_BUCKET"))


def _local_dir():
    path = os.path.join(current_app.instance_path, "receipts")
    os.makedirs(path, exist_ok=True)
    return path


def local_receipts_dir():
    """Solo para el modo disco local — la ruta que sirve el archivo la usa
    con send_from_directory. No se debe llamar en modo S3."""
    return _local_dir()


def _s3_bucket():
    return current_app.config["AWS_S3_BUCKET"]


def _s3_prefix():
    # Permite compartir un bucket entre varias cosas si algún día hiciera
    # falta (ej. "harraso-erp/comprobantes"); por defecto todo va bajo
    # "comprobantes/".
    return (current_app.config.get("AWS_S3_PREFIX") or "comprobantes").strip("/")


def _s3_key(filename):
    return f"{_s3_prefix()}/{filename}"


def _s3_client():
    import boto3

    # boto3 puede tomar las credenciales (AWS_ACCESS_KEY_ID /
    # AWS_SECRET_ACCESS_KEY) y la región (AWS_DEFAULT_REGION) directo de las
    # variables de entorno estándar, pero NO recorta espacios ni saltos de
    # línea de esos valores — en producción real (31 ago) esto causó
    # "SignatureDoesNotMatch" persistente, resuelto leyendo y limpiando
    # (`strip()`) las variables acá mismo en vez de dejar que boto3 las tome
    # "tal cual".
    #
    # Además, para `generate_presigned_url()` específicamente (no para
    # put_object ni otras llamadas normales), boto3 puede terminar armando
    # la URL contra el endpoint "global" de S3 (que se valida como si fuera
    # us-east-1) en vez del endpoint regional real del bucket, aunque la
    # región pasada a `region_name` sea la correcta — visto en producción
    # real (31 ago) como "AuthorizationQueryParametersError: ... the region
    # 'us-east-2' is wrong; expecting 'us-east-1'" con un bucket confirmado
    # en us-east-2 (Ohio) desde la propia consola de AWS. Pasar
    # `endpoint_url` explícito con la región fuerza el host correcto sin
    # depender de esa resolución interna.
    access_key = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    region = (os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    kwargs = {}
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    if region:
        kwargs["region_name"] = region
        kwargs["endpoint_url"] = f"https://s3.{region}.amazonaws.com"
    return boto3.client("s3", **kwargs)


def save_receipt(filename, raw_bytes):
    """Guarda los bytes de un comprobante bajo `filename` (un nombre único
    ya generado por el llamador, ej. un uuid.hex + extensión). No sabe nada
    de fotos/PDFs ni de compresión — el llamador decide eso antes."""
    if using_s3():
        # Sin ContentType, S3 guarda el objeto como "binary/octet-stream"
        # por defecto — el navegador no sabe que es una imagen/PDF y fuerza
        # la descarga en vez de mostrarlo (visto en producción real, 31
        # ago: en disco local sí se veía bien, porque send_from_directory
        # infiere el tipo solo por la extensión; en S3 hay que decírselo
        # explícitamente al subir el archivo). ContentDisposition=inline
        # refuerza lo mismo para que el navegador lo abra en pestaña en vez
        # de descargarlo, incluso si por algún motivo no reconoce el tipo.
        content_type, _ = mimetypes.guess_type(filename)
        _s3_client().put_object(
            Bucket=_s3_bucket(),
            Key=_s3_key(filename),
            Body=raw_bytes,
            ServerSideEncryption="AES256",
            ContentType=content_type or "application/octet-stream",
            ContentDisposition="inline",
        )
    else:
        with open(os.path.join(_local_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def receipt_url(filename):
    """URL firmada de corta duración para descargar/ver un comprobante ya
    guardado en S3. Solo válida en modo S3 — en modo disco local, la ruta
    que sirve el archivo debe usar local_receipts_dir() + send_from_directory
    en su lugar (ver using_s3())."""
    content_type, _ = mimetypes.guess_type(filename)
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": _s3_bucket(),
            "Key": _s3_key(filename),
            # Se piden estos dos encabezados en la respuesta del propio
            # GET firmado (S3 los permite sobrescribir por request, sin
            # importar los metadatos guardados en el objeto) para que los
            # comprobantes subidos ANTES de este arreglo — que se guardaron
            # sin ContentType, por eso el navegador los descargaba en vez
            # de mostrarlos — también se abran bien, sin tener que volver
            # a subirlos.
            "ResponseContentType": content_type or "application/octet-stream",
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=300,
    )
