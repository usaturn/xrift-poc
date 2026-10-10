"""client（#385）のオフライン試験。外部ネットワークと実キーを使わない。"""

import hashlib
import http.client
import http.server
import json
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from tools.review_triage import client as client_module
from tools.review_triage.client import (
    API_KEY_ENV,
    MAX_TIMEOUT_S,
    RESPONSE_BYTE_LIMIT,
    HttpsTransport,
    SystemOneClient,
    TransportError,
    api_key_from_env,
    connectivity_request,
)
from tools.review_triage.models import (
    API_ENDPOINT,
    DEFAULT_MODEL_ID,
    REQUEST_BYTE_LIMIT,
    ChoiceAnswer,
    ChoiceQuestion,
    ClientError,
    ClientOutcome,
    ErrorKind,
    HttpRequest,
    HttpResponse,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
    SystemOneRequest,
    Transport,
    state_digest,
)

KEY = "ts-test-key-SENTINEL-7f3a"  # gitleaks:allow
STATE_TEXT = "STATE-SENTINEL private finding text"
FIXTURES = Path(__file__).parent / "fixtures" / "api"
# fixtures/api の JSON は公式文書の応答例をそのまま保存したもの（2026-09-26 取得）
QUICKSTART_SOURCE = "https://docs.typesafe.ai/introduction/quickstart"
NOUL_SOURCE = "https://docs.typesafe.ai/primitives/noul"
SCHEMA_MISMATCH = "response does not match the SystemOneResponse schema"
ERROR_BODY_SENTINEL = "ERROR-BODY-SENTINEL"
TRUNCATED_BODY = b'{"model":"jev-latest","answers"'  # Content-Length 未達で切れた本文の断片
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
    """``http.client.HTTPResponse`` の代わり。``read`` の要求量を記録する。"""

    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes, *, delay_s: float = 0.0) -> None:
        self.status = status
        self._headers = headers
        self._body = body
        self._delay_s = delay_s
        self.reads: list[int | None] = []

    def getheaders(self) -> list[tuple[str, str]]:
        return self._headers

    def read(self, amt: int | None = None) -> bytes:
        self.reads.append(amt)
        if self._delay_s:
            time.sleep(self._delay_s)
        size = len(self._body) if amt is None else amt
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk


class FakeConnection:
    """``http.client.HTTPSConnection`` の代わり。送った内容と ``close`` を記録する。"""

    def __init__(self, host: str, port: int, timeout: float, response: FakeHttpResponse | BaseException) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._response = response
        self.sent: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        self.sent.append((method, url, body, dict(headers)))

    def getresponse(self) -> FakeHttpResponse:
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    def close(self) -> None:
        self.closed = True


class ConnectionRecorder:
    """``connection_factory`` として渡し、作られた接続を記録する。"""

    def __init__(self, response: FakeHttpResponse | BaseException) -> None:
        self._response = response
        self.connections: list[FakeConnection] = []

    def __call__(self, host: str, port: int, timeout: float) -> FakeConnection:
        connection = FakeConnection(host, port, timeout, self._response)
        self.connections.append(connection)
        return connection


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        return None  # 切断済みの接続への書き込み失敗を出力しない


