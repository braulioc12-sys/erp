"""Cliente para emitir comprobantes electrónicos (facturas y guías de
remisión) ante SUNAT a través de un OSE (Operador de Servicios
Electrónicos), por ejemplo NubeFacT, Efact, BizLinks, etc.

IMPORTANTE — léeme antes de usar en producción
================================================
Conectarse directamente a SUNAT (sin un OSE) exige un certificado digital
propio, firmar XML en formato UBL 2.1 y hablar su webservice SOAP — es una
integración pesada y cara de mantener. La ruta que casi todos los negocios
pequeños/medianos usan (y la que implementa este archivo) es contratar un
OSE: le mandas un JSON simple por HTTPS, el OSE arma el XML, lo firma, lo
envía a SUNAT y te devuelve el PDF/XML/CDR. Es la misma idea que planes de
GPS: el ERP habla el "idioma común" de este tipo de API, pero **los nombres
exactos de campos, catálogos (tipo de comprobante, tipo de documento del
cliente, unidad de medida, motivo de traslado, tipo de transporte) y el
formato de respuesta pueden variar según el OSE que contrates y se
actualizan con el tiempo**. Este cliente sigue el formato público que
NubeFacT documenta (RUTA + TOKEN + JSON), que es el más común entre OSEs
peruanos orientados a REST/JSON, pero **no ha podido probarse contra una
cuenta real** porque esta instalación no tiene credenciales de ningún OSE.

Antes de emitir un solo comprobante real:
1. Contrata un OSE autorizado por SUNAT (NubeFacT, Efact, BizLinks,
   Facturalo Perú, etc.) y crea una cuenta de pruebas (sandbox).
2. Con su manual de integración en mano, confirma en este archivo (buscan
   los comentarios "AJUSTAR"):
   - La URL exacta de la RUTA (suele haber una de pruebas y otra de
     producción).
   - Los códigos de catálogo SUNAT que uses: tipo de comprobante, tipo de
     documento de identidad del cliente, unidad de medida, tipo de IGV,
     moneda, y — para guías de remisión — motivo de traslado y modalidad
     de transporte.
   - El formato exacto de la respuesta (campo de éxito/error, URLs de
     PDF/XML/CDR).
3. Pide a tu contador que revise los primeros comprobantes de prueba antes
   de usarlo con clientes reales — un código de catálogo equivocado puede
   hacer que SUNAT rechace el comprobante o, peor, que lo acepte mal
   clasificado.

Mientras tanto, el resto del ERP funciona perfectamente sin esto: las
facturas y guías se siguen generando y controlando dentro del sistema,
simplemente no quedan enviadas a SUNAT hasta que actives y confirmes esta
integración.
"""
from datetime import datetime

import json
import urllib.error
import urllib.request

IGV_RATE = 0.18


class SunatOseError(Exception):
    """Error al comunicarse con el OSE, o respuesta de rechazo de SUNAT."""


