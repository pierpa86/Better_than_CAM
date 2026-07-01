from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .models import CoolerStatus, StatusSnapshot, SystemStatus


DEGREE = "\N{DEGREE SIGN}"
GAUGE_MIN_C = 0.0
GAUGE_MAX_C = 130.0
GAUGE_START_DEGREES = 138.0
GAUGE_END_DEGREES = 405.0
TEMPERATURE_LAYOUT_DETAILED = "detailed"
TEMPERATURE_LAYOUT_CENTER_GAUGE = "center_gauge"
TEMPERATURE_LAYOUT_DUAL = "dual"
TEMPERATURE_CENTER_SOURCES = ("gpu", "cpu", "liquid")
DEFAULT_TEMPERATURE_CENTER_TITLE = "TEMP"
DEFAULT_DUAL_TEMPERATURE_TITLE = "NZXT"

DEFAULT_TEMPERATURE_COLORS: dict[str, str] = {
    "background": "#000000",
    "text": "#eef4f0",
    "gauge_start": "#1ebef8",
    "gauge_end": "#ad53ed",
    "gauge_track_start": "#1e3748",
    "gauge_track_end": "#48144e",
    "center_gauge_start": "#35ff00",
    "center_gauge_end": "#bfff00",
    "center_track_start": "#3f4700",
    "center_track_end": "#601006",
    "dual_track": "#151515",
    "dual_left_gauge": "#7100d7",
    "dual_right_gauge": "#d700bd",
    "divider": "#b2b2b2",
}

DEFAULT_TEMPERATURE_ELEMENTS: dict[str, bool] = {
    "gauge": True,
    "primary": True,
    "divider": True,
    "cpu": True,
    "liquid": True,
    "title": False,
}

DEFAULT_DETAILED_TEMPERATURE_LAYOUT: dict[str, dict[str, float]] = {
    "gpu": {"x": 0.412, "y": 0.252, "scale": 1.0},
    "divider": {"x": 0.628, "y": 0.315, "scale": 1.0},
    "cpu": {"x": 0.662, "y": 0.308, "scale": 1.0},
    "liquid": {"x": 0.662, "y": 0.510, "scale": 1.0},
}

DEFAULT_CENTER_GAUGE_LAYOUT: dict[str, dict[str, float]] = {
    "center_gauge": {"x": 0.500, "y": 0.500, "scale": 1.0},
    "center_title": {"x": 0.500, "y": 0.272, "scale": 1.0},
    "center_primary": {"x": 0.500, "y": 0.340, "scale": 1.0},
}

DEFAULT_DUAL_TEMPERATURE_LAYOUT: dict[str, dict[str, float]] = {
    "dual_title": {"x": 0.500, "y": 0.222, "scale": 1.0},
    "dual_cpu": {"x": 0.340, "y": 0.370, "scale": 1.0},
    "dual_gpu": {"x": 0.675, "y": 0.370, "scale": 1.0},
}

DEFAULT_TEMPERATURE_LAYOUT: dict[str, dict[str, float]] = {
    **DEFAULT_DETAILED_TEMPERATURE_LAYOUT,
    **DEFAULT_CENTER_GAUGE_LAYOUT,
    **DEFAULT_DUAL_TEMPERATURE_LAYOUT,
}

DEFAULT_TEMPERATURE_SLOT_SOURCES: dict[str, str] = {
    "gpu": "gpu",
    "cpu": "cpu",
    "liquid": "liquid",
}


@dataclass(frozen=True, slots=True)
class RenderTheme:
    background_top: tuple[int, int, int] = (8, 13, 18)
    background_bottom: tuple[int, int, int] = (18, 24, 29)
    text: tuple[int, int, int] = (238, 244, 240)
    muted: tuple[int, int, int] = (137, 153, 151)
    aqua: tuple[int, int, int] = (77, 224, 202)
    green: tuple[int, int, int] = (126, 231, 135)
    amber: tuple[int, int, int] = (245, 179, 82)
    red: tuple[int, int, int] = (239, 93, 93)
    panel: tuple[int, int, int] = (26, 35, 39)


