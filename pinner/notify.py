"""Telegram notifications — fire-and-forget by contract.

A broken notification must NEVER kill a run: ``send_telegram`` swallows all
errors and returns False. Uses the shared tools HTTP seam for testability."""

from __future__ import annotations

from pinner.tools.http import Transport, httpx_transport


def send_telegram(
    text: str, *, bot_token: str, chat_id: str, transport: Transport | None = None
) -> bool:
    if not bot_token or not chat_id:
        return False  # not configured — silently skip
    transport = transport or httpx_transport
    try:
        reply = transport(
            "POST",
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json_body={"chat_id": chat_id, "text": text[:4000]},
        )
        return reply.status == 200
    except Exception:
        return False
