"""crumbpos.admin — operaciones cross-ambiente ejecutadas por super admin.

Este paquete contiene acciones administrativas que tocan **ambos** ambientes
de una empresa (certificación + producción) o mueven archivos en disco
fuera del core. Está aislado del core precisamente porque la regla R4
prohíbe mezclar ambientes o tocar `produccion.db` desde cualquier otro
lado: las excepciones narrow-scoped viven aquí y en ningún otro módulo.

Módulos:
    eliminacion_empresa — baja soft, restauración y baja hard con
                          exportación previa obligatoria a ZIP.

No importar desde routers/ ni desde core/ directamente. Los endpoints
que invocan este paquete viven en crumbpos/api/routers/admin_empresas.py
(o el router oficial de super admin).
"""
