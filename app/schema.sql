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

-- Qué trabajos (de maintenance_job_types) se realizaron en cada registro de
-- mantenimiento. Se guarda una copia del nombre y minutos estimados al
-- momento de seleccionarlos, para que el historial no cambie si luego se
-- edita el catálogo de trabajos.
CREATE TABLE IF NOT EXISTS maintenance_record_jobs (
    maintenance_record_id INTEGER NOT NULL REFERENCES maintenance_records(id),
    job_type_id INTEGER REFERENCES maintenance_job_types(id),
    job_name TEXT NOT NULL,
    estimated_minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (maintenance_record_id, job_name)
);

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
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS inspection_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id),
    item_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'FALLA', 'NA')),
    observation TEXT
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
-- el anticipo de gastos de viaje al conductor.
CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    default_expense_amount REAL NOT NULL DEFAULT 0,
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
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
