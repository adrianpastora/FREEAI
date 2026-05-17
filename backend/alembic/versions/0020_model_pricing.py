"""Cost tracking — model price table + per-event frozen cost.

Adds three changes:

  • New table ``model_prices`` keyed on (provider_name, model) holding the
    input / output price per *million* tokens in USD. Per-million is the
    industry-standard unit for LLM pricing and avoids ``Numeric`` precision
    games on per-token rounding.

  • New column ``cost_usd`` on ``usage_events`` (nullable float). Frozen at
    write time so future price changes don't rewrite history. ``NULL`` means
    "no price was on file when this call was billed" — distinct from ``0``
    which is "free tier / explicitly zero".

  • New column ``sum_cost_usd`` on ``usage_daily_rollup`` so the long-window
    historical view (≥30 d) carries cost alongside tokens without scanning
    raw events.

Seed data covers the ``KNOWN_MODELS`` list as of 2026-05. Prices are
inserted with ``ON CONFLICT DO NOTHING`` so admin-tuned rows survive a
re-run of the migration. Free-tier and ``:free`` routes are seeded at
``0.0/0.0``.

Revision ID: 0020
Revises: 0019
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels = None
depends_on = None


# (provider_name, model, input_per_million_usd, output_per_million_usd)
# Sourced from the providers' public pricing pages as of 2026-05. Free
# tiers and OpenRouter ``:free`` routes go in as 0/0 — they cost the user
# nothing even if the upstream lists a nominal price for the paid SKU.
_SEED_PRICES: list[tuple[str, str, float, float]] = [
    # Cerebras — gpt-oss-120b is on the perpetual free tier (1M tokens/day).
    ("cerebras", "gpt-oss-120b", 0.0, 0.0),
    # Groq — public price-list as of 2026-05.
    ("groq", "llama-3.3-70b-versatile", 0.59, 0.79),
    ("groq", "llama-3.1-70b-versatile", 0.59, 0.79),
    ("groq", "llama-3.1-8b-instant", 0.05, 0.08),
    ("groq", "mixtral-8x7b-32768", 0.24, 0.24),
    ("groq", "gemma2-9b-it", 0.20, 0.20),
    # Gemini — Google AI Studio pricing for paid tier.
    ("gemini", "gemini-2.5-flash", 0.30, 2.50),
    ("gemini", "gemini-2.5-flash-lite", 0.10, 0.40),
    ("gemini", "gemini-2.5-pro", 1.25, 10.00),
    ("gemini", "gemini-3-flash-preview", 0.30, 2.50),
    # Mistral La Plateforme.
    ("mistral", "mistral-small-latest", 0.20, 0.60),
    ("mistral", "mistral-large-latest", 2.00, 6.00),
    ("mistral", "open-mistral-nemo", 0.15, 0.15),
    ("mistral", "codestral-latest", 0.30, 0.90),
    # OpenRouter — all seeded routes are explicit ``:free`` SKUs.
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free", 0.0, 0.0),
    ("openrouter", "meta-llama/llama-3.2-3b-instruct:free", 0.0, 0.0),
    ("openrouter", "google/gemini-2.0-flash-exp:free", 0.0, 0.0),
    ("openrouter", "mistralai/mistral-small-3.1-24b-instruct:free", 0.0, 0.0),
    ("openrouter", "qwen/qwen-2.5-72b-instruct:free", 0.0, 0.0),
    # Cohere.
    ("cohere", "command-r-08-2024", 0.15, 0.60),
    ("cohere", "command-r-plus-08-2024", 2.50, 10.00),
    ("cohere", "command-r7b-12-2024", 0.0375, 0.15),
    # HuggingFace Inference Router — open-weight models, billed by the host
    # provider rather than HF directly. We seed at 0 and let admins tune
    # per their deployment.
    ("huggingface", "meta-llama/Llama-3.2-3B-Instruct", 0.0, 0.0),
    ("huggingface", "meta-llama/Llama-3.3-70B-Instruct", 0.0, 0.0),
    ("huggingface", "Qwen/Qwen2.5-72B-Instruct", 0.0, 0.0),
    ("huggingface", "mistralai/Mixtral-8x7B-Instruct-v0.1", 0.0, 0.0),
]


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("provider_name", sa.String(64), primary_key=True),
        sa.Column("model", sa.String(256), primary_key=True),
        # Per million tokens so we can store fractional cents (e.g. 0.0375)
        # without resorting to Numeric. Float is fine for accounting at this
        # granularity — the rounding error on a $0.0001 line item is below
        # what either we or the upstream commits to in the first place.
        sa.Column(
            "input_price_per_million_usd", sa.Float,
            nullable=False, server_default="0",
        ),
        sa.Column(
            "output_price_per_million_usd", sa.Float,
            nullable=False, server_default="0",
        ),
        sa.Column(
            "currency", sa.String(8),
            nullable=False, server_default="USD",
        ),
        sa.Column(
            "updated_at", sa.Float,
            nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
    )

    # Nullable because pre-existing rows have no price on file and we want
    # NULL ≠ 0 in the analytics layer (0 means "explicitly free", NULL
    # means "we couldn't price this when it happened").
    op.add_column(
        "usage_events",
        sa.Column("cost_usd", sa.Float, nullable=True),
    )

    op.add_column(
        "usage_daily_rollup",
        sa.Column(
            "sum_cost_usd", sa.Float,
            nullable=False, server_default="0",
        ),
    )

    # Seed prices. ON CONFLICT keeps admin tunings safe across re-runs and
    # across the migration being applied to an already-populated DB.
    bind = op.get_bind()
    for provider, model, input_p, output_p in _SEED_PRICES:
        bind.execute(
            sa.text(
                """
                INSERT INTO model_prices (
                    provider_name, model,
                    input_price_per_million_usd, output_price_per_million_usd
                ) VALUES (
                    :provider, :model, :input_p, :output_p
                )
                ON CONFLICT (provider_name, model) DO NOTHING
                """
            ),
            {"provider": provider, "model": model, "input_p": input_p, "output_p": output_p},
        )


def downgrade() -> None:
    op.drop_column("usage_daily_rollup", "sum_cost_usd")
    op.drop_column("usage_events", "cost_usd")
    op.drop_table("model_prices")
