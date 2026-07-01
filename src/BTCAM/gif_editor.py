from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps, ImageSequence


def sanitize_gif_stem(value: str, fallback: str = "giphy") -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_").lower()
    return (stem or fallback)[:48]


def initial_crop_zoom(width: int, height: int, output_size: int) -> float:
    return max(output_size / max(1, width), output_size / max(1, height))


def clamp_crop_offset(
    frame_width: int,
    frame_height: int,
    output_size: int,
    zoom: float,
    offset_x: float,
    offset_y: float,
) -> tuple[float, float]:
    resized_width = frame_width * zoom
    resized_height = frame_height * zoom
    max_x = max(0.0, (resized_width - output_size) / 2)
    max_y = max(0.0, (resized_height - output_size) / 2)
    return _clamp(offset_x, -max_x, max_x), _clamp(offset_y, -max_y, max_y)


def render_cropped_gif_frame(
    frame: Image.Image,
    output_size: int,
    zoom: float,
    offset_x: float,
    offset_y: float,
    mask_to_circle: bool = False,
) -> Image.Image:
    source = ImageOps.exif_transpose(frame.convert("RGBA"))
    offset_x, offset_y = clamp_crop_offset(source.width, source.height, output_size, zoom, offset_x, offset_y)
    resized_size = (max(1, int(round(source.width * zoom))), max(1, int(round(source.height * zoom))))
    resized = source.resize(resized_size, Image.Resampling.LANCZOS)
    output = Image.new("RGBA", (output_size, output_size), (0, 0, 0, 255))
    left = int(round((output_size - resized.width) / 2 + offset_x))
    top = int(round((output_size - resized.height) / 2 + offset_y))
    output.alpha_composite(resized, (left, top))
    if mask_to_circle:
        output = _mask_outside_circle(output)
    return output.convert("RGB")


def save_cropped_gif(
    source_path: str | Path,
    output_path: str | Path,
    output_size: int,
    zoom: float,
    offset_x: float,
    offset_y: float,
    mask_to_circle: bool = False,
    palette_colors: int = 96,
) -> Path:
    source_path = Path(source_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    frames: list[Image.Image] = []
    durations: list[int] = []
    with Image.open(source_path) as source:
        if source.format != "GIF":
            raise ValueError("the selected file is not a GIF")
        for frame in ImageSequence.Iterator(source):
            frames.append(render_cropped_gif_frame(frame, output_size, zoom, offset_x, offset_y, mask_to_circle=mask_to_circle))
            durations.append(int(frame.info.get("duration") or source.info.get("duration") or 100))

    if not frames:
        raise ValueError("the GIF does not contain readable frames")

    colors = max(2, min(256, int(palette_colors)))
    frames = [frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=colors) for frame in frames]
    frames[0].save(
        output_path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=1,
        optimize=True,
        interlace=False,
    )
    return output_path


def save_lcd_ready_gif(
    source_path: str | Path,
    output_path: str | Path,
    output_size: int,
    mask_to_circle: bool = True,
    palette_colors: int = 96,
) -> Path:
    source_path = Path(source_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    frames: list[Image.Image] = []
    durations: list[int] = []
    with Image.open(source_path) as source:
        if source.format != "GIF":
            raise ValueError("the selected file is not a GIF")
        for frame in ImageSequence.Iterator(source):
            frames.append(render_fitted_gif_frame(frame, output_size, mask_to_circle=mask_to_circle))
            durations.append(int(frame.info.get("duration") or source.info.get("duration") or 100))

    if not frames:
        raise ValueError("the GIF does not contain readable frames")

    colors = max(2, min(256, int(palette_colors)))
    frames = [frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=colors) for frame in frames]
    frames[0].save(
        output_path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=1,
        optimize=True,
        interlace=False,
    )
    return output_path


def render_fitted_gif_frame(
    frame: Image.Image,
    output_size: int,
    mask_to_circle: bool = False,
) -> Image.Image:
    source = ImageOps.exif_transpose(frame.convert("RGBA"))
    fitted = ImageOps.contain(source, (output_size, output_size), Image.Resampling.LANCZOS)
    output = Image.new("RGBA", (output_size, output_size), (0, 0, 0, 255))
    left = int(round((output_size - fitted.width) / 2))
    top = int(round((output_size - fitted.height) / 2))
    output.alpha_composite(fitted, (left, top))
    if mask_to_circle:
        output = _mask_outside_circle(output)
    return output.convert("RGB")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _mask_outside_circle(image: Image.Image) -> Image.Image:
    circle_mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(circle_mask)
    draw.ellipse((0, 0, image.width - 1, image.height - 1), fill=255)
    background = Image.new("RGBA", image.size, (0, 0, 0, 255))
    background.paste(image, (0, 0), circle_mask)
    return background
