"""Almacenamiento de archivos subidos por los usuarios: los comprobantes de
gastos de Liquidaciones (ver app/routes/liquidaciones.py) y, desde el 1 sep,
las fotos de conductores (ver app/routes/conductores.py) — mismo mecanismo,
cada tipo bajo su propio prefijo/carpeta para no mezclarlos.

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
activo, para no exigir esa dependencia en desarrollo local.

30 sep, segundo WORKER TIMEOUT real en producción (después de arreglar el
de extract_pdf_text -- ver app/integrations/pdf_text.py): con el timeout de
gunicorn ya subido a 120s, "Cargar factura ya emitida (SUNAT)" en zip (ver
manual_zip() en app/routes/facturacion.py) volvió a colgarse, esta vez a
mitad de un put_object() a S3 -- el traceback de Render mostró el corte
justo adentro del handshake TLS (ssl_wrap_socket/load_verify_locations) al
conectar con S3. La causa: _s3_client() armaba un boto3.client() NUEVO en
CADA llamada (cada PDF y cada XML subido), así que cada archivo del zip
pagaba una conexión TCP+TLS nueva a S3 en vez de reutilizar una ya abierta
-- con varias facturas reales en un mismo zip (2 archivos por factura) eso
se acumula rápido y puede superar hasta un timeout ya generoso. Ahora
_s3_client() cachea el cliente a nivel de módulo (se arma una sola vez por
proceso worker y se reutiliza en todas las llamadas siguientes, dejando que
botocore reutilice sus conexiones HTTP con keep-alive) -- gunicorn (ver
Procfile) usa workers de tipo sync, un solo request a la vez por proceso,
así que no hace falta más que un lock simple para que la primera vez que
dos requests coincidan justo al arrancar un worker no arme el cliente dos
veces por accidente."""
import mimetypes
import os
import threading

from flask import current_app

_s3_client_cache = None
_s3_client_lock = threading.Lock()


def using_s3():
    return bool(current_app.config.get("AWS_S3_BUCKET"))


def _local_dir(subfolder):
    path = os.path.join(current_app.instance_path, subfolder)
    os.makedirs(path, exist_ok=True)
    return path


def local_receipts_dir():
    """Solo para el modo disco local — la ruta que sirve el archivo la usa
    con send_from_directory. No se debe llamar en modo S3."""
    return _local_dir("receipts")


def local_photos_dir():
    """Igual que local_receipts_dir() pero para las fotos de conductores
    (1 sep) — carpeta separada en disco para no mezclarlas con los
    comprobantes de gastos."""
    return _local_dir("driver_photos")


def local_carrier_waybills_dir():
    """Igual que local_receipts_dir()/local_photos_dir() pero para las
    guías de transportista adjuntas a un viaje (3 sep) — carpeta separada
    en disco para no mezclarlas con lo demás."""
    return _local_dir("carrier_waybills")


def local_delivery_proofs_dir():
    """Igual que local_carrier_waybills_dir() pero para la conformidad de
    entrega adjunta a un viaje (4 sep) — carpeta separada en disco."""
    return _local_dir("delivery_proofs")


def local_container_photos_dir():
    """Igual que local_carrier_waybills_dir() pero para la foto de evidencia
    del estado del contenedor (10 sep, solo cuando cargo_type='Contenedor')
    — carpeta separada en disco."""
    return _local_dir("container_photos")


def local_sunat_documents_dir():
    """Igual que las anteriores, pero para el PDF real que devuelve
    tefacturo.pe al emitir una factura o guía de remisión (7 sep, segunda
    ronda) — carpeta separada en disco para no mezclarla con lo demás."""
    return _local_dir("sunat_documents")


def local_vehicle_documents_dir():
    """Igual que las anteriores, pero para los documentos escaneados de una
    unidad de Flota (tarjeta de propiedad, SOAT, revisión técnica, MTC,
    póliza — 9 sep, pedido de Braulio). Los 5 tipos de documento comparten
    esta misma carpeta/prefijo (cada archivo tiene un nombre único
    generado con uuid.hex, igual que el resto de esta lista, así que no
    hay riesgo de choque entre ellos)."""
    return _local_dir("vehicle_documents")


