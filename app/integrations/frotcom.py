"""Cliente para la API de Frotcom (GPS / rastreo de flota).

IMPORTANTE — léeme antes de usar en producción:
Frotcom entrega acceso a su "API V2" bajo pedido (contacta a su soporte
para solicitar credenciales: usuario/contraseña o token de API). Su
referencia de endpoints es autodocumentada dentro de tu propia cuenta de
Frotcom, por lo que los nombres exactos de rutas y campos pueden variar
según tu plan/región. Este cliente implementa el patrón más común para
APIs REST de este tipo (login → token → consulta de posiciones), pero
**no ha podido probarse contra la API real** porque esta instalación no
cuenta con credenciales de Frotcom. Antes de usarlo en serio:

1. Pide a Frotcom (o a tu contacto comercial) acceso a "Frotcom API V2".
2. Con las credenciales en mano, entra a su Help Center / referencia
   autodocumentada y confirma:
   - La URL base de tu cuenta (suele ser específica por región/cliente).
   - El endpoint y método exacto de autenticación (login).
   - El endpoint que devuelve la posición/odómetro de cada vehículo y los
     nombres exactos de los campos de la respuesta.
3. Ajusta las constantes y el parseo de la respuesta en este archivo según
   lo que confirmes en el paso 2 (están marcadas con "# AJUSTAR").

Mientras tanto, el resto del ERP funciona perfectamente sin esto —
simplemente no habrá datos de ubicación hasta que esta integración quede
confirmada contra tu cuenta real.
"""
from datetime import datetime, timezone

import urllib.error
import urllib.request
import json


class FrotcomError(Exception):
    """Error al comunicarse con la API de Frotcom (credenciales, red, o
    respuesta inesperada)."""


class FrotcomClient:
    def __init__(self, base_url, username, password, timeout=15):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token = None

    def is_configured(self):
        return bool(self.base_url and self.username and self.password)

    def _request(self, method, path, token=None, payload=None):
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
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
                "Frotcom no está configurado. Define FROTCOM_BASE_URL, FROTCOM_USERNAME y "
                "FROTCOM_PASSWORD en las variables de entorno (ver README)."
            )
        # AJUSTAR: confirma el endpoint y el nombre de los campos de login
        # contra la referencia de tu cuenta ("Authentication in Frotcom API").
        result = self._request(
            "POST",
            "/api/v2/login",
            payload={"username": self.username, "password": self.password},
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
        # AJUSTAR: confirma el endpoint real (por ejemplo podría ser
        # "/api/v2/vehicles/positions" o similar) y el nombre de los campos
        # de la respuesta (PVT = position/velocity/time) contra tu referencia.
        result = self._request("GET", "/api/v2/vehicles/positions", token=token)
        raw_items = result.get("vehicles") or result.get("data") or result if isinstance(result, list) else []

        positions = []
        for item in raw_items:
            positions.append(
                {
                    "external_id": str(item.get("id") or item.get("vehicleId") or item.get("plate") or ""),
                    "latitude": item.get("latitude") or item.get("lat"),
                    "longitude": item.get("longitude") or item.get("lon") or item.get("lng"),
                    "speed_kmh": item.get("speed"),
                    "heading": item.get("heading") or item.get("course"),
                    "odometer_km": item.get("odometer") or item.get("odometerKm"),
                    "recorded_at": item.get("timestamp") or item.get("recordedAt"),
                }
            )
        return positions


def build_client_from_config(app_config):
    return FrotcomClient(
        base_url=app_config.get("FROTCOM_BASE_URL"),
        username=app_config.get("FROTCOM_USERNAME"),
        password=app_config.get("FROTCOM_PASSWORD"),
    )
