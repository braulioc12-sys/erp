"""30 sep, pedido de Braulio ("quiero subir de manera manual, o en un zip
todas las facturas que antes he emitido como BRMS desde el portal sunat"):
lee el XML del comprobante electrónico (factura/boleta) que cualquier
proveedor OSE -- incluido el propio portal de SUNAT -- entrega junto al PDF
al emitir un comprobante. El formato es el estándar UBL 2.1 que exige SUNAT
para todo comprobante electrónico en el Perú, así que este parser sirve
igual sin importar qué proveedor lo generó (tefacturo.pe -- ver
app/integrations/sunat_ose.py -- o cualquier otro), a diferencia de
sunat_ose.py que sí es específico de tefacturo.pe.

Se usa desde app/routes/facturacion.py (ver manual_extract()/manual_zip())
para leer automáticamente cliente, monto, fecha y serie-número de una
factura que Braulio ya emitió directamente en el portal SUNAT (fuera de
este ERP), en vez de tener que tipearlos a mano."""
import xml.etree.ElementTree as ET


class SunatXmlError(Exception):
    """El archivo no es un XML de comprobante electrónico UBL válido, o le
    falta alguno de los datos que necesitamos leer de él."""


def _strip_namespaces(root):
    # Se ignoran los namespaces (xmlns cac:/cbc:) en vez de calzarlos exacto:
    # distintos proveedores/versiones usan URIs de namespace algo distintas
    # para lo mismo, pero los nombres de elemento (ID, IssueDate,
    # PartyIdentification, etc.) son siempre los mismos por estándar SUNAT.
    for elem in root.iter():
        if isinstance(elem.tag, str) and "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
    return root


def _text(el, path):
    if el is None:
        return None
    node = el.find(path)
    if node is None or not node.text:
        return None
    return node.text.strip()


def parse_invoice_xml(raw_bytes):
    """Devuelve un dict con series, correlativo (int), issue_date
    ("YYYY-MM-DD" o None), supplier_ruc/supplier_name (empresa que emitió --
    para confirmar que corresponde a Harraso o BRMS), customer_ruc/
    customer_name (cliente) y total_amount (float). Lanza SunatXmlError si
    el XML no se puede leer o le falta algún dato imprescindible.

    30 sep, tras un 500 real en producción al cargar un .zip de facturas
    (Render solo mostró "Error handling request", sin traceback visible):
    TODO el cuerpo de esta función corre envuelto en un try/except Exception
    -- un XML real puede venir en una codificación rara (ISO-8859-1 en vez
    de UTF-8, con tildes/ñ), truncado, o con una estructura que ET nunca
    esperó, y cualquiera de esos casos puede escaparse como una excepción
    de Python que NO es xml.etree.ElementTree.ParseError (ej.
    UnicodeDecodeError). Antes, eso se colaba sin capturar hasta manual_zip()
    (que si solo atrapaba SunatXmlError, dejaba pasar cualquier otra) y
    tiraba abajo la carga completa del zip con un 500 crudo. Ahora CUALQUIER
    problema al leer un XML se convierte en SunatXmlError, que manual_zip()
    y manual_extract() ya saben manejar sin reventar."""
    try:
        return _parse_invoice_xml(raw_bytes)
    except SunatXmlError:
        raise
    except Exception as exc:
        raise SunatXmlError(f"No se pudo leer el XML ({type(exc).__name__}): {exc}") from exc


def _parse_invoice_xml(raw_bytes):
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
        raise SunatXmlError(f"El archivo no es un XML válido: {exc}") from exc
    _strip_namespaces(root)

    doc_id = _text(root, "ID")
    if not doc_id or "-" not in doc_id:
        raise SunatXmlError(
            "No se encontró el número de serie-correlativo (<cbc:ID>) en el XML, o no tiene el "
            "formato esperado ('F001-123') — ¿es realmente el XML de un comprobante electrónico?"
        )
    series, _, correlativo_raw = doc_id.partition("-")
    try:
        correlativo = int(correlativo_raw)
    except ValueError:
        raise SunatXmlError(f"El correlativo del comprobante ('{correlativo_raw}') no es un número.")

    issue_date = _text(root, "IssueDate")

    supplier_party = root.find("AccountingSupplierParty/Party")
    supplier_ruc = _text(supplier_party, "PartyIdentification/ID")
    supplier_name = _text(supplier_party, "PartyLegalEntity/RegistrationName")

    customer_party = root.find("AccountingCustomerParty/Party")
    customer_ruc = _text(customer_party, "PartyIdentification/ID")
    customer_name = _text(customer_party, "PartyLegalEntity/RegistrationName")
    if not customer_name:
        raise SunatXmlError("No se encontró el nombre del cliente (razón social) en el XML.")

    total_node = root.find("LegalMonetaryTotal")
    total_text = _text(total_node, "PayableAmount") or _text(total_node, "TaxInclusiveAmount")
    if not total_text:
        raise SunatXmlError("No se encontró el monto total (<cbc:PayableAmount>) del comprobante en el XML.")
    try:
        total_amount = round(float(total_text), 2)
    except ValueError:
        raise SunatXmlError(f"El monto total del XML ('{total_text}') no es un número válido.")

    return {
        "series": series,
        "correlativo": correlativo,
        "issue_date": issue_date,
        "supplier_ruc": supplier_ruc,
        "supplier_name": supplier_name,
        "customer_ruc": customer_ruc,
        "customer_name": customer_name,
        "total_amount": total_amount,
    }
