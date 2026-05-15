"""
Azure streaming usage proxy.

Sits between the OpenClaw gateway and Azure OpenAI. For streaming chat
completion requests it injects `stream_options: {include_usage: true}` so
that Azure returns token counts in the final SSE chunk. The gateway reads that
data and populates session inputTokens/outputTokens normally.

Usage:
    python3 azure_usage_proxy.py --upstream https://<resource>.openai.azure.com/openai/v1 --port 18800
"""
import argparse, asyncio, json, logging, sys
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response
import uvicorn

log = logging.getLogger("azure-proxy")


def make_app(upstream: str) -> FastAPI:
    app = FastAPI()
    upstream = upstream.rstrip("/")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy(request: Request, path: str):
        url = f"{upstream}/{path}"
        if request.query_params:
            url += "?" + str(request.query_params)

        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding")
        }

        is_streaming_chat = (
            path.rstrip("/").endswith("chat/completions")
            and request.method == "POST"
        )

        if is_streaming_chat and body:
            try:
                data = json.loads(body)
                if data.get("stream"):
                    data.setdefault("stream_options", {})["include_usage"] = True
                    body = json.dumps(data).encode()
                    log.warning("injected stream_options.include_usage=true → %s", path)
                else:
                    log.warning("non-streaming chat completions → %s", path)
            except (json.JSONDecodeError, AttributeError):
                pass
        else:
            log.warning("request → /%s", path)

        async def stream_upstream():
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    request.method, url, headers=headers, content=body
                ) as resp:
                    yield (resp.status_code, dict(resp.headers))
                    async for chunk in resp.aiter_bytes(chunk_size=None):
                        yield chunk

        gen = stream_upstream()
        first = await gen.__anext__()
        status_code, resp_headers = first

        safe_headers = {
            k: v for k, v in resp_headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        }

        return StreamingResponse(
            gen,
            status_code=status_code,
            headers=safe_headers,
            media_type=resp_headers.get("content-type", "application/octet-stream"),
        )

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True, help="Azure OpenAI base URL (e.g. https://<resource>.openai.azure.com/openai/v1)")
    parser.add_argument("--port", type=int, default=18800)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        stream=sys.stdout,
        format="[proxy] %(message)s",
    )

    app = make_app(args.upstream)
    log.warning("listening on %s:%d → %s", args.host, args.port, args.upstream)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
