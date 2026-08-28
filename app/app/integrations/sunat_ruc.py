"""Cliente para consultar la razón social de un RUC y autocompletar el
proveedor al registrar un gasto (Liquidaciones) — Braulio pidió esto el 28
de agosto, después de confirmar que era factible usando el mismo
proveedor externo que ya usábamos para el tipo de cambio SUNAT
(app/integrations/sunat_exchange_rate.py): decolecta.com.

IMPORTANTE — léeme antes de usar en producción:
Igual que con el tipo de cambio, SUNAT no ofrece una API pública propia
para esto sin trámite. decolecta.com sí tiene un endpoint de consulta de
RUC documentado (consultado agosto 2026,
https://decolecta.gitbook.io/docs/servicios/integrations):

  GET https://api.decolecta.com/v1/sunat/ruc?numero=<RUC>
  Header: Authorization: Bearer <token>

Respuesta de ejemplo (confirmada contra su documentación):
  {"razon_social": "REXTIE S.A.C.", "numero_documento": "20601030013",
   "estado": "ACTIVO", "condicion": "HABIDO",
   "direccion": "AV. JOSE GALVEZ BARRENECHEA NRO 566 INT. 101 URB. CORPAC",
   "distrito": "SAN ISIDRO", "provincia": "LIMA", "departamento": "LIMA"}

A diferencia del tipo de cambio, este endpoint sí exige el token en todas
las pruebas documentadas (no es opcional) — usa el mismo `DECOLECTA_TOKEN`
que ya se configuró para el tipo de cambio, por decisión explícita de
Braulio ("Si vamos a usar el mismo token de decolecta"), en vez de pedir
una cuenta/token aparte. Su plan gratuito da 1000 consultas mensuales, muy
por encima del volumen de gastos de un negocio de transporte como este.

Lo que **no** se pudo verificar desde este entorno de desarrollo (no tiene
salida a internet hacia este servicio) es una llamada real de punta a
punta — el endpoint y el formato de respuesta están tomados de su
documentación oficial, marcados abajo con "AJUSTAR" donde haga falta
confirmar contra una respuesta real una vez desplegado.

Si la consulta falla por cualquier motivo (sin internet, servicio caído,
RUC inválido/no encontrado, token vencido, etc.) el gasto igual se puede
registrar — la razón social simplemente no se autocompleta y se escribe a
mano, nunca bloquea el registro. El resultado exitoso se cachea en la
tabla `sunat_ruc_cache` (una fila por RUC) para no consultar el servicio
más de una vez por proveedor y para que el dato quede disponible aunque
el servicio se caiga más adelante.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

# AJUSTAR si decolecta.com cambia su dominio/ruta.
DEFAULT_BASE_URL = "https://api.decolecta.com/v1/sunat/ruc"


class RucLookupError(Exception):
    """No se pudo consultar el RUC (red, formato de respuesta, RUC no encontrado, etc.)."""


def _clean_ruc(ruc):
    return re.sub(r"\D", "", ruc or "")


def fetch_ruc(ruc, base_url=None, token=None, timeout=8):
    """Consulta la razón social de un RUC directo al servicio externo, sin
    pasar por el caché local. Devuelve un dict con razon_social/estado/
    condicion/direccion, o lanza RucLookupError."""
    clean = _clean_ruc(ruc)
    if len(clean) != 11:
        raise RucLookupError("El RUC debe tener 11 dígitos.")

    url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}?numero={urllib.parse.quote(clean)}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RucLookupError(f"No se encontró el RUC {clean}.")
        raise RucLookupError(f"El servicio de consulta de RUC respondió {exc.code} para {clean}.")
    except urllib.error.URLError as exc:
        raise RucLookupError(f"No se pudo conectar al servicio de consulta de RUC: {exc.reason}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise RucLookupError(f"Respuesta inesperada del servicio de consulta de RUC: {exc}")

    # AJUSTAR: nombres de campo tomados de la documentación pública de
    # decolecta.com — confirmar que coinciden con una respuesta real.
    razon_social = data.get("razon_social")
    if not razon_social:
        raise RucLookupError(f"El servicio no devolvió razón social para {clean}.")
    return {
        "razon_social": razon_social,
        "estado": data.get("estado") or "",
        "condicion": data.get("condicion") or "",
        "direccion": data.get("direccion") or "",
    }


def get_company_for_ruc(ruc, base_url=None, token=None):
    """Como fetch_ruc, pero primero busca en el caché local (tabla
    `sunat_ruc_cache`) y solo llama al servicio externo si no lo tiene
    guardado. A diferencia de fetch_ruc, nunca lanza una excepción:
    devuelve None si no se pudo obtener de ninguna forma — el llamador
    decide qué hacer (típicamente, dejar el campo vacío para completarlo a
    mano)."""
    from app.db import execute, query_one

    clean = _clean_ruc(ruc)
    if len(clean) != 11:
        return None

    cached = query_one("SELECT razon_social, estado, condicion, direccion FROM sunat_ruc_cache WHERE ruc = ?", (clean,))
    if cached and cached["razon_social"]:
        return dict(cached)

    try:
        company = fetch_ruc(clean, base_url=base_url, token=token)
    except RucLookupError:
        return None

    execute(
        """INSERT INTO sunat_ruc_cache (ruc, razon_social, estado, condicion, direccion)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(ruc) DO UPDATE SET razon_social = excluded.razon_social,
             estado = excluded.estado, condicion = excluded.condicion,
             direccion = excluded.direccion, fetched_at = datetime('now')""",
        (clean, company["razon_social"], company["estado"], company["condicion"], company["direccion"]),
    )
    return company
