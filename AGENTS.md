# AGENTS.md — Reglas del proyecto trestresPOS

Este archivo es el contrato operativo entre Matías (humano) y cualquier agente
IA que trabaje en este repo. Las reglas **no son opcionales**. Existen porque
la experiencia demostró que los principios que viven solamente en conversación
se olvidan entre turnos, así que los movimos a un archivo que se lee al inicio
de cada sesión y que los hooks y los tests consumen.

El proyecto es un **software de producción** de punto de venta con facturación
electrónica SII Chile. La certificación es un trámite obligatorio de una sola
vez — el objetivo nunca es "pasar la certificación", el objetivo es que el
software no falle en producción. La certificación es un fuzz test gratuito del
core; si el set de pruebas del SII encuentra un bug, ese bug existe también
para el primer cliente real, y ahí es donde se arregla.

---

## Estructura del core

Toda la lógica de negocio DTE vive en rutas únicas. No hay copias paralelas
para certificación y producción — la diferencia entre ambos vive únicamente en
configuración (URL del SII, qué archivo SQLite se abre).

| Ruta | Qué hace | Comparte cert/prod |
|---|---|---|
| `crumbpos/core/dte/` | Construcción de DTE (EnvioDTE, DTE, documentos) | Sí — único |
| `crumbpos/core/caf/` | Lectura y asignación de CAFs | Sí — único |
| `crumbpos/core/firma/` | Firma digital XML-DSig | Sí — único |
| `crumbpos/core/libros/` | Libros IECV / LGC | Sí — único |
| `crumbpos/core/rcof/` | Reporte consumo de folios | Sí — único |
| `crumbpos/core/sii_client/` | Cliente HTTP al SII | Sí — único (URL cambia por ambiente) |
| `crumbpos/core/envio_receptor/` | Respuestas al receptor del DTE | Sí — único |
| `crumbpos/core/impresion/` | PDFs, representaciones impresas | Sí — único |
| `crumbpos/db/models.py` | Modelos SQLAlchemy (DTE, empresa, sucursal, CAFs) | Sí — único |
| `crumbpos/db/multi_tenant.py` | `get_empresa_db_session(rut, ambiente)` | Aquí vive el switch |
| `crumbpos/models/dte_models.py` | Pydantic schemas de la API | Sí — único |
| `crumbpos/utils/rut.py` | Utilidades RUT | Sí — único |
| `crumbpos/api/routers/` | Endpoints FastAPI | Ver excepciones abajo |
| `crumbpos/certificacion/` | Parser del set de pruebas, wizard | Solo certificación |

**Routers que SÍ pueden leer `ambiente` literal** (porque son meta-gestores de
los ambientes, no operan sobre datos fiscales):
- `crumbpos/api/routers/empresas.py`
- `crumbpos/api/routers/certificacion.py`

**Todos los demás routers** (`ventas.py`, `facturacion.py`, `inventario.py`,
`reportes.py`, `libros.py`, `folios.py`, `rcof.py`, `clientes.py`, etc.) deben
obtener el ambiente exclusivamente desde `EmpresaRegistro.ambiente_activo`
vía la dependencia `get_tenant`. **Nunca** pasar el string `"certificacion"` o
`"produccion"` literal a `get_empresa_db_session` dentro de esos archivos.

---

## Reglas

### R1 — Un solo código, dos almacenes

La lógica de negocio no bifurca por ambiente. Prohibido escribir
`if ambiente == "certificacion"` (o `ambiente_activo == "certificacion"`,
`tenant.ambiente == ...`, equivalentes) dentro de:

- `crumbpos/core/`
- `crumbpos/db/`
- `crumbpos/models/`
- `crumbpos/utils/`
- Cualquier archivo bajo `crumbpos/api/routers/` **excepto**
  `certificacion.py` y `empresas.py`.

Si aparece la tentación de escribir ese `if`, la respuesta correcta es
extraer la diferencia a configuración (una URL, un path, un flag en
`EmpresaRegistro`), nunca a una rama de código.

**Ejemplo prohibido:**
```python
# crumbpos/core/dte/constructor.py
if ambiente == "certificacion":
    tasa_iva = 19  # hack para pasar el set SII
else:
    tasa_iva = empresa.tasa_iva
```

