from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence


TEMPERATURE_PHASE = "temperature"
GIF_PHASE = "gif"
TEMPERATURE_ITEM = "temperature"
GIF_ITEM = "gif"
DEFAULT_CAROUSEL_PHASE_SECONDS = 5.0
CAROUSEL_DURATION_OPTIONS = (5, 10, 15, 20, 25, 30)


def carousel_phase(elapsed_seconds: float, phase_seconds: float = DEFAULT_CAROUSEL_PHASE_SECONDS) -> str:
    phase_seconds = max(1.0, float(phase_seconds))
    phase_index = int(max(0.0, float(elapsed_seconds)) // phase_seconds)
    return TEMPERATURE_PHASE if phase_index % 2 == 0 else GIF_PHASE


def normalize_carousel_duration(value: float | int | str | None) -> int:
    try:
        seconds = int(float(value or DEFAULT_CAROUSEL_PHASE_SECONDS))
    except (TypeError, ValueError):
        seconds = int(DEFAULT_CAROUSEL_PHASE_SECONDS)
    return min(CAROUSEL_DURATION_OPTIONS, key=lambda option: abs(option - seconds))


def normalize_carousel_items(items: object) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(items, list):
        return normalized

    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == TEMPERATURE_ITEM:
            normalized.append({"type": TEMPERATURE_ITEM})
        elif item_type == GIF_ITEM:
            path = str(item.get("path") or "").strip()
            if path:
                normalized.append({"type": GIF_ITEM, "path": path})
    return normalized


def carousel_item_at(
    items: list[dict[str, str]],
    elapsed_seconds: float,
    item_seconds: float,
) -> tuple[int, dict[str, str], float]:
    if not items:
        raise ValueError("carousel item list is empty")

    item_seconds = max(1.0, float(item_seconds))
    elapsed_seconds = max(0.0, float(elapsed_seconds))
    item_index = int(elapsed_seconds // item_seconds) % len(items)
    item_elapsed = elapsed_seconds % item_seconds
    return item_index, items[item_index], item_elapsed


class CarouselGifPlayer:
    def __init__(self, path: str | Path, size: int) -> None:
        self.path = Path(path)
        self.size = int(size)
        self.frames, self.durations = _load_gif_frames(self.path, self.size)
        self.timeline = _frame_timeline(self.durations)
        self.total_duration = self.timeline[-1]
        self.index = 0

    def next_frame(self) -> Image.Image:
        frame = self.frames[self.index].copy()
        self.index = (self.index + 1) % len(self.frames)
        return frame

    def frame_at(self, elapsed_seconds: float) -> Image.Image:
        frame_index = self._frame_index_at(elapsed_seconds)
        return self.frames[frame_index].copy()

    def seconds_until_next_frame(self, elapsed_seconds: float) -> float:
        position = float(elapsed_seconds) % self.total_duration
        frame_index = self._frame_index_at(elapsed_seconds)
        return max(0.01, self.timeline[frame_index] - position)

    def _frame_index_at(self, elapsed_seconds: float) -> int:
        position = float(elapsed_seconds) % self.total_duration
        return min(len(self.frames) - 1, bisect_right(self.timeline, position))


def _load_gif_frames(path: Path, size: int) -> tuple[list[Image.Image], list[float]]:
    if not path.exists():
        raise ValueError(f"GIF file not found: {path}")

    frames: list[Image.Image] = []
    durations: list[float] = []
    with Image.open(path) as source:
        if source.format != "GIF":
            raise ValueError("the selected file is not a GIF")

        for frame in ImageSequence.Iterator(source):
            rgba = ImageOps.exif_transpose(frame.convert("RGBA"))
            background = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
            background.alpha_composite(rgba)
            fitted = ImageOps.fit(
                background.convert("RGB"),
                (size, size),
                method=Image.Resampling.LANCZOS,
            )
            frames.append(fitted)
            durations.append(max(0.01, float(frame.info.get("duration") or source.info.get("duration") or 100) / 1000))

    if not frames:
        raise ValueError("the GIF does not contain readable frames")
    return frames, durations


def _frame_timeline(durations: list[float]) -> list[float]:
    timeline: list[float] = []
    elapsed = 0.0
    for duration in durations:
        elapsed += duration
        timeline.append(elapsed)
    return timeline
