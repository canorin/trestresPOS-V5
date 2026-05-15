"""Tests para D1 — enviar_dte_async / enviar_boleta_async con httpx.

Verifica:
- Respuesta OK con TrackID extraído del XML
- Respuesta ERROR sin TrackID
- Reintento automático ante ConnectError (con asyncio.sleep mockeado)
- enviar_boleta_async parsea JSON correctamente
- enviar_boleta_async fallback a XML si SII responde no-JSON
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crumbpos.core.sii_client.envio import enviar_dte_async, enviar_boleta_async


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _mock_response(text: str, status_code: int = 200):
    """Construye un mock de httpx.Response."""
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx_client(response):
    """Parchea httpx.AsyncClient para devolver `response` en .post()."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=response)
    return patch("crumbpos.core.sii_client.envio.httpx.AsyncClient", return_value=mock_client)


# ──────────────────────────────────────────────────────────────────────────────
# enviar_dte_async
# ──────────────────────────────────────────────────────────────────────────────

def test_enviar_dte_async_retorna_track_id_xml():
    """SII responde XML con TRACKID → status OK y track_id extraído."""
    sii_xml = (
        "<RECEPCIONDTE>"
        "<STATUS>0</STATUS>"
        "<TRACKID>98765</TRACKID>"
        "<GLOSA>Envio Recibido</GLOSA>"
        "</RECEPCIONDTE>"
    )
    resp = _mock_response(sii_xml)

    with _patch_httpx_client(resp):
        result = asyncio.run(
            enviar_dte_async(
                xml_bytes=b"<EnvioDTE/>",
                token="tok-abc",
                rut_emisor="76354771-K",
                ambiente="certificacion",
            )
        )

    assert result["status"] == "OK"
    assert result["track_id"] == "98765"
    assert result["glosa"] == "Envio Recibido"
    assert result["status_code"] == "0"


def test_enviar_dte_async_sin_track_id_retorna_error():
    """SII responde sin TrackID → status ERROR."""
    sii_xml = "<RECEPCIONDTE><STATUS>99</STATUS><GLOSA>Error de autenticacion</GLOSA></RECEPCIONDTE>"
    resp = _mock_response(sii_xml)

    with _patch_httpx_client(resp):
        result = asyncio.run(
            enviar_dte_async(
                xml_bytes=b"<EnvioDTE/>",
                token="tok-bad",
                rut_emisor="76354771-K",
                ambiente="certificacion",
            )
        )

    assert result["status"] == "ERROR"
    assert result["track_id"] is None


def test_enviar_dte_async_reintenta_en_connect_error():
    """ConnectError en primer intento → reintenta y triunfa en el segundo."""
    import httpx as _httpx

    sii_xml = "<RECEPCIONDTE><STATUS>0</STATUS><TRACKID>11111</TRACKID></RECEPCIONDTE>"
    ok_resp = _mock_response(sii_xml)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    # Primer intento falla, segundo OK
    mock_client.post = AsyncMock(
        side_effect=[_httpx.ConnectError("connection refused"), ok_resp]
    )

    with (
        patch("crumbpos.core.sii_client.envio.httpx.AsyncClient", return_value=mock_client),
        patch("crumbpos.core.sii_client.envio.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = asyncio.run(
            enviar_dte_async(
                xml_bytes=b"<EnvioDTE/>",
                token="tok-abc",
                rut_emisor="76354771-K",
                ambiente="certificacion",
            )
        )

    assert result["status"] == "OK"
    assert result["track_id"] == "11111"
    # Verificar que esperó antes del reintento (10s × (0+1) = 10s)
    mock_sleep.assert_awaited_once_with(10)


def test_enviar_dte_async_agota_reintentos_propaga_excepcion():
    """Si todos los reintentos fallan, se propaga la excepción."""
    import httpx as _httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(
        side_effect=_httpx.ConnectError("no route to host")
    )

    with (
        patch("crumbpos.core.sii_client.envio.httpx.AsyncClient", return_value=mock_client),
        patch("crumbpos.core.sii_client.envio.asyncio.sleep", new_callable=AsyncMock),
    ):
        with pytest.raises(_httpx.ConnectError):
            asyncio.run(
                enviar_dte_async(
                    xml_bytes=b"<EnvioDTE/>",
                    token="tok-abc",
                    rut_emisor="76354771-K",
                    ambiente="certificacion",
                )
            )


def test_enviar_dte_async_rut_envia_separado():
    """rut_envia distinto de rut_emisor se usa como sender."""
    sii_xml = "<RECEPCIONDTE><STATUS>0</STATUS><TRACKID>22222</TRACKID></RECEPCIONDTE>"
    resp = _mock_response(sii_xml)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=resp)

    with patch("crumbpos.core.sii_client.envio.httpx.AsyncClient", return_value=mock_client):
        result = asyncio.run(
            enviar_dte_async(
                xml_bytes=b"<EnvioDTE/>",
                token="tok",
                rut_emisor="76354771-K",
                ambiente="certificacion",
                rut_envia="12345678-9",
            )
        )

    assert result["status"] == "OK"
    # Verificar que el data enviado usa el sender correcto
    call_kwargs = mock_client.post.call_args
    data_sent = call_kwargs.kwargs.get("data") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
    # data puede estar en kwargs["data"]
    data_sent = mock_client.post.call_args.kwargs.get("data", {})
    assert data_sent.get("rutSender") == "12345678"
    assert data_sent.get("dvSender") == "9"


