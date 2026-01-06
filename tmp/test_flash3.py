#!/usr/bin/env python3
import argparse
import os
import time

import litellm


DEFAULT_MODEL = "gemini/gemini-3-flash-preview"


def _safe_repr(value, limit=800):
    text = repr(value)
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def _print_exception(e):
    print("Exception type:", type(e))
    print("Exception str:", str(e))
    attrs = [
        "status_code",
        "message",
        "response",
        "body",
        "headers",
        "llm_output",
        "original_exception",
        "provider",
    ]
    for attr in attrs:
        if hasattr(e, attr):
            print(f"{attr}:", _safe_repr(getattr(e, attr)))
    extra = getattr(e, "__dict__", None)
    if extra:
        print("exception.__dict__:", _safe_repr(extra))


def main():
    parser = argparse.ArgumentParser(
        description="Repro for litellm gemini-3-flash-preview rate limit issues."
    )
    parser.add_argument(
        "--api-key", default=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--n", type=int, default=1, help="number of requests to send")
    parser.add_argument(
        "--sleep", type=float, default=0.0, help="sleep between requests (seconds)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="enable litellm debug logs"
    )
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("Missing API key. Set GEMINI_API_KEY or pass --api-key.")

    if args.debug:
        litellm._turn_on_debug()

    print("litellm version:", getattr(litellm, "__version__", "unknown"))
    print("model:", args.model)

    for i in range(1, args.n + 1):
        print(f"\n--- request {i}/{args.n} ---")
        try:
            response = litellm.completion(
                model=args.model,
                api_key=args.api_key,
                messages=[{"role": "user", "content": "Reply with a single word: ok"}],
                max_tokens=16,
                temperature=0,
            )
            content = response.choices[0].message.get("content")
            print("response:", content)
        except Exception as e:
            _print_exception(e)
        if args.sleep and i < args.n:
            time.sleep(args.sleep)


if __name__ == "__main__":
    main()
