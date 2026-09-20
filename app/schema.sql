-- Esquema de base de datos para ERP de Transporte de Carga
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    -- Roles agregados el 3 sep (pedido de Braulio: definir roles de
    -- usuario) — ver PERMISSIONS en app/auth.py para el detalle de qué
    -- puede ver/editar cada uno. Ampliar esta lista en una base YA
    -- desplegada requiere además migrar el CHECK constraint ya creado
    -- (ver _apply_role_check_migration_sqlite/_postgres en app/db.py) —
    -- a diferencia de otros CHECK de este archivo, acá no hay forma de
    -- evitarlo derivando el estado de otra manera: el rol se usa tal
    -- cual como clave de PERMISSIONS.
    --
    -- 3 sep, mismo día, ronda siguiente (pedido de Braulio: "quiero poder
    -- poner mas de 1 rol a un usuario"): esta columna quedó como el rol
    -- "principal"/de respaldo (se sigue llenando y validando igual, nunca
    -- vacía) pero YA NO es la fuente de la verdad para permisos — eso pasó
    -- a la tabla user_roles de abajo (relación muchos-a-muchos). Se dejó a
    -- propósito en vez de borrarla: users.id es referenciado por 9 tablas
    -- y no aporta nada tocar esa columna; con user_roles ya alcanza para
    -- que un usuario tenga 2+ roles a la vez.
    role TEXT NOT NULL CHECK (role IN ('ADMIN', 'OPERADOR', 'DESPACHADOR', 'ALMACEN', 'CONTABILIDAD', 'MECANICO', 'RRHH')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 3 sep (pedido de Braulio, mismo día que se definieron los roles: "quiero
-- poder poner mas de 1 rol a un usuario, alguien puede ser almacen y
-- mecanico y otro solo almacen"): un usuario puede tener 2 o más roles a
-- la vez — sus permisos son la UNIÓN de lo que permite cada rol asignado
-- (ver can() en app/auth.py). Tabla nueva (no una migración de columna en
-- una tabla ya existente), así que no hace falta tocar ningún CHECK
-- desplegado — CREATE TABLE IF NOT EXISTS alcanza igual en una base nueva
-- que en una ya desplegada. Los usuarios ya existentes se completan solos
-- la primera vez que corre init_db() después de este cambio, tomando su
-- users.role de siempre como su único rol inicial (ver
-- _backfill_user_roles_sqlite/_postgres en app/db.py) — nadie pierde
-- acceso por este cambio en particular.
CREATE TABLE IF NOT EXISTS user_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('ADMIN', 'OPERADOR', 'DESPACHADOR', 'ALMACEN', 'CONTABILIDAD', 'MECANICO', 'RRHH')),
    UNIQUE (user_id, role)
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ruc TEXT,
    phone TEXT,
    email TEXT,
    address TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tarifario (15 sep, pedido de Braulio: "un menu que se llame tarifario,
-- este agrupado por clientes y uses este excel para los datos. Aparte de
-- estos 2 clientes quiero que dejes la opcion de poder agregar luego
-- manualmente otros"). Cada ruta (origen/destino) de un cliente puede
-- tener una o más tarifas con su propia etiqueta -- Backus trae 2 por
-- ruta ("PT + ENVASES"/"PT + VACIO", pedido explícito de Braulio) y
-- Lindley trae 1 ("Tarifa"); un cliente nuevo puede tener cualquier
-- cantidad (ver tariff_items). Sin UNIQUE(client_id, origen, destino) a
-- propósito: la planilla real de Backus que compartió Braulio trae
-- PUCALLPA -> CHANCHAMAYO dos veces con montos distintos (S/ 9899.81/
-- 9742.81 vs S/ 13943/13080) -- se importan ambas filas tal cual hasta
-- que confirme cuál es la correcta (ver la nota de entrega del patch).
CREATE TABLE IF NOT EXISTS tariff_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tariff_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tariff_route_id INTEGER NOT NULL REFERENCES tariff_routes(id),
    label TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate TEXT NOT NULL UNIQUE,
    brand TEXT,
    model TEXT,
    capacity_kg REAL,
    status TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (status IN ('ACTIVO', 'MANTENIMIENTO', 'INACTIVO')),
    -- Tipo de unidad: determina el diagrama de posiciones de neumáticos que
    -- se usa para ella (ver app/tire_positions.py). CAMION = unidad simple
    -- (chasis con carrocería propia, sin remolque separado); TRACTO =
    -- cabezal tractor (jala una carreta); CARRETA = semirremolque/carreta.
    vehicle_type TEXT NOT NULL DEFAULT 'CAMION' CHECK (vehicle_type IN ('CAMION', 'TRACTO', 'CARRETA')),
    -- Documentos obligatorios de la unidad (Perú): SOAT (seguro obligatorio)
    -- y Revisión Técnica vehicular. Solo se guarda su fecha de vencimiento;
    -- aparecen como alerta en el Panel cuando están por vencer.
    soat_expiry TEXT,
    technical_review_expiry TEXT,
    notes TEXT,
    current_km REAL,
    current_km_updated_at TEXT,
    -- Último cambio de aceite (3 sep, pedido de Braulio: carga masiva desde
    -- un Excel con el historial de cambios de varias placas — ver
    -- app/bulk_import.py OIL_CHANGE_COLUMNS y app/routes/flota.py
    -- import_oil_changes()). "Observación" (lo que pidió Braulio agregar)
    -- no es un campo nuevo: reutiliza el "notes" ya existente arriba, por
    -- decisión explícita de Braulio ("un campo de notas general de la
    -- unidad").
    last_oil_change_km REAL,
    last_oil_change_date TEXT,
    last_oil_change_workshop TEXT,
    last_oil_change_oil TEXT,
    gps_external_id TEXT,
    -- Propietario de la unidad (texto libre, tomado del catálogo
    -- "vehicle_owner" en Catálogos — ver app/routes/catalogos.py). Copia el
    -- nombre elegido, no un id, mismo patrón que otros campos que ya usan
    -- catálogo en vez de texto libre en este proyecto.
    owner TEXT,
    -- Documentos escaneados (foto o PDF) de la unidad, para verlos online
    -- (9 sep, pedido de Braulio) — mismo mecanismo de almacenamiento que la
    -- guía de transportista de un viaje o las fotos de conductores (ver
    -- app/storage.py, save_vehicle_document()). Cada columna guarda solo el
    -- nombre del archivo ya subido (o NULL si no se ha subido nada
    -- todavía) — el archivo en sí vive en disco/S3, nunca en la base de
    -- datos. Tarjeta de propiedad, Revisión Técnica y MTC aplican a
    -- cualquier tipo de unidad; SOAT y la Póliza de responsabilidad civil
    -- solo a Camión/Tracto (una Carreta no se asegura ni tiene SOAT propio
    -- en Perú — depende del tracto que la jala) — ver
    -- VEHICLE_DOCUMENT_TYPES en app/routes/flota.py para el detalle exacto
    -- de qué tipo de unidad necesita cada documento.
    property_card_filename TEXT,
    soat_filename TEXT,
    technical_review_filename TEXT,
    -- 9 sep, pedido de Braulio: "revisión técnica especial", opcional para
    -- cualquier tipo de unidad — a diferencia de los demás documentos de
    -- esta tabla, no se resalta como faltante en ningún lado (ver
    -- VEHICLE_DOCUMENT_TYPES en app/routes/flota.py, único con
    -- `opcional=True`).
    special_technical_review_filename TEXT,
    mtc_filename TEXT,
    civil_liability_policy_filename TEXT,
    -- 15 sep, pedido de Braulio: "cuando ingrese una unidad [a mantenimiento]
    -- debe salir la opcion [de] que ya este disponible para programar...
    -- esta opcion solo la puede habilitar el administrador y el personal de
    -- mantenimiento." Solo importa mientras status='MANTENIMIENTO' -- con
    -- cualquier otro status la unidad ya está disponible normalmente, sin
    -- necesitar este flag (ver _active_vehicles()/_active_trailers() en
    -- app/routes/viajes.py, que solo la consultan en ese caso). Se resetea a
    -- 0 cada vez que la unidad sale de mantenimiento (ver edit_vehicle() en
    -- app/routes/flota.py) para no dejar un flag viejo activo la próxima vez
    -- que vuelva a entrar a mantenimiento. Sin CHECK: es un booleano simple
    -- (0/1), igual que "paid"/"double_driver" en trips.
    available_for_scheduling INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS drivers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    document_number TEXT,
    license_number TEXT,
    license_expiry TEXT,
    -- Fecha del último examen médico ocupacional y cuándo vence.
    medical_exam_date TEXT,
    medical_exam_expiry TEXT,
    -- Requisitos específicos para operar con Backus: examen de manejo,
    -- capacitación del plan de tráfico (backus_training_*), y escuela de
    -- conductores (dds_*, nombre de columna heredado del primer borrador
    -- del feature — el campo real, según la plantilla de Braulio, es
    -- "Escuela de conductores", no DDS). Se guarda la fecha del último
    -- realizado y cuándo vence cada uno.
    backus_driving_exam_date TEXT,
    backus_driving_exam_expiry TEXT,
    backus_training_date TEXT,
    backus_training_expiry TEXT,
    dds_date TEXT,
    dds_expiry TEXT,
    phone TEXT,
    -- Nombre del archivo de la foto del conductor, guardado con el mismo
    -- mecanismo que los comprobantes de gastos (ver app/storage.py, bajo
    -- un prefijo/carpeta separada para no mezclarlos).
    photo_filename TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (status IN ('ACTIVO', 'INACTIVO')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    driver_id INTEGER REFERENCES drivers(id),
    -- Segundo conductor, solo cuando double_driver = 1 (viaje "doble conductor").
    driver2_id INTEGER REFERENCES drivers(id),
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    cargo_description TEXT,
    cargo_weight_kg REAL,
    scheduled_date TEXT NOT NULL,
    delivered_date TEXT,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'EN_CURSO', 'ENTREGADO', 'CANCELADO')),
    rate REAL NOT NULL DEFAULT 0,
    -- Comisión que le corresponde a cada conductor por este viaje. Se sugiere
    -- automáticamente según el monto configurado para la ruta (origen-destino)
    -- en el catálogo de Rutas, ajustado por double_driver (x0.6) y single_leg
    -- (x0.5) cuando aplican, pero se puede editar por viaje. Si double_driver
    -- está activo, driver_id y driver2_id reciben cada uno este mismo monto
    -- completo (no se reparte).
    driver_commission REAL NOT NULL DEFAULT 0,
    -- Viaje con doble conductor: comisión sugerida = 60% de la comisión normal
    -- de la ruta, y se le asigna ese mismo monto a CADA uno de los 2 conductores.
    double_driver INTEGER NOT NULL DEFAULT 0,
    -- Viaje de un solo tramo (no ida y vuelta / no varias etapas): comisión
    -- sugerida = 50% de la comisión normal de la ruta. Se puede combinar con
    -- double_driver (los porcentajes se multiplican: 60% x 50% = 30% c/u).
    single_leg INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    invoiced INTEGER NOT NULL DEFAULT 0,
    -- 3 sep, pedido de Braulio ("cambios en el módulo de viajes") — empresa
    -- que opera el viaje, mismo patrón/valores que quotations.issuer.
    issuer TEXT NOT NULL DEFAULT 'HARRASO' CHECK (issuer IN ('HARRASO', 'BRMS')),
    -- Carreta (semirremolque) enganchada a la unidad tracto de vehicle_id —
    -- solo aplica a viajes con unidad propia (ownership='PROPIA'); se elige
    -- del catálogo de Flota igual que vehicle_id, restringido a
    -- vehicle_type='CARRETA' en la consulta del formulario (ver
    -- _active_trailers() en app/routes/viajes.py).
    trailer_vehicle_id INTEGER REFERENCES vehicles(id),
    cargo_type TEXT CHECK (cargo_type IN ('PLATAFORMA', 'CONTENEDOR', 'PARIHUELERO', 'FURGON', 'OTROS')),
    -- Código del contenedor + foto de evidencia de buen estado (10 sep,
    -- pedido de Braulio) — solo aplican cuando cargo_type='CONTENEDOR' (se
    -- limpian a NULL si el viaje no es de contenedor, ver new()/edit() en
    -- app/routes/viajes.py). La foto se guarda con el mismo mecanismo que
    -- la guía de transportista (ver app/storage.py).
    container_code TEXT,
    container_photo_filename TEXT,
    -- Unidad propia (de Flota) vs. subcontratada a un tercero. Si es
    -- TERCERO, vehicle_id/trailer_vehicle_id no aplican — la unidad del
    -- tercero se anota en third_party_unit (texto libre, no está en Flota).
    ownership TEXT NOT NULL DEFAULT 'PROPIA' CHECK (ownership IN ('PROPIA', 'TERCERO')),
    third_party_name TEXT,
    third_party_unit TEXT,
    third_party_rate REAL,
    third_party_payment_term TEXT CHECK (
        third_party_payment_term IN ('CONTADO', '15_DIAS', '30_DIAS', '45_DIAS', '60_DIAS')
    ),
    -- Guía de transportista: documento propio del tercero/transportista que
    -- hizo el viaje (distinto de la guía de remisión SUNAT que ya genera el
    -- módulo Guías) — se agrega DESPUÉS de creado el viaje, con un número a
    -- mano y/o una foto/PDF adjunta (mismo mecanismo de almacenamiento que
    -- los comprobantes de Liquidaciones, ver app/storage.py).
    carrier_waybill_number TEXT,
    carrier_waybill_filename TEXT,
    -- 15 sep, pedido de Braulio: "Una vez iniciado el viaje, a la hora de
    -- crear o subir guia primero debe especificarse si la guia de remision
    -- figura nuestros datos como transportista. Si figuran, no es necesario
    -- emitir una guia nueva, solo adjuntar la de remitente. Si no figuran,
    -- ahi es necesario crear la guia de transportista." Campos NUEVOS y
    -- separados de carrier_waybill_* de arriba a propósito: carrier_waybill_*
    -- es un documento genérico del transportista (no ligado a esta pregunta
    -- de "¿el remitente ya nos puso como transportista?"); aquí se guarda
    -- específicamente la guía de remisión que emitió el REMITENTE (el
    -- cliente/dueño de la carga), cuando esa guía ya trae los datos de
    -- Harraso/BRMS como transportista y por eso no hace falta emitir una
    -- guía de transportista nueva. NULL = todavía no se respondió la
    -- pregunta; 'SI'/'NO' = respuesta. Sin CHECK (mismo criterio que
    -- ownership/issuer cuando la columna se agrega después vía
    -- COLUMN_MIGRATIONS) -- se valida en Python.
    shipper_waybill_shows_carrier TEXT,
    shipper_waybill_number TEXT,
    shipper_waybill_filename TEXT,
    -- Conformidad de entrega (4 sep, pedido de Braulio): foto o PDF del
    -- comprobante de entrega firmado, adjuntado mientras el viaje está
    -- EN_CURSO. Adjuntarla es lo que marca el viaje como ENTREGADO (ver
    -- save_delivery_proof() en app/routes/viajes.py) — no hay forma de
    -- llegar a ENTREGADO sin este archivo.
    delivery_proof_filename TEXT,
    -- 20 sep, pedido de Braulio: "número de pedido" que Backus o Naviera
    -- Oriente (clientes de BRMS) mandan DESPUÉS de la guía -- necesario
    -- para poder facturarles. Se guarda por viaje (no por guía electrónica
    -- en `waybills`) porque un viaje puede tener guía electrónica, guía de
    -- transportista (carrier_waybill_number) o guía del remitente
    -- (shipper_waybill_number) indistintamente, y el pedido aplica igual
    -- en cualquiera de los tres casos -- ver app/routes/guias.py
    -- (ORDER_NUMBER_CLIENTS, link_orders()/link_orders_upload(), que
    -- también enlaza en bloque desde el Excel semanal que manda Naviera
    -- Oriente). Texto libre y opcional.
    client_order_number TEXT,
    -- Pagado: si el cliente ya pagó este viaje. Independiente de "invoiced"
    -- (si ya se facturó) — ambos se pueden marcar/desmarcar a mano desde el
    -- detalle del viaje, además de que "invoiced" se sigue marcando solo al
    -- generar una factura desde Facturación.
    paid INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    expense_date TEXT NOT NULL,
    description TEXT,
    -- Nombre del archivo del comprobante adjunto (foto o PDF), guardado en
    -- instance/receipts/. NULL si no se adjuntó nada.
    receipt_filename TEXT,
    -- Campos contables para el export de liquidación (ver
    -- app/accounting.py y la sección "Gastos: liquidación contable
    -- exportable" del README). concept_id determina la cuenta contable y
    -- el tipo de comprobante/documento; los demás son datos propios del
    -- comprobante de este gasto en particular. Todos NULL/con valor por
    -- defecto para no romper gastos ya registrados antes de este cambio.
    concept_id INTEGER REFERENCES expense_concepts(id),
    document_number TEXT,
    due_date TEXT,
    provider_ruc TEXT,
    provider_name TEXT,
    currency TEXT NOT NULL DEFAULT 'S',
    exchange_rate REAL,
    -- A qué anticipo de viáticos quedó vinculado este gasto al momento de
    -- liquidarlo (ver app/routes/viaticos.py `liquidate()`). NULL si el
    -- gasto no forma parte de ninguna liquidación (todavía, o nunca).
    expense_advance_id INTEGER REFERENCES expense_advances(id),
    -- Tipo de comprobante elegido al registrar el gasto (10 sep, pedido de
    -- Braulio), 'factura' o 'boleta' — se pide ANTES del concepto. Si es
    -- 'factura', la cuenta contable y el tipo de documento del export se
    -- fuerzan a 42121/01 sin importar el concepto (ver
    -- app.accounting.FACTURA_ACCOUNT_CODE/FACTURA_DOCUMENT_TYPE_CODE); si
    -- es 'boleta', se mantiene la cuenta/documento propios del concepto,
    -- como ya funcionaba. NULL en gastos registrados antes de este cambio
    -- — se tratan igual que 'boleta' al exportar, para no alterar
    -- liquidaciones ya cerradas.
    voucher_type TEXT,
    -- Datos del comprobante de combustible (10 sep, pedido de Braulio) —
    -- solo se completan cuando el concepto elegido es "Combustible": nombre
    -- del grifo y galones/precio unitario del vale. El "Precio total" no
    -- se guarda en una columna aparte: es el mismo "amount" de arriba,
    -- calculado solo como galones × precio unitario (ver
    -- new_expense()/edit_expense() en app/routes/liquidaciones.py).
    -- Ciudad donde se hizo el consumo (10 sep, 2da ronda, pedido de
    -- Braulio: "ciudad, nombre del grifo, nro vale/factura..." para poder
    -- listar este gasto como una fila más en la tabla de combustible de la
    -- liquidación del viaje — ver _fuel_rows() en app/routes/liquidaciones.py).
    -- fuel_station_id (10 sep, 3ra ronda, pedido de Braulio: "dentro de
    -- catalogos hay que poner los grifos... en la pantalla de liquidaciones
    -- se eligan los que estan registrados") amarra el gasto al grifo
    -- elegido en el catálogo (ver CREATE TABLE fuel_stations más abajo,
    -- administrado desde Catálogos → Grifos). fuel_city/fuel_station_name
    -- se siguen guardando acá también, como una "foto" de ese momento
    -- (ciudad y razón social del grifo elegido) para que la fila no cambie
    -- de valor si el grifo se edita o desactiva más adelante.
    fuel_station_id INTEGER REFERENCES fuel_stations(id),
    fuel_city TEXT,
    fuel_station_name TEXT,
    fuel_gallons REAL,
    fuel_unit_price REAL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS maintenance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    type TEXT NOT NULL,
    maintenance_date TEXT NOT NULL,
    cost REAL NOT NULL DEFAULT 0,
    description TEXT,
    odometer_km REAL,
    next_due_date TEXT,
    next_due_km REAL,
    -- Suma de los minutos estimados de los trabajos seleccionados (ver
    -- maintenance_job_types / maintenance_record_jobs) al momento de guardar.
    estimated_minutes INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Catálogo de "trabajos" de mantenimiento con su tiempo estimado (ej.
-- "Cambio de aceite" = 60 min). Se administra desde Mantenimiento →
-- Trabajos, con un botón para registrar trabajos nuevos que no estén en
-- la lista. Independiente del catálogo de "conceptos" (catalog_items,
-- categoría maintenance_type) que se sigue usando para clasificar el
-- registro; este catálogo es específicamente para estimar tiempos.
CREATE TABLE IF NOT EXISTS maintenance_job_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    estimated_minutes INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Catálogo de mecánicos del taller, para poder asignar quién está
-- trabajando cada trabajo dentro de una orden de mantenimiento. Se
-- administra desde Mantenimiento → Mecánicos, mismo patrón que el
-- catálogo de Trabajos (nombre + activo/inactivo). mechanic_type es uno de
-- app.seed_data.MECHANIC_TYPES (Senior/Junior/Practicante/Otros) — pedido
-- de Braulio, 28 ago (2ª ronda): cada tipo tiene su propio costo por
-- minuto (ver app_settings, claves labor_cost_per_minute_<tipo>).
CREATE TABLE IF NOT EXISTS mechanics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    mechanic_type TEXT NOT NULL DEFAULT 'Otros',
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Qué trabajos (de maintenance_job_types) se realizaron en cada registro de
-- mantenimiento. Se guarda una copia del nombre y minutos estimados al
-- momento de seleccionarlos, para que el historial no cambie si luego se
-- edita el catálogo de trabajos. `status` permite marcar cada trabajo como
-- PENDIENTE o TERMINADO por separado dentro de la misma orden, y
-- `mechanic_id`/`mechanic_name` quién lo está trabajando (se guarda también
-- el nombre aparte, mismo motivo que job_name: que el historial no cambie
-- si luego se edita o desactiva ese mecánico en el catálogo).
-- `mechanic_type` (Senior/Junior/Practicante/Otros) es el tipo de mecánico
-- que hace ese trabajo — se elige al registrar la orden (o después, en su
-- detalle) y determina el costo de mano de obra sugerido para ese trabajo;
-- es independiente de `mechanic_id`/`mechanic_name` (quién específicamente
-- lo hace), aunque normalmente coinciden con el tipo registrado para esa
-- persona en el catálogo de Mecánicos. `mechanic_count` (pedido de
-- Braulio, 28 ago — 3ª ronda) es cuántos mecánicos de ese tipo hacen falta
-- para el trabajo — el costo sugerido de ese trabajo es
-- minutos × costo_por_minuto_del_tipo × mechanic_count.
CREATE TABLE IF NOT EXISTS maintenance_record_jobs (
    maintenance_record_id INTEGER NOT NULL REFERENCES maintenance_records(id),
    job_type_id INTEGER REFERENCES maintenance_job_types(id),
    job_name TEXT NOT NULL,
    estimated_minutes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDIENTE',
    mechanic_type TEXT,
    mechanic_count INTEGER NOT NULL DEFAULT 1,
    mechanic_id INTEGER REFERENCES mechanics(id),
    mechanic_name TEXT,
    completed_at TEXT,
    PRIMARY KEY (maintenance_record_id, job_name)
);

-- Cuadrilla de un trabajo dentro de una orden (2 sep, pedido de Braulio:
-- "en el cambio de aceite se pueda elegir 1 mecánico senior y 2 junior" —
-- antes un trabajo solo admitía UN tipo+cantidad de mecánico, guardado
-- directo en maintenance_record_jobs.mechanic_type/mechanic_count). Ahora
-- un trabajo puede tener varias combinaciones tipo+cantidad a la vez; cada
-- una es una fila acá. El costo de mano de obra de un trabajo es la suma,
-- por cada fila de su cuadrilla, de minutos_del_trabajo × costo_por_minuto
-- del tipo × mechanic_count. maintenance_record_jobs.mechanic_type/
-- mechanic_count se dejan de usar para trabajos nuevos (quedan en NULL/1
-- por defecto) — se conservan solo por compatibilidad con órdenes ya
-- creadas antes de este cambio, que se siguen leyendo como una cuadrilla
-- de una sola fila si no tienen ninguna acá (ver _job_crew() en
-- app/routes/mantenimiento.py).
-- Sin FOREIGN KEY declarada a propósito: maintenance_record_jobs usa una
-- llave primaria compuesta (maintenance_record_id, job_name) y el
-- mecanismo de _strip_forward_fks/init_db() (app/db.py) que traduce las
-- foreign keys de una sola columna a Postgres no entiende una FK
-- compuesta de dos columnas — declararla igual arriesgaba corromper el
-- esquema traducido en producción. La integridad se mantiene desde el
-- código (siempre se inserta/borra a través de las rutas de Mantenimiento).
-- mechanic_id/mechanic_name (18 sep, pedido de Braulio: "cuando se
-- selecciona el tipo de mecanico tambien se debe elegir el nombre de la
-- base de registrados") -- quién específicamente cubre esa fila de
-- cuadrilla, elegido del catálogo de Mecánicos (mismo patrón que
-- maintenance_record_jobs.mechanic_id/mechanic_name: se guarda también el
-- nombre aparte para que el historial no cambie si luego se edita o
-- desactiva ese mecánico). Es independiente de mechanic_type/mechanic_count
-- de esta misma fila -- opcional, una fila de cuadrilla puede quedar sin
-- mecánico específico asignado (solo tipo + cantidad).
CREATE TABLE IF NOT EXISTS maintenance_record_job_crew (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maintenance_record_id INTEGER NOT NULL,
    job_name TEXT NOT NULL,
    mechanic_type TEXT NOT NULL DEFAULT 'Otros',
    mechanic_count INTEGER NOT NULL DEFAULT 1,
    mechanic_id INTEGER REFERENCES mechanics(id),
    mechanic_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_maintenance_record_job_crew_job ON maintenance_record_job_crew(maintenance_record_id, job_name);

-- Módulo Inventarios (29 ago — pedido de Braulio: "cada compra de
-- repuestos debe figurar el proveedor, orden de compra, cantidad y
-- precio; una vez ingresado al stock, Mantenimiento puede disponer de
-- estos repuestos"). Unifica lo que antes era el catálogo "Materiales" de
-- Mantenimiento (nombre + costo unitario, sin stock, tabla
-- maintenance_materials) con control de stock real: este es ahora el
-- catálogo de repuestos, con la cantidad disponible en almacén.
-- unit_cost es el costo de referencia (se actualiza solo al último precio
-- de compra recibido) usado para sugerir el costo de mano de obra +
-- materiales de una orden de mantenimiento.
CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    unit_cost REAL NOT NULL DEFAULT 0,
    stock_quantity REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Proveedores de repuestos — catálogo simple, mismo patrón que Mecánicos y
-- el resto de catálogos de Mantenimiento/Inventarios.
CREATE TABLE IF NOT EXISTS inventory_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ruc TEXT,
    phone TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Compras de repuestos (encabezado): proveedor + N° de orden de compra +
-- fecha. Nace en PENDIENTE (borrador, todavía editable/eliminable) y debe
-- ser AUTORIZADA por un Administrador (authorized_at/authorized_by_*, 1 sep
-- — pedido de Braulio: "cuando lo autoriza un administrador se registra su
-- autorización debajo de la orden") antes de poder generarse su PDF o
-- recibirse — ver purchases_authorize()/purchases_pdf() en
-- app/routes/inventarios.py. Una vez autorizada, la recepción puede
-- llegar en varias entregas parciales (ver inventory_purchase_receptions
-- más abajo) — "status" solo pasa a RECIBIDO cuando ya no se espera más
-- mercadería (todo llegó, o el usuario cierra la orden con lo que llegó);
-- mientras tanto sigue en PENDIENTE aunque ya esté autorizada y con
-- recepciones parciales — el avance real se calcula comparando
-- inventory_purchase_items.received_quantity contra quantity (ver
-- _purchase_display_status()), no se guarda como un tercer valor de
-- status para no tener que alterar el CHECK ya desplegado en producción.
-- authorized_by_name/received_by_name (en las recepciones) son copias,
-- mismo motivo que provider_name: el historial no cambia si el usuario se
-- desactiva después.
CREATE TABLE IF NOT EXISTS inventory_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER REFERENCES inventory_providers(id),
    provider_name TEXT NOT NULL,
    purchase_order_number TEXT,
    purchase_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'RECIBIDO')),
    received_at TEXT,
    notes TEXT,
    authorized_at TEXT,
    authorized_by_name TEXT,
    authorized_by_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Líneas de una compra: qué repuesto, cuánta cantidad y a qué precio
-- unitario. item_name es una copia (mismo motivo que en el resto del
-- proyecto: que el historial de la compra no cambie si el repuesto se
-- renombra o desactiva después en el catálogo). received_quantity (1 sep)
-- es el acumulado de lo que ya llegó de esta línea, sumado en cada
-- recepción parcial — nunca puede superar quantity (validado en
-- purchases_receive()).
CREATE TABLE IF NOT EXISTS inventory_purchase_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES inventory_purchases(id),
    item_id INTEGER REFERENCES inventory_items(id),
    item_name TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    received_quantity REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inventory_purchase_items_purchase ON inventory_purchase_items(purchase_id);

-- Recepciones de una orden de compra (1 sep): un evento por cada vez que
-- llega mercadería al almacén — puede haber varios por orden, ya que
-- Braulio pidió explícitamente poder recibir en entregas parciales ("puede
-- que a veces no lleguen todos"). Cabecera + líneas, mismo patrón ya usado
-- en el proyecto (inventory_purchases+items, tire_rotations+moves).
CREATE TABLE IF NOT EXISTS inventory_purchase_receptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES inventory_purchases(id),
    received_at TEXT NOT NULL,
    received_by_name TEXT,
    received_by_user_id INTEGER REFERENCES users(id),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inventory_purchase_receptions_purchase ON inventory_purchase_receptions(purchase_id);

CREATE TABLE IF NOT EXISTS inventory_purchase_reception_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reception_id INTEGER NOT NULL REFERENCES inventory_purchase_receptions(id),
    purchase_item_id INTEGER NOT NULL REFERENCES inventory_purchase_items(id),
    item_name TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inventory_purchase_reception_items_reception ON inventory_purchase_reception_items(reception_id);

-- Materiales/repuestos usados en cada orden de mantenimiento (además de
-- los trabajos). Se guarda una copia del nombre y costo unitario al
-- momento de agregarlo (mismo motivo que job_name/estimated_minutes en
-- maintenance_record_jobs: que el historial no cambie si luego se edita
-- ese repuesto en el catálogo). Usa un id propio (a diferencia de
-- maintenance_record_jobs) porque no hay razón para impedir agregar el
-- mismo repuesto más de una vez a la misma orden. Al agregarse, descuenta
-- la cantidad del stock de inventory_items (se permite que el stock quede
-- en negativo — solo se avisa, no se bloquea; pedido explícito de
-- Braulio).
CREATE TABLE IF NOT EXISTS maintenance_record_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maintenance_record_id INTEGER NOT NULL REFERENCES maintenance_records(id),
    material_id INTEGER REFERENCES inventory_items(id),
    material_name TEXT NOT NULL,
    unit_cost REAL NOT NULL DEFAULT 0,
    quantity REAL NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_maintenance_record_materials_record ON maintenance_record_materials(maintenance_record_id);

-- Neumáticos: una fila por cada llanta que ha pasado por una posición de
-- una unidad (tracto, carreta o camión). No se guarda un "kilometraje
-- acumulado" como número aparte: se calcula en el momento comparando el
-- kilometraje actual de la unidad (vehicles.current_km, que ya se actualiza
-- solo con el movimiento del vehículo vía Mantenimiento/Flota/GPS) contra
-- km_at_install. Así el acumulado siempre queda al día automáticamente.
-- Cuando se reemplaza una llanta, la fila vieja pasa a status='RETIRADO'
-- (con fecha/km de retiro) y se crea una fila nueva para esa posición —
-- esto conserva el historial completo de cada posición en el tiempo.
-- Inventario de llantas por código (2 sep, pedido de Braulio): un registro
-- independiente del inventario de repuestos (ver "inventory_items"). Cada
-- llanta física tiene una fila acá, creada ANTES de poder asignarla a una
-- unidad — "tires" (más abajo) sigue siendo el historial de instalaciones
-- (una fila por cada vez que esa llanta estuvo en una posición), enlazado a
-- su registro de inventario vía tires.tire_inventory_id.
CREATE TABLE IF NOT EXISTS tire_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Código físico de la llanta (el que trae grabado / el que se le pone al
    -- comprarla), único para poder ubicarla rápido.
    code TEXT NOT NULL,
    brand TEXT,
    -- 18 sep, pedido de Braulio ("historial por unidad... debe mostrar los
    -- datos Marca, Modelo, Tipo de llanta"): datos de la llanta física en
    -- sí, independientes de en qué unidad esté instalada. tire_type es el
    -- rol de la llanta (para saber en qué posiciones debería ir según su
    -- diseño): TRACCION, MIXTA o DIRECCION. Ambos opcionales -- llantas
    -- registradas antes de este cambio quedan sin dato hasta que se edite.
    model TEXT,
    tire_type TEXT CHECK (tire_type IN ('TRACCION', 'MIXTA', 'DIRECCION')),
    -- Última medida de profundidad de cocada conocida (mm) -- se copia acá
    -- desde la última fila de tire_inspections (por fecha) cada vez que se
    -- registra una medición nueva, para poder mostrarla de un vistazo en el
    -- inventario sin tener que ir al historial de inspecciones cada vez.
    -- NULL hasta la primera medición.
    tread_depth_mm REAL,
    -- Vida útil estimada en km — se copia a tires.expected_life_km al
    -- asignar la llanta a una unidad (donde puede ajustarse si hace falta),
    -- pero queda acá como el valor de referencia de la llanta en sí.
    expected_life_km REAL NOT NULL DEFAULT 60000,
    -- 'DISPONIBLE' = en inventario, todavía sin instalar en ninguna unidad.
    -- 'ASIGNADA' = instalada actualmente en una unidad (ver la fila ACTIVO
    -- correspondiente en "tires" con este tire_inventory_id).
    -- 'RETIRADA' = se descartó definitivamente (no se movió a otra unidad).
    status TEXT NOT NULL DEFAULT 'DISPONIBLE' CHECK (status IN ('DISPONIBLE', 'ASIGNADA', 'RETIRADA')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tire_inventory_code ON tire_inventory(code);

-- 18 sep, pedido de Braulio: "debe haber un historial de fecha de la
-- inspeccion y medida encontrada" -- una fila por cada vez que se mide la
-- profundidad de cocada de una llanta de inventario (con su propio código),
-- sin importar si en ese momento está Disponible, Asignada o Retirada. Se
-- guarda por tire_inventory_id (la llanta física en sí) y no por "tires"
-- (que es la instalación puntual en una unidad) porque la medición es un
-- dato de la llanta, no de dónde estaba instalada -- aunque sí queda
-- registrado en qué unidad/posición estaba en ese momento, si aplica, solo
-- como referencia (vehicle_plate/position_code de texto libre, no FK, para
-- no perder el dato si esa instalación se borra o cambia después).
CREATE TABLE IF NOT EXISTS tire_inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tire_inventory_id INTEGER NOT NULL REFERENCES tire_inventory(id),
    inspection_date TEXT NOT NULL,
    tread_depth_mm REAL NOT NULL,
    vehicle_plate_at_inspection TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tire_inspections_tire ON tire_inspections(tire_inventory_id);

CREATE TABLE IF NOT EXISTS tires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    -- Código de posición según el diagrama de app/tire_positions.py, ej.
    -- "EJE1_IZQ", "EJE2_IZQ_EXT".
    position_code TEXT NOT NULL,
    brand TEXT,
    install_date TEXT NOT NULL,
    km_at_install REAL NOT NULL DEFAULT 0,
    -- Vida útil estimada en km para esta llanta (varía por marca/modelo);
    -- se usa para calcular el % de desgaste y las alertas.
    expected_life_km REAL NOT NULL DEFAULT 80000,
    status TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (status IN ('ACTIVO', 'RETIRADO')),
    removed_date TEXT,
    removed_km REAL,
    removal_reason TEXT,
    -- Qué pasó con la llanta al retirarla (31 ago — pedido de Braulio: un
    -- historial que diga a qué otro carro se puso una llanta, o si se
    -- descartó). NULL mientras está ACTIVO. 'MOVIDA' significa que se
    -- instaló en otra unidad/posición — moved_to_tire_id apunta a esa fila
    -- nueva (la misma llanta física, otra fila porque cada fila es una
    -- instalación puntual). Para ir "hacia atrás" desde la fila nueva y
    -- encontrar de dónde vino, se busca la fila cuyo moved_to_tire_id sea
    -- el id de esta — no hace falta una columna inversa.
    disposition TEXT,
    moved_to_tire_id INTEGER REFERENCES tires(id),
    -- Llanta de "tire_inventory" que corresponde a esta instalación (2 sep):
    -- NULL para llantas creadas antes de este cambio (nunca pasaron por el
    -- inventario por código). Se copia de fila en fila al mover una llanta
    -- de unidad (misma llanta física, mismo registro de inventario).
    tire_inventory_id INTEGER REFERENCES tire_inventory(id),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tires_vehicle ON tires(vehicle_id);
-- Garantiza que no haya dos llantas "ACTIVO" a la vez en la misma posición
-- de la misma unidad (índice único parcial).
CREATE UNIQUE INDEX IF NOT EXISTS idx_tires_active_position
    ON tires(vehicle_id, position_code) WHERE status = 'ACTIVO';

-- Rotación de neumáticos (30 ago — pedido de Braulio: poder rotar llantas
-- por desgaste). Mueve llantas ACTIVAS entre posiciones de una misma
-- unidad para parejar el desgaste — no cambia km_at_install ni
-- expected_life_km de cada llanta, solo su position_code: el acumulado de
-- cada llanta se sigue calculando igual que siempre, ahora medido desde su
-- nueva posición. tire_rotations es el evento (una rotación puede mover
-- varias llantas a la vez); tire_rotation_moves es el detalle de qué
-- llanta pasó de qué posición a cuál.
CREATE TABLE IF NOT EXISTS tire_rotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    rotation_date TEXT NOT NULL,
    km_at_rotation REAL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tire_rotations_vehicle ON tire_rotations(vehicle_id);

CREATE TABLE IF NOT EXISTS tire_rotation_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rotation_id INTEGER NOT NULL REFERENCES tire_rotations(id),
    tire_id INTEGER NOT NULL REFERENCES tires(id),
    from_position_code TEXT NOT NULL,
    to_position_code TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tire_rotation_moves_rotation ON tire_rotation_moves(rotation_id);

-- Inspecciones de unidades (checklist antes/después de un viaje: llantas,
-- frenos, luces, etc.). Los ítems del checklist se administran desde
-- Catálogos (categoría "inspection_item").
CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    trip_id INTEGER REFERENCES trips(id),
    driver_id INTEGER REFERENCES drivers(id),
    type TEXT NOT NULL CHECK (type IN ('PRE', 'POST')),
    inspection_date TEXT NOT NULL,
    notes TEXT,
    -- Campos de los checklists físicos detallados de Harraso (tracto y
    -- carreta): solo se llenan cuando la inspección se hace con uno de
    -- esos checklists detallados (ver app/detailed_checklists.py). NULL
    -- para el checklist genérico. odometer_km queda NULL también para
    -- carretas, que no tienen odómetro propio.
    checklist_code TEXT,
    location TEXT,
    odometer_km REAL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS inspection_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id),
    item_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'FALLA', 'NA')),
    observation TEXT,
    -- Solo para los checklists detallados (tracto/carreta): la sección a
    -- la que pertenece el ítem (ver app/detailed_checklists.py) y un
    -- valor extra puntual (cantidad de recarga, "sopleteado" sí/no, o el
    -- código de llanta según posición). NULL en el checklist genérico.
    section TEXT,
    extra_value TEXT,
    -- 18 sep, pedido de Braulio ("la altura de la cocada... tiene que
    -- estar enlazado con las inspecciones... cada vez que se realice una
    -- inspeccion nueva actualizar la medida de la llanta en el
    -- inventario"): solo se llena en las filas de la sección de llantas
    -- (section = REVISION_LLANTAS) del checklist detallado. Al guardar la
    -- inspección, si esta posición tiene una llanta ACTIVA instalada (ver
    -- "tires" en app/routes/neumaticos.py), este valor se copia también
    -- como una fila nueva de "tire_inspections" para esa llanta -- ver
    -- record_tire_inspection() en app/routes/neumaticos.py, usado desde
    -- _save_detailed_inspection() en app/routes/inspecciones.py.
    tread_depth_mm REAL
);

-- Presupuesto mensual de gastos por unidad o por tipo de gasto, para
-- avisar cuando el gasto acumulado del mes lo supera.
CREATE TABLE IF NOT EXISTS expense_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('VEHICLE', 'TYPE')),
    scope_value TEXT NOT NULL,
    monthly_amount REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scope_type, scope_value)
);

-- Rutas frecuentes con un monto de viáticos predeterminado, para agilizar
-- el anticipo de gastos de viaje al conductor. También define el monto de
-- comisión estándar por viaje en esa ruta (igual para cualquier conductor).
CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    default_expense_amount REAL NOT NULL DEFAULT 0,
    default_commission_amount REAL NOT NULL DEFAULT 0,
    -- 4 sep, pedido de Braulio: "tabla de consumo de combustible" por
    -- ruta (en galones) — se muestra como referencia al liquidar el
    -- anticipo de viáticos de un viaje en esa ruta, para compararla contra
    -- el combustible real que registre el liquidador (ver
    -- expense_advances.fuel_actual / fuel_excess / fuel_notes abajo).
    default_fuel_amount REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(origin, destination)
);

-- Anticipo de viáticos entregado al conductor para un viaje, y su
-- liquidación posterior (comparación contra los gastos reales registrados
-- para ese viaje).
CREATE TABLE IF NOT EXISTS expense_advances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    route_id INTEGER REFERENCES routes(id),
    amount_given REAL NOT NULL,
    given_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'LIQUIDADO')),
    liquidated_at TEXT,
    liquidated_expenses_total REAL,
    -- Oficina donde se hace la liquidación (ver app/accounting.py
    -- OFFICES) y correlativo de liquidación de ese anticipo dentro de esa
    -- oficina y ese mes (reinicia en 1 cada mes — ver `_next_voucher_number`
    -- en app/routes/viaticos.py). Ambos se asignan recién al liquidar, no
    -- al crear el anticipo.
    office TEXT,
    voucher_number INTEGER,
    notes TEXT,
    -- 4 sep, pedido de Braulio: consumo de combustible de este viaje,
    -- comparado contra la tabla de consumo estimado de la ruta
    -- (routes.default_fuel_amount). fuel_actual ("combustible físico") es
    -- lo que registra el liquidador; fuel_notes son sus observaciones. Los
    -- tres NULL hasta que se registre combustible para esta liquidación.
    -- fuel_excess ("exceso") — 10 sep, pedido de Braulio: DESDE esa fecha
    -- ya no se digita a mano (antes sí, 4-9 sep) — se calcula solo en
    -- save_fuel() como fuel_actual menos routes.default_fuel_amount (nunca
    -- negativo; NULL si no hay ruta/estimado con qué compararlo).
    fuel_actual REAL,
    fuel_excess REAL,
    fuel_notes TEXT,
    -- 10 sep, pedido de Braulio: "en el caso que el exceso sea mayor a 0,
    -- a la derecha que diga ajuste y sea un cuadro que solo... el
    -- administrador pueda editar y poner el OK para enviar a RRHH" — nota
    -- libre y opcional del Administrador (motivo, descuento a aplicar,
    -- etc.), capturada junto con la aprobación de RRHH — ver
    -- rrhh_approve() en app/routes/liquidaciones.py. Solo tiene sentido
    -- cuando fuel_excess > 0 (si no hay exceso no hace falta ajustar
    -- nada), pero no se restringe a nivel de base de datos.
    fuel_adjustment TEXT,
    -- 4 sep, pedido de Braulio: código correlativo de la liquidación según
    -- la empresa operadora del viaje (trips.issuer) — B-0001, B-0002... si
    -- es BRMS, H-0001, H-0002... si es Harraso. Se asigna al crear el
    -- anticipo (a diferencia del voucher_number contable, que se asigna
    -- recién al liquidar) y cada empresa lleva su propio correlativo, que
    -- nunca se reinicia — ver `_next_liquidation_code` en
    -- app/routes/liquidaciones.py.
    code TEXT,
    -- 9 sep, pedido de Braulio: "una vez que se cierren [las liquidaciones]
    -- tienen que estar listas para ser enviadas a RRHH, pero para esto
    -- solo el administrador puede dar el OK final luego de ver el consumo
    -- de combustible para los casos de excesos". Es una aprobación
    -- SEPARADA del cierre (status = 'LIQUIDADO') — mismo patrón que
    -- inventory_purchases.authorized_at/authorized_by_* (autorización de
    -- una orden de compra, exclusiva de Administrador, chequeada por ROL
    -- directamente en el código y no por el permiso "edit" del módulo, ya
    -- que Contabilidad también tiene "edit" sobre Liquidaciones pero no
    -- debe poder dar este OK). NULL hasta que un Administrador la apruebe;
    -- ver rrhh_approve() en app/routes/liquidaciones.py.
    rrhh_approved_at TEXT,
    rrhh_approved_by_name TEXT,
    rrhh_approved_by_user_id INTEGER REFERENCES users(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Catálogo de grifos (10 sep, 3ra ronda, pedido de Braulio: "dentro de
-- catalogos hay que poner los grifos, en este se registren de acuerdo a su
-- ciudad razon social y ruc, en la pantalla de liquidaciones se eligan los
-- que estan registrados"). Administrado desde Catálogos → Grifos (ver
-- app/routes/catalogos.py) — solo Administrador, igual que el resto de
-- Catálogos. Se elige un grifo registrado (no texto libre) tanto al
-- registrar un gasto con concepto "Combustible" como al cargar una entrada
-- rápida en el panel de la liquidación — ver expenses.fuel_station_id y
-- fuel_entries.fuel_station_id más abajo.
CREATE TABLE IF NOT EXISTS fuel_stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    business_name TEXT NOT NULL,
    ruc TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Consumo de combustible cargado a mano directamente en el panel de la
-- liquidación (10 sep, 2da ronda, pedido de Braulio: "en la parte de
-- combustible sea para agregar porque pueden ser varios por ruta... ciudad,
-- nombre del grifo, nro vale/factura, cant galones, precio unit y precio
-- total"). Es un registro rápido SIN comprobante/foto (si el gasto sí tiene
-- comprobante, se registra como un gasto normal con concepto "Combustible"
-- en app/routes/liquidaciones.py `new_expense()`, y esas filas se listan
-- acá también, mezcladas con estas — ver _fuel_rows()). El total de galones
-- de ambas fuentes es lo que alimenta expense_advances.fuel_actual — ver
-- _recalc_fuel_actual(). fuel_station_id (10 sep, 3ra ronda) amarra la fila
-- al grifo elegido del catálogo; city/station_name se guardan igual, como
-- "foto" de ese momento (ver el mismo comentario en expenses más arriba).
CREATE TABLE IF NOT EXISTS fuel_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    advance_id INTEGER NOT NULL REFERENCES expense_advances(id),
    fuel_station_id INTEGER REFERENCES fuel_stations(id),
    city TEXT,
    station_name TEXT,
    document_number TEXT,
    gallons REAL NOT NULL DEFAULT 0,
    unit_price REAL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cada entrega de dinero al conductor dentro de una misma liquidación
-- (pedido de Braulio, 28 ago: a veces se da un anticipo al inicio del
-- viaje y otro a mitad de camino). `expense_advances.amount_given` sigue
-- existiendo y sigue siendo el total — se recalcula como la suma de estas
-- filas cada vez que se agrega o elimina una, así el resto del sistema
-- (Resumen contable, exports, la comparación contra lo gastado) no tiene
-- que cambiar: sigue leyendo `amount_given` como el monto total entregado.
CREATE TABLE IF NOT EXISTS advance_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    advance_id INTEGER NOT NULL REFERENCES expense_advances(id),
    amount REAL NOT NULL,
    payment_date TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Conceptos de gasto para el export contable de liquidación: cada uno
-- amarra un nombre visible (glosa) a su cuenta contable, tipo de
-- comprobante y código de documento SUNAT, según la plantilla real de
-- liquidación de Harraso (ver app/accounting.py). Los conceptos con
-- document_type_code = 'PL' (vale / por liquidar) son de uso interno del
-- sistema, uno por oficina — no aparecen en el desplegable del formulario
-- de gastos; se usan solo para generar la fila "Haber" del anticipo al
-- exportar la liquidación.
CREATE TABLE IF NOT EXISTS expense_concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    account_code TEXT NOT NULL,
    voucher_type_label TEXT NOT NULL,
    document_type_code TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Caché del tipo de cambio SUNAT por fecha (ver
-- app/integrations/sunat_exchange_rate.py), para no consultar el servicio
-- externo más de una vez por día y para que el dato quede disponible
-- aunque el servicio esté caído más adelante.
CREATE TABLE IF NOT EXISTS sunat_exchange_rates (
    rate_date TEXT PRIMARY KEY,
    buy_rate REAL,
    sell_rate REAL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Caché de la consulta de RUC (ver app/integrations/sunat_ruc.py), para
-- autocompletar la razón social del proveedor al registrar un gasto sin
-- volver a golpear el servicio externo cada vez que se repite un RUC (el
-- mismo grifo, el mismo taller, etc.).
CREATE TABLE IF NOT EXISTS sunat_ruc_cache (
    ruc TEXT PRIMARY KEY,
    razon_social TEXT,
    estado TEXT,
    condicion TEXT,
    direccion TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Catálogos editables por el administrador (conceptos de mantenimiento,
-- tipos de gasto, etc.) sin necesidad de tocar código.
CREATE TABLE IF NOT EXISTS catalog_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(category, name)
);

-- Ajustes generales de la aplicación, clave/valor (ej. el costo de mano de
-- obra por minuto que usa Mantenimiento para calcular el costo de una
-- orden). Se administra desde Catálogos.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Última ubicación conocida de cada unidad (alimentada por la integración
-- con el proveedor de GPS, ver app/integrations/frotcom.py).
CREATE TABLE IF NOT EXISTS vehicle_locations (
    vehicle_id INTEGER PRIMARY KEY REFERENCES vehicles(id),
    latitude REAL,
    longitude REAL,
    speed_kmh REAL,
    heading REAL,
    odometer_km REAL,
    recorded_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Historial de posiciones de GPS (31 ago, pedido de Braulio: horas manejadas
-- por día y reportes diarios de km/horas). A diferencia de vehicle_locations
-- (una sola fila por unidad, se sobrescribe), acá se guarda una fila por
-- cada sincronización con Frotcom, para poder calcular cuánto se movió cada
-- unidad en un rango de tiempo (ej. "hoy"). Alimentada tanto por el botón
-- manual "Sincronizar" como por la sincronización automática en segundo
-- plano cada 2 minutos (ver app/scheduler.py).
CREATE TABLE IF NOT EXISTS vehicle_location_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    latitude REAL,
    longitude REAL,
    speed_kmh REAL,
    odometer_km REAL,
    recorded_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vehicle_location_history_vehicle_time
    ON vehicle_location_history(vehicle_id, created_at);

-- Viajes de Frotcom (31 ago) — a diferencia de vehicle_location_history
-- (posiciones sueltas, calculamos nosotros horas/km con un estimado), acá
-- se guardan los viajes YA calculados por Frotcom (GET
-- /v2/vehicles/{id}/trips — ver app/integrations/frotcom.py), con tiempo de
-- manejo y kilometraje exactos, y origen/destino de cada viaje. Se llena
-- con el botón "Traer historial" (bajo demanda, no automático cada 2
-- minutos — ver nota de "rate limit" en frotcom.py) y sirve tanto para
-- rellenar reportes de días anteriores como, más adelante, para el reporte
-- de cumplimiento de hoja de ruta que pidió Braulio.
CREATE TABLE IF NOT EXISTS vehicle_trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    frotcom_trip_id TEXT NOT NULL UNIQUE,
    started_at TEXT,
    ended_at TEXT,
    start_place TEXT,
    start_address TEXT,
    start_latitude REAL,
    start_longitude REAL,
    start_odometer_km REAL,
    end_place TEXT,
    end_address TEXT,
    end_latitude REAL,
    end_longitude REAL,
    end_odometer_km REAL,
    driver_name TEXT,
    drive_time_sec INTEGER,
    trip_duration_sec INTEGER,
    mileage_km REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vehicle_trips_vehicle_started
    ON vehicle_trips(vehicle_id, started_at);

-- Progreso de una importación de historial de viajes en curso (31 ago) —
-- como puede tardar varios minutos (varias unidades x varios tramos de 7
-- días), corre en un hilo de segundo plano (igual que app/scheduler.py) en
-- vez de bloquear la página; esta tabla es lo que permite mostrarle a
-- Braulio el avance sin que tenga que quedarse con la pestaña abierta.
CREATE TABLE IF NOT EXISTS frotcom_trip_import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDIENTE',
    vehicles_total INTEGER NOT NULL DEFAULT 0,
    vehicles_done INTEGER NOT NULL DEFAULT 0,
    trips_imported INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    -- Primer error real que devolvió la API de Frotcom durante esta
    -- importación (si hubo alguno) — distinto de error_message, que solo
    -- se llena si el job entero se cae. Un job puede terminar
    -- "COMPLETADO" con 0 viajes importados porque CADA llamada a Frotcom
    -- falló (fecha con formato rechazado, límite de llamadas, etc.) sin
    -- que eso tumbe el job completo (se seguía intentando con el resto de
    -- unidades) — sin esto, esa causa quedaba invisible salvo en logs de
    -- Render (31 ago, tras el primer intento real de Braulio: 46/46
    -- unidades, 0 viajes, sin pista de por qué).
    sample_error TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    issue_date TEXT NOT NULL,
    due_date TEXT,
    amount REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'PAGADA', 'VENCIDA', 'ANULADA')),
    -- 7 sep, integración con tefacturo.pe: empresa emisora (Harraso o BRMS,
    -- cada una con su propio RUC) — se guarda al crear la factura a partir
    -- de los viajes elegidos (todos deben tener el mismo issuer, ver
    -- app/routes/facturacion.py) y no se recalcula después, mismo criterio
    -- que quotations.issuer/trips.issuer.
    issuer TEXT NOT NULL DEFAULT 'HARRASO' CHECK (issuer IN ('HARRASO', 'BRMS')),
    notes TEXT,
    -- Serie/número correlativo exigido por SUNAT para el comprobante
    -- electrónico (distinto del código interno "number" de arriba).
    series TEXT NOT NULL DEFAULT 'F001',
    series_number INTEGER NOT NULL DEFAULT 0,
    sunat_status TEXT NOT NULL DEFAULT 'NO_ENVIADA' CHECK (sunat_status IN ('NO_ENVIADA', 'ACEPTADO', 'RECHAZADO', 'ERROR')),
    sunat_message TEXT,
    sunat_pdf_url TEXT,
    -- Nombre del archivo PDF real devuelto por tefacturo.pe al emitir la
    -- factura (7 sep, segunda ronda), guardado vía app/storage.py —
    -- sunat_pdf_url pasa a apuntar a la ruta interna que sirve este archivo.
    sunat_pdf_filename TEXT,
    sunat_xml_url TEXT,
    sunat_cdr_url TEXT,
    sunat_sent_at TEXT,
    -- Detracción (SPOT) — 9 sep, Braulio compartió una factura real ya
    -- emitida (fuera de este ERP) que incluye el bloque "Concepto de
    -- Detracción": el servicio de transporte de bienes por vía terrestre
    -- (código de bien 027 del catálogo SUNAT) está sujeto a detracción del
    -- 4% cuando el importe de la operación supera S/ 400 — confirmado
    -- contra la orientación oficial de SUNAT
    -- (orientacion.sunat.gob.pe/detracciones-en-el-transporte-de-bienes-por-via-terrestre)
    -- y contra la factura real (S/708 * 4% = S/28.32, coincide exacto). Se
    -- calcula y guarda al crear la factura (ver app/routes/facturacion.py)
    -- porque Harraso/BRMS solo prestan este único servicio — no se
    -- recalcula después, mismo criterio que "issuer". IMPORTANTE: por
    -- ahora esto es informativo dentro del ERP — el envío a tefacturo.pe
    -- (ver app/integrations/sunat_ose.py) todavía NO manda estos datos en
    -- el JSON de la factura porque su documentación pública no especifica
    -- el campo correspondiente (se le pidió a Braulio consultarlo con el
    -- soporte de tefacturo.pe).
    detraction_applies INTEGER NOT NULL DEFAULT 0,
    detraction_code TEXT,
    detraction_percentage REAL,
    detraction_amount REAL,
    detraction_bank_account TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    description TEXT,
    amount REAL NOT NULL DEFAULT 0
);

-- Guías de remisión electrónicas — modalidad "Transportista" (la empresa
-- de transporte traslada carga de un cliente y debe sustentar el traslado
-- ante SUNAT). Una guía se genera a partir de un viaje.
CREATE TABLE IF NOT EXISTS waybills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    -- La serie debe empezar con "V" para tefacturo.pe (guía transportista,
    -- confirmado 7 sep, segunda ronda) — antes se usaba "T001" por
    -- suposición propia.
    series TEXT NOT NULL DEFAULT 'V001',
    series_number INTEGER NOT NULL DEFAULT 0,
    -- 7 sep, integración con tefacturo.pe: empresa emisora (Harraso o BRMS),
    -- copiada del viaje (trips.issuer) al crear la guía — no se recalcula
    -- después, mismo criterio que invoices.issuer arriba.
    issuer TEXT NOT NULL DEFAULT 'HARRASO' CHECK (issuer IN ('HARRASO', 'BRMS')),
    issue_date TEXT NOT NULL,
    -- 14 sep, patch 0029: fecha de entrega (fechaEntrega) — campo real de
    -- tefacturo.pe, ver el comentario largo en build_waybill_payload().
    -- Opcional: si se deja vacía, se usa issue_date.
    delivery_date TEXT,
    weight_kg REAL,
    packages INTEGER,
    origin_address TEXT,
    destination_address TEXT,
    -- 7 sep, segunda ronda: ubigeo (código INEI de 6 dígitos) de partida y
    -- llegada, exigidos por SUNAT en la guía transportista — el sistema no
    -- tiene un catálogo de ubigeos, se llenan a mano en el formulario.
    origin_ubigeo TEXT,
    destination_ubigeo TEXT,
    -- Motivo de traslado (catálogo SUNAT: VENTA/DEVOLUCION/COMPRA/
    -- TRASLADO_ESTABLECIMINTOS/CONSIGNACION/EXPORTACION/IMPORTACION/OTROS)
    -- — exigido por tefacturo.pe, default OTROS porque Harraso/BRMS no
    -- necesariamente conocen el motivo comercial de la carga del cliente.
    transfer_reason TEXT NOT NULL DEFAULT 'OTROS',
    vehicle_plate TEXT,
    -- 15 sep, pedido de Braulio (una guía real aceptada trae un bloque
    -- "VEHICULO Y CONDUCTOR SECUNDARIO" con placa + tarjeta de circulación
    -- de la carreta): placa de la carreta/semirremolque, para mandarla
    -- como segundo elemento de "vehiculos" en el JSON de tefacturo.pe. Se
    -- precarga desde trips.trailer_vehicle_id al crear la guía, editable
    -- igual que vehicle_plate/driver_* -- ver build_waybill_payload().
    trailer_plate TEXT,
    driver_document TEXT,
    driver_name TEXT,
    driver_license TEXT,
    -- 15 sep, pedido de Braulio ("hay que especificar remitente,
    -- destinatario, subcontratado, pagador"): una guía real aceptada trae
    -- bloques separados de DESTINATARIO, SUBCONTRATADO y PAGADOR con su
    -- propio RUC/razón social -- no siempre coinciden con el remitente
    -- (cliente del viaje). remitente sigue siendo el cliente del viaje (sin
    -- cambios); estos campos son opcionales, editables al crear la guía
    -- (igual que vehicle_plate/driver_*) -- ver la nota larga en
    -- build_waybill_payload() sobre qué tan confirmado está cada uno contra
    -- el servidor real de tefacturo.pe (destinatario sí, subcontratado y
    -- pagador NO todavía).
    recipient_ruc TEXT,
    recipient_name TEXT,
    subcontractor_ruc TEXT,
    subcontractor_name TEXT,
    payer_type TEXT NOT NULL DEFAULT 'DESTINATARIO' CHECK (payer_type IN ('REMITENTE', 'DESTINATARIO', 'TERCERO')),
    payer_ruc TEXT,
    payer_name TEXT,
    -- 17 sep, pedido de Braulio: "documento asociado" -- como en el portal
    -- de SUNAT, donde se referencia la factura/boleta o la guía de
    -- remisión del REMITENTE (el cliente) que sustenta el traslado.
    -- related_document_type: NULL/'FACTURA'/'BOLETA'/'GUIA_REMITENTE'/'OTRO'.
    -- related_document_number es texto libre (ej. "F001-000123") porque
    -- puede venir de un documento externo (del cliente) que este sistema
    -- no tiene registrado. Si el viaje ya tiene una factura propia
    -- (invoice_items) o una guía del remitente (trips.shipper_waybill_*),
    -- el formulario los sugiere solos (ver app/routes/guias.py) -- pero
    -- siempre son editables a mano.
    -- IMPORTANTE: por ahora esto es solo informativo dentro del ERP (se
    -- guarda y se muestra) -- todavía NO se manda a tefacturo.pe en
    -- build_waybill_payload() porque su documentación/JSON confirmado para
    -- la guía TRANSPORTISTA no incluye este campo (mismo criterio que la
    -- detracción en `invoices`, ver ese comentario) -- hace falta
    -- confirmar con su soporte técnico el nombre real del campo antes de
    -- agregarlo al envío real.
    related_document_type TEXT,
    related_document_number TEXT,
    notes TEXT,
    sunat_status TEXT NOT NULL DEFAULT 'NO_ENVIADA' CHECK (sunat_status IN ('NO_ENVIADA', 'ACEPTADO', 'RECHAZADO', 'ERROR')),
    sunat_message TEXT,
    sunat_pdf_url TEXT,
    -- Nombre del archivo PDF real devuelto por tefacturo.pe al emitir la
    -- guía (7 sep, segunda ronda), guardado vía app/storage.py — sunat_pdf_url
    -- pasa a apuntar a la ruta interna que sirve este archivo.
    sunat_pdf_filename TEXT,
    sunat_xml_url TEXT,
    sunat_cdr_url TEXT,
    sunat_sent_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cotizaciones (1 sep) — documento comercial que se le envía a un cliente
-- antes de un viaje/servicio, con el mismo formato de columnas/totales que
-- ya usa Harraso en sus cotizaciones reales (Gravado/Exonerado/Inafecto/
-- IGV/Descuentos/Otros Cargos). Independiente de Viajes/Facturación por
-- ahora (decisión de Braulio, 1 sep) — solo genera el PDF para enviar.
CREATE TABLE IF NOT EXISTS quotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number INTEGER NOT NULL UNIQUE,
    client_id INTEGER REFERENCES clients(id),
    -- Copia del cliente al momento de crear la cotización (mismo criterio
    -- que inventory_purchases.provider_name) — permite corregir el dato a
    -- mano para esta cotización puntual sin tocar el catálogo de Clientes,
    -- y conserva el dato aunque el cliente se desactive después.
    client_name TEXT NOT NULL,
    client_ruc TEXT,
    client_address TEXT,
    -- Empresa que emite la cotización (1 sep, pedido de Braulio: "la
    -- cotizacion debes poder elegir entre Harraso o BRMS que sea la
    -- empresa que cotice ( ya que son las 2 )") — define qué RUC/dirección/
    -- cuenta bancaria se muestran en el PDF (ver app/routes/cotizaciones.py
    -- → pdf()). Se guarda por cotización (no se recalcula) para que el
    -- documento ya emitido no cambie si algún día se edita la config.
    issuer TEXT NOT NULL DEFAULT 'HARRASO' CHECK (issuer IN ('HARRASO', 'BRMS')),
    issue_date TEXT NOT NULL,
    due_date TEXT,
    currency TEXT NOT NULL DEFAULT 'SOLES',
    payment_method TEXT,
    payment_condition TEXT,
    observation TEXT,
    discount_total REAL NOT NULL DEFAULT 0,
    other_charges_total REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'BORRADOR' CHECK (status IN ('BORRADOR', 'ENVIADA', 'ACEPTADA', 'RECHAZADA')),
    created_by_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quotation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id INTEGER NOT NULL REFERENCES quotations(id),
    sort_order INTEGER NOT NULL DEFAULT 0,
    code TEXT,
    description TEXT NOT NULL,
    unit TEXT,
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0,
    unit_discount REAL NOT NULL DEFAULT 0,
    -- Tratamiento tributario de la línea (18% IGV solo sobre lo Gravado) —
    -- la mayoría de líneas de Harraso son Gravado, pero se deja por línea
    -- (no fijo para toda la cotización) para que el total Exonerado/
    -- Inafecto salga bien si algún día hace falta.
    tax_treatment TEXT NOT NULL DEFAULT 'GRAVADO' CHECK (tax_treatment IN ('GRAVADO', 'EXONERADO', 'INAFECTO'))
);
CREATE INDEX IF NOT EXISTS idx_quotation_items_quotation ON quotation_items(quotation_id);

-- Borradores de gasto extraídos automáticamente de una foto de factura
-- recibida por WhatsApp (1 sep, pedido de Braulio: "quiero usar n8n para
-- integrar Whatsapp... que tomando una foto a la factura se llene
-- automaticamente los campos"). Un workflow de n8n (fuera de este
-- repositorio — ver n8n/whatsapp-factura-intake.json) recibe la foto por
-- WhatsApp Business API, la manda a un modelo de IA con visión para
-- extraer los datos, y llama a POST /liquidaciones/whatsapp/intake (ver
-- app/routes/liquidaciones.py) con esos datos + la imagen.
--
-- A propósito NO se escribe directo en `expenses`: la extracción por IA
-- puede equivocarse (monto, RUC) y esto es dinero real, así que cada foto
-- entra aquí como borrador PENDIENTE y un Administrador u Operador
-- (mismo permiso "liquidaciones"/"edit" que ya existe, sin permiso nuevo)
-- lo revisa/corrige y recién ahí lo aprueba, creando la fila real en
-- `expenses` (queda enlazada en resulting_expense_id) — o lo rechaza si la
-- foto no corresponde a un gasto válido.
CREATE TABLE IF NOT EXISTS whatsapp_expense_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'APROBADO', 'RECHAZADO')),
    -- Número de WhatsApp que mandó la foto, y el id del mensaje original
    -- (lo manda n8n) — este último sirve para deduplicar: si n8n reintenta
    -- la llamada (por un timeout de red, por ejemplo) no se crea un
    -- segundo borrador para la misma foto. Puede quedar NULL si el
    -- workflow no lo manda; UNIQUE permite varios NULL sin problema.
    source_phone TEXT,
    source_wa_message_id TEXT UNIQUE,
    -- Nombre de archivo de la foto de la factura, guardada con el mismo
    -- mecanismo que los comprobantes de gastos (ver app/storage.py). Al
    -- aprobar el borrador, este mismo archivo pasa a ser el comprobante
    -- del gasto real (expenses.receipt_filename) — no se duplica.
    image_filename TEXT NOT NULL,
    -- Datos que extrajo la IA — se muestran precargados (pero editables)
    -- en la pantalla de revisión; nada de esto se copia a `expenses` hasta
    -- que un humano aprueba el borrador.
    extracted_provider_ruc TEXT,
    extracted_provider_name TEXT,
    extracted_amount REAL,
    extracted_currency TEXT,
    extracted_document_number TEXT,
    extracted_document_date TEXT,
    -- Respuesta cruda del modelo de IA, tal cual la mandó n8n — solo para
    -- auditoría/depuración si algún día hay que revisar por qué extrajo
    -- mal un dato; no se usa para nada funcional.
    ai_raw_response TEXT,
    -- Texto que haya escrito el conductor/usuario junto con la foto en
    -- WhatsApp (el "caption" del mensaje), si lo hay.
    caption TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    rejection_reason TEXT,
    resulting_expense_id INTEGER REFERENCES expenses(id)
);
CREATE INDEX IF NOT EXISTS idx_whatsapp_expense_drafts_status ON whatsapp_expense_drafts(status);

