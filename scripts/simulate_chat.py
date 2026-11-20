"""
Interactive Terminal Chat Simulator for WhatsApp Agent Gateway.
Allows real-time interactive testing of dialogue turns, tool execution,
and booking confirmations as if messaging from a WhatsApp client.
"""
import asyncio
import hmac
import hashlib
import json
import time
import uuid
import sys
import httpx
from app.config import settings

GATEWAY_URL = "http://localhost:8000"
APP_SECRET = settings.WHATSAPP_APP_SECRET


def create_signed_payload(phone: str, name: str, text: str) -> tuple[bytes, str]:
    wamid = f"wamid.HBgM{uuid.uuid4().hex[:18]}AA=="
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550012345",
                                "phone_number_id": "PHONE_NUMBER_ID"
                            },
                            "contacts": [
                                {
                                    "profile": {"name": name},
                                    "wa_id": phone
                                }
                            ],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": wamid,
                                    "timestamp": str(int(time.time())),
                                    "text": {"body": text},
                                    "type": "text"
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = f"sha256={hmac.new(APP_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()}"
    return body_bytes, sig


async def chat_session(phone: str = "919876543210", name: str = "Aarav Sharma"):
    print("\n" + "="*60)
    print("💬 HeyDoc WhatsApp Interactive Simulator")
    print(f"Patient: {name} ({phone})")
    print("Type your message below (or 'exit' to quit)")
    print("Examples:")
    print("  - 'Check available slots for CBC tomorrow'")
    print("  - 'Book CBC for 10:00 AM'")
    print("  - 'I need to speak to a human doctor'")
    print("="*60 + "\n")

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                user_msg = input("\nYou: ").strip()
            except (KeyboardInterrupt, EOFError):
                break

            if not user_msg or user_msg.lower() in ["exit", "quit", "q"]:
                break

            body, sig = create_signed_payload(phone, name, user_msg)
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig
            }

            t0 = time.perf_counter()
            resp = await client.post(f"{GATEWAY_URL}/webhook", content=body, headers=headers)
            ack_lat = (time.perf_counter() - t0) * 1000.0

            if resp.status_code != 200:
                print(f"[Error: Gateway returned {resp.status_code} - {resp.text}]")
                continue

            print(f"[Gateway: Acknowledged in {ack_lat:.1f}ms]")

            # Poll for worker response
            await asyncio.sleep(1.2)
            try:
                details_resp = await client.get(f"{GATEWAY_URL}/api/conversations/{phone}")
                if details_resp.status_code == 200:
                    data = details_resp.json()
                    conv = data.get("conversation", {})
                    msgs = data.get("recent_messages", [])
                    assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
                    if assistant_msgs:
                        last_reply = assistant_msgs[-1].get("content")
                        print(f"\nHeyDoc Agent: {last_reply}")
                        print(f"💰 [Session Spend: ${float(conv.get('total_cost_usd', 0)):.5f} | ₹{float(conv.get('total_cost_inr', 0)):.3f}]")
            except Exception as e:
                print(f"[Could not retrieve reply: {e}]")


if __name__ == "__main__":
    asyncio.run(chat_session())
