"""
Configuration module for WhatsApp Agent Gateway.
Loads settings from environment variables or .env file using Pydantic Settings.
"""
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Environment
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"

    # Gateway / Server
    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 8000
    MOCK_LAB_HOST: str = "0.0.0.0"
    MOCK_LAB_PORT: int = 8001

    # WhatsApp Cloud API Credentials
    WHATSAPP_APP_SECRET: str = "mock_secret_for_tests_only_3b9f"
    WHATSAPP_VERIFY_TOKEN: str = "heydoc_verify_token_2026"
    WHATSAPP_PHONE_NUMBER_ID: str = "109876543210123"

    # PostgreSQL Database
    DATABASE_URL: str = "postgresql://postgres:postgres@postgres:5432/whatsapp_gateway"
    DB_POOL_MIN_SIZE: int = 2
    DB_POOL_MAX_SIZE: int = 20

    # Redis Cache, Deduplication & Queue
    REDIS_URL: str = "redis://redis:6379/0"
    DEDUP_TTL_SECONDS: int = 86400  # 24 hours deduplication window
    MESSAGE_QUEUE_NAME: str = "queue:whatsapp"

    # Rate Limiting (per phone number)
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_MAX_REQUESTS: int = 15  # requests per window
    RATE_LIMIT_WINDOW_SECONDS: int = 60  # 1 minute window

    # Upstream Mock Lab API
    LAB_API_BASE_URL: str = "http://mock-lab:8001"
    LAB_API_TIMEOUT_SECONDS: float = 5.0
    LAB_FAILURE_RATE: float = 0.20  # 20% simulated failure rate
    LAB_MIN_LATENCY_MS: int = 500   # 500ms min latency
    LAB_MAX_LATENCY_MS: int = 1500  # 1500ms max latency

    # Retry with Exponential Backoff + Full Jitter
    RETRY_MAX_ATTEMPTS: int = 3
    RETRY_BASE_BACKOFF: float = 0.3  # seconds
    RETRY_MAX_BACKOFF: float = 2.0   # seconds

    # LLM Settings (Generic OpenAI-Compatible: DeepSeek, OpenRouter, Groq, Ollama, etc.)
    LLM_PROVIDER: str = "mock"  # "mock", "live", "deepseek", etc.
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_MODEL_NAME: str = "deepseek-chat"
    
    # Token Pricing (DeepSeek baseline: $0.140 / 1M in, $0.280 / 1M out)
    COST_PER_1M_PROMPT_TOKENS_USD: float = 0.140
    COST_PER_1M_COMPLETION_TOKENS_USD: float = 0.280
    USD_TO_INR_RATE: float = 85.0


settings = Settings()
