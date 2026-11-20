import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Request
from indexer.contracts import IndexerError
from pydantic import BaseModel, Field


def encode(data: bytes):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode(data: str):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign(payload: dict, key: bytes):
    token = encode(json.dumps(payload, separators=(",", ":")).encode())
    return token + "." + encode(hmac.digest(key, token.encode(), "sha256"))


def verify(token: str, key: bytes, purpose: str):
    try:
        if len(token) > 4096:
            return None
        data, signature = token.split(".")
        if not hmac.compare_digest(encode(hmac.digest(key, data.encode(), "sha256")), signature):
            return None
        claims = json.loads(decode(data))
        if claims["purpose"] == purpose and type(claims["exp"]) is int and claims["exp"] > time.time():
            return claims
    except (ValueError, TypeError, KeyError, binascii.Error):
        pass
    return None


class Login(BaseModel):
    access_key: str = Field(default="", max_length=512)


class EmbedExchange(BaseModel):
    code: str = Field(max_length=4096)
    nonce: str = Field(min_length=16, max_length=128)
    parent_origin: str = Field(max_length=512)


class Auth:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.key = secrets.token_bytes(32)

    def local(self, request):
        return bool(
            self.config.dev_auth
            and request.client
            and request.client.host in {"127.0.0.1", "::1"}
            and request.url.hostname in {"127.0.0.1", "::1", "localhost"}
        )

    def origin(self, request):
        return self.config.public_origin or str(request.base_url).rstrip("/")

    def same_origin(self, request):
        if request.headers.get("origin") != self.origin(request):
            raise IndexerError("ORIGIN_FORBIDDEN", "이 출처에서는 요청할 수 없습니다.", 403)

    def login(self, request, access_key):
        self.same_origin(request)
        local = self.local(request)
        if not local and not self.config.access_key:
            raise IndexerError("AUTH_NOT_CONFIGURED", "서버 접근 키가 설정되지 않았습니다.", 503)
        if self.config.access_key and not hmac.compare_digest(
            access_key.encode(), self.config.access_key.encode()
        ):
            raise IndexerError("AUTH_REQUIRED", "접근 키를 확인해 주세요.", 401)
        claims = {"purpose": "session", "exp": int(time.time()) + 28800, "csrf": secrets.token_urlsafe(24)}
        return sign(claims, self.key), claims["csrf"], not local

    def session(self, request: Request):
        token = request.cookies.get("indexer_session", "")
        claims = verify(token, self.key, "session") if token else None
        if claims:
            return claims
        raise IndexerError("AUTH_REQUIRED", "로그인이 필요합니다.", 401)

    def require(self, request: Request):
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization[7:]
            if verify(token, self.key, "embed_access"):
                return
            raise IndexerError("AUTH_REQUIRED", "접근 권한이 만료되었습니다.", 401)
        claims = self.session(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.same_origin(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), claims["csrf"]):
                raise IndexerError("CSRF_FORBIDDEN", "세션이 갱신되었습니다. 다시 시도해 주세요.", 403)

    def require_settings(self, request: Request):
        if request.headers.get("authorization", "").startswith("Bearer "):
            raise IndexerError("SETTINGS_FORBIDDEN", "삽입 화면에서는 서버 설정을 변경할 수 없습니다.", 403)
        self.require(request)

    def exchange(self, request, body):
        self.same_origin(request)
        if not self.config.embed_secret or body.parent_origin not in self.config.frame_origins:
            raise IndexerError("EMBED_FORBIDDEN", "허용된 삽입 환경이 아닙니다.", 403)
        claims = verify(body.code, self.config.embed_secret.encode(), "embed_code")
        timestamp = int(time.time())
        if (
            not claims
            or claims.get("aud") != self.origin(request)
            or claims.get("host") != body.parent_origin
            or claims.get("nonce") != body.nonce
            or not isinstance(claims.get("id"), str)
            or not 8 <= len(claims["id"]) <= 128
            or claims["exp"] > timestamp + 120
        ):
            raise IndexerError("EMBED_FORBIDDEN", "삽입 인증 코드가 유효하지 않습니다.", 403)
        if not self.store.redeem(claims["id"], claims["exp"], timestamp):
            raise IndexerError("EMBED_CODE_REUSED", "이미 사용된 삽입 인증 코드입니다.", 403)
        token = sign(
            {
                "purpose": "embed_access",
                "exp": timestamp + 900,
                "session": hashlib.sha256(body.code.encode()).hexdigest(),
            },
            self.key,
        )
        return {"access_token": token, "expires_in": 900}
