"""
LLM abstraction layer supporting:
1. Real OpenAI Tool Calling (e.g. gpt-4o-mini)
2. Deterministic Mock LLM for high-throughput zero-cost testing and CI
Tracks token counts, USD cost, and INR cost per call, updating Prometheus metrics.
"""
import re
import json
import uuid
import logging
from typing import List, Dict, Any, Optional, Tuple
from app.config import settings
from app.agent.tools import TOOL_DEFINITIONS
from app.agent.prompt import get_system_prompt
from app.metrics import LLM_TOKENS_TOTAL, LLM_COST_TOTAL

logger = logging.getLogger("agent_llm")


class LLMResponse:
    def __init__(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        cost_inr: float = 0.0,
        model_name: str = "mock-agent"
    ):
        self.content = content
        self.tool_calls = tool_calls or []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cost_usd = cost_usd
        self.cost_inr = cost_inr
        self.model_name = model_name


def calculate_cost(prompt_tokens: int, completion_tokens: int) -> Tuple[float, float]:
    """Calculate cost in USD and INR based on configured pricing tiers."""
    cost_usd = (
        (prompt_tokens / 1_000_000.0) * settings.COST_PER_1M_PROMPT_TOKENS_USD
        + (completion_tokens / 1_000_000.0) * settings.COST_PER_1M_COMPLETION_TOKENS_USD
    )
    cost_inr = cost_usd * settings.USD_TO_INR_RATE
    return round(cost_usd, 6), round(cost_inr, 4)


class MockLLMClient:
    """
    Deterministic rule-based agent simulator with tool-calling capabilities.
    Mimics GPT-4o-mini tool calling decisions based on natural language intent:
    - User asks for human/help -> calls handoff_to_human
    - User asks for slots/availability -> calls check_slots
    - User provides booking details -> calls book_test
    - Other conversation -> returns friendly contextual message
    """
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        patient_name: str = "Patient",
        patient_phone: str = ""
    ) -> LLMResponse:
        # Get latest user message
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = (m.get("content") or "").strip().lower()
                break

        # Simulate realistic token usage
        prompt_text = "".join([str(m.get("content", "")) for m in messages]) + get_system_prompt()
        prompt_tokens = max(120, len(prompt_text.split()) + 80)
        completion_tokens = 45

        tool_calls = []
        content = None

        # Pattern 1: Escalation to human coordinator
        if any(w in last_user_msg for w in ["human", "agent", "doctor", "operator", "person", "emergency", "help me"]):
            priority = "HIGH" if "emergency" in last_user_msg else "MEDIUM"
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "handoff_to_human",
                    "arguments": json.dumps({
                        "reason": f"Patient requested human coordinator: '{last_user_msg}'",
                        "priority": priority
                    })
                }
            })
            content = "I am connecting you with a human healthcare coordinator right now."

        # Pattern 2: Explicit booking request
        elif any(w in last_user_msg for w in ["book", "reserve", "confirm"]):
            # Detect test code
            test_code = "CBC"
            for code in ["LIPID", "FBS", "THYROID", "COVID", "CBC"]:
                if code.lower() in last_user_msg:
                    test_code = code
                    break

            # Date & Slot detection
            date = "2026-11-21"
            slot_time = f"{date}T10:00:00Z"
            for hour in ["08", "09", "10", "11", "14", "16"]:
                if f"{hour}:00" in last_user_msg or f"{int(hour)} am" in last_user_msg or f"{int(hour)} pm" in last_user_msg:
                    slot_time = f"{date}T{hour}:00:00Z"
                    break

            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "book_test",
                    "arguments": json.dumps({
                        "patient_name": patient_name or "Aarav Sharma",
                        "patient_phone": patient_phone or "919876543210",
                        "test_code": test_code,
                        "slot_time": slot_time
                    })
                }
            })
            content = f"Booking your {test_code} appointment for {slot_time}..."

        # Pattern 3: Slot / availability inquiry
        elif any(w in last_user_msg for w in ["slot", "available", "timing", "when", "date", "schedule", "test"]):
            test_code = "CBC"
            for code in ["LIPID", "FBS", "THYROID", "COVID", "CBC"]:
                if code.lower() in last_user_msg:
                    test_code = code
                    break

            date = "2026-11-21"
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "check_slots",
                    "arguments": json.dumps({
                        "test_code": test_code,
                        "date": date
                    })
                }
            })
            content = f"Checking available slots for {test_code} on {date}..."

        # Default conversational reply
        else:
            content = (
                f"Hello {patient_name}! Welcome to HeyDoc Diagnostics. "
                "I can help you check slots and book CBC, Lipid Profile, FBS, Thyroid, or Covid tests. "
                "Which test would you like to schedule?"
            )
            completion_tokens = 35

        cost_usd, cost_inr = calculate_cost(prompt_tokens, completion_tokens)

        # Update metrics
        LLM_TOKENS_TOTAL.labels(type="prompt", model="mock-agent").inc(prompt_tokens)
        LLM_TOKENS_TOTAL.labels(type="completion", model="mock-agent").inc(completion_tokens)
        LLM_COST_TOTAL.labels(currency="USD").inc(cost_usd)
        LLM_COST_TOTAL.labels(currency="INR").inc(cost_inr)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            cost_inr=cost_inr,
            model_name="mock-agent"
        )


class OpenAILLMClient:
    """OpenAI-compatible client supporting DeepSeek and OpenAI."""
    def __init__(self, provider: str = "deepseek"):
        from openai import AsyncOpenAI
        if provider == "deepseek" or (settings.LLM_PROVIDER == "deepseek" or settings.DEEPSEEK_API_KEY):
            self.client = AsyncOpenAI(
                api_key=settings.DEEPSEEK_API_KEY or settings.OPENAI_API_KEY,
                base_url=settings.DEEPSEEK_BASE_URL
            )
            self.model_name = settings.DEEPSEEK_MODEL_NAME
        else:
            self.client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL
            )
            self.model_name = settings.OPENAI_MODEL_NAME

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        patient_name: str = "Patient",
        patient_phone: str = ""
    ) -> LLMResponse:
        system_msg = {"role": "system", "content": get_system_prompt()}
        full_messages = [system_msg] + messages

        response = await self.client.chat.completions.create(
            model=self.model_name,
            messages=full_messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            temperature=0.2
        )

        choice = response.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 150
        completion_tokens = usage.completion_tokens if usage else 40
        cost_usd, cost_inr = calculate_cost(prompt_tokens, completion_tokens)

        LLM_TOKENS_TOTAL.labels(type="prompt", model=self.model_name).inc(prompt_tokens)
        LLM_TOKENS_TOTAL.labels(type="completion", model=self.model_name).inc(completion_tokens)
        LLM_COST_TOTAL.labels(currency="USD").inc(cost_usd)
        LLM_COST_TOTAL.labels(currency="INR").inc(cost_inr)

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            cost_inr=cost_inr,
            model_name=self.model_name
        )


def get_llm_client():
    """Return configured LLM provider (DeepSeek / OpenAI) or fallback to mock."""
    if (settings.LLM_PROVIDER == "deepseek" and (settings.DEEPSEEK_API_KEY or settings.OPENAI_API_KEY)) or \
       (settings.LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY):
        try:
            return OpenAILLMClient(provider=settings.LLM_PROVIDER)
        except Exception as e:
            logger.warning(f"Failed to initialize {settings.LLM_PROVIDER} client ({e}). Falling back to MockLLMClient.")
            return MockLLMClient()
    return MockLLMClient()


llm_client = get_llm_client()