class LcdRenderer:
    def __init__(
        self,
        size: int = 640,
        theme: RenderTheme | None = None,
        background_path: str | Path | None = None,
        temperature_colors: dict[str, str] | None = None,
        temperature_elements: dict[str, bool] | None = None,
        temperature_layout: dict[str, dict[str, float]] | None = None,
        temperature_layout_mode: str | None = None,
        temperature_center_source: str | None = None,
        temperature_sources: dict[str, str] | None = None,
        temperature_center_title: str | None = None,
    ) -> None:
        self.size = int(size)
        self.theme = theme or RenderTheme()
        self.background_path = Path(background_path) if background_path else None
        self.temperature_colors = normalize_temperature_colors(temperature_colors)
        self.temperature_elements = normalize_temperature_elements(temperature_elements)
        self.temperature_layout = normalize_temperature_layout(temperature_layout)
        self.temperature_layout_mode = normalize_temperature_layout_mode(temperature_layout_mode)
        self.temperature_center_source = normalize_temperature_center_source(temperature_center_source)
        self.temperature_sources = normalize_temperature_sources(temperature_sources)
        self.temperature_center_title = normalize_temperature_center_title(temperature_center_title)
        self.fonts = _FontSet(self.size)

    def render(self, snapshot: StatusSnapshot) -> Image.Image:
        image = Image.new("RGB", (self.size, self.size), _hex_to_rgb(self.temperature_colors["background"]))
        draw = ImageDraw.Draw(image)
        if self.temperature_layout_mode == TEMPERATURE_LAYOUT_DUAL:
            self._draw_dual_infographic_face(image, draw, snapshot)
        elif self.temperature_layout_mode == TEMPERATURE_LAYOUT_CENTER_GAUGE:
            self._draw_center_gauge_face(image, draw, snapshot)
        else:
            self._draw_nzxt_face(image, draw, snapshot)
        return image

    def save(self, snapshot: StatusSnapshot, path: str | Path) -> Path:
        image = self.render(snapshot)
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output)
        return output

    def save_gif(self, snapshot: StatusSnapshot, path: str | Path) -> Path:
        image = self.render(snapshot)
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        image.convert("P", palette=Image.Palette.ADAPTIVE, colors=256).save(output, format="GIF", optimize=True)
        return output

    def _base_image(self) -> Image.Image:
        if self.background_path and self.background_path.exists():
            try:
                with Image.open(self.background_path) as source:
                    image = ImageOps.exif_transpose(source.convert("RGB"))
                    image = ImageOps.fit(image, (self.size, self.size), method=Image.Resampling.LANCZOS)
                    overlay = Image.new("RGB", image.size, (0, 0, 0))
                    return Image.blend(image, overlay, 0.38)
            except OSError:
                pass
        return Image.new("RGB", (self.size, self.size), self.theme.background_top)

    def _draw_generated_background(self, draw: ImageDraw.ImageDraw) -> None:
        for y in range(self.size):
            t = y / max(1, self.size - 1)
            color = tuple(
                int(self.theme.background_top[i] * (1 - t) + self.theme.background_bottom[i] * t)
                for i in range(3)
            )
            draw.line([(0, y), (self.size, y)], fill=color)

        self._draw_grid(draw)

    def _apply_background_vignette(self, image: Image.Image) -> Image.Image:
        center = self.size / 2
        max_distance = math.hypot(center, center)
        mask = Image.new("L", (self.size, self.size), 0)
        pixels = mask.load()
        for y in range(self.size):
            for x in range(self.size):
                distance = math.hypot(x - center, y - center)
                pixels[x, y] = int(_clamp((distance / max_distance) ** 1.8, 0, 1) * 95)
        overlay = Image.new("RGB", image.size, (0, 0, 0))
        image.paste(overlay, (0, 0), mask)
        return image

    def _draw_grid(self, draw: ImageDraw.ImageDraw) -> None:
        step = self.size // 8
        grid_color = (30, 40, 42)
        for pos in range(-self.size, self.size * 2, step):
            draw.line([(pos, 0), (pos - self.size, self.size)], fill=grid_color, width=1)

    def _draw_ring(self, draw: ImageDraw.ImageDraw, cooler: CoolerStatus) -> None:
        center = self.size // 2
        radius = int(self.size * 0.37)
        width = max(10, self.size // 34)
        box = [center - radius, center - radius, center + radius, center + radius]
        draw.arc(box, 0, 360, fill=(39, 52, 54), width=width)

        value = cooler.liquid_temp_c
        if value is None:
            sweep = 270
            color = self.theme.muted
        else:
            sweep = int(_clamp((value - 20) / 35, 0, 1) * 300)
            color = _temperature_color(value, self.theme)
        start = -220
        draw.arc(box, start, start + max(18, sweep), fill=color, width=width)

        for angle in range(-220, 80, 20):
            rad = math.radians(angle)
            inner = radius - width // 2 - 8
            outer = radius + width // 2 + 4
            x1 = center + math.cos(rad) * inner
            y1 = center + math.sin(rad) * inner
            x2 = center + math.cos(rad) * outer
            y2 = center + math.sin(rad) * outer
            draw.line([(x1, y1), (x2, y2)], fill=(55, 71, 73), width=2)

    def _draw_nzxt_face(self, image: Image.Image, draw: ImageDraw.ImageDraw, snapshot: StatusSnapshot) -> None:
        center = self.size // 2
        arc_radius = int(self.size * 0.425)
        arc_width = max(11, int(self.size * 0.019))

        primary_source = self.temperature_sources["gpu"]
        primary_value, primary_unit, primary_label, primary_temp = _temperature_source_metric(
            snapshot,
            primary_source,
            use_gpu_load_fallback=True,
        )
        gauge_temperature = primary_temp
        if gauge_temperature is None and primary_source == "gpu":
            gauge_temperature = _primary_gauge_temperature(snapshot)

        if self.temperature_elements["gauge"]:
            self._draw_temperature_gauge(
                image,
                draw,
                center=(center, center),
                radius=arc_radius,
                width=arc_width,
                temp_c=gauge_temperature,
            )

        if self.temperature_elements.get("title") and self.temperature_center_title:
            self._draw_temperature_title(draw)

        gpu_layout = self.temperature_layout["gpu"]
        left_center_x = int(self.size * gpu_layout["x"])
        value_top = int(self.size * gpu_layout["y"])
        if self.temperature_elements["primary"]:
            self._draw_primary_metric(
                draw,
                primary_value,
                primary_unit,
                primary_label,
                left_center_x,
                value_top,
                gpu_layout["scale"],
            )

        if self.temperature_elements["divider"]:
            divider_layout = self.temperature_layout["divider"]
            divider_x = int(self.size * divider_layout["x"])
            divider_y = int(self.size * divider_layout["y"])
            divider_height = int(self.size * 0.310 * divider_layout["scale"])
            draw.line(
                [(divider_x, divider_y), (divider_x, divider_y + divider_height)],
                fill=_hex_to_rgb(self.temperature_colors["divider"]),
                width=max(1, int((self.size // 320) * divider_layout["scale"])),
            )

        if self.temperature_elements["cpu"]:
            cpu_layout = self.temperature_layout["cpu"]
            cpu_value, cpu_unit, cpu_label, _cpu_temp = _temperature_source_metric(snapshot, self.temperature_sources["cpu"])
            self._draw_side_metric(
                draw,
                int(self.size * cpu_layout["x"]),
                int(self.size * cpu_layout["y"]),
                cpu_value,
                cpu_unit,
                cpu_label,
                cpu_layout["scale"],
            )
        if self.temperature_elements["liquid"]:
            liquid_layout = self.temperature_layout["liquid"]
            liquid_value, liquid_unit, liquid_label, _liquid_temp = _temperature_source_metric(
                snapshot,
                self.temperature_sources["liquid"],
            )
            self._draw_side_metric(
                draw,
                int(self.size * liquid_layout["x"]),
                int(self.size * liquid_layout["y"]),
                liquid_value,
                liquid_unit,
                liquid_label,
                liquid_layout["scale"],
            )

    def _draw_center_gauge_face(self, image: Image.Image, draw: ImageDraw.ImageDraw, snapshot: StatusSnapshot) -> None:
        gauge_layout = self.temperature_layout["center_gauge"]
        center = (int(self.size * gauge_layout["x"]), int(self.size * gauge_layout["y"]))
        radius = int(self.size * 0.435 * gauge_layout["scale"])
        width = max(18, int(self.size * 0.060 * gauge_layout["scale"]))
        value, unit, label, temp_c = _center_temperature_source(snapshot, self.temperature_center_source)
        text_color = _hex_to_rgb(self.temperature_colors["text"])

        if self.temperature_elements["gauge"]:
            self._draw_temperature_gauge(
                image,
                draw,
                center=center,
                radius=radius,
                width=width,
                temp_c=temp_c,
                start_color_key="center_gauge_start",
                end_color_key="center_gauge_end",
                track_start_color_key="center_track_start",
                track_end_color_key="center_track_end",
                dot_radius_factor=0.62,
                marker_gap_degrees=12.0,
                track_from_current=True,
                max_c=120.0,
            )

        if self.temperature_center_title:
            self._draw_temperature_title(draw)

        if not self.temperature_elements["primary"]:
            return

        primary_layout = self.temperature_layout["center_primary"]
        primary_scale = primary_layout["scale"]
        value_font = _load_font(self.size, max(8, int(195 * primary_scale)), bold=True)
        unit_font = _load_font(self.size, max(8, int(90 * primary_scale)), bold=True)
        label_font = _load_font(self.size, max(8, int(45 * primary_scale)), bold=True)
        value_box = draw.textbbox((0, 0), value, font=value_font)
        value_width = value_box[2] - value_box[0]
        value_height = value_box[3] - value_box[1]
        total_width = value_width
        unit_gap = int(self.size * 0.045 * primary_scale)
        if unit:
            unit_box = draw.textbbox((0, 0), unit, font=unit_font)
            total_width += unit_gap + (unit_box[2] - unit_box[0])
        x = int(self.size * primary_layout["x"]) - total_width / 2 + int(self.size * 0.027 * primary_scale)
        y = int(self.size * primary_layout["y"])
        draw.text((x, y), value, font=value_font, fill=text_color)
        if unit:
            unit_x = x + value_width + unit_gap
            draw.text((unit_x, y + int(value_height * 0.31)), unit, font=unit_font, fill=text_color)
        label_y = y + int(self.size * 0.365 * primary_scale)
        _center_text(draw, label, label_font, text_color, int(self.size * primary_layout["x"]), label_y)

    def _draw_dual_infographic_face(self, image: Image.Image, draw: ImageDraw.ImageDraw, snapshot: StatusSnapshot) -> None:
        center = self.size // 2
        radius = int(self.size * 0.447)
        width = max(22, int(self.size * 0.091))
        track_color = _hex_to_rgb(self.temperature_colors["dual_track"])
        cpu_value, cpu_unit, cpu_label, cpu_temp = _temperature_source_metric(snapshot, "cpu")
        gpu_value, gpu_unit, gpu_label, gpu_temp = _temperature_source_metric(snapshot, "gpu")

        if self.temperature_elements["gauge"]:
            self._draw_gradient_arc(
                image,
                center=(center, center),
                radius=radius,
                start_degrees=0,
                end_degrees=359.8,
                width=width,
                start_color=track_color,
                end_color=track_color,
            )
            if self.temperature_elements.get("cpu", True):
                self._draw_dual_temperature_arc(
                    image,
                    center=(center, center),
                    radius=radius,
                    width=width,
                    center_degrees=180,
                    temp_c=cpu_temp,
                    color=_hex_to_rgb(self.temperature_colors["dual_left_gauge"]),
                )
            if self.temperature_elements.get("primary", True):
                self._draw_dual_temperature_arc(
                    image,
                    center=(center, center),
                    radius=radius,
                    width=width,
                    center_degrees=0,
                    temp_c=gpu_temp,
                    color=_hex_to_rgb(self.temperature_colors["dual_right_gauge"]),
                )

        text_color = _hex_to_rgb(self.temperature_colors["text"])
        title_layout = self.temperature_layout["dual_title"]
        title_text = self.temperature_center_title
        if title_text == DEFAULT_TEMPERATURE_CENTER_TITLE:
            title_text = DEFAULT_DUAL_TEMPERATURE_TITLE
        title_font = _fit_text_font(
            draw,
            title_text,
            self.size,
            max(8, int(47 * title_layout["scale"])),
            int(self.size * 0.36 * title_layout["scale"]),
            bold=True,
        )
        _center_text(
            draw,
            title_text,
            title_font,
            text_color,
            int(self.size * title_layout["x"]),
            int(self.size * title_layout["y"]),
        )

        if self.temperature_elements.get("cpu", True):
            cpu_layout = self.temperature_layout["dual_cpu"]
            self._draw_dual_metric(
                draw,
                cpu_value,
                cpu_unit,
                cpu_label,
                int(self.size * cpu_layout["x"]),
                int(self.size * cpu_layout["y"]),
                cpu_layout["scale"],
            )
        if self.temperature_elements.get("primary", True):
            gpu_layout = self.temperature_layout["dual_gpu"]
            self._draw_dual_metric(
                draw,
                gpu_value,
                gpu_unit,
                gpu_label,
                int(self.size * gpu_layout["x"]),
                int(self.size * gpu_layout["y"]),
                gpu_layout["scale"],
            )

    def _draw_dual_temperature_arc(
        self,
        image: Image.Image,
        center: tuple[int, int],
        radius: int,
        width: int,
        center_degrees: float,
        temp_c: float | None,
        color: tuple[int, int, int],
    ) -> None:
        if temp_c is None:
            return
        half_span = 90.0 * _temperature_gauge_ratio(temp_c)
        if half_span <= 0:
            return
        self._draw_gradient_arc(
            image,
            center=center,
            radius=radius,
            start_degrees=center_degrees - half_span,
            end_degrees=center_degrees + half_span,
            width=width,
            start_color=color,
            end_color=color,
        )

    def _draw_temperature_title(self, draw: ImageDraw.ImageDraw) -> None:
        title_layout = self.temperature_layout["center_title"]
        text_color = _hex_to_rgb(self.temperature_colors["text"])
        title_font = _fit_text_font(
            draw,
            self.temperature_center_title,
            self.size,
            max(8, int(47 * title_layout["scale"])),
            int(self.size * 0.33 * title_layout["scale"]),
            bold=True,
        )
        _center_text(
            draw,
            self.temperature_center_title,
            title_font,
            text_color,
            int(self.size * title_layout["x"]),
            int(self.size * title_layout["y"]),
        )

    def _draw_temperature_gauge(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        center: tuple[int, int],
        radius: int,
        width: int,
        temp_c: float | None,
        start_color_key: str = "gauge_start",
        end_color_key: str = "gauge_end",
        track_start_color_key: str = "gauge_track_start",
        track_end_color_key: str = "gauge_track_end",
        dot_radius_factor: float = 1.35,
        marker_gap_degrees: float = 0.0,
        track_from_current: bool = False,
        max_c: float = GAUGE_MAX_C,
    ) -> None:
        start_color = _hex_to_rgb(self.temperature_colors[start_color_key])
        end_color = _hex_to_rgb(self.temperature_colors[end_color_key])
        track_start_color = _hex_to_rgb(self.temperature_colors[track_start_color_key])
        track_end_color = _hex_to_rgb(self.temperature_colors[track_end_color_key])

        if temp_c is None:
            self._draw_gradient_arc(
                image,
                center=center,
                radius=radius,
                start_degrees=GAUGE_START_DEGREES,
                end_degrees=GAUGE_END_DEGREES,
                width=width,
                start_color=track_start_color,
                end_color=track_end_color,
            )
            return

        ratio = _temperature_gauge_ratio(temp_c, max_c=max_c)
        current_degrees = GAUGE_START_DEGREES + (GAUGE_END_DEGREES - GAUGE_START_DEGREES) * ratio
        current_color = _mix_color(start_color, end_color, ratio)
        active_end_degrees = max(GAUGE_START_DEGREES, current_degrees - marker_gap_degrees)

        if track_from_current:
            track_start_degrees = min(GAUGE_END_DEGREES, current_degrees + marker_gap_degrees)
            if track_start_degrees < GAUGE_END_DEGREES:
                self._draw_gradient_arc(
                    image,
                    center=center,
                    radius=radius,
                    start_degrees=track_start_degrees,
                    end_degrees=GAUGE_END_DEGREES,
                    width=width,
                    start_color=track_start_color,
                    end_color=track_end_color,
                )
        else:
            self._draw_gradient_arc(
                image,
                center=center,
                radius=radius,
                start_degrees=GAUGE_START_DEGREES,
                end_degrees=GAUGE_END_DEGREES,
                width=width,
                start_color=track_start_color,
                end_color=track_end_color,
            )

        if active_end_degrees > GAUGE_START_DEGREES:
            self._draw_gradient_arc(
                image,
                center=center,
                radius=radius,
                start_degrees=GAUGE_START_DEGREES,
                end_degrees=active_end_degrees,
                width=width,
                start_color=start_color,
                end_color=current_color,
            )

        dot_angle = math.radians(current_degrees)
        dot_center = (
            center[0] + math.cos(dot_angle) * radius,
            center[1] + math.sin(dot_angle) * radius,
        )
        self._draw_dot(draw, dot_center, max(2, int(width * dot_radius_factor)), current_color)

    def _draw_gradient_arc(
        self,
        image: Image.Image,
        center: tuple[int, int],
        radius: int,
        start_degrees: float,
        end_degrees: float,
        width: int,
        start_color: tuple[int, int, int],
        end_color: tuple[int, int, int],
        start_dot: bool = False,
        end_dot: bool = False,
    ) -> None:
        if end_degrees <= start_degrees:
            end_degrees += 360
        scale = 4
        layer_size = self.size * scale
        layer = Image.new("RGBA", (layer_size, layer_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        scaled_center = (center[0] * scale, center[1] * scale)
        scaled_radius = radius * scale
        scaled_width = max(1, width * scale)
        arc_length = radius * math.radians(end_degrees - start_degrees)
        steps = max(96, int(arc_length / 1.2))
        points: list[tuple[float, float]] = []
        for index in range(steps + 1):
            angle = math.radians(start_degrees + (end_degrees - start_degrees) * index / steps)
            points.append(
                (
                    scaled_center[0] + math.cos(angle) * scaled_radius,
                    scaled_center[1] + math.sin(angle) * scaled_radius,
                )
            )

        cap_radius = max(1, scaled_width // 2)
        for index in range(steps):
            t = index / max(1, steps - 1)
            color = (*_mix_color(start_color, end_color, t), 255)
            draw.line([points[index], points[index + 1]], fill=color, width=scaled_width)
            draw.ellipse(
                [
                    points[index][0] - cap_radius,
                    points[index][1] - cap_radius,
                    points[index][0] + cap_radius,
                    points[index][1] + cap_radius,
                ],
                fill=color,
            )
            draw.ellipse(
                [
                    points[index + 1][0] - cap_radius,
                    points[index + 1][1] - cap_radius,
                    points[index + 1][0] + cap_radius,
                    points[index + 1][1] + cap_radius,
                ],
                fill=color,
            )

        dot_radius = int(width * 1.35)
        if start_dot:
            scaled_dot_radius = dot_radius * scale
            color = (*start_color, 255)
            draw.ellipse(
                [
                    points[0][0] - scaled_dot_radius,
                    points[0][1] - scaled_dot_radius,
                    points[0][0] + scaled_dot_radius,
                    points[0][1] + scaled_dot_radius,
                ],
                fill=color,
            )
        if end_dot:
            scaled_dot_radius = dot_radius * scale
            color = (*end_color, 255)
            draw.ellipse(
                [
                    points[-1][0] - scaled_dot_radius,
                    points[-1][1] - scaled_dot_radius,
                    points[-1][0] + scaled_dot_radius,
                    points[-1][1] + scaled_dot_radius,
                ],
                fill=color,
            )

        antialiased = layer.resize((self.size, self.size), Image.Resampling.LANCZOS)
        image.paste(antialiased.convert("RGB"), (0, 0), antialiased.getchannel("A"))

    def _draw_dot(
        self,
        draw: ImageDraw.ImageDraw,
        center: tuple[float, float],
        radius: int,
        color: tuple[int, int, int],
    ) -> None:
        draw.ellipse(
            [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
            fill=color,
        )

    def _draw_primary_metric(
        self,
        draw: ImageDraw.ImageDraw,
        value: str,
        unit: str,
        label: str,
        center_x: int,
        top_y: int,
        scale: float,
    ) -> None:
        text_color = _hex_to_rgb(self.temperature_colors["text"])
        value_font = _load_font(self.size, int(172 * scale), bold=True)
        unit_font = _load_font(self.size, int(60 * scale), bold=True)
        label_font = _load_font(self.size, int(24 * scale), bold=True)
        value_box = draw.textbbox((0, 0), value, font=value_font)
        value_width = value_box[2] - value_box[0]
        value_height = value_box[3] - value_box[1]
        x = center_x - value_width / 2
        draw.text((x, top_y), value, font=value_font, fill=text_color)
        if unit:
            unit_x = x + value_width + max(4, self.size // 100)
            unit_y = top_y + int(value_height * (0.35 if unit == DEGREE else 0.06))
            draw.text((unit_x, unit_y), unit, font=unit_font, fill=text_color)
        label_y = top_y + int(self.size * 0.318 * scale)
        _center_text(draw, label, label_font, text_color, center_x, label_y)

    def _draw_side_metric(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        value: str,
        unit: str,
        label: str,
        scale: float = 1.0,
    ) -> None:
        text_color = _hex_to_rgb(self.temperature_colors["text"])
        value_font = _load_font(self.size, int(72 * scale), bold=True)
        unit_font = _load_font(self.size, int(34 * scale), bold=True)
        label_font = _load_font(self.size, int(22 * scale), bold=True)
        draw.text((x, y), value, font=value_font, fill=text_color)
        value_box = draw.textbbox((x, y), value, font=value_font)
        if unit:
            unit_y = y + int((value_box[3] - value_box[1]) * 0.32) if unit == DEGREE else y + 2
            draw.text((value_box[2] + max(4, self.size // 120), unit_y), unit, font=unit_font, fill=text_color)
        draw.text((x + 2, value_box[3] - max(4, int(4 * scale))), label, font=label_font, fill=text_color)

    def _draw_dual_metric(
        self,
        draw: ImageDraw.ImageDraw,
        value: str,
        unit: str,
        label: str,
        center_x: int,
        top_y: int,
        scale: float,
    ) -> None:
        text_color = _hex_to_rgb(self.temperature_colors["text"])
        value_font = _load_font(self.size, int(105 * scale), bold=True)
        unit_font = _load_font(self.size, int(52 * scale), bold=True)
        label_font = _load_font(self.size, int(48 * scale), bold=True)
        value_box = draw.textbbox((0, 0), value, font=value_font)
        value_width = value_box[2] - value_box[0]
        value_height = value_box[3] - value_box[1]
        unit_gap = int(self.size * 0.012 * scale)
        unit_width = 0
        if unit:
            unit_box = draw.textbbox((0, 0), unit, font=unit_font)
            unit_width = unit_box[2] - unit_box[0]
        total_width = value_width + (unit_gap + unit_width if unit else 0)
        x = center_x - total_width / 2
        draw.text((x, top_y), value, font=value_font, fill=text_color)
        if unit:
            unit_x = x + value_width + unit_gap
            unit_y = top_y + int(value_height * (0.27 if unit == DEGREE else 0.06))
            draw.text((unit_x, unit_y), unit, font=unit_font, fill=text_color)
        label_y = top_y + int(self.size * 0.236 * scale)
        _center_text(draw, label, label_font, text_color, center_x, label_y)

    def _draw_center(self, draw: ImageDraw.ImageDraw, snapshot: StatusSnapshot) -> None:
        liquid = snapshot.cooler.liquid_temp_c
        if liquid is None:
            main = "--"
            unit = DEGREE + "C"
        else:
            main = _temperature_digits(liquid)
            unit = DEGREE + "C"

        top = int(self.size * 0.245)
        value_y = int(self.size * 0.335)
        _center_text(draw, "LIQUID", self.fonts.label, self.theme.muted, self.size // 2, top)
        self._center_temperature(draw, main, unit, value_y)

        pump = _metric_value(snapshot.cooler.pump_rpm, "rpm")
        _center_text(draw, f"PUMP {pump}", self.fonts.small, self.theme.muted, self.size // 2, int(self.size * 0.59))

    def _center_temperature(self, draw: ImageDraw.ImageDraw, value: str, unit: str, y: int) -> None:
        value_box = draw.textbbox((0, 0), value, font=self.fonts.huge)
        unit_box = draw.textbbox((0, 0), unit, font=self.fonts.unit)
        value_width = value_box[2] - value_box[0]
        unit_width = unit_box[2] - unit_box[0]
        gap = max(8, self.size // 48)
        total_width = value_width + gap + unit_width
        x = self.size / 2 - total_width / 2
        draw.text((x, y), value, font=self.fonts.huge, fill=self.theme.text)
        draw.text((x + value_width + gap, y + int(self.size * 0.095)), unit, font=self.fonts.unit, fill=self.theme.aqua)

    def _draw_metric_pills(self, draw: ImageDraw.ImageDraw, snapshot: StatusSnapshot) -> None:
        size = self.size
        pill_w = int(size * 0.34)
        pill_h = int(size * 0.105)
        gap = int(size * 0.025)
        x_left = int(size * 0.14)
        x_right = size - x_left - pill_w
        y = int(size * 0.70)

        cpu_temp = _temp_or_load(snapshot.system.cpu_temp_c, snapshot.system.cpu_load_percent)
        gpu_temp = _temp_or_load(snapshot.system.gpu_temp_c, snapshot.system.gpu_load_percent)
        memory = _percent(snapshot.system.memory_percent)
        fan = _metric_value(snapshot.cooler.fan_rpm, "rpm")

        self._pill(draw, x_left, y, pill_w, pill_h, "CPU", cpu_temp, self.theme.green)
        self._pill(draw, x_right, y, pill_w, pill_h, "GPU", gpu_temp, self.theme.aqua)
        self._pill(draw, x_left, y + pill_h + gap, pill_w, pill_h, "RAM", memory, self.theme.amber)
        self._pill(draw, x_right, y + pill_h + gap, pill_w, pill_h, "FAN", fan, self.theme.text)

    def _pill(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        width: int,
        height: int,
        label: str,
        value: str,
        accent: tuple[int, int, int],
    ) -> None:
        radius = height // 3
        draw.rounded_rectangle(
            [x, y, x + width, y + height],
            radius=radius,
            fill=self.theme.panel,
            outline=(48, 62, 64),
            width=1,
        )
        draw.rectangle([x, y + radius, x + 5, y + height - radius], fill=accent)
        draw.text((x + 20, y + 9), label, font=self.fonts.tiny, fill=self.theme.muted)
        draw.text((x + 20, y + 29), value, font=self.fonts.metric, fill=self.theme.text)

    def _draw_footer(self, draw: ImageDraw.ImageDraw, captured_at: datetime, simulated: bool) -> None:
        label = captured_at.strftime("%H:%M")
        if simulated:
            label += " SIM"
        _center_text(draw, label, self.fonts.tiny, (89, 105, 103), self.size // 2, int(self.size * 0.943))


class _FontSet:
    def __init__(self, size: int) -> None:
        self.hero = _load_font(size, 172, bold=True)
        self.side_value = _load_font(size, 72, bold=True)
        self.side_label = _load_font(size, 22, bold=True)
        self.degree_big = _load_font(size, 60, bold=True)
        self.degree_small = _load_font(size, 34, bold=True)
        self.huge = _load_font(size, 116, bold=True)
        self.unit = _load_font(size, 28, bold=True)
        self.label = _load_font(size, 24, bold=True)
        self.small = _load_font(size, 22, bold=False)
        self.metric = _load_font(size, 24, bold=True)
        self.tiny = _load_font(size, 16, bold=True)


def _load_font(canvas_size: int, px_at_640: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    px = max(10, int(px_at_640 * canvas_size / 640))
    candidates = []
    if bold:
        candidates.extend(
            [
                r"C:\Windows\Fonts\segoeuib.ttf",
                r"C:\Windows\Fonts\arialbd.ttf",
            ]
        )
    candidates.extend(
        [
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default()


def normalize_temperature_colors(values: object) -> dict[str, str]:
    normalized = dict(DEFAULT_TEMPERATURE_COLORS)
    if not isinstance(values, dict):
        return normalized

    for key in DEFAULT_TEMPERATURE_COLORS:
        color = str(values.get(key) or "").strip()
        if _is_hex_color(color):
            normalized[key] = color.lower()
    return normalized


def normalize_temperature_elements(values: object) -> dict[str, bool]:
    normalized = dict(DEFAULT_TEMPERATURE_ELEMENTS)
    if not isinstance(values, dict):
        return normalized

    for key in DEFAULT_TEMPERATURE_ELEMENTS:
        if key in values:
            normalized[key] = bool(values[key])
    return normalized


def normalize_temperature_layout(values: object) -> dict[str, dict[str, float]]:
    normalized = {key: dict(value) for key, value in DEFAULT_TEMPERATURE_LAYOUT.items()}
    if not isinstance(values, dict):
        return normalized

    for key in DEFAULT_TEMPERATURE_LAYOUT:
        item = values.get(key)
        if not isinstance(item, dict):
            continue
        normalized[key]["x"] = _normalized_float(item.get("x"), normalized[key]["x"], 0.05, 0.95)
        normalized[key]["y"] = _normalized_float(item.get("y"), normalized[key]["y"], 0.02, 0.90)
        normalized[key]["scale"] = _normalized_float(item.get("scale"), normalized[key]["scale"], 0.50, 1.60)
    return normalized


def normalize_temperature_layout_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == TEMPERATURE_LAYOUT_DUAL:
        return TEMPERATURE_LAYOUT_DUAL
    if normalized == TEMPERATURE_LAYOUT_CENTER_GAUGE:
        return TEMPERATURE_LAYOUT_CENTER_GAUGE
    return TEMPERATURE_LAYOUT_DETAILED


def normalize_temperature_center_source(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in TEMPERATURE_CENTER_SOURCES:
        return normalized
    return "liquid"


def normalize_temperature_center_title(value: object) -> str:
    if value is None:
        return DEFAULT_TEMPERATURE_CENTER_TITLE
    normalized = str(value).strip()
    return normalized[:16]


def normalize_temperature_sources(values: object) -> dict[str, str]:
    normalized = dict(DEFAULT_TEMPERATURE_SLOT_SOURCES)
    if not isinstance(values, dict):
        return normalized

    for key in DEFAULT_TEMPERATURE_SLOT_SOURCES:
        if key in values:
            source = str(values[key] or "").strip().lower()
            if source in TEMPERATURE_CENTER_SOURCES:
                normalized[key] = source
    return normalized


def _normalized_float(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return _clamp(number, low, high)


def _center_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    center_x: int,
    top_y: int,
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    draw.text((center_x - width / 2, top_y), text, font=font, fill=fill)


def _fit_text_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    canvas_size: int,
    px_at_640: int,
    max_width: int,
    bold: bool = False,
) -> ImageFont.ImageFont:
    size = max(8, px_at_640)
    while size > 8:
        font = _load_font(canvas_size, size, bold=bold)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
        size -= 1
    return _load_font(canvas_size, 8, bold=bold)


def _temperature_color(temp_c: float, theme: RenderTheme) -> tuple[int, int, int]:
    if temp_c < 36:
        return theme.green
    if temp_c < 45:
        return theme.amber
    return theme.red


def _metric_value(value: float | int | None, suffix: str) -> str:
    if value is None:
        return "--"
    return f"{int(round(value))} {suffix}"


def _percent(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{int(round(value))}%"


def _temp_or_load(temp_c: float | None, load_percent: float | None) -> str:
    if temp_c is not None:
        return f"{_temperature_digits(temp_c)}{DEGREE}C"
    if load_percent is not None:
        return f"{int(round(load_percent))}%"
    return "--"


def _temperature_or_load(temp_c: float | None, load_percent: float | None) -> tuple[str, str]:
    if temp_c is not None:
        return _temperature_digits(temp_c), DEGREE
    if load_percent is not None:
        return str(int(round(load_percent))), "%"
    return "--", ""


def _temperature_only(temp_c: float | None) -> tuple[str, str]:
    if temp_c is None:
        return "--", DEGREE
    return _temperature_digits(temp_c), DEGREE


def _temperature_digits(temp_c: float) -> str:
    return str(int(round(temp_c)))


def _primary_gauge_temperature(snapshot: StatusSnapshot) -> float | None:
    if snapshot.system.gpu_temp_c is not None:
        return snapshot.system.gpu_temp_c
    if snapshot.system.cpu_temp_c is not None:
        return snapshot.system.cpu_temp_c
    return snapshot.cooler.liquid_temp_c


def _center_temperature_source(snapshot: StatusSnapshot, source: str) -> tuple[str, str, str, float | None]:
    return _temperature_source_metric(snapshot, source)


def _temperature_source_metric(
    snapshot: StatusSnapshot,
    source: str,
    use_gpu_load_fallback: bool = False,
) -> tuple[str, str, str, float | None]:
    source = normalize_temperature_center_source(source)
    if source == "gpu":
        if use_gpu_load_fallback:
            value, unit = _temperature_or_load(snapshot.system.gpu_temp_c, snapshot.system.gpu_load_percent)
        else:
            value, unit = _temperature_only(snapshot.system.gpu_temp_c)
        return value, unit, "GPU", snapshot.system.gpu_temp_c
    if source == "cpu":
        value, unit = _temperature_only(snapshot.system.cpu_temp_c)
        return value, unit, "CPU", snapshot.system.cpu_temp_c

    value = "--" if snapshot.cooler.liquid_temp_c is None else _temperature_digits(snapshot.cooler.liquid_temp_c)
    return value, DEGREE, "Liquid", snapshot.cooler.liquid_temp_c


def _temperature_gauge_ratio(temp_c: float, max_c: float = GAUGE_MAX_C) -> float:
    high = max(max_c, GAUGE_MIN_C + 1.0)
    return _clamp((float(temp_c) - GAUGE_MIN_C) / (high - GAUGE_MIN_C), 0.0, 1.0)


def _mix_color(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(start[i] * (1 - t) + end[i] * t) for i in range(3))


def _is_hex_color(value: str) -> bool:
    if len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip()
    if not _is_hex_color(value):
        value = "#000000"
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
