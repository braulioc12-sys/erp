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
- **Flota:** unidades (placa, capacidad, tipo — camión, tracto o carreta —, estado, **propietario**), con vencimiento de **SOAT** y **Revisión Técnica** (alertas en el Panel).
- **Conductores:** datos personales, vencimiento de brevete, y control de vencimientos de **examen médico ocupacional** y de los requisitos para operar con **Backus** (examen de manejo, capacitación, y DDS) — todos con alerta en el Panel.
- **Liquidaciones:** una liquidación contable por viaje — el anticipo de viáticos entregado al conductor, los gastos reales (combustible, peajes, viáticos, mantenimiento u otros) que se le van asignando manualmente, y el cierre por oficina con numeración de voucher. Incluye **presupuestos mensuales** por unidad o tipo (con alerta en el Panel), un **historial de gastos** filtrable y exportable a Excel, y un **resumen contable exportable** en el formato exacto de la plantilla de liquidación de Harraso — ver la sección dedicada más abajo.
- **Rutas:** catálogo de rutas frecuentes con un monto de viáticos predeterminado (usado para sugerir el anticipo de gastos de cada viaje) y un monto de **comisión del conductor** predeterminado (usado para sugerir la comisión al registrar un viaje por esa ruta).
- **Mantenimiento:** historial de mantenimientos por unidad, costo, kilometraje registrado y próxima fecha/kilometraje. Los conceptos (tipos de mantenimiento) se administran desde Catálogos. Si indicas el kilometraje al registrar un mantenimiento, actualiza automáticamente el kilometraje actual de la unidad. Incluye un catálogo de **trabajos con tiempo estimado** (ej. cambio de aceite = 60 min) que se seleccionan al registrar un mantenimiento, y una vista de **historial y costos totales por unidad**. Los repuestos usados en una orden se descuentan del stock de **Inventarios** — ver la sección dedicada más abajo.
- **Inventarios:** módulo ligado a Mantenimiento — catálogo de repuestos con stock, proveedores, y compras (proveedor, orden de compra, cantidad, precio) que suman al stock una vez recibidas; Mantenimiento dispone de ese mismo stock al usar un repuesto en una orden — ver la sección dedicada más abajo.
- **Neumáticos:** módulo independiente para controlar la vida útil y posición de cada llanta de cada unidad, con un diagrama distinto según el tipo de unidad — ver la sección dedicada más abajo.
- **Inspecciones:** checklist de inspección de una unidad (llantas, frenos, luces, etc.) antes de salir o al llegar de un viaje; los ítems del checklist se administran desde Catálogos. Cada inspección se puede **imprimir o descargar como PDF** con el logo de la empresa en el encabezado.
- **Facturación:** genera facturas por cliente a partir de viajes entregados y aún no facturados; controla estado (pendiente, pagada, vencida, anulada); puede enviarse electrónicamente a SUNAT — ver la sección dedicada más abajo.
- **Guías de Remisión:** genera la guía de remisión electrónica ("modalidad Transportista") de un viaje, con los datos de traslado, vehículo y conductor; puede enviarse a SUNAT igual que las facturas.
- **Usuarios:** solo el Administrador puede crear usuarios y asignar el rol Administrador u Operador.
- **Catálogos** (solo Administrador): agrega o desactiva conceptos de mantenimiento, ítems de inspección y **propietarios de unidades** desde la web, sin tocar código (los conceptos de gasto, con su cuenta contable, se administran aparte en Liquidaciones → Conceptos).
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
| Liquidaciones (gastos, viáticos, presupuestos, resumen contable) | Ver/Crear/Editar | Ver/Crear/Editar |
| Rutas | Ver/Crear/Editar | Solo ver |
| Mantenimiento (incl. trabajos y costos por unidad) | Ver/Crear/Editar | Solo ver |
| Inventarios (repuestos, proveedores, compras) | Ver/Crear/Editar/Autorizar órdenes | Ver/Crear/Editar (crea órdenes y confirma recepción, pero no puede autorizarlas — 1 sep) |
| Neumáticos | Ver/Crear/Editar | Solo ver |
| Inspecciones | Ver/Crear/Editar | Ver/Crear/Editar |
| Facturación | Ver/Crear/Editar/Enviar a SUNAT | Sin acceso |
| Guías de Remisión | Ver/Crear/Editar/Enviar a SUNAT | Ver/Crear/Editar/Enviar a SUNAT |
| Usuarios | Ver/Crear/Editar | Sin acceso |
| Catálogos | Ver/Crear/Editar | Sin acceso |
| Ubicación GPS | Ver/Sincronizar | Sin acceso |

Esto se puede ajustar fácilmente editando el diccionario `PERMISSIONS` en `app/auth.py`.

## Mantenimiento: trabajos con tiempo estimado e historial por unidad

Desde **Mantenimiento → Trabajos y tiempos** se administra un catálogo de trabajos de mantenimiento con su tiempo estimado en minutos (ej. "Cambio de aceite" = 60 min, "Fugas de aire" = 120 min), con un botón para agregar trabajos nuevos que no estén en la lista. Al registrar un mantenimiento, se pueden marcar uno o más trabajos realizados y el sistema suma automáticamente el tiempo estimado total (útil para planificar cuánto tiempo va a estar la unidad en taller).

**El formulario ya no pide un "Concepto" aparte** (retirado el 28 ago): los trabajos que marques son los que clasifican la orden — evita elegir prácticamente lo mismo dos veces, mismo criterio que ya se aplicó en Liquidaciones al retirar "Tipo". El catálogo "Conceptos de mantenimiento" (Catálogos) se mantiene administrable por separado (con la misma lista de 23 actividades) por si hace falta para algo más adelante, pero ya no interviene en este formulario.

**El costo se calcula solo, pero se puede ajustar**: al marcar un trabajo, además se elige qué **tipo de mecánico** lo va a hacer (Senior/Junior/Practicante/Otros — ver más abajo) y **cuántos mecánicos** se necesitan para ese trabajo (pedido de Braulio, 28 ago 3ª ronda; por defecto 1), y el campo "Costo (S/)" se precarga automáticamente sumando, por cada trabajo marcado, sus minutos × el costo por minuto de ese tipo × la cantidad de mecánicos. También se pueden marcar **materiales usados** (ver "Catálogo de materiales" abajo), sumando cantidad × costo unitario de cada uno al mismo total sugerido — se puede editar el costo a mano después si hace falta ajustar algo.

El catálogo actual (23 trabajos, cargado el 28 ago a partir del Excel "ACTIVIDADES TALLER" que entregó Braulio) vive como lista por defecto en `app/seed_data.py` (`DEFAULT_JOB_TYPES`). Si más adelante llega otra lista actualizada (otro Excel, otro archivo), lo más simple es reemplazar esa misma constante en el código y volver a desplegar — pero **no hace falta tocar código para reemplazar el catálogo en un sistema que ya está en uso**: el botón **"⟳ Reemplazar catálogo completo"** en Mantenimiento → Trabajos borra todos los trabajos actuales y vuelve a cargar la lista de `DEFAULT_JOB_TYPES` en un solo clic (pide confirmación antes de hacerlo). El historial de mantenimientos ya registrados no se pierde ni cambia: cada registro guarda su propia copia del nombre y los minutos del trabajo al momento de crearse, independiente de si ese trabajo sigue existiendo después en el catálogo.

