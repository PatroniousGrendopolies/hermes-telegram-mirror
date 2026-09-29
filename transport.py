"""Minimal Bot API transport. Credentials remain in memory, never in diagnostics."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request


def scrub(value: object, token: str = "") -> str:
    text = str(value)
    if token:
        for secret in (token, urllib.parse.quote(token, safe="")):
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", text)


def chunks(text: str, limit: int = 4000) -> list[str]:
    """Conservative UTF-16 limit, including astral emoji; preserve exact text."""
    if limit < 2:
        raise ValueError("chunk limit must be at least 2")
    result, start, units = [], 0, 0
    for i, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            result.append(text[start:i])
            start, units = i, 0
        units += size
    if start < len(text):
        result.append(text[start:])
    return result


class APIError(Exception):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        self.code = code
        self.retry_after = max(0, retry_after)
        self.description = scrub(description)
        super().__init__(f"Telegram API {code}: {self.description}")


class UncertainSend(Exception):
    """The server may have accepted the request. Never automatically resend."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Telegram:
    def __init__(self, service: str, account: str, chat_id: int):
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service,
             "-a", account, "-w"], capture_output=True, timeout=10,
        )
        if result.returncode:
            raise RuntimeError("Telegram mirror Keychain lookup failed")
        self._token = result.stdout.decode().strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", self._token):
            raise RuntimeError("Telegram mirror Keychain item has invalid format")
        self.chat_id = int(chat_id)
        # Do not send the token through environment-configured HTTP proxies.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        )

    def call(self, method: str, payload: dict | None = None, timeout: float = 5):
        if method not in {"getMe", "getUpdates", "getWebhookInfo", "sendMessage", "editMessageText", "sendChatAction"}:
            raise ValueError("Unsupported Telegram API method")
        payload = dict(payload or {})
        if method in {"sendMessage", "editMessageText", "sendChatAction"}:
            if int(payload.get("chat_id", 0)) != self.chat_id:
                raise ValueError("Telegram destination is not configured chat")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    data = json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    data = json.loads(exc.read())
                except Exception:
                    raise UncertainSend("Telegram returned an unreadable HTTP response") from None
            if not isinstance(data, dict):
                raise UncertainSend("Telegram returned an invalid response")
            if not data.get("ok"):
                raise APIError(
                    int(data.get("error_code", 0)),
                    scrub(data.get("description", "request failed"), self._token),
                    int((data.get("parameters") or {}).get("retry_after", 0)),
                )
            return data["result"]
        except (APIError, UncertainSend):
            raise
        except Exception:
            # urllib exceptions include the credential-bearing URL. Never expose them.
            raise UncertainSend("Telegram network outcome unknown") from None

    def send(self, text: str, html: str | None = None, message_id: int | None = None) -> dict:
        text = scrub(text, self._token)
        payload = {
            "chat_id": self.chat_id, "text": scrub(html, self._token) if html else text,
            "link_preview_options": {"is_disabled": True},
        }
        if html:
            payload['parse_mode'] = 'HTML'
        if message_id is not None:
            payload['message_id'] = message_id
        else:
            payload.update(disable_notification=False, protect_content=False)
        result = self.call('editMessageText' if message_id is not None else 'sendMessage', payload)
        if (result.get("chat", {}).get("id") != self.chat_id
                or result.get("text", '').strip() != text.strip() or not isinstance(result.get("message_id"), int)):
            raise UncertainSend("Telegram message receipt did not match requested target/text")
        return result
