"""Cliente para la API de Frotcom (GPS / rastreo de flota).

IMPORTANTE — léeme antes de usar en producción:
Según la documentación pública de Frotcom (Help Center, "Authentication in
Frotcom API" y "How to get API V2 credentials", consultadas en agosto de
2026 — https://frotcominternational.zendesk.com/hc/en-gb/sections/202320689-Frotcom-API-V2):

- Las credenciales de "Frotcom API V2" **no las da el soporte general de
  Frotcom directamente**: hay que pedirlas a tu **Frotcom Certified
  Partner** (el distribuidor/instalador local que te vendió el sistema).
  Diles literalmente que necesitas "credenciales de acceso a la API V2 de
  Frotcom para una integración de terceros" (usuario y contraseña de tipo
  "thirdparty", distintos del usuario con el que entras a la web de
  Frotcom).
- La URL base pública de la API V2 es `https://v2api.frotcom.com`.
- Autenticación: `POST /v2/authorize` con JSON
  `{"provider": "thirdparty", "username": ..., "password": ...}`. La
  respuesta trae un token.
- Ese token se manda como **parámetro de query `api_key`** en cada
  llamada siguiente (NO como header `Authorization: Bearer ...`), por
  ejemplo `GET /v2/vehicles?api_key=<token>`. El token expira si no se usa
  por más de 20 minutos (hay que volver a autenticar).

Lo que **no** se pudo confirmar sin credenciales reales es el endpoint y
los nombres de campo exactos para la posición/odómetro de cada vehículo
(la doc pública de Frotcom es "autodocumentada" dentro de la cuenta real,
vía su "Reference guide"). Este cliente ya implementa el login real
confirmado arriba, y usa `/v2/vehicles` como mejor estimación para el
endpoint de posiciones (aparece como ejemplo en la doc oficial de
autenticación). Antes de usarlo en serio:

1. Pide a tu Frotcom Certified Partner las credenciales de API V2 (ver
   arriba).
2. Con las credenciales en mano, entra a Frotcom Web → busca en su Help
   Center la sección "Frotcom API V2" → artículo "Reference guide", y
   confirma el endpoint exacto de posiciones/odómetro y los nombres de
   los campos de esa respuesta si difieren de `/v2/vehicles`.
3. Ajusta el endpoint y el parseo de la respuesta en `get_vehicle_positions`
   según lo que confirmes (está marcado con "# AJUSTAR").

Mientras tanto, el resto del ERP funciona perfectamente sin esto —
simplemente no habrá datos de ubicación hasta que esta integración quede
confirmada contra tu cuenta real.
"""
from datetime import datetime, timezone

import urllib.error
import urllib.parse
import urllib.request
import json


class FrotcomError(Exception):
    """Error al comunicarse con la API de Frotcom (credenciales, red, o
    respuesta inesperada)."""