def local_driver_documents_dir():
    """Igual que local_vehicle_documents_dir(), pero para los documentos
    escaneados de un CONDUCTOR (brevete, DNI, examen médico ocupacional —
    29 sep, pedido de Braulio). Carpeta separada de "driver_photos" (la
    foto del conductor, ver local_photos_dir()) porque son cosas distintas.
    Los 3 tipos de documento comparten esta misma carpeta/prefijo (cada
    archivo tiene un nombre único generado con uuid.hex, así que no hay
    riesgo de choque entre ellos)."""
    return _local_dir("driver_documents")


def local_staff_documents_dir():
    """Igual que local_driver_documents_dir(), pero para el LEGAJO digital
    del personal (contratos y documentos de cada trabajador -- 6 oct, pedido
    de Braulio: "algo similar a Buk"). Carpeta separada de los comprobantes
    de pago (local_staff_payment_receipts_dir)."""
    return _local_dir("staff_documents")


def local_staff_payment_receipts_dir():
    """Igual que las anteriores, pero para los comprobantes de pago de
    personal (boleta de planilla o recibo por honorarios — 18 sep, módulo
    nuevo "Pagos personal", ver app/routes/pagos_personal.py) — carpeta
    separada en disco."""
    return _local_dir("staff_payment_receipts")


def local_payment_vouchers_dir():
    """Igual que local_staff_payment_receipts_dir(), pero para las
    constancias de pago que da cada BANCO por un lote de Telecrédito (18
    sep, 4ta ronda — distinto del comprobante de cada persona) — carpeta
    separada en disco."""
    return _local_dir("payment_vouchers")


def _s3_bucket():
    return current_app.config["AWS_S3_BUCKET"]


def _s3_prefix():
    # Permite compartir un bucket entre varias cosas si algún día hiciera
    # falta (ej. "harraso-erp/comprobantes"); por defecto todo va bajo
    # "comprobantes/".
    return (current_app.config.get("AWS_S3_PREFIX") or "comprobantes").strip("/")


def _s3_photos_prefix():
    return (current_app.config.get("AWS_S3_PHOTOS_PREFIX") or "fotos-conductores").strip("/")


def _s3_carrier_waybills_prefix():
    return (current_app.config.get("AWS_S3_CARRIER_WAYBILLS_PREFIX") or "guias-transportista").strip("/")


def _s3_delivery_proofs_prefix():
    return (current_app.config.get("AWS_S3_DELIVERY_PROOFS_PREFIX") or "conformidad-entrega").strip("/")


def _s3_container_photos_prefix():
    return (current_app.config.get("AWS_S3_CONTAINER_PHOTOS_PREFIX") or "fotos-contenedor").strip("/")


def _s3_sunat_documents_prefix():
    return (current_app.config.get("AWS_S3_SUNAT_DOCUMENTS_PREFIX") or "comprobantes-sunat").strip("/")


def _s3_vehicle_documents_prefix():
    return (current_app.config.get("AWS_S3_VEHICLE_DOCUMENTS_PREFIX") or "documentos-flota").strip("/")


def _s3_driver_documents_prefix():
    return (current_app.config.get("AWS_S3_DRIVER_DOCUMENTS_PREFIX") or "documentos-conductores").strip("/")


def _s3_staff_documents_prefix():
    return (current_app.config.get("AWS_S3_STAFF_DOCUMENTS_PREFIX") or "legajo-personal").strip("/")


def _s3_staff_payment_receipts_prefix():
    return (current_app.config.get("AWS_S3_STAFF_PAYMENT_RECEIPTS_PREFIX") or "comprobantes-personal").strip("/")


def _s3_payment_vouchers_prefix():
    return (current_app.config.get("AWS_S3_PAYMENT_VOUCHERS_PREFIX") or "constancias-pago").strip("/")


def _s3_key(filename, prefix):
    return f"{prefix}/{filename}"


