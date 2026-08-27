# ERP de Transporte de Carga — Harraso Transport

Sistema web para gestionar Harraso Transport (empresa de transporte de carga): viajes/órdenes de servicio, flota y conductores, clientes, gastos, mantenimiento y facturación, con usuarios y roles (Administrador / Operador).

## Marca

El logo de Harraso Transport / BRMS está en `app/static/img/` (`logo-lockup.png`, el logotipo horizontal usado en el login y el menú lateral; `logo-mark.png`, la marca circular "B" usada como base de los íconos de la app). Los íconos de la PWA (`app/static/icons/icon-192.png` y `icon-512.png`) y el manifest (`app/static/manifest.webmanifest`) ya están generados a partir de ese logo. `COMPANY_NAME` (variable de entorno, ver `.env.example`) controla el nombre que se muestra junto al logo y en el título de la pestaña del navegador — por defecto "Harraso Transport". Si el logo cambia, basta con reemplazar esos dos archivos PNG y regenerar los íconos con el mismo recorte/relleno.

## Stack técnico

- **Backend:** Python 3 + Flask (sin frameworks adicionales de por medio).
- **Base de datos:** SQLite (archivo único, sin servidor de base de datos que configurar). Fácil de migrar a PostgreSQL más adelante si el negocio crece.
- **Frontend:** plantillas Jinja2 renderizadas en el servidor + CSS propio (sin dependencias externas ni CDNs).
- **Autenticación:** sesiones de Flask (cookies firmadas) + contraseñas con hash (Werkzeug).

## Módulos incluidos

- **Panel:** indicadores clave (viajes activos, ingresos del mes, facturas por cobrar, unidades en mantenimiento) y alertas de licencias/mantenimientos próximos a vencer.
- **Viajes / Órdenes de servicio:** registro de viajes con cliente, unidad, conductor, ruta, carga, tarifa y **comisión del conductor**; flujo de estados Pendiente → En curso → Entregado (o Cancelado). Incluye un **reporte mensual de comisiones** por conductor y ruta — ver la sección dedicada más abajo.
- **Clientes:** datos de contacto y facturación.
- **Flota:** unidades (placa, capacidad, tipo — camión, tracto o carreta —, estado), con vencimiento de **SOAT** y **Revisión Técnica** (alertas en el Panel).
- **Conductores:** datos personales, vencimiento de brevete, y control de vencimientos de **examen médico ocupacional** y de los requisitos para operar con **Backus** (examen de manejo, capacitación, y DDS) — todos con alerta en el Panel.
- **Gastos:** combustible, peajes, viáticos, mantenimiento u otros, asociados a un viaje y/o unidad. Filtrable por tipo y rango de fechas, con exportación a Excel (`.xlsx`) agrupada por tipo, con subtotales y total general. Se puede adjuntar el comprobante (foto o PDF) a cada gasto, definir **presupuestos mensuales** por unidad o tipo (con alerta en el Panel al acercarse o superarse), y gestionar **anticipos de viáticos** por viaje con su liquidación — ver la sección "Gastos, presupuestos y viáticos" más abajo.
- **Rutas:** catálogo de rutas frecuentes con un monto de viáticos predeterminado (usado para sugerir el anticipo de gastos de cada viaje) y un monto de **comisión del conductor** predeterminado (usado para sugerir la comisión al registrar un viaje por esa ruta).
- **Mantenimiento:** historial de mantenimientos por unidad, costo, kilometraje registrado y próxima fecha/kilometraje. Los conceptos (tipos de mantenimiento) se administran desde Catálogos. Si indicas el kilometraje al registrar un mantenimiento, actualiza automáticamente el kilometraje actual de la unidad. Incluye un catálogo de **trabajos con tiempo estimado** (ej. cambio de aceite = 60 min) que se seleccionan al registrar un mantenimiento, y una vista de **historial y costos totales por unidad**.
- **Neumáticos:** módulo independiente para controlar la vida útil y posición de cada llanta de cada unidad, con un diagrama distinto según el tipo de unidad — ver la sección dedicada más abajo.
- **Inspecciones:** checklist de inspección de una unidad (llantas, frenos, luces, etc.) antes de salir o al llegar de un viaje; los ítems del checklist se administran desde Catálogos. Cada inspección se puede **imprimir o descargar como PDF** con el logo de la empresa en el encabezado.
- **Facturación:** genera facturas por cliente a partir de viajes entregados y aún no facturados; controla estado (pendiente, pagada, vencida, anulada); puede enviarse electrónicamente a SUNAT — ver la sección dedicada más abajo.
- **Guías de Remisión:** genera la guía de remisión electrónica ("modalidad Transportista") de un viaje, con los datos de traslado, vehículo y conductor; puede enviarse a SUNAT igual que las facturas.
- **Usuarios:** solo el Administrador puede crear usuarios y asignar el rol Administrador u Operador.
- **Catálogos** (solo Administrador): agrega o desactiva conceptos de mantenimiento y tipos de gasto desde la web, sin tocar código.
- **Ubicación GPS** (solo Administrador): integración con Frotcom para ver la última posición conocida de cada unidad — ver la sección dedicada más abajo.
- **App instalable (PWA):** el sistema se puede "instalar" desde el navegador del celular o la computadora (ícono propio, se abre como app). No requiere nada adicional de tu parte.

