"""OpenAI-compatible chat completion client and encrypted model credentials."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpcore
from cryptography.fernet import Fernet, InvalidToken


class ModelAPIError(RuntimeError):
    """A safe-to-display model API error; never contains request credentials."""


@dataclass(frozen=True)
class ResolvedEndpoint:
    """An HTTPS endpoint whose DNS result is pinned for the following connection."""

    base_url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def _load_cipher(*, required: bool = False) -> Fernet | None:
    key = os.getenv("MODEL_CREDENTIAL_KEY", "").strip()
    if not key:
        if required:
            raise ModelAPIError(
                "服务器尚未配置 MODEL_CREDENTIAL_KEY，暂时不能保存或调用模型 API。"
            )
        return None
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ModelAPIError(
            "MODEL_CREDENTIAL_KEY 格式无效，请使用 Fernet.generate_key() 生成。"
        ) from None


def validate_credential_key(*, required: bool = False) -> bool:
    """Validate the configured Fernet key without exposing its value."""
    return _load_cipher(required=required) is not None


def has_valid_credential_key() -> bool:
    """Return whether a syntactically valid Fernet key is configured."""
    try:
        return validate_credential_key()
    except ModelAPIError:
        return False


def _cipher() -> Fernet:
    cipher = _load_cipher(required=True)
    if cipher is None:
        raise ModelAPIError(
            "服务器尚未配置 MODEL_CREDENTIAL_KEY，暂时不能保存或调用模型 API。"
        )
    return cipher


def encrypt_api_key(api_key: str) -> str:
    return _cipher().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ModelAPIError("保存的 API Key 数据无效。")
    try:
        return _cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ModelAPIError(
            "API Key 无法解密，请确认 MODEL_CREDENTIAL_KEY 与保存时完全一致。"
        ) from None
    except (ValueError, UnicodeEncodeError, TypeError):
        raise ModelAPIError("保存的 API Key 数据无效。") from None


def resolve_base_url(value: str) -> ResolvedEndpoint:
    """Validate an HTTPS base URL and resolve every address exactly once."""
    value = value.strip().rstrip("/")
    if not value or len(value) > 500:
        raise ModelAPIError("Base URL 长度无效。")
    try:
        parsed = urlsplit(value)
        raw_host = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        raise ModelAPIError("Base URL 格式无效。") from None
    if (parsed.scheme.lower() != "https" or not raw_host or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ModelAPIError("Base URL 必须是 HTTPS 地址，且不能包含账号、密码、查询参数或片段。")

    try:
        host = raw_host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ModelAPIError("Base URL 主机名格式无效。") from None
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
        raise ModelAPIError("出于安全考虑，不允许连接本机或局域网地址。")

    try:
        literal = ipaddress.ip_address(host)
        resolved_addresses = [literal]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        except OSError:
            raise ModelAPIError("无法解析 Base URL 主机名，请检查地址。") from None
        resolved_addresses = []
        for item in resolved:
            try:
                resolved_addresses.append(ipaddress.ip_address(item[4][0]))
            except ValueError:
                continue

    if not resolved_addresses or any(not address.is_global for address in resolved_addresses):
        raise ModelAPIError("Base URL 指向非公网地址，已拒绝连接。")

    # Deduplicate without performing another DNS lookup later.
    addresses = tuple(dict.fromkeys(address.compressed for address in resolved_addresses))
    is_ipv6_literal = False
    try:
        is_ipv6_literal = ipaddress.ip_address(host).version == 6
    except ValueError:
        pass
    netloc_host = f"[{host}]" if is_ipv6_literal else host
    netloc = netloc_host if port == 443 else f"{netloc_host}:{port}"
    path = parsed.path.rstrip("/")
    base_url = urlunsplit(("https", netloc, path, "", ""))
    return ResolvedEndpoint(base_url=base_url, host=host, port=port, addresses=addresses)


def validate_base_url(value: str) -> str:
    """Compatibility wrapper used when saving a model profile."""
    return resolve_base_url(value).base_url


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to addresses from the already validated DNS result.

    HTTP Core still sees the original hostname as the request origin, so its TLS
    layer keeps using that hostname for SNI and certificate verification.
    """

    def __init__(self, endpoint: ResolvedEndpoint, delegate=None):
        self.endpoint = endpoint
        self.delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        normalized = host.rstrip(".").lower()
        if normalized != self.endpoint.host or port != self.endpoint.port:
            raise httpcore.ConnectError("Pinned endpoint host or port changed before connect.")

        last_error = None
        for address in self.endpoint.addresses:
            try:
                return await self.delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("Pinned endpoint has no usable address.")

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not allowed for model API requests.")

    async def sleep(self, seconds: float) -> None:
        await self.delegate.sleep(seconds)


async def chat_completion(
    profile: dict,
    messages: list[dict],
    api_key: str,
    *,
    network_backend=None,
) -> dict:
    """Call /chat/completions using a DNS-pinned HTTPS connection."""
    endpoint = await asyncio.to_thread(resolve_base_url, profile["base_url"])
    payload = {
        "model": profile["model"],
        "messages": messages,
        "temperature": float(profile.get("temperature", 0.7)),
        "max_tokens": int(profile.get("max_tokens", 2048)),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timeout = max(10, min(int(profile.get("timeout_seconds", 60)), 120))
    timeouts = {"connect": timeout, "read": timeout, "write": timeout, "pool": timeout}
    backend = _PinnedNetworkBackend(endpoint, delegate=network_backend)

    try:
        async with httpcore.AsyncConnectionPool(
            network_backend=backend,
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
        ) as pool:
            response = await pool.request(
                "POST",
                endpoint.base_url + "/chat/completions",
                headers=[
                    ("Authorization", "Bearer " + api_key),
                    ("Content-Type", "application/json"),
                    ("Accept", "application/json"),
                ],
                content=body,
                extensions={"timeout": timeouts},
            )
    except httpcore.TimeoutException:
        raise ModelAPIError(f"模型请求超过 {timeout} 秒，已超时。") from None
    except (httpcore.NetworkError, httpcore.ProtocolError):
        raise ModelAPIError("无法安全连接模型服务，请检查 Base URL 和服务状态。") from None

    if response.status < 200 or response.status >= 300:
        raise ModelAPIError(
            f"模型服务返回 HTTP {response.status}，请检查 API Key、模型名称和账户额度。"
        )
    try:
        data = json.loads(response.content)
        message = data["choices"][0]["message"]
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in ("text", "output_text")
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        raise ModelAPIError("模型服务返回的数据不符合 Chat Completions 格式。") from None
    usage = data.get("usage") if isinstance(data, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    return {
        "text": content.strip(),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
    }


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ModelAPIError("AI 裁判没有返回有效的 JSON 判决。")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ModelAPIError("AI 裁判返回的 JSON 无法解析。") from None
    if not isinstance(value, dict):
        raise ModelAPIError("AI 裁判的判决格式必须是 JSON 对象。")
    return value
