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
USER_ROLES = ("ADMIN", "OPERADOR", "DESPACHADOR", "ALMACEN", "CONTABILIDAD", "MECANICO")


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
    que tocar ninguna de esas otras tablas."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if not row or not row[0] or "DESPACHADOR" in row[0]:
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
            _backfill_user_roles_postgres(conn)
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
        _backfill_user_roles_sqlite(conn)
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