### Alertas del panel

El panel avisa (30 días antes de vencer, o si ya venció) sobre: vencimiento de **brevete**, **examen médico ocupacional** y los requisitos de **Backus** (examen de manejo, capacitación, DDS) de cada conductor; vencimiento de **SOAT** y **Revisión Técnica** de cada unidad; mantenimientos programados por fecha; y presupuestos de gastos cercanos o superados. Además, avisa cuando una unidad se acerca (o ya superó) su próximo mantenimiento **por kilometraje**, comparando el kilometraje actual de la unidad contra el kilometraje programado del mantenimiento — el umbral son 1000 km (ajustable en `KM_ALERT_THRESHOLD`, en `app/routes/mantenimiento.py`) — y cuando una llanta llega al 90% o más de su vida útil estimada (ver la sección de Neumáticos). El kilometraje actual de una unidad se actualiza de tres formas: editándolo manualmente en Flota, indicándolo al registrar un mantenimiento, o automáticamente vía la sincronización con Frotcom (si está configurada).

### Permisos por rol

| Módulo | Administrador | Operador |
|---|---|---|
| Panel | Ver | Ver |
| Viajes | Ver/Crear/Editar | Ver/Crear/Editar |
| Clientes | Ver/Crear/Editar | Ver/Crear/Editar |
| Flota | Ver/Crear/Editar | Solo ver |
| Conductores | Ver/Crear/Editar | Solo ver |
| Gastos (incl. presupuestos y viáticos) | Ver/Crear/Editar | Ver/Crear/Editar |
| Rutas | Ver/Crear/Editar | Solo ver |
| Mantenimiento (incl. trabajos y costos por unidad) | Ver/Crear/Editar | Solo ver |
| Neumáticos | Ver/Crear/Editar | Solo ver |
| Inspecciones | Ver/Crear/Editar | Ver/Crear/Editar |
| Facturación | Ver/Crear/Editar/Enviar a SUNAT | Sin acceso |
| Guías de Remisión | Ver/Crear/Editar/Enviar a SUNAT | Ver/Crear/Editar/Enviar a SUNAT |
| Usuarios | Ver/Crear/Editar | Sin acceso |
| Catálogos | Ver/Crear/Editar | Sin acceso |
| Ubicación GPS | Ver/Sincronizar | Sin acceso |

Esto se puede ajustar fácilmente editando el diccionario `PERMISSIONS` en `app/auth.py`.

## Mantenimiento: trabajos con tiempo estimado e historial por unidad

Desde **Mantenimiento → Trabajos y tiempos** se administra un catálogo de trabajos de mantenimiento con su tiempo estimado en minutos (ej. "Cambio de aceite" = 60 min, "Cambio de filtro de aire" = 30 min), con un botón para agregar trabajos nuevos que no estén en la lista. Al registrar un mantenimiento, se pueden marcar uno o más trabajos realizados y el sistema suma automáticamente el tiempo estimado total (útil para planificar cuánto tiempo va a estar la unidad en taller). Esto es independiente del "Concepto" de mantenimiento (que sigue viniendo del catálogo en Catálogos) — el concepto clasifica el registro, los trabajos estiman el tiempo.

**Mantenimiento → Historial y costos por unidad** muestra, por cada unidad, el número de mantenimientos, el costo total acumulado y la fecha del último, con un enlace para ver el historial completo de esa unidad.

