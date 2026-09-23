"""Registro de actividad ("quién hizo qué") -- pedido de Braulio (22 sep):

    "Hay manera de que yo como administrador, pueda saber quien ha hecho
    cada cosa? Es decir que usuario creo el viaje, subio algun doc, borro
    guia o genero guias, etc?"

Confirmó, al preguntarle, que lo quiere para TODO el sistema de una vez (no
solo los módulos más importantes) y que se muestre de LAS DOS formas: una
pantalla nueva de Actividad con filtros (ver app/routes/actividad.py) y
también "creado por" dentro de cada registro individual.

Este módulo es el único punto de escritura/lectura de la tabla
activity_log (ver app/schema.sql) -- cualquier vista que cree, edite,
elimine, suba un archivo, cambie un estado o genere un documento debe
llamar a log_activity() justo DESPUÉS de que la operación ya se hizo con
éxito (nunca antes: si algo falla a mitad de camino no queremos un registro
de actividad de algo que en realidad no pasó, y nunca dentro de la misma
sentencia que valida datos, para no mezclar el flujo normal con el de
auditoría).

Patrón de uso típico dentro de una vista (ver app/routes/viajes.py para el
caso de referencia, ya instrumentado):

    trip_id = execute("INSERT INTO trips (...) VALUES (...)", (...))
    log_activity(
        "viajes", "CREAR", f"Viaje {code} ({origin} → {destination})",
        entity_type="viaje", entity_id=trip_id,
        entity_url=url_for("viajes.detail", trip_id=trip_id),
    )

IMPORTANTE: log_activity() NUNCA debe poder tumbar la petición que la
llama. Todo el cuerpo va envuelto en try/except -- si la tabla no existe
todavía (ej. un despliegue a mitad de migración), si se llama fuera de un
contexto de petición (algún script), o hay cualquier otro problema
imprevisto, se registra en el logger de Flask y la vista sigue su curso
normal. Perder un registro de auditoría es aceptable; romper la creación de
un viaje (o cualquier otra operación real del negocio) por un error acá NO
lo es.
"""
import logging

from flask import g

from app.db import execute, query_all, query_one

# Etiquetas legibles para las acciones más comunes -- ver app/routes/*.py
# para dónde se usa cada una. Cualquier otro valor de `action` que no esté
# acá se muestra tal cual, con la primera letra en mayúscula, en
# app/routes/actividad.py -- no hace falta declarar aquí una acción nueva
# para que funcione, esto solo mejora cómo se ve en pantalla.
ACTION_LABELS = {
    "CREAR": "Creó",
    "EDITAR": "Editó",
    "ELIMINAR": "Eliminó",
    "RESTAURAR": "Restauró",
    "SUBIR": "Subió",
    "GENERAR": "Generó",
    "ENVIAR": "Envió",
    "ESTADO": "Cambió estado",
    "DESACTIVAR": "Desactivó",
    "REACTIVAR": "Reactivó",
    "APROBAR": "Aprobó",
    "RECHAZAR": "Rechazó",
    "PAGAR": "Marcó como pagado",
    "FACTURAR": "Marcó como facturado",
}


def log_activity(module, action, label=None, entity_type=None, entity_id=None, entity_url=None, details=None):
    """Registra una acción del usuario actualmente logueado (g.user). Ver el
    docstring del módulo para cuándo y cómo llamarla. No devuelve nada ni
    lanza excepciones -- si falla, solo queda en el log de errores de Flask."""
    try:
        try:
            user = g.user
        except RuntimeError:
            user = None  # fuera de un contexto de petición (ej. algún script/seed)
        user_id = user["id"] if user else None
        user_name = user["name"] if user else "Sistema"
        execute(
            """INSERT INTO activity_log (user_id, user_name, module, action, entity_type, entity_id,
               label, entity_url, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, user_name, module, action, entity_type, entity_id, label, entity_url, details),
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "No se pudo registrar actividad (module=%s, action=%s, entity=%s/%s)",
            module, action, entity_type, entity_id,
        )


def get_activity_for(entity_type, entity_id, limit=50):
    """Historial de actividad de UN registro puntual -- para mostrar en su
    propia pantalla de detalle (ej. "Historial" de un viaje o una guía)."""
    if not entity_id:
        return []
    return query_all(
        """SELECT * FROM activity_log WHERE entity_type = ? AND entity_id = ?
           ORDER BY id DESC LIMIT ?""",
        (entity_type, entity_id, limit),
    )


def get_creator_info(entity_type, entity_id):
    """La primera entrada CREAR registrada para este entity_type/entity_id,
    si existe -- lo que alimenta la línea "Creado por Fulano el 22/09/2026"
    en las pantallas de detalle. Devuelve None para registros de ANTES de
    que existiera esta función (no hay forma de saber retroactivamente quién
    los creó) -- en ese caso, la plantilla debe usar como respaldo la
    columna created_by de la propia tabla cuando exista (ver, por ejemplo,
    trips.created_by), o simplemente no mostrar la línea."""
    if not entity_id:
        return None
    return query_one(
        """SELECT * FROM activity_log WHERE entity_type = ? AND entity_id = ? AND action = 'CREAR'
           ORDER BY id ASC LIMIT 1""",
        (entity_type, entity_id),
    )