### Orden de mantenimiento: marcar trabajos terminados/pendientes, tipo/cantidad de mecánico, asignar mecánico y agregar más sobre la marcha

Cada registro de mantenimiento (una "orden") ahora tiene su propia pantalla de detalle (botón **"Ver orden"** en el listado de Mantenimiento). Ahí, cada trabajo marcado al crear la orden se puede:
- **Marcar como Terminado o Pendiente** individualmente (botón que alterna entre los dos estados, guarda la fecha/hora exacta en que se marcó terminado).
- **Cambiar el tipo de mecánico** (Senior/Junior/Practicante/Otros) que lo va a hacer — se elige por primera vez al crear la orden, y se puede corregir aquí después. Este es uno de los dos campos que determinan el costo de mano de obra sugerido de ese trabajo (ver "Costo de mano de obra por tipo de mecánico" abajo); es independiente de a quién se asigne (columna siguiente).
- **Cambiar la cantidad de mecánicos** que hacen ese trabajo (columna "Cantidad", editable igual que el tipo, guarda apenas se cambia) — pedido de Braulio, 28 ago 3ª ronda. El costo de mano de obra de un trabajo es minutos × costo por minuto del tipo × esta cantidad.
- **Asignar a un mecánico específico** desde un desplegable (el nombre de cada mecánico se muestra junto con su tipo, ej. "Juan Pérez (Senior)") — el cambio se guarda apenas se elige, sin botón aparte.

La orden en sí muestra un **estado general calculado automáticamente** (badge "Pendiente" si ningún trabajo está terminado, "En proceso" si hay una mezcla, "Terminada" si todos lo están, "Sin trabajos" si la orden no tiene ningún trabajo marcado) — visible tanto en el detalle como en una columna nueva del listado de Mantenimiento.

Los mecánicos se administran en **Mantenimiento → Mecánicos** (nombre + **tipo** + activo/inactivo). El tipo se elige al agregar un mecánico y se puede corregir después desde la misma lista (desplegable que guarda el cambio apenas se elige). Desactivar un mecánico lo quita del desplegable de asignación, pero no borra ni cambia los trabajos que ya se le habían asignado — el nombre y el tipo quedan guardados en el propio trabajo (igual que ya pasaba con el nombre y los minutos del trabajo), así que el historial no se ve afectado aunque después se edite o desactive ese mecánico en el catálogo.

**Agregar trabajos o materiales a una orden ya creada** (pedido de Braulio, 28 ago 3ª ronda: "poder editarlas para agregar más que quizás salgan en el camino"): en el detalle de la orden, si quedan trabajos del catálogo o materiales que todavía no están en esa orden, aparece un panel **"+ Agregar trabajos o materiales a esta orden"** — se marcan los que salieron sobre la marcha durante el mantenimiento (con su tipo/cantidad de mecánico o cantidad de material), el costo a agregar se sugiere solo igual que en el formulario de creación, y al guardar se **suman** al costo total y al tiempo estimado de la orden (no se reemplaza nada de lo que ya había). Esta pantalla **no permite editar los datos generales de la orden** (unidad, fecha, kilometraje, próximo mantenimiento, descripción) — solo agregar trabajos/materiales nuevos; si en algún momento hace falta poder corregir esos otros campos también, avisa y se agrega.

### Costo de mano de obra por tipo de mecánico y catálogo de materiales

En **Catálogos** hay un panel "Costo de mano de obra (Mantenimiento)" con **un costo por minuto (S/) para cada tipo de mecánico** (Senior, Junior, Practicante, Otros) — pedido de Braulio, 28 ago (2ª ronda): antes había un solo valor general, ahora cada tipo tiene el suyo. Al registrar o editar un trabajo dentro de una orden se elige qué tipo de mecánico lo hará y cuántos, y el costo sugerido de la orden suma, por cada trabajo marcado, sus minutos × el costo del tipo elegido × la cantidad de mecánicos. **Vienen con valores de arranque (Senior S/ 4.00, Junior S/ 3.00, Practicante S/ 1.50, Otros S/ 2.50 por minuto) — AJUSTAR: confirma las tarifas reales de tu taller ahí antes de confiar en los costos calculados.** Solo Administrador puede cambiarlos (mismo permiso que el resto de Catálogos); Operador los ve pero no los edita.

Los materiales/repuestos que se pueden marcar al crear una orden o agregar después desde su detalle (indicando la cantidad usada) vienen del catálogo de repuestos del módulo **Inventarios** (antes vivían en Mantenimiento → Materiales; desde el 29 ago ese catálogo es el mismo que usa Inventarios, con stock real) — el subtotal de cada uno (cantidad × costo unitario) se suma al costo sugerido de la orden junto con la mano de obra, y usarlo en una orden descuenta esa cantidad del stock disponible en almacén. El detalle de la orden muestra por separado el costo de materiales y el total de la orden. Ver la sección dedicada de Inventarios más abajo para el detalle completo (compras, stock, proveedores).

**Mantenimiento → Historial y costos por unidad** muestra, por cada unidad, el número de mantenimientos, el costo total acumulado y la fecha del último, con un enlace para ver el historial completo de esa unidad.

## Inventarios: repuestos con stock, proveedores y compras (29 ago)

Braulio pidió un módulo de Inventarios ligado a Mantenimiento: "cada compra de repuestos que se haga debe figurar el proveedor, orden de compra, cantidad y precio. Una vez que se ingrese al stock disponible de nuestro almacén el área de mantenimiento puede disponer de estos repuestos." Es un módulo propio (menú **Inventarios**, con su propio permiso — Operador solo ve, Administrador administra), pero comparte el catálogo de repuestos con Mantenimiento: es el mismo catálogo que antes vivía en Mantenimiento → Materiales, ahora con stock real.

