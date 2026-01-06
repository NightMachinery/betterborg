#!/usr/bin/env python3
import argparse
import asyncio
import faulthandler
import importlib.util
import os
import signal
import sys
from pathlib import Path

import litellm


DEFAULT_MODEL = "gemini/gemini-3-flash-preview"


class _ImportTimeout(Exception):
    pass


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


class _DummyLoop:
    def create_task(self, coro):
        # Prevent "coroutine was never awaited" warnings on import.
        try:
            coro.close()
        except Exception:
            pass
        return None


class _DummyBorg:
    loop = _DummyLoop()

    def on(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator


def _load_llm_chat_module(repo_root: Path):
    llm_chat_path = repo_root / "llm_chat_plugins" / "llm_chat.py"
    if not llm_chat_path.exists():
        raise FileNotFoundError(f"Missing: {llm_chat_path}")

    os.environ.setdefault("borg_brish_count", "1")

    spec = importlib.util.spec_from_file_location("llm_chat_standalone", llm_chat_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to create module spec for llm_chat.py")

    module = importlib.util.module_from_spec(spec)
    module.borg = _DummyBorg()
    sys.path.insert(0, str(repo_root))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_llm_chat_module_with_timeout(repo_root: Path, timeout_s: int | None):
    if not timeout_s or timeout_s <= 0:
        return _load_llm_chat_module(repo_root)

    def _alarm_handler(_signum, _frame):
        raise _ImportTimeout(f"Import timed out after {timeout_s}s")

    prev_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout_s)
    try:
        return _load_llm_chat_module(repo_root)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_handler)


async def _run_with_llm_chat(
    module,
    api_key,
    model,
    stream,
    *,
    include_system_prompt,
    system_prompt,
    user_text,
    tools,
    json_mode,
    thinking,
    cache_control,
    no_response_retries_max,
    retry_sleep,
):
    from uniborg import util

    async def _noop_edit_message(*args, **kwargs):
        return None

    util.edit_message = _noop_edit_message

    messages = []
    if include_system_prompt:
        messages.append(
            {
                "role": "system",
                "content": system_prompt or module.DEFAULT_SYSTEM_PROMPT,
            }
        )
    messages.append({"role": "user", "content": user_text})
    if cache_control and model.startswith("gemini/"):
        for message in messages:
            message["cache_control"] = {"type": "ephemeral"}

    model_capabilities = module.get_model_capabilities(model)
    use_streaming = stream and not model_capabilities.get("image_generation", False)

    api_kwargs = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "stream": use_streaming,
    }
    if json_mode:
        api_kwargs["response_format"] = {"type": "json_object"}

    if getattr(module, "is_gemini_model", lambda *_: False)(model):
        safety_settings = getattr(module, "SAFETY_SETTINGS", None)
        if safety_settings:
            api_kwargs["safety_settings"] = safety_settings

        if tools and not json_mode:
            tools_to_use = list(tools)
            if (
                "gemini-2.0-flash" in model
                and "googleSearch" in tools_to_use
                and "urlContext" in tools_to_use
            ):
                tools_to_use.remove("urlContext")
            api_kwargs["tools"] = [{t: {}} for t in tools_to_use]

        if thinking and "2.5-pro" not in model:
            api_kwargs["reasoning_effort"] = thinking

        if model_capabilities.get("image_generation", False):
            api_kwargs["modalities"] = ["image", "text"]

    edit_interval = module.get_streaming_delay(model) if use_streaming else None
    response = await module._retry_on_no_response_with_reasons(
        user_id=0,
        event=None,
        response_message=None,
        api_kwargs=api_kwargs,
        edit_interval=edit_interval,
        model_capabilities=model_capabilities,
        streaming_p=use_streaming,
        no_response_retries_max=no_response_retries_max,
        sleep=retry_sleep,
    )
    return response


