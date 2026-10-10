"""TypeSafe System One（Jev）の REST クライアント（#385）。

標準ライブラリの ``http.client`` で request を 1 回だけ送り、
どの失敗でも例外を投げずに ``ClientOutcome`` を返す。
redirect を追わず、自動 retry をしない。
キーは ``TYPESAFE_API_KEY`` だけから読み、outcome・message・repr に出さない。
仕様の定義元: ``interfaces.rst`` の「client の振る舞い」「エラー種別と意味」。
"""

import hashlib
import http.client
import json
import math
import os
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Protocol, TypeGuard

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

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
    SystemOneRequest,
    SystemOneResponse,
    Transport,
    state_digest,
)

RESPONSE_BYTE_LIMIT = 1_000_000  # 応答本文の上限。v1 の応答は数 KB
_MESSAGE_LIMIT = 200  # ClientError.message の上限と同じ
_READ_CHUNK = 65_536

API_KEY_ENV = "TYPESAFE_API_KEY"
REQUEST_ID_HEADER = "x-typesafe-request-id"
MAX_TIMEOUT_S = 300.0
_MAX_KEY_CHARS = 4096
_USER_AGENT = "maatlog-review-triage/1"
_KEY_PATTERN = re.compile(r"[\x21-\x7e]+")  # HTTP ヘッダ値に使える可視 ASCII だけ
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_RETRYABLE = frozenset({ErrorKind.TIMEOUT, ErrorKind.CONNECTION, ErrorKind.RATE_LIMIT, ErrorKind.SERVICE})


class TransportError(Exception):
    """transport が失敗種別を明示するときに送出する。``safe_message`` にキー・state・応答本文を入れない。"""

    def __init__(self, kind: ErrorKind, safe_message: str) -> None:
        self.kind = kind
        self.safe_message = safe_message[:_MESSAGE_LIMIT]
        super().__init__(self.safe_message)


class ResponseLike(Protocol):
    """``http.client.HTTPResponse`` のうち transport が使う部分。"""

    status: int

    def getheaders(self) -> list[tuple[str, str]]: ...

    def read(self, amt: int | None = None, /) -> bytes: ...


class ConnectionLike(Protocol):
    """``http.client.HTTPConnection`` のうち transport が使う部分。"""

    def request(self, method: str, url: str, body: bytes, headers: Mapping[str, str]) -> None: ...

    def getresponse(self) -> ResponseLike: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, int, float], ConnectionLike]


def _https_connection(host: str, port: int, timeout: float) -> ConnectionLike:
    """証明書とホスト名を検証する HTTPS 接続。proxy の環境変数は読まない。"""
    connection: http.client.HTTPSConnection = http.client.HTTPSConnection(
        host, port, timeout=timeout, context=ssl.create_default_context()
    )
    # プロセス全体の HTTPConnection.debuglevel が 1 以上だと http.client が送信ヘッダ（Authorization）と本文を
    # 標準出力へ print するため、既定の接続では常に 0 にする。
    connection.set_debuglevel(0)
    return connection