- **Repuestos** (Inventarios, pantalla principal): catálogo de repuestos con su costo unitario de referencia y su **stock actual** en almacén, con un **buscador por nombre** (30 ago) para encontrar uno rápido en catálogos grandes. El costo unitario se actualiza solo al precio de la última compra recibida (ver más abajo); el stock se puede además ajustar a mano (para corregir un conteo físico) sin pasar por una compra.
- **Historial de compras por repuesto** (30 ago): al hacer clic en un repuesto (o en el botón "Historial" de su fila) se abre su detalle, con el último precio pagado, el precio promedio pagado, y una tabla con cada compra en la que apareció ese repuesto — fecha, **proveedor**, orden de compra, cantidad y **precio** de esa compra en particular, incluyendo compras todavía pendientes de recibir.
- **Proveedores** (Inventarios → Proveedores): catálogo simple (nombre, RUC opcional, teléfono opcional) — aparecen al registrar una compra. Desactivar un proveedor no afecta las compras que ya se le hicieron (cada compra guarda su propia copia del nombre).
- **Compras** (Inventarios → Compras → + Nueva compra): registras el proveedor, el N° de orden de compra, la fecha, y uno o más repuestos con su cantidad y precio unitario en esa compra (una compra puede incluir varios repuestos distintos). Operador puede crear la orden (además de Administrador) — ver "Autorización, PDF y recepción parcial" más abajo para el flujo completo, cambiado el 1 sep.
- **Mantenimiento dispone del stock**: al marcar un repuesto en una orden de mantenimiento (al crearla, o después desde "+ Agregar trabajos o materiales a esta orden"), se descuenta la cantidad usada del stock de Inventarios — **se permite usar más de lo que hay en stock, no se bloquea, solo se avisa** (pedido explícito de Braulio) con un mensaje señalando que el stock quedó en negativo, para que se revise cuando corresponda.
- **Viene con datos de ejemplo** (6 repuestos con stock y precios de referencia, 2 proveedores, una compra ya recibida y otra pendiente) **— AJUSTAR: son placeholders, carga tu catálogo real de repuestos, tus proveedores reales, y (si quieres arrancar con el stock real ya cargado) usa "Ajustar" stock por repuesto o registra tus compras reales una vez desplegado.**

### Órdenes de compra: autorización de administrador, PDF para el proveedor, y recepción parcial (1 sep)

Braulio pidió: "necesitamos que se pueda generar una orden de compra, esta orden de compra cuando lo autoriza un administrador se registra su autorización abajo de la orden, se genera un pdf y se pueda enviar al proveedor. Luego el usuario con esta orden de compra tiene que confirmar la recepción de los items, puede que a veces no lleguen todos." El flujo anterior (Pendiente → "✓ Marcar como recibida" de un solo clic) se reemplazó por uno de tres pasos:

1. **Crear la orden** (Operador o Administrador): nace como **"Pendiente de autorización"** — todavía se puede editar/eliminar.
2. **Autorizar** (exclusivo de Administrador): desde el detalle de la orden, el botón "✓ Autorizar orden de compra" queda registrado con quién y cuándo, justo debajo de los datos de la orden. **A partir de acá la orden queda bloqueada** — ya no se puede eliminar — y recién ahí aparecen disponibles el PDF y la recepción.
3. **Generar el PDF** ("🖨️ Ver / Descargar PDF", mismo mecanismo que las Inspecciones: una página lista para "Guardar como PDF" desde el propio navegador, sin librería de PDF en el servidor) para mandárselo al proveedor por correo o WhatsApp — incluye el bloque de autorización (quién y cuándo) al final.
4. **Confirmar la recepción** (Operador o Administrador): puede llegar todo junto o **en varias entregas parciales** — cada vez que algo llega, se indica cuánto de cada repuesto (puede ser menos de lo pedido) y eso sube el stock y queda registrado como un evento de recepción con fecha y quién lo confirmó. La orden se cierra sola cuando ya se recibió el 100% de todas las líneas; si algo nunca termina de llegar, el botón **"Cerrar orden (no llegará el resto)"** la cierra igual con lo que sí llegó, para que no quede indefinidamente en "recepción parcial".

**Cambio de permisos**: Operador ahora tiene permiso de **edición completa** en Inventarios (antes solo veía) para poder crear órdenes y confirmar recepciones — como el permiso es por módulo completo (igual que en el resto del sistema, no hay un permiso más fino solo para compras), esto también le da a Operador acceso para administrar el catálogo de repuestos/proveedores y ajustar stock a mano. Autorizar sigue siendo exclusivo de Administrador, verificado por rol directamente en el código (no por este permiso).

## Flota: propietario de la unidad

Cada unidad tiene un campo **Propietario** (pedido de Braulio, 28 ago), elegido de un desplegable en vez de escrito a mano — los propietarios disponibles se administran en **Catálogos → Propietarios de unidades** (solo Administrador), mismo patrón que los demás catálogos editables del sistema: se agregan o desactivan sin tocar código. La columna aparece también en el listado de Flota. **Viene con dos valores de ejemplo ("Harraso Transport", "Tercero afiliado") — AJUSTAR: reemplázalos por los propietarios reales de tu flota en Catálogos.** Si una unidad ya tenía un propietario que después se desactivó en el catálogo, no se pierde el dato — se sigue mostrando en su formulario como una opción aparte ("no está en el catálogo") hasta que se cambie explícitamente.

## Importación masiva desde Excel: Flota, Conductores y Rutas (30 ago)

Braulio pidió poder cargar varias unidades, conductores o rutas de una sola vez en vez de crearlos uno por uno: "habilita una opción de poder importar de manera masiva a través de un formato de excel que tú pongas disponible para importar." Cada uno de los tres módulos tiene ahora un botón **"Importar desde Excel"** junto al de "+ Nuevo/a" en su listado, que lleva a una pantalla de dos pasos:

1. **Descargar la plantilla** (botón "⬇ Descargar plantilla Excel"): un archivo `.xlsx` con una hoja **Datos** (encabezados de columna en la fila 3, una fila de ejemplo ya llena en la fila 4, y listas desplegables ya puestas en las columnas que solo aceptan ciertos valores — por ejemplo Tipo de unidad o Estado) y una hoja **Instrucciones** con el detalle de cada columna (cuáles son obligatorias, formato de fecha, valores válidos).
2. **Subir el archivo completado**: se valida fila por fila y se muestra un resumen — cuántos se crearon, cuántos se actualizaron (solo aplica a Rutas, ver abajo), cuántos se omitieron y por qué, y cualquier observación (por ejemplo una fecha con formato inválido, o un valor que no coincide con ninguno de los válidos) con el número de fila del Excel para ubicarla fácil. Cuando la observación es sobre un dato opcional, la fila igual se importa, sin ese dato — solo se avisa.

Cómo se maneja cada módulo si el dato ya existe (para poder reimportar el mismo archivo corregido sin duplicar nada):

- **Flota**: se identifica por **placa**. Si la placa ya existe, esa fila se omite (no se sobrescribe la unidad existente) — hay que editarla a mano si se quiere corregir. Si el archivo trae la misma placa dos veces, solo se importa la primera aparición.
- **Conductores**: se identifica por **DNI** si la fila lo trae; si no, por el **nombre exacto** (sin distinguir mayúsculas). Igual que en Flota, si ya existe se omite en vez de sobrescribir.
- **Rutas**: se identifica por **origen + destino**, la misma combinación que ya era única en el catálogo de Rutas — si ya existe, en vez de omitirla **se actualizan sus montos de viáticos y comisión** (mismo comportamiento que ya tenía el formulario de "+ Guardar ruta" del listado), y por eso Rutas sí puede mostrar "actualizados" además de "creados".

