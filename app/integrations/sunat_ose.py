"""Cliente para emitir comprobantes electrónicos (facturas y guías de
remisión) ante SUNAT a través de tefacturo.pe (OSE — Operador de Servicios
Electrónicos autorizado por SUNAT).

CONFIRMADO contra la documentación técnica real (7 sep, segunda ronda —
Braulio compartió https://api.tefacturo.pe/doc/integracion/docs/get-started/,
que reemplaza al primer intento del mismo día basado en un PDF suelto de
2019 sin autenticación documentada ni guía de remisión):

- Base URL: https://tefacturo.pe (configurable, TEFACTURO_BASE_URL). 21 sep,
  CONFIRMADO por soporte técnico de tefacturo.pe (Juan Reyes, Close2u,
  correo "CONFIGURACION - 20610357726 - HARRASO TRANSPORT S.A.C. -
  INTEGRACION APIREST - CPE - GRT", con el RUC real de Harraso en el
  asunto): antes se usaba "https://jarvis.tefacturo.pe" como default (el
  entorno de PRUEBAS con el que Braulio ya había probado, según el mismo
  correo: "Como ya realizó pruebas con nuestro entorno de JARVIS...") — el
  correo da las URI reales de PRODUCCIÓN, con el dominio raíz
  "tefacturo.pe" (sin el subdominio "jarvis."), así que ese pasa a ser el
  default. Sigue siendo configurable vía TEFACTURO_BASE_URL, así que si
  hiciera falta volver a apuntar a "jarvis.tefacturo.pe" (por ejemplo,
  para seguir probando antes de emitir comprobantes reales), basta con
  esa variable de entorno, sin tocar código.
- Autenticación: POST /tokenapi/secure/v2/login/token con
  {"aplicacion": {"codigo": "1"}, "clave", "mail", "ruc"} (ruc como número).
  Devuelve {"c2uToken": "<JWT>", "fechaExpiracion": "<ISO>", ...} — el JWT
  vale ~8 horas; se manda como "Authorization: Bearer <token>" en cada
  llamada siguiente. Es un login POR RUC (cada empresa — Harraso y BRMS —
  necesita su propia cuenta usuario/clave en tefacturo.pe, no un solo
  usuario para ambas).
- Emitir factura: PUT /factura-api/invoice2u/integracion/factura/{ruc} —
  URI reconfirmada tal cual por el correo del 21 sep de arriba.
- Emitir guía de remisión (TRANSPORTISTA): POST
  /guiatransportista-api/invoice2u/integracion/guiaremision/transportista/{ruc}.
  21 sep, CORREGIDO contra el mismo correo de soporte: la URI real es
  ".../guiaremision/transportista/{ruc}" (SIN guión, una sola palabra) —
  la versión anterior de este archivo (desde el patch 0032, 14 sep) usaba
  ".../guia-remision/transportista/{ruc}" (CON guión), un nombre que nunca
  se confirmó contra un 201/200 real de este endpoint específico (el 201
  Created del patch 0032 fue sobre la ESTRUCTURA del payload, sin que
  quedara registrado ahí el path exacto de la URL usada). Con el guión de
  más, cualquier guía transportista real habría estado fallando con 404
  ("ruta no encontrada") en vez de llegar siquiera a validarse — si
  Braulio mandó guías transportista reales y quedaron ACEPTADAS antes de
  este fix, avisar para revisar qué URI se usó de verdad en ese momento
  (por si el servidor de tefacturo.pe acepta ambas variantes).
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
- **`destinatario` ya NO es siempre igual a `remitente`** (15 sep, patch
  0037): Braulio confirmó que remitente y destinatario no siempre
  coinciden. `remitente` sigue siendo el cliente del viaje
  (`trips.client_id`). `destinatario` usa `waybills.recipient_ruc`/
  `recipient_name` cuando se completan al crear la guía; si se dejan en
  blanco, sigue cayendo al cliente del viaje (mismo comportamiento que
  antes de este patch, para no romper guías simples donde de verdad son la
  misma empresa). La ESTRUCTURA de `destinatario` (mismo formato que
  `remitente`: correo/nombreComercial/nombreLegal/
  numeroDocumentoIdentidad/tipoDocumentoIdentidad) sí está confirmada
  contra el servidor real (patch 0032, Jorge) — solo estaba hardcodeado a
  ser igual a `remitente`, eso es lo que cambia acá.
- **`subcontratado` y `pagador` (15 sep, patch 0037) — SIN CONFIRMAR,
  adivinados de buena fe:** Braulio pidió poder especificar también el
  subcontratado (empresa de transporte con RUC propio que no usa las
  unidades de Harraso/BRMS — `waybills.subcontractor_ruc`/
  `subcontractor_name`, se precarga con `trips.third_party_name` cuando el
  viaje es de un tercero) y el pagador de flete (quién paga: remitente,
  destinatario o un tercero con su propio RUC —
  `waybills.payer_type`/`payer_ruc`/`payer_name`). Se revisó de nuevo la
  documentación pública de tefacturo.pe (guía transportista y la página de
  ejemplos) el 15 sep y NINGUNA de las dos muestra un campo para esto:
  la única pista real es `datosEnvio.transporteSubcontratado` (booleano,
  ya mapeado desde `trip["ownership"] == "TERCERO"`, sin detalle de la
  empresa). Aun así, se agregan acá dos claves nuevas a nivel raíz,
  `subcontratado` y `pagador`, con la MISMA forma que `remitente`/
  `destinatario`/`transportista` (es el patrón más consistente del resto
  de este payload) — es una apuesta razonada, no una confirmación. Como
  tefacturo.pe tolera claves desconocidas sin error (confirmado en el
  patch 0032: un deserializador que ignora campos que no reconoce), esto
  no debería romper el envío aunque el nombre esté mal — en el peor caso,
  simplemente no le llega a SUNAT. **Braulio: la próxima guía con
  subcontratado y/o pagador distinto del destinatario, revisa si el PDF de
  SUNAT sale con esos datos correctos o vacíos, y avísame — si hace falta,
  hay que preguntarle a Jorge el nombre real de estos campos.**
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

from app.helpers import get_detraction_tefacturo_code
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
        # 21 sep: default cambiado de "https://jarvis.tefacturo.pe" (entorno
        # de pruebas) a "https://tefacturo.pe" (producción, confirmado por
        # soporte técnico de tefacturo.pe) -- ver la nota grande al inicio
        # del archivo. TEFACTURO_BASE_URL sigue pudiendo pisar esto.
        self.base_url = (base_url or "https://tefacturo.pe").rstrip("/")
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
        # 21 sep: "guiaremision" SIN guión -- corregido contra el correo de
        # soporte técnico de tefacturo.pe, ver la nota grande al inicio del
        # archivo. Antes decía "guia-remision" (con guión), nunca confirmado
        # contra un 201/200 real de este endpoint específico.
        return self._request(
            "POST",
            f"/guiatransportista-api/invoice2u/integracion/guiaremision/transportista/{self.ruc}",
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

    def get_xml_bytes(self, tipo_comprobante, serie, numero):
        """Descarga el XML firmado de un comprobante ya emitido y devuelve
        sus bytes (decodificados de base64). 24 sep, pedido de Braulio
        ("ya funciona genera el pdf, pero para descargar el xml?") --
        mismo patrón que get_pdf_bytes(), pero contra el endpoint
        consultarXml confirmado contra la documentación real de tefacturo.pe
        (https://api.tefacturo.pe/doc/integracion/docs/api/consultar-xml/):
        PUT /consulta-api/invoice2u/integracion/consultarXml/{ruc} -- a
        diferencia de consultarPdf, aquí la documentación SÍ confirma que
        la respuesta siempre viene envuelta en JSON, con el base64 en el
        campo "xmlFirma" (no "pdf"/"archivo"/etc. como se cubre por las
        dudas en consultarPdf) -- así que se reutiliza _request() en vez
        de armar la llamada a mano."""
        body = {
            "emisor": int(self.ruc),
            "numero": int(numero),
            "serie": serie,
            "tipoComprobante": tipo_comprobante,
        }
        result = self._request(
            "PUT", f"/consulta-api/invoice2u/integracion/consultarXml/{self.ruc}", body
        )
        b64 = None
        if isinstance(result, dict):
            b64 = result.get("xmlFirma")
        if not b64 and isinstance(result, str):
            # Por si algún día responden como texto plano, igual que a
            # veces pasa con consultarPdf -- no debería ocurrir según la
            # documentación, pero no cuesta cubrirlo.
            b64 = result
        if not b64:
            raise SunatOseError("tefacturo.pe no devolvió el XML del comprobante.")
        try:
            return base64.b64decode(b64.strip(), validate=False)
        except (ValueError, TypeError) as exc:
            raise SunatOseError(f"El XML devuelto por tefacturo.pe no se pudo decodificar: {exc}")


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
    en cuanto tengamos el nombre real del campo, agregarlo acá.

    ACTUALIZACIÓN (22 sep) — Braulio mandó una factura real (FFA1-1176,
    Harraso, RUC 20610357726) emitida hace tiempo desde el portal de
    tefacturo.pe que SÍ trae la tabla "Concepto de Detracción" en el PDF
    (Código del Bien 027, Monto S/393.29, Porcentaje 4.00%, Cuenta Bco. de
    la Nación 00003351882) — confirma que tefacturo.pe sí soporta esto
    (al menos desde su portal web) y que el formato esperado en el PDF es
    ese. Se volvió a revisar la documentación pública en esta fecha
    (endpoint de factura, catálogos, página de "Ejemplos JSON", el listado
    de endpoints del MCP server que ofrecen) sin encontrar ningún campo de
    detracción documentado -- mismo resultado que la revisión anterior. A
    propósito NO se agrega acá un campo adivinado sin evidencia (a
    diferencia de `subcontratado`/`pagador` en build_waybill_payload, donde
    sí había una pista real cerca -- `datosEnvio.transporteSubcontratado`
    -- que sugería el patrón a seguir): acá no hay ninguna pista, y
    equivocarse en un campo de detracción (que si el cliente lo lee del
    ERP y no del PDF real podría depositar mal o no depositar) es peor que
    dejarlo pendiente con el aviso bien visible que ya tiene detail.html.
    Braulio: la próxima vez que hables con soporte de tefacturo.pe,
    pregúntales puntualmente qué campo del JSON de /factura-api/.../factura
    hay que mandar para reportar detracción (código de bien, %, monto,
    cuenta del Banco de la Nación) -- en cuanto tengas la respuesta, el
    cambio acá es rápido.

    CONFIRMADO (23 sep) -- tefacturo.pe mandó un JSON de ejemplo real (una
    factura de OTRO cliente de ellos, con detracción de "Azúcar") que por
    fin muestra el campo: va un objeto `detraccion` SUELTO en la raíz del
    payload (mismo nivel que `close2u`/`datosDocumento`/`emisor`/etc.), con
    esta forma:

        "detraccion": {
            "codigoBienServicio": "AZUCAR",
            "numeroCuenta": "00000000",
            "porcentaje": "12",
            "redondeo": false
        }

    `numeroCuenta` (la cuenta del Banco de la Nación) y `porcentaje` salen
    directo de la factura (`invoice["detraction_bank_account"]`/
    `invoice["detraction_percentage"]`) -- eso ya se puede mandar sin
    adivinar nada. `redondeo` se asume `false` (no hay ninguna otra pista de
    cuándo debería ir en `true`; si algún día tefacturo.pe rechaza un envío
    por esto, ahí se revisa).

    `codigoBienServicio` SIGUE siendo un misterio parcial: el ejemplo manda
    "AZUCAR" (el nombre del bien, en mayúsculas sin tilde) en vez del código
    numérico de SUNAT que usamos internamente acá (027, etc.) -- es
    evidentemente una palabra clave propia del enum interno de tefacturo.pe,
    no el mismo catálogo. No hay forma de adivinar la palabra clave exacta
    para "transporte de bienes por vía terrestre" (o cualquier otro de nuestros
    conceptos) sin que tefacturo.pe la confirme concepto por concepto -- por
    eso se agregó una columna editable `tefacturo_codigo_bien_servicio` en
    Catálogos → Conceptos de detracción (ver schema.sql/app/helpers.py):
    mientras un concepto no la tenga cargada, `get_detraction_tefacturo_code()`
    devuelve None y esta función sigue SIN mandar el bloque `detraccion`
    completo (mismo criterio conservador de siempre) -- la factura se emite
    igual, solo que sin reportar la detracción a SUNAT todavía."""
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
        # disponible, y si no, se cae al id del viaje. 21 sep, pedido de
        # Braulio (ítems manuales sin viaje, ej. alquileres): un ítem así
        # tiene trip_id NULL -- se identifica por su descripción (o, si ni
        # eso se completó, por el id del propio ítem) en vez de mostrar
        # "viaje #None".
        def _identificar(it):
            if "trip_code" in it.keys() and it["trip_code"]:
                return it["trip_code"]
            if it["trip_id"]:
                return f"viaje #{it['trip_id']}"
            return it["description"] or f"ítem #{it['id']}"

        codigos = ", ".join(_identificar(it) for it in sin_tarifa)
        raise SunatOseError(
            f"El ítem {codigos} tiene tarifa S/ 0.00 en esta factura — SUNAT no permite "
            "un ítem de venta con valor cero. Corrige el monto (o quítalo de esta factura "
            "y factúralo aparte, si de verdad no tiene costo) antes de enviar."
        )

    detalle = []
    for it in items:
        gravada, _igv = _split_igv(float(it["amount"]))
        # 21 sep, pedido de Braulio (ítems manuales sin viaje): sin trip_id,
        # el código de producto se arma con el id del propio ítem de factura
        # en vez de "SERV-None".
        codigo_producto = f"SERV-{it['trip_id']}" if it["trip_id"] else f"ITEM-{it['id']}"
        detalle.append(
            {
                "codigoProducto": codigo_producto,
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

    payload = {
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

    # 23 sep, CONFIRMADO -- ver el bloque "CONFIRMADO (23 sep)" en el
    # docstring de esta función para la forma exacta del bloque
    # "detraccion" y por qué `codigoBienServicio` puede seguir faltando.
    # Solo se agrega cuando la factura tiene detracción aplicada Y el
    # concepto usado ya tiene su código de tefacturo.pe configurado en
    # Catálogos → Conceptos de detracción -- si falta cualquiera de las dos
    # cosas, la factura se emite igual, solo sin reportar la detracción.
    if invoice["detraction_applies"] and invoice["detraction_code"]:
        tefacturo_codigo_bien_servicio = get_detraction_tefacturo_code(invoice["detraction_code"])
        if tefacturo_codigo_bien_servicio:
            payload["detraccion"] = {
                "codigoBienServicio": tefacturo_codigo_bien_servicio,
                "numeroCuenta": invoice["detraction_bank_account"] or "",
                # Sin decimales de sobra ("12" en vez de "12.00", "5.5" en
                # vez de "5.50") -- mismo criterio que ya se usa para
                # mostrar el porcentaje en catalogos/detraccion.html.
                "porcentaje": f"{invoice['detraction_percentage']:g}",
                "redondeo": False,
            }

    return payload


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

    # remitente = siempre el cliente del viaje (sin cambios). destinatario
    # usa recipient_ruc/recipient_name si se completaron al crear la guía;
    # si se dejaron en blanco, cae al mismo cliente (15 sep, patch 0037 —
    # ver la nota de "destinatario" al inicio del archivo).
    remitente = {
        "correo": client["email"] or "",
        "nombreComercial": client["name"],
        "nombreLegal": client["name"],
        "numeroDocumentoIdentidad": client["ruc"],
        "tipoDocumentoIdentidad": "RUC",
    }
    if waybill["recipient_ruc"]:
        destinatario = {
            "correo": "",
            "nombreComercial": waybill["recipient_name"] or "",
            "nombreLegal": waybill["recipient_name"] or "",
            "numeroDocumentoIdentidad": waybill["recipient_ruc"],
            "tipoDocumentoIdentidad": "RUC",
        }
    else:
        destinatario = dict(remitente)

    # subcontratado/pagador (15 sep, patch 0037) — SIN CONFIRMAR contra
    # tefacturo.pe, ver la nota larga al inicio del archivo. subcontratado
    # solo se incluye si de verdad se cargó un RUC (si no, se omite la
    # clave entera en vez de mandar un bloque vacío).
    subcontratado = None
    if waybill["subcontractor_ruc"]:
        subcontratado = {
            "correo": "",
            "nombreComercial": waybill["subcontractor_name"] or "",
            "nombreLegal": waybill["subcontractor_name"] or "",
            "numeroDocumentoIdentidad": waybill["subcontractor_ruc"],
            "tipoDocumentoIdentidad": "RUC",
        }

    payer_type = waybill["payer_type"] or "DESTINATARIO"
    if payer_type == "REMITENTE":
        pagador = dict(remitente)
    elif payer_type == "TERCERO" and waybill["payer_ruc"]:
        pagador = {
            "correo": "",
            "nombreComercial": waybill["payer_name"] or "",
            "nombreLegal": waybill["payer_name"] or "",
            "numeroDocumentoIdentidad": waybill["payer_ruc"],
            "tipoDocumentoIdentidad": "RUC",
        }
    else:
        # DESTINATARIO (default) o TERCERO sin RUC cargado todavía -- cae
        # al destinatario, igual que en la guía real que compartió Braulio
        # (pagador == destinatario en ese ejemplo).
        pagador = dict(destinatario)

    payload = {
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
        "remitente": remitente,
        "destinatario": destinatario,
        # 15 sep, patch 0037: "pagador" agregado como apuesta razonada, sin
        # confirmar -- ver la nota larga al inicio del archivo.
        "pagador": pagador,
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
        # 15 sep, pedido de Braulio: una guía real aceptada por tefacturo.pe
        # trae un bloque "VEHICULO Y CONDUCTOR SECUNDARIO" (la carreta) con
        # su propia placa, además del vehículo principal. "vehiculos" ya es
        # una lista (confirmado en el JSON real de Jorge, aunque su caso de
        # prueba solo tenía un elemento) -- se agrega la carreta como
        # segundo elemento cuando la guía tiene una registrada. A
        # diferencia del resto de este payload, esta parte NO está
        # confirmada contra un envío real con dos vehículos: si tefacturo.pe
        # la rechaza o pide algo más (p.ej. tarjeta de circulación, dato que
        # este sistema no tiene todavía), avisar con el error exacto.
        "vehiculos": (
            [{"placa": waybill["vehicle_plate"] or ""}]
            + ([{"placa": waybill["trailer_plate"]}] if waybill["trailer_plate"] else [])
        ),
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
    # 15 sep, patch 0037: "subcontratado" solo se manda si de verdad hay un
    # RUC cargado -- una guía sin subcontratado (la mayoría, ownership
    # PROPIA) no debe mandar un bloque vacío/inventado.
    if subcontratado:
        payload["subcontratado"] = subcontratado
    return payload


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