class OseClient:
    def __init__(self, ruta, token, timeout=20):
        self.ruta = (ruta or "").strip()
        self.token = token
        self.timeout = timeout

    def is_configured(self):
        return bool(self.ruta and self.token)

    def send(self, payload):
        """Envía un documento (factura o guía) al OSE y devuelve su
        respuesta ya parseada como dict. Lanza SunatOseError si no se pudo
        conectar o el OSE devolvió un error HTTP."""
        if not self.is_configured():
            raise SunatOseError(
                "La facturación electrónica no está configurada. Define OSE_RUTA y "
                "OSE_TOKEN en las variables de entorno (ver README, sección "
                "'Facturación electrónica (SUNAT)')."
            )
        # AJUSTAR: NubeFacT y OSEs similares usan un solo POST con
        # Authorization: Token <token> — confirma el esquema exacto de tu OSE.
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Token {self.token}",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.ruta, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise SunatOseError(
                f"El OSE respondió {exc.code}: {exc.read().decode('utf-8', 'ignore')}"
            )
        except urllib.error.URLError as exc:
            raise SunatOseError(f"No se pudo conectar al OSE ({self.ruta}): {exc.reason}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise SunatOseError(f"Respuesta inesperada del OSE: {exc}")


def _split_igv(total):
    """A partir de un monto que YA incluye IGV, devuelve (gravada, igv)."""
    gravada = round(total / (1 + IGV_RATE), 2)
    igv = round(total - gravada, 2)
    return gravada, igv


def build_invoice_payload(invoice, items, client, company):
    """Arma el JSON de una FACTURA electrónica en el formato tipo
    NubeFacT (operación / tipo_de_comprobante / items[]). AJUSTAR los
    códigos de catálogo marcados abajo contra el manual real de tu OSE.

    invoice: fila de `invoices`. items: filas de `invoice_items`.
    client: fila de `clients` (requiere `ruc`). company: dict con
    ruc/nombre/dirección propios (ver Config).
    """
    if not client["ruc"]:
        raise SunatOseError(
            f"El cliente '{client['name']}' no tiene RUC registrado; una factura "
            "electrónica requiere el RUC del cliente. Edita el cliente y agrégalo."
        )

    gravada_total, igv_total = _split_igv(float(invoice["amount"]))

    ose_items = []
    for it in items:
        item_gravada, item_igv = _split_igv(float(it["amount"]))
        ose_items.append(
            {
                "unidad_de_medida": "ZZ",  # AJUSTAR: catálogo 03 SUNAT — ZZ = "Unidad de Servicio"
                "codigo": f"SERV-{it['trip_id']}",
                "descripcion": it["description"] or "Servicio de transporte de carga",
                "cantidad": 1,
                "valor_unitario": item_gravada,
                "precio_unitario": round(item_gravada + item_igv, 2),
                "subtotal": item_gravada,
                "tipo_de_igv": 1,  # AJUSTAR: catálogo 07 SUNAT — 1 = Gravado, operación onerosa
                "igv": item_igv,
                "total": round(item_gravada + item_igv, 2),
            }
        )

    return {
        "operacion": "generar_comprobante",
        "tipo_de_comprobante": 1,  # AJUSTAR: catálogo 01 SUNAT — 1 = Factura, 2 = Boleta
        "serie": invoice["series"],
        "numero": invoice["series_number"],
        "sunat_transaction": 1,
        "cliente_tipo_de_documento": 6,  # AJUSTAR: catálogo 06 SUNAT — 6 = RUC
        "cliente_numero_de_documento": client["ruc"],
        "cliente_denominacion": client["name"],
        "cliente_direccion": client["address"] or "",
        "fecha_de_emision": _format_date_ose(invoice["issue_date"]),
        "fecha_de_vencimiento": _format_date_ose(invoice["due_date"]) if invoice["due_date"] else None,
        "moneda": 1,  # AJUSTAR: catálogo 02 SUNAT — 1 = Soles
        "porcentaje_de_igv": IGV_RATE * 100,
        "total_gravada": gravada_total,
        "total_igv": igv_total,
        "total": round(float(invoice["amount"]), 2),
        "observaciones": invoice["notes"] or "",
        "items": ose_items,
    }


def build_waybill_payload(waybill, trip, company):
    """Arma el JSON de una GUÍA DE REMISIÓN ELECTRÓNICA — TRANSPORTISTA
    (la empresa de transporte es quien traslada mercancía de terceros).
    AJUSTAR los códigos de catálogo marcados abajo: los catálogos de
    "motivo de traslado" y "modalidad de transporte" para la guía del
    transportista tienen particularidades frente a la guía del remitente;
    confírmalos con tu OSE y tu contador antes de emitir en producción.
    """
    return {
        "operacion": "generar_guia",
        "tipo_de_comprobante": 7,  # AJUSTAR: catálogo NubeFacT — 7 suele ser "Guía de Remisión Transportista"
        "serie": waybill["series"],
        "numero": waybill["series_number"],
        "cliente_tipo_de_documento": 6,
        "cliente_numero_de_documento": company.get("ruc", ""),
        "cliente_denominacion": company.get("name", ""),
        "fecha_de_emision": _format_date_ose(waybill["issue_date"]),
        "motivo_de_traslado": "04",  # AJUSTAR: catálogo 20 SUNAT — 04 = Traslado de bienes por transporte de servicio prestado a terceros (verificar código vigente)
        "peso_bruto_total": waybill["weight_kg"] or 0,
        "peso_bruto_unidad_de_medida": "KGM",
        "numero_de_bultos": waybill["packages"] or 1,
        "tipo_de_transporte": "02",  # AJUSTAR: catálogo 18 SUNAT — 01 = Público, 02 = Privado (confirmar según tu operación)
        "fecha_de_inicio_de_traslado": _format_date_ose(waybill["issue_date"]),
        "dir_partida_direccion": waybill["origin_address"] or trip["origin"],
        "dir_llegada_direccion": waybill["destination_address"] or trip["destination"],
        "transportista_tipo_de_documento": 6,
        "transportista_numero_de_documento": company.get("ruc", ""),
        "transportista_denominacion": company.get("name", ""),
        "vehiculo_placa_numero": waybill["vehicle_plate"] or "",
        "conductor_tipo_de_documento": 1,  # AJUSTAR: catálogo 06 SUNAT — 1 = DNI
        "conductor_numero_de_documento": waybill["driver_document"] or "",
        "conductor_nombre": waybill["driver_name"] or "",
        "conductor_licencia": waybill["driver_license"] or "",
        "observaciones": waybill["notes"] or "",
        "items": [
            {
                "unidad_de_medida": "KGM",
                "codigo": f"CARGA-{trip['id']}",
                "descripcion": trip["cargo_description"] or "Carga general",
                "cantidad": waybill["weight_kg"] or 1,
            }
        ],
    }


def _format_date_ose(date_str):
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%d-%m-%Y")


def build_client_from_config(app_config):
    return OseClient(ruta=app_config.get("OSE_RUTA"), token=app_config.get("OSE_TOKEN"))


def parse_ose_response(response):
    """Normaliza la respuesta del OSE a un dict simple. AJUSTAR según el
    formato real que devuelva tu OSE (aquí se sigue el patrón NubeFacT:
    campo 'errors' si algo falló; si no, trae enlaces y aceptación)."""
    if response.get("errors"):
        return {
            "accepted": False,
            "message": str(response.get("errors")),
            "pdf_url": None,
            "xml_url": None,
            "cdr_url": None,
        }
    return {
        "accepted": True,
        "message": response.get("sunat_description") or response.get("sunat_note") or "Aceptado",
        "pdf_url": response.get("enlace_del_pdf"),
        "xml_url": response.get("enlace_del_xml"),
        "cdr_url": response.get("enlace_del_cdr"),
    }