La fila de ejemplo de la plantilla se reconoce y se descarta sola al importar (no hace falta acordarse de borrarla); si de todos modos la reemplazas con datos reales, se importa como cualquier otra fila. Las fechas se aceptan en formato AAAA-MM-DD (el que pide la plantilla) o DD/MM/AAAA. Por ahora la importación solo crea/actualiza registros — no hay una opción de "deshacer" una importación completa, así que conviene revisar el resumen antes de dar por buena una carga grande.

## Viajes: comisión del conductor y reporte mensual

Cada viaje tiene, además de la tarifa, un campo de **comisión del conductor**. Al registrar o editar un viaje, si el campo se deja vacío, el sistema sugiere automáticamente el monto configurado para esa ruta en **Rutas** — al elegir la ruta verás la sugerencia aparecer debajo del campo; si lo dejas en blanco al guardar, se usa ese monto. El campo siempre se puede editar manualmente para un viaje puntual.

**Ruta por catálogo, no por texto libre (28 ago):** el origen/destino de un viaje ya no se escribe a mano — se elige de un desplegable con las rutas activas registradas en **Rutas** (para agregar una ruta nueva, primero se registra ahí). Si no hay ninguna ruta en el catálogo, el formulario avisa y enlaza directo a Rutas para agregar una. Al editar un viaje cuya ruta ya no está en el catálogo activo (por ejemplo, se desactivó esa ruta después), el desplegable muestra esa combinación como "Ruta actual (no está en el catálogo)" para no perder el dato — si no la vuelves a elegir explícitamente, se mantiene igual.

**Viajes → Comisiones por mes** muestra, para el mes seleccionado, cuántos viajes hizo cada conductor, a qué ruta, y el total de comisión correspondiente (agrupado por conductor y luego por ruta, con subtotal por conductor y total general). Los viajes cancelados no se cuentan. Se puede exportar el mismo reporte a Excel (`.xlsx`) con el botón "Exportar a Excel".

## Neumáticos: vida útil y posición por unidad

Desde el módulo **Neumáticos** (o el botón "Neumáticos" en cada fila de Flota) se controla cada llanta de cada unidad, identificada por su posición en un diagrama según el tipo de unidad (**Flota → editar unidad → "Tipo de unidad"**):

- **Tracto camión:** eje de dirección (2 llantas) + 2 ejes de tracción dobles (4 llantas cada uno) = 10 posiciones.
- **Carreta / semirremolque:** 3 ejes dobles (4 llantas cada uno) = 12 posiciones.
- **Camión (unidad simple):** eje de dirección (2 llantas) + eje trasero doble (4 llantas) = 6 posiciones.

Si tu configuración de ejes es distinta, se ajustan fácilmente en `app/tire_positions.py` (todo centralizado ahí).

Al hacer clic en una posición vacía del diagrama (o en su fila de la tabla) se registra una llanta nueva: marca, fecha de instalación, kilometraje de la unidad en ese momento, y vida útil estimada en km (por defecto 80,000, ajustable por llanta según la marca/modelo). **El kilometraje acumulado no se guarda como un número aparte — se calcula solo**, comparando el kilometraje actual de la unidad (que ya se actualiza al registrar un mantenimiento, editar la unidad en Flota, o sincronizar GPS) contra el kilometraje que tenía al instalarse esa llanta. Así el acumulado de cada llanta siempre está al día sin ningún paso adicional.

Cada llanta activa muestra un indicador de color según su % de vida útil consumida (verde por debajo de 80%, ámbar de 80% a 99%, rojo en 100% o más — con alerta en el Panel a partir de 90%). Desde el detalle de una llanta puedes **"Reemplazar"** (retira la actual y registra la nueva en un solo paso, conservando el historial) o **"Retirar sin reemplazar"** (deja la posición vacía). El historial completo de llantas retiradas por unidad queda disponible en la misma página del diagrama.

### Rotar llantas por desgaste (30 ago)

Desde el diagrama de una unidad, el botón **"Rotar llantas"** (visible cuando hay 2 o más llantas activas) abre un formulario donde eliges la nueva posición para cada llanta según el patrón de rotación que estés aplicando (por ejemplo, cruzar las del eje de dirección con las de tracción para parejar el desgaste). Solo se registran los cambios reales — si dejas una llanta en su misma posición, no se toca — y el sistema no te deja dejar dos llantas terminando en la misma posición.

Rotar **no reinicia el contador de vida útil de cada llanta**: cada llanta conserva su fecha e kilometraje de instalación originales, solo cambia de posición — el % de desgaste se sigue calculando igual que siempre, ahora medido desde su nueva ubicación. Cada rotación queda registrada con fecha, kilometraje de la unidad y el detalle de qué llanta pasó de qué posición a cuál, visible en el panel **"Historial de rotaciones"** de esa misma página.

## Inspecciones de unidades

Desde el detalle de un viaje (o desde el menú **Inspecciones**) se puede registrar un checklist de inspección de la unidad — "antes de salir" o "al llegar". Hay dos formatos, según el tipo de unidad:

- **Camiones:** checklist genérico configurable — un ítem por fila (llantas, frenos, luces, niveles, extintor, etc.), marcando cada uno como OK, Falla o N/A, y una observación opcional. Los ítems se administran desde **Catálogos → Ítems de inspección** (solo Administrador).
- **Tractos y carretas:** al elegir una unidad de uno de estos dos tipos, el formulario cambia automáticamente al **Check List de Tracto** o al **Check List de Carreta**, cada uno calcado del formato físico que usa Harraso hoy:
  - **Check List de Tracto:** mismas secciones que el papel (Personal, Revisión de niveles, Sistema de admisión, Revisión general, Actividades, Tablero de control, Accesorios de seguridad) con sus mismas columnas de estado por sección (Bien/Mal, Normal/Falta, Completo/Falta, Normal/Obstruido), más código de llanta según posición (10 posiciones + repuesto), **kilometraje** (actualiza el kilometraje de la unidad, igual que en Mantenimiento) y **lugar** (Pucallpa/Tarapoto/Lima).
  - **Check List de Carreta:** una sola tabla de "Revisión general" (Bien/Mal) con los ítems propios de una carreta (muelles, kin pin y plancha, sistema eléctrico, freno, retráctil, suspensión, etc.), más código de llanta y **presión** según posición (12 posiciones + repuesto). No tiene kilometraje, porque una carreta no trae odómetro propio.
  - Ambos comparten el mismo **código correlativo** al guardar (ej. CL-0001), el mismo **operador**, y las firmas de Operador / Mantenimiento al pie. Quedan centralizados en `app/detailed_checklists.py` — si Harraso ajusta alguno de los formatos físicos, ese es el único archivo que hay que tocar. Los camiones seguirán usando el checklist genérico hasta que se comparta su propio formato.