def _s3_client():
    # 30 sep: cacheado a nivel de módulo -- ver el comentario grande al
    # inicio del archivo. Antes se armaba un boto3.client() (con su propia
    # conexión TCP+TLS a S3) en cada llamada; ahora se arma una sola vez por
    # proceso worker y se reutiliza, para que subir varios archivos seguidos
    # (ej. el zip de "Cargar factura ya emitida") no pague una conexión
    # nueva por cada uno.
    global _s3_client_cache
    if _s3_client_cache is not None:
        return _s3_client_cache
    with _s3_client_lock:
        if _s3_client_cache is None:
            import boto3

            # boto3 puede tomar las credenciales (AWS_ACCESS_KEY_ID /
            # AWS_SECRET_ACCESS_KEY) y la región (AWS_DEFAULT_REGION) directo
            # de las variables de entorno estándar, pero NO recorta espacios
            # ni saltos de línea de esos valores — en producción real (31
            # ago) esto causó "SignatureDoesNotMatch" persistente, resuelto
            # leyendo y limpiando (`strip()`) las variables acá mismo en vez
            # de dejar que boto3 las tome "tal cual".
            #
            # Además, para `generate_presigned_url()` específicamente (no
            # para put_object ni otras llamadas normales), boto3 puede
            # terminar armando la URL contra el endpoint "global" de S3 (que
            # se valida como si fuera us-east-1) en vez del endpoint
            # regional real del bucket, aunque la región pasada a
            # `region_name` sea la correcta — visto en producción real (31
            # ago) como "AuthorizationQueryParametersError: ... the region
            # 'us-east-2' is wrong; expecting 'us-east-1'" con un bucket
            # confirmado en us-east-2 (Ohio) desde la propia consola de AWS.
            # Pasar `endpoint_url` explícito con la región fuerza el host
            # correcto sin depender de esa resolución interna.
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
            _s3_client_cache = boto3.client("s3", **kwargs)
        return _s3_client_cache


def _put_object(prefix, filename, raw_bytes):
    # Sin ContentType, S3 guarda el objeto como "binary/octet-stream" por
    # defecto — el navegador no sabe que es una imagen/PDF y fuerza la
    # descarga en vez de mostrarlo (visto en producción real, 31 ago: en
    # disco local sí se veía bien, porque send_from_directory infiere el
    # tipo solo por la extensión; en S3 hay que decírselo explícitamente al
    # subir el archivo). ContentDisposition=inline refuerza lo mismo para
    # que el navegador lo abra en pestaña en vez de descargarlo, incluso si
    # por algún motivo no reconoce el tipo.
    content_type, _ = mimetypes.guess_type(filename)
    _s3_client().put_object(
        Bucket=_s3_bucket(),
        Key=_s3_key(filename, prefix),
        Body=raw_bytes,
        ServerSideEncryption="AES256",
        ContentType=content_type or "application/octet-stream",
        ContentDisposition="inline",
    )


def _presigned_url(prefix, filename, as_attachment=False, download_name=None):
    """24 sep, pedido de Braulio ("cuando haga click quiero que se
    descargue, no que se abra en otra ventana del explorador" -- sobre el
    XML de un comprobante): `as_attachment=True` fuerza la descarga en vez
    de abrir el archivo en una pestaña nueva, igual que `as_attachment` de
    Flask para el modo disco local (ver send_from_directory() en
    facturacion.py/guias.py). `download_name`, si se pasa, es el nombre
    con el que se descarga el archivo (ej. "F001-000009.xml") en vez del
    nombre interno con el uuid.hex.

    30 sep, pedido de Braulio ("que el nombre del archivo sea la factura +
    su extension"): antes, cuando as_attachment=False (ej. "Ver PDF" de una
    factura, que se abre en una pestaña en vez de descargarse), el
    `download_name` se ignoraba del todo -- el disposition quedaba en
    "inline" sin ningún nombre, así que si el usuario le hacía "Guardar
    como" desde ahí, el navegador sugería el nombre feo de la URL firmada
    de S3 en vez de "F001-000009.pdf". Ahora el nombre bonito se manda
    también en modo "inline" (confirmado con una prueba directa: Flask ya
    hace esto mismo en disco local con send_from_directory(download_name=),
    sin necesidad de as_attachment=True) — sigue abriéndose en el navegador
    igual que antes, solo cambia el nombre sugerido al guardarlo."""
    content_type, _ = mimetypes.guess_type(filename)
    name = download_name or filename
    if as_attachment:
        disposition = f'attachment; filename="{name}"'
    else:
        disposition = f'inline; filename="{name}"'
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": _s3_bucket(),
            "Key": _s3_key(filename, prefix),
            # Se piden estos dos encabezados en la respuesta del propio
            # GET firmado (S3 los permite sobrescribir por request, sin
            # importar los metadatos guardados en el objeto) para que los
            # comprobantes subidos ANTES de este arreglo — que se guardaron
            # sin ContentType, por eso el navegador los descargaba en vez
            # de mostrarlos — también se abran bien, sin tener que volver
            # a subirlos.
            "ResponseContentType": content_type or "application/octet-stream",
            "ResponseContentDisposition": disposition,
        },
        ExpiresIn=300,
    )


