"""OpenAI-compatible chat completion client and encrypted model credentials."""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken


class ModelAPIError(RuntimeError):
    """A safe-to-display model API error; never contains request credentials."""


def _cipher() -> Fernet:
    key = os.getenv("MODEL_CREDENTIAL_KEY", "").strip()
    if not key:
        raise ModelAPIError(
            "服务器尚未配置 MODEL_CREDENTIAL_KEY，暂时不能保存或调用模型 API。"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ModelAPIError(
            "MODEL_CREDENTIAL_KEY 格式无效，请使用 Fernet.generate_key() 生成。"
        ) from None


def encrypt_api_key(api_key: str) -> str:
    return _cipher().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ModelAPIError(
            "API Key 无法解密，请确认 MODEL_CREDENTIAL_KEY 与保存时完全一致。"
        ) from None
    except (ValueError, UnicodeEncodeError):
        raise ModelAPIError("保存的 API Key 数据无效。") from None


def validate_base_url(value: str) -> str:
    """Require HTTPS and reject local/private destinations to reduce SSRF risk."""
    value = value.strip().rstrip("/")
    if not value or len(value) > 500:
        raise ModelAPIError("Base URL 长度无效。")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        raise ModelAPIError("Base URL 格式无效。") from None
    if (parsed.scheme.lower() != "https" or not host or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ModelAPIError("Base URL 必须是 HTTPS 地址，且不能包含账号、密码、查询参数或片段。")
    host = host.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
        raise ModelAPIError("出于安全考虑，不允许连接本机或局域网地址。")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise ModelAPIError("无法解析 Base URL 主机名，请检查地址。") from None
        addresses = []
        for item in resolved:
            try:
                addresses.append(ipaddress.ip_address(item[4][0]))
            except ValueError:
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise ModelAPIError("Base URL 指向非公网地址，已拒绝连接。")
    return value


async def chat_completion(profile: dict, messages: list[dict], api_key: str, transport=None) -> dict:
    """Call the OpenAI-compatible /chat/completions endpoint without following redirects."""
    base_url = validate_base_url(profile["base_url"])
    payload = {
        "model": profile["model"],
        "messages": messages,
        "temperature": float(profile.get("temperature", 0.7)),
        "max_tokens": int(profile.get("max_tokens", 2048)),
    }
    timeout = max(10, min(int(profile.get("timeout_seconds", 60)), 120))
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            response = await client.post(
                base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.TimeoutException:
        raise ModelAPIError(f"模型请求超过 {timeout} 秒，已超时。") from None
    except httpx.HTTPError:
        raise ModelAPIError("无法连接模型服务，请检查 Base URL 和服务状态。") from None
    if response.status_code < 200 or response.status_code >= 300:
        raise ModelAPIError(
            f"模型服务返回 HTTP {response.status_code}，请检查 API Key、模型名称和账户额度。"
        )
    try:
        data = response.json()
        message = data["choices"][0]["message"]
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in ("text", "output_text")
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
    except (ValueError, TypeError, KeyError, IndexError):
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