**Imprimir / descargar como PDF:** en el detalle de cualquier inspección, el botón "🖨️ Imprimir / Descargar PDF" abre una vista de impresión aparte (calcada del formato correspondiente: genérico, tracto o carreta), con el logo de la empresa en el encabezado y espacios de firma. Desde ahí se usa el diálogo de impresión del propio navegador (Ctrl+P / el botón de la página) eligiendo "Guardar como PDF" como destino — no requiere ninguna librería adicional en el servidor ni descargas, funciona igual en celular y en computadora.

## Liquidaciones: una liquidación contable por viaje (gastos, viáticos, asignación manual y export)

Este módulo (menú **Liquidaciones**, antes eran dos menús separados "Gastos" y "Viáticos") reúne todo el ciclo de gasto de un viaje: el anticipo entregado al conductor, los gastos que se van registrando, y el cierre contable de esa liquidación. Hay **exactamente una liquidación por viaje** (nace al confirmar el anticipo de viáticos desde el detalle del viaje).

- **Abrir una liquidación:** desde el detalle de un viaje, "Confirmar anticipo de viáticos" — el sistema sugiere el monto según la ruta (configurable en Rutas) y registra que el conductor recibió ese dinero. Esto abre la liquidación de ese viaje.
- **Más de un anticipo por liquidación:** una liquidación puede recibir varios anticipos por separado (ej. uno al inicio del viaje y otro a mitad de camino, pedido de Braulio, 28 ago) — desde el detalle de la liquidación, mientras siga pendiente, hay un formulario "+ Agregar anticipo" (monto, fecha, notas) debajo de la tabla de anticipos ya entregados; el total mostrado (y el que se compara contra lo gastado) siempre es la suma de todos. Un anticipo se puede eliminar mientras la liquidación siga pendiente y quede al menos uno; una vez liquidada, ni se pueden agregar ni eliminar.
- **Registrar gastos:** desde el detalle de la liquidación (o desde el detalle del viaje) se registran los gastos reales con su comprobante — foto con cámara o archivo/PDF, comprimidos automáticamente (máximo 1600px, JPEG calidad optimizada, `_compress_receipt_image` en `app/routes/liquidaciones.py`, usa Pillow), concepto, monto, descripción. Al entrar desde el link "+ Registrar gasto para este viaje" de una liquidación específica, el viaje ya viene fijo (se muestra solo como texto, no como desplegable) — no hay que volver a elegirlo (pedido de Braulio, 28 ago); si se entra desde Liquidaciones → Historial de gastos → "+ Registrar gasto" sin venir de un viaje puntual, sí aparece el desplegable de viaje normal. La **Unidad** también se completa sola con la que ya tiene asignada ese viaje (`trips.vehicle_id`, elegida al crear el viaje) — si se entra desde una liquidación puntual y el viaje ya tiene unidad, ni siquiera se muestra el campo; en el flujo general, se auto-selecciona en cuanto se elige el viaje pero se puede cambiar a mano si ese gasto en particular fue con otra unidad (pedido de Braulio, 28 ago: "si el viaje ya tiene unidad asignada, ¿para qué la vuelve a pedir?").
- **Asignar gastos a la liquidación (manual):** en el detalle de la liquidación, cada gasto del viaje aparece con una casilla "Incluir" — por defecto vienen marcados, pero se puede desmarcar cualquiera antes de cerrar (por ejemplo, un gasto que en realidad corresponde a otra liquidación, o que se quiere corregir después). Antes esta asignación era automática (todo lo del viaje entraba); ahora es una decisión explícita del usuario, pedida por Braulio al replantear el módulo (28 ago).
- **Liquidar:** se elige la **oficina** (Lima, Pucallpa o Tarapoto) donde se hace la liquidación y se presiona "Liquidar" — el selector de oficina y el botón están arriba de la lista de gastos, junto con las casillas de inclusión, todo en un mismo formulario. Al liquidar se calcula el correlativo de voucher de esa oficina (se reinicia cada mes, empieza en 01) y la liquidación queda **cerrada**: ya no se pueden cambiar los gastos incluidos, y los gastos que quedaron dentro no se pueden eliminar (si hace falta corregir un monto, se puede editar el gasto, pero conviene volver a exportar el resumen de ese mes/oficina después).
- **Presupuestos** (Liquidaciones → Presupuestos): define un tope mensual de gasto por unidad o por concepto de gasto (ej. "Peaje: S/ 2000/mes"). Cuando el gasto acumulado del mes llega al 90% del presupuesto o lo supera, aparece una alerta en el Panel.
- **Historial de gastos** (Liquidaciones → Historial de gastos): la lista plana de todos los gastos, tengan o no viaje asociado (útil para gastos sueltos de una unidad, sin viaje) — filtrable por concepto y fecha, exportable a Excel.

### Resumen contable exportable

Cada gasto se clasifica con un solo campo, **Concepto** (obligatorio), tomado de **Liquidaciones → Conceptos** (solo Administrador, mismo permiso que Catálogos porque define códigos contables). Cada concepto amarra un nombre (ej. "PEAJE") a su **cuenta contable** (ej. 42121) y su **tipo de comprobante** (ej. "factura", código SUNAT "01"); al elegirlo en el formulario de gasto, la cuenta y el comprobante se completan solos, y ese mismo nombre alimenta Presupuestos y el Historial. Los 16 conceptos de la hoja "Conceptos" de la plantilla de liquidación de Harraso vienen precargados por el seed de datos de ejemplo.

> El formulario de gasto tenía antes dos campos de clasificación ("Tipo" y "Concepto") que pedían lo mismo dos veces — Braulio lo reportó el 28 ago con una captura de pantalla. Se retiró el campo "Tipo" (y el catálogo "Tipos de gasto" de Catálogos, que quedó sin uso): ahora Concepto es la única clasificación, y Presupuestos pasó a estar organizado por Concepto en vez del catálogo anterior (decisión explícita de Braulio: "Presupuestos pasan a ser por Concepto"). Internamente la columna `expenses.type` se sigue completando sola con el nombre del concepto elegido — no fue necesario ningún cambio de esquema de base de datos.

Con eso, **Liquidaciones → Resumen contable** arma automáticamente, por mes y oficina, una fila **Haber** por cada liquidación de viaje ya cerrada (el "vale", contra la cuenta "por liquidar" de esa oficina) más una fila **Debe** por cada gasto que quedó incluido en esa liquidación — exactamente en el mismo formato de columnas de la "hoja resumen" de la plantilla real de Harraso (Origen, Num.Voucher, Fecha de Liquidacion, Cuenta, Monto Debe, Monto Haber, Moneda S/D, T.Cambio, Doc, Num.Doc, Fec.Doc, Fec.Ven, RUC O DNI, Glosa, RUC O DNI, R. Social). El botón "⬇ Exportar a Excel" descarga esa misma tabla lista para pegar directo en el sistema contable.