-- Módulo "Pagos personal" (18 sep, pedido de Braulio: "creemos un modulo
-- mas que se llame Pagos personal. En este modulo vamos a subir los
-- comprobantes de pagos del personal tanto de planilla como recibo de
-- honorario. Quiero que haya la opcion de que cree un excel con los
-- formatos que usa la plataforma Telecredito de BCP.") -- ver
-- app/routes/pagos_personal.py.
--
-- Catálogo de personal (administrativos, choferes, etc.) al que se le
-- registran pagos. driver_id (opcional) enlaza esta fila con su registro
-- ya existente en Conductores, para los choferes que también reciben
-- pagos por este módulo -- así no se duplican sus datos en dos catálogos
-- (pedido explícito de Braulio al elegir entre las opciones que se le
-- dieron). Los datos bancarios (banco/cuenta/CCI) son los que se usan
-- luego para armar el archivo de Telecrédito.
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    document_type TEXT NOT NULL DEFAULT 'DNI' CHECK (document_type IN ('DNI', 'CE', 'RUC')),
    document_number TEXT,
    position TEXT,
    -- Sin "REFERENCES drivers(id)" aquí a propósito, mismo motivo que en
    -- otras columnas de este esquema (ver nota junto a mechanic_id en
    -- maintenance_record_jobs, más arriba): la FK la agrega el paso
    -- dedicado de init_db() (Postgres) una vez que la columna ya existe.
    -- No aplica en este caso porque la tabla es nueva (se crea con la FK
    -- declarada tal cual, sin pasar por ALTER TABLE), se deja la nota
    -- igual por si algún día se necesita agregar otra columna así acá.
    driver_id INTEGER REFERENCES drivers(id),
    -- 18 sep, 2da ronda (archivo real de Telecrédito de Braulio: los pagos
    -- de esta persona se hacen desde la cuenta de OTRA empresa del grupo,
    -- ej. "pagamos la planilla a traves de nosotros" -- Harraso/BRMS pagan
    -- por una empresa hermana). Va en el archivo de Telecrédito como
    -- "Referencia para el beneficiario" -- si queda vacío, se usa el
    -- nombre de la empresa dueña de la cuenta de cargo elegida al generar
    -- el archivo (ver app/telecredito.py). NO es la empresa que paga, es
    -- la que figura como empleadora de esta persona.
    company TEXT,
    bank_name TEXT,
    account_number TEXT,
    -- Tipo de la cuenta de ABONO (para el archivo de Telecrédito) --
    -- Ahorros por defecto porque es lo que usa Braulio para todo su
    -- personal en el archivo real que mandó de ejemplo. Solo aplica
    -- cuando se paga con account_number; si hay CCI, el tipo de cuenta es
    -- siempre "Interbancaria" sin importar este campo (ver
    -- app/telecredito.py, abono_bank_fields()).
    account_type TEXT NOT NULL DEFAULT 'AHORROS' CHECK (account_type IN ('AHORROS', 'CORRIENTE', 'MAESTRA')),
    -- Código de Cuenta Interbancario (20 dígitos) -- con esto se puede
    -- pagar a una cuenta de cualquier banco, no solo BCP.
    cci TEXT,
    currency TEXT NOT NULL DEFAULT 'S' CHECK (currency IN ('S', 'D')),
    status TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (status IN ('ACTIVO', 'INACTIVO')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Un comprobante de pago (boleta de planilla o recibo por honorarios) por
-- persona y periodo. payment_type se elige POR COMPROBANTE, no queda fijo
-- por persona (pedido explícito de Braulio: "puede variar pago a pago,
-- pero la planilla y honorarios son distintas nunca van a estar mezcladas
-- en el mismo archivo de pago") -- por eso el export a Telecrédito
-- (pagos_personal.export_telecredito) siempre filtra por un solo
-- payment_type a la vez, nunca junta los dos en un mismo archivo.
-- exported_at (18 sep) es solo informativo -- queda marcado cuando ese
-- pago ya se incluyó en un archivo de Telecrédito generado, para poder
-- avisar en pantalla "ya se exportó" sin impedir volver a exportarlo si
-- hace falta (ej. si el banco rechazó el lote).
CREATE TABLE IF NOT EXISTS staff_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    payment_type TEXT NOT NULL CHECK (payment_type IN ('PLANILLA', 'RECIBO_HONORARIOS')),
    period TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    concept TEXT,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'PAGADO')),
    payment_date TEXT,
    -- Comprobante subido (boleta de pago o recibo por honorarios) --
    -- mismo mecanismo de almacenamiento que el resto de archivos del
    -- sistema (ver app/storage.py, save_staff_payment_receipt()).
    receipt_filename TEXT,
    -- N° del recibo por honorarios emitido por la persona ese mes (texto
    -- libre, no un archivo) -- distinto cada mes, así que va por pago y no
    -- en la plantilla reusable (ver COLUMN_MIGRATIONS en app/db.py).
    receipt_number TEXT,
    -- Constancia del banco enlazada a mano (19 sep) -- ver la nota grande
    -- junto a esta misma columna en COLUMN_MIGRATIONS (app/db.py). Un
    -- pago pasa a PAGADO recién cuando se le enlaza una constancia.
    payment_voucher_id INTEGER REFERENCES payment_vouchers(id),
    exported_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staff_payments_staff ON staff_payments(staff_id);
CREATE INDEX IF NOT EXISTS idx_staff_payments_period ON staff_payments(period);

-- 18 sep, 6ta ronda (pedido de Braulio: "tengo un excel con nombres y
-- numeros de cuenta que quiero que sea la plantilla default con montos
-- que se paga cada mes de recibos por honorario, quiero que cada mes se
-- use esta por default y se editen los montos, agreguen o borren
-- personas") -- lista de personas (siempre de RECIBO_HONORARIOS, no
-- aplica a Planilla) que se ofrecen por defecto cada mes al generar el
-- lote de honorarios, cada una con su monto por defecto (editable ese mes
-- sin afectar el default guardado acá) -- ver
-- honorarios_plantilla()/honorarios_generar() en
-- app/routes/pagos_personal.py. "active" permite sacar a alguien de la
-- oferta mensual sin perder su monto guardado, por si vuelve más
-- adelante.
CREATE TABLE IF NOT EXISTS honorarios_template_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL UNIQUE REFERENCES staff(id),
    default_amount REAL NOT NULL DEFAULT 0,
    default_concept TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 18 sep (pedido de Braulio: "el usuario Gustavo Lopez puede entrar a
-- neumatico pero no puede agregar llantas al inventario, a pesar de tener
-- permiso... podemos ser mas especificos a la hora de dar accesos a los
-- usuarios?") -- hasta ahora el acceso solo salía del/los rol(es) del
-- usuario (PERMISSIONS en app/auth.py), "editar" un módulo era todo o
-- nada, y no había forma de darle o quitarle una acción puntual a UNA
-- persona sin afectar a todos los que comparten su rol.
--
-- Esta tabla guarda excepciones puntuales por usuario, por módulo y por
-- acción (ver app/permissions_catalog.py para el catálogo de acciones de
-- cada módulo -- Neumáticos, por ejemplo, ahora tiene "campo" e
-- "inventario" en vez de un solo "edit"). Si existe una fila para
-- (user_id, module, action), esa fila GANA por encima de lo que diga su
-- rol (allowed=1 permite aunque su rol no lo haga, allowed=0 bloquea
-- aunque su rol sí lo permita); si no existe fila, se usa el rol como
-- siempre. Se edita desde Usuarios > editar > "Permisos específicos" --
-- ver app/routes/usuarios.py (_current_overrides/_save_user_overrides) y
-- app/auth.py (can(), que ahora revisa esta tabla antes que el rol).
CREATE TABLE IF NOT EXISTS user_permission_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    module TEXT NOT NULL,
    action TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, module, action)
);
CREATE INDEX IF NOT EXISTS idx_user_permission_overrides_user ON user_permission_overrides(user_id);

