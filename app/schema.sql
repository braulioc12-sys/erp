-- Esquema de base de datos para ERP de Transporte de Carga
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADMIN', 'OPERADOR')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    gps_external_id TEXT,
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
    -- capacitación, y DDS (Diálogo Diario de Seguridad). Se guarda la
    -- fecha del último realizado y cuándo vence cada uno.
    backus_driving_exam_date TEXT,
    backus_driving_exam_expiry TEXT,
    backus_training_date TEXT,
    backus_training_expiry TEXT,
    dds_date TEXT,
    dds_expiry TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (status IN ('ACTIVO', 'INACTIVO')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    driver_id INTEGER REFERENCES drivers(id),
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    cargo_description TEXT,
    cargo_weight_kg REAL,
    scheduled_date TEXT NOT NULL,
    delivered_date TEXT,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'EN_CURSO', 'ENTREGADO', 'CANCELADO')),
    rate REAL NOT NULL DEFAULT 0,
    -- Comisión que le corresponde al conductor por este viaje. Se sugiere
    -- automáticamente según el monto configurado para la ruta (origen-destino)
    -- en el catálogo de Rutas, pero se puede editar por viaje.
    driver_commission REAL NOT NULL DEFAULT 0,
    notes TEXT,
    invoiced INTEGER NOT NULL DEFAULT 0,
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
-- catálogo de Trabajos (nombre + activo/inactivo).
CREATE TABLE IF NOT EXISTS mechanics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
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
CREATE TABLE IF NOT EXISTS maintenance_record_jobs (
    maintenance_record_id INTEGER NOT NULL REFERENCES maintenance_records(id),
    job_type_id INTEGER REFERENCES maintenance_job_types(id),
    job_name TEXT NOT NULL,
    estimated_minutes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDIENTE',
    mechanic_id INTEGER REFERENCES mechanics(id),
    mechanic_name TEXT,
    completed_at TEXT,
    PRIMARY KEY (maintenance_record_id, job_name)
);

-- Neumáticos: una fila por cada llanta que ha pasado por una posición de
-- una unidad (tracto, carreta o camión). No se guarda un "kilometraje
-- acumulado" como número aparte: se calcula en el momento comparando el
-- kilometraje actual de la unidad (vehicles.current_km, que ya se actualiza
-- solo con el movimiento del vehículo vía Mantenimiento/Flota/GPS) contra
-- km_at_install. Así el acumulado siempre queda al día automáticamente.
-- Cuando se reemplaza una llanta, la fila vieja pasa a status='RETIRADO'
-- (con fecha/km de retiro) y se crea una fila nueva para esa posición —
-- esto conserva el historial completo de cada posición en el tiempo.
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
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tires_vehicle ON tires(vehicle_id);
-- Garantiza que no haya dos llantas "ACTIVO" a la vez en la misma posición
-- de la misma unidad (índice único parcial).
CREATE UNIQUE INDEX IF NOT EXISTS idx_tires_active_position
    ON tires(vehicle_id, position_code) WHERE status = 'ACTIVO';

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
    extra_value TEXT
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

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    issue_date TEXT NOT NULL,
    due_date TEXT,
    amount REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (status IN ('PENDIENTE', 'PAGADA', 'VENCIDA', 'ANULADA')),
    notes TEXT,
    -- Serie/número correlativo exigido por SUNAT para el comprobante
    -- electrónico (distinto del código interno "number" de arriba).
    series TEXT NOT NULL DEFAULT 'F001',
    series_number INTEGER NOT NULL DEFAULT 0,
    sunat_status TEXT NOT NULL DEFAULT 'NO_ENVIADA' CHECK (sunat_status IN ('NO_ENVIADA', 'ACEPTADO', 'RECHAZADO', 'ERROR')),
    sunat_message TEXT,
    sunat_pdf_url TEXT,
    sunat_xml_url TEXT,
    sunat_cdr_url TEXT,
    sunat_sent_at TEXT,
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
    series TEXT NOT NULL DEFAULT 'T001',
    series_number INTEGER NOT NULL DEFAULT 0,
    issue_date TEXT NOT NULL,
    weight_kg REAL,
    packages INTEGER,
    origin_address TEXT,
    destination_address TEXT,
    vehicle_plate TEXT,
    driver_document TEXT,
    driver_name TEXT,
    driver_license TEXT,
    notes TEXT,
    sunat_status TEXT NOT NULL DEFAULT 'NO_ENVIADA' CHECK (sunat_status IN ('NO_ENVIADA', 'ACEPTADO', 'RECHAZADO', 'ERROR')),
    sunat_message TEXT,
    sunat_pdf_url TEXT,
    sunat_xml_url TEXT,
    sunat_cdr_url TEXT,
    sunat_sent_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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