- **Origen / Num.Voucher:** "Origen" es el código de la oficina donde se liquida (14 = Lima, 15 = Pucallpa, 16 = Tarapoto — ver nota abajo). "Num.Voucher" es el correlativo de liquidación de esa oficina, y se reinicia cada mes empezando en 01 (pedido explícito de Braulio) — se asigna recién al momento de liquidar, no al confirmar el anticipo.
- **Tipo de cambio:** se completa automáticamente con el tipo de cambio SUNAT del día de emisión del comprobante (`app/integrations/sunat_exchange_rate.py`, vía la API pública de [decolecta.com](https://decolecta.com)), y queda en caché en la base de datos para no volver a consultarlo. Si el servicio no responde (sin internet, caído, etc.) el gasto se guarda igual — el campo queda vacío y aparece un aviso para completarlo a mano si hace falta; nunca bloquea el registro del gasto. Se puede forzar un valor manual en el campo "Tipo de cambio (opcional)" del formulario.
- **Razón social del proveedor:** al escribir el RUC del proveedor (11 dígitos) en el formulario de gasto, la "Razón social / Nombre del proveedor" se autocompleta directo en el campo (consultando SUNAT vía el mismo servicio de decolecta.com — `app/integrations/sunat_ruc.py`, endpoint propio `GET /liquidaciones/gastos/consultar-ruc` — y queda en caché por RUC para no repetir la consulta). El campo queda bloqueado (gris) con un botón "✎ Editar" al lado para cambiar el nombre a mano si hace falta. Si el RUC no existe o el servicio no responde, el campo queda editable para escribirlo a mano — nunca bloquea el registro del gasto. Comparte el mismo `DECOLECTA_TOKEN` que el tipo de cambio (no hace falta una cuenta aparte).
- **Fecha de vencimiento:** siempre igual a la fecha de emisión del gasto (pedido explícito de Braulio), no se pide por separado.

**Cosas por confirmar con Braulio antes de usar esto con datos reales** (marcadas "AJUSTAR" en el código):
- El código de oficina y la cuenta contable del vale de **Tarapoto** (16 / 14133) son una inferencia a partir del patrón de Lima (14) y Pucallpa (15) — Braulio solo confirmó esas dos.
- La integración con la API de tipo de cambio SUNAT y la de consulta de RUC de decolecta.com no se pudieron probar con una llamada real desde este entorno (sin salida a internet) — ambas se probaron simulando la respuesta del servicio. Conviene verificarlas ya desplegado en Render, registrando un gasto y revisando que el "Tipo de cambio" y, al escribir un RUC real, la "Razón social" se completen solos. La consulta de RUC además necesita que `DECOLECTA_TOKEN` esté configurado en Render (registrarse gratis en decolecta.com) — sin token, el campo simplemente no se autocompleta.

## Integración con Frotcom (GPS)

El sistema incluye la integración lista para conectarse a Frotcom y mostrar la última ubicación conocida de cada unidad (Menú → Ubicación GPS, solo Administrador). Para activarla:

1. **Pide las credenciales a tu Frotcom Certified Partner** (el distribuidor/instalador local que te vendió el sistema de rastreo) — **no** es el mismo usuario/contraseña con el que entras a la web de Frotcom. Según la documentación oficial de Frotcom ([Authentication in Frotcom API](https://frotcominternational.zendesk.com/hc/en-gb/articles/360001005854-Authentication-in-Frotcom-API), [How to get API V2 credentials](https://frotcominternational.zendesk.com/hc/en-gb/articles/209450709-How-to-get-API-V2-credentials)), pídeles textualmente: **"credenciales de acceso a la API V2 de Frotcom para una integración de terceros ('thirdparty')"**. Te van a dar un usuario y contraseña específicos para eso.
2. Define estas variables de entorno con esos datos: `FROTCOM_USERNAME`, `FROTCOM_PASSWORD` (deja `FROTCOM_BASE_URL` vacío — el sistema ya usa por defecto la URL pública real de la API, `https://v2api.frotcom.com`; solo la necesitas si tu partner te da una URL distinta).
3. En cada unidad (Flota → editar unidad), completa el campo **"ID en el proveedor de GPS"** con el identificador exacto que usa Frotcom para esa unidad (puede ser su ID interno o la placa, según cómo esté configurada tu cuenta — confírmalo con tu partner o revisando la respuesta de la API una vez conectada). También se puede cargar en bloque desde **Flota → Importar unidades**, subiendo un Excel con la columna "ID en el proveedor de GPS" llena — actualiza solo ese campo en las unidades que ya existen.
4. Entra a **Ubicación GPS** y haz clic en "Sincronizar con Frotcom". Una vez configuradas las credenciales, el sistema además sincroniza solo en segundo plano cada 2 minutos (no hace falta tener la página abierta) — controlado por la variable de entorno `FROTCOM_AUTO_SYNC_SECONDS` (segundos entre sincronizaciones automáticas, por defecto `120`; ponla en `0` para desactivarla y depender solo del botón manual).

**Aviso importante:** el cliente de Frotcom (`app/integrations/frotcom.py`) ya implementa el flujo de autenticación real y confirmado contra la documentación pública de Frotcom (login en `POST /v2/authorize` con `provider: "thirdparty"`, y el token se manda como parámetro `api_key` en cada llamada siguiente — no como header). Lo único que **no se pudo confirmar sin credenciales reales** es el endpoint exacto y los nombres de campo para la posición/odómetro de cada vehículo, porque esa parte de la documentación de Frotcom es autodocumentada dentro de la cuenta real (su "Reference guide") y puede variar por plan/región. El cliente usa `/v2/vehicles` como mejor estimación (aparece como ejemplo en la propia documentación oficial de autenticación). Cuando tengas tus credenciales:
- Entra a Frotcom Web → Help Center → sección "Frotcom API V2" → artículo "Reference guide", y confirma el endpoint y los campos exactos de la respuesta de posiciones si difieren de `/v2/vehicles` (está marcado con comentarios "AJUSTAR" en el código).

El resto del sistema funciona con total normalidad sin esta integración — simplemente no habrá datos de ubicación hasta confirmarla.

### Historial de viajes y reportes diarios de horas/km (31 ago)

Además de la última posición, el sistema calcula horas manejadas y km avanzados por día por unidad (Menú → Ubicación GPS → "📅 Reportes diarios"). Hay dos fuentes de datos, y el sistema usa automáticamente la mejor disponible para cada unidad y día:

- **Viajes de Frotcom (exacto)**: si se importó el historial de viajes de Frotcom para ese día (ver siguiente punto), se usan los números que Frotcom ya calculó (`driveTimeSec`, `mileage` del endpoint `GET /v2/vehicles/{id}/trips`) — más precisos que cualquier estimado propio.
- **Estimado (posiciones)**: si no hay viajes importados ese día, se estima a partir del historial de posiciones sueltas que se va guardando en cada sincronización (manual o automática cada 2 minutos) — puede quedar por debajo de lo real si todavía no se acumularon suficientes sincronizaciones ese día.

**Ubicación GPS → "🗂️ Historial de viajes"** deja traer de Frotcom el historial de viajes de TODAS las unidades para un rango de fechas (útil para rellenar reportes de días anteriores a que existiera esta función, ya que antes solo se guardaba la última posición, nunca un historial). Corre en segundo plano (puede tardar varios minutos con una flota grande) — la pantalla muestra el avance y no hace falta quedarse esperando. Esta misma tabla de viajes (`vehicle_trips`, con origen/destino/horarios reales de cada viaje) es también la base para el futuro reporte de cumplimiento de hoja de ruta.

**Nota sobre límites de la API:** el endpoint de viajes solo admite pedir un rango de máximo 7 días por llamada (el sistema ya parte rangos más largos en tramos automáticamente) y es una llamada **por unidad** — a diferencia del endpoint de posición actual (una sola llamada trae todas las unidades), así que traer el historial de una flota grande implica muchas llamadas seguidas. El límite real de "rate limit" de la cuenta de Frotcom no está confirmado, por eso esta importación es manual (bajo demanda desde el botón), no automática cada 2 minutos como las posiciones.

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

- **Subir a un plan de pago con disco persistente** (Render: agrega un "Persistent Disk" montado en `/opt/render/project/src/instance` y define la variable `DATABASE_PATH=/opt/render/project/src/instance/erp.db`; luego pon `AUTO_SEED_DEMO=0` para que no se recreen los usuarios de ejemplo). Solución rápida, pero los comprobantes de gastos (Liquidaciones) seguirían en ese mismo disco.
- **Base de datos y comprobantes persistentes en AWS** (ver la sección "Base de datos persistente en AWS (RDS + S3)" más abajo) — PostgreSQL gestionado (Amazon RDS) + almacenamiento de archivos (Amazon S3). Es más trabajo de configuración inicial, pero es la solución más robusta y la que no depende de las políticas de disco de Render.

## Base de datos persistente en AWS (RDS + S3)

Si el negocio ya no puede depender de que la base de datos y los comprobantes de gastos se pierdan en cada reinicio (ver sección anterior), el proyecto ya viene preparado para guardar ambas cosas en Amazon Web Services, sin tocar ni una línea de código — solo hay que crear los recursos en AWS y poner sus datos como variables de entorno en Render:

- La **base de datos** pasa de SQLite a **Amazon RDS para PostgreSQL** (gestionada por AWS: backups automáticos, no se pierde nunca).
- Los **comprobantes de gastos** (Liquidaciones) pasan de guardarse en el disco de Render a un **bucket privado de Amazon S3**.

Si no defines estas variables, la app sigue funcionando exactamente igual que ahora (SQLite + disco local) — es un cambio de todo o nada por variable, no un punto sin retorno. Como hoy el disco de Render es efímero, no hay datos reales que "migrar": es un cambio en limpio, no una migración con riesgo de pérdida de información.

### Antes de empezar: por qué un usuario IAM y no la cuenta root

La cuenta con la que te registras en AWS (la "root") puede hacer *cualquier cosa*, incluyendo borrar toda la cuenta o cambiar la forma de pago — por eso AWS mismo recomienda no usarla para el día a día. Todo lo de abajo se hace con un **usuario IAM** aparte, con permisos limitados solo a lo que este proyecto necesita (un bucket de S3 específico). Guarda la contraseña root en un lugar seguro, actívale verificación en dos pasos, y no la vuelvas a usar salvo para tareas de cuenta (facturación, cerrar la cuenta, etc.).

### 1. Crea la cuenta de AWS y protégela

1. Ve a [aws.amazon.com](https://aws.amazon.com/) → **Crear una cuenta de AWS** y sigue el registro (pide una tarjeta, aunque no se cobra nada si te mantienes dentro de lo gratuito).
2. Activa verificación en dos pasos (MFA) en el usuario root: **IAM** → **Panel** → **Agregar MFA** en tu usuario root.
3. Pon una alerta de gasto para que nunca te lleves una sorpresa: **Billing** → **Budgets** → **Create budget** → *Zero spend budget* (te avisa por correo apenas se genera cualquier cargo) o un monto fijo bajo (ej. US$ 10/mes).
4. Elige una región cercana a Perú con buena latencia — normalmente **us-east-1 (Virginia del Norte)** o **sa-east-1 (São Paulo)**; usa la misma región para todo lo que sigue (S3, RDS).

Los términos exactos de la capa gratuita (horas incluidas de RDS, créditos de bienvenida, GB gratis de S3) cambian de tanto en tanto — revisa la [calculadora de precios de AWS](https://calculator.aws/) o las páginas de precios de [RDS](https://aws.amazon.com/rds/postgresql/pricing/) y [S3](https://aws.amazon.com/s3/pricing/) al momento de crear los recursos para saber exactamente qué te van a cobrar. Como referencia: una instancia RDS pequeña (db.t3.micro/db.t4g.micro) y unos pocos GB de comprobantes en S3 suelen costar pocos dólares al mes fuera de cualquier periodo gratuito — muy por debajo de lo que cuesta un disco persistente de pago en la mayoría de plataformas.

### 2. Crea el usuario IAM para el bucket de S3

1. **IAM** → **Users** → **Create user**. Nombre: por ejemplo `erp-harraso-s3`. NO le des acceso a la consola (solo necesita acceso "programático").
2. En permisos, elige **Attach policies directly** → **Create policy** → pestaña **JSON**, y pega una política que solo permita leer/escribir en el bucket que vas a crear en el siguiente paso (reemplaza `harraso-erp-comprobantes` por el nombre que le vayas a poner):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": "arn:aws:s3:::harraso-erp-comprobantes/*"
       }
     ]
   }
   ```
3. Termina de crear el usuario, ábrelo, pestaña **Security credentials** → **Create access key** → elige "Application running outside AWS" → copia el **Access key ID** y el **Secret access key** (el secreto solo se muestra una vez — guárdalo ahora, lo vas a necesitar en el paso 5).

### 3. Crea el bucket de S3 (comprobantes de gastos)

1. **S3** → **Create bucket**. Nombre único globalmente, por ejemplo `harraso-erp-comprobantes` (debe coincidir exactamente con el que pusiste en la política IAM).
2. Región: la misma que elegiste arriba.
3. **Block all public access**: déjalo **activado** (por defecto) — el bucket debe ser privado; la app genera enlaces temporales (5 minutos) para que cada usuario vea su comprobante solo después de que la aplicación ya comprobó sus permisos, nunca por acceso público directo.
4. **Bucket Versioning**: opcional, pero recomendable si quieres poder recuperar un comprobante si algo lo sobrescribe por error.
5. **Default encryption**: déjalo en **SSE-S3** (por defecto) — la app ya pide cifrado en cada archivo que sube.
6. Crea el bucket.

### 4. Crea la base de datos en Amazon RDS (PostgreSQL)

1. **RDS** → **Create database**.
2. **Engine options**: PostgreSQL (la versión más reciente disponible).
3. **Templates**: **Free tier** si tu cuenta todavía califica, o **Dev/Test** si no.
4. **DB instance identifier**: por ejemplo `harraso-erp-db`.
5. **Master username**: por ejemplo `erp_admin`. **Master password**: genera una larga y aleatoria y guárdala (ej. `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`).
6. **Instance configuration**: la más pequeña disponible (db.t3.micro o db.t4g.micro alcanza de sobra para este proyecto).
7. **Storage**: 20 GB gp3 (de sobra para empezar; se puede crecer después sin recrear la base).
8. **Connectivity**:
   - **Public access**: **Yes** — Render no corre dentro de tu VPC de AWS, así que la base necesita ser alcanzable desde internet (protegida por el security group del siguiente punto y por la contraseña).
   - **VPC security group**: crea uno nuevo, por ejemplo `harraso-erp-db-sg`.
9. Crea la base. Tarda unos minutos en quedar disponible.
10. Una vez creada, ábrela y en la pestaña **Connectivity & security** anota el **Endpoint** (algo como `harraso-erp-db.xxxxxxxxxx.us-east-1.rds.amazonaws.com`).
11. Abre el security group `harraso-erp-db-sg` (**EC2** → **Security Groups**) → **Inbound rules** → **Edit** → agrega una regla: **Type** PostgreSQL, **Port** 5432, **Source**: `0.0.0.0/0` (cualquier IP — Render no publica un rango fijo de IPs salientes en el plan gratuito). Esto es seguro porque el acceso real sigue exigiendo la contraseña de la base; si más adelante usas un plan de Render con IP saliente fija, puedes restringir el Source a esa IP para una capa extra de seguridad.

### 5. Configura las variables de entorno en Render

En tu Web Service de Render → **Environment**, agrega:

| Variable | Valor |
|---|---|
| `DATABASE_URL` | `postgresql://erp_admin:TU_PASSWORD@TU_ENDPOINT:5432/postgres` — si tu contraseña tiene símbolos especiales (`!`, `@`, `/`, `#`, etc.) hay que codificarlos (ej. `!` → `%21`), si no la conexión falla |
| `AWS_S3_BUCKET` | `harraso-erp-comprobantes` |
| `AWS_ACCESS_KEY_ID` | el Access key ID del usuario IAM (paso 2) |
| `AWS_SECRET_ACCESS_KEY` | el Secret access key del usuario IAM (paso 2) |
| `AWS_DEFAULT_REGION` | la región que elegiste (ej. `us-east-1`) |
| `AUTO_SEED_DEMO` | `0` — **importante**: ya con una base persistente de verdad, no quieres que la app recree los usuarios de ejemplo encima de tus datos reales |

Guarda los cambios — Render vuelve a desplegar automáticamente. Al arrancar, la app crea sola todas las tablas en tu base de Postgres (igual que hoy hace con SQLite) — no hace falta correr ningún script aparte.

**Nota sobre la versión de Python (agosto 2026)**: si al desplegar ves en los logs de Render un error como `ImportError: ... undefined symbol: _PyInterpreterState_Get` al importar `psycopg2`, es porque Render está usando una versión de Python demasiado nueva (ej. 3.14) para la que el paquete `psycopg2-binary` todavía no tiene una versión compatible — no tiene que ver con tus credenciales de AWS. El proyecto ya trae un archivo `.python-version` (fijado en `3.12.8`) para que Render use esa versión automáticamente; si tu servicio ya estaba creado antes de que existiera ese archivo, agrega además la variable `PYTHON_VERSION` = `3.12.8` en Render → Environment para forzarlo de inmediato.

### Verificación después de desplegar

1. Entra a la URL de tu app y confirma que el login funciona (si `AUTO_SEED_DEMO=0` y la base es nueva, no habrá usuarios todavía — pon `AUTO_SEED_DEMO=1` en el primer despliegue para que se cree el usuario Administrador, entra, cambia la contraseña, y luego vuelve a poner `AUTO_SEED_DEMO=0`).
2. En **Liquidaciones**, registra un gasto con un comprobante adjunto y confirma que puedes volver a verlo — eso confirma que S3 está funcionando.
3. Reinicia manualmente el servicio en Render (**Manual Deploy** → **Deploy latest commit**, o simplemente espera a que se duerma y despierte) y confirma que los datos siguen ahí — eso confirma que RDS está funcionando (ya no depende del disco efímero).

### Notas de seguridad y costos

- Nunca compartas el `AWS_SECRET_ACCESS_KEY` ni la contraseña de la base — viven solo como variables de entorno en Render, igual que las demás credenciales del proyecto (Frotcom, SUNAT, decolecta.com).
- El bucket de S3 es privado: nadie puede leer un comprobante sin pasar antes por el login y los permisos de la propia aplicación.
- Revisa el **Billing Dashboard** de AWS cada tanto los primeros meses para confirmar que el gasto es el esperado, sobre todo si dejaste pasar el periodo de capa gratuita.
- Si por lo que sea quieres volver atrás, basta con borrar (o vaciar) `DATABASE_URL` y `AWS_S3_BUCKET` en Render — la app vuelve a SQLite + disco local sin ningún otro cambio.

## Estructura del proyecto

```
erp-transporte/
├── app/
│   ├── __init__.py         # fábrica de la aplicación Flask
│   ├── auth.py              # login, sesiones y permisos por rol
│   ├── db.py                 # acceso a SQLite
│   ├── helpers.py            # utilidades (fechas, montos, códigos correlativos, pretty_label)
│   ├── accounting.py          # oficinas, tipos de documento y columnas de la liquidación contable
│   ├── reports.py             # generación de reportes en Excel (gastos, comisiones, liquidación)
│   ├── seed_data.py           # datos y catálogos de ejemplo (usados por seed.py y por el auto-seed)
│   ├── schema.sql            # esquema de la base de datos
│   ├── integrations/
│   │   ├── frotcom.py          # cliente de la API de Frotcom (GPS) — ver aviso en el archivo
│   │   ├── sunat_ose.py         # cliente OSE para facturación electrónica SUNAT — ver aviso en el archivo
│   │   ├── sunat_exchange_rate.py # tipo de cambio SUNAT del día (para la liquidación contable)
│   │   └── sunat_ruc.py           # consulta de RUC (autocompletar razón social del proveedor)
│   ├── routes/                # un blueprint por módulo
│   │   ├── dashboard.py
│   │   ├── clientes.py
│   │   ├── flota.py
│   │   ├── viajes.py
│   │   ├── liquidaciones.py       # gastos, viáticos, presupuestos, conceptos y liquidación contable
│   │   ├── mantenimiento.py
│   │   ├── inventarios.py        # repuestos con stock, proveedores y compras
│   │   ├── facturacion.py
│   │   ├── guias.py              # guías de remisión electrónica (modalidad Transportista)
│   │   ├── inspecciones.py        # checklist de inspección de unidades
│   │   ├── rutas.py                # rutas frecuentes con viáticos predeterminados
│   │   ├── usuarios.py
│   │   ├── catalogos.py         # catálogos editables (conceptos de mantenimiento, ítems de inspección)
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
