"""Capa de acceso a datos para el ERP de Transporte.

Soporta dos motores, elegidos por la variable de entorno DATABASE_URL:

- Si NO está seteada (por defecto — desarrollo local, y producción en Render
  con disco efímero como hasta ahora): SQLite en DATABASE_PATH, exactamente
  igual que siempre.
- Si está seteada con una URL "postgres://" o "postgresql://" (producción
  real, ej. Amazon RDS — ver README, sección "Base de datos persistente en
  AWS (RDS + S3)"): PostgreSQL vía psycopg2.

El resto de la aplicación (todas las rutas) NO cambia: sigue escribiendo SQL
"estilo SQLite" — placeholders "?", `INSERT OR IGNORE`, `strftime('%Y-%m',
col)`, `datetime('now')` — y leyendo las filas por nombre de columna
(fila["columna"]). Esta capa traduce cada consulta al dialecto de Postgres
cuando corresponde (función `_translate`), y envuelve la conexión cruda de
psycopg2 (`_PGConnCompat`/`_PGCursorCompat`) para que los pocos lugares que
usan `get_db().execute(...)`/`.commit()`/`cur.lastrowid`/`cur.rowcount`
directamente (para agrupar varias escrituras en una transacción) sigan
funcionando sin cambiar esos archivos.

psycopg2 solo se importa (import perezoso, dentro de las funciones) cuando
el modo Postgres está realmente activo, para no exigir esa dependencia en
desarrollo local con SQLite."""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, g

_POSTGRES_PREFIXES = ("postgres://", "postgresql://")

# Tablas cuya clave primaria NO es una columna "id" autoincremental (se
# revisó schema.sql a mano para armar esta lista completa: 26 tablas usan
# "id INTEGER PRIMARY KEY AUTOINCREMENT" y estas 5 son la excepción). Un
# INSERT sobre estas tablas nunca debe recibir "RETURNING id" agregado
# automáticamente en modo Postgres — esa columna no existe ahí.
_TABLES_WITHOUT_ID = {
    "maintenance_record_jobs",  # PK compuesta (maintenance_record_id, job_name)
    "sunat_exchange_rates",  # PK: rate_date
    "sunat_ruc_cache",  # PK: ruc
    "app_settings",  # PK: key
    "vehicle_locations",  # PK: vehicle_id
}

_INSERT_INTO_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
_STRFTIME_NOW_RE = re.compile(r"strftime\(\s*'%Y-%m'\s*,\s*'now'\s*\)", re.IGNORECASE)
_STRFTIME_COL_RE = re.compile(r"strftime\(\s*'%Y-%m'\s*,\s*([A-Za-z0-9_.]+)\s*\)", re.IGNORECASE)
_DATETIME_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_PG_NOW_TIMESTAMP = "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')"
_DATE_NOW_OFFSET_RE = re.compile(
    r"date\(\s*'now'\s*,\s*'([+-]?)(\d+)\s+days?'\s*\)", re.IGNORECASE
)
_DATE_COL_RE = re.compile(r"\bdate\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)", re.IGNORECASE)


def _date_now_offset_sub(match):
    sign, days = match.group(1), match.group(2)
    op = "-" if sign == "-" else "+"
    return f"(current_date {op} interval '{days} days')"


def using_postgres():
    url = current_app.config.get("DATABASE_URL") or ""
    return url.startswith(_POSTGRES_PREFIXES)


def _pg_connection_string(database_url):
    """Amazon RDS exige conexión cifrada por defecto (el `pg_hba.conf` que
    administra AWS solo acepta entradas `hostssl`); psycopg2/libpq no
    siempre negocia SSL solo, y sin `sslmode` puede terminar intentando una
    conexión sin cifrar que RDS rechaza con "no pg_hba.conf entry for host
    ..., no encryption" (visto en despliegue real, 31 ago). Para no
    depender de que cada `DATABASE_URL` lo incluya a mano, se agrega
    `sslmode=require` automáticamente si el propio valor no trae ya un
    `sslmode` explícito (por si alguna vez se conecta contra un Postgres
    que no lo exige, o que use un modo distinto a propósito)."""
    if "sslmode=" in database_url.lower():
        return database_url
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}sslmode=require"


def _translate(sql):
    """Traduce una consulta "estilo SQLite" a PostgreSQL cuando ese es el
    motor activo. No toca nada si se está en modo SQLite (comportamiento
    idéntico al de siempre). Diferencias de sintaxis que aparecen en el
    proyecto: placeholder "?", `INSERT OR IGNORE`, `strftime('%Y-%m', ...)`,
    `datetime('now')` y `date(...)` (usado en las alertas de vencimiento de
    documentos y mantenimientos — bug real detectado en producción el
    31 ago, `date(unknown, unknown) does not exist` en Postgres)."""
    if not using_postgres():
        return sql
    if _INSERT_OR_IGNORE_RE.search(sql):
        sql = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql)
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    sql = _STRFTIME_NOW_RE.sub("to_char(now(), 'YYYY-MM')", sql)
    sql = _STRFTIME_COL_RE.sub(r"substr(\1, 1, 7)", sql)
    sql = _DATETIME_NOW_RE.sub(_PG_NOW_TIMESTAMP, sql)
    # date('now', '+30 days') -> (current_date + interval '30 days'); debe
    # ir antes de _DATE_COL_RE para que no quede un `date(...)` suelto.
    sql = _DATE_NOW_OFFSET_RE.sub(_date_now_offset_sub, sql)
    # date(columna) -> (columna)::date
    sql = _DATE_COL_RE.sub(r"(\1)::date", sql)
    sql = sql.replace("?", "%s")
    return sql


def _insert_target_wants_id(sql):
    """Para una consulta ya traducida: ¿es un INSERT sobre una tabla que
    tiene columna "id"? (ver _TABLES_WITHOUT_ID)."""
    match = _INSERT_INTO_RE.match(sql)
    table = match.group(1).lower() if match else None
    return table is not None and table not in _TABLES_WITHOUT_ID


def _pg_execute(conn, sql, params):
    """Ejecuta una consulta contra una conexión psycopg2 cruda, agregando
    "RETURNING id" a los INSERT que lo necesiten. Devuelve (cursor,
    wants_id) — wants_id indica si ya se debe leer el id de la fila
    insertada con cursor.fetchone()."""
    sql_t = _translate(sql)
    wants_id = _insert_target_wants_id(sql_t)
    if wants_id and "RETURNING" not in sql_t.upper():
        sql_t = sql_t.rstrip().rstrip(";") + " RETURNING id"
    cur = conn.cursor()
    cur.execute(sql_t, params)
    return cur, wants_id


