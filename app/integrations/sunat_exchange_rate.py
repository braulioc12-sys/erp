"""Cliente para obtener el tipo de cambio oficial SUNAT (USD → PEN) por
fecha, usado en el export de liquidación de Gastos (columna "T.Cambio") —
Braulio pidió que sea "el de SUNAT del día de emisión del comprobante".

IMPORTANTE — léeme antes de usar en producción:
SUNAT no ofrece una API pública propia sin trámite (su página de consulta
es para uso manual, sin endpoint documentado). El servicio de terceros
más conocido y usado en Perú para esto es decolecta.com (antes
apis.net.pe), que republica el tipo de cambio oficial que SUNAT publica
cada día. Según su documentación pública (consultada agosto 2026,
https://decolecta.gitbook.io/docs/servicios/integrations):

  GET https://api.decolecta.com/v1/tipo-cambio/sunat?date=YYYY-MM-DD
  Header opcional: Authorization: Bearer <token>

Respuesta de ejemplo (confirmada contra su documentación):
  {"buy_price": "3.540", "sell_price": "3.552",
   "base_currency": "USD", "quote_currency": "PEN", "date": "2025-07-26"}

Se usa `sell_price` ("tipo de cambio venta"), que es el que corresponde a
operaciones de gasto/compra según el criterio contable estándar en Perú
(para ingresos se usaría el de compra). Su propia página de marketing dice
que el uso de token es opcional, pero su documentación técnica sí lo lista
como header requerido — este cliente lo manda solo si está configurado
(DECOLECTA_TOKEN); si el servicio empieza a rechazar peticiones sin token,
regístrate gratis en https://decolecta.com y defínelo.

Lo que **no** se pudo verificar desde este entorno de desarrollo (no tiene
salida a internet hacia este servicio) es una llamada real de punta a
punta — el endpoint y el formato de respuesta están tomados de su
documentación oficial, marcados abajo con "AJUSTAR" donde haga falta
confirmar contra una respuesta real una vez desplegado.

Si la consulta falla por cualquier motivo (sin internet, servicio caído,
fecha futura, formato de respuesta inesperado, etc.) el gasto igual se
guarda — el tipo de cambio queda vacío y se puede completar a mano desde
el formulario de Gastos. El resultado exitoso se cachea en la tabla
`sunat_exchange_rates` (una fila por fecha) para no consultar el servicio
más de una vez por día y para que el dato quede disponible aunque el
servicio se caiga más adelante.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

# AJUSTAR si decolecta.com cambia su dominio/ruta.
DEFAULT_BASE_URL = "https://api.decolecta.com/v1/tipo-cambio/sunat"


class ExchangeRateError(Exception):
    """No se pudo obtener el tipo de cambio (red, formato de respuesta, fecha sin dato, etc.)."""


def fetch_rate(date_str, base_url=None, token=None, timeout=8):
    """Consulta el tipo de cambio SUNAT para una fecha (YYYY-MM-DD) directo
    al servicio externo, sin pasar por el caché local. Devuelve
    {"buy_rate": float, "sell_rate": float} o lanza ExchangeRateError."""
    url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}?date={urllib.parse.quote(date_str)}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise ExchangeRateError(f"El servicio de tipo de cambio respondió {exc.code} para {date_str}.")
    except urllib.error.URLError as exc:
        raise ExchangeRateError(f"No se pudo conectar al servicio de tipo de cambio: {exc.reason}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExchangeRateError(f"Respuesta inesperada del servicio de tipo de cambio: {exc}")

    # AJUSTAR: nombres de campo tomados de la documentación pública de
    # decolecta.com — confirmar que coinciden con una respuesta real.
    try:
        buy = float(data["buy_price"])
        sell = float(data["sell_price"])
    except (KeyError, TypeError, ValueError):
        raise ExchangeRateError(f"El servicio no devolvió buy_price/sell_price válidos para {date_str}.")
    return {"buy_rate": buy, "sell_rate": sell}


def get_rate_for_date(date_str, base_url=None, token=None):
    """Como fetch_rate, pero primero busca en el caché local
    (tabla `sunat_exchange_rates`) y solo llama al servicio externo si no
    la tiene guardada. A diferencia de fetch_rate, nunca lanza una
    excepción: devuelve None si no se pudo obtener de ninguna forma — el
    llamador decide qué hacer (típicamente, dejar el campo vacío para que
    se complete a mano)."""
    from app.db import execute, query_one

    cached = query_one("SELECT buy_rate, sell_rate FROM sunat_exchange_rates WHERE rate_date = ?", (date_str,))
    if cached and cached["sell_rate"] is not None:
        return {"buy_rate": cached["buy_rate"], "sell_rate": cached["sell_rate"]}

    try:
        rate = fetch_rate(date_str, base_url=base_url, token=token)
    except ExchangeRateError:
        return None

    execute(
        """INSERT INTO sunat_exchange_rates (rate_date, buy_rate, sell_rate)
           VALUES (?, ?, ?)
           ON CONFLICT(rate_date) DO UPDATE SET buy_rate = excluded.buy_rate,
             sell_rate = excluded.sell_rate, fetched_at = datetime('now')""",
        (date_str, rate["buy_rate"], rate["sell_rate"]),
    )
    return rate