**Ejemplo correcto:**
```python
# La tasa IVA vive en empresa (misma lógica en ambos ambientes)
tasa_iva = empresa.tasa_iva  # 19 en Chile, único lugar
```

### R2 — Fixes en el core, no parches

Prohibido crear archivos cuyo nombre sugiera parche puntual:
`fix_*.py`, `parche_*.py`, `hotfix_*.py`, `workaround_*.py`,
`arreglar_*.py`, `temp_*.py`, `emergency_*.py`, `rapido_*.py`.

La excepción son migraciones legítimas de datos (`crumbpos/scripts/migrar_*.py`)
que tienen propósito documentado y se corren una vez. Si dudas, no es migración.

Prohibido editar datos del set de pruebas del SII (`temp/cert1/SET/`, OLD/ de
referencia) para esquivar un bug. El set de pruebas es input, no código.

Memoria: `feedback_sin_parches.md`, `feedback_textos_exactos.md`.

### R3 — Fix por causa raíz, no por síntoma

Cuando el SII rechace un documento, cuando un test falle, cuando un cliente
reporte un problema, el ciclo obligatorio es:

1. **Leer el error literal** (glosa SII, traceback, captura).
2. **Traducir a pregunta de producción**: *"si mañana un cliente real tiene
   exactamente este mismo input, ¿qué pasa?"*
3. **Identificar la causa raíz en el core** (no en los datos, no en el UI).
4. **Arreglar ahí**, en el único lugar donde vive esa lógica.
5. **Agregar un test de regresión** en `tests/` que reproduzca el input
   original y verifique que ahora pasa. Sin test, el fix no existe.
6. **Validar que el fix no rompió otra cosa** (correr la suite completa).

Está prohibido "ajustar" el input para que el bug no se dispare. Está prohibido
relajar una validación existente para que pase algo que antes rechazaba — si
la validación es correcta, el input es incorrecto; si la validación es
incorrecta, se arregla la validación con test que documente el porqué.

Memoria: `feedback_validar_antes_enviar.md`, `project_auditoria_core.md`.

### R4 — Nunca mezclar documentos entre ambientes

Los DTEs de certificación son ficticios (folios de CAFs ambiente cert, firmados
contra `maullin.sii.cl`). Los DTEs de producción son fiscalmente reales (folios
de CAFs ambiente prod, firmados contra `palena.sii.cl`). No pueden compartir
almacén, no pueden compartir backup, no pueden compartir reporte.

- **`crumbpos/certificacion/cleanup.py`** (cuando exista) solo puede abrir
  `certificacion.db`. Prohibido importar `"produccion"` o mencionar
  `produccion.db` dentro de ese módulo.
- **Routers de producción** (`ventas`, `inventario`, `reportes`, `libros`,
  `folios`, `rcof`, `clientes`, etc.) nunca pasan el literal `"certificacion"`
  a `get_empresa_db_session`. Usan `tenant.ambiente` que proviene de
  `EmpresaRegistro.ambiente_activo`.
- **`produccion.db` nunca se borra**. El hook debe bloquear cualquier comando
  que haga `rm` o `DELETE FROM` sobre ese archivo.

#### R4.a — Excepción narrow-scoped: baja de empresas

La única operación que legítimamente mueve o borra archivos dentro de `data/`
es la baja de una empresa (Fase 7). Esa operación vive en un solo archivo,
con guardas verificables automáticamente:

- **Único archivo autorizado a hacer `shutil.move`, `shutil.rmtree`,
  `os.remove`, `Path.unlink` sobre rutas dentro de `data/`**:
  `crumbpos/admin/eliminacion_empresa.py`.
- **Toda función destructiva** en ese archivo (`confirmar_baja`,
  `eliminar_definitivo`) **debe tener como primera instrucción ejecutable**
  una llamada a `_verificar_zip_descargado_o_error(rut)`. Esa llamada falla
  con `RuntimeError` si la empresa no registró en `master.db` el hash SHA-256
  del ZIP descargado. Sin esa confirmación, no hay forma de disparar una
  operación destructiva sobre `data/`.
