"""List or latency-test free Mesh text models without exposing credentials."""

import argparse
import json
import time

import httpx
from openai import OpenAI

from app.config import get_settings


def free_text_models() -> list[dict]:
    settings = get_settings()
    if not settings.mesh_api_key:
        raise SystemExit("MESH_API_KEY is not configured")
    response = httpx.get(
        f"{settings.mesh_base_url.rstrip('/')}/models/free",
        headers={"Authorization": f"Bearer {settings.mesh_api_key}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    models = payload if isinstance(payload, list) else payload.get("data", [])
    return [
        model for model in models
        if model.get("model_type") == "text"
        and model.get("supports_completions_api", True)
        and model.get("supports_system_prompt", True)
    ]


def benchmark(model_ids: list[str]) -> None:
    settings = get_settings()
    client = OpenAI(
        api_key=settings.mesh_api_key,
        base_url=settings.mesh_base_url,
        timeout=30,
        max_retries=0,
    )
    prompt = (
        "Return JSON only with keys headline and reason. Connect a streaming data "
        "course to a feature-store course in one concise, personalized sentence."
    )
    for model_id in model_ids:
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=model_id,
                temperature=0,
                max_tokens=180,
                messages=[
                    {"role": "system", "content": "Write concise grounded course recommendation copy as JSON."},
                    {"role": "user", "content": prompt},
                ],
            )
            latency = time.perf_counter() - started
            content = response.choices[0].message.content or ""
            try:
                json.loads(content.strip().removeprefix("```json").removesuffix("```").strip())
                valid_json = True
            except json.JSONDecodeError:
                valid_json = False
            usage = response.usage
            print(
                f"{model_id}\t{latency:.2f}s\tjson={valid_json}\t"
                f"tokens={(usage.prompt_tokens + usage.completion_tokens) if usage else 0}"
            )
        except Exception as exc:
            latency = time.perf_counter() - started
            print(
                f"{model_id}\t{latency:.2f}s\tfailed={type(exc).__name__}"
                f"\tstatus={getattr(exc, 'status_code', 'n/a')}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="*", help="Model IDs to latency-test")
    parser.add_argument("--allow-paid", action="store_true", help="Benchmark explicitly supplied paid model IDs")
    args = parser.parse_args()
    available = free_text_models()
    available_ids = [model["id"] for model in available]
    if args.models:
        unknown = [] if args.allow_paid else sorted(set(args.models) - set(available_ids))
        if unknown:
            print(f"Not currently free/compatible: {', '.join(unknown)}")
        benchmark(args.models if args.allow_paid else [model for model in args.models if model in available_ids])
        return
    for model in available:
        print(f"{model['id']}\t{model.get('name', '')}")


if __name__ == "__main__":
    main()
