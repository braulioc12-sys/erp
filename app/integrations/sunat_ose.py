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
  {"emisor", "numero", "serie", "tipoComprobante"} (01=Factura, 31=Guía de
  remisión TRANSPORTISTA — ver la nota del 14 sep en get_pdf_bytes() sobre
  el "09" que se usaba antes por error) — la respuesta trae el PDF completo
  codificado en base64. Se usa aquí para
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
- **`modalidadTransporte` fija en "TRANSPORTE_PUBLICO"**: Harraso/BRMS
  siempre emiten la guía transportista como transportistas que cobran por
  el servicio a un tercero (nunca mueven su propia carga con su propia
  flota) — ese es exactamente el caso "TRANSPORTE_PUBLICO" del catálogo de
  tefacturo.pe (visto en el ejemplo real de "Guía Remitente", que usa
  TRANSPORTE_PRIVADO para el caso contrario: una empresa moviendo su
  propia mercadería). No hay ningún campo/formulario para elegirlo porque
  no varía caso a caso en este negocio — avisar si alguna vez sí debiera
  variar.
- **Un solo conductor y un solo vehículo por guía** (`conductores`/
  `vehiculos` en el JSON real son arrays que soportan varios, con un flag
  `principal` por entrada — pensado para doble conductor o tracto+carreta):
  el formulario de Guías (`waybills`) solo guarda un conductor y una placa,
  así que se manda un único elemento con `principal: true` en cada array.
  Si Braulio necesita reportar el segundo conductor de un viaje de doble
  conductor, o la carreta además del tracto, en la guía transportista,
  seria una ampliación aparte del formulario — no incluida en este fix.
- **IGV**: se asume que todos los ítems de una factura son
  "GRAVADO_OPERACION_ONEROSA" (18%) — igual que el resto del sistema desde
  el primer intento de esta integración.

