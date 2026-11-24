"""Brand presets for the relay worker form: pick a company, get its base URL, a few well-known
models and reference prices filled in, instead of typing an OpenAI-compatible address from memory.

Static data only — nothing here calls the network, and the prices are a convenience for the form's
defaults, never a billing source (the gateway prices a call from the worker's own config). A price
of 0 means "not filled in": the note then says to check the vendor's pricing page. The catalog is
dated; model line-ups move fast, so treat every entry as a starting point the owner can edit.
"""

from __future__ import annotations

CATALOG_DATE = "2026-11-25"
_REFERENCE = "Giá tham chiếu, cập nhật 25/11/2026 — kiểm tra lại trên trang hãng trước khi dùng."

PROVIDER_PRESETS: list[dict] = [
    {
        "brand": "openai",
        "label": "OpenAI",
        "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"id": "gpt-5", "price_in": 1.25, "price_out": 10.0, "context": 400000},
            {"id": "gpt-4o", "price_in": 2.5, "price_out": 10.0, "context": 128000},
            {"id": "gpt-4o-mini", "price_in": 0.15, "price_out": 0.6, "context": 128000},
        ],
        "note": f"API gốc của chuẩn OpenAI. {_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "deepseek",
        "label": "DeepSeek",
        "type": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"id": "deepseek-chat", "price_in": 0.0, "price_out": 0.0, "context": 128000},
            {"id": "deepseek-reasoner", "price_in": 0.0, "price_out": 0.0, "context": 128000},
        ],
        "note": "Rẻ, mạnh về code; giá thay đổi theo đợt khuyến mãi và giờ cao điểm nên không điền sẵn. "
                f"{_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "xai",
        "label": "xAI (Grok)",
        "type": "openai",
        "base_url": "https://api.x.ai/v1",
        "models": [
            {"id": "grok-4", "price_in": 3.0, "price_out": 15.0, "context": 256000},
            {"id": "grok-code-fast-1", "price_in": 0.2, "price_out": 1.5, "context": 256000},
        ],
        "note": f"Grok của xAI qua API tương thích OpenAI. {_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "groq",
        "label": "Groq",
        "type": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "models": [
            {"id": "llama-3.3-70b-versatile", "price_in": 0.59, "price_out": 0.79, "context": 128000},
            {"id": "openai/gpt-oss-120b", "price_in": 0.15, "price_out": 0.75, "context": 128000},
        ],
        "note": f"Chạy model mở trên chip LPU, rất nhanh; đừng nhầm với Grok của xAI. {_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "openrouter",
        "label": "OpenRouter",
        "type": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            {"id": "deepseek/deepseek-chat", "price_in": 0.0, "price_out": 0.0, "context": 128000},
            {"id": "openai/gpt-4o", "price_in": 0.0, "price_out": 0.0, "context": 128000},
            {"id": "anthropic/claude-sonnet-4.5", "price_in": 0.0, "price_out": 0.0, "context": 200000},
            {"id": "google/gemini-2.5-flash", "price_in": 0.0, "price_out": 0.0, "context": 1000000},
        ],
        "note": "Một key gọi được model của nhiều hãng; giá tùy model và nhà chạy phía sau nên không điền sẵn. "
                f"{_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "together",
        "label": "Together AI",
        "type": "openai",
        "base_url": "https://api.together.xyz/v1",
        "models": [
            {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "price_in": 0.88, "price_out": 0.88,
             "context": 128000},
            {"id": "Qwen/Qwen2.5-Coder-32B-Instruct", "price_in": 0.0, "price_out": 0.0, "context": 128000},
        ],
        "note": f"Chuyên chạy model mở. {_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "mistral",
        "label": "Mistral AI",
        "type": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "models": [
            {"id": "mistral-large-latest", "price_in": 2.0, "price_out": 6.0, "context": 128000},
            {"id": "codestral-latest", "price_in": 0.3, "price_out": 0.9, "context": 256000},
            {"id": "mistral-small-latest", "price_in": 0.1, "price_out": 0.3, "context": 128000},
        ],
        "note": f"Model của Mistral (Pháp); tên '-latest' tự trỏ bản mới nhất. {_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "gemini",
        "label": "Google Gemini",
        "type": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": [
            {"id": "gemini-2.5-pro", "price_in": 1.25, "price_out": 10.0, "context": 1000000},
            {"id": "gemini-2.5-flash", "price_in": 0.3, "price_out": 2.5, "context": 1000000},
        ],
        "note": f"Gemini qua cổng tương thích OpenAI của Google; giá bản Pro tăng khi prompt rất dài. {_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
    {
        "brand": "anthropic",
        "label": "Anthropic (Claude)",
        "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": [
            {"id": "claude-sonnet-4-5", "price_in": 3.0, "price_out": 15.0, "context": 200000},
            {"id": "claude-opus-4-5", "price_in": 5.0, "price_out": 25.0, "context": 200000},
            {"id": "claude-haiku-4-5", "price_in": 1.0, "price_out": 5.0, "context": 200000},
        ],
        "note": f"API riêng của Anthropic (không phải chuẩn OpenAI). {_REFERENCE}",
        "catalog_date": CATALOG_DATE,
    },
]
