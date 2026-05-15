# Hardening de producción — trestresPOS

Estado al cierre de sesión 2026-05-15. Este documento es la fuente única
para continuar el endurecimiento del software de cara a operación
productiva real (clientes facturando dinero todos los días).

**Contexto**: el software ya está apto para **certificación SII**. Esta
fase ataca los gaps que aparecen solo en **producción real**: durabilidad
ante caídas, fallos transitorios del SII, multi-tenant concurrente,
auditabilidad legal, seguridad de secretos, recovery automático.

## Stack de cambios completados en esta sesión

### 🔴 Tier S — Seguridad (10/10 ✅)

| # | Cambio | Archivo principal |
|---|--------|-------------------|
| S1 | Módulo cifrado Fernet + master key con fail-fast en producción | `crumbpos/core/security/cifrado.py` |
| S2 | `cert_password` cifrado en BD vía `EncryptedString` TypeDecorator | `crumbpos/db/types.py`, `db/models.py:50` |
| S3 | `cert_data` (.pfx) cifrado en BD vía `EncryptedText` | `db/models.py:49` |
| S4 | `caf_xml_raw` cifrado en BD (clave privada del timbre) | `db/models.py:448` |
| S5 | Boot falla si `JWT_SECRET` o `SUPER_ADMIN_PASSWORD` son defaults en prod | `api/dependencies.py:32`, `api/app.py:30` |
| S6 | CORS desde env `CRUMBPOS_ALLOWED_ORIGINS`, prohibido `*` en prod | `api/app.py:93` |
| S7 | Rate limit login con lockout exponencial (60s→1h cap) | `core/security/rate_limit.py` |
| S8 | `must_change_password` + `password_changed_at` con migración idempotente | `db/multi_tenant.py:235`, `routers/auth.py` |
| S9 | Parser XML endurecido contra XXE/billion laughs en uploads (CAF, intercambio) | `core/security/xml_safe.py` |
| S10 | Validación RUT con regex `^\d{1,8}-[0-9Kk]$` cierra path traversal | `utils/rut.py`, `api/dependencies.py:177` |

### 🟡 Tier A — Operacional (5/9 ✅ los principales)

| # | Cambio | Archivo |
|---|--------|---------|
| A3 | `DteEmitido` con `usuario_id`, `caja_id`, `ip_origen`, `user_agent`, `timestamp_envio` | `db/models.py:577` |
| A1+A4+A9 | `_persist_dte_emitido` ahora persiste SIEMPRE (incluso rechazos), UPSERT por (empresa, tipo, folio), preserva `timestamp_envio` para reintentos idempotentes | `api/routers/facturacion.py:129` |
| A5 | `_loop_polling_sii` agendado cada 30 min — consulta `QueryEstUp` + `QueryEstDte` por empresa activa, token boleta lazy | `api/scheduler.py:_loop_polling_sii` |

### Stats

- **569 tests passing** (22 nuevos en módulos de seguridad)
- 25 archivos modificados, 5 archivos nuevos
- 1372 líneas añadidas, 182 eliminadas

## Pendientes para próxima(s) sesión(es)

### 🟡 Tier A — Operacional restante (3 fixes, ~4h)

#### A6 — RCOF backfill + reintento intra-día
**Síntoma actual**: si el SII está caído entre 22:30 y 22:34 (cuando dispara
el scheduler RCOF), el RCOF de ese día se pierde. Boletas emitidas después
de las 22:30 quedan sin reporte.
**Solución**:
1. Persistir `RcofDiario` con estado `pendiente/enviado/error_envio`.
2. Loop de reintento intra-día cada 30 min hasta 23:55 si no se envió.
3. Backfill al boot: revisar RCOFs de los últimos 7 días, si alguno
   quedó `error_envio` con boletas pendientes, reintentar.

#### A7 — IECV mensual catch-up al boot
**Síntoma actual**: si el server está caído el día 1 a las 09:00, el
recordatorio IECV mensual no dispara hasta el día 1 del mes siguiente.
**Solución**: persistir `ultimo_iecv_recordatorio_periodo` (YYYY-MM). Al
boot, si difiere del periodo anterior, disparar inmediatamente.

#### A8 — Reintento automático ante token SII expirado
**Síntoma actual**: si una emisión batch tarda más de 30 min y el token
SOAP expira, el envío falla sin reintentar con token nuevo.
**Solución**: detectar respuesta `STATUS=7` / "TOKEN INVALIDO" en
`enviar_dte` → invalidar cache del token → reintentar UNA VEZ.

### 🟢 Tier B — Compliance legal (3 fixes, ~5h)

