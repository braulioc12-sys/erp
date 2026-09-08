"""Cliente para emitir comprobantes electrónicos (facturas y guías de
remisión) ante SUNAT a través de tefacturo.pe (OSE — Operador de Servicios
Electrónicos autorizado por SUNAT).

CONFIRMADO contra la documentación técnica real (7 sep, segunda ronda —
Braulio compartió https://api.tefacturo.pe/doc/integracion/docs/get-started/,
que reemplaza al primer intento del mismo día basado en un PDF suelto de
2019 sin autenticación documentada ni guía de remisión):

- Base URL: https://jarvis.tefacturo.pe (configurable, TEFACTURO_BASE_URL).
- Autenticación: POST /tokenapi/secure/v2/login/token con
  {"aplicacion": {"codigo": "1"}, "clave", "mail", "ruc"} (ruc como número).
  Devuelve {"c2uToken": "<JWT>", "fechaExpiracion": "<ISO>", ...} — el JWT
  vale ~8 horas; se manda como "Authorization: Bearer <token>" en cada
  llamada siguiente. Es un login POR RUC (cada empresa — Harraso y BRMS —
  necesita su propia cuenta usuario/clave en tefacturo.pe, no un solo
  usuario para ambas).
- Emitir factura: PUT /factura-api/invoice2u/integracion/factura/{ruc}.
- Emitir guía de remisión (TRANSPORTISTA): POST
  /guiatransportista-api/invoice2u/integracion/guia-remision/transportista/{ruc}.
  tefacturo.pe también documenta un endpoint de guía REMITENTE
  (guiaremitente-api/.../guia-remision/{ruc}) — no se implementó aquí a
  propósito: la guía remitente la debe emitir el DUEÑO de la carga (el
  cliente de Harraso, con su propio RUC/cuenta), no la empresa de
  transporte. Harraso/BRMS, como transportistas, emiten la guía
  transportista — que es la que evidencia SU servicio de transporte.
  Avisar si esta interpretación no es la correcta.
- Consultar PDF ya emitido: PUT /pdfapi/pdfapi/consultarPdf/{ruc} con
  {"emisor", "numero", "serie", "tipoComprobante"} (01=Factura, 09=Guía) —
  la respuesta trae el PDF completo codificado en base64. Se usa aquí para
  guardar una copia del PDF real en el propio ERP apenas se emite un
  comprobante (mismo mecanismo de almacenamiento que el resto del sistema,
  ver app/storage.py) — así Braulio no depende de volver a consultarlo en
  el portal de tefacturo.pe para verlo o mandárselo a un cliente.
  tefacturo.pe también documenta /consultarXml y /consultarCdr (no
  implementados todavía — AJUSTAR si hace falta guardarlos también).

SIMPLIFICACIONES pendientes de confirmar con Braulio (quedan documentadas
para no perder el rastro; ver también README):
- **`destinatario` = mismo dato que `remitente`** en la guía transportista:
  el formato de tefacturo.pe distingue quién ENVÍA la carga (remitente) de
  quién la RECIBE en destino (destinatario) — hoy el ERP solo conoce un
  cliente por viaje (`trips.client_id`), así que se usa el mismo para
  ambos. Si en la práctica el destinatario suele ser una empresa distinta
  (ej. la sucursal de destino del cliente), avisar para agregar un campo
  aparte en Guías.
- **Ubigeo de partida/llegada**: SUNAT exige el código INEI de 6 dígitos
  del distrito de origen/destino en la guía — el ERP no tiene un catálogo
  de ubigeos, así que se pide como campo de texto manual en el formulario
  de Guías (`waybills.origin_ubigeo`/`destination_ubigeo`). Sin esto
  completo, no se puede enviar la guía a SUNAT (ver validación en
  build_waybill_payload).
- **Registro MTC**: requerido por SUNAT para la guía transportista, no
  existía ningún dato parecido en el sistema — ver
  HARRASO_MTC_REGISTRATION/BRMS_MTC_REGISTRATION en config.py, vacíos por
  defecto (AJUSTAR).
- **IGV**: se asume que todos los ítems de una factura son
  "GRAVADO_OPERACION_ONEROSA" (18%) — igual que el resto del sistema desde
  el primer intento de esta integración.

Antes de emitir un solo comprobante real:
1. Crea una cuenta de pruebas en tefacturo.pe para Harraso y/o BRMS
   (https://api.tefacturo.pe/doc/integracion/registro/) y consigue
   usuario/clave — configúralos en HARRASO_TEFACTURO_EMAIL/PASSWORD y
   BRMS_TEFACTURO_EMAIL/PASSWORD.
2. Completa BRMS_RUC/BRMS_ADDRESS (ver Cotizaciones, siguen vacíos) y
   HARRASO_MTC_REGISTRATION/BRMS_MTC_REGISTRATION.
3. Prueba primero con una factura/guía de prueba y pide a tu contador que
   revise el PDF resultante antes de usarlo con clientes reales.

Mientras tanto, el resto del ERP funciona perfectamente sin esto: las
facturas y guías se siguen generando y controlando dentro del sistema,
simplemente no quedan enviadas a SUNAT hasta que actives y confirmes esta
integración.
"""
import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IGV_RATE = 0.18