# ──────────────────────────────────────────────────────────────────────────────
# enviar_boleta_async
# ──────────────────────────────────────────────────────────────────────────────

def test_enviar_boleta_async_parsea_json():
    """SII responde JSON con trackid → track_id y estado extraídos."""
    sii_json = json.dumps({
        "trackid": 55555,
        "estado": "EPR",
        "estadistica": [{"tipo": 39, "cantidad": 3}],
    })
    resp = _mock_response(sii_json)

    with _patch_httpx_client(resp):
        result = asyncio.run(
            enviar_boleta_async(
                xml_bytes=b"<EnvioBOLETA/>",
                token="tok-bol",
                rut_emisor="76354771-K",
                ambiente="certificacion",
            )
        )

    assert result["status"] == "OK"
    assert result["track_id"] == "55555"
    assert result["estado"] == "EPR"
    assert result["estadistica"] == [{"tipo": 39, "cantidad": 3}]


def test_enviar_boleta_async_fallback_xml():
    """Si SII responde XML (no JSON), extrae TrackID del XML."""
    sii_xml = "<RECEPCIONDTE><TRACKID>77777</TRACKID></RECEPCIONDTE>"
    resp = _mock_response(sii_xml)

    with _patch_httpx_client(resp):
        result = asyncio.run(
            enviar_boleta_async(
                xml_bytes=b"<EnvioBOLETA/>",
                token="tok-bol",
                rut_emisor="76354771-K",
                ambiente="certificacion",
            )
        )

    assert result["status"] == "OK"
    assert result["track_id"] == "77777"


def test_enviar_boleta_async_sin_track_retorna_error():
    """Respuesta JSON sin trackid → status ERROR."""
    sii_json = json.dumps({"estado": "RCT", "error": "Token invalido"})
    resp = _mock_response(sii_json)

    with _patch_httpx_client(resp):
        result = asyncio.run(
            enviar_boleta_async(
                xml_bytes=b"<EnvioBOLETA/>",
                token="tok-bad",
                rut_emisor="76354771-K",
                ambiente="certificacion",
            )
        )

    assert result["status"] == "ERROR"
    assert result["track_id"] is None


def test_enviar_boleta_async_reintenta_en_timeout():
    """TimeoutException → reintenta correctamente."""
    import httpx as _httpx

    sii_json = json.dumps({"trackid": 99999, "estado": "EPR"})
    ok_resp = _mock_response(sii_json)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(
        side_effect=[_httpx.TimeoutException("timeout"), ok_resp]
    )

    with (
        patch("crumbpos.core.sii_client.envio.httpx.AsyncClient", return_value=mock_client),
        patch("crumbpos.core.sii_client.envio.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = asyncio.run(
            enviar_boleta_async(
                xml_bytes=b"<EnvioBOLETA/>",
                token="tok-bol",
                rut_emisor="76354771-K",
                ambiente="certificacion",
            )
        )

    assert result["status"] == "OK"
    assert result["track_id"] == "99999"
    mock_sleep.assert_awaited_once_with(10)
