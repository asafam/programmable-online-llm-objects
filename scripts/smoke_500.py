"""
Smoke test: are Azure 500 InternalServerErrors implementation-related or server-wide?

Runs the same prompt N times under several call shapes and counts OK / 500 / other,
so we can see whether 500s correlate with HOW we call (structured output schema,
large prompts, client config) or hit even the official minimal call.

Usage:
    ./.venv/bin/python scripts/smoke_500.py [--n 20] [--model gpt-5.4-mini]
                       [--endpoint-env PROJECT]   # use AZURE_OPENAI_*_PROJECT pair

Reads AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT from .env (or the *_<SUFFIX>
pair when --endpoint-env is given). No secrets are printed.
"""
from __future__ import annotations
import argparse, os, time, json, collections
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"))


def _client(suffix: str | None):
    import httpx
    from openai import AzureOpenAI
    key = os.environ[f"AZURE_OPENAI_API_KEY_{suffix}"] if suffix else os.environ["AZURE_OPENAI_API_KEY"]
    ep = (os.environ[f"AZURE_OPENAI_ENDPOINT_{suffix}"] if suffix else os.environ["AZURE_OPENAI_ENDPOINT"]).strip().strip('"')
    ver = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    # No keepalive reuse + explicit timeout — matches the runtime client.
    return AzureOpenAI(api_key=key, azure_endpoint=ep, api_version=ver, timeout=60.0, max_retries=0,
                       http_client=httpx.Client(timeout=60.0, limits=httpx.Limits(max_keepalive_connections=0))), ep


# A representative structured-output schema (mirrors the runtime's react shape:
# a small object the model must fill, forcing constrained decoding).
_STRUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ["finish", "tool_call"]},
        "reply": {"type": "string"},
    },
    "required": ["thought", "action", "reply"],
    "additionalProperties": False,
}

# A deliberately large system prompt (~3k tokens) to mimic our object prompts.
_BIG_SYSTEM = ("You are an LLM-object in a message-passing workflow. " + (
    "Maintain your canonical state, follow your behavior contract, and act on each "
    "incoming event by reading peers, deciding per policy, and dispatching writes. ") * 80)


def _classify(exc: Exception) -> str:
    s = str(exc).lower()
    if "code: 500" in s or "internalservererror" in type(exc).__name__.lower() or "server had an error" in s:
        return "500"
    if "429" in s or "rate limit" in s:
        return "429"
    if "timeout" in s:
        return "timeout"
    if "401" in s or "403" in s:
        return "auth"
    return "other:" + type(exc).__name__


def _one_call(client, model, i, *, structured, big_prompt, max_tokens):
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": _BIG_SYSTEM if big_prompt else "You are a helpful assistant."},
            {"role": "user", "content": f"Reply briefly. Iteration {i}: name one thing to see in Paris."},
        ],
        max_completion_tokens=max_tokens,
    )
    if structured:
        kwargs["response_format"] = {"type": "json_schema",
                                     "json_schema": {"name": "smoke", "schema": _STRUCT_SCHEMA}}
    t0 = time.time()
    try:
        client.chat.completions.create(**kwargs)
        return "ok", time.time() - t0
    except Exception as e:
        return _classify(e), time.time() - t0


def run_variant(name, client, model, n, *, structured, big_prompt, max_tokens, concurrent=1):
    """Run n calls. concurrent=1 → sequential (0.3s apart); concurrent>1 → that
    many calls in flight at once (mimics the eval's --workers load)."""
    counts = collections.Counter()
    lat = []
    call = lambda i: _one_call(client, model, i, structured=structured, big_prompt=big_prompt, max_tokens=max_tokens)
    if concurrent <= 1:
        for i in range(n):
            r, dt = call(i)
            counts[r] += 1
            if r == "ok":
                lat.append(dt)
            time.sleep(0.3)
    else:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=concurrent) as ex:
            for r, dt in ex.map(call, range(n)):
                counts[r] += 1
                if r == "ok":
                    lat.append(dt)
    avg = sum(lat) / len(lat) if lat else 0
    detail = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"  {name:42s} ok={counts['ok']}/{n}  avg={avg:.1f}s  [{detail}]")
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="calls per variant")
    ap.add_argument("--model", default="gpt-5.4-mini")
    ap.add_argument("--endpoint-env", default=None, help="suffix for AZURE_OPENAI_*_<SUFFIX> (e.g. PROJECT)")
    ap.add_argument("--concurrent", "-c", type=int, default=1,
                    help="concurrent calls in flight (1=sequential; set to your --workers, e.g. 16, to mimic the eval)")
    a = ap.parse_args()
    client, ep = _client(a.endpoint_env)
    mode = "sequential" if a.concurrent <= 1 else f"{a.concurrent} concurrent"
    print(f"smoke test against {ep}  model={a.model}  n={a.n}/variant  ({mode})\n")
    # Variant matrix: isolate call shape from prompt size.
    run_variant("A official-minimal (plain, short)", client, a.model, a.n,
                structured=False, big_prompt=False, max_tokens=64, concurrent=a.concurrent)
    run_variant("B structured-output (json_schema)", client, a.model, a.n,
                structured=True, big_prompt=False, max_tokens=256, concurrent=a.concurrent)
    run_variant("C big-prompt (plain, ~3k-tok system)", client, a.model, a.n,
                structured=False, big_prompt=True, max_tokens=64, concurrent=a.concurrent)
    run_variant("D ours (structured + big-prompt)", client, a.model, a.n,
                structured=True, big_prompt=True, max_tokens=256, concurrent=a.concurrent)
    print("\nReading: if A is clean but B/C/D fail, it's our call shape (schema/prompt).")
    print("If all four fail similarly under load (500/429), it's the deployment's")
    print("throughput/quota — lower --workers or raise the deployment's TPM quota.")


if __name__ == "__main__":
    main()
