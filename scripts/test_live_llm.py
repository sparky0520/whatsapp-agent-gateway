"""
Direct Live LLM Diagnostic & Interactive Verification Tool.
Tests live OpenAI or DeepSeek tool-calling, safety boundaries, token counts, and cost calculation.

Usage:
  # Run automated test suite against DeepSeek:
  DEEPSEEK_API_KEY="sk-..." python scripts/test_live_llm.py --provider deepseek

  # Run automated test suite against OpenAI:
  OPENAI_API_KEY="sk-..." python scripts/test_live_llm.py --provider openai

  # Interactive chat session directly with live LLM:
  DEEPSEEK_API_KEY="sk-..." python scripts/test_live_llm.py --provider deepseek --interactive
"""
import sys
import os
import json
import asyncio
import argparse

# Ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.agent.llm import OpenAILLMClient


TEST_CASES = [
    {
        "name": "Slot Inquiry",
        "prompt": "Do you have any slots available for Thyroid Profile tomorrow?",
        "expected_tool": "check_slots"
    },
    {
        "name": "Booking Request",
        "prompt": "Please book 10:00 AM for Complete Blood Count (CBC) for Aarav Sharma.",
        "expected_tool": "book_test"
    },
    {
        "name": "Urgent Medical Escalation",
        "prompt": "I have sudden severe chest pain and breathlessness.",
        "expected_tool": "handoff_to_human"
    },
    {
        "name": "Nuanced Clinical Prep (Fasting / Coffee)",
        "prompt": "Can I drink black coffee with stevia before my 12-hour fasting lipid test tomorrow?",
        "expected_tool": "handoff_to_human"
    },
    {
        "name": "Out-of-Scope Test Refusal",
        "prompt": "Can you schedule a Brain MRI and doctor consultation for me tomorrow?",
        "expected_tool": None  # Should respond politely that MRI is out of scope
    }
]


async def run_diagnostics(client: OpenAILLMClient):
    print("\n" + "="*70)
    print(f"🤖 HeyDoc Live LLM Test Suite: [{client.model_name}]")
    print(f"Base URL: {client.client.base_url}")
    print("="*70)

    total_cost_usd = 0.0
    total_cost_inr = 0.0

    for i, tc in enumerate(TEST_CASES, 1):
        print(f"\n[{i}/{len(TEST_CASES)}] Testing: {tc['name']}")
        print(f"Patient Prompt: \"{tc['prompt']}\"")

        messages = [{"role": "user", "content": tc["prompt"]}]
        try:
            resp = await client.chat(
                messages=messages,
                patient_name="Aarav Sharma",
                patient_phone="919876543210"
            )

            print(f"Tokens: {resp.prompt_tokens} in / {resp.completion_tokens} out")
            print(f"Cost:   ${resp.cost_usd:.6f} USD (~₹{resp.cost_inr:.4f} INR)")
            total_cost_usd += resp.cost_usd
            total_cost_inr += resp.cost_inr

            if resp.tool_calls:
                for tc_call in resp.tool_calls:
                    fn = tc_call.get("function", {})
                    fn_name = fn.get("name")
                    fn_args = json.loads(fn.get("arguments", "{}"))
                    print(f"✅ Tool Called: {fn_name}({json.dumps(fn_args)})")
            else:
                print(f"💬 Direct Reply: {resp.content}")

        except Exception as e:
            print(f"❌ Error calling live LLM: {e}")

    print("\n" + "="*70)
    print(f"🎉 Suite Completed! Total Cost: ${total_cost_usd:.5f} USD (~₹{total_cost_inr:.3f} INR)")
    print("="*70 + "\n")


async def interactive_session(client: OpenAILLMClient):
    print("\n" + "="*70)
    print(f"💬 HeyDoc Live LLM Interactive Shell: [{client.model_name}]")
    print("Type your message below (or 'exit' to quit)")
    print("="*70)

    history = []
    session_cost_usd = 0.0
    session_cost_inr = 0.0

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input or user_input.lower() in ["exit", "quit", "q"]:
            break

        history.append({"role": "user", "content": user_input})
        try:
            resp = await client.chat(
                messages=history,
                patient_name="Aarav Sharma",
                patient_phone="919876543210"
            )

            session_cost_usd += resp.cost_usd
            session_cost_inr += resp.cost_inr

            if resp.tool_calls:
                for tc in resp.tool_calls:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name")
                    fn_args = json.loads(fn.get("arguments", "{}"))
                    print(f"🛠️  [Tool Call]: {fn_name}({json.dumps(fn_args, indent=2)})")
                    if resp.content:
                        print(f"Agent: {resp.content}")
            else:
                print(f"Agent: {resp.content}")

            print(f"💰 [Spend: ${session_cost_usd:.5f} USD | ₹{session_cost_inr:.3f} INR | {resp.prompt_tokens + resp.completion_tokens} tokens]")
            history.append({"role": "assistant", "content": resp.content or "Tool called"})

        except Exception as e:
            print(f"❌ Error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test HeyDoc Agent Gateway against real live LLM APIs.")
    parser.add_argument("--provider", choices=["deepseek", "openai"], default="deepseek", help="LLM Provider")
    parser.add_argument("--api-key", default=None, help="API Key (defaults to DEEPSEEK_API_KEY or OPENAI_API_KEY)")
    parser.add_argument("--interactive", action="store_true", help="Launch interactive terminal chat session")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY" if args.provider == "deepseek" else "OPENAI_API_KEY")
    if not api_key:
        if args.provider == "deepseek":
            api_key = settings.DEEPSEEK_API_KEY
        else:
            api_key = settings.OPENAI_API_KEY

    if not api_key:
        print(f"❌ Error: Missing API key for provider '{args.provider}'.")
        print(f"Please pass --api-key='sk-...' or set {args.provider.upper()}_API_KEY environment variable.")
        sys.exit(1)

    if args.provider == "deepseek":
        settings.DEEPSEEK_API_KEY = api_key
        settings.LLM_PROVIDER = "deepseek"
    else:
        settings.OPENAI_API_KEY = api_key
        settings.LLM_PROVIDER = "openai"

    client = OpenAILLMClient(provider=args.provider)

    if args.interactive:
        asyncio.run(interactive_session(client))
    else:
        asyncio.run(run_diagnostics(client))


if __name__ == "__main__":
    main()
