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