class LoopbackServer:
    """127.0.0.1 で待ち受ける平文 HTTP サーバ。受けた request を記録し、決めた応答を返す。

    ``declared_length`` を渡すと、本文の長さの代わりにその値を Content-Length に宣言する（途中切断の再現用）。
    """

    def __init__(
        self,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        delay_s: float = 0.0,
        declared_length: int | None = None,
    ) -> None:
        self.received: list[tuple[str, str, str | None, bytes]] = []
        self.requested: list[tuple[str, int]] = []
        received = self.received

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                received.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(length)))
                time.sleep(delay_s)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body) if declared_length is None else declared_length))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self.port: int = self._httpd.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def connection_factory(self, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        self.requested.append((host, port))
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def make_server() -> Iterator[Callable[..., LoopbackServer]]:
    servers: list[LoopbackServer] = []

    def make(
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        delay_s: float = 0.0,
        declared_length: int | None = None,
    ) -> LoopbackServer:
        server = LoopbackServer(status, headers, body, delay_s, declared_length)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _http_request(url: str = API_ENDPOINT, *, timeout_s: float = 5.0) -> HttpRequest:
    return HttpRequest(
        url=url,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        body=b'{"x":1}',
        timeout_s=timeout_s,
    )


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


def _response(
    status: int = 200,
    body: bytes = b"",
    *,
    headers: Mapping[str, str] | None = None,
    final_url: str = API_ENDPOINT,
) -> HttpResponse:
    return HttpResponse(status=status, headers=dict(headers or {}), body=body, final_url=final_url)


def _ok_body(**overrides: object) -> bytes:
    """``_noul_questions()`` に対応する正常な応答本文。``overrides`` で最上位の項目を差し替える。"""
    body: dict[str, object] = {
        "model": "jev-latest",
        "answers": {"is_urgent": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    body.update(overrides)
    return _json(body)


def _nested_list(depth: int) -> object:
    """``depth`` 段に入れ子にしたリスト。"""
    nested: object = 0
    for _ in range(depth):
        nested = [nested]
    return nested


def _noul_questions() -> dict[str, Question]:
    return {"is_urgent": NoulQuestion(instructions="Is `finding` urgent?")}


def _quickstart_questions() -> dict[str, Question]:
    """quickstart の応答例に対応する質問。"""
    return {
        "department": ChoiceQuestion(
            instructions="Which department should handle this?",
            criteria={"billing": None, "technical": None, "sales": None},
        ),
        "frustration": ScoreQuestion(
            instructions="How frustrated is the customer?",
            criteria=("Calm, just stating facts", "Frustrated but civil", "Very angry, strong language"),
        ),
        "is_urgent": NoulQuestion(instructions="Is this urgent?"),
    }


def _request(
    questions: Mapping[str, Question] | None = None,
    *,
    state: Mapping[str, str] | None = None,
    model: str = DEFAULT_MODEL_ID,
    state_sha256: str | None = None,
) -> SystemOneRequest:
    sections = dict(state) if state is not None else {"finding": STATE_TEXT}
    return SystemOneRequest(
        model=model,
        state=sections,
        questions=dict(questions) if questions is not None else _noul_questions(),
        state_sha256=state_sha256 or state_digest(sections)[0],
        question_set_version="test-1",
        purpose="finding",
        finding_ids=("F-1",),
    )


def _client(
    transport: Transport | None = None,
    *,
    api_key: str | None = KEY,
    model: str = DEFAULT_MODEL_ID,
    timeout_s: float = 30.0,
    max_request_bytes: int = REQUEST_BYTE_LIMIT,
    max_calls: int = 50,
) -> SystemOneClient:
    return SystemOneClient(
        api_key=api_key,
        model=model,
        transport=transport,
        timeout_s=timeout_s,
        max_request_bytes=max_request_bytes,
        max_calls=max_calls,
    )


def _refused(outcome: ClientOutcome, kind: ErrorKind) -> ClientError:
    """通信 0 回で kind の失敗になったことを確かめ、ClientError を返す。"""
    assert outcome.ok is False and outcome.response is None
    assert outcome.error is not None and outcome.error.kind is kind
    assert outcome.error.http_status is None and outcome.error.retryable is False
    assert outcome.calls_made == 0 and outcome.elapsed_ms is None
    return outcome.error


def _failed(outcome: ClientOutcome, kind: ErrorKind) -> ClientError:
    """1 回送って kind の失敗になったことを確かめ、ClientError を返す。"""
    assert outcome.ok is False and outcome.response is None
    assert outcome.error is not None and outcome.error.kind is kind
    assert outcome.calls_made == 1 and outcome.elapsed_ms is not None and outcome.elapsed_ms >= 0
    return outcome.error


def _error_body() -> bytes:
    """サーバがキーや state を反射したと仮定したエラー本文。"""
    return _json({"error": {"message": f"{ERROR_BODY_SENTINEL} rejected key {KEY} for {STATE_TEXT}"}})


def _assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in (KEY, STATE_TEXT, ERROR_BODY_SENTINEL, EXC_SENTINEL):
            assert secret not in text


# --- transport ------------------------------------------------------------------


def test_https_transport_sends_once_and_returns_the_raw_response() -> None:
    recorder = ConnectionRecorder(
        FakeHttpResponse(
            200, [("Content-Type", "application/json"), ("X-Multi", "a"), ("x-multi", "b")], b'{"ok":true}'
        )
    )
    response = HttpsTransport(connection_factory=recorder).send(_http_request())
    [connection] = recorder.connections
    assert (connection.host, connection.port, connection.timeout) == ("api.typesafe.ai", 443, 5.0)
    assert connection.sent == [
        ("POST", "/v1/systemone", b'{"x":1}', {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    ]
    assert connection.closed
    assert response == HttpResponse(
        status=200,
        headers={"content-type": "application/json", "x-multi": "a, b"},
        body=b'{"ok":true}',
        final_url=API_ENDPOINT,
    )


def test_https_transport_returns_a_redirect_without_following_it() -> None:
    recorder = ConnectionRecorder(FakeHttpResponse(302, [("Location", "https://evil.example/steal")], b"moved"))
    response = HttpsTransport(connection_factory=recorder).send(_http_request())
    assert (response.status, response.final_url) == (302, API_ENDPOINT)
    assert [(c.host, c.port, len(c.sent)) for c in recorder.connections] == [("api.typesafe.ai", 443, 1)]


def test_https_transport_stops_reading_one_byte_past_the_limit() -> None:
    fake = FakeHttpResponse(200, [], b"x" * 100)
    response = HttpsTransport(max_response_bytes=10, connection_factory=ConnectionRecorder(fake)).send(_http_request())
    assert response.body == b"x" * 11
    assert sum(amt or 0 for amt in fake.reads) == 11


def test_https_transport_reports_an_out_of_range_status() -> None:
    recorder = ConnectionRecorder(FakeHttpResponse(999, [], b""))
    with pytest.raises(TransportError) as caught:
        HttpsTransport(connection_factory=recorder).send(_http_request())
    assert caught.value.kind is ErrorKind.UNEXPECTED_STATUS
    assert caught.value.safe_message == "HTTP status 999 is out of range"
    assert recorder.connections[0].closed


def test_https_transport_closes_the_connection_when_the_peer_fails() -> None:
    recorder = ConnectionRecorder(ConnectionResetError("reset"))
    with pytest.raises(ConnectionResetError):
        HttpsTransport(connection_factory=recorder).send(_http_request())
    assert recorder.connections[0].closed


def test_https_transport_times_out_while_reading_a_slow_body() -> None:
    recorder = ConnectionRecorder(FakeHttpResponse(200, [], b"x" * 400_000, delay_s=0.05))
    with pytest.raises(TimeoutError):
        HttpsTransport(connection_factory=recorder).send(_http_request(timeout_s=0.08))
    assert recorder.connections[0].closed


@pytest.mark.parametrize(
    "url",
    [
        "http://api.typesafe.ai/v1/systemone",
        "https://user:pw@api.typesafe.ai/v1/systemone",
        "https://api.typesafe.ai/v1/systemone?debug=1",
        "https://api.typesafe.ai/v1/systemone#frag",
        "https:///v1/systemone",
    ],
)
def test_https_transport_rejects_anything_but_a_plain_https_url(url: str) -> None:
    recorder = ConnectionRecorder(FakeHttpResponse(200, [], b""))
    with pytest.raises(ValueError):
        HttpsTransport(connection_factory=recorder).send(_http_request(url))
    assert recorder.connections == []


@pytest.mark.parametrize("limit", [0, -1, True])
def test_https_transport_rejects_a_non_positive_limit(limit: int) -> None:
    with pytest.raises(ValueError):
        HttpsTransport(max_response_bytes=limit)


def test_https_transport_default_limit_is_the_module_limit() -> None:
    fake = FakeHttpResponse(200, [], b"x" * (RESPONSE_BYTE_LIMIT + 50))
    response = HttpsTransport(connection_factory=ConnectionRecorder(fake)).send(_http_request())
    assert len(response.body) == RESPONSE_BYTE_LIMIT + 1


def test_https_transport_reports_a_body_cut_before_its_content_length() -> None:
    # http.client は Content-Length 未達の切断で IncompleteRead を送出せず空バイトを返すため、transport が検出する
    recorder = ConnectionRecorder(FakeHttpResponse(200, [("Content-Length", "1000")], TRUNCATED_BODY))
    with pytest.raises(TransportError) as caught:
        HttpsTransport(connection_factory=recorder).send(_http_request())
    assert caught.value.kind is ErrorKind.CONNECTION
    assert caught.value.safe_message == "response body ended before Content-Length"
    assert recorder.connections[0].closed


@pytest.mark.parametrize(
    ("status", "headers", "body", "max_response_bytes", "expected_body"),
    [
        # 304 は本文を持たない。Content-Length は表現の長さを示すだけ
        (304, [("Content-Length", "100")], b"", 100, b""),
        # Transfer-Encoding がある応答では http.client が Content-Length を使わない
        (200, [("Transfer-Encoding", "chunked"), ("Content-Length", "1000")], b"{}", 100, b"{}"),
        # 上限まで読めたら宣言長に届かなくても切断ではない。上限超過の判定は client に任せる
        (200, [("Content-Length", "1000")], b"x" * 100, 10, b"x" * 11),
        # 整数として解釈できない Content-Length は判定に使わない
        (200, [("Content-Length", "abc")], b"{}", 100, b"{}"),
        (200, [("Content-Length", "2"), ("Content-Length", "2")], b"{}", 100, b"{}"),
        # int の桁数上限（既定 4300 桁）を超える値でも例外を送出しない
        (200, [("Content-Length", "1" * 4301)], b"{}", 100, b"{}"),
        (200, [("Content-Length", "2")], b"{}", 100, b"{}"),
    ],
)
def test_https_transport_does_not_misreport_a_complete_body(
    status: int, headers: list[tuple[str, str]], body: bytes, max_response_bytes: int, expected_body: bytes
) -> None:
    recorder = ConnectionRecorder(FakeHttpResponse(status, headers, body))
    transport = HttpsTransport(max_response_bytes=max_response_bytes, connection_factory=recorder)
    response = transport.send(_http_request())
    assert (response.status, response.body) == (status, expected_body)


def test_default_connection_checks_the_certificate_and_the_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # 既定の factory を差し替えずに呼び、TLS 検証（証明書・ホスト名）の退行を検出する（final review F2）。
    # HTTPSConnection は構築しただけでは名前解決も接続もしないため、オフラインで確かめられる
    assert HttpsTransport()._connect is client_module._https_connection  # pyright: ignore[reportPrivateUsage]
    connection = client_module._https_connection("api.typesafe.ai", 443, 1.5)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(connection, http.client.HTTPSConnection)
    assert (connection.host, connection.port, connection.timeout) == ("api.typesafe.ai", 443, 1.5)
    assert connection.sock is None
    assert connection.debuglevel == 0  # 送信ヘッダ（Authorization）を標準出力へ書かない
    context = vars(connection)["_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED

    # プロセス全体の debuglevel が 1（有効）に設定されていても、既定の接続は 0 に固定する（final review M4）
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    patched_connection = client_module._https_connection(  # pyright: ignore[reportPrivateUsage]
        "api.typesafe.ai", 443, 1.5
    )
    assert isinstance(patched_connection, http.client.HTTPSConnection)
    assert patched_connection.sock is None
    assert patched_connection.debuglevel == 0


def test_transport_error_truncates_its_message() -> None:
    error = TransportError(ErrorKind.CONNECTION, "x" * 500)
    assert error.safe_message == "x" * 200
    assert str(error) == "x" * 200


def test_https_transport_over_loopback_does_not_follow_a_redirect(
    make_server: Callable[..., LoopbackServer],
) -> None:
    server = make_server(302, {"Location": "http://127.0.0.1:9/steal"}, b"moved")
    response = HttpsTransport(connection_factory=server.connection_factory).send(_http_request())
    assert response.status == 302
    assert response.headers["location"] == "http://127.0.0.1:9/steal"
    assert server.requested == [("api.typesafe.ai", 443)]
    assert server.received == [("POST", "/v1/systemone", f"Bearer {KEY}", b'{"x":1}')]


def test_https_transport_over_loopback_times_out(make_server: Callable[..., LoopbackServer]) -> None:
    server = make_server(200, {}, b"{}", delay_s=0.5)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        HttpsTransport(connection_factory=server.connection_factory).send(_http_request(timeout_s=0.1))
    assert time.monotonic() - started < 0.5


# --- client: 構築 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": ""},
        {"timeout_s": 0.0},
        {"timeout_s": -1.0},
        {"timeout_s": float("nan")},
        {"timeout_s": float("inf")},
        {"timeout_s": MAX_TIMEOUT_S + 0.1},
        {"max_request_bytes": 0},
        {"max_request_bytes": REQUEST_BYTE_LIMIT + 1},
        {"max_request_bytes": True},
        {"max_calls": -1},
        {"max_calls": True},
    ],
)
def test_invalid_settings_are_rejected(overrides: dict[str, Any]) -> None:
    settings: dict[str, Any] = {"api_key": KEY, "model": DEFAULT_MODEL_ID, **overrides}
    with pytest.raises(ValueError):
        SystemOneClient(**settings)


def test_boundary_settings_are_accepted() -> None:
    SystemOneClient(
        api_key=None,
        model=DEFAULT_MODEL_ID,
        timeout_s=MAX_TIMEOUT_S,
        max_request_bytes=REQUEST_BYTE_LIMIT,
        max_calls=0,
    )
    SystemOneClient(api_key=None, model=DEFAULT_MODEL_ID, timeout_s=0.001, max_request_bytes=1)


def test_repr_never_shows_the_key() -> None:
    text = repr(_client(api_key=KEY))
    assert KEY not in text and "api_key=<set>" in text
    assert "api_key=<unset>" in repr(_client(api_key=None))


# --- client: 送信前の検査 ----------------------------------------------------------


@pytest.mark.parametrize("api_key", [None, "", "   ", "bad key", "key\nnext", "キー", "k" * 4097])
def test_unusable_key_is_refused_without_calling_the_transport(api_key: str | None) -> None:
    transport = FakeTransport()
    _refused(_client(transport, api_key=api_key).evaluate(_request()), ErrorKind.NO_KEY)
    assert transport.requests == []


def test_key_of_the_maximum_length_is_sent() -> None:
    assert _client(FakeTransport(_response(200, _ok_body())), api_key="k" * 4096).evaluate(_request()).ok


def test_state_that_does_not_match_its_hash_is_not_sent() -> None:
    transport = FakeTransport()
    error = _refused(_client(transport).evaluate(_request(state_sha256="0" * 64)), ErrorKind.NOT_ALLOWED)
    assert error.message == "request state does not match state_sha256; refusing to send"
    assert transport.requests == []


def test_missing_key_is_reported_before_other_refusals() -> None:
    outcome = _client(FakeTransport(), api_key=None).evaluate(_request(state_sha256="0" * 64, model="other"))
    _refused(outcome, ErrorKind.NO_KEY)


def test_request_for_another_model_is_an_invalid_request() -> None:
    transport = FakeTransport()
    error = _refused(_client(transport).evaluate(_request(model="jev-1.13.0")), ErrorKind.INVALID_REQUEST)
    assert error.message == "request model does not match the client model"
    assert transport.requests == []


def test_request_over_the_byte_limit_is_too_large() -> None:
    request = _request()
    size = request.payload_bytes()
    transport = FakeTransport(_response(200, _ok_body()))
    error = _refused(_client(transport, max_request_bytes=size - 1).evaluate(request), ErrorKind.TOO_LARGE)
    assert error.message == f"request body is {size} bytes, limit is {size - 1}"
    assert _client(transport, max_request_bytes=size).evaluate(request).ok
    assert len(transport.requests) == 1


def test_call_budget_counts_every_attempt() -> None:
    transport = FakeTransport(_response(503, b""), _response(200, _ok_body()))
    client = _client(transport, max_calls=1)
    assert client.evaluate(_request()).ok is False  # 失敗した送信も budget を消費する
    error = _refused(client.evaluate(_request()), ErrorKind.CALL_BUDGET)
    assert error.message == "call budget of 1 request(s) exhausted"
    assert len(transport.requests) == 1


def test_zero_call_budget_never_sends() -> None:
    transport = FakeTransport()
    _refused(_client(transport, max_calls=0).evaluate(_request()), ErrorKind.CALL_BUDGET)
    assert transport.requests == []


def _request_not_utf8(where: str) -> SystemOneRequest:
    """孤立サロゲートを state の本文か ``finding_ids`` に置いた request。"""
    if where == "state":
        return _request(state={"finding": "\ud800"}, state_sha256="0" * 64)
    # finding_ids は payload に含まれないが outcome へ転記される。pydantic はこの値も受け付ける
    return SystemOneRequest.model_validate({**_request().model_dump(), "finding_ids": ("\ud800",)})


@pytest.mark.parametrize("where", ["state", "finding_ids"])
def test_request_cannot_hold_text_that_is_not_utf8(where: str) -> None:
    # pydantic は孤立サロゲートを含む str を受け付けるため、client の送信前検査で弾く（Plan の contingency 参照）
    transport = FakeTransport()
    outcome = _client(transport).evaluate(_request_not_utf8(where))
    error = _refused(outcome, ErrorKind.INVALID_REQUEST)
    assert error.message == "request text is not valid UTF-8"
    assert transport.requests == []
    if where == "finding_ids":  # 送信本文は変わらないため、request_sha256 も finding_ids が正常な request と同じ
        sent = _client(FakeTransport(_response(200, _ok_body()))).evaluate(_request())
        assert outcome.request_sha256 == sent.request_sha256


@pytest.mark.parametrize("where", ["state", "finding_ids"])
def test_missing_key_is_reported_before_the_utf8_check(where: str) -> None:
    # Spec §5.3 の検査順 1（no_key）を、孤立サロゲート検出より前に保つ（review 指摘 F1）。
    transport = FakeTransport()
    _refused(_client(transport, api_key=None).evaluate(_request_not_utf8(where)), ErrorKind.NO_KEY)
    assert transport.requests == []


# --- client: 送信内容 ----------------------------------------------------------------


@pytest.mark.parametrize("state", [{"finding": STATE_TEXT}, {"finding": "日本語の指摘 — 全角を含む"}])
def test_sends_the_exact_payload_to_the_fixed_endpoint(state: dict[str, str]) -> None:
    request = _request(state=state)
    transport = FakeTransport(_response(200, _ok_body()))
    outcome = _client(transport, timeout_s=12.5).evaluate(request)
    expected = json.dumps(request.to_api_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    [sent] = transport.requests
    assert (sent.method, sent.url, sent.timeout_s) == ("POST", API_ENDPOINT, 12.5)
    assert sent.headers == {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "maatlog-review-triage/1",
    }
    assert sent.body == expected
    assert len(sent.body) == request.payload_bytes()
    assert outcome.request_sha256 == hashlib.sha256(expected).hexdigest()


def test_refused_outcome_carries_the_same_request_hash() -> None:
    request = _request()
    sent = _client(FakeTransport(_response(200, _ok_body()))).evaluate(request)
    refused = _client(api_key=None).evaluate(request)
    assert refused.request_sha256 == sent.request_sha256
    assert (refused.purpose, refused.finding_ids, refused.model_requested) == ("finding", ("F-1",), DEFAULT_MODEL_ID)


def test_default_transport_is_not_built_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> Transport:
        raise AssertionError("HttpsTransport must not be built without a key")

    monkeypatch.setattr(client_module, "HttpsTransport", forbidden)
    _refused(SystemOneClient(api_key=None, model=DEFAULT_MODEL_ID).evaluate(_request()), ErrorKind.NO_KEY)


def test_default_transport_is_built_once_on_first_send(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTransport(_response(200, _ok_body()), _response(200, _ok_body()))
    built: list[FakeTransport] = []

    def build() -> FakeTransport:
        built.append(fake)
        return fake

    monkeypatch.setattr(client_module, "HttpsTransport", build)
    client = SystemOneClient(api_key=KEY, model=DEFAULT_MODEL_ID)
    assert client.evaluate(_request()).ok and client.evaluate(_request()).ok
    assert built == [fake] and len(fake.requests) == 2


# --- client: 200 応答 ------------------------------------------------------------------


def test_parses_the_quickstart_example_as_is() -> None:
    body = (FIXTURES / "quickstart_response.json").read_bytes()
    transport = FakeTransport(_response(200, body, headers={"X-TypeSafe-Request-Id": "req_01HX"}))
    outcome = _client(transport).evaluate(_request(_quickstart_questions()))
    assert outcome.ok and outcome.error is None and outcome.response is not None
    assert outcome.calls_made == 1 and outcome.elapsed_ms is not None
    response = outcome.response
    # 応答の model は要求した alias（jev-latest）ではなく、実際に応答した版
    assert (response.model, response.request_id) == ("jev-1.13.0", "req_01HX")
    assert (response.usage.input_tokens, response.usage.output_tokens) == (392, 65)
    department = response.answers["department"]
    assert isinstance(department, ChoiceAnswer)
    assert (department.choice, department.confidence) == ("technical", 0.78)
    assert department.probabilities == {"technical": 0.85, "sales": 0.0, "billing": 0.15}
    frustration = response.answers["frustration"]
    assert isinstance(frustration, ScoreAnswer)
    assert (frustration.score, frustration.confidence) == (1.0, 1.0)
    assert frustration.legend == {
        "0": "Calm, just stating facts",
        "1": "Frustrated but civil",
        "2": "Very angry, strong language",
    }
    assert frustration.probabilities == {"0": 0.0, "1": 1.0, "2": 0.0}
    urgent = response.answers["is_urgent"]
    assert isinstance(urgent, NoulAnswer) and urgent.noul == 1.0


def test_parses_the_noul_example_as_is() -> None:
    body = (FIXTURES / "noul_response.json").read_bytes()
    questions: dict[str, Question] = {
        "is_human_escalation": NoulQuestion(instructions="Does the customer ask for a human?"),
        "is_repeat_contact": NoulQuestion(instructions="Has the customer contacted us before?"),
    }
    outcome = _client(FakeTransport(_response(200, body))).evaluate(_request(questions))
    assert outcome.response is not None
    assert outcome.response.model == "jev-1.13.0"
    nouls = [(key, answer.noul) for key, answer in outcome.response.answers.items() if isinstance(answer, NoulAnswer)]
    assert nouls == [("is_human_escalation", 0.99), ("is_repeat_contact", 0.93)]
    assert (outcome.response.usage.input_tokens, outcome.response.usage.output_tokens) == (360, 39)


def test_keeps_score_probabilities() -> None:
    questions: dict[str, Question] = {
        "severity": ScoreQuestion(instructions="How severe is it?", criteria=("low", "mid", "high"))
    }
    score = {
        "type": "score",
        "score": 1.2,
        "legend": {"0": "low", "1": "mid", "2": "high"},
        "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
        "confidence": 0.7,
    }
    body = _ok_body(answers={"severity": score})
    outcome = _client(FakeTransport(_response(200, body))).evaluate(_request(questions))
    assert outcome.response is not None
    answer = outcome.response.answers["severity"]
    assert isinstance(answer, ScoreAnswer) and answer.probabilities == {"0": 0.1, "1": 0.6, "2": 0.3}


def test_records_the_model_that_actually_answered() -> None:
    outcome = _client(FakeTransport(_response(200, _ok_body(model="jev-1.14.0")))).evaluate(_request())
    assert outcome.ok and outcome.response is not None
    assert (outcome.model_requested, outcome.response.model) == ("jev-latest", "jev-1.14.0")


def test_keeps_unknown_response_fields_aside() -> None:
    outcome = _client(FakeTransport(_response(200, _ok_body(latency_ms=12)))).evaluate(_request())
    assert outcome.response is not None and outcome.response.model_extra == {"latency_ms": 12}


@pytest.mark.parametrize(
    ("headers", "body_request_id", "expected"),
    [
        ({}, None, None),
        ({"x-typesafe-request-id": "req-9"}, None, "req-9"),
        ({"X-TYPESAFE-REQUEST-ID": "req:9.a_b-c"}, None, "req:9.a_b-c"),
        ({"x-typesafe-request-id": "bad id"}, None, None),
        ({"x-typesafe-request-id": "r" * 129}, None, None),
        ({"x-typesafe-request-id": "req-9"}, "from-body", "req-9"),
        ({}, "from-body", None),
    ],
)
def test_request_id_comes_only_from_a_safe_header(
    headers: dict[str, str], body_request_id: str | None, expected: str | None
) -> None:
    body = _ok_body(request_id=body_request_id) if body_request_id is not None else _ok_body()
    outcome = _client(FakeTransport(_response(200, body, headers=headers))).evaluate(_request())
    assert outcome.response is not None and outcome.response.request_id == expected


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not json", "response is not valid JSON"),
        (b"\xff\xfe{}", "response is not valid JSON"),
        # Python 3.14 の json は 10,000 段では JSONDecodeError を出し、RecursionError の経路を通らない
        (b"[" * 200_000, "response is not valid JSON"),
        (b"[]", "response JSON is not an object"),
        (_json({"model": "jev-latest", "usage": {"input_tokens": 1, "output_tokens": 1}}), SCHEMA_MISMATCH),
        (_json({"model": "jev-latest", "answers": {"is_urgent": {"type": "noul", "noul": 0.9}}}), SCHEMA_MISMATCH),
        (
            b'{"model":"jev-latest","answers":{"is_urgent":{"type":"noul","noul":NaN}},'
            b'"usage":{"input_tokens":1,"output_tokens":1}}',
            SCHEMA_MISMATCH,
        ),
        (_ok_body(answers={"is_urgent": {"type": "noul", "noul": 1.5}}), SCHEMA_MISMATCH),
        # 型の違う値を数値へ変換しない（true を 1.0、"0.9" を 0.9 として受け取らない）
        (_ok_body(answers={"is_urgent": {"type": "noul", "noul": True}}), SCHEMA_MISMATCH),
        (_ok_body(answers={"is_urgent": {"type": "noul", "noul": "0.9"}}), SCHEMA_MISMATCH),
        (_ok_body(usage={"input_tokens": "12", "output_tokens": 2}), SCHEMA_MISMATCH),
        (_ok_body(usage={"input_tokens": True, "output_tokens": 2}), SCHEMA_MISMATCH),
        (_ok_body(usage={"input_tokens": 10, "output_tokens": True}), SCHEMA_MISMATCH),
        (
            _ok_body(answers={"is_urgent": {"type": "choice", "choice": "yes", "confidence": 0.9}}),
            "answers do not match the request (1 problem(s))",
        ),
        (
            _ok_body(answers={"is_urgent": {"type": "noul", "noul": 0.9}, "extra": {"type": "noul", "noul": 0.5}}),
            "answers do not match the request (1 problem(s))",
        ),
        (_ok_body(answers={}), "answers do not match the request (1 problem(s))"),
        # 未知フィールドは値を検証せずに残るため、JSON へ書き出せない値を成功にしない
        # （_json は "\ud800" をエスケープで出力し、json.loads が孤立サロゲートを含む str に戻す）
        (_ok_body(note="\ud800"), "response cannot be serialized as JSON"),
        (_ok_body(note=_nested_list(300)), "response cannot be serialized as JSON"),
    ],
)
def test_malformed_200_response_is_not_a_success(body: bytes, message: str) -> None:
    error = _failed(_client(FakeTransport(_response(200, body))).evaluate(_request()), ErrorKind.MALFORMED_RESPONSE)
    assert (error.message, error.http_status, error.retryable) == (message, 200, False)


def test_recursion_error_while_parsing_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    # インタプリタの再帰上限やスタック量に依らず、RecursionError を malformed_response にする（final review F3）
    body = _ok_body()

    def too_deep(*args: object, **kwargs: object) -> object:
        raise RecursionError(EXC_SENTINEL)

    monkeypatch.setattr(json, "loads", too_deep)
    outcome = _client(FakeTransport(_response(200, body))).evaluate(_request())
    error = _failed(outcome, ErrorKind.MALFORMED_RESPONSE)
    assert (error.message, error.http_status, error.retryable) == ("response is not valid JSON", 200, False)
    _assert_no_secrets(outcome.model_dump_json())


@pytest.mark.parametrize(
    ("original", "replaced"),
    [
        ('"confidence": 0.78', '"confidence": "0.78"'),
        ('"technical": 0.85', '"technical": true'),
        ('"score": 1.0', '"score": "1.0"'),
        ('"confidence": 1.0', '"confidence": true'),
        ('"noul": 1.0', '"noul": true'),
    ],
)
def test_answer_value_of_another_type_is_not_a_success(original: str, replaced: str) -> None:
    # 回答の種別が合っていても、値の型が違えば成功にしない（final review F1）
    text = (FIXTURES / "quickstart_response.json").read_text(encoding="utf-8")
    assert text.count(original) == 1
    body = text.replace(original, replaced).encode("utf-8")
    outcome = _client(FakeTransport(_response(200, body))).evaluate(_request(_quickstart_questions()))
    assert _failed(outcome, ErrorKind.MALFORMED_RESPONSE).message == SCHEMA_MISMATCH


def test_integer_is_accepted_in_a_float_field() -> None:
    # strict の検証でも JSON の整数は float の欄に入れてよい（1 は 1.0）。float の欄で拒否するのは真偽値と数値の文字列
    text = (FIXTURES / "quickstart_response.json").read_text(encoding="utf-8")
    assert text.count('"score": 1.0') == 1 and text.count('"noul": 1.0') == 1
    body = text.replace('"score": 1.0', '"score": 1').replace('"noul": 1.0', '"noul": 1').encode("utf-8")
    # 公式例の値はもともと 1.0 のため、置換が起きずに通る空振りを防ぐ
    raw = json.loads(body)["answers"]
    assert type(raw["frustration"]["score"]) is int and type(raw["is_urgent"]["noul"]) is int
    outcome = _client(FakeTransport(_response(200, body))).evaluate(_request(_quickstart_questions()))
    assert outcome.ok and outcome.response is not None
    frustration = outcome.response.answers["frustration"]
    urgent = outcome.response.answers["is_urgent"]
    assert isinstance(frustration, ScoreAnswer) and frustration.score == 1.0
    assert isinstance(urgent, NoulAnswer) and urgent.noul == 1.0


def test_choice_outside_the_criteria_is_not_a_success() -> None:
    text = (FIXTURES / "quickstart_response.json").read_text(encoding="utf-8")
    body = text.replace('"choice": "technical"', '"choice": "legal"').encode("utf-8")
    outcome = _client(FakeTransport(_response(200, body))).evaluate(_request(_quickstart_questions()))
    error = _failed(outcome, ErrorKind.MALFORMED_RESPONSE)
    assert error.message == "answers do not match the request (1 problem(s))"


def test_response_body_limit_is_inclusive() -> None:
    body = _ok_body()
    padded = body + b" " * (RESPONSE_BYTE_LIMIT - len(body))
    assert _client(FakeTransport(_response(200, padded))).evaluate(_request()).ok
    outcome = _client(FakeTransport(_response(200, padded + b" "))).evaluate(_request())
    error = _failed(outcome, ErrorKind.MALFORMED_RESPONSE)
    assert error.message == f"response body exceeds {RESPONSE_BYTE_LIMIT} bytes"


# --- client: 失敗の分類 ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "retryable", "message"),
    [
        (301, ErrorKind.REDIRECT, False, "HTTP 301 redirect not followed"),
        (302, ErrorKind.REDIRECT, False, "HTTP 302 redirect not followed"),
        (307, ErrorKind.REDIRECT, False, "HTTP 307 redirect not followed"),
        (308, ErrorKind.REDIRECT, False, "HTTP 308 redirect not followed"),
        (401, ErrorKind.AUTH, False, "HTTP 401: authentication rejected"),
        (403, ErrorKind.AUTH, False, "HTTP 403: authentication rejected"),
        (422, ErrorKind.INVALID_REQUEST, False, "HTTP 422: request failed validation"),
        (429, ErrorKind.RATE_LIMIT, True, "HTTP 429: rate limited"),
        (500, ErrorKind.SERVICE, True, "HTTP 500: service unavailable"),
        (502, ErrorKind.SERVICE, True, "HTTP 502: service unavailable"),
        (503, ErrorKind.SERVICE, True, "HTTP 503: service unavailable"),
        (529, ErrorKind.SERVICE, True, "HTTP 529: service unavailable"),
        (201, ErrorKind.UNEXPECTED_STATUS, False, "HTTP 201: unexpected status"),
        (204, ErrorKind.UNEXPECTED_STATUS, False, "HTTP 204: unexpected status"),
        (400, ErrorKind.UNEXPECTED_STATUS, False, "HTTP 400: unexpected status"),
        (404, ErrorKind.UNEXPECTED_STATUS, False, "HTTP 404: unexpected status"),
        (408, ErrorKind.UNEXPECTED_STATUS, False, "HTTP 408: unexpected status"),
        (413, ErrorKind.UNEXPECTED_STATUS, False, "HTTP 413: unexpected status"),
    ],
)
def test_non_200_status_is_a_sanitized_failure(status: int, kind: ErrorKind, retryable: bool, message: str) -> None:
    transport = FakeTransport(_response(status, _error_body(), headers={"Location": "https://evil.example/steal"}))
    outcome = _client(transport).evaluate(_request())
    error = _failed(outcome, kind)
    assert (error.http_status, error.retryable, error.message) == (status, retryable, message)
    assert len(transport.requests) == 1
    _assert_no_secrets(outcome.model_dump_json())


def test_success_from_another_url_is_treated_as_a_redirect() -> None:
    transport = FakeTransport(_response(200, _ok_body(), final_url="https://evil.example/v1/systemone"))
    error = _failed(_client(transport).evaluate(_request()), ErrorKind.REDIRECT)
    assert (error.http_status, error.message) == (200, "transport reported a different final URL")


@pytest.mark.parametrize(
    ("raised", "kind", "retryable", "message"),
    [
        (TimeoutError(EXC_SENTINEL), ErrorKind.TIMEOUT, True, "no response within 30s"),
        (ssl.SSLCertVerificationError(EXC_SENTINEL), ErrorKind.TLS, False, "TLS failed (SSLCertVerificationError)"),
        (ssl.SSLError(EXC_SENTINEL), ErrorKind.TLS, False, "TLS failed (SSLError)"),
        (
            ConnectionRefusedError(EXC_SENTINEL),
            ErrorKind.CONNECTION,
            True,
            "connection failed (ConnectionRefusedError)",
        ),
        (socket.gaierror(EXC_SENTINEL), ErrorKind.CONNECTION, True, "connection failed (gaierror)"),
        (
            http.client.RemoteDisconnected(EXC_SENTINEL),
            ErrorKind.CONNECTION,
            True,
            "connection failed (RemoteDisconnected)",
        ),
        (
            http.client.IncompleteRead(EXC_SENTINEL.encode()),
            ErrorKind.CONNECTION,
            True,
            "connection failed (IncompleteRead)",
        ),
        (
            TransportError(ErrorKind.RATE_LIMIT, "throttled before the endpoint"),
            ErrorKind.RATE_LIMIT,
            True,
            "throttled before the endpoint",
        ),
        (
            TransportError(ErrorKind.UNEXPECTED_STATUS, "HTTP status 999 is out of range"),
            ErrorKind.UNEXPECTED_STATUS,
            False,
            "HTTP status 999 is out of range",
        ),
    ],
)
def test_transport_failures_become_sanitized_outcomes(
    raised: BaseException, kind: ErrorKind, retryable: bool, message: str
) -> None:
    outcome = _client(FakeTransport(raised)).evaluate(_request())
    error = _failed(outcome, kind)
    assert (error.http_status, error.retryable, error.message) == (None, retryable, message)
    _assert_no_secrets(outcome.model_dump_json())


def test_a_transport_bug_is_not_hidden() -> None:
    with pytest.raises(RuntimeError):
        _client(FakeTransport(RuntimeError("bug"))).evaluate(_request())


def test_no_secret_reaches_repr_logs_or_output(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
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
    recorder = ConnectionRecorder(FakeHttpResponse(302, [("Location", "https://evil.example/steal")], _error_body()))
    outcome = _client(HttpsTransport(connection_factory=recorder)).evaluate(_request())
    error = _failed(outcome, ErrorKind.REDIRECT)
    assert error.http_status == 302
    assert [(c.host, c.port) for c in recorder.connections] == [("api.typesafe.ai", 443)]
    [(method, path, _, headers)] = recorder.connections[0].sent
    assert (method, path, headers["Authorization"]) == ("POST", "/v1/systemone", f"Bearer {KEY}")
    _assert_no_secrets(outcome.model_dump_json())


def test_loopback_success_goes_through_http_client(make_server: Callable[..., LoopbackServer]) -> None:
    server = make_server(200, {"X-TypeSafe-Request-Id": "req-loop"}, _ok_body())
    outcome = _client(HttpsTransport(connection_factory=server.connection_factory)).evaluate(_request())
    assert outcome.ok and outcome.response is not None and outcome.response.request_id == "req-loop"
    [(method, path, authorization, body)] = server.received
    assert (method, path, authorization) == ("POST", "/v1/systemone", f"Bearer {KEY}")
    assert hashlib.sha256(body).hexdigest() == outcome.request_sha256


def test_loopback_timeout_becomes_a_timeout_outcome(make_server: Callable[..., LoopbackServer]) -> None:
    server = make_server(200, {}, _ok_body(), delay_s=0.5)
    transport = HttpsTransport(connection_factory=server.connection_factory)
    outcome = _client(transport, timeout_s=0.1).evaluate(_request())
    error = _failed(outcome, ErrorKind.TIMEOUT)
    assert error.message == "no response within 0.1s"
    assert outcome.elapsed_ms is not None and outcome.elapsed_ms < 500


def test_loopback_redirect_becomes_a_redirect_outcome(make_server: Callable[..., LoopbackServer]) -> None:
    server = make_server(302, {"Location": "http://127.0.0.1:9/steal"}, _error_body())
    outcome = _client(HttpsTransport(connection_factory=server.connection_factory)).evaluate(_request())
    error = _failed(outcome, ErrorKind.REDIRECT)
    assert error.http_status == 302
    assert len(server.received) == 1
    assert server.requested == [("api.typesafe.ai", 443)]
    _assert_no_secrets(outcome.model_dump_json())


def test_loopback_body_cut_before_its_content_length_is_a_connection_failure(
    make_server: Callable[..., LoopbackServer],
) -> None:
    # 実際の http.client で、Content-Length 未達の切断が chunked の途中切断と同じ再送可能な connection になる
    server = make_server(200, {"Content-Type": "application/json"}, TRUNCATED_BODY, declared_length=1000)
    outcome = _client(HttpsTransport(connection_factory=server.connection_factory)).evaluate(_request())
    error = _failed(outcome, ErrorKind.CONNECTION)
    assert (error.http_status, error.retryable, error.message) == (
        None,
        True,
        "response body ended before Content-Length",
    )
    assert len(server.received) == 1
    dumped = outcome.model_dump_json()
    _assert_no_secrets(dumped)
    assert TRUNCATED_BODY.decode() not in dumped


# --- 補助関数 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, None),
        ({API_KEY_ENV: ""}, None),
        ({API_KEY_ENV: "  \n"}, None),
        ({API_KEY_ENV: " abc\n"}, "abc"),
        ({"OTHER_API_KEY": "x", "TYPESAFE_TOKEN": "y"}, None),
    ],
)
def test_api_key_is_read_only_from_its_variable(environ: dict[str, str], expected: str | None) -> None:
    assert api_key_from_env(environ) == expected


def test_api_key_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, " from-env\n")
    assert api_key_from_env() == "from-env"
    monkeypatch.delenv(API_KEY_ENV)
    assert api_key_from_env() is None


def test_connectivity_request_is_synthetic_and_sendable() -> None:
    request = connectivity_request()
    assert request.model == DEFAULT_MODEL_ID
    assert request.state == {"note": "The sky is blue on a clear day."}
    assert list(request.questions) == ["sky_is_blue"]
    assert (request.purpose, request.finding_ids) == ("finding", ("connectivity-check",))
    assert request.payload_bytes() < 1_000
    body = _json(
        {
            "model": "jev-latest",
            "answers": {"sky_is_blue": {"type": "noul", "noul": 0.99}},
            "usage": {"input_tokens": 20, "output_tokens": 1},
        }
    )
    assert _client(FakeTransport(_response(200, body))).evaluate(request).ok


def test_connectivity_request_uses_the_given_model() -> None:
    assert connectivity_request(model="jev-1.13.0").model == "jev-1.13.0"


def test_offline_check_in_the_access_guide_reports_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # access.rst の「キーなしの確認」と同じ呼び出し
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    outcome = SystemOneClient(api_key=api_key_from_env(), model=DEFAULT_MODEL_ID).evaluate(connectivity_request())
    _refused(outcome, ErrorKind.NO_KEY)