def _merge_headers(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """ヘッダ名を小文字にし、同名のヘッダは ``", "`` で連結する。"""
    merged: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        merged[key] = f"{merged[key]}, {value}" if key in merged else value
    return merged


def _read_body(response: ResponseLike, limit: int, deadline: float) -> bytes:
    """締切までに最大 ``limit`` バイトを読む。締切を過ぎたら ``TimeoutError``。"""
    chunks: list[bytes] = []
    size = 0
    while size < limit:
        if time.monotonic() > deadline:
            raise TimeoutError("response body was not read before the deadline")
        chunk = response.read(min(_READ_CHUNK, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _declared_length(status: int, headers: Mapping[str, str]) -> int | None:
    """本文の長さとして宣言された Content-Length。本文の長さを示さない応答では ``None``。

    本文を持たない status（1xx・204・304）、Transfer-Encoding がある応答（http.client は Content-Length を使わない）、
    1 個の非負整数として読めない値（``_merge_headers`` が連結した複数値や、int の桁数上限を超える値を含む）は
    ``None`` とする。
    """
    if 100 <= status < 200 or status in (204, 304) or "transfer-encoding" in headers:
        return None
    value = headers.get("content-length", "").strip()
    if not (value.isascii() and value.isdigit()):
        return None
    try:
        return int(value)
    except ValueError:  # int の桁数上限（既定 4300 桁）を超える。http.client も長さ不明として扱う
        return None


class HttpsTransport:
    """``http.client`` による既定の transport。

    1 回だけ送り、redirect を追わず 3xx をそのまま返す。
    本文は ``max_response_bytes + 1`` バイトで打ち切り、上限を超えたかの判定は client に任せる。
    本文が Content-Length より前に終わったら ``TransportError(CONNECTION)`` を送出する。
    """

    def __init__(
        self,
        *,
        max_response_bytes: int = RESPONSE_BYTE_LIMIT,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if isinstance(max_response_bytes, bool) or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive int")
        self._max_response_bytes = max_response_bytes
        self._connect: ConnectionFactory = connection_factory or _https_connection

    def send(self, request: HttpRequest) -> HttpResponse:
        parts = urllib.parse.urlsplit(request.url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("HttpsTransport sends only to a plain https URL")
        deadline = time.monotonic() + request.timeout_s
        connection = self._connect(parts.hostname, parts.port or 443, request.timeout_s)
        try:
            connection.request(request.method, parts.path or "/", request.body, request.headers)
            response = connection.getresponse()
            status = response.status
            if not 100 <= status <= 599:
                raise TransportError(ErrorKind.UNEXPECTED_STATUS, f"HTTP status {status} is out of range")
            headers = _merge_headers(response.getheaders())
            limit = self._max_response_bytes + 1
            body = _read_body(response, limit, deadline)
            # http.client は Content-Length 未達の切断を IncompleteRead にせず空バイトで返す。
            # limit 未満で読み終えたのは空バイト（EOF）で止まった場合だけなので、長さの比較で途中切断と分かる
            declared = _declared_length(status, headers)
            if declared is not None and len(body) < min(declared, limit):
                raise TransportError(ErrorKind.CONNECTION, "response body ended before Content-Length")
        finally:
            connection.close()
        return HttpResponse(status=status, headers=headers, body=body, final_url=request.url)


def _encode_body(request: SystemOneRequest) -> bytes:
    """``SystemOneRequest.payload_bytes()`` と同じ直列化で送信本文を作る。"""
    return json.dumps(request.to_api_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _failure(
    request: SystemOneRequest,
    request_sha256: str,
    kind: ErrorKind,
    message: str,
    *,
    http_status: int | None = None,
    calls_made: int = 0,
    elapsed_ms: int | None = None,
) -> ClientOutcome:
    error = ClientError(
        kind=kind, http_status=http_status, message=message[:_MESSAGE_LIMIT], retryable=kind in _RETRYABLE
    )
    return ClientOutcome(
        ok=False,
        error=error,
        model_requested=request.model,
        request_sha256=request_sha256,
        purpose=request.purpose,
        finding_ids=request.finding_ids,
        elapsed_ms=elapsed_ms,
        calls_made=calls_made,
    )


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _request_id(headers: Mapping[str, str]) -> str | None:
    """``x-typesafe-request-id`` の値。安全な文字の 128 文字以内でなければ ``None``。"""
    for name, value in headers.items():
        if name.lower() == REQUEST_ID_HEADER:
            return value if _REQUEST_ID_PATTERN.fullmatch(value) else None
    return None


def _parse_response(response: HttpResponse) -> SystemOneResponse | str:
    """200 の本文を解析した結果、または失敗理由（サニタイズ済みの短文）を返す。"""
    if len(response.body) > RESPONSE_BYTE_LIMIT:
        return f"response body exceeds {RESPONSE_BYTE_LIMIT} bytes"
    try:
        loaded: object = json.loads(response.body.decode("utf-8"))
    except ValueError, RecursionError:  # UnicodeDecodeError と JSONDecodeError は ValueError の派生
        return "response is not valid JSON"
    if not _is_object(loaded):
        return "response JSON is not an object"
    try:
        # strict: 真偽値や数値の文字列を数値へ変換しない（true を 1.0 にしない）。float の欄は JSON の整数を受け取る
        parsed = SystemOneResponse.model_validate({**loaded, "request_id": _request_id(response.headers)}, strict=True)
    except ValidationError:
        return "response does not match the SystemOneResponse schema"
    try:
        # 未知フィールドは値を検証せずに残すため、孤立サロゲートや深い入れ子を含むと JSON へ書き出せない。
        # ok=True の outcome を model_dump_json() できるよう、ここで確かめる
        parsed.model_dump_json()
    except PydanticSerializationError:
        return "response cannot be serialized as JSON"
    return parsed


_STATUS_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.REDIRECT: "HTTP {status} redirect not followed",
    ErrorKind.AUTH: "HTTP {status}: authentication rejected",
    ErrorKind.INVALID_REQUEST: "HTTP {status}: request failed validation",
    ErrorKind.RATE_LIMIT: "HTTP {status}: rate limited",
    ErrorKind.SERVICE: "HTTP {status}: service unavailable",
    ErrorKind.UNEXPECTED_STATUS: "HTTP {status}: unexpected status",
}


def _status_kind(status: int) -> ErrorKind:
    """200 以外の status の失敗種別（interfaces.rst の ErrorKind 表）。"""
    if 300 <= status <= 399:
        return ErrorKind.REDIRECT
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 422:
        return ErrorKind.INVALID_REQUEST
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if 500 <= status <= 599:
        return ErrorKind.SERVICE
    return ErrorKind.UNEXPECTED_STATUS


def _judge(request: SystemOneRequest, request_sha256: str, response: HttpResponse, elapsed_ms: int) -> ClientOutcome:
    """送信後の応答を outcome にする。"""
    status = response.status

    def failed(kind: ErrorKind, message: str) -> ClientOutcome:
        return _failure(
            request, request_sha256, kind, message, http_status=status, calls_made=1, elapsed_ms=elapsed_ms
        )

    if response.final_url != API_ENDPOINT:  # transport が redirect を追った場合の多重防御
        return failed(ErrorKind.REDIRECT, "transport reported a different final URL")
    if status != 200:
        kind = _status_kind(status)
        return failed(kind, _STATUS_MESSAGES[kind].format(status=status))
    parsed = _parse_response(response)
    if isinstance(parsed, str):
        return failed(ErrorKind.MALFORMED_RESPONSE, parsed)
    problems = parsed.check_against(request)
    if problems:
        return failed(ErrorKind.MALFORMED_RESPONSE, f"answers do not match the request ({len(problems)} problem(s))")
    return ClientOutcome(
        ok=True,
        response=parsed,
        model_requested=request.model,
        request_sha256=request_sha256,
        purpose=request.purpose,
        finding_ids=request.finding_ids,
        elapsed_ms=elapsed_ms,
        calls_made=1,
    )


class SystemOneClient:
    """Jev へ request を 1 回だけ送り ``ClientOutcome`` を返す。失敗は例外にせず outcome で返す。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        transport: Transport | None = None,
        timeout_s: float = 30.0,
        max_request_bytes: int = REQUEST_BYTE_LIMIT,
        max_calls: int = 50,
    ) -> None:
        if not model:
            raise ValueError("model must not be empty")
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout_s must be in (0, {MAX_TIMEOUT_S:g}]")
        if isinstance(max_request_bytes, bool) or not 1 <= max_request_bytes <= REQUEST_BYTE_LIMIT:
            raise ValueError(f"max_request_bytes must be in [1, {REQUEST_BYTE_LIMIT}]")
        if isinstance(max_calls, bool) or max_calls < 0:
            raise ValueError("max_calls must be a non-negative int")
        self._api_key = api_key
        self._model = model
        self._transport = transport
        self._timeout_s = float(timeout_s)
        self._max_request_bytes = max_request_bytes
        self._max_calls = max_calls
        self._calls_made = 0

    def __repr__(self) -> str:
        key_state = "<set>" if self._api_key else "<unset>"
        return (
            f"SystemOneClient(model={self._model!r}, api_key={key_state}, timeout_s={self._timeout_s:g}, "
            f"max_request_bytes={self._max_request_bytes}, max_calls={self._max_calls}, calls_made={self._calls_made})"
        )

    def evaluate(self, request: SystemOneRequest) -> ClientOutcome:
        """request を 1 回だけ送る。送信前の検査に当たれば通信せずに失敗の outcome を返す。"""
        not_utf8 = False
        try:
            body = _encode_body(request)
            for finding_id in request.finding_ids:
                finding_id.encode("utf-8")
        except UnicodeEncodeError:
            # pydantic は孤立サロゲートを含む str を受け付けるため、ここで弾く（Plan の contingency）。
            # finding_ids は payload に含まれないが outcome へ転記するため、同じ検査にかける。
            # Spec §5.3 は検査 1（no_key）を最優先にするため、ここでは request_sha256 の計算だけ行い、
            # invalid_request の返却はキー検査の後（_refusal の前）まで遅らせる。
            body = json.dumps(request.to_api_payload(), ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8", errors="surrogatepass"
            )
            not_utf8 = True
        request_sha256 = hashlib.sha256(body).hexdigest()
        key = self._api_key
        if key is None or not key.strip():
            return _failure(request, request_sha256, ErrorKind.NO_KEY, "no API key configured")
        if len(key) > _MAX_KEY_CHARS or not _KEY_PATTERN.fullmatch(key):
            return _failure(request, request_sha256, ErrorKind.NO_KEY, "API key is not a valid HTTP header value")
        if not_utf8:
            return _failure(request, request_sha256, ErrorKind.INVALID_REQUEST, "request text is not valid UTF-8")
        refusal = self._refusal(request, len(body))
        if refusal is not None:
            return _failure(request, request_sha256, refusal[0], refusal[1])
        http_request = HttpRequest(
            url=API_ENDPOINT,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            body=body,
            timeout_s=self._timeout_s,
        )
        self._calls_made += 1
        started = time.monotonic()

        def failed(kind: ErrorKind, message: str) -> ClientOutcome:
            return _failure(request, request_sha256, kind, message, calls_made=1, elapsed_ms=_elapsed_ms(started))

        # TimeoutError と ssl.SSLError は OSError の派生なので、この順で捕まえる。
        # ほかの例外は transport のバグとして伝える
        try:
            response = self._get_transport().send(http_request)
        except TransportError as exc:
            return failed(exc.kind, exc.safe_message)
        except TimeoutError:
            return failed(ErrorKind.TIMEOUT, f"no response within {self._timeout_s:g}s")
        except ssl.SSLError as exc:
            return failed(ErrorKind.TLS, f"TLS failed ({type(exc).__name__})")
        except (OSError, http.client.HTTPException) as exc:
            return failed(ErrorKind.CONNECTION, f"connection failed ({type(exc).__name__})")
        return _judge(request, request_sha256, response, _elapsed_ms(started))

    def _refusal(self, request: SystemOneRequest, body_bytes: int) -> tuple[ErrorKind, str] | None:
        """キー以外の送信前検査。最初に当たった (kind, message) を返す。"""
        if state_digest(request.state)[0] != request.state_sha256:
            return ErrorKind.NOT_ALLOWED, "request state does not match state_sha256; refusing to send"
        if request.model != self._model:
            return ErrorKind.INVALID_REQUEST, "request model does not match the client model"
        if body_bytes > self._max_request_bytes:  # body_bytes == request.payload_bytes()
            return ErrorKind.TOO_LARGE, f"request body is {body_bytes} bytes, limit is {self._max_request_bytes}"
        if self._calls_made >= self._max_calls:
            return ErrorKind.CALL_BUDGET, f"call budget of {self._max_calls} request(s) exhausted"
        return None

    def _get_transport(self) -> Transport:
        if self._transport is None:
            self._transport = HttpsTransport()
        return self._transport


def api_key_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """``TYPESAFE_API_KEY`` だけを読む。前後の空白を除いて空なら ``None``。"""
    source = os.environ if environ is None else environ
    return source.get(API_KEY_ENV, "").strip() or None


def connectivity_request(*, model: str = DEFAULT_MODEL_ID) -> SystemOneRequest:
    """疎通確認用の固定の合成 request。秘密やリポジトリの内容を含まない。"""
    state = {"note": "The sky is blue on a clear day."}
    return SystemOneRequest(
        model=model,
        state=state,
        questions={"sky_is_blue": NoulQuestion(instructions="Does `note` say that the sky is blue?")},
        state_sha256=state_digest(state)[0],
        question_set_version="connectivity-1",
        purpose="finding",
        finding_ids=("connectivity-check",),
    )