def save_receipt(filename, raw_bytes):
    """Guarda los bytes de un comprobante bajo `filename` (un nombre único
    ya generado por el llamador, ej. un uuid.hex + extensión). No sabe nada
    de fotos/PDFs ni de compresión — el llamador decide eso antes."""
    if using_s3():
        _put_object(_s3_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_receipts_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def receipt_url(filename):
    """URL firmada de corta duración para descargar/ver un comprobante ya
    guardado en S3. Solo válida en modo S3 — en modo disco local, la ruta
    que sirve el archivo debe usar local_receipts_dir() + send_from_directory
    en su lugar (ver using_s3())."""
    return _presigned_url(_s3_prefix(), filename)


def save_driver_photo(filename, raw_bytes):
    """Igual que save_receipt(), pero para las fotos de conductores (1 sep)
    — mismo mecanismo (disco local o S3 según el ambiente), guardadas bajo
    un prefijo/carpeta separada para no mezclarlas con los comprobantes de
    gastos."""
    if using_s3():
        _put_object(_s3_photos_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_photos_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def driver_photo_url(filename):
    """Igual que receipt_url(), pero para una foto de conductor guardada en
    S3. En disco local, usar local_photos_dir() + send_from_directory."""
    return _presigned_url(_s3_photos_prefix(), filename)


def save_carrier_waybill(filename, raw_bytes):
    """Igual que save_receipt()/save_driver_photo(), pero para la guía de
    transportista adjunta a un viaje (3 sep) — carpeta/prefijo separado."""
    if using_s3():
        _put_object(_s3_carrier_waybills_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_carrier_waybills_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def carrier_waybill_url(filename):
    """Igual que receipt_url()/driver_photo_url(), pero para una guía de
    transportista guardada en S3. En disco local, usar
    local_carrier_waybills_dir() + send_from_directory."""
    return _presigned_url(_s3_carrier_waybills_prefix(), filename)


def local_shipper_waybills_dir():
    """Igual que local_carrier_waybills_dir() pero para la guía de remisión
    que emitió el REMITENTE (15 sep, pedido de Braulio) — carpeta separada
    en disco. Documento distinto de carrier_waybill_* (ver comentario en
    schema.sql junto a shipper_waybill_shows_carrier)."""
    return _local_dir("shipper_waybills")


def _s3_shipper_waybills_prefix():
    return (current_app.config.get("AWS_S3_SHIPPER_WAYBILLS_PREFIX") or "guias-remitente").strip("/")


def save_shipper_waybill(filename, raw_bytes):
    """Igual que save_carrier_waybill(), pero para la guía de remisión del
    remitente adjunta a un viaje (15 sep) — carpeta/prefijo separado."""
    if using_s3():
        _put_object(_s3_shipper_waybills_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_shipper_waybills_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def shipper_waybill_url(filename):
    """Igual que carrier_waybill_url(), pero para una guía del remitente
    guardada en S3. En disco local, usar local_shipper_waybills_dir() +
    send_from_directory."""
    return _presigned_url(_s3_shipper_waybills_prefix(), filename)


def save_delivery_proof(filename, raw_bytes):
    """Igual que save_carrier_waybill(), pero para la conformidad de entrega
    adjunta a un viaje (4 sep) — carpeta/prefijo separado."""
    if using_s3():
        _put_object(_s3_delivery_proofs_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_delivery_proofs_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def delivery_proof_url(filename):
    """Igual que carrier_waybill_url(), pero para una conformidad de entrega
    guardada en S3. En disco local, usar local_delivery_proofs_dir() +
    send_from_directory."""
    return _presigned_url(_s3_delivery_proofs_prefix(), filename)


def save_container_photo(filename, raw_bytes):
    """Igual que save_carrier_waybill()/save_delivery_proof(), pero para la
    foto de evidencia del estado del contenedor (10 sep) — carpeta/prefijo
    separado."""
    if using_s3():
        _put_object(_s3_container_photos_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_container_photos_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def container_photo_url(filename):
    """Igual que carrier_waybill_url()/delivery_proof_url(), pero para una
    foto de contenedor guardada en S3. En disco local, usar
    local_container_photos_dir() + send_from_directory."""
    return _presigned_url(_s3_container_photos_prefix(), filename)


def save_sunat_document(filename, raw_bytes):
    """Igual que save_receipt()/save_carrier_waybill(), pero para el PDF
    real que devuelve tefacturo.pe al emitir una factura o guía (7 sep,
    segunda ronda) — ver app/integrations/sunat_ose.py ->
    TefacturoClient.get_pdf_bytes()."""
    if using_s3():
        _put_object(_s3_sunat_documents_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_sunat_documents_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def sunat_document_url(filename, as_attachment=False, download_name=None):
    """Igual que receipt_url()/carrier_waybill_url(), pero para un PDF/XML de
    SUNAT guardado en S3. En disco local, usar
    local_sunat_documents_dir() + send_from_directory. `as_attachment`/
    `download_name`: ver _presigned_url()."""
    return _presigned_url(
        _s3_sunat_documents_prefix(), filename, as_attachment=as_attachment, download_name=download_name
    )


def save_vehicle_document(filename, raw_bytes):
    """Igual que save_carrier_waybill()/save_sunat_document(), pero para un
    documento escaneado de una unidad de Flota (9 sep) — carpeta/prefijo
    separado. Los 5 tipos de documento (tarjeta de propiedad, SOAT,
    revisión técnica, MTC, póliza) comparten esta misma función/carpeta."""
    if using_s3():
        _put_object(_s3_vehicle_documents_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_vehicle_documents_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def vehicle_document_url(filename):
    """Igual que carrier_waybill_url()/sunat_document_url(), pero para un
    documento de Flota guardado en S3. En disco local, usar
    local_vehicle_documents_dir() + send_from_directory."""
    return _presigned_url(_s3_vehicle_documents_prefix(), filename)


def save_driver_document(filename, raw_bytes):
    """Igual que save_vehicle_document(), pero para un documento escaneado
    de un CONDUCTOR (brevete, DNI, examen médico ocupacional — 29 sep,
    pedido de Braulio) — carpeta/prefijo separado. Los 3 tipos de
    documento comparten esta misma función/carpeta."""
    if using_s3():
        _put_object(_s3_driver_documents_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_driver_documents_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def driver_document_url(filename):
    """Igual que vehicle_document_url(), pero para un documento de
    conductor guardado en S3. En disco local, usar
    local_driver_documents_dir() + send_from_directory."""
    return _presigned_url(_s3_driver_documents_prefix(), filename)


def save_staff_document(filename, raw_bytes):
    """Igual que save_driver_document(), pero para un contrato o documento
    del legajo de una persona del catálogo de Personal."""
    if using_s3():
        _put_object(_s3_staff_documents_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_staff_documents_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def staff_document_url(filename):
    """Igual que driver_document_url(), pero para el legajo del personal
    guardado en S3. En disco local, usar local_staff_documents_dir() +
    send_from_directory."""
    return _presigned_url(_s3_staff_documents_prefix(), filename)


def save_staff_payment_receipt(filename, raw_bytes):
    """Igual que save_vehicle_document(), pero para el comprobante de un
    pago de personal (boleta de planilla o recibo por honorarios — 18 sep,
    módulo "Pagos personal") — carpeta/prefijo separado."""
    if using_s3():
        _put_object(_s3_staff_payment_receipts_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_staff_payment_receipts_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def staff_payment_receipt_url(filename):
    """Igual que vehicle_document_url(), pero para un comprobante de pago
    de personal guardado en S3. En disco local, usar
    local_staff_payment_receipts_dir() + send_from_directory."""
    return _presigned_url(_s3_staff_payment_receipts_prefix(), filename)


def save_payment_voucher(filename, raw_bytes):
    """Igual que save_staff_payment_receipt(), pero para la constancia de
    pago que da el banco por un lote de Telecrédito (18 sep, 4ta ronda) —
    carpeta/prefijo separado."""
    if using_s3():
        _put_object(_s3_payment_vouchers_prefix(), filename, raw_bytes)
    else:
        with open(os.path.join(local_payment_vouchers_dir(), filename), "wb") as f:
            f.write(raw_bytes)


def payment_voucher_url(filename):
    """Igual que staff_payment_receipt_url(), pero para una constancia de
    pago guardada en S3. En disco local, usar local_payment_vouchers_dir()
    + send_from_directory."""
    return _presigned_url(_s3_payment_vouchers_prefix(), filename)
