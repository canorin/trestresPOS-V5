"""Tests del rate limiter para login."""
from __future__ import annotations

import time

import pytest

from crumbpos.core.security.rate_limit import RateLimiter


class TestRateLimiter:
    def test_pocos_intentos_no_bloquean(self):
        rl = RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(4):
            assert rl.check("ip1:user@x.cl") == 0
            rl.fail("ip1:user@x.cl")

    def test_quinto_intento_dispara_lockout(self):
        rl = RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(4):
            rl.fail("ip1:user@x.cl")
            assert rl.check("ip1:user@x.cl") == 0
        # 5to intento dispara lockout
        retry = rl.fail("ip1:user@x.cl")
        assert retry >= 60  # 1er lockout = 60s
        # check ahora retorna retry-after > 0
        retry_check = rl.check("ip1:user@x.cl")
        assert retry_check > 0

    def test_success_resetea_estado(self):
        rl = RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(3):
            rl.fail("ip1:user@x.cl")
        rl.success("ip1:user@x.cl")
        # ahora puede volver a fallar 4 veces sin lockout
        for _ in range(4):
            assert rl.check("ip1:user@x.cl") == 0
            rl.fail("ip1:user@x.cl")

    def test_claves_distintas_no_interfieren(self):
        rl = RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(10):
            rl.fail("ip1:user@x.cl")
        # ip2 sigue limpia
        assert rl.check("ip2:user@x.cl") == 0

    def test_lockout_exponencial(self):
        rl = RateLimiter(max_attempts=2, window_seconds=60)
        rl.fail("k")
        primer_lockout = rl.fail("k")  # 1er lockout
        # Avanzar tiempo manualmente: limpiamos el estado y forzamos nuevo lockout
        rl._estado["k"].lockout_hasta = 0  # simular que pasó el primer lockout
        rl._estado["k"].intentos.clear()
        rl.fail("k")
        segundo_lockout = rl.fail("k")  # 2do lockout
        assert segundo_lockout >= primer_lockout  # debe ser >= (exponencial)
        # En la práctica 2do es 2x el 1ro (60→120)
        assert segundo_lockout >= primer_lockout * 2 - 5  # tolerancia

    def test_lockout_no_excede_cap_de_una_hora(self):
        """Tras muchos lockouts seguidos, el exponencial se capea en 1h."""
        rl = RateLimiter(max_attempts=1, window_seconds=60)
        # Disparar el primer lockout
        rl.fail("k")
        # Forzar simulación de muchos lockouts subiendo el nivel manualmente
        # (en producción esto pasaría tras múltiples ciclos de fallo y espera)
        last_lockout = 0
        for _ in range(20):
            # Reset solo lockout_hasta para permitir nuevo fail
            rl._estado["k"].lockout_hasta = 0
            rl._estado["k"].intentos.clear()
            last_lockout = rl.fail("k")
        # El último lockout no debe exceder 1 hora (3600s)
        assert last_lockout <= 3600
        # Y debe estar capeado en 3600 (no algo absurdamente grande)
        assert last_lockout == 3600

    def test_ventana_sliding_purga_intentos_viejos(self):
        rl = RateLimiter(max_attempts=3, window_seconds=1)
        for _ in range(2):
            rl.fail("k")
        # Esperar a que pase la ventana
        time.sleep(1.2)
        # Ahora podemos volver a hacer 2 intentos sin lockout
        assert rl.check("k") == 0
        rl.fail("k")
        assert rl.check("k") == 0