async def _run_import_and_call(
    repo_root,
    api_key,
    model,
    stream,
    import_timeout,
    *,
    include_system_prompt,
    system_prompt,
    user_text,
    tools,
    json_mode,
    thinking,
    cache_control,
    no_response_retries_max,
    retry_sleep,
):
    module = _load_llm_chat_module_with_timeout(repo_root, import_timeout)
    print("llm_chat import: ok")
    response = await _run_with_llm_chat(
        module,
        api_key,
        model,
        stream,
        include_system_prompt=include_system_prompt,
        system_prompt=system_prompt,
        user_text=user_text,
        tools=tools,
        json_mode=json_mode,
        thinking=thinking,
        cache_control=cache_control,
        no_response_retries_max=no_response_retries_max,
        retry_sleep=retry_sleep,
    )
    return response


def main():
    parser = argparse.ArgumentParser(
        description="Repro for llm_chat plugin behavior with gemini-3-flash-preview."
    )
    parser.add_argument(
        "--api-key", default=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="disable streaming (llm_chat uses streaming by default)",
    )
    parser.set_defaults(stream=True)
    parser.add_argument(
        "--import-timeout",
        type=int,
        default=10,
        help="seconds to wait for llm_chat import (0 = no timeout)",
    )
    parser.add_argument(
        "--no-system",
        dest="include_system_prompt",
        action="store_false",
        help="omit the system prompt in the message list",
    )
    parser.set_defaults(include_system_prompt=True)
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="override system prompt text (default: llm_chat DEFAULT_SYSTEM_PROMPT)",
    )
    parser.add_argument(
        "--user-text",
        default="Reply with a single word: ok",
        help="user message content",
    )
    parser.add_argument(
        "--tools",
        default="",
        help="comma-separated tool list (e.g., googleSearch,urlContext)",
    )
    parser.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="enable JSON mode (disables tools)",
    )
    parser.add_argument(
        "--thinking",
        default=None,
        help="set reasoning_effort for Gemini models",
    )
    parser.add_argument(
        "--no-cache-control",
        dest="cache_control",
        action="store_false",
        help="disable cache_control=ephemeral on Gemini messages",
    )
    parser.set_defaults(cache_control=True)
    parser.add_argument(
        "--no-response-retries",
        type=int,
        default=1,
        help="max retries for no-response cases (default: 1)",
    )
    parser.add_argument(
        "--retry-sleep",
        type=float,
        default=0.0,
        help="sleep between no-response retries (seconds)",
    )
    parser.add_argument(
        "--dump-stack-after",
        type=float,
        default=0.0,
        help="dump Python stack after N seconds (0 disables)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="enable litellm debug logs"
    )
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("Missing API key. Set GEMINI_API_KEY or pass --api-key.")

    if args.debug:
        litellm._turn_on_debug()

    faulthandler.enable()
    if args.dump_stack_after and args.dump_stack_after > 0:
        faulthandler.dump_traceback_later(args.dump_stack_after, repeat=True)

    print("litellm version:", getattr(litellm, "__version__", "unknown"))
    print("model:", args.model)
    print("stream:", args.stream)

    repo_root = Path(__file__).resolve().parents[1]
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    json_mode = args.json_mode
    if json_mode and tools:
        print("Note: tools ignored because JSON mode is enabled.")
        tools = []
    include_system_prompt = args.include_system_prompt
    system_prompt = args.system_prompt
    user_text = args.user_text
    thinking = args.thinking
    cache_control = args.cache_control
    no_response_retries_max = max(1, int(args.no_response_retries))
    retry_sleep = max(0.0, float(args.retry_sleep))

    try:
        response = asyncio.run(
            _run_import_and_call(
                repo_root,
                args.api_key,
                args.model,
                args.stream,
                args.import_timeout,
                include_system_prompt=include_system_prompt,
                system_prompt=system_prompt,
                user_text=user_text,
                tools=tools,
                json_mode=json_mode,
                thinking=thinking,
                cache_control=cache_control,
                no_response_retries_max=no_response_retries_max,
                retry_sleep=retry_sleep,
            )
        )
        print("response:", getattr(response, "text", response))
    except Exception as e:
        _print_exception(e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