## Viajes: comisión del conductor y reporte mensual

Cada viaje tiene, además de la tarifa, un campo de **comisión del conductor**. Al registrar o editar un viaje, si el campo se deja vacío, el sistema sugiere automáticamente el monto configurado para esa ruta (origen-destino exactos) en **Rutas** — si escribes el origen/destino de una ruta ya configurada, verás la sugerencia aparecer debajo del campo; si la dejas en blanco al guardar, se usa ese monto. El campo siempre se puede editar manualmente para un viaje puntual.

**Viajes → Comisiones por mes** muestra, para el mes seleccionado, cuántos viajes hizo cada conductor, a qué ruta, y el total de comisión correspondiente (agrupado por conductor y luego por ruta, con subtotal por conductor y total general). Los viajes cancelados no se cuentan. Se puede exportar el mismo reporte a Excel (`.xlsx`) con el botón "Exportar a Excel".

## Neumáticos: vida útil y posición por unidad

Desde el módulo **Neumáticos** (o el botón "Neumáticos" en cada fila de Flota) se controla cada llanta de cada unidad, identificada por su posición en un diagrama según el tipo de unidad (**Flota → editar unidad → "Tipo de unidad"**):

- **Tracto camión:** eje de dirección (2 llantas) + 2 ejes de tracción dobles (4 llantas cada uno) = 10 posiciones.
- **Carreta / semirremolque:** 3 ejes dobles (4 llantas cada uno) = 12 posiciones.
- **Camión (unidad simple):** eje de dirección (2 llantas) + eje trasero doble (4 llantas) = 6 posiciones.

Si tu configuración de ejes es distinta, se ajustan fácilmente en `app/tire_positions.py` (todo centralizado ahí).

Al hacer clic en una posición vacía del diagrama (o en su fila de la tabla) se registra una llanta nueva: marca, fecha de instalación, kilometraje de la unidad en ese momento, y vida útil estimada en km (por defecto 80,000, ajustable por llanta según la marca/modelo). **El kilometraje acumulado no se guarda como un número aparte — se calcula solo**, comparando el kilometraje actual de la unidad (que ya se actualiza al registrar un mantenimiento, editar la unidad en Flota, o sincronizar GPS) contra el kilometraje que tenía al instalarse esa llanta. Así el acumulado de cada llanta siempre está al día sin ningún paso adicional.

Cada llanta activa muestra un indicador de color según su % de vida útil consumida (verde por debajo de 80%, ámbar de 80% a 99%, rojo en 100% o más — con alerta en el Panel a partir de 90%). Desde el detalle de una llanta puedes **"Reemplazar"** (retira la actual y registra la nueva en un solo paso, conservando el historial) o **"Retirar sin reemplazar"** (deja la posición vacía). El historial completo de llantas retiradas por unidad queda disponible en la misma página del diagrama.

## Inspecciones de unidades

Desde el detalle de un viaje (o desde el menú **Inspecciones**) se puede registrar un checklist de inspección de la unidad — "antes de salir" o "al llegar". Hay dos formatos, según el tipo de unidad:

- **Camiones:** checklist genérico configurable — un ítem por fila (llantas, frenos, luces, niveles, extintor, etc.), marcando cada uno como OK, Falla o N/A, y una observación opcional. Los ítems se administran desde **Catálogos → Ítems de inspección** (solo Administrador).
- **Tractos y carretas:** al elegir una unidad de uno de estos dos tipos, el formulario cambia automáticamente al **Check List de Tracto** o al **Check List de Carreta**, cada uno calcado del formato físico que usa Harraso hoy:
  - **Check List de Tracto:** mismas secciones que el papel (Personal, Revisión de niveles, Sistema de admisión, Revisión general, Actividades, Tablero de control, Accesorios de seguridad) con sus mismas columnas de estado por sección (Bien/Mal, Normal/Falta, Completo/Falta, Normal/Obstruido), más código de llanta según posición (10 posiciones + repuesto), **kilometraje** (actualiza el kilometraje de la unidad, igual que en Mantenimiento) y **lugar** (Pucallpa/Tarapoto/Lima).
  - **Check List de Carreta:** una sola tabla de "Revisión general" (Bien/Mal) con los ítems propios de una carreta (muelles, kin pin y plancha, sistema eléctrico, freno, retráctil, suspensión, etc.), más código de llanta y **presión** según posición (12 posiciones + repuesto). No tiene kilometraje, porque una carreta no trae odómetro propio.
  - Ambos comparten el mismo **código correlativo** al guardar (ej. CL-0001), el mismo **operador**, y las firmas de Operador / Mantenimiento al pie. Quedan centralizados en `app/detailed_checklists.py` — si Harraso ajusta alguno de los formatos físicos, ese es el único archivo que hay que tocar. Los camiones seguirán usando el checklist genérico hasta que se comparta su propio formato.