class _PGCursorCompat:
    """Envuelve un cursor de psycopg2 recién ejecutado para que
    `.lastrowid` funcione igual que en sqlite3 (leyendo la fila que
    devuelve el "RETURNING id" agregado por _pg_execute). `.rowcount` y
    `.fetchone()/.fetchall()` se delegan tal cual — psycopg2 ya los
    soporta con la misma semántica que sqlite3 (incluido rowcount = 0
    cuando un `ON CONFLICT DO NOTHING` no insertó nada)."""

    def __init__(self, cursor, wants_id):
        self._cursor = cursor
        self._lastrowid = None
        if wants_id:
            try:
                row = cursor.fetchone()
                self._lastrowid = row["id"] if row else None
            except Exception:
                self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _PGConnCompat:
    """Envuelve una conexión psycopg2 para que el puñado de rutas que usan
    `get_db().execute(...)` / `.commit()` directamente (para agrupar varias
    escrituras en una sola transacción — inventarios.py, mantenimiento.py,
    inspecciones.py, facturacion.py, integraciones.py, seed_data.py) sigan
    funcionando en modo Postgres sin reescribir esos bucles. Las funciones
    de este módulo (query_all/query_one/execute), usadas en el resto del
    proyecto, no pasan por aquí."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur, wants_id = _pg_execute(self._conn, sql, params)
        return _PGCursorCompat(cur, wants_id)

    def commit(self):
        self._conn.commit()

    def cursor(self):
        return self._conn.cursor()

    def close(self):
        self._conn.close()


def get_db():
    """Devuelve la conexión de la petición actual (se crea una vez por request)."""
    if "db" not in g:
        if using_postgres():
            import psycopg2
            import psycopg2.extras

            conn = psycopg2.connect(
                _pg_connection_string(current_app.config["DATABASE_URL"]),
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            g.db = _PGConnCompat(conn)
        else:
            g.db = sqlite3.connect(
                current_app.config["DATABASE_PATH"],
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columnas agregadas a tablas ya existentes en versiones posteriores del
# sistema. `CREATE TABLE IF NOT EXISTS` (usado en schema.sql) no modifica
# una tabla que ya existe, así que en una base de datos que viene de una
# versión anterior (por ejemplo, con disco persistente en producción, o con
# RDS) esa columna nueva no aparecería sola. Esta lista la agrega si falta,
# sin tocar nada más. En hosting con disco efímero (ver README) esto no
# hace falta porque la base se recrea entera en cada despliegue, pero es
# justo lo que entra en juego al usar RDS con datos reales.
COLUMN_MIGRATIONS = [
    ("vehicles", "vehicle_type", "TEXT NOT NULL DEFAULT 'CAMION'"),
    ("vehicles", "soat_expiry", "TEXT"),
    ("vehicles", "technical_review_expiry", "TEXT"),
    ("drivers", "medical_exam_date", "TEXT"),
    ("drivers", "medical_exam_expiry", "TEXT"),
    ("drivers", "backus_driving_exam_date", "TEXT"),
    ("drivers", "backus_driving_exam_expiry", "TEXT"),
    ("drivers", "backus_training_date", "TEXT"),
    ("drivers", "backus_training_expiry", "TEXT"),
    ("drivers", "dds_date", "TEXT"),
    ("drivers", "dds_expiry", "TEXT"),
    ("drivers", "photo_filename", "TEXT"),
    ("trips", "driver_commission", "REAL NOT NULL DEFAULT 0"),
    ("routes", "default_commission_amount", "REAL NOT NULL DEFAULT 0"),
    ("inspections", "checklist_code", "TEXT"),
    ("inspections", "location", "TEXT"),
    ("inspections", "odometer_km", "REAL"),
    ("inspection_items", "section", "TEXT"),
    ("inspection_items", "extra_value", "TEXT"),
    ("expenses", "concept_id", "INTEGER REFERENCES expense_concepts(id)"),
    ("expenses", "document_number", "TEXT"),
    ("expenses", "due_date", "TEXT"),
    ("expenses", "provider_ruc", "TEXT"),
    ("expenses", "provider_name", "TEXT"),
    ("expenses", "currency", "TEXT NOT NULL DEFAULT 'S'"),
    ("expenses", "exchange_rate", "REAL"),
    ("expenses", "expense_advance_id", "INTEGER REFERENCES expense_advances(id)"),
    ("expense_advances", "office", "TEXT"),
    ("expense_advances", "voucher_number", "INTEGER"),
    ("maintenance_record_jobs", "status", "TEXT NOT NULL DEFAULT 'PENDIENTE'"),
    # Sin "REFERENCES" aquí a propósito (aunque schema.sql sí lo declara
    # inline en la columna): la foreign key la agrega el propio mecanismo
    # de _strip_forward_fks/init_db() una vez que la columna ya existe —
    # declararla también aquí crearía una segunda FK duplicada en Postgres
    # (una autogenerada por este ADD COLUMN, otra con nombre fijo del
    # esquema). Ver la nota de "moved_to_tire_id" más abajo (31 ago).
    ("maintenance_record_jobs", "mechanic_id", "INTEGER"),
    ("maintenance_record_jobs", "mechanic_name", "TEXT"),
    ("maintenance_record_jobs", "completed_at", "TEXT"),
    ("maintenance_record_jobs", "mechanic_type", "TEXT"),
    ("mechanics", "mechanic_type", "TEXT NOT NULL DEFAULT 'Otros'"),
    ("maintenance_record_jobs", "mechanic_count", "INTEGER NOT NULL DEFAULT 1"),
    ("vehicles", "owner", "TEXT"),
    ("tires", "disposition", "TEXT"),
    # Sin "REFERENCES tires(id)" aquí — mismo motivo que "mechanic_id" arriba:
    # la FK la agrega el paso dedicado en init_db() (Postgres) una vez que
    # esta columna ya existe, para no duplicarla. En SQLite esto no importa
    # (no valida la referencia al hacer ALTER TABLE ADD COLUMN).
    ("tires", "moved_to_tire_id", "INTEGER"),
    # 31 ago, pedido de Braulio: reporte de cumplimiento de hoja de ruta
    # (horas de manejo/parada esperadas vs. reales por viaje). Se necesita
    # saber CUÁNDO empezó y terminó realmente un viaje (no solo la fecha)
    # para poder comparar contra el GPS de ese tramo — se completan solos
    # al cambiar el estado del viaje a EN_CURSO/ENTREGADO (ver viajes.py).
    ("trips", "actual_start_at", "TEXT"),
    ("trips", "actual_end_at", "TEXT"),
    # 31 ago, tras el primer intento real de "Traer historial" (Braulio):
    # el job terminó "Completado" con 0 viajes importados, sin ninguna
    # pista de por qué — perform_trips_backfill ya capturaba el error real
    # de Frotcom por cada llamada fallida, pero solo lo mandaba al log del
    # servidor (logger.warning), invisible para Braulio. Esta columna deja
    # ver el primer error real de la API directamente en la pantalla de
    # "Historial de viajes", sin tener que pedir logs de Render.
    ("frotcom_trip_import_jobs", "sample_error", "TEXT"),
    # 1 sep, pedido de Braulio: autorización de administrador + recepción
    # parcial en Inventarios → Compras (ver app/routes/inventarios.py).
    # authorized_by_user_id sin "REFERENCES" aquí a propósito — mismo
    # motivo que mechanic_id/moved_to_tire_id más arriba: la FK la agrega
    # el paso dedicado de init_db() (Postgres) una vez que la columna ya
    # existe, tomándola del "REFERENCES users(id)" que sí sigue declarado
    # en schema.sql.
    ("inventory_purchases", "authorized_at", "TEXT"),
    ("inventory_purchases", "authorized_by_name", "TEXT"),
    ("inventory_purchases", "authorized_by_user_id", "INTEGER"),
    ("inventory_purchase_items", "received_quantity", "REAL NOT NULL DEFAULT 0"),
    # 1 sep, pedido de Braulio: elegir si una cotización la emite Harraso o
    # BRMS ("ya que son las 2"). Sin CHECK aquí (a diferencia del schema.sql
    # de una base nueva) para no arriesgar la sintaxis de ADD COLUMN con
    # CHECK en Postgres/SQLite — se valida igual en app/routes/cotizaciones.py.
    ("quotations", "issuer", "TEXT NOT NULL DEFAULT 'HARRASO'"),
    # 2 sep, pedido de Braulio: inventario de llantas por código
    # (tire_inventory, tabla nueva — se crea sola vía CREATE TABLE IF NOT
    # EXISTS). Esta columna es la que enlaza cada instalación en "tires" con
    # su registro de inventario. Sin "REFERENCES" aquí a propósito — mismo
    # motivo que mechanic_id/moved_to_tire_id más arriba: la FK la agrega el
    # paso dedicado de init_db() (Postgres) una vez que la columna ya existe.
    ("tires", "tire_inventory_id", "INTEGER"),
    # 3 sep, pedido de Braulio: viajes con "doble conductor" (comisión al
    # 60%, un segundo conductor) y "solo 1 tramo" (comisión al 50%,
    # combinable con doble conductor: 30% c/u). driver2_id sin "REFERENCES"
    # aquí a propósito — mismo motivo que tire_inventory_id/mechanic_id más
    # arriba: la FK la agrega el paso dedicado de init_db() (Postgres) una
    # vez que la columna ya existe, tomándola del "REFERENCES drivers(id)"
    # que sí sigue declarado en schema.sql.
    ("trips", "driver2_id", "INTEGER"),
    ("trips", "double_driver", "INTEGER NOT NULL DEFAULT 0"),
    ("trips", "single_leg", "INTEGER NOT NULL DEFAULT 0"),
    # 3 sep, pedido de Braulio: carga masiva del último cambio de aceite por
    # placa (Excel con PLACA/KILOMETRAJE/FECHA/TALLER/ACEITE) — ver
    # app/bulk_import.py OIL_CHANGE_COLUMNS y app/routes/flota.py
    # import_oil_changes(). "Observación" reutiliza el "notes" ya existente
    # de vehicles, no es una columna nueva.
    ("vehicles", "last_oil_change_km", "REAL"),
    ("vehicles", "last_oil_change_date", "TEXT"),
    ("vehicles", "last_oil_change_workshop", "TEXT"),
    ("vehicles", "last_oil_change_oil", "TEXT"),
    # 3 sep, pedido de Braulio ("cambios en el módulo de viajes"): empresa
    # operadora (Harraso/BRMS, igual que quotations.issuer), tracto+carreta,
    # tipo de carga, viajes con terceros (unidad ajena, flete acordado,
    # periodo de pago), guía de transportista (número + archivo, distinta de
    # la guía de remisión SUNAT que ya genera el módulo Guías) y el nuevo
    # "pagado" (independiente de "invoiced", que ya existía). Sin CHECK aquí
    # (a diferencia del schema.sql de una base nueva) — mismo motivo que
    # quotations.issuer más arriba: se valida en app/routes/viajes.py.
    # trailer_vehicle_id sin "REFERENCES" a propósito, mismo motivo que
    # driver2_id/tire_inventory_id más arriba.
    ("trips", "issuer", "TEXT NOT NULL DEFAULT 'HARRASO'"),
    ("trips", "trailer_vehicle_id", "INTEGER"),
    ("trips", "cargo_type", "TEXT"),
    ("trips", "ownership", "TEXT NOT NULL DEFAULT 'PROPIA'"),
    ("trips", "third_party_name", "TEXT"),
    ("trips", "third_party_unit", "TEXT"),
    ("trips", "third_party_rate", "REAL"),
    ("trips", "third_party_payment_term", "TEXT"),
    ("trips", "carrier_waybill_number", "TEXT"),
    ("trips", "carrier_waybill_filename", "TEXT"),
    ("trips", "paid", "INTEGER NOT NULL DEFAULT 0"),
    # 15 sep, pedido de Braulio: ¿la guía de remisión del remitente ya
    # figura con los datos de Harraso/BRMS como transportista? Si "SI", no
    # hace falta emitir una guía de transportista nueva (ver
    # viajes/detail.html y set_shipper_waybill_decision() en
    # app/routes/viajes.py). Sin CHECK -- se valida en Python (mismo
    # criterio que "ownership"/"issuer" arriba).
    ("trips", "shipper_waybill_shows_carrier", "TEXT"),
    ("trips", "shipper_waybill_number", "TEXT"),
    ("trips", "shipper_waybill_filename", "TEXT"),
    # 4 sep, pedido de Braulio: conformidad de entrega (foto/PDF) adjuntada
    # mientras el viaje está EN_CURSO — adjuntarla es lo que marca el viaje
    # como ENTREGADO (ver save_delivery_proof() en app/routes/viajes.py).
    ("trips", "delivery_proof_filename", "TEXT"),
    # 4 sep, pedido de Braulio: tabla de consumo de combustible por ruta
    # (galones) y su comparación contra el combustible real al liquidar
    # (ver app/routes/liquidaciones.py save_fuel() y detail.html).
    ("routes", "default_fuel_amount", "REAL NOT NULL DEFAULT 0"),
    ("expense_advances", "fuel_actual", "REAL"),
    ("expense_advances", "fuel_excess", "REAL"),
    ("expense_advances", "fuel_notes", "TEXT"),
    # 4 sep, pedido de Braulio: código de la liquidación según la empresa
    # operadora del viaje — B-0001... (BRMS) / H-0001... (Harraso).
    ("expense_advances", "code", "TEXT"),
    # 7 sep, integración con tefacturo.pe: empresa emisora (Harraso/BRMS) de
    # cada factura/guía — sin CHECK aquí, mismo criterio ya usado para las
    # columnas de arriba sobre tablas ya desplegadas (ver schema.sql para
    # el CHECK completo en instalaciones nuevas).
    ("invoices", "issuer", "TEXT NOT NULL DEFAULT 'HARRASO'"),
    ("waybills", "issuer", "TEXT NOT NULL DEFAULT 'HARRASO'"),
    # 7 sep, integración con tefacturo.pe (segunda ronda, con la
    # documentación técnica real): campos que exige el formato real de la
    # guía transportista (ubigeo, motivo de traslado) y el nombre del PDF
    # real que devuelve tefacturo.pe al emitir cada comprobante.
    ("waybills", "origin_ubigeo", "TEXT"),
    ("waybills", "destination_ubigeo", "TEXT"),
    ("waybills", "transfer_reason", "TEXT NOT NULL DEFAULT 'OTROS'"),
    ("waybills", "sunat_pdf_filename", "TEXT"),
    ("invoices", "sunat_pdf_filename", "TEXT"),
    # Detracción (SPOT) — 9 sep, ver el comentario de estas mismas columnas
    # en schema.sql (CREATE TABLE invoices) para el detalle completo.
    ("invoices", "detraction_applies", "INTEGER NOT NULL DEFAULT 0"),
    ("invoices", "detraction_code", "TEXT"),
    ("invoices", "detraction_percentage", "REAL"),
    ("invoices", "detraction_amount", "REAL"),
    ("invoices", "detraction_bank_account", "TEXT"),
    # Documentos de Flota (foto/PDF) — 9 sep, ver el comentario de estas
    # mismas columnas en schema.sql (CREATE TABLE vehicles).
    ("vehicles", "property_card_filename", "TEXT"),
    ("vehicles", "soat_filename", "TEXT"),
    ("vehicles", "technical_review_filename", "TEXT"),
    ("vehicles", "mtc_filename", "TEXT"),
    ("vehicles", "civil_liability_policy_filename", "TEXT"),
    # "Revisión técnica especial" (9 sep, pedido de Braulio) — opcional,
    # ver VEHICLE_DOCUMENT_TYPES en app/routes/flota.py.
    ("vehicles", "special_technical_review_filename", "TEXT"),
    # Aprobación de RRHH de una liquidación cerrada (9 sep, pedido de
    # Braulio) — exclusiva de Administrador, ver el comentario de estas
    # columnas en schema.sql (CREATE TABLE expense_advances) y
    # rrhh_approve() en app/routes/liquidaciones.py.
    ("expense_advances", "rrhh_approved_at", "TEXT"),
    ("expense_advances", "rrhh_approved_by_name", "TEXT"),
    ("expense_advances", "rrhh_approved_by_user_id", "INTEGER REFERENCES users(id)"),
    # Nota de ajuste del Administrador (10 sep, pedido de Braulio) — ver el
    # comentario de esta columna en schema.sql.
    ("expense_advances", "fuel_adjustment", "TEXT"),
    # Código de contenedor + foto de evidencia de buen estado (10 sep,
    # pedido de Braulio) — ver el comentario de estas columnas en
    # schema.sql (CREATE TABLE trips).
    ("trips", "container_code", "TEXT"),
    ("trips", "container_photo_filename", "TEXT"),
    # Tipo de comprobante (factura/boleta) elegido por gasto + datos del
    # comprobante de combustible (10 sep, pedido de Braulio) — ver el
    # comentario de estas columnas en schema.sql (CREATE TABLE expenses).
    ("expenses", "voucher_type", "TEXT"),
    ("expenses", "fuel_station_name", "TEXT"),
    ("expenses", "fuel_gallons", "REAL"),
    ("expenses", "fuel_unit_price", "REAL"),
    # Ciudad del consumo de combustible (10 sep, 2da ronda, pedido de
    # Braulio) — ver el comentario de esta columna en schema.sql. La tabla
    # nueva fuel_entries no necesita entrada acá: se crea sola vía
    # CREATE TABLE IF NOT EXISTS en schema.sql en cada init_db().
    ("expenses", "fuel_city", "TEXT"),
    # Catálogo de grifos (10 sep, 3ra ronda, pedido de Braulio: "dentro de
    # catalogos hay que poner los grifos... en la pantalla de liquidaciones
    # se eligan los que estan registrados") — ver el comentario de esta
    # columna en schema.sql (CREATE TABLE expenses / CREATE TABLE
    # fuel_entries). La tabla fuel_stations tampoco necesita entrada acá:
    # es nueva, se crea sola en cada init_db().
    ("expenses", "fuel_station_id", "INTEGER REFERENCES fuel_stations(id)"),
    ("fuel_entries", "fuel_station_id", "INTEGER REFERENCES fuel_stations(id)"),
    # 14 sep, patch 0029: "Fecha de entrega" (fechaEntrega) — campo real que
    # exige tefacturo.pe en la guía transportista y que el payload actual
    # nunca mandaba (ver la nota larga en build_waybill_payload() sobre la
    # guía real que Braulio compartió y el NullPointerException que persistía
    # después del patch 0028 de ubigeo). Opcional en el formulario: si se
    # deja en blanco, se usa la misma fecha de emisión.
    ("waybills", "delivery_date", "TEXT"),
    # 15 sep, pedido de Braulio: una guía real aceptada trae un bloque
    # "VEHICULO Y CONDUCTOR SECUNDARIO" con la placa de la carreta -- ver
    # la nota larga en build_waybill_payload() y app/routes/guias.py.
    ("waybills", "trailer_plate", "TEXT"),
    # 15 sep, pedido de Braulio ("hay que especificar remitente,
    # destinatario, subcontratado, pagador"): campos opcionales para los 3
    # bloques que no siempre coinciden con el cliente del viaje -- ver la
    # nota larga en build_waybill_payload() y schema.sql.
    ("waybills", "recipient_ruc", "TEXT"),
    ("waybills", "recipient_name", "TEXT"),
    ("waybills", "subcontractor_ruc", "TEXT"),
    ("waybills", "subcontractor_name", "TEXT"),
    ("waybills", "payer_type", "TEXT NOT NULL DEFAULT 'DESTINATARIO'"),
    ("waybills", "payer_ruc", "TEXT"),
    ("waybills", "payer_name", "TEXT"),
    # 15 sep, pedido de Braulio: "disponible para programar" mientras una
    # unidad está en mantenimiento -- solo Administrador/Mecánico pueden
    # marcarla (ver Mantenimiento -> Por unidad), y solo entonces se puede
    # elegir esa unidad al crear/editar un viaje (ver
    # _active_vehicles()/_active_trailers() en app/routes/viajes.py). Ver
    # la nota larga junto a esta columna en schema.sql.
    ("vehicles", "available_for_scheduling", "INTEGER NOT NULL DEFAULT 0"),
    # 17 sep, pedido de Braulio: "documento asociado" en la guía de
    # remisión (factura/boleta o guía del remitente que sustenta el
    # traslado) -- ver el comentario largo junto a estas columnas en
    # schema.sql (CREATE TABLE waybills) y app/routes/guias.py.
    ("waybills", "related_document_type", "TEXT"),
    ("waybills", "related_document_number", "TEXT"),
    # 18 sep, pedido de Braulio ("historial por unidad... Marca, Modelo,
    # Tipo de llanta... altura de la cocada") -- ver el comentario largo
    # junto a estas columnas en schema.sql (CREATE TABLE tire_inventory).
    # tire_inspections no necesita entrada acá: es una tabla nueva, se crea
    # sola en cada init_db().
    ("tire_inventory", "model", "TEXT"),
    ("tire_inventory", "tire_type", "TEXT"),
    ("tire_inventory", "tread_depth_mm", "REAL"),
    # 18 sep, pedido de Braulio ("la cocada tiene que estar enlazado con
    # las inspecciones... actualizar la medida de la llanta en el
    # inventario") -- ver el comentario largo junto a esta columna en
    # schema.sql (CREATE TABLE inspection_items).
    ("inspection_items", "tread_depth_mm", "REAL"),
    # 18 sep, pedido de Braulio ("cuando se selecciona el tipo de mecanico
    # tambien se debe elegir el nombre de la base de registrados") -- ver
    # el comentario largo junto a estas columnas en schema.sql (CREATE
    # TABLE maintenance_record_job_crew). Sin "REFERENCES" aquí a propósito
    # -- mismo motivo que mechanic_id en maintenance_record_jobs más arriba.
    ("maintenance_record_job_crew", "mechanic_id", "INTEGER"),
    ("maintenance_record_job_crew", "mechanic_name", "TEXT"),
    # 18 sep, 2da ronda (archivo real de Telecrédito de Braulio) -- ver el
    # comentario largo junto a estas columnas en schema.sql (CREATE TABLE
    # staff). Sin CHECK acá a propósito, mismo criterio que el resto de
    # esta lista (SQLite no deja agregar un CHECK constraint vía ALTER
    # TABLE de forma simple) -- se valida en app/routes/pagos_personal.py.
    ("staff", "company", "TEXT"),
    ("staff", "account_type", "TEXT NOT NULL DEFAULT 'AHORROS'"),
    # 18 sep, 5ta ronda (pedido de Braulio: papelera para constancias de
    # pago, con quién la eliminó) -- ver el comentario largo junto a estas
    # columnas en schema.sql (CREATE TABLE payment_vouchers). Sin
    # "REFERENCES" en deleted_by a propósito -- mismo motivo que
    # authorized_by_user_id/driver2_id más arriba: la FK la agrega el paso
    # dedicado de init_db() (Postgres) una vez que la columna ya existe.
    ("payment_vouchers", "deleted_at", "TEXT"),
    ("payment_vouchers", "deleted_by", "INTEGER"),
    # 18 sep, 7ma ronda (pedido de Braulio, sobre la plantilla de
    # honorarios: "agrega la columna Nro de comprobante que sera el numero
    # de recibo emitido que cada mes cambiara") -- el N° de recibo por
    # honorarios que emite cada persona (natural persona con RUC de
    # cuarta categoría) es distinto cada mes, así que se guarda por pago
    # (staff_payments), no en la plantilla (honorarios_template_items),
    # que solo guarda los valores por defecto reusables mes a mes.
    ("staff_payments", "receipt_number", "TEXT"),
    # 19 sep (pedido de Braulio, reorganización del menú de Pagos
    # personal: "una vez que se paguen enlazar la constancia de pago de
    # manera manual para que figuren como pagados. Una constancia puede
    # contener varios pagos.") -- de ahí en más un pago solo pasa a
    # PAGADO cuando se enlaza a mano una constancia ya subida en el
    # archivador (ver payment_vouchers, patch 0055/0056); generar el
    # archivo de Telecrédito dejó de marcar como pagado por sí solo (ver
    # telecredito_generate() en app/routes/pagos_personal.py). Una misma
    # constancia puede quedar enlazada a muchos pagos (uno a muchos desde
    # acá), así que no hace falta una tabla intermedia. Sin "REFERENCES"
    # acá a propósito -- mismo motivo que el resto de esta lista.
    ("staff_payments", "payment_voucher_id", "INTEGER"),
    # 20 sep, pedido de Braulio: "número de pedido" que Backus/Naviera
    # Oriente mandan después de la guía de un viaje de BRMS, para poder
    # facturarles -- ver el comentario largo junto a esta columna en
    # schema.sql (CREATE TABLE trips) y app/routes/guias.py.
    ("trips", "client_order_number", "TEXT"),
    # 22 sep, pedido de Braulio ("a la hora de agregar un item debe salir
    # cantidad, descripcion y monto") -- ver el comentario largo junto a
    # esta columna en schema.sql (CREATE TABLE invoice_items).
    ("invoice_items", "quantity", "REAL NOT NULL DEFAULT 1"),
    # 23 sep, pedido de Braulio: código propio de tefacturo.pe para el campo
    # "codigoBienServicio" del bloque "detraccion" del payload de factura --
    # ver el comentario largo junto a esta columna en schema.sql (CREATE
    # TABLE detraction_concepts) y app/integrations/sunat_ose.py.
    ("detraction_concepts", "tefacturo_codigo_bien_servicio", "TEXT"),
    # 24 sep, pedido de Braulio ("ya funciona genera el pdf, pero para
    # descargar el xml?"): igual que sunat_pdf_filename más arriba, pero
    # para el XML firmado que devuelve tefacturo.pe (consultarXml) -- ver
    # get_xml_bytes() en app/integrations/sunat_ose.py y send_sunat() en
    # facturacion.py/guias.py. sunat_xml_url/sunat_cdr_url YA existían en
    # schema.sql desde el 7 sep (siempre se guardaban en NULL porque nunca
    # se implementó la descarga real) -- lo único que faltaba era esta
    # columna para el nombre del archivo guardado en disco/S3, igual que ya
    # existe para el PDF.
    ("invoices", "sunat_xml_filename", "TEXT"),
    ("waybills", "sunat_xml_filename", "TEXT"),
]


def _apply_column_migrations_sqlite(conn):
    for table, column, ddl in COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _apply_column_migrations_postgres(conn):
    # Postgres soporta "ADD COLUMN IF NOT EXISTS" directamente (9.6+), así
    # que no hace falta consultar information_schema primero.
    cur = conn.cursor()
    for table, column, ddl in COLUMN_MIGRATIONS:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")


# Roles de usuario nuevos (3 sep, pedido de Braulio: "definamos los roles de
# usuario") — DESPACHADOR/ALMACEN/CONTABILIDAD/MECANICO, además de los ya
# existentes ADMIN/OPERADOR. Ver PERMISSIONS en app/auth.py para el permiso
# de cada uno. A diferencia de los demás CHECK de schema.sql (ver el
# comentario en inventory_purchases.status), acá no hay forma de evitar
# tocar el CHECK de una base ya desplegada derivando el estado de otra
# forma: el rol se guarda y se usa tal cual como clave de PERMISSIONS, así
# que las funciones de abajo migran el CHECK constraint existente en vez de
# solo agregar una columna.
#
# 18 sep, 5ta ronda: se agregó RRHH (pedido de Braulio: "en el menu de
# usuarios tambien hay que poner rol RRHH, el cual tenga acceso a RRHH,
# pagos personal, conductores y liquidaciones") — ver PERMISSIONS en
# app/auth.py para el detalle de qué puede ver/editar.
USER_ROLES = ("ADMIN", "OPERADOR", "DESPACHADOR", "ALMACEN", "CONTABILIDAD", "MECANICO", "RRHH")


def _apply_role_check_migration_sqlite(conn):
    """SQLite no soporta ALTER TABLE para modificar un CHECK constraint ya
    creado — hay que recrear la tabla. El truco es 'PRAGMA legacy_alter_table
    = ON' durante el RENAME: sin él, SQLite reescribe automáticamente el
    texto de "REFERENCES users(id)" en las 9 tablas que apuntan a "users"
    (trips, expenses, inventory_purchases, etc.) para que apunten al nombre
    temporal en vez de a "users". PERO ese auto-reescrito solo se suprime de
    verdad si además "PRAGMA foreign_keys" está OFF durante el RENAME —
    confirmado probándolo: con foreign_keys=ON (como lo deja
    "PRAGMA foreign_keys = ON;" al inicio de schema.sql, ya ejecutado antes
    de llegar acá), SQLite reescribe las referencias de todos modos aunque
    legacy_alter_table esté en ON, porque si no lo hiciera terminaría con
    una FK "colgada" mientras la enforcement está activa. Con las dos
    pragmas en el estado correcto, la tabla nueva solo necesita seguir
    llamándose "users" para que esas 9 FK sigan apuntando bien, sin tener
    que tocar ninguna de esas otras tablas.

    18 sep, 5ta ronda: el centinela de "ya migrada" pasó de "DESPACHADOR" a
    "RRHH" para que esto corra una vez más y recree el CHECK con el rol
    nuevo en una base que ya tenía la primera tanda de roles — sigue siendo
    la misma función/mecanismo, solo se corrió el centinela hacia el rol
    agregado más reciente."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if not row or not row[0] or "RRHH" in row[0]:
        return  # ya migrada, o todavía no existe (base nueva: schema.sql ya trae el CHECK actualizado)
    fk_was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE users RENAME TO users_role_check_old")
        conn.execute(
            f"""CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN {USER_ROLES!r}),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            """INSERT INTO users (id, name, email, password_hash, role, active, created_at)
               SELECT id, name, email, password_hash, role, active, created_at FROM users_role_check_old"""
        )
        conn.execute("DROP TABLE users_role_check_old")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_was_on else 'OFF'}")


def _apply_invoice_items_trip_nullable_sqlite(conn):
    """21 sep, pedido de Braulio ("aparte de facturar los viajes, tambien
    se puedan emitir facturas no relacionadas a viajes... de todo tipo"):
    un ítem de factura ya no tiene por qué venir de un viaje (puede ser un
    alquiler u otro concepto libre, ver app/routes/facturacion.py) -- trip_id
    pasa a ser OPCIONAL. SQLite no soporta ALTER TABLE para quitar un NOT
    NULL ya creado, así que hay que recrear la tabla -- mismo mecanismo que
    _apply_role_check_migration_sqlite (rename -> crear la nueva -> copiar
    -> drop), pero sin necesidad de tocar PRAGMA foreign_keys/
    legacy_alter_table: a diferencia de "users", ninguna otra tabla tiene una
    FOREIGN KEY que apunte a invoice_items, así que no hay ninguna referencia
    que SQLite pueda reescribir sola al renombrarla."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='invoice_items'"
    ).fetchone()
    if not row or not row[0] or "trip_id INTEGER NOT NULL" not in row[0]:
        return  # ya migrada, o todavía no existe (schema.sql ya la crea nullable)
    conn.execute("ALTER TABLE invoice_items RENAME TO invoice_items_trip_nullable_old")
    conn.execute(
        """CREATE TABLE invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            trip_id INTEGER REFERENCES trips(id),
            description TEXT,
            amount REAL NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        """INSERT INTO invoice_items (id, invoice_id, trip_id, description, amount)
           SELECT id, invoice_id, trip_id, description, amount FROM invoice_items_trip_nullable_old"""
    )
    conn.execute("DROP TABLE invoice_items_trip_nullable_old")


def _apply_invoice_items_trip_nullable_postgres(conn):
    """Equivalente Postgres de _apply_invoice_items_trip_nullable_sqlite --
    acá sí se puede quitar el NOT NULL directamente, y es seguro repetirlo
    en cada arranque: si la columna ya es nullable, DROP NOT NULL es un
    no-op (a diferencia del CHECK de "role", Postgres no se queja)."""
    cur = conn.cursor()
    cur.execute("ALTER TABLE invoice_items ALTER COLUMN trip_id DROP NOT NULL")


def _apply_role_check_migration_postgres(conn):
    """Equivalente para Postgres/RDS: Postgres sí soporta ALTER TABLE ...
    DROP/ADD CONSTRAINT directamente, pero hay que encontrar el nombre real
    del CHECK ya creado en vez de asumir el nombre autogenerado
    "users_role_check" — si el nombre real fuera otro, un ADD CONSTRAINT con
    ese nombre fijo dejaría el CHECK viejo (restrictivo) conviviendo con el
    nuevo, y los dos se exigen a la vez (el viejo seguiría bloqueando los
    roles nuevos). Se corre en cada arranque; siempre termina en el mismo
    estado (drop + add), así que es seguro repetirlo."""
    cur = conn.cursor()
    cur.execute(
        """SELECT con.conname FROM pg_constraint con
           JOIN pg_class rel ON rel.oid = con.conrelid
           WHERE rel.relname = 'users' AND con.contype = 'c'
             AND pg_get_constraintdef(con.oid) ILIKE '%role%'"""
    )
    for (conname,) in cur.fetchall():
        cur.execute(f'ALTER TABLE users DROP CONSTRAINT "{conname}"')
    cur.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN {USER_ROLES!r})")


# 3 sep, mismo día, ronda siguiente (pedido de Braulio: un usuario puede
# tener más de 1 rol a la vez, ej. Almacén y Mecánico) — user_roles es una
# tabla NUEVA (ver schema.sql), así que no hace falta migrar ningún CHECK
# existente para crearla; lo único que hace falta es completarla sola para
# los usuarios que ya existían ANTES de este cambio, tomando su users.role
# de siempre como su único rol inicial, para que nadie quede sin roles (y
# por lo tanto sin ningún permiso) apenas se despliegue esto. Idempotente:
# el WHERE de abajo solo toca usuarios que todavía no tienen ninguna fila
# en user_roles, así que un usuario al que ya se le asignaron roles a mano
# nunca se pisa en un arranque posterior.
def _backfill_user_roles_sqlite(conn):
    conn.execute(
        """INSERT INTO user_roles (user_id, role)
           SELECT id, role FROM users WHERE id NOT IN (SELECT DISTINCT user_id FROM user_roles)"""
    )


def _backfill_user_roles_postgres(conn):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO user_roles (user_id, role)
           SELECT id, role FROM users WHERE id NOT IN (SELECT DISTINCT user_id FROM user_roles)
           ON CONFLICT (user_id, role) DO NOTHING"""
    )


# Concepto "COMBUSTIBLE" (10 sep, pedido de Braulio: cuadros de grifo/
# galones/precio al registrar un gasto de combustible) — no estaba en el
# catálogo original de conceptos (DEFAULT_EXPENSE_CONCEPTS en
# app/seed_data.py solo corre en una base nueva y vacía), así que hace
# falta agregarlo también a las bases ya desplegadas. Se corre en cada
# arranque, igual que _backfill_user_roles_*: idempotente (solo inserta si
# todavía no existe un concepto con ese nombre, sin importar mayúsculas —
# por si Braulio ya lo había agregado a mano desde Catálogos), así que es
# seguro repetirlo. Mismos valores que Peaje/Lavado/Consumo/etc.: cuenta
# 42121, factura, documento "01" (ver DEFAULT_EXPENSE_CONCEPTS).
_COMBUSTIBLE_CONCEPT = ("COMBUSTIBLE", "42121", "factura", "01")


def _ensure_combustible_concept_sqlite(conn):
    row = conn.execute(
        "SELECT id FROM expense_concepts WHERE UPPER(name) = ?", (_COMBUSTIBLE_CONCEPT[0],)
    ).fetchone()
    if row:
        return
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM expense_concepts").fetchone()[0]
    conn.execute(
        """INSERT INTO expense_concepts (name, account_code, voucher_type_label, document_type_code, sort_order)
           VALUES (?, ?, ?, ?, ?)""",
        (*_COMBUSTIBLE_CONCEPT, max_order + 1),
    )


def _ensure_combustible_concept_postgres(conn):
    cur = conn.cursor()
    cur.execute("SELECT id FROM expense_concepts WHERE UPPER(name) = %s", (_COMBUSTIBLE_CONCEPT[0],))
    if cur.fetchone():
        return
    cur.execute("SELECT COALESCE(MAX(sort_order), -1) FROM expense_concepts")
    max_order = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO expense_concepts (name, account_code, voucher_type_label, document_type_code, sort_order)
           VALUES (%s, %s, %s, %s, %s)""",
        (*_COMBUSTIBLE_CONCEPT, max_order + 1),
    )


# 14 sep, pedido de Braulio: "cuando se selecciona factura las cuentas
# estan correctas, pero cuando es boleta sigue jalando cuentas de
# factura". Estos 8 conceptos se sembraron (y siguen en la base ya
# desplegada de Braulio) con la cuenta de FACTURA (42121) como su propia
# cuenta — como resolve_expense_account() (app/accounting.py) solo
# reemplaza la cuenta cuando el radio dice "factura" y deja la del
# concepto tal cual cuando dice "boleta", elegir boleta en estos conceptos
# seguía mostrando 42121. Mismo patrón que
# _ensure_combustible_concept_sqlite/_postgres: corre en cada arranque,
# solo actualiza los conceptos que coincidan por nombre Y que TODAVÍA
# tengan la cuenta vieja (42121) — así no pisa nada si Braulio ya lo
# corrigió a mano desde Catálogos antes de este patch.
_BOLETA_ACCOUNT_FIXES = {
    "PEAJE": ("6313", "03", "boleta"),
    "AFLOJATODO": ("63433", "03", "boleta"),
    "ARREGLO CARGA": ("63433", "03", "boleta"),
    "LAVADO": ("63433", "03", "boleta"),
    "CONSUMO": ("6314", "03", "boleta"),
    "SILICONA": ("63433", "03", "boleta"),
    "ENGRASE": ("63433", "03", "boleta"),
    "COCHERA": ("63433", "03", "boleta"),
}
_BOLETA_ACCOUNT_FIXES_OLD_CODE = "42121"


def _fix_boleta_account_codes_sqlite(conn):
    for name, (account_code, doc_code, label) in _BOLETA_ACCOUNT_FIXES.items():
        conn.execute(
            """UPDATE expense_concepts SET account_code = ?, document_type_code = ?, voucher_type_label = ?
               WHERE UPPER(name) = ? AND account_code = ?""",
            (account_code, doc_code, label, name, _BOLETA_ACCOUNT_FIXES_OLD_CODE),
        )


# 23 sep, 2da ronda: soporte de tefacturo.pe (Jorge) confirmó por WhatsApp su
# "Catálogo Tipo de Detracción" completo -- la tabla oficial que mapea cada
# código SUNAT a la palabra clave que espera su campo `codigoBienServicio`
# (ver la nota larga en schema.sql y get_detraction_tefacturo_code() en
# app/helpers.py). Con esto ya no hace falta que Braulio cargue a mano el
# código de tefacturo.pe de cada concepto que YA estaba en su catálogo desde
# antes -- se autocompleta acá, una sola vez por concepto (mismo patrón que
# _fix_boleta_account_codes_* arriba: corre en cada arranque pero solo toca
# filas que TODAVÍA no tengan nada cargado en esa columna, así que nunca pisa
# un valor que Braulio haya puesto o corregido a mano desde Catálogos).
#
# Los códigos que quedan en `_DETRACTION_GOODS_SEED` pero NO aparecen acá
# (hoy, solo "006 Algodón") es porque tampoco aparecen en el catálogo de
# tefacturo.pe -- coincide con la nota de arriba de que ese código ya no
# está vigente en SUNAT.
#
# OJO -- este mismo catálogo de tefacturo.pe trae, para 5 de estos códigos,
# un porcentaje DISTINTO al que quedó cargado acá (008 Madera 12%→4%, 013
# Animales vivos 10%→4%, 014 Carnes y despojos comestibles 10%→4%, 015
# Abonos/cueros/pieles 10%→4%, 017 Harina/pellets de pescado 10%→4%) --
# coincide con lo que ya advertían las fuentes públicas (ver la nota junto a
# _DETRACTION_GOODS_SEED en app/helpers.py). A propósito NO se corrige el
# porcentaje acá (un depósito de detracción con el % equivocado no se puede
# arreglar después con SUNAT) -- si Braulio confirma con su contador que el
# de tefacturo.pe es el correcto, se corrige a mano desde Catálogos →
# Conceptos de detracción. El único código que de verdad usa Harraso hoy
# (027, transporte de bienes) ya tenía el porcentaje correcto (4%) desde
# antes, coincide con tefacturo.pe -- no hay ningún cambio ahí.
_TEFACTURO_CODIGO_BIEN_SERVICIO = {
    "001": "AZUCAR",
    "003": "ALCOHOL_ETILICO",
    "004": "RECURSOS_HIDROBIOLOGICO",
    "005": "MAIZ_AMARILLO_DURO",
    "007": "CANA_DE_AZUCAR",
    "008": "MADERA",
    "009": "ARENA_Y_PIEDRA",
    "010": "RESIDUOS",
    "013": "ANIMALES_VIVOS",
    "014": "CARNES_Y_DESPOJOS_COMESTIBLES",
    "015": "ABONOS_CUEROS_Y_PIELES",
    "016": "ACEITE_DE_PESCADO",
    "017": "HARIRA_POLVO_MOLUSCOS",
    "027": "TRANSPORTE_DE_BIENES",
}


def _backfill_tefacturo_codigo_bien_servicio_sqlite(conn):
    for code, tefacturo_code in _TEFACTURO_CODIGO_BIEN_SERVICIO.items():
        conn.execute(
            """UPDATE detraction_concepts SET tefacturo_codigo_bien_servicio = ?
               WHERE code = ? AND (tefacturo_codigo_bien_servicio IS NULL OR tefacturo_codigo_bien_servicio = '')""",
            (tefacturo_code, code),
        )


def _backfill_tefacturo_codigo_bien_servicio_postgres(conn):
    cur = conn.cursor()
    for code, tefacturo_code in _TEFACTURO_CODIGO_BIEN_SERVICIO.items():
        cur.execute(
            """UPDATE detraction_concepts SET tefacturo_codigo_bien_servicio = %s
               WHERE code = %s AND (tefacturo_codigo_bien_servicio IS NULL OR tefacturo_codigo_bien_servicio = '')""",
            (tefacturo_code, code),
        )


def _fix_boleta_account_codes_postgres(conn):
    cur = conn.cursor()
    for name, (account_code, doc_code, label) in _BOLETA_ACCOUNT_FIXES.items():
        cur.execute(
            """UPDATE expense_concepts SET account_code = %s, document_type_code = %s, voucher_type_label = %s
               WHERE UPPER(name) = %s AND account_code = %s""",
            (account_code, doc_code, label, name, _BOLETA_ACCOUNT_FIXES_OLD_CODE),
        )


# 15 sep, pedido de Braulio: "no tenemos marcadas las llantas aun y recien
# vamos a marcar las nuevas, para empezar vamos a codificar por primera y
# unica vez todas las llantas de cada tracto y carreta por default
# 'PLACA-1' ... de 1 al 12 en carreta, y de 1 al 10 en tracto. Esto para
# poder inicialmente ver las rotaciones y cuando se vayan descartando
# llantas." Hoy ningún TRACTO/CARRETA tiene llantas cargadas porque
# neumaticos.new_tire() EXIGE elegir una llanta ya registrada en
# tire_inventory (con su código) -- sin códigos, no había forma de cargar
# ninguna. Este seed crea, para cada POSICIÓN sin llanta ACTIVO de cada
# unidad TRACTO/CARRETA, una llanta de inventario con código
# "<placa>-<n>" (n = 1..10 para tracto, 1..12 para carreta, numeradas en
# el mismo orden que devuelve app.tire_positions.get_positions() -- de
# adelante hacia atrás, izquierda antes que derecha) y la asigna de
# inmediato a esa posición, con fecha de instalación de hoy y el
# kilometraje actual de la unidad. Así Braulio puede usar rotación y
# descarte desde ya, y reemplazar cada código default por el código real
# de fábrica a medida que se vayan marcando las llantas físicas de verdad
# (con "Reemplazar" desde el detalle de cada llanta).
#
# Corre en cada arranque, mismo patrón que
# _ensure_combustible_concept_sqlite/_fix_boleta_account_codes_sqlite --
# pero solo llena POSICIONES que todavía no tienen ninguna llanta ACTIVO,
# así que en la práctica es "primera y única vez": una vez sembrada una
# posición, el próximo arranque ya no la toca (tampoco después de que
# Braulio reemplace ese código default por uno real, porque en ese momento
# la posición ya tiene una llanta ACTIVO distinta). Aviso importante: si
# más adelante se da de alta un TRACTO/CARRETA nuevo y queda con
# posiciones sin llanta, este mismo seed le va a poner códigos default
# "<placa>-<n>" en el siguiente arranque del servidor -- si se prefiere
# que las unidades nuevas NO reciban códigos default (para forzar cargar
# siempre llantas reales desde el principio), avisar para agregar esa
# excepción.
_DEFAULT_TIRE_CODE_NOTE = (
    "Código provisional asignado en bloque (15 sep) -- reemplazar por el "
    "código real de la llanta física cuando se la marque, usando "
    "'Reemplazar' desde el detalle de esta llanta."
)


def _default_tire_codes_for_vehicle(get_positions_fn, vehicle_type):
    """Lista [(position_code, n)] en el mismo orden que el diagrama, n
    empezando en 1 -- ese "n" es el número que va en el código default
    "<placa>-n"."""
    return [(p["code"], i) for i, p in enumerate(get_positions_fn(vehicle_type), start=1)]


def _seed_default_tire_codes_sqlite(conn):
    from app.tire_positions import DEFAULT_EXPECTED_LIFE_KM, get_positions

    today = datetime.now().strftime("%Y-%m-%d")
    vehicles = conn.execute(
        "SELECT id, plate, vehicle_type, current_km FROM vehicles WHERE vehicle_type IN ('TRACTO', 'CARRETA')"
    ).fetchall()
    for vehicle_id, plate, vehicle_type, current_km in vehicles:
        for position_code, n in _default_tire_codes_for_vehicle(get_positions, vehicle_type):
            has_active = conn.execute(
                "SELECT 1 FROM tires WHERE vehicle_id = ? AND position_code = ? AND status = 'ACTIVO'",
                (vehicle_id, position_code),
            ).fetchone()
            if has_active:
                continue
            code = f"{plate}-{n}"
            inv_row = conn.execute(
                "SELECT id, status FROM tire_inventory WHERE code = ?", (code,)
            ).fetchone()
            if inv_row is None:
                cur = conn.execute(
                    """INSERT INTO tire_inventory (code, expected_life_km, status, notes)
                       VALUES (?, ?, 'ASIGNADA', ?)""",
                    (code, DEFAULT_EXPECTED_LIFE_KM, _DEFAULT_TIRE_CODE_NOTE),
                )
                inv_id = cur.lastrowid
            elif inv_row[1] == "DISPONIBLE":
                conn.execute("UPDATE tire_inventory SET status = 'ASIGNADA' WHERE id = ?", (inv_row[0],))
                inv_id = inv_row[0]
            else:
                # Ya existe un código igual y está ASIGNADA/RETIRADA por otro
                # motivo (caso raro) -- no se sabe a qué corresponde, se deja
                # esta posición sin sembrar en vez de arriesgar pisar algo.
                continue
            conn.execute(
                """INSERT INTO tires (vehicle_id, position_code, install_date, km_at_install,
                   expected_life_km, tire_inventory_id, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (vehicle_id, position_code, today, current_km or 0, DEFAULT_EXPECTED_LIFE_KM, inv_id, _DEFAULT_TIRE_CODE_NOTE),
            )


def _seed_default_tire_codes_postgres(conn):
    from app.tire_positions import DEFAULT_EXPECTED_LIFE_KM, get_positions

    today = datetime.now().strftime("%Y-%m-%d")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, plate, vehicle_type, current_km FROM vehicles WHERE vehicle_type IN ('TRACTO', 'CARRETA')"
    )
    vehicles = cur.fetchall()
    for vehicle_id, plate, vehicle_type, current_km in vehicles:
        for position_code, n in _default_tire_codes_for_vehicle(get_positions, vehicle_type):
            cur.execute(
                "SELECT 1 FROM tires WHERE vehicle_id = %s AND position_code = %s AND status = 'ACTIVO'",
                (vehicle_id, position_code),
            )
            if cur.fetchone():
                continue
            code = f"{plate}-{n}"
            cur.execute("SELECT id, status FROM tire_inventory WHERE code = %s", (code,))
            inv_row = cur.fetchone()
            if inv_row is None:
                cur.execute(
                    """INSERT INTO tire_inventory (code, expected_life_km, status, notes)
                       VALUES (%s, %s, 'ASIGNADA', %s) RETURNING id""",
                    (code, DEFAULT_EXPECTED_LIFE_KM, _DEFAULT_TIRE_CODE_NOTE),
                )
                inv_id = cur.fetchone()[0]
            elif inv_row[1] == "DISPONIBLE":
                cur.execute("UPDATE tire_inventory SET status = 'ASIGNADA' WHERE id = %s", (inv_row[0],))
                inv_id = inv_row[0]
            else:
                continue
            cur.execute(
                """INSERT INTO tires (vehicle_id, position_code, install_date, km_at_install,
                   expected_life_km, tire_inventory_id, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (vehicle_id, position_code, today, current_km or 0, DEFAULT_EXPECTED_LIFE_KM, inv_id, _DEFAULT_TIRE_CODE_NOTE),
            )


# Tarifario (15 sep, pedido de Braulio: "un menu que se llame tarifario...
# uses este excel para los datos" -- carga, UNA SOLA VEZ, las tarifas del
# Excel "Tarifario clientes corporativos.xlsx" que compartió, para Backus y
# Lindley. Backus: 2 tarifas por ruta ("PT + ENVASES"/"PT + VACIO", pedido
# explícito de Braulio) -- son PT+ENV y PT+VACIO tal cual venían en el
# Excel (ya sumados ahí, no se recalculan acá). Lindley: 1 tarifa
# ("Tarifa"). Un duplicado EXACTO en la hoja de Lindley (Planta Pucusana ->
# Cusco, S/ 11000, aparecía 2 veces con el mismo monto) se omitió acá por
# ser un duplicado sin ambigüedad. Un duplicado con montos DISTINTOS en la
# hoja de Backus (Pucallpa -> Chanchamayo: S/ 9899.81/9742.81 vs S/
# 13943/13080) SÍ se cargan ambas filas tal cual -- no hay forma de saber
# cuál es la correcta sin preguntarle a Braulio (ver la nota de entrega del
# patch), así que se deja que él la revise/borre en la pantalla de
# Tarifario en vez de que este código decida por su cuenta.
_TARIFARIO_SEED_DATA = {
    "Backus": [
        ("PUCALLPA", "AGUAYTIA", [("PT + ENVASES", 3068.63), ("PT + VACIO", 3068.63)]),
        ("PUCALLPA", "TINGOMARIA", [("PT + ENVASES", 4462.56), ("PT + VACIO", 4267.56)]),
        ("PUCALLPA", "TOCACHE", [("PT + ENVASES", 6067.6), ("PT + VACIO", 5895.6)]),
        ("PUCALLPA", "HUANUCO OLARTE", [("PT + ENVASES", 5776.87), ("PT + VACIO", 5616.87)]),
        ("PUCALLPA", "HUANUCO CD", [("PT + ENVASES", 5788.22), ("PT + VACIO", 5628.22)]),
        ("PUCALLPA", "JUANJUI", [("PT + ENVASES", 8289.35), ("PT + VACIO", 7619.35)]),
        ("PUCALLPA", "CHANCHAMAYO", [("PT + ENVASES", 9899.81), ("PT + VACIO", 9742.81)]),
        ("PUCALLPA", "TARAPOTO", [("PT + ENVASES", 9653.67), ("PT + VACIO", 8765.67)]),
        ("PUCALLPA", "YURIMAGUAS", [("PT + ENVASES", 11471.59), ("PT + VACIO", 10794.59)]),
        ("PUCALLPA", "SATIPO", [("PT + ENVASES", 11123.24), ("PT + VACIO", 10769.24)]),
        ("PUCALLPA", "MOYOBAMBA", [("PT + ENVASES", 11582.34), ("PT + VACIO", 11434.34)]),
        ("PUCALLPA", "BAGUA", [("PT + ENVASES", 14199), ("PT + VACIO", 13325)]),
        ("PUCALLPA", "SAN IGNACIO", [("PT + ENVASES", 16417), ("PT + VACIO", 15408)]),
        ("PUCALLPA", "PUCARA", [("PT + ENVASES", 16609), ("PT + VACIO", 15629)]),
        ("PUCALLPA", "JAEN", [("PT + ENVASES", 15284), ("PT + VACIO", 14348)]),
        # Duplicado con montos distintos frente a la fila de arriba -- ver
        # la nota grande al inicio de este bloque.
        ("PUCALLPA", "CHANCHAMAYO", [("PT + ENVASES", 13943), ("PT + VACIO", 13080)]),
        ("PUCALLPA", "PUERTO (LOCAL)", [("PT + ENVASES", 590.4), ("PT + VACIO", 544)]),
    ],
    "Lindley": [
        ("Planta Pucusana", "Tarma", [("Tarifa", 4800)]),
        ("Planta Pucusana", "Cerro Pasco", [("Tarifa", 5400)]),
        ("Planta Pucusana", "OL Huánuco", [("Tarifa", 7400)]),
        ("Planta Pucusana", "La Merced", [("Tarifa", 6000)]),
        ("Planta Pucusana", "Pucallpa", [("Tarifa", 11000)]),
        ("Planta Pucusana", "Tingo María", [("Tarifa", 7800)]),
        ("Planta Pucusana", "Tocache", [("Tarifa", 10500)]),
        ("Planta Pucusana", "Huánuco", [("Tarifa", 7400)]),
        ("Planta Pucusana", "Satipo", [("Tarifa", 7575)]),
        ("Planta Pucusana", "OL Pucallpa", [("Tarifa", 11000)]),
        ("Planta Pucusana", "Huancavelica", [("Tarifa", 7100)]),
        ("Planta Pucusana", "Planta Trujillo", [("Tarifa", 6600)]),
        ("Planta Pucusana", "Cusco", [("Tarifa", 11000)]),
        ("Planta Pucusana", "Planta Cusco", [("Tarifa", 11000)]),
        ("Planta Pucusana", "OL Cusco", [("Tarifa", 11000)]),
        ("Planta Pucusana", "Planta Arequipa", [("Tarifa", 9500)]),
        ("Planta Pucusana", "Arequipa", [("Tarifa", 9500)]),
        ("Planta Pucusana", "OL Juliaca", [("Tarifa", 11000)]),
        ("Planta Pucusana", "OL Arequipa", [("Tarifa", 9500)]),
        ("Planta Pucusana", "Juliaca", [("Tarifa", 11000)]),
    ],
}


def _seed_tarifario_sqlite(conn):
    for client_name, routes in _TARIFARIO_SEED_DATA.items():
        row = conn.execute("SELECT id FROM clients WHERE name = ?", (client_name,)).fetchone()
        if row is None:
            cur = conn.execute("INSERT INTO clients (name) VALUES (?)", (client_name,))
            client_id = cur.lastrowid
        else:
            client_id = row[0]
        # Si el cliente ya tiene alguna ruta en el tarifario, no se vuelve a
        # sembrar -- evita duplicar filas en cada reinicio y respeta lo que
        # Braulio ya haya editado/borrado a mano desde entonces.
        has_routes = conn.execute(
            "SELECT 1 FROM tariff_routes WHERE client_id = ?", (client_id,)
        ).fetchone()
        if has_routes:
            continue
        for origin, destination, items in routes:
            cur = conn.execute(
                "INSERT INTO tariff_routes (client_id, origin, destination) VALUES (?, ?, ?)",
                (client_id, origin, destination),
            )
            route_id = cur.lastrowid
            for order, (label, amount) in enumerate(items):
                conn.execute(
                    "INSERT INTO tariff_items (tariff_route_id, label, amount, sort_order) VALUES (?, ?, ?, ?)",
                    (route_id, label, amount, order),
                )


def _seed_tarifario_postgres(conn):
    cur = conn.cursor()
    for client_name, routes in _TARIFARIO_SEED_DATA.items():
        cur.execute("SELECT id FROM clients WHERE name = %s", (client_name,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO clients (name) VALUES (%s) RETURNING id", (client_name,))
            client_id = cur.fetchone()[0]
        else:
            client_id = row[0]
        cur.execute("SELECT 1 FROM tariff_routes WHERE client_id = %s", (client_id,))
        if cur.fetchone():
            continue
        for origin, destination, items in routes:
            cur.execute(
                "INSERT INTO tariff_routes (client_id, origin, destination) VALUES (%s, %s, %s) RETURNING id",
                (client_id, origin, destination),
            )
            route_id = cur.fetchone()[0]
            for order, (label, amount) in enumerate(items):
                cur.execute(
                    "INSERT INTO tariff_items (tariff_route_id, label, amount, sort_order) VALUES (%s, %s, %s, %s)",
                    (route_id, label, amount, order),
                )


# 22 sep, 2da ronda, pedido de Braulio ("en catalogos hay que incluir
# conceptos de detraccion y en este se puedan agregar o modificar los
# conceptos o porcentajes"): siembra la tabla `detraction_concepts` UNA
# SOLA VEZ (si ya tiene alguna fila, no hace nada -- respeta lo que Braulio
# ya haya editado/agregado/desactivado desde Catálogos) con el mismo
# contenido que antes vivía fijo en DETRACTION_GOODS_CATALOG. Import local
# de app.helpers (no al inicio del archivo) para evitar un import circular:
# app/helpers.py ya importa de app.db.
def _seed_detraction_concepts_sqlite(conn):
    from app.helpers import _DETRACTION_GOODS_SEED

    has_rows = conn.execute("SELECT 1 FROM detraction_concepts LIMIT 1").fetchone()
    if has_rows:
        return
    for order, (code, name, percentage) in enumerate(_DETRACTION_GOODS_SEED):
        conn.execute(
            "INSERT INTO detraction_concepts (code, name, percentage, sort_order) VALUES (?, ?, ?, ?)",
            (code, name, percentage, order),
        )


def _seed_detraction_concepts_postgres(conn):
    from app.helpers import _DETRACTION_GOODS_SEED

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM detraction_concepts LIMIT 1")
    if cur.fetchone():
        return
    for order, (code, name, percentage) in enumerate(_DETRACTION_GOODS_SEED):
        cur.execute(
            "INSERT INTO detraction_concepts (code, name, percentage, sort_order) VALUES (%s, %s, %s, %s)",
            (code, name, percentage, order),
        )


_PRAGMA_LINE_RE = re.compile(r"^\s*PRAGMA\s[^\n]*;\s*$", re.MULTILINE | re.IGNORECASE)
_CREATE_TABLE_START_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(")
_COL_REFERENCES_RE = re.compile(r"\s+REFERENCES\s+(\w+)\s*\(([^)]+)\)")
_COL_NAME_RE = re.compile(r"^\s*(\w+)\s")


def _strip_forward_fks(sql_text):
    """PostgreSQL valida que la tabla de una FOREIGN KEY ya exista en el
    momento de crear la tabla — a diferencia de SQLite, que no lo comprueba
    hasta que en verdad se hace un INSERT/UPDATE (y solo si PRAGMA
    foreign_keys está activo). schema.sql tiene varias tablas que
    referencian otra definida más abajo en el archivo (ej. "expenses"
    referencia "expense_concepts", definida ~340 líneas después) — nunca
    dio problema en SQLite, pero rompe la carga en Postgres con "relation
    ... does not exist". En vez de reordenar las ~31 tablas del archivo (y
    arriesgar romper algo al hacerlo a mano), esta función quita las
    cláusulas "REFERENCES tabla(col)" de las columnas al crear cada tabla,
    y las vuelve a agregar todas al final como ALTER TABLE ... ADD
    CONSTRAINT, una vez que todas las tablas ya existen — el resultado es
    exactamente la misma integridad referencial, solo que declarada en dos
    pasos. Verificado cargando el resultado contra un Postgres real (ver
    notas de despliegue): 0 errores, mismas 31 tablas y las 34 foreign
    keys del esquema original."""
    lines = sql_text.split("\n")
    out_lines = []
    fk_constraints = []
    current_table = None
    paren_depth = 0
    for line in lines:
        if current_table is None:
            m = _CREATE_TABLE_START_RE.search(line)
            if m:
                current_table = m.group(1)
                paren_depth = line.count("(") - line.count(")")
                out_lines.append(line)
                continue
            out_lines.append(line)
            continue

        ref_match = _COL_REFERENCES_RE.search(line)
        if ref_match:
            ref_table, ref_col = ref_match.group(1), ref_match.group(2)
            col_match = _COL_NAME_RE.match(line)
            if col_match:
                fk_constraints.append((current_table, col_match.group(1), ref_table, ref_col))
            line = _COL_REFERENCES_RE.sub("", line)

        paren_depth += line.count("(") - line.count(")")
        out_lines.append(line)
        if paren_depth <= 0:
            current_table = None

    # init_db() se ejecuta en cada arranque de la app (no solo la primera
    # vez), y Postgres no soporta "ADD CONSTRAINT IF NOT EXISTS" — a
    # diferencia de "CREATE TABLE IF NOT EXISTS" (ya idempotente) y "ADD
    # COLUMN IF NOT EXISTS" (usado en _apply_column_migrations_postgres).
    # Sin este bloque DO/EXCEPTION, el segundo arranque (cualquier reinicio
    # o redeploy posterior al primero) tira abajo la app entera con
    # "constraint ... already exists" (visto en producción real, 31 ago).
    alter_statements = "\n".join(
        f"DO $$ BEGIN\n"
        f"    ALTER TABLE {table} ADD CONSTRAINT fk_{table}_{column} "
        f"FOREIGN KEY ({column}) REFERENCES {ref_table}({ref_col});\n"
        f"EXCEPTION WHEN duplicate_object THEN NULL;\n"
        f"END $$;"
        for table, column, ref_table, ref_col in fk_constraints
    )
    # Se devuelven por separado (no concatenados) porque quien llama debe
    # correr las migraciones de columnas (_apply_column_migrations_postgres)
    # ENTRE los dos: si una columna referenciada por una FK es nueva (agregada
    # solo vía COLUMN_MIGRATIONS porque la tabla ya existía de antes, como
    # pasó con tires.moved_to_tire_id el 31 ago), el ALTER TABLE ADD
    # CONSTRAINT de más abajo fallaría con "column ... does not exist" si
    # corriera antes de que la columna exista de verdad. Ver init_db().
    return "\n".join(out_lines) + "\n", alter_statements + "\n"


def _sqlite_schema_to_postgres(sql_text):
    """Convierte el schema.sql (escrito para SQLite) a una variante
    compatible con PostgreSQL, sustituyendo solo la sintaxis que difiere
    entre motores. El archivo schema.sql en sí no se toca — esta conversión
    ocurre en memoria, únicamente al inicializar la base en modo Postgres.
    Devuelve (create_sql, fk_sql): las sentencias CREATE TABLE por un lado,
    y los ALTER TABLE ... ADD CONSTRAINT de las foreign keys por otro — deben
    ejecutarse por separado, con las migraciones de columnas en medio (ver
    init_db())."""
    sql_text = _PRAGMA_LINE_RE.sub("", sql_text)
    sql_text = sql_text.replace(
        "INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"
    )
    sql_text = _DATETIME_NOW_RE.sub(_PG_NOW_TIMESTAMP, sql_text)
    return _strip_forward_fks(sql_text)


def init_db(app):
    """Crea las tablas si no existen, usando app/schema.sql."""
    schema_path = Path(__file__).parent / "schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    database_url = app.config.get("DATABASE_URL") or ""
    if database_url.startswith(_POSTGRES_PREFIXES):
        import psycopg2

        conn = psycopg2.connect(_pg_connection_string(database_url))
        try:
            cur = conn.cursor()
            create_sql, fk_sql = _sqlite_schema_to_postgres(schema_sql)
            # Orden importante: 1) crear tablas, 2) agregar columnas nuevas
            # a tablas ya existentes (COLUMN_MIGRATIONS), 3) recién ahí
            # agregar las foreign keys — algunas FK referencian una columna
            # que en una base ya desplegada solo existe gracias al paso 2
            # (ej. tires.moved_to_tire_id, 31 ago).
            cur.execute(create_sql)
            _apply_column_migrations_postgres(conn)
            cur.execute(fk_sql)
            _apply_role_check_migration_postgres(conn)
            _apply_invoice_items_trip_nullable_postgres(conn)
            _backfill_user_roles_postgres(conn)
            _ensure_combustible_concept_postgres(conn)
            _fix_boleta_account_codes_postgres(conn)
            _seed_default_tire_codes_postgres(conn)
            _seed_tarifario_postgres(conn)
            _seed_detraction_concepts_postgres(conn)
            _backfill_tefacturo_codigo_bien_servicio_postgres(conn)
            conn.commit()
        finally:
            conn.close()
    else:
        db_path = Path(app.config["DATABASE_PATH"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.executescript(schema_sql)
        _apply_column_migrations_sqlite(conn)
        _apply_role_check_migration_sqlite(conn)
        _apply_invoice_items_trip_nullable_sqlite(conn)
        _backfill_user_roles_sqlite(conn)
        _ensure_combustible_concept_sqlite(conn)
        _fix_boleta_account_codes_sqlite(conn)
        _seed_default_tire_codes_sqlite(conn)
        _seed_tarifario_sqlite(conn)
        _seed_detraction_concepts_sqlite(conn)
        _backfill_tefacturo_codigo_bien_servicio_sqlite(conn)
        conn.commit()
        conn.close()


def register_db(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db(app)


def query_all(sql, params=()):
    db = get_db()
    if using_postgres():
        cur = db.cursor()
        cur.execute(_translate(sql), params)
        return cur.fetchall()
    return db.execute(sql, params).fetchall()


def query_one(sql, params=()):
    db = get_db()
    if using_postgres():
        cur = db.cursor()
        cur.execute(_translate(sql), params)
        return cur.fetchone()
    return db.execute(sql, params).fetchone()


def execute(sql, params=()):
    """Ejecuta INSERT/UPDATE/DELETE y hace commit. Devuelve el id de la fila
    insertada (equivalente a sqlite3's lastrowid) cuando la consulta es un
    INSERT sobre una tabla con columna "id" (ver _TABLES_WITHOUT_ID para la
    excepción)."""
    db = get_db()
    if using_postgres():
        cur, wants_id = _pg_execute(db._conn, sql, params)
        new_id = None
        if wants_id:
            try:
                row = cur.fetchone()
                new_id = row["id"] if row else None
            except Exception:
                new_id = None
        db.commit()
        return new_id
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


def get_setting(key, default=None):
    """Lee un ajuste general (tabla app_settings, clave/valor). Devuelve
    `default` si todavía no se ha guardado ese ajuste."""
    row = query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row["value"] if row is not None else default


def set_setting(key, value):
    execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
