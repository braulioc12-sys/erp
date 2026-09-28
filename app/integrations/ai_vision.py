"""Lectura con IA (con visión) de un screenshot para precargar una factura
de Facturación → "Facturar desde imagen" (28 sep, pedido de Braulio: "quiero
que yo subiendo una imagen crees la factura... como la 13").

El caso real que motivó esto: el portal de un cliente (ver captura
compartida por Braulio) muestra una tabla "Listado entrega de
mercancías/HES" con columnas "Documento de compra" (número de OC),
"Número HES", "Sociedad" (el cliente) e "Imp. recepcionado" (el monto a
facturar). Se sube ese screenshot y se extraen esos 4 datos con un modelo
de IA con visión (Anthropic) para precargar el formulario de nueva
factura -- SIEMPRE con un paso de revisión antes de crear la factura de
verdad (ver from_image_confirm() en app/routes/facturacion.py), nunca
automático sin que Braulio lo vea: es un documento fiscal real y la IA
puede leer mal un dato.

Distinto de la extracción de gastos por WhatsApp (ver
app/routes/liquidaciones.py, whatsapp_intake()): esa corre AFUERA del ERP,
en un workflow de n8n, que llama a Anthropic con su propia cuenta y le
manda al ERP el resultado ya extraído. Acá, en cambio, no hay WhatsApp de
por medio -- es una subida directa desde el navegador -- así que la
llamada a la IA la hace el propio ERP, con su PROPIA API key
(ANTHROPIC_API_KEY, ver config.py). Se usa `urllib.request` (sin el SDK
`anthropic`, que no está entre las dependencias de este proyecto) para no
agregar una dependencia nueva -- mismo criterio que el resto de las
integraciones externas de este proyecto (sunat_ose.py, frotcom.py,
sunat_ruc.py)."""
import base64
import io
import json
import urllib.error
import urllib.request

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
# Mismo modelo que ya usa el workflow de n8n de Braulio para la extracción
# de gastos por WhatsApp (ver n8n/whatsapp-factura-intake.json, nodo
# "Extraer datos con IA") -- se reutiliza el mismo id a propósito, es el
# que ya está confirmado funcionando contra su cuenta/plan de Anthropic.
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# Tamaño máximo del lado más largo de la imagen antes de mandarla a la IA
# (redimensionada con Pillow, ya una dependencia de este proyecto -- ver
# _compress_receipt_image en app/routes/liquidaciones.py, mismo criterio):
# un screenshot de celular puede pesar varios MB sin necesidad -- una
# imagen más chica cuesta menos tokens y tarda menos, sin perder legibilidad
# del texto de una tabla como la de este caso.
MAX_IMAGE_DIMENSION = 1600
JPEG_QUALITY = 85

# Tipos de imagen que la API de Anthropic acepta directamente (Vision) --
# a diferencia de VEHICLE_DOCUMENT_MIME_TO_EXTENSION (flota.py), acá NO se
# acepta PDF ni HEIC: un screenshot subido desde un navegador o celular
# recién exportado casi siempre es JPEG o PNG.
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class AiVisionError(Exception):
    """La API de Anthropic no está configurada, no se pudo contactar, o
    devolvió algo que no se pudo interpretar como el JSON esperado."""


def _compress_image(raw_bytes):
    """Redimensiona/recomprime la imagen a JPEG antes de mandarla a la IA
    (ver MAX_IMAGE_DIMENSION arriba). Si Pillow no puede abrirla (formato
    raro, archivo corrupto), se manda tal cual -- que sea la propia API de
    Anthropic la que la rechace con un mensaje claro, en vez de fallar acá
    con un error genérico de PIL."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return raw_bytes, "image/jpeg"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return buffer.getvalue(), "image/jpeg"
    except Exception:
        return raw_bytes, "image/jpeg"


# Instrucción para el modelo: mismo espíritu que el prompt real que ya usa
# el workflow de n8n para gastos por WhatsApp (n8n/whatsapp-factura-intake.json,
# nodo "Extraer datos con IA") -- JSON puro, sin texto alrededor, y "null"
# en vez de inventar/adivinar un dato que no se ve con confianza en la
# imagen (es un documento fiscal real, mejor un campo vacío para completar
# a mano que un dato incorrecto).
_PROMPT = """Esta imagen es un screenshot del portal de un cliente, con una tabla de \
"entrega de mercancías" o "HES" (hoja de entrada de servicio). Extrae EXACTAMENTE estos \
4 datos de la tabla:

- numero_oc: el número que aparece en la columna "Documento de compra" (la orden de compra).
- numero_hes: el número que aparece en la columna "Número HES".
- sociedad: el nombre que aparece en la columna "Sociedad" (el cliente al que se le factura).
- monto: el importe que aparece en la columna "Imp. recepcionado" (o "Importe recepcionado"), \
como número plano SIN separador de miles y con punto decimal (ejemplo: si en la imagen dice \
"8.400,00" o "8,400.00", el valor de "monto" debe ser 8400.00). Si la columna trae también la \
moneda (ej. "PEN"), no la incluyas en "monto", va aparte en "moneda".
- moneda: el código de moneda si aparece junto al importe (ej. "PEN"), o null si no aparece.

Si hay más de una fila en la tabla, usa la PRIMERA fila con datos.

Responde ÚNICAMENTE con un objeto JSON con exactamente estas 5 claves (numero_oc, numero_hes, \
sociedad, monto, moneda), sin texto antes ni después, sin bloque de código markdown. Si no \
puedes leer algún dato con confianza, pon su valor en null -- nunca inventes ni adivines un \
número o nombre que no se vea claro en la imagen."""


def extract_invoice_fields_from_image(raw_bytes, mime_type, api_key, timeout=45):
    """Manda la imagen a Anthropic (Claude con visión) y devuelve un dict
    {numero_oc, numero_hes, sociedad, monto, moneda} -- cualquier campo que
    la IA no haya podido leer con confianza llega como None. Lanza
    AiVisionError si falta la API key, si no se pudo contactar a Anthropic,
    o si la respuesta no se pudo interpretar como el JSON esperado."""
    if not api_key:
        raise AiVisionError(
            "La lectura automática de screenshots no está configurada todavía "
            "(falta ANTHROPIC_API_KEY). Completa la factura a mano mientras tanto."
        )
    if (mime_type or "").lower() not in ALLOWED_IMAGE_MIME_TYPES:
        # Se intenta igual como JPEG -- por ejemplo, un navegador que mandó
        # el archivo sin mimetype (application/octet-stream) pero que sí es
        # una imagen válida. Si de verdad no lo es, la recompresión de
        # abajo o la propia API de Anthropic lo van a rechazar con un
        # mensaje claro.
        mime_type = "image/jpeg"

    compressed_bytes, compressed_mime = _compress_image(raw_bytes)
    b64 = base64.b64encode(compressed_bytes).decode("ascii")

    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": compressed_mime, "data": b64},
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            result = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise AiVisionError(f"Anthropic respondió {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise AiVisionError(f"No se pudo conectar con Anthropic: {exc.reason}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise AiVisionError(f"Respuesta inesperada de Anthropic: {exc}")

    try:
        text = result["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise AiVisionError(f"Anthropic no devolvió el texto esperado: {result}")

    # Igual que el nodo "Parsear JSON de la IA" del workflow de n8n: el
    # modelo casi siempre responde JSON puro (se le pidió explícitamente),
    # pero por si acaso envuelve la respuesta en un bloque ```json ... ```,
    # se le quitan esas cercas antes de parsear.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AiVisionError(f"No se pudo interpretar el JSON devuelto por la IA: {exc} -- texto: {text[:500]}")

    return {
        "numero_oc": _clean_str(data.get("numero_oc")),
        "numero_hes": _clean_str(data.get("numero_hes")),
        "sociedad": _clean_str(data.get("sociedad")),
        "monto": _clean_float(data.get("monto")),
        "moneda": _clean_str(data.get("moneda")),
    }


def _clean_str(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _clean_float(value):
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