- **El router HTTP** (`crumbpos/api/routers/baja_empresas.py`) es un wrapper
  delgado sobre el módulo — traduce HTTP a llamadas Python y errores Python
  a códigos HTTP. No toca `data/` directamente, no reimplementa los guards.
- **Los core paths, routers de producción y utilidades no importan este
  módulo.** Solo el router dedicado puede invocar sus funciones. El punto
  de entrada es `POST /api/admin/empresas/{rut}/confirmar-baja` con
  confirmación super_admin + hash SHA-256 verificado del lado del navegador.

Estas invariantes están codificadas en tres lugares:
1. `.claude/hooks/guardian.py` bloquea writes a archivos que introduzcan
   `shutil.move`/`shutil.rmtree`/`os.remove`/`Path.unlink` contra rutas en
   `data/` fuera de `eliminacion_empresa.py`.
2. `tests/test_invariantes_produccion.py::test_R4a_*` verifica por AST que
   el guard existe y que las operaciones destructivas están confinadas.
3. Este archivo (contrato leído al inicio de cada sesión).

### R5 — Sin envíos parciales al SII

Un sobre al SII contiene el set completo. Prohibido enviar un subset para
"probar si pasa un caso" — eso quema folios y distorsiona la validación del
set por parte del SII.

Memoria: `feedback_no_envios_parciales.md`.

### R6 — Preguntar antes de enviar al SII

Ningún `POST` a `palena.sii.cl` o `maullin.sii.cl` se dispara sin que Matías
confirme explícitamente en el chat. El agente construye el sobre, muestra un
resumen (cantidad de documentos, folios, totales, hash del XML) y **espera
confirmación humana** antes de enviar.

En la práctica esto significa que cualquier endpoint de envío al SII debe ser
disparado por un click humano en la UI o una confirmación explícita en el
chat, nunca por un job en background ni por una reacción automática.

Memoria: `feedback_preguntar_antes_enviar.md`.

### R7 — Español neutro

Toda comunicación con el usuario y todo texto dentro del código (strings,
mensajes de error, docstrings, comentarios, nombres de variables en español)
se escribe en español neutro. Prohibido voseo y regionalismos argentinos:
`vos`, `tenés`, `hacés`, `querés`, `sabés`, `mirá`, `andá`, `dale`.

Memoria: `feedback_espanol_neutro.md`.

### R8 — EPR no es aprobado

Cuando el SII responde `RESERVADO` / `RPR` / `Envío procesado con reservas` o
cualquier variante de "recibido, pero todavía en revisión", eso es **EPR**
(Envío Procesado con Reservas), no aprobación. El set se considera aprobado
únicamente cuando:

1. Se declara avance del set en el sistema del SII, y
2. Se consulta revisión de envío y el SII responde `ACEPTADO` o `APROBADO`.

Hasta entonces, el wizard y cualquier endpoint muestran el estado real
(`enviado`, `en_revision`) y **no** marcan `aprobado`. El paso a producción
solo se permite con `aprobado` verificado.

Memoria: `feedback_epr_vs_aprobacion.md`.

### R9 — Certificación se ejecuta desde la API

El flujo de certificación corre a través del software real (API FastAPI +
wizard), no a través de scripts standalone. Está prohibido crear scripts
aparte en `crumbpos/scripts/` o en la raíz del repo para emitir, firmar,
enviar o parsear el set "solo para certificar". Si se necesita una nueva
pieza de funcionalidad para la certificación, va al core y al router de
certificación de la API.

Memoria: `feedback_certificacion_desde_api.md`.

### R10 — No envíos a SII desde el agente

El agente IA no ejecuta comandos `curl` ni llamadas Python que toquen
`palena.sii.cl` o `maullin.sii.cl` directamente. Esas llamadas siempre pasan
por el flujo de la API (endpoint → confirmación humana → cliente SII del
core). Prohibido hacer tests manuales contra el SII desde el chat.

### R11 — Leer memoria antes de retomar

Al inicio de cada sesión y después de cualquier compactación de contexto, el
agente lee:

