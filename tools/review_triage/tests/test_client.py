"""client（#385）のオフライン試験。外部ネットワークと実キーを使わない。"""

import http.client
import json
import logging
import socket
import ssl
from collections.abc import Mapping
from typing import Any

import pytest

from tools.review_triage import client as client_module
from tools.review_triage.client import HttpsTransport, SystemOneClient, api_key_from_env
from tools.review_triage.models import (
    API_ENDPOINT,
    DEFAULT_MODEL_ID,
    REQUEST_BYTE_LIMIT,
    ClientError,
    ClientOutcome,
    ErrorKind,
    HttpRequest,
    HttpResponse,
    NoulQuestion,
    Question,
    SystemOneRequest,
    Transport,
    state_digest,
)

KEY = "ts-test-key-SENTINEL-7f3a"  # gitleaks:allow
STATE_TEXT = "STATE-SENTINEL private finding text"
ERROR_BODY_SENTINEL = "ERROR-BODY-SENTINEL"
EXC_SENTINEL = "EXCEPTION-SENTINEL"


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """名前解決を loopback だけに限り、外部への接続を試験の失敗にする。"""
    real_getaddrinfo = socket.getaddrinfo

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"external name resolution attempted: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


# --- transport 用の偽の接続 ---------------------------------------------------


class FakeHttpResponse:
    """``http.client.HTTPResponse`` の代わり。"""

    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self._headers = headers
        self._body = body

    def getheaders(self) -> list[tuple[str, str]]:
        return self._headers

    def read(self, amt: int | None = None) -> bytes:
        size = len(self._body) if amt is None else amt
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk


class FakeConnection:
    """``http.client.HTTPSConnection`` の代わり。送った内容を記録する。"""

    def __init__(self, host: str, port: int, response: FakeHttpResponse) -> None:
        self.host = host
        self.port = port
        self._response = response
        self.sent: list[tuple[str, str, bytes, dict[str, str]]] = []

    def request(self, method: str, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        self.sent.append((method, url, body, dict(headers)))

    def getresponse(self) -> FakeHttpResponse:
        return self._response

    def close(self) -> None:
        return None


class ConnectionRecorder:
    """``connection_factory`` として渡し、作られた接続を記録する。"""

    def __init__(self, response: FakeHttpResponse) -> None:
        self._response = response
        self.connections: list[FakeConnection] = []

    def __call__(self, host: str, port: int, timeout: float) -> FakeConnection:
        connection = FakeConnection(host, port, self._response)
        self.connections.append(connection)
        return connection


# --- client 用の fake と helper -------------------------------------------------


class FakeTransport:
    """渡された HttpRequest を記録し、用意した結果を順に返す（例外なら送出する）。"""

    def __init__(self, *results: HttpResponse | BaseException) -> None:
        self._results = list(results)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _json(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _response(status: int = 200, body: bytes = b"") -> HttpResponse:
    return HttpResponse(status=status, headers={}, body=body, final_url=API_ENDPOINT)


def _ok_body(**overrides: object) -> bytes:
    """``_noul_questions()`` に対応する正常な応答本文。``overrides`` で最上位の項目を差し替える。"""
    body: dict[str, object] = {
        "model": "jev-latest",
        "answers": {"is_urgent": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    body.update(overrides)
    return _json(body)


def _noul_questions() -> dict[str, Question]:
    return {"is_urgent": NoulQuestion(instructions="Is `finding` urgent?")}


def _request(*, state_sha256: str | None = None) -> SystemOneRequest:
    sections = {"finding": STATE_TEXT}
    return SystemOneRequest(
        model=DEFAULT_MODEL_ID,
        state=sections,
        questions=_noul_questions(),
        state_sha256=state_sha256 or state_digest(sections)[0],
        question_set_version="test-1",
        purpose="finding",
        finding_ids=("F-1",),
    )


def _client(transport: Transport | None = None, *, api_key: str | None = KEY, max_calls: int = 50) -> SystemOneClient:
    return SystemOneClient(
        api_key=api_key,
        model=DEFAULT_MODEL_ID,
        transport=transport,
        max_request_bytes=REQUEST_BYTE_LIMIT,
        max_calls=max_calls,
    )


def _refused(outcome: ClientOutcome, kind: ErrorKind) -> ClientError:
    """通信 0 回で kind の失敗になったことを確かめ、ClientError を返す。"""
    assert outcome.ok is False and outcome.response is None
    assert outcome.error is not None and outcome.error.kind is kind
    assert outcome.calls_made == 0
    return outcome.error


def _failed(outcome: ClientOutcome, kind: ErrorKind) -> ClientError:
    """1 回送って kind の失敗になったことを確かめ、ClientError を返す。"""
    assert outcome.ok is False and outcome.response is None
    assert outcome.error is not None and outcome.error.kind is kind
    assert outcome.calls_made == 1
    return outcome.error


def _error_body() -> bytes:
    """サーバがキーや state を反射したと仮定したエラー本文。"""
    return _json({"error": {"message": f"{ERROR_BODY_SENTINEL} rejected key {KEY} for {STATE_TEXT}"}})


def _assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in (KEY, STATE_TEXT, ERROR_BODY_SENTINEL, EXC_SENTINEL):
            assert secret not in text


# --- transport ------------------------------------------------------------------


def test_default_connection_checks_the_certificate_and_the_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定の接続で証明書・ホスト名の検証が外れる、または http.client が Authorization と本文を標準出力へ書く。"""
    # HTTPSConnection は構築しただけでは名前解決も接続もしないため、オフラインで確かめられる
    assert HttpsTransport()._connect is client_module._https_connection  # pyright: ignore[reportPrivateUsage]
    connection = client_module._https_connection("api.typesafe.ai", 443, 1.5)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(connection, http.client.HTTPSConnection)
    assert connection.sock is None
    context = vars(connection)["_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED

    # プロセス全体の debuglevel が 1（有効）に設定されていても、既定の接続は 0 に固定する
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    patched_connection = client_module._https_connection(  # pyright: ignore[reportPrivateUsage]
        "api.typesafe.ai", 443, 1.5
    )
    assert isinstance(patched_connection, http.client.HTTPSConnection)
    assert patched_connection.debuglevel == 0


# --- client: 送信前の検査 ----------------------------------------------------------


def test_state_that_does_not_match_its_hash_is_not_sent() -> None:
    """送る直前の本文が許可のハッシュ（state_sha256）と食い違っても、そのまま外部へ送る。"""
    transport = FakeTransport()
    _refused(_client(transport).evaluate(_request(state_sha256="0" * 64)), ErrorKind.NOT_ALLOWED)
    assert transport.requests == []


def test_call_budget_counts_every_attempt() -> None:
    """失敗した送信を数えず、外部 API へ上限を超えて送り続ける。"""
    transport = FakeTransport(_response(503, b""), _response(200, _ok_body()))
    client = _client(transport, max_calls=1)
    assert client.evaluate(_request()).ok is False  # 失敗した送信も budget を消費する
    _refused(client.evaluate(_request()), ErrorKind.CALL_BUDGET)
    assert len(transport.requests) == 1


# --- client: 200 応答 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "noul",
    [
        pytest.param(1.5, id="out-of-range"),
        # 型の違う値を数値へ変換しない（true を 1.0 として受け取らない）
        pytest.param(True, id="boolean"),
    ],
)
def test_malformed_200_response_is_not_a_success(noul: object) -> None:
    """範囲外や型の違う回答を読み替えて成功の outcome にし、確信ありの助言の材料にする。"""
    body = _ok_body(answers={"is_urgent": {"type": "noul", "noul": noul}})
    _failed(_client(FakeTransport(_response(200, body))).evaluate(_request()), ErrorKind.MALFORMED_RESPONSE)


# --- client: 失敗の分類 ------------------------------------------------------------


def test_no_secret_reaches_repr_logs_or_output(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """API キー・送った本文・エラー応答の本文・例外の文言が、outcome（outcomes.json）・ログ・標準出力・repr に残る。"""
    caplog.set_level(logging.DEBUG)
    transport = FakeTransport(
        _response(401, _error_body()),
        _response(500, _error_body()),
        TimeoutError(EXC_SENTINEL),
        ssl.SSLError(EXC_SENTINEL),
        _response(200, _error_body()),
        _response(200, _ok_body()),
    )
    client = _client(transport)
    outcomes = [client.evaluate(_request()) for _ in range(6)]
    outcomes.append(_client(api_key=None).evaluate(_request()))
    captured = capsys.readouterr()
    assert [outcome.ok for outcome in outcomes] == [False, False, False, False, False, True, False]
    _assert_no_secrets(
        repr(client), caplog.text, captured.out, captured.err, *(outcome.model_dump_json() for outcome in outcomes)
    )


def test_redirect_never_carries_the_key_to_another_host() -> None:
    """redirect を追い、Authorization ヘッダ付きの request を別のホストへ送る。"""
    recorder = ConnectionRecorder(FakeHttpResponse(302, [("Location", "https://evil.example/steal")], _error_body()))
    outcome = _client(HttpsTransport(connection_factory=recorder)).evaluate(_request())
    _failed(outcome, ErrorKind.REDIRECT)
    assert [(c.host, c.port) for c in recorder.connections] == [("api.typesafe.ai", 443)]
    assert sum(len(c.sent) for c in recorder.connections) == 1
    _assert_no_secrets(outcome.model_dump_json())


# --- 補助関数 --------------------------------------------------------------------------


def test_api_key_is_read_only_from_its_variable() -> None:
    """TYPESAFE_API_KEY 以外の変数（他社の API キーなど）を読み、TypeSafe へ送る。"""
    assert api_key_from_env({"OTHER_API_KEY": "x", "TYPESAFE_TOKEN": "y"}) is None
