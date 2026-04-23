# CLAUDE.md — trestresPOS

Sistema POS multi-tenant con facturación electrónica SII Chile.

## Stack técnico

- **Backend:** FastAPI + SQLAlchemy + SQLite (una base por empresa)
- **Firma:** RSA-SHA1 via `cryptography` o `lxml` con xmlsig
- **Comunicación SII:** SOAP sobre HTTPS
- **Python:** 3.10+

## Reglas críticas del proyecto

1. **No parches**: todo bug se resuelve en el core, nunca con scripts externos.
2. **Textos exactos**: nunca modificar nombres de ítems ni caracteres en XML.
3. **No envíos parciales**: sets completos en 1 envío. No quemar folios innecesariamente.
4. **Validar antes de enviar**: firma y esquema XSD deben validar antes de cualquier envío al SII.
5. **Sin retrocesos**: guardar avances en memoria antes de actuar.
6. **Español neutro**: código, comentarios y UI en español neutro.
7. **CAFs centralizados**: upload único al servidor, asignación manual a sucursales.
8. **EPR ≠ aprobado**: el SII devuelve EPR al recibir, pero falta consultar revisión real.

## Archivos de memoria relevantes

| Archivo | Contenido |
|---------|-----------|
| `sii_formato_dte.md` | Estructura XML completa de todos los tipos de DTE |
| `sii_formato_libros.md` | Libros IECV, LibroGuia, LibroBoleta (RCOF) |
| `sii_comunicacion.md` | Autenticación, envío, consulta estado |
| `sii_certificacion.md` | Proceso de certificación y sets de prueba |
| `sii_muestras_impresas.md` | Representación impresa (PDF/papel) de DTEs |

## Estructura del proyecto

```
trestresPOS/
├── app/
│   ├── core/           # Motor DTE: construcción XML, firma, envío
│   ├── models/         # SQLAlchemy models
│   ├── routers/        # FastAPI routers
│   └── services/       # Servicios de negocio
├── docs/
│   └── sii/            # Documentación oficial SII (PDFs)
│       ├── instructivo_tecnico/
│       │   ├── descripcion_de_formato_de_docuemntos_electronicos/
│       │   ├── formato_XLM_de_docuementos_electronicos/
│       │   └── automatizacion_de_procesos/
│       └── OLD/        # Schemas y ejemplos oficiales SII (REFERENCIA ÚNICA)
└── CLAUDE.md
```

## Tipos de DTE soportados

| Código | Descripción |
|--------|-------------|
| 33 | Factura electrónica |
| 34 | Factura no afecta o exenta electrónica |
| 39 | Boleta electrónica |
| 41 | Boleta no afecta o exenta electrónica |
| 52 | Guía de despacho electrónica |
| 56 | Nota de débito electrónica |
| 61 | Nota de crédito electrónica |

## Ambientes SII

| Ambiente | Host |
|----------|------|
| Certificación | `maullin.sii.cl` |
| Producción | `palena.sii.cl` |

## Endpoints principales

- Autenticación semilla: `https://{host}/DTEWS/CrSeed.jws`
- Autenticación token: `https://{host}/DTEWS/GetTokenFromSeed.jws`
- Upload DTE: `https://{host}/cgi_dte/UPL/DTEUpload`
- Consulta estado DTE: `https://{host}/DTEWS/QueryEstDte.jws`
- Consulta estado envío: `https://{host}/DTEWS/QueryEstUp.jws`