**Corrección importante (9 sep)**: la versión anterior de `build_waybill_payload()`
(escrita el 8 sep contra la página pública de documentación de
tefacturo.pe, https://api.tefacturo.pe/doc/integracion/docs/api/guia-transportista/)
exigía `datosDocumento.codigoAlmacen` (HARRASO_WAREHOUSE_CODE/
BRMS_WAREHOUSE_CODE) y usaba una estructura con un objeto `datosEnvio`
anidado (`transbordoProgramado`, `numeroPallet`, `transporteSubcontratado`,
`trasladoTotalBienes`, etc.) — eso bloqueaba a Braulio con "falta el código
de almacén..." antes de siquiera intentar el envío real. Braulio compartió
la colección Postman OFICIAL de tefacturo.pe (9 sep), que trae un ejemplo
de request para "Guía Transportista" y otro para "Guía Remitente" — ambos
con una estructura MÁS SIMPLE y SIN `codigoAlmacen` ni `datosEnvio`: los
campos de envío (`motivoTraslado`, `modalidadTransporte`,
`fechaInicioTraslado`, `pesoBrutoTotal`, `unidadPeso`, `numeroBultos`,
`puntoPartida`, `puntoLlegada`) van SUELTOS al nivel raíz del JSON, no
anidados. Se volvió a consultar la página pública de documentación citando
su JSON de ejemplo verbatim, y por increíble que parezca SIGUE mostrando
la estructura vieja (con `codigoAlmacen`/`datosEnvio`) — es decir, la
documentación pública está desactualizada frente a la colección Postman
real que tefacturo.pe le dio a Braulio. Se optó por confiar en la colección
Postman (evidencia concreta y consistente entre AMBOS endpoints de guía,
remitente y transportista, con el mismo `baseUrl` real
`https://jarvis.tefacturo.pe`) en vez de la documentación pública. Esto NO
se pudo probar contra el servidor real de tefacturo.pe desde este entorno
(sin credenciales ni acceso de red) — **Braulio: la primera guía que
mandes con este cambio, revisa bien el resultado (aceptada/rechazada y el
PDF) antes de asumir que quedó resuelto del todo, y si sigue fallando
mándame el error exacto de nuevo.**

**Segunda corrección, ya confirmada contra el servidor real (9 sep, mismo
día — Braulio mandó el primer envío real con el fix anterior y compartió
la respuesta 400 completa de tefacturo.pe)**: la estructura general (sin
`codigoAlmacen`/`datosEnvio`) quedó validada — el servidor llegó a
procesar el JSON completo — pero rechazó `conductores[].tipoDocumentoIdentidad:
"DNI"` (el valor que trae el ejemplo de la colección Postman) con un 400
que además trae, en el mensaje de error, la lista completa de valores que
sí acepta ese enum: `RUC, PERMISO_TEMPORAL_PERMANENCIA,
DOC_NACIONAL_DE_IDENTIDAD, CED_DIPLOMATICA_IDENTIDAD, CARNET_DE_EXTRANJERIA,
DOC_TRIB_NO_DOM_SIN_RUC_GUION, DOC_TRIB_NO_DOM_SIN_RUC, PASAPORTE`. Es
decir, "DNI" corresponde a `DOC_NACIONAL_DE_IDENTIDAD` (el valor largo que
ya se usaba en la versión del 8 sep) — la colección Postman tenía un error
puntual en ese único campo. Corregido: `conductores[].tipoDocumentoIdentidad`
vuelve a `"DOC_NACIONAL_DE_IDENTIDAD"`. Esta vez sí hay evidencia directa
del servidor real (no solo documentación) de que el resto de la estructura
es correcta — este es el primer ajuste de esta integración confirmado así.

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

from app.ubigeo import validar_ubigeo

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
        '31' = guía de remisión TRANSPORTISTA (Catálogo No. 01 de SUNAT —
        '09' es la guía de remisión REMITENTE, un documento distinto que
        este sistema no emite; ver la nota del 14 sep más abajo).

        Confirmado en real (8 sep, Factura F-0003 — quedó ACEPTADA por
        SUNAT, pero la descarga del PDF falló con "Respuesta inesperada de
        tefacturo.pe: Expecting value: line 1 column 1 (char 0)"):
        `consultarPdf` NO envuelve la respuesta en JSON como el resto de
        endpoints — devuelve el base64 del PDF como texto plano directo
        (sin comillas ni llaves), así que `_request()` (que siempre hace
        `json.loads`) fallaba antes de llegar siquiera a `_extract_base64_pdf`.
        Por eso esta llamada se arma a mano en vez de reusar `_request()`:
        intenta interpretar la respuesta como JSON (por si en algún caso sí
        viene envuelta) y, si eso falla, usa el texto tal cual.

        Nota (14 sep, patch 0033): guias.py llamaba a este método con
        tipo_comprobante='09' para la guía TRANSPORTISTA. Una guía real
        quedó ACEPTADA por SUNAT pero la descarga del PDF falló con 404:
        "No se encontró el tipo: 09 del comprobante: V001_10 para el RUC:
        ...". Según el Catálogo No. 01 de SUNAT (confirmado contra el
        anexo oficial: sunat.gob.pe/legislacion/superin/2017/anexoE-245-2017.pdf),
        el código 09 es "GUIA DE REMISION REMITENTE" y el código 31 es
        "GUIA DE REMISION TRANSPORTISTA" — este sistema únicamente emite
        la guía TRANSPORTISTA (ver la nota del endpoint de emisión más
        arriba), así que el tipoComprobante correcto para consultar su PDF
        es '31', no '09'. Corregido en guias.py."""
        self._require_configured()
        body = {
            "emisor": int(self.ruc),
            "numero": int(numero),
            "serie": serie,
            "tipoComprobante": tipo_comprobante,
        }
        url = f"{self.base_url}/pdfapi/pdfapi/consultarPdf/{self.ruc}"
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = self._get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise SunatOseError(f"tefacturo.pe respondió {exc.code} al consultar el PDF: {detail}")
        except urllib.error.URLError as exc:
            raise SunatOseError(f"No se pudo conectar con tefacturo.pe ({url}): {exc.reason}")

        raw = raw.strip()
        if not raw:
            raise SunatOseError("tefacturo.pe no devolvió el PDF del comprobante.")

        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            result = raw

        b64 = _extract_base64_pdf(result)
        if not b64:
            raise SunatOseError("tefacturo.pe no devolvió el PDF del comprobante.")
        try:
            return base64.b64decode(b64.strip(), validate=False)
        except (ValueError, TypeError) as exc:
            raise SunatOseError(f"El PDF devuelto por tefacturo.pe no se pudo decodificar: {exc}")


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

    PENDIENTE (9 sep) — Detracción (SPOT): cuando `invoice.detraction_applies`
    es verdadero (ver `compute_detraction()` en app/helpers.py), esta
    factura debería reportarle a SUNAT el concepto de detracción (código de
    bien, porcentaje, monto, cuenta del Banco de la Nación) — es un dato
    real del comprobante electrónico UBL, no solo informativo. Este payload
    TODAVÍA NO lo incluye: se revisó la documentación pública de
    tefacturo.pe (tabla de campos de este endpoint, el JSON de ejemplo
    completo, la página de "Ejemplos", y el catálogo de valores de
    `tipoOperacion`) en varias pasadas independientes y en ninguna aparece
    un campo de detracción — a diferencia de `codigoAlmacen` en la guía
    transportista (que sí estaba, aunque solo en el ejemplo), acá no hay
    ninguna pista de dónde iría. Se le pidió a Braulio consultarlo
    directamente con el soporte de tefacturo.pe (ellos sí lo soportan desde
    su portal web, con un toggle "Activar detracción" al emitir a mano) —
    en cuanto tengamos el nombre real del campo, agregarlo acá."""
    if not client["ruc"]:
        raise SunatOseError(
            f"El cliente '{client['name']}' no tiene RUC registrado; una factura "
            "electrónica requiere el RUC del cliente. Edita el cliente y agrégalo."
        )

    # Confirmado contra la API real (8 sep, segundo envío real de Braulio):
    # un ítem con valorVentaUnitarioItem = 0 hace que tefacturo.pe rechace
    # TODA la factura ("Al menos debe ingresar un precio de venta
    # referencial" / "El descuento no puede ser mayor o igual al valor de
    # item") — SUNAT no admite un ítem "GRAVADO_OPERACION_ONEROSA" (venta
    # con contraprestación) con valor cero; sería una contradicción legal.
    # Se valida ACÁ, antes de llamar a tefacturo.pe, para dar un mensaje
    # claro señalando qué viaje tiene tarifa en 0 en vez de dejar que el
    # error genérico y confuso de la API llegue tal cual al usuario.
    sin_tarifa = [it for it in items if not float(it["amount"] or 0) > 0]
    if sin_tarifa:
        # `items` no siempre trae `trip_code` (algunas consultas solo hacen
        # SELECT * FROM invoice_items, sin JOIN con trips) — se usa si está
        # disponible, y si no, se cae al id del viaje.
        codigos = ", ".join(
            (it["trip_code"] if "trip_code" in it.keys() and it["trip_code"] else f"viaje #{it['trip_id']}")
            for it in sin_tarifa
        )
        raise SunatOseError(
            f"El viaje {codigos} tiene tarifa S/ 0.00 en esta factura — SUNAT no permite "
            "un ítem de venta con valor cero. Corrige la tarifa de ese viaje (o quítalo de "
            "esta factura y factúralo aparte, si de verdad no tiene costo) antes de enviar."
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
    real de tefacturo.pe. `company` es quien transporta (Harraso o BRMS, el
    `transportista`); `client` (fila de `clients`) se usa tanto como
    `remitente` como `destinatario` — ver la nota de "SIMPLIFICACIONES" al
    inicio de este archivo.

    **14 sep, patch 0032 — CAUSA REAL ENCONTRADA Y CONFIRMADA por soporte
    técnico de tefacturo.pe (Jorge, vía WhatsApp), con evidencia de un 201
    Created real.** El `NullPointerException` en
    `BaseDispatchAdviceBuilder.buildPoint` que persistía desde el reporte
    original (ver patches 0028/0029) NUNCA fue por el ubigeo, la dirección,
    ni por campos faltantes al nivel raíz — fue porque, desde el patch 0017
    (9 sep), este payload manda `puntoPartida`/`puntoLlegada` y el resto de
    los datos de envío SUELTOS en la raíz del JSON, cuando el endpoint de
    guía TRANSPORTISTA los espera anidados dentro de un objeto
    `datosEnvio`. Al no encontrar esa clave, el deserializador de
    tefacturo.pe simplemente deja `datosEnvio` en `null` (JSON tolera
    campos desconocidos sin error) — y `buildPoint()`, que arma el punto de
    partida/llegada A PARTIR de `datosEnvio`, revienta con NPE apenas
    intenta leerlo. Por eso el error era IDÉNTICO sin importar qué tan
    válidos fueran el ubigeo o la dirección: nunca llegaban a usarse.

    Esto explica también por qué el patch 0017/0018 sí pudo confirmarse
    parcialmente contra el servidor real en su momento (un 400 real sobre
    `conductores[].tipoDocumentoIdentidad`): `conductores`/`vehiculos`
    SIEMPRE fueron correctos sueltos en la raíz (nunca estuvieron dentro de
    `datosEnvio`), así que el deserializador llegaba a procesarlos sin
    problema — el bug vivía específicamente en los campos que sí debían ir
    dentro de `datosEnvio` y nunca se habían probado de punta a punta
    contra un envío ACEPTADO.

    La página pública de documentación (la misma que ya se había marcado
    como "desactualizada" en el patch 0017, precisamente por anidar estos
    campos bajo `datosEnvio`) tenía razón en la forma — el error del patch
    0017 no fue descartarla por capricho, sino que en ese momento SÍ exigía
    de más (`codigoAlmacen`), y la colección Postman parecía más simple y
    confiable. La lección: la forma correcta y los campos obligatorios son
    cosas separadas, y sin poder probar contra el servidor real, conviene
    quedarse con la duda en vez de asumir que toda una fuente está mal.

    Jorge (tefacturo.pe) probó en su ambiente el JSON que le pasamos (con
    los datos reales del caso) reestructurado así y confirmó 201 Created:
    - `datosEnvio` (nuevo objeto): `transbordoProgramado`,
      `retornoVehiculoVacio`, `retornoVehiculoContenedoresVacios`
      (strings "False"/"True"), `unidadMedida` ("KILOS", NO "KGM"),
      `pesoBruto` (string), `numeroBultos` (número), `fechaTraslado`,
      `fechaEntrega`, `puntoPartida`/`puntoLlegada`,
      `transporteSubcontratado` (boolean), `trasladoTotalBienes` (boolean).
    - `conductores[]` usa `nombreLegal` (no `nombreCompleto`) y
      `liscenciaConducir` (SÍ, con ese error de tipeo — es el nombre real
      del campo, confirmado en el JSON que probó Jorge) — sin flag
      `principal`.
    - `vehiculos[]` solo trae `placa` — sin flag `principal`.
    - El detalle se llama `detalleGuia` (no `detalleDocumento`), con
      `numeroOrden` (int) y `unidadNombre` nuevos, y `cantidad` como
      NÚMERO (no string).
    - `motivoTraslado` y `modalidadTransporte` NO aparecen en absoluto en
      el JSON confirmado — se quitan de este payload. Tiene sentido:
      "modalidad de transporte" (público/privado) es información que solo
      hace falta en la guía REMITENTE, para que el dueño de la carga
      declare cómo mueve su mercadería — en la guía TRANSPORTISTA ya es
      implícito (por definición, la emite quien presta el servicio de
      transporte). Si más adelante hace falta reportar el motivo de
      traslado igual, hay que preguntarle a tefacturo.pe dónde va — no se
      adivina un nombre de campo nuevo sin evidencia.
    - `numeroPallet` (que sí aparece en la doc pública y en el JSON de
      prueba de Jorge) NO se agrega acá: no hay ningún dato de "número de
      pallet" en el sistema, y mandar un valor inventado en un documento
      fiscal es peor que no mandarlo. Si tefacturo.pe lo exige como
      obligatorio, el próximo envío real lo va a decir con un error
      puntual — recién ahí se agrega un campo real al formulario."""
    missing = []
    if not client["ruc"]:
        missing.append(f"el cliente '{client['name']}' no tiene RUC registrado")
    if not company.get("mtc_registration"):
        missing.append(f"falta el registro MTC de {company.get('name')} (HARRASO_MTC_REGISTRATION/BRMS_MTC_REGISTRATION)")
    if not waybill["origin_ubigeo"] or not waybill["destination_ubigeo"]:
        missing.append("falta el ubigeo de partida y/o llegada (SUNAT lo exige, 6 dígitos)")
    else:
        # 10 sep, patch 0028: revalida contra el catálogo del INEI/SUNAT
        # justo antes de enviar — por si la guía se guardó antes de este
        # patch, o su ubigeo se tocó directamente en la base de datos. Sin
        # esto, un código inválido como "080000" llega a tefacturo.pe y
        # provoca un error interno suyo (NullPointerException) en vez de un
        # mensaje claro.
        origin_error = validar_ubigeo(waybill["origin_ubigeo"], "el ubigeo de partida")
        destination_error = validar_ubigeo(waybill["destination_ubigeo"], "el ubigeo de llegada")
        if origin_error:
            missing.append(origin_error.rstrip("."))
        if destination_error:
            missing.append(destination_error.rstrip("."))
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
        # 14 sep, patch 0032 (ver la nota larga arriba): CONFIRMADO por
        # soporte técnico de tefacturo.pe (201 Created real) — estos campos
        # van anidados acá, no sueltos en la raíz como se mandaban desde el
        # patch 0017. Ese era el bug real detrás del NullPointerException en
        # BaseDispatchAdviceBuilder.buildPoint que persistía desde el
        # reporte original.
        "datosEnvio": {
            # Sin datos propios para "transbordo"/"retorno de vehículo
            # vacío" en el sistema, se dejan fijos en "False" (igual que en
            # el ejemplo real de Braulio, donde los tres salían "NO") —
            # avisar si algún viaje sí necesita alguno en true, para
            # agregarlo como campo del formulario.
            "transbordoProgramado": "False",
            "retornoVehiculoVacio": "False",
            "retornoVehiculoContenedoresVacios": "False",
            # "KILOS", NO "KGM" — confirmado en el JSON real que aceptó
            # tefacturo.pe.
            "unidadMedida": "KILOS",
            "pesoBruto": str(waybill["weight_kg"] or 0),
            "numeroBultos": waybill["packages"] or 0,
            "fechaTraslado": waybill["issue_date"],
            # "delivery_date" es opcional en el formulario de Guías — si se
            # deja vacío, se usa la misma fecha de emisión.
            "fechaEntrega": waybill["delivery_date"] or waybill["issue_date"],
            "puntoPartida": {
                "ubigeo": waybill["origin_ubigeo"],
                "direccion": waybill["origin_address"] or trip["origin"],
            },
            "puntoLlegada": {
                "ubigeo": waybill["destination_ubigeo"],
                "direccion": waybill["destination_address"] or trip["destination"],
            },
            # trips.ownership distingue flota PROPIA de un viaje operado
            # por un TERCERO subcontratado (ver app/routes/viajes.py) —
            # mapeo directo a este campo de tefacturo.pe.
            "transporteSubcontratado": trip["ownership"] == "TERCERO",
            # Harraso/BRMS siempre trasladan la totalidad de la carga del
            # viaje en una sola guía (no hay concepto de "envío parcial" en
            # el sistema) — avisar si eso llega a no ser cierto en algún
            # caso.
            "trasladoTotalBienes": True,
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
                # "nombreLegal" y "liscenciaConducir" (con ese error de
                # tipeo) — nombres reales confirmados en el JSON que probó
                # tefacturo.pe, no "nombreCompleto"/"licenciaConducir" ni
                # flag "principal" (ver nota al inicio del archivo).
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


def is_duplicate_comprobante_error(exc):
    """True si el SunatOseError viene de reenviar un comprobante que
    tefacturo.pe ya tiene registrado. Confirmado en real (8 sep, Factura
    F-0003): al reintentar "Enviar a SUNAT" sobre una factura que ya había
    quedado ACEPTADO (se reintentó solo para volver a descargar el PDF,
    tras el bug corregido en get_pdf_bytes()), tefacturo.pe respondió 400
    con `{"message": "El comprobante <ruc>-<tipo>-<serie>-<numero> ya
    existe", ...}`. Antes esto se trataba como un rechazo nuevo y pisaba
    el estado ACEPTADO ya guardado con ERROR — un comprobante que SUNAT ya
    tiene registrado sigue estando aceptado, así que send_sunat() usa esto
    para NO tratarlo como una falla (ver facturacion.py/guias.py)."""
    return "ya existe" in str(exc).lower()


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