-- 18 sep, 2da ronda (pedido de Braulio, al armar el archivo real de
-- Telecrédito: "Hay que crear archivos por empresa, Harraso y BRMS tienen
-- cuentas distintas. En el menu de catalogos pon la parte de bancos en la
-- cual yo pueda registrar las cuentas de cada empresa y estas se
-- seleccionen a la hora de crear el archivo y se llenen sus datos.") --
-- catálogo de "Catálogos > Bancos" (ver app/routes/catalogos.py
-- bancos_list/bancos_add/bancos_toggle, mismo patrón que fuel_stations
-- /Grifos). Cada fila es una cuenta de cargo real: al generar el archivo
-- de Telecrédito (app/routes/pagos_personal.py,
-- telecredito_configure/telecredito_generate) se elige una de acá y sus
-- datos completan la cabecera del archivo (ver app/telecredito.py).
-- account_type NO admite 'AHORROS' -- BCP exige que la cuenta de CARGO
-- sea Corriente o Maestra (a diferencia de la cuenta de ABONO de cada
-- persona en staff.account_type, que sí puede ser Ahorros).
CREATE TABLE IF NOT EXISTS company_bank_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    bank_name TEXT NOT NULL DEFAULT 'BCP',
    account_type TEXT NOT NULL DEFAULT 'CORRIENTE' CHECK (account_type IN ('CORRIENTE', 'MAESTRA')),
    currency TEXT NOT NULL DEFAULT 'S' CHECK (currency IN ('S', 'D')),
    account_number TEXT NOT NULL,
    alias TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 18 sep, 4ta ronda (pedido de Braulio: "quiero que el menu de Pagos
-- personal este agrupado por año y luego mes, y una vez que se entra a
-- cada mes pueda ver pdfs de constancias de pago antiguas, asi mismo para
-- los meses de ahora en adelante quiero poder subir las constancias que
-- me brindara cada banco.") -- una "constancia de pago" es el comprobante
-- que da el BANCO de que un lote de Telecrédito se procesó (distinto del
-- receipt_filename de staff_payments, que es la boleta/recibo de CADA
-- persona). Se sube por periodo (año-mes), opcionalmente ligada a la
-- cuenta de cargo (company_bank_accounts) y/o al tipo de pago (Planilla u
-- Honorarios) que generó el lote -- ambos opcionales porque un mismo
-- periodo puede tener varias constancias (una por banco/cuenta, o una por
-- tipo) o, para el historial viejo, ninguna clasificación puntual, solo
-- el PDF y una etiqueta libre. Se sirve con el mismo mecanismo de
-- almacenamiento que el resto del sistema (ver app/storage.py,
-- save_payment_voucher()).
CREATE TABLE IF NOT EXISTS payment_vouchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    bank_account_id INTEGER REFERENCES company_bank_accounts(id),
    payment_type TEXT CHECK (payment_type IS NULL OR payment_type IN ('PLANILLA', 'RECIBO_HONORARIOS')),
    label TEXT,
    filename TEXT NOT NULL,
    original_filename TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 18 sep, 5ta ronda (pedido de Braulio: "si se elimina un archivo desde
    -- el menu de constancias, hay manera que se envie primero a una
    -- papelera... y saber que usuario lo hizo?") -- "eliminar" una
    -- constancia ahora es un soft-delete: se marcan estas 2 columnas en vez
    -- de borrar la fila, así se puede restaurar y queda quién la mandó a la
    -- papelera. Ver constancias_delete()/constancias_restore() y
    -- constancias_papelera() en app/routes/pagos_personal.py.
    deleted_at TEXT,
    deleted_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_payment_vouchers_period ON payment_vouchers(period);

CREATE INDEX IF NOT EXISTS idx_trips_status ON trips(status);
CREATE INDEX IF NOT EXISTS idx_trips_client ON trips(client_id);
CREATE INDEX IF NOT EXISTS idx_expenses_trip ON expenses(trip_id);
CREATE INDEX IF NOT EXISTS idx_expenses_vehicle ON expenses(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_vehicle ON maintenance_records(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_waybills_trip ON waybills(trip_id);
CREATE INDEX IF NOT EXISTS idx_inspections_vehicle ON inspections(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_inspection_items_inspection ON inspection_items(inspection_id);
CREATE INDEX IF NOT EXISTS idx_expense_advances_trip ON expense_advances(trip_id);
CREATE INDEX IF NOT EXISTS idx_fuel_entries_advance ON fuel_entries(advance_id);