1. Este archivo (`AGENTS.md`).
2. El índice de memoria en `~/.claude/projects/-Users-matiasbanados-POS-NANUC/memory/MEMORY.md`.
3. En particular: `project_certificacion_estado.md` y `feedback_checklist_preaccion.md`.

No se toca código ni se envía nada al SII sin haber hecho esa lectura.

Memoria: `feedback_sin_retrocesos.md`.

### R12 — Guardar avances automáticamente

Cualquier estado intermedio del wizard o de una corrida de certificación se
persiste en `certificacion_run` en la BD tenant en el momento que se produce,
no al final. Un crash del server, una recarga del browser o un cambio de
pantalla no pueden perder trabajo ya hecho.

Memoria: `feedback_sin_retrocesos.md`.

### R13 — Textos exactos

Nombres de ítems, montos, descripciones, giros, RUTs de receptores vienen
tal cual del set de pruebas o del input del cliente. Prohibido modificar
tildes, mayúsculas, espacios, puntuación o acortar texto para que "se vea
mejor" o "calce en la celda". Si un campo no calza, se arregla el layout
del PDF, nunca el contenido.

Memoria: `feedback_textos_exactos.md`.

### R14 — OLD/ es material oficial del SII

El directorio `OLD/` contiene schemas XSD y ejemplos oficiales publicados por
el SII. Sirve como referencia de verdad cuando hay dudas sobre formato XML,
tipos de documentos, códigos. **Jamás** se toman folios, CAFs ni código de
ejecución desde `OLD/` — esos archivos son pedagógicos, no son fuentes de
datos ni dependencias del código de producción.

Memoria: `feedback_no_old.md`.

### R15 — CAFs centralizados por empresa, asignados a sucursal

Los CAFs se suben una vez por empresa al servidor. Cada CAF tiene **un solo
dueño a la vez**: una sucursal específica o el pool central del servidor.
Prohibido que un mismo CAF esté asignado a dos sucursales al mismo tiempo.
Los CAFs los gestiona el admin de la empresa, no el super_admin.

Memoria: `project_cafs_por_sucursal.md`.

### R16 — Emisión desde servidor obliga a elegir sucursal

Cuando el panel web del admin de empresa emite un DTE, obliga a seleccionar
sucursal antes de emitir porque la dirección de emisión del DTE se toma de
la sucursal. No hay "emisión genérica" sin sucursal.

Memoria: `project_panel_admin_empresa.md`.

---

## Flujo obligatorio al arreglar un bug encontrado por certificación

Cuando el SII rechace un documento del set de pruebas, el agente sigue este
procedimiento sin omitir pasos:

1. **Reportar el rechazo literal** — glosa SII, código, XML del caso, trackid.
2. **Identificar la causa raíz** — un análisis escrito de "esto falló porque X".
3. **Proponer el fix en el core** — ubicación exacta, diff, justificación en
   términos de producción ("esto también hubiera roto en producción si...").
4. **Esperar confirmación humana** de que el fix propuesto es el correcto.
5. **Aplicar el fix**.
6. **Agregar test de regresión** en `tests/test_<modulo>.py` con el input
   original.
7. **Correr la suite completa** (`pytest tests/ -q`) y pegar el resultado.
8. **Correr los invariantes de producción**
   (`pytest tests/test_invariantes_produccion.py -q`) y pegar el resultado.
9. **Recién entonces** reintentar contra el set de pruebas.

No se saltean pasos. No se "arregla y pruebo después". No se "arreglo, corro,
y si pasa ya está".

---

## Qué hacer cuando aparece una regla nueva

Cuando Matías define una regla nueva en conversación, el agente:

1. **Confirma haber entendido** con las palabras propias.
2. **Agrega la regla a este archivo** como `Rn` con descripción + ejemplo.
3. **Agrega un check al hook de Claude Code** en
   `.claude/hooks/guardian.py` si es automatizable pre-escritura.
4. **Agrega un test en `tests/test_invariantes_produccion.py`** si es
   automatizable post-escritura.
5. **Recién entonces** continúa con la tarea original.

Las reglas no se quedan solo en conversación. Siempre terminan materializadas
en tres lugares: este archivo (para lectura humana), el hook (para bloqueo
en tiempo de edición) y los tests (para auditoría continua).
