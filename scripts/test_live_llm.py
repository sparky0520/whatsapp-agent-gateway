"""
Single-turn live LLM verification script.
Tests an actual OpenAI-compatible LLM endpoint (DeepSeek, OpenRouter, Groq, Ollama, etc.)
with the HeyDoc WhatsApp system prompt and tool definitions.

Usage:
  python scripts/test_live_llm.py --api-key "sk-..."
  or
  python scripts/test_live_llm.py --api-key "sk-..." --base-url "https://api.deepseek.com" --model "deepseek-chat"
  or (using settings from .env / environment):
  python scripts/test_live_llm.py
"""
import sys
import os
import argparse
import asyncio
import json

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.agent.llm import OpenAICompatibleLLMClient
from app.agent.tools import (
    TOOL_DEFINITIONS,
    execute_check_slots,
    execute_book_test,
    execute_handoff_to_human
)


def mask_key(key: str) -> str:
    if not key:
        return "<none>"
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


async def run_live_llm_test(
    api_key: str,
    base_url: str,
    model: str,
    user_query: str,
    execute_tools: bool = True
):
    print("=" * 65)
    print("🚀 HeyDoc Live LLM Test Runner (OpenAI-Compatible)")
    print("=" * 65)
    print(f"• Base URL    : {base_url}")
    print(f"• Model Name  : {model}")
    print(f"• API Key     : {mask_key(api_key)}")
    print(f"• Patient Msg : \"{user_query}\"")
    print("-" * 65)

    if not api_key and not ("localhost" in base_url or "127.0.0.1" in base_url):
        print("❌ Error: No API key provided! Please specify --api-key or set LLM_API_KEY in your environment or .env file.")
        sys.exit(1)

    client = OpenAICompatibleLLMClient(
        api_key=api_key,
        base_url=base_url,
        model_name=model
    )

    print("⏳ Sending request to model with system prompt & tool schemas...")
    messages = [{"role": "user", "content": user_query}]

    try:
        resp = await client.chat(
            messages=messages,
            patient_name="Aarav Sharma",
            patient_phone="919876543210"
        )
    except Exception as e:
        print(f"\n❌ Error during LLM API call: {e}")
        return False

    print("\n✅ Response Received Successfully!")
    print("-" * 65)
    if resp.content:
        print(f"💬 Assistant Content:\n{resp.content}\n")
    else:
        print("💬 Assistant Content: (No textual message, model emitted direct tool calls)\n")

    if resp.tool_calls:
        print(f"🛠️  Tool Calls ({len(resp.tool_calls)}):")
        for i, tc in enumerate(resp.tool_calls, 1):
            fn = tc.get("function", {})
            name = fn.get("name")
            args_str = fn.get("arguments", "{}")
            print(f"  [{i}] Function: {name}")
            print(f"      Arguments: {args_str}")

            if execute_tools:
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    print(f"      ⏳ Executing {name} against upstream lab...")
                    if name == "check_slots":
                        tool_res = await execute_check_slots(
                            test_code=args.get("test_code", "CBC"),
                            date=args.get("date", "2026-11-21")
                        )
                        print(f"      ✅ Result: {tool_res.get('status')} | {len(tool_res.get('available_slots', []))} slots available")
                    elif name == "book_test":
                        tool_res = await execute_book_test(
                            patient_name=args.get("patient_name", "Aarav Sharma"),
                            patient_phone=args.get("patient_phone", "919876543210"),
                            test_code=args.get("test_code", "CBC"),
                            slot_time=args.get("slot_time", "2026-11-21T10:00:00Z")
                        )
                        print(f"      ✅ Result: {tool_res.get('status')} | Booking ID: {tool_res.get('booking_id')}")
                    elif name == "handoff_to_human":
                        tool_res = await execute_handoff_to_human(
                            conversation_id="live-test-conv",
                            wa_id="919876543210",
                            reason=args.get("reason", "Patient escalated query"),
                            priority=args.get("priority", "HIGH")
                        )
                        print(f"      ✅ Result: {tool_res.get('status')} | {tool_res.get('message')}")
                except Exception as ex:
                    print(f"      ⚠️  Tool execution note: {ex}")
    else:
        print("ℹ️  No tool calls emitted for this message.")

    print("\n📊 Usage & Cost Metrics:")
    print(f"  • Prompt Tokens     : {resp.prompt_tokens}")
    print(f"  • Completion Tokens : {resp.completion_tokens}")
    print(f"  • Total Tokens      : {resp.prompt_tokens + resp.completion_tokens}")
    print(f"  • Cost (USD)        : ${resp.cost_usd:.6f}")
    print(f"  • Cost (INR)        : ₹{resp.cost_inr:.4f}")
    print("=" * 65)
    return True


def main():
    parser = argparse.ArgumentParser(description="Test HeyDoc Agent with an actual LLM.")
    parser.add_argument("--api-key", default=settings.LLM_API_KEY, help="LLM API key")
    parser.add_argument("--base-url", default=settings.LLM_BASE_URL, help="OpenAI-compatible base URL")
    parser.add_argument("--model", default=settings.LLM_MODEL_NAME, help="Model identifier")
    parser.add_argument(
        "--message",
        default="Hi, can you check what slots are available for a CBC test tomorrow?",
        help="Test message to send"
    )
    parser.add_argument("--no-tools", action="store_true", help="Do not execute returned tool calls")

    args = parser.parse_args()

    asyncio.run(
        run_live_llm_test(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            user_query=args.message,
            execute_tools=not args.no_tools
        )
    )


if __name__ == "__main__":
    main()