#### B1 — Tabla `AuditoriaEvento` append-only
Tabla con `id, timestamp, empresa_rut, user_id, ip, evento, payload_json,
hash_prev` (cadena hash para inmutabilidad). Trigger SQL `BEFORE UPDATE`
y `BEFORE DELETE` que `RAISE EXCEPTION`. Eventos: login, emisión, anulación,
shadow_session_inicio/fin, cert_upload, password_change, baja_empresa.

#### B2 — Política WORM para conservación 6 años SII
- Trigger `BEFORE DELETE` en `dte_emitido`: bloquear si
  `created_at > now() - 6 años`.
- Endpoint admin para exportar DTEs antes de cualquier eliminación.
- Documentar política de retención.

#### B3 — Endpoint ARCO (Ley 19.628 datos personales)
`GET /api/datos-personales/me` (export JSON de todo lo del usuario),
`POST /api/datos-personales/solicitud-cancelacion` (ticket interno —
no permite borrado de DTEs por retención obligatoria).

### 🔵 Tier C — Módulos faltantes (3 fixes, ~10h)

#### C1 — CAF con marcador `ambiente` (cert/prod)
**Síntoma**: hoy un CAF de certificación puede usarse contra producción y
viceversa, quemando folios. **Solución**: agregar columna `ambiente` a
`CafFolio`, validar al upload y al consumir. Migración: backfill desde el
`ambiente_sii` de la empresa al momento del upload.

#### C2 — Endpoint anulación NC automatizada
`POST /api/facturacion/{folio}/anular` que:
1. Carga el DTE original (T33/T34/T39 etc.).
2. Genera una NC (T61) con `CodRef=1` y todos los items replicados.
3. Persiste relación `Venta.estado="anulada"` + `dte_anulado_por` (FK
   en `DteEmitido`).

#### C3 — Módulo recepción DTE proveedores (~6-8h)
Tabla `DteRecibido` + endpoint `POST /api/dtes-recibidos/upload` que:
1. Parsea XML con `fromstring_safe` (Tier S9 ✅).
2. Valida firma y XSD.
3. Persiste con `estado_recepcion=pendiente`.
4. Genera acuse de recibo automático (`EnvioRecibos`) firmado y
   enviado al RUT emisor.
5. Endpoint `POST /api/dtes-recibidos/{id}/reclamar` para reclamar
   contenido (plazo 8 días Ley 19.983) o aceptar mercadería (30 días).

### ⚪ Tier D — Robustez (4 fixes, ~4h)

#### D1 — Migrar `enviar_dte` a `httpx.AsyncClient`
Hoy `enviar_dte` usa `requests.post` + `time.sleep` síncrono. Con 5
reintentos × 50s + 90s timeout = hasta 340s por petición ocupando un
thread del pool. Migrar a `httpx.AsyncClient` + handlers `async def`
libera concurrencia.

#### D2 — `PRAGMA synchronous=FULL`
Hoy `multi_tenant.py:782` setea `synchronous=NORMAL` que puede perder
transacciones recientes ante crash del SO. Para datos contables (valor
legal) usar `FULL`.

#### D3 — Stack traces no se filtran al cliente
Hoy `emision_dte.py:1252` retorna `traceback.format_exc()` en
`result.error`. Exposición de paths internos, nombres de tabla, hosts SII.
Reemplazar por `error_id` único y log server-side detallado.

#### D4 — Tamaño máximo en upload de cert .pfx
Hoy `empresas.py:436` solo valida extensión `.pfx/.p12`. Agregar cap
(ej. 100 KB típico del PFX SII).

## Cómo retomar

1. **Próxima sesión**: leer este documento + `MEMORY.md` del proyecto.
2. **Orden recomendado**: A6 → A7 → A8 → D2 → D3 → D4 (quick wins) →
   C1 → C2 → B1 → B2 → B3 → D1 → C3 (el más grande, al final).
3. **Tests**: cada fix con su test. Suite full debe quedar verde antes
   de pasar al siguiente.
4. **Producción real**: no abrir piloto con clientes hasta completar
   Tier A + Tier B (mínimo).

## Configuración de entorno (producción)

Variables de entorno obligatorias en producción:

```bash
export CRUMBPOS_ENV=production
export CRUMBPOS_MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(64))")
export SUPER_ADMIN_PASSWORD=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
export SUPER_ADMIN_EMAIL=matias@trestres.cl
export CRUMBPOS_ALLOWED_ORIGINS="https://app.trestrespos.cl,https://admin.trestrespos.cl"
```

**Crítico**: hacer backup de `CRUMBPOS_MASTER_KEY` en cold storage. Si
se pierde, TODOS los secretos cifrados (certificados, CAFs) quedan
inaccesibles.

## Variables opcionales

```bash
export RCOF_HORA=22
export RCOF_MINUTO=30
export IECV_HORA=9
export IECV_MINUTO=0
export POLLING_INTERVALO_MIN=30
```
