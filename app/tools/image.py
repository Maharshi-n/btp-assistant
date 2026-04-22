"""DALL-E 3 image generation tool."""
from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime
from typing import Annotated

import httpx
from langchain_core.tools import tool

import app.config as app_config

logger = logging.getLogger(__name__)


@tool
async def generate_image(
    prompt: Annotated[str, "Detailed description of the image to generate"],
    filename: Annotated[str, "Output filename without extension (e.g. 'sunset_beach')"] = "",
) -> str:
    """Generate an image using DALL-E 3 and save it to the workspace.

    Uses DALL-E 3 Standard 1024x1024 ($0.04/image).
    Returns the saved file path so you can send it via telegram_send_file or reference it.

    Args:
        prompt: Detailed description of the image. Be specific — style, lighting, mood, subject.
        filename: Optional output filename (no extension). Defaults to a timestamp-based name.
    """
    from openai import AsyncOpenAI

    if not app_config.OPENAI_API_KEY:
        return "Error: OPENAI_API_KEY not configured."

    client = AsyncOpenAI(api_key=app_config.OPENAI_API_KEY)

    try:
        response = await client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
    except Exception as exc:
        logger.warning("generate_image: DALL-E 3 API error: %s", exc)
        return f"Image generation failed: {exc}"

    image_url = response.data[0].url
    revised_prompt = response.data[0].revised_prompt or prompt

    # Download the image
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            dl = await http.get(image_url)
            dl.raise_for_status()
            image_bytes = dl.content
    except Exception as exc:
        logger.warning("generate_image: download failed: %s", exc)
        return f"Image generated but download failed: {exc}\nURL: {image_url}"

    # Save to workspace/images/
    images_dir = app_config.WORKSPACE_DIR / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    safe_name = (
        "".join(c if c.isalnum() or c in "-_" else "_" for c in filename)[:60]
        if filename
        else datetime.now().strftime("image_%Y%m%d_%H%M%S")
    )
    dest = images_dir / f"{safe_name}.png"
    # Avoid overwriting — append counter if needed
    counter = 1
    while dest.exists():
        dest = images_dir / f"{safe_name}_{counter}.png"
        counter += 1

    dest.write_bytes(image_bytes)
    logger.info("generate_image: saved %s (%d bytes)", dest, len(image_bytes))

    return (
        f"Image saved to: {dest}\n"
        f"Revised prompt: {revised_prompt}\n"
        f"Size: {len(image_bytes):,} bytes\n"
        f"Use telegram_send_file('{dest}') to send it to Telegram."
    )