# Token cacheado en memoria del propio proceso, por RUC — evita loguearse
# de nuevo en cada envío mientras el token siga vigente (~8 horas). Esto es
# seguro en este proyecto concretamente porque el Procfile corre gunicorn
# SIN --workers (un solo proceso, ver app/scheduler.py) — si eso cambiara,
# este caché habría que revisarlo (cada worker tendría su propio caché, sin
# problema real más allá de logins de más).
_TOKEN_CACHE = {}
_TOKEN_SAFETY_BUFFER = timedelta(minutes=15)


class SunatOseError(Exception):
    """Error al comunicarse con tefacturo.pe, datos incompletos para armar
    el comprobante, o rechazo de SUNAT."""


class TefacturoClient:
    def __init__(self, ruc, email, password, base_url=None, timeout=25):
        self.ruc = (ruc or "").strip()
        self.email = (email or "").strip()
        self.password = password or ""
        self.base_url = (base_url or "https://jarvis.tefacturo.pe").rstrip("/")
        self.timeout = timeout

    def is_configured(self):
        return bool(self.ruc and self.email and self.password)

    def _require_configured(self):
        if not self.is_configured():
            raise SunatOseError(
                "La facturación electrónica no está configurada para esta empresa. "
                "Define su RUC, usuario y clave de tefacturo.pe en las variables de "
                "entorno correspondientes (ver README, sección 'Facturación "
                "electrónica (SUNAT)')."
            )

    def _request(self, method, path, body):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = self._get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise SunatOseError(f"tefacturo.pe respondió {exc.code} en {path}: {detail}")
        except urllib.error.URLError as exc:
            raise SunatOseError(f"No se pudo conectar con tefacturo.pe ({url}): {exc.reason}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise SunatOseError(f"Respuesta inesperada de tefacturo.pe: {exc}")

    def _login(self):
        self._require_configured()
        url = f"{self.base_url}/tokenapi/secure/v2/login/token"
        body = {
            "aplicacion": {"codigo": "1"},
            "clave": self.password,
            "mail": self.email,
            "ruc": int(self.ruc),
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                result = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise SunatOseError(
                f"tefacturo.pe rechazó el usuario/clave configurado para el RUC "
                f"{self.ruc} ({exc.code}): {detail}"
            )
        except urllib.error.URLError as exc:
            raise SunatOseError(f"No se pudo conectar con tefacturo.pe ({url}): {exc.reason}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise SunatOseError(f"Respuesta inesperada de tefacturo.pe al iniciar sesión: {exc}")

        token = result.get("c2uToken")
        if not token:
            raise SunatOseError("tefacturo.pe no devolvió un token de acceso válido.")
        expires_at = _parse_expiration(result.get("fechaExpiracion")) or (
            datetime.now(timezone.utc) + timedelta(hours=8)
        )
        _TOKEN_CACHE[self.ruc] = (token, expires_at)
        return token

    def _get_token(self):
        cached = _TOKEN_CACHE.get(self.ruc)
        if cached:
            token, expires_at = cached
            if datetime.now(timezone.utc) < (expires_at - _TOKEN_SAFETY_BUFFER):
                return token
        return self._login()

    def emit_factura(self, payload):
        self._require_configured()
        return self._request("PUT", f"/factura-api/invoice2u/integracion/factura/{self.ruc}", payload)

    def emit_guia_transportista(self, payload):
        self._require_configured()
        return self._request(
            "POST",
            f"/guiatransportista-api/invoice2u/integracion/guia-remision/transportista/{self.ruc}",
            payload,
        )

    def get_pdf_bytes(self, tipo_comprobante, serie, numero):
        """Descarga el PDF de un comprobante ya emitido y devuelve sus
        bytes (decodificados de base64). tipo_comprobante: '01' = factura,
        '09' = guía de remisión."""
        self._require_configured()
        body = {
            "emisor": int(self.ruc),
            "numero": int(numero),
            "serie": serie,
            "tipoComprobante": tipo_comprobante,
        }
        result = self._request("PUT", f"/pdfapi/pdfapi/consultarPdf/{self.ruc}", body)
        b64 = _extract_base64_pdf(result)
        if not b64:
            raise SunatOseError("tefacturo.pe no devolvió el PDF del comprobante.")
        return base64.b64decode(b64)


def _extract_base64_pdf(result):
    """El manual de tefacturo.pe describe la respuesta de consultarPdf
    simplemente como 'el PDF completo en base64', sin especificar si viaja
    como texto plano o dentro de un campo JSON — se cubren ambos casos."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("pdf", "archivo", "documento", "base64", "contenido", "data"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _parse_expiration(value):
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _split_igv(total):
    """A partir de un monto que YA incluye IGV, devuelve (gravada, igv)."""
    gravada = round(total / (1 + IGV_RATE), 2)
    igv = round(total - gravada, 2)
    return gravada, igv


def build_invoice_payload(invoice, items, client, company):
    """Arma el JSON de una FACTURA en el formato real de tefacturo.pe
    (close2u / datosDocumento / detalleDocumento / emisor / receptor /
    informacionAdicional — confirmado 7 sep).

    invoice: fila de `invoices`. items: filas de `invoice_items`.
    client: fila de `clients` (requiere `ruc`). company: dict devuelto por
    `company_info_for_issuer()`.
    """
    if not client["ruc"]:
        raise SunatOseError(
            f"El cliente '{client['name']}' no tiene RUC registrado; una factura "
            "electrónica requiere el RUC del cliente. Edita el cliente y agrégalo."
        )

    detalle = []
    for it in items:
        gravada, _igv = _split_igv(float(it["amount"]))
        detalle.append(
            {
                "codigoProducto": f"SERV-{it['trip_id']}",
                "descripcion": it["description"] or "Servicio de transporte de carga",
                "tipoAfectacion": "GRAVADO_OPERACION_ONEROSA",
                # Confirmado contra la API real (8 sep, primer envío real de
                # Braulio): "SERVICIO" no es un valor aceptado — el propio
                # error 400 de tefacturo.pe listó el catálogo completo del
                # enum UnidadMedida, y "UNIDAD_SERVICIOS" es el que
                # corresponde a un servicio (no un bien físico).
                "unidadMedida": "UNIDAD_SERVICIOS",
                "cantidad": "1",
                "valorVentaUnitarioItem": gravada,
            }
        )

    forma_pago = "CREDITO" if invoice["due_date"] else "CONTADO"

    return {
        "close2u": {
            "tipoIntegracion": "OFFLINE",
            "tipoPlantilla": "01",
            # Los montos de detalleDocumento van SIN IGV (ver _split_igv) —
            # tefacturo.pe calcula el IGV (18%) y los totales por su cuenta,
            # evitando descuadres de redondeo entre lo que nosotros
            # calculamos y lo que SUNAT valida.
            "tipoRegistro": "PRECIOS_SIN_IGV",
        },
        "datosDocumento": {
            "serie": invoice["series"],
            "numero": invoice["series_number"],
            "moneda": "PEN",
            "fechaEmision": invoice["issue_date"],
            "horaEmision": None,
            "formaPago": forma_pago,
            "ordencompra": None,
            "glosa": invoice["notes"] or "",
        },
        "detalleDocumento": detalle,
        "emisor": {
            "correo": company.get("email", ""),
            "nombreComercial": company.get("commercial_name", ""),
            "nombreLegal": company.get("legal_name", ""),
            "numeroDocumentoIdentidad": company.get("ruc", ""),
            "tipoDocumentoIdentidad": "RUC",
        },
        "receptor": {
            "correo": client["email"] or "",
            "domicilioFiscal": {
                "direccion": client["address"] or "",
            },
            "nombreComercial": client["name"],
            "nombreLegal": client["name"],
            "numeroDocumentoIdentidad": client["ruc"],
            "tipoDocumentoIdentidad": "RUC",
        },
        "informacionAdicional": {
            "tipoOperacion": "VENTA_INTERNA",
        },
    }


def build_waybill_payload(waybill, trip, company, client):
    """Arma el JSON de una GUÍA DE REMISIÓN — TRANSPORTISTA en el formato
    real de tefacturo.pe (confirmado 7 sep). `company` es quien transporta
    (Harraso o BRMS, el `transportista`); `client` (fila de `clients`) se
    usa tanto como `remitente` como `destinatario` — ver la nota de
    "SIMPLIFICACIONES" al inicio de este archivo."""
    missing = []
    if not client["ruc"]:
        missing.append(f"el cliente '{client['name']}' no tiene RUC registrado")
    if not company.get("mtc_registration"):
        missing.append(f"falta el registro MTC de {company.get('name')} (HARRASO_MTC_REGISTRATION/BRMS_MTC_REGISTRATION)")
    if not waybill["origin_ubigeo"] or not waybill["destination_ubigeo"]:
        missing.append("falta el ubigeo de partida y/o llegada (SUNAT lo exige, 6 dígitos)")
    if not waybill["vehicle_plate"]:
        missing.append("falta la placa del vehículo")
    if not waybill["driver_document"]:
        missing.append("falta el DNI del conductor")
    if not str(waybill["series"]).upper().startswith("V"):
        missing.append(f"la serie de la guía ('{waybill['series']}') debe empezar con 'V' para tefacturo.pe")
    if missing:
        raise SunatOseError(
            "No se puede enviar la guía a SUNAT todavía: " + "; ".join(missing) + "."
        )

    party = {
        "correo": client["email"] or "",
        "nombreComercial": client["name"],
        "nombreLegal": client["name"],
        "numeroDocumentoIdentidad": client["ruc"],
        "tipoDocumentoIdentidad": "RUC",
    }

    return {
        "close2u": {
            "tipoIntegracion": "OFFLINE",
            "tipoPlantilla": "01",
        },
        "datosDocumento": {
            "serie": waybill["series"],
            "numero": waybill["series_number"],
            "fechaEmision": waybill["issue_date"],
            "glosa": waybill["notes"] or trip["cargo_description"] or "",
        },
        "remitente": party,
        "destinatario": dict(party),
        "datosEnvio": {
            "motivoTraslado": waybill["transfer_reason"] or "OTROS",
            "indicadorVehiculosConductoresTransportista": "False",
            "indicadorTrasladoVehiculosM1oL": "False",
            "indicadorTrasladoTotalDAM": "False",
            # Harraso/BRMS transportan carga de terceros por encargo (nunca
            # su propia mercadería) — desde la perspectiva del remitente
            # esto siempre es transporte "PUBLICO" (contratado a un
            # tercero), no "PRIVADO" (con vehículo propio del remitente).
            "modalidadTraslado": "PUBLICO",
            "descripcionTraslado": waybill["notes"] or trip["cargo_description"] or "",
            "transbordoProgramado": "False",
            "retornoVehiculoContenedoresVacios": "False",
            "retornoVehiculoVacio": "False",
            "unidadMedida": "KILOS",
            "pesoBruto": str(waybill["weight_kg"] or 0),
            "numeroBultos": waybill["packages"] or None,
            "fechaTraslado": waybill["issue_date"],
            "fechaEntrega": trip["delivered_date"] or waybill["issue_date"],
            "puntoPartida": {
                "ubigeo": waybill["origin_ubigeo"],
                "direccion": waybill["origin_address"] or trip["origin"],
            },
            "puntoLlegada": {
                "ubigeo": waybill["destination_ubigeo"],
                "direccion": waybill["destination_address"] or trip["destination"],
            },
        },
        "transportista": {
            "correo": company.get("email", ""),
            "nombreComercial": company.get("commercial_name", ""),
            "nombreLegal": company.get("legal_name", ""),
            "numeroDocumentoIdentidad": company.get("ruc", ""),
            "tipoDocumentoIdentidad": "RUC",
            "registroMTC": company.get("mtc_registration", ""),
        },
        "conductores": [
            {
                "nombreLegal": waybill["driver_name"] or "",
                "numeroDocumentoIdentidad": waybill["driver_document"] or "",
                "tipoDocumentoIdentidad": "DOC_NACIONAL_DE_IDENTIDAD",
                "liscenciaConducir": waybill["driver_license"] or "",
            }
        ],
        "vehiculos": [
            {"placa": waybill["vehicle_plate"] or ""},
        ],
        "detalleGuia": [
            {
                "numeroOrden": 1,
                "codigoProducto": f"CARGA-{trip['id']}",
                "descripcion": trip["cargo_description"] or "Carga general",
                "unidadMedida": "KILOGRAMO",
                "unidadNombre": "KILOGRAMO",
                "cantidad": waybill["weight_kg"] or 1,
            }
        ],
    }


def build_client_from_config(app_config, issuer="HARRASO"):
    """Devuelve el cliente de tefacturo.pe de la empresa emisora
    correspondiente (Harraso o BRMS). Cada una tiene su propia
    cuenta/credenciales (usuario/clave) — ver HARRASO_TEFACTURO_EMAIL/
    PASSWORD y BRMS_TEFACTURO_EMAIL/PASSWORD en config.py."""
    base_url = app_config.get("TEFACTURO_BASE_URL")
    if issuer == "BRMS":
        return TefacturoClient(
            ruc=app_config.get("BRMS_RUC"),
            email=app_config.get("BRMS_TEFACTURO_EMAIL"),
            password=app_config.get("BRMS_TEFACTURO_PASSWORD"),
            base_url=base_url,
        )
    return TefacturoClient(
        ruc=app_config.get("COMPANY_RUC"),
        email=app_config.get("HARRASO_TEFACTURO_EMAIL"),
        password=app_config.get("HARRASO_TEFACTURO_PASSWORD"),
        base_url=base_url,
    )


def parse_ose_response(response):
    """Normaliza la respuesta de tefacturo.pe a un dict simple. Una
    respuesta HTTP no-2xx ya levanta SunatOseError antes de llegar aquí
    (ver TefacturoClient._request) — si llegamos hasta acá, el comprobante
    fue aceptado. pdf_url/xml_url/cdr_url se completan aparte (ver
    send_sunat() en facturacion.py/guias.py), después de descargar y
    guardar el PDF con get_pdf_bytes()."""
    identificador = response.get("identificador")
    if not identificador:
        return {
            "accepted": False,
            "message": f"Respuesta inesperada de tefacturo.pe: {response}",
            "pdf_url": None,
            "xml_url": None,
            "cdr_url": None,
        }
    return {
        "accepted": True,
        "message": f"Aceptado por SUNAT (identificador {identificador}).",
        "pdf_url": None,
        "xml_url": None,
        "cdr_url": None,
    }
