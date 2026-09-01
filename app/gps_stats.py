"""Cálculo de horas manejadas y km avanzados por unidad, a partir del
historial de posiciones de GPS (tabla `vehicle_location_history`, ver
app/schema.sql) — 31 ago, pedido de Braulio: "cuantas horas ha manejado esa
unidad" / "cuantos kilometros ha avanzado", con reportes diarios.

No depende de que Frotcom exponga esos totales directamente (no está
confirmado que lo haga para todas las cuentas) — se calculan solos a partir
de los puntos de posición que ya se van guardando en cada sincronización
(manual, botón "Sincronizar", o automática cada 2 minutos en segundo
plano — ver app/scheduler.py). Cuantos más puntos haya guardados en el día,
más preciso el cálculo; con sync cada 2 minutos el margen de error es bajo.
"""
import math
from datetime import datetime

from app.db import query_all

# Por debajo de esta velocidad (km/h) se considera que la unidad está
# detenida (semáforo, tráfico, o el "temblor" típico de un GPS en reposo) y
# ese tramo no cuenta como tiempo manejando. Un umbral en vez de "> 0" evita
# contar horas de manejo por puro ruido de posición con la unidad parada.
MOVING_SPEED_THRESHOLD_KMH = 3.0

# Si dos puntos consecutivos de una misma unidad están separados por más de
# esto, no se cuentan como un tramo continuo — evita sumar horas/km durante
# un hueco real (GPS caído, unidad apagada, no hubo sincronizaciones por un
# buen rato). Con sync cada ~2 minutos, 20 minutos da margen de sobra sin
# inventar movimiento donde no hay evidencia de que haya pasado.
MAX_GAP_MINUTES = 20


def _haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _parse_dt(text):
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _minutes_between(prev_text, cur_text):
    prev_dt, cur_dt = _parse_dt(prev_text), _parse_dt(cur_text)
    if prev_dt is None or cur_dt is None:
        return None
    return (cur_dt - prev_dt).total_seconds() / 60.0


def daily_stats_all(date_str):
    """Devuelve {vehicle_id: {"hours": float, "km": float}} para todas las
    unidades con algún punto de historial ese día (formato AAAA-MM-DD).
    Las unidades sin ningún punto ese día simplemente no aparecen en el
    resultado (se interpretan como 0 en la vista/reporte)."""
    rows = query_all(
        """SELECT vehicle_id, latitude, longitude, speed_kmh, odometer_km, created_at
           FROM vehicle_location_history
           WHERE date(created_at) = ?
           ORDER BY vehicle_id, created_at""",
        (date_str,),
    )
    by_vehicle = {}
    for row in rows:
        by_vehicle.setdefault(row["vehicle_id"], []).append(row)

    result = {}
    for vehicle_id, points in by_vehicle.items():
        hours = 0.0
        km = 0.0
        for prev, cur in zip(points, points[1:]):
            gap_minutes = _minutes_between(prev["created_at"], cur["created_at"])
            if gap_minutes is None or gap_minutes <= 0 or gap_minutes > MAX_GAP_MINUTES:
                continue
            prev_speed = prev["speed_kmh"] or 0
            cur_speed = cur["speed_kmh"] or 0
            if prev_speed >= MOVING_SPEED_THRESHOLD_KMH or cur_speed >= MOVING_SPEED_THRESHOLD_KMH:
                hours += gap_minutes / 60.0
            # Distancia: se prefiere la diferencia de odómetro de Frotcom si
            # viene en ambos puntos y no retrocede (más preciso que sumar
            # tramos rectos entre coordenadas GPS); si no está disponible,
            # se estima con la distancia entre las dos posiciones.
            if (
                prev["odometer_km"] is not None
                and cur["odometer_km"] is not None
                and cur["odometer_km"] >= prev["odometer_km"]
            ):
                km += cur["odometer_km"] - prev["odometer_km"]
            else:
                km += _haversine_km(prev["latitude"], prev["longitude"], cur["latitude"], cur["longitude"])
        # "points" (31 ago, tras reporte de Braulio de horas/km en 0): cuántos
        # puntos de historial hay ese día para la unidad. Con 0 o 1 punto es
        # matemáticamente imposible calcular una diferencia — no es un error,
        # simplemente todavía no se guardaron (o solo se guardó) suficientes
        # sincronizaciones ese día. Se expone para que la vista pueda avisar
        # "sin datos suficientes" en vez de mostrar un 0 que parece un bug.
        result[vehicle_id] = {"hours": round(hours, 1), "km": round(km, 1), "points": len(points)}
    return result


def daily_stats_from_trips(date_str):
    """Igual que daily_stats_all pero a partir de `vehicle_trips` (31 ago) —
    viajes ya calculados por Frotcom (GET /v2/vehicles/{id}/trips), con
    tiempo de manejo y kilometraje exactos en vez de estimados a partir de
    posiciones sueltas. Un viaje se cuenta en el día en que EMPEZÓ
    (started_at) — un viaje que cruza la medianoche queda completo en el
    día que arrancó, igual de simple que el criterio ya usado en
    daily_stats_all (created_at de cada punto).

    Devuelve {vehicle_id: {"hours": float, "km": float, "trips": int}}.
    Solo trae algo para vehículos con viajes importados ese día — para eso
    hace falta haber corrido "Traer historial" al menos una vez sobre ese
    rango de fechas (ver perform_trips_backfill en integraciones.py)."""
    rows = query_all(
        """SELECT vehicle_id, drive_time_sec, mileage_km
           FROM vehicle_trips
           WHERE date(started_at) = ?""",
        (date_str,),
    )
    result = {}
    for row in rows:
        entry = result.setdefault(row["vehicle_id"], {"hours": 0.0, "km": 0.0, "trips": 0})
        entry["hours"] += (row["drive_time_sec"] or 0) / 3600.0
        entry["km"] += row["mileage_km"] or 0.0
        entry["trips"] += 1
    for entry in result.values():
        entry["hours"] = round(entry["hours"], 1)
        entry["km"] = round(entry["km"], 1)
    return result


def combined_daily_stats(date_str):
    """Combina daily_stats_from_trips (preferido — números exactos de
    Frotcom) con daily_stats_all (estimado a partir de posiciones sueltas)
    (31 ago). Por unidad: si hay viajes importados ese día, se usan esos
    números y se marca source="frotcom"; si no, se usa el estimado y se
    marca source="estimado" (o no aparece si tampoco hay posiciones). Así
    el reporte siempre muestra el mejor dato disponible sin que Braulio
    tenga que saber cuál de las dos fuentes se está usando — aunque igual
    se lo mostramos (ver plantillas) para que sepa qué tan confiable es
    cada número."""
    from_trips = daily_stats_from_trips(date_str)
    from_positions = daily_stats_all(date_str)
    result = {}
    for vehicle_id, stats in from_trips.items():
        result[vehicle_id] = {**stats, "source": "frotcom"}
    for vehicle_id, stats in from_positions.items():
        if vehicle_id not in result:
            result[vehicle_id] = {**stats, "source": "estimado"}
    return result
