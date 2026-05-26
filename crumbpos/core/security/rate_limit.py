"""Rate limiter in-memory simple para endpoints sensibles (login, etc.).

Diseño:
- **Sliding window** por clave: máx N intentos en M segundos.
- **Storage**: dict en memoria. Para multi-worker o multi-host hay que
  migrar a Redis. Para el caso típico de SaaS pequeño (1 servidor uvicorn,
  4 workers) basta con memoria por proceso si se acepta que un atacante
  puede multiplicar su tasa por el número de workers.
- **Lockout exponencial**: si se llega al límite, el siguiente lockout
  dura 2× el anterior (capeado a 1 hora).
- **Clave**: típicamente IP + email (intentar locking por solo IP es DoS;
  por solo email permite reset por IP rotating).

Uso:
    limiter = RateLimiter(max_attempts=5, window_seconds=60)

    @router.post("/login")
    def login(body: LoginRequest, request: Request):
        key = f"{request.client.host}:{body.email}"
        retry_after = limiter.check(key)
        if retry_after:
            raise HTTPException(429, f"Demasiados intentos. Reintenta en {retry_after}s")
        # ... resto del login
        if login_falla:
            limiter.fail(key)
        else:
            limiter.success(key)
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Estado:
    intentos: deque = field(default_factory=deque)  # timestamps recientes
    lockout_hasta: float = 0.0  # epoch — si > now(), está bloqueado
    nivel_lockout: int = 0  # 0=ninguno, 1=primera vez, 2,3... exponencial


class RateLimiter:
    """Limitador in-memory thread-safe."""

    LOCKOUT_BASE_SEC = 60      # primer lockout: 60s
    LOCKOUT_MAX_SEC = 3600     # cap: 1 hora

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._estado: dict[str, _Estado] = {}
        self._lock = threading.Lock()

    def check(self, clave: str) -> int:
        """Devuelve segundos de retry-after si la clave está bloqueada, sino 0.

        Llamar ANTES del intento. Si retorna > 0, devolver 429 sin procesar.
        """
        with self._lock:
            ahora = time.time()
            est = self._estado.get(clave)
            if est is None:
                return 0
            # Ventana sliding: purgar intentos viejos
            limite = ahora - self.window
            while est.intentos and est.intentos[0] < limite:
                est.intentos.popleft()
            # Lockout activo
            if est.lockout_hasta > ahora:
                return int(est.lockout_hasta - ahora) + 1
            return 0

    def fail(self, clave: str) -> int:
        """Registra un intento fallido. Devuelve retry-after si gatilla lockout.

        Llamar DESPUÉS de comprobar que las credenciales son incorrectas.
        """
        with self._lock:
            ahora = time.time()
            est = self._estado.setdefault(clave, _Estado())
            est.intentos.append(ahora)
            # Si superamos el máximo, activar lockout exponencial
            if len(est.intentos) >= self.max_attempts:
                est.nivel_lockout += 1
                duracion = min(
                    self.LOCKOUT_BASE_SEC * (2 ** (est.nivel_lockout - 1)),
                    self.LOCKOUT_MAX_SEC,
                )
                est.lockout_hasta = ahora + duracion
                est.intentos.clear()
                return int(duracion)
            return 0

    def success(self, clave: str) -> None:
        """Resetea el estado tras un intento exitoso."""
        with self._lock:
            self._estado.pop(clave, None)

    def _reset(self) -> None:
        """Resetea todo el estado. Solo para tests."""
        with self._lock:
            self._estado.clear()


# Instancia global compartida para login.
# Política: 5 intentos en 60s → lockout exponencial 60s, 120s, 240s, 480s, ..., max 1h.
login_limiter = RateLimiter(max_attempts=5, window_seconds=60)


# ══════════════════════════════════════════════════════════════════
# REQUEST LIMITER — cuenta TODAS las solicitudes (no solo fallos)
# ══════════════════════════════════════════════════════════════════

class RequestLimiter:
    """Sliding-window counter para limitar tasa de solicitudes.

    A diferencia de RateLimiter, cuenta TODAS las solicitudes (no solo
    las fallidas). Sin lockout exponencial — solo ventana deslizante.

    Uso:
        limiter = RequestLimiter(max_requests=60, window_seconds=60)

        allowed, retry_after = limiter.is_allowed("empresa:77829149-5")
        if not allowed:
            raise HTTPException(429, f"Límite alcanzado. Reintenta en {retry_after}s")
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._ventanas: dict[str, deque] = {}
        self._lock = threading.Lock()

    def is_allowed(self, clave: str) -> tuple[bool, int]:
        """Registra la solicitud y devuelve (permitido, retry_after_segundos).

        Siempre registra la solicitud; si supera el límite devuelve False
        y los segundos hasta que se libere el slot más antiguo.
        """
        with self._lock:
            ahora = time.time()
            limite = ahora - self.window

            if clave not in self._ventanas:
                self._ventanas[clave] = deque()

            ventana = self._ventanas[clave]
            # Purgar solicitudes fuera de la ventana
            while ventana and ventana[0] < limite:
                ventana.popleft()

            if len(ventana) >= self.max_requests:
                # Tiempo hasta que salga la solicitud más antigua
                retry_after = int(ventana[0] - limite) + 1
                return False, retry_after

            ventana.append(ahora)
            return True, 0

    def _reset(self) -> None:
        """Resetea todo el estado. Solo para tests."""
        with self._lock:
            self._ventanas.clear()


# ── Instancias nombradas ───────────────────────────────────────────

# Emisión DTE y generación de libros/RCOF (por empresa_rut).
# 60 operaciones/min = 1/seg — suficiente para restaurante ocupado.
dte_limiter = RequestLimiter(max_requests=60, window_seconds=60)

# Consultas de estado al SII (por empresa_rut).
# 30 polls/min — evita que SII bloquee por flood.
sii_polling_limiter = RequestLimiter(max_requests=30, window_seconds=60)

# Cambio de contraseña (por user_id).
# 5 cambios cada 10 minutos.
password_change_limiter = RequestLimiter(max_requests=5, window_seconds=600)

# Rate limit por IP para toda la API.
# 600 req/min = 10/seg por IP — protección contra DoS basal.
ip_limiter = RequestLimiter(max_requests=600, window_seconds=60)

# Escrituras POS por sucursal: ventas + sync push.
# 120/min = 2/seg — holgado para restaurante ocupado.
pos_write_limiter = RequestLimiter(max_requests=120, window_seconds=60)

# Pull completo por sucursal: descarga inicial costosa.
# 10/min — no debería ocurrir más de 1 vez en reinstalación.
pos_pull_completo_limiter = RequestLimiter(max_requests=10, window_seconds=60)
