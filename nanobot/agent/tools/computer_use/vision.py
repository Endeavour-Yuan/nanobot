"""Vision routing for computer_use captures.

When the main model is not vision-capable (or when explicitly configured),
screenshots are sent through an auxiliary vision model (e.g. Ollama with
SmolVLM2) to produce a text description that the main model can work with.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def describe_screenshot(
    png_b64: str,
    provider: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
    element_summary: str = "",
) -> str | None:
    """Send a screenshot to an auxiliary vision model and return a text description.

    Args:
        png_b64:   base64-encoded PNG/JPEG screenshot
        provider:  provider id (e.g. "ollama", "openai")
        model:     model name (e.g. "ahmadwaqar/smolvlm2-500m-video")
        base_url:  custom API base url
        api_key:   API key
        element_summary: AX element tree summary for cross-reference

    Returns:
        Text description of the screenshot, or None if the call fails.
    """
    if not provider or not model:
        return None

    prompt = (
        "Describe what is visible in this macOS application screenshot in "
        "concise but specific terms. Mention the app name and window "
        "title if visible, the overall layout, any labelled buttons, "
        "menus or text fields, and any prominent text content. "
        "Do not invent details that are not actually visible."
    )
    if element_summary:
        prompt += f"\n\nAX element index for cross-reference:\n{element_summary}"

    try:
        result = await _call_vision_api(png_b64, prompt, provider, model, base_url, api_key)
    except Exception as e:
        logger.warning("vision model call failed: %s", e)
        return None

    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(result.get("description") or result.get("text") or "").strip()
    return None


async def _call_vision_api(
    png_b64: str,
    prompt: str,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
) -> str | dict:
    """Call the configured vision provider's chat/completions API.

    Supports any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, etc.).
    For non-OpenAI providers, add custom logic here.
    """
    import urllib.request as _req

    # Build the OpenAI-compatible request
    url = _resolve_base_url(provider, base_url)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 1024,
    }

    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = _req.Request(url, data=data, headers=headers, method="POST")
    with _req.urlopen(request, timeout=60) as resp:
        body = resp.read().decode()

    parsed = json.loads(body)

    # Extract text from OpenAI-style response
    choices = parsed.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list) and content:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
    return parsed.get("response", "") or parsed


def _resolve_base_url(provider: str, base_url: str) -> str:
    """Resolve the chat/completions URL for a provider."""
    if base_url:
        return base_url.rstrip("/") + "/chat/completions"

    # Known defaults
    defaults = {
        "ollama": "http://localhost:11434/v1/chat/completions",
        "lm_studio": "http://localhost:1234/v1/chat/completions",
        "vllm": "http://localhost:8000/v1/chat/completions",
    }
    if provider in defaults:
        return defaults[provider]

    # Assume OpenAI-compatible at /v1/chat/completions
    return f"https://api.{provider}.com/v1/chat/completions"