**Imprimir / descargar como PDF:** en el detalle de cualquier inspección, el botón "🖨️ Imprimir / Descargar PDF" abre una vista de impresión aparte (calcada del formato correspondiente: genérico, tracto o carreta), con el logo de la empresa en el encabezado y espacios de firma. Desde ahí se usa el diálogo de impresión del propio navegador (Ctrl+P / el botón de la página) eligiendo "Guardar como PDF" como destino — no requiere ninguna librería adicional en el servidor ni descargas, funciona igual en celular y en computadora.

## Gastos: presupuestos y viáticos

- **Comprobantes:** al registrar un gasto puedes adjuntar una foto o un PDF del recibo/boleta, de dos formas: el campo "tomar foto ahora" abre la cámara directamente en el celular (usa el atributo estándar HTML `capture="environment"` — funciona en Chrome/Android y en Safari/iOS al agregar el sistema a la pantalla de inicio como PWA); el campo "o subir un archivo" abre el selector normal de galería/archivos, para elegir una foto ya tomada o un PDF. Solo se llena uno de los dos. El archivo se guarda en el servidor y aparece como enlace "Ver" en la lista de gastos. *Aviso:* en hosting gratuito con disco efímero (ver la sección de Render más abajo) estos archivos se pierden al reiniciar/redesplegar, igual que la base de datos SQLite — el mismo `AUTO_SEED_DEMO` no los recupera. Para un uso serio en producción, sube a un plan con disco persistente (ver esa sección).
- **Compresión automática de fotos:** toda foto de comprobante se redimensiona (máximo 1600px en el lado más largo) y se recodifica como JPEG con calidad optimizada antes de guardarse (`app/routes/gastos.py`, función `_compress_receipt_image`, usa Pillow). Una foto de celular sin comprimir suele pesar 3–8 MB; después de este proceso normalmente queda entre 50 KB y 300 KB, sin que se note pérdida de legibilidad del comprobante. Esto ahorra muchísimo espacio en disco (importante en planes gratuitos con disco limitado) y hace que "Ver" cargue más rápido en el celular. Los PDF no se comprimen, se guardan tal cual. Si una foto viene en un formato que no se puede abrir (poco común, algunos HEIC de iPhone sin convertir), se guarda el original sin comprimir para no perder el comprobante.
- **Presupuestos** (Gastos → Presupuestos): define un tope mensual de gasto por unidad o por tipo de gasto (ej. "Combustible: S/ 2000/mes"). Cuando el gasto acumulado del mes llega al 90% del presupuesto o lo supera, aparece una alerta en el Panel.
- **Rutas y viáticos** (menú Rutas): define, por cada ruta frecuente (origen → destino), un monto estándar de viáticos. Desde el detalle de un viaje puedes "Confirmar anticipo de viáticos" — el sistema sugiere el monto de la ruta si existe una configurada, y confirma que el conductor recibió ese dinero. Más adelante, desde el mismo anticipo, el botón "Liquidar" compara el monto entregado contra la suma de los gastos reales registrados para ese viaje y muestra el saldo a favor o el exceso.

## Integración con Frotcom (GPS)

El sistema incluye la integración lista para conectarse a Frotcom y mostrar la última ubicación conocida de cada unidad (Menú → Ubicación GPS, solo Administrador). Para activarla:

1. **Pide las credenciales a tu Frotcom Certified Partner** (el distribuidor/instalador local que te vendió el sistema de rastreo) — **no** es el mismo usuario/contraseña con el que entras a la web de Frotcom. Según la documentación oficial de Frotcom ([Authentication in Frotcom API](https://frotcominternational.zendesk.com/hc/en-gb/articles/360001005854-Authentication-in-Frotcom-API), [How to get API V2 credentials](https://frotcominternational.zendesk.com/hc/en-gb/articles/209450709-How-to-get-API-V2-credentials)), pídeles textualmente: **"credenciales de acceso a la API V2 de Frotcom para una integración de terceros ('thirdparty')"**. Te van a dar un usuario y contraseña específicos para eso.
2. Define estas variables de entorno con esos datos: `FROTCOM_USERNAME`, `FROTCOM_PASSWORD` (deja `FROTCOM_BASE_URL` vacío — el sistema ya usa por defecto la URL pública real de la API, `https://v2api.frotcom.com`; solo la necesitas si tu partner te da una URL distinta).
3. En cada unidad (Flota → editar unidad), completa el campo **"ID en el proveedor de GPS"** con el identificador exacto que usa Frotcom para esa unidad (puede ser su ID interno o la placa, según cómo esté configurada tu cuenta — confírmalo con tu partner o revisando la respuesta de la API una vez conectada).
4. Entra a **Ubicación GPS** y haz clic en "Sincronizar con Frotcom".

**Aviso importante:** el cliente de Frotcom (`app/integrations/frotcom.py`) ya implementa el flujo de autenticación real y confirmado contra la documentación pública de Frotcom (login en `POST /v2/authorize` con `provider: "thirdparty"`, y el token se manda como parámetro `api_key` en cada llamada siguiente — no como header). Lo único que **no se pudo confirmar sin credenciales reales** es el endpoint exacto y los nombres de campo para la posición/odómetro de cada vehículo, porque esa parte de la documentación de Frotcom es autodocumentada dentro de la cuenta real (su "Reference guide") y puede variar por plan/región. El cliente usa `/v2/vehicles` como mejor estimación (aparece como ejemplo en la propia documentación oficial de autenticación). Cuando tengas tus credenciales:
- Entra a Frotcom Web → Help Center → sección "Frotcom API V2" → artículo "Reference guide", y confirma el endpoint y los campos exactos de la respuesta de posiciones si difieren de `/v2/vehicles` (está marcado con comentarios "AJUSTAR" en el código).

El resto del sistema funciona con total normalidad sin esta integración — simplemente no habrá datos de ubicación hasta confirmarla.

## Facturación electrónica (SUNAT)

El sistema puede emitir **facturas** y **guías de remisión electrónica (modalidad Transportista)** y enviarlas a SUNAT. Antes de activarlo conviene entender que hay dos caminos totalmente distintos, y el sistema usa el segundo:

**Camino 1 — Conexión directa a SUNAT ("SEE del Contribuyente").** Requiere comprar un certificado digital propio (~S/150–600/año), armar y firmar digitalmente el XML en formato UBL 2.1, y hablar el webservice SOAP de SUNAT. Es una integración pesada, cara de mantener y con un margen de error alto si no se hace con experiencia previa — normalmente solo tiene sentido para empresas grandes con muchísimo volumen de comprobantes.

**Camino 2 — A través de un OSE (Operador de Servicios Electrónicos), como NubeFacT, Efact, BizLinks o Facturalo Perú.** Le mandas un JSON simple por HTTPS con los datos del comprobante, el OSE arma el XML, lo firma con su propio certificado, lo envía a SUNAT y te devuelve el PDF, el XML y la constancia de aceptación (CDR). Es el camino que usa casi cualquier negocio pequeño o mediano, y **es el que implementa este sistema** (`app/integrations/sunat_ose.py`), siguiendo el formato público que documenta NubeFacT (RUTA + TOKEN + JSON), que es el más extendido entre OSEs peruanos orientados a REST. El OSE cobra por comprobante emitido (consulta tarifas directamente con el proveedor que elijas) — es un costo aparte del hosting del ERP.

Ten en cuenta también que, según cambios normativos recientes, los negocios que superan cierto nivel de ingresos anuales (un umbral en UIT) están obligados a emitir a través de un OSE en vez de ir directo a SUNAT — confírmalo con tu contador, ya que las cifras y fechas exactas de esta obligación cambian con el tiempo.

### Cómo activarlo

1. Contrata un OSE autorizado por SUNAT (por ejemplo, NubeFacT: [nubefact.com](https://www.nubefact.com)) y crea primero una cuenta de **pruebas/sandbox**.
2. Con las credenciales que te den, define estas variables de entorno:
   - `OSE_RUTA`: la URL del endpoint que te indique tu OSE.
   - `OSE_TOKEN`: el token de autenticación de tu cuenta.
   - `COMPANY_RUC` y `COMPANY_ADDRESS`: el RUC y la dirección fiscal de tu propia empresa (el emisor de los comprobantes).
   - `INVOICE_SERIES` y `WAYBILL_SERIES`: las series que hayas dado de alta para facturas y guías (por defecto `F001` y `T001`).
3. Registra el RUC de cada cliente (Clientes → editar) — es obligatorio para emitir una factura electrónica.
4. Para conductores, registra también su **DNI** (Conductores → editar) — se necesita para las guías de remisión.
5. Emite una factura de prueba (Facturación → detalle de una factura → "Enviar a SUNAT") o una guía (Viajes → viaje en curso/entregado → "Generar guía de remisión" → "Enviar a SUNAT") y revisa la respuesta.

### Aviso importante — léelo antes de emitir comprobantes reales

Esta integración **no ha podido probarse contra una cuenta real de ningún OSE**, porque esta instalación no cuenta con credenciales de NubeFacT ni de otro proveedor. El archivo `app/integrations/sunat_ose.py` sigue el formato público que NubeFacT documenta en su web, pero varios valores son catálogos oficiales de SUNAT que **debes confirmar antes de usarlo en producción** (están marcados con comentarios "AJUSTAR" en el código):

- Tipo de comprobante (factura vs. guía transportista).
- Tipo de documento de identidad del cliente/conductor (RUC, DNI).
- Unidad de medida de los ítems.
- Tipo de IGV.
- Motivo de traslado y modalidad de transporte (específicos de la guía de remisión).
- El formato exacto de la respuesta del OSE (nombres de campos de éxito/error, enlaces de PDF/XML/CDR).

**Antes de emitir un solo comprobante a un cliente real, pide a tu contador (o a alguien con experiencia en facturación electrónica peruana) que revise los primeros comprobantes emitidos en modo de pruebas.** Un código de catálogo equivocado puede hacer que SUNAT rechace el comprobante, o que lo acepte mal clasificado — con las implicancias tributarias que eso conlleva. Mientras tanto, el resto del sistema (facturas y guías internas, control de estados, historial) funciona con normalidad sin esta integración; simplemente los comprobantes quedan como "No enviados" hasta que la actives y confirmes.

## Cómo correrlo en tu computadora

Requisitos: Python 3.10 o superior.

```bash
# 1. Entra a la carpeta del proyecto
cd erp-transporte

# 2. Crea un entorno virtual (recomendado)
python3 -m venv .venv
source .venv/bin/activate        # En Windows: .venv\Scripts\activate

# 3. Instala las dependencias
pip install -r requirements.txt

# 4. Copia el archivo de variables de entorno
cp .env.example .env
# Abre .env y cambia SECRET_KEY por un valor aleatorio y largo

# 5. Carga datos de ejemplo (usuarios, clientes, flota, viajes)
python seed.py

# 6. Inicia el servidor de desarrollo
python run.py
```

Abre http://localhost:5000 en tu navegador. Usuarios de ejemplo creados por `seed.py`:

- **Administrador:** `admin@erp.local` / `admin1234`
- **Operador:** `operador@erp.local` / `operador1234`

**Importante:** cambia estas contraseñas (o crea tus propios usuarios y desactiva estos) antes de usar el sistema con datos reales.

## Cómo desplegarlo en Render (plan gratuito)

El proyecto ya incluye `requirements.txt` (con `gunicorn` para producción) y un `Procfile`, listos para Render.

### 1. Sube el proyecto a GitHub

Necesitas un repositorio en GitHub porque Render despliega desde ahí.

1. Crea un repositorio nuevo y vacío en [github.com/new](https://github.com/new) (sin README, sin .gitignore — ya tenemos uno).
2. En tu terminal, dentro de la carpeta `erp-transporte`:
   ```bash
   git init
   git add .
   git commit -m "ERP de transporte inicial"
   git branch -M main
   git remote add origin https://github.com/TU-USUARIO/TU-REPO.git
   git push -u origin main
   ```
   Cuando te pida usuario y contraseña: GitHub ya no acepta tu contraseña normal de la cuenta para `git push` por HTTPS. Como contraseña, usa un **Personal Access Token** (Settings de GitHub → Developer settings → Personal access tokens → Generate new token, con permiso `repo`) — como usuario, tu usuario normal de GitHub. Si usas GitHub Desktop o el CLI `gh`, ellos manejan esto automáticamente sin que tengas que crear el token a mano.

### Alternativa: usar Bitbucket en vez de GitHub

Render también se conecta directo a Bitbucket, así que si tu equipo ya usa Bitbucket no hace falta pasar por GitHub.

1. Crea un repositorio nuevo y vacío en Bitbucket ([bitbucket.org](https://bitbucket.org) → **Create repository**).
2. Bitbucket ya no acepta tu contraseña normal ni "app passwords" para `git push` (los app passwords dejaron de funcionar el 9 de junio de 2026) — ahora necesitas un **API token**:
   - Ve a tu perfil de Atlassian → **Settings** → **Security** → **Create and manage API tokens** → **Create API token with scopes**.
   - Dale un nombre, una fecha de expiración, elige la app **Bitbucket** y márcale permisos de **Repositories: Read and Write**.
   - Copia el token — solo se muestra una vez.
3. En tu terminal, dentro de la carpeta `erp-transporte`:
   ```bash
   git init
   git add .
   git commit -m "ERP de transporte inicial"
   git branch -M main
   git remote add origin https://bitbucket.org/TU-WORKSPACE/TU-REPO.git
   git push -u origin main
   ```
   Cuando te pida usuario y contraseña: como usuario pon `x-bitbucket-api-token-auth` (así, literal) y como contraseña pega el **API token** que copiaste — no tu usuario ni tu contraseña normales de Atlassian, esos ya no funcionan para `git push`. Si prefieres no escribirlo cada vez, pon el token directo en la URL del remoto:
   ```bash
   git remote set-url origin https://x-bitbucket-api-token-auth:TU_TOKEN@bitbucket.org/TU-WORKSPACE/TU-REPO.git
   ```
4. En Render, al crear el Web Service (siguiente paso), elige **Connect Bitbucket** en vez de GitHub, autoriza el acceso, y selecciona tu repositorio de la lista.

### 2. Crea el Web Service en Render

1. Entra a [render.com](https://render.com) y crea una cuenta (puedes usar tu cuenta de GitHub o de Bitbucket para registrarte, así Render ya queda conectado).
2. Click en **New +** → **Web Service**.
3. Conecta el repositorio que acabas de subir (a GitHub o a Bitbucket, según el que hayas usado).
4. Configura:
   - **Name:** `erp-transporte` (o el nombre que prefieras; será parte de la URL pública).
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn run:app`
   - **Instance Type:** Free
5. En **Environment Variables**, agrega:
   - `SECRET_KEY` → un valor largo y aleatorio (por ejemplo, genera uno con `python3 -c "import secrets; print(secrets.token_hex(32))"`)
   - `COMPANY_NAME` → el nombre de tu empresa
   - `SESSION_COOKIE_SECURE` → `1`
6. Click en **Create Web Service**. Render construye y despliega automáticamente (toma 1-2 minutos). Al terminar te da una URL pública tipo `https://erp-transporte.onrender.com` — esa es la que compartes.

### 3. Inicia sesión

Abre la URL y entra con los usuarios de ejemplo (se crean solos, ver siguiente sección):

- **Administrador:** `admin@erp.local` / `admin1234`
- **Operador:** `operador@erp.local` / `operador1234`

Cambia estas contraseñas desde el módulo **Usuarios** en cuanto quieras usarlo con datos reales.

### Sobre el plan gratuito: la base de datos se reinicia

Render explica en su documentación que, sin un disco persistente (que solo está disponible en planes de pago), **cualquier archivo local se pierde cada vez que el servicio se reinicia o se vuelve a desplegar** — y en el plan gratuito el servicio se "duerme" tras 15 minutos sin visitas y se reinicia al recibir la siguiente solicitud. Como este proyecto guarda todo en un archivo SQLite, eso significa que los datos que se carguen mientras el servicio está despierto se conservan, pero pueden perderse cuando vuelve a dormirse. ([Persistent Disks – Render Docs](https://render.com/docs/disks); [Deploy for Free – Render Docs](https://render.com/docs/free))

Para que el enlace siempre funcione para que la gente lo pruebe, la app ya viene preparada para esto: **si detecta que la base de datos está vacía, recrea automáticamente los usuarios y datos de ejemplo al iniciar** (ver `AUTO_SEED_DEMO` en `app/__init__.py`). Así, aunque el servicio se duerma y despierte, el link de demo siempre va a tener con qué iniciar sesión — perfecto para que la gente lo pruebe. Lo que sí puede perderse entre reinicios es cualquier dato *nuevo* que alguien cargue durante una sesión de prueba (un cliente o viaje que agreguen ellos, por ejemplo).

Si más adelante quieres usarlo en serio con tu negocio (datos que no se deben perder nunca), tienes dos opciones:

- **Subir a un plan de pago con disco persistente** (Render: agrega un "Persistent Disk" montado en `/opt/render/project/src/instance` y define la variable `DATABASE_PATH=/opt/render/project/src/instance/erp.db`; luego pon `AUTO_SEED_DEMO=0` para que no se recreen los usuarios de ejemplo).
- **Migrar a PostgreSQL** (ver siguiente sección) — Render ofrece una base de datos Postgres gratuita, aunque expira 30 días después de creada.

### Alternativa: crecer a PostgreSQL

Si el negocio crece y necesitas más de un servidor, mayor concurrencia, o simplemente no quieres depender de un disco persistente, el siguiente paso natural es migrar de SQLite a PostgreSQL (por ejemplo con Neon, Supabase, o el Postgres de Render/Railway). La capa de acceso a datos está centralizada en `app/db.py`, lo que facilita ese cambio más adelante.

## Estructura del proyecto

```
erp-transporte/
├── app/
│   ├── __init__.py         # fábrica de la aplicación Flask
│   ├── auth.py              # login, sesiones y permisos por rol
│   ├── db.py                 # acceso a SQLite
│   ├── helpers.py            # utilidades (fechas, montos, códigos correlativos, pretty_label)
│   ├── reports.py             # generación del reporte de gastos en Excel
│   ├── seed_data.py           # datos y catálogos de ejemplo (usados por seed.py y por el auto-seed)
│   ├── schema.sql            # esquema de la base de datos
│   ├── integrations/
│   │   ├── frotcom.py          # cliente de la API de Frotcom (GPS) — ver aviso en el archivo
│   │   └── sunat_ose.py         # cliente OSE para facturación electrónica SUNAT — ver aviso en el archivo
│   ├── routes/                # un blueprint por módulo
│   │   ├── dashboard.py
│   │   ├── clientes.py
│   │   ├── flota.py
│   │   ├── viajes.py
│   │   ├── gastos.py
│   │   ├── mantenimiento.py
│   │   ├── facturacion.py
│   │   ├── guias.py              # guías de remisión electrónica (modalidad Transportista)
│   │   ├── inspecciones.py        # checklist de inspección de unidades
│   │   ├── rutas.py                # rutas frecuentes con viáticos predeterminados
│   │   ├── viaticos.py             # anticipos de viáticos y liquidación
│   │   ├── usuarios.py
│   │   ├── catalogos.py         # catálogos editables (conceptos, tipos de gasto, ítems de inspección)
│   │   └── integraciones.py      # pantalla y sincronización de ubicación GPS
│   ├── templates/             # vistas Jinja2, organizadas por módulo
│   └── static/
│       ├── css/style.css        # estilos (sin dependencias externas)
│       ├── manifest.webmanifest  # manifest de la PWA
│       ├── sw.js                  # service worker (cache mínimo de estáticos)
│       └── icons/                  # íconos de la PWA
├── config.py                  # configuración (lee variables de entorno)
├── seed.py                    # datos de ejemplo
├── run.py                     # punto de entrada para desarrollo
├── requirements.txt
├── Procfile                   # para despliegue con gunicorn
└── .env.example
```

## Próximos pasos sugeridos

- Terminar de confirmar y probar la integración con Frotcom contra tu cuenta real (ver sección dedicada arriba).
- Contratar un OSE, confirmar los catálogos SUNAT marcados como "AJUSTAR" y probar la facturación electrónica en modo sandbox antes de usarla con clientes reales (ver sección dedicada arriba).
- Exportar facturas y reportes a PDF.
- Notificaciones automáticas (correo/WhatsApp) para licencias y mantenimientos próximos a vencer.
- Ver la ubicación de la flota en un mapa (hoy se muestra como tabla de coordenadas).
- Reportes de rentabilidad por viaje (tarifa menos gastos asociados).
- Migrar de SQLite a PostgreSQL si el volumen de datos o usuarios concurrentes crece.
