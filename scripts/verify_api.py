"""Quick health check for your OpenRouter key + model slugs.

Run this once after creating your .env to confirm BOTH the chat model
(DeepSeek V4 Flash) and the embedding model (Gemini Embedding 2) work end to
end through your single OpenRouter key, before launching the app.

    python scripts/verify_api.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from app.config import settings  # noqa: E402


def main() -> int:
    print("University of Jordan RAG — API verification\n" + "-" * 44)
    if not settings.is_configured():
        print("✗ OPENROUTER_API_KEY is missing. Copy .env.example to .env and add it.")
        return 2
    print(f"Base URL : {settings.OPENROUTER_BASE_URL}")
    print(f"Chat     : {settings.CHAT_MODEL}")
    print(f"Embedding: {settings.EMBED_MODEL}\n")

    from app.core.llm import get_client
    from app.pipeline.embed import embed_query

    # 1) Embeddings
    try:
        vec = embed_query("University of Jordan admissions")
        print(f"✓ Embeddings OK — vector dimension = {len(vec)}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Embeddings FAILED: {exc}")
        print("  Check EMBED_MODEL slug and that your key has credit.")
        return 1

    # 2) Chat
    try:
        resp = get_client().chat.completions.create(
            model=settings.CHAT_MODEL,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=10, temperature=0,
        )
        print(f"✓ Chat OK — model replied: {resp.choices[0].message.content!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Chat FAILED: {exc}")
        print("  Check CHAT_MODEL slug and that your key has credit.")
        return 1

    print("\nAll good — you're ready to run:  python run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