class FrotcomClient:
    def __init__(self, base_url, username, password, timeout=15):
        # Si no se define FROTCOM_BASE_URL, se usa la URL pública real de
        # la API V2 de Frotcom confirmada en su documentación oficial.
        self.base_url = (base_url or "https://v2api.frotcom.com").rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token = None
        # Diagnóstico del experimento "kind=A" (ver get_vehicle_positions):
        # None = no se intentó todavía, "" = se intentó y funcionó,
        # cualquier otro string = el mensaje de error de ese intento.
        self.last_asset_fetch_error = None
        self.last_asset_fetch_count = None

    def is_configured(self):
        return bool(self.username and self.password)

    def _request(self, method, path, token=None, payload=None):
        url = f"{self.base_url}{path}"
        if token:
            # Frotcom API V2 espera el token como parámetro de query
            # "api_key" en cada llamada (no como header Authorization).
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api_key={urllib.parse.quote(token)}"
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise FrotcomError(f"Frotcom respondió {exc.code} en {path}: {exc.read().decode('utf-8', 'ignore')}")
        except urllib.error.URLError as exc:
            raise FrotcomError(f"No se pudo conectar a Frotcom ({self.base_url}): {exc.reason}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise FrotcomError(f"Respuesta inesperada de Frotcom en {path}: {exc}")

    def authenticate(self):
        if not self.is_configured():
            raise FrotcomError(
                "Frotcom no está configurado. Define FROTCOM_USERNAME y FROTCOM_PASSWORD "
                "(las credenciales de tipo 'thirdparty' que te da tu Frotcom Certified "
                "Partner) en las variables de entorno (ver README)."
            )
        result = self._request(
            "POST",
            "/v2/authorize",
            payload={"provider": "thirdparty", "username": self.username, "password": self.password},
        )
        token = result.get("token") or result.get("access_token")
        if not token:
            raise FrotcomError("Frotcom no devolvió un token de autenticación (revisa el formato de la respuesta).")
        self._token = token
        return token

    def get_vehicle_positions(self):
        """Devuelve una lista de posiciones normalizadas:
        [{external_id, latitude, longitude, speed_kmh, heading, odometer_km, recorded_at}, ...]
        """
        token = self._token or self.authenticate()
        # AJUSTAR: "/v2/vehicles" es la mejor estimación confirmada (aparece
        # como ejemplo en la doc oficial de autenticación de Frotcom), pero
        # el nombre exacto del endpoint de posición/odómetro y sus campos
        # hay que confirmarlos contra el "Reference guide" de tu cuenta real.
        result = self._request("GET", "/v2/vehicles", token=token)
        # Confirmado contra la cuenta real de Braulio (31 ago): /v2/vehicles
        # devuelve directamente una lista JSON (no un objeto envolvente con
        # "vehicles"/"data"). Se dejan esas dos claves como fallback por si
        # la respuesta cambia de forma en otro endpoint/cuenta.
        # (Antes había un bug de precedencia de operadores en Python: "A or B
        # or C if D else E" evalúa "A or B or C" ANTES del if/else, así que
        # "result.get(...)" se ejecutaba igual aunque result ya fuera una
        # lista, y explotaba con "'list' object has no attribute 'get'".)
        if isinstance(result, list):
            raw_items = result
        elif isinstance(result, dict):
            raw_items = result.get("vehicles") or result.get("data") or []
        else:
            raw_items = []

        # EXPERIMENTAL (31 ago): sin filtro, /v2/vehicles solo devuelve 15
        # unidades en la cuenta real de Braulio, pero él tiene 50+ placas.
        # La doc de Frotcom documenta un parámetro "kind" en este mismo
        # endpoint (V = Vehicle, A = Asset) — es posible que sin ese
        # parámetro la API solo traiga un tipo, y que las carretas /
        # semirremolques estén registradas como "Asset" en vez de
        # "Vehicle". Se agrega, sin tocar lo anterior (que ya funciona),
        # un segundo pedido con kind=A y se suma lo que traiga (sin
        # duplicar por id). Si esto no cambia el total, hay que seguir
        # buscando por otro lado (paginación, otra cuenta/sub-cuenta,
        # etc.) — no se asume que esto resuelve el tema.
        seen_ids = {str(item.get("id") or item.get("vehicleId") or item.get("plate") or "") for item in raw_items}
        try:
            asset_result = self._request("GET", "/v2/vehicles?kind=A", token=token)
        except FrotcomError as exc:
            self.last_asset_fetch_error = str(exc)
            self.last_asset_fetch_count = None
        else:
            if isinstance(asset_result, list):
                asset_items = asset_result
            elif isinstance(asset_result, dict):
                asset_items = asset_result.get("vehicles") or asset_result.get("data") or []
            else:
                asset_items = []
            added = 0
            for item in asset_items:
                item_id = str(item.get("id") or item.get("vehicleId") or item.get("plate") or "")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    raw_items.append(item)
                    added += 1
            self.last_asset_fetch_error = ""
            self.last_asset_fetch_count = added

        positions = []
        for item in raw_items:
            # "label" es un intento de mostrar algo humano-reconocible (placa
            # o nombre) junto al id interno de Frotcom, para que emparejar
            # unidades no dependa de adivinar el id a ciegas — probamos varios
            # nombres de campo típicos; si Frotcom no trae ninguno, queda
            # vacío y no pasa nada (el emparejamiento sigue siendo por id).
            label = (
                item.get("plate") or item.get("licensePlate") or item.get("licencePlate")
                or item.get("registrationNumber") or item.get("name") or item.get("vehicleName")
                or item.get("description") or ""
            )
            positions.append(
                {
                    "external_id": str(item.get("id") or item.get("vehicleId") or item.get("plate") or ""),
                    "label": str(label) if label else "",
                    "latitude": item.get("latitude") or item.get("lat"),
                    "longitude": item.get("longitude") or item.get("lon") or item.get("lng"),
                    "speed_kmh": item.get("speed"),
                    "heading": item.get("heading") or item.get("course"),
                    "odometer_km": item.get("odometer") or item.get("odometerKm"),
                    "recorded_at": item.get("timestamp") or item.get("recordedAt"),
                    # Se guarda crudo (solo cuando aún no se pudo emparejar
                    # nada) para poder mostrar TODOS los campos reales que
                    # trae Frotcom y así terminar de confirmar los nombres
                    # correctos sin adivinar más — ver sync_frotcom().
                    "raw": item,
                }
            )
        return positions


def build_client_from_config(app_config):
    return FrotcomClient(
        base_url=app_config.get("FROTCOM_BASE_URL"),
        username=app_config.get("FROTCOM_USERNAME"),
        password=app_config.get("FROTCOM_PASSWORD"),
    )
