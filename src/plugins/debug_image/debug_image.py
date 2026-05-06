from datetime import datetime

from PIL import Image, ImageColor, ImageDraw

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import get_font


class DebugImage(BasePlugin):
    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        orientation = device_config.get_config("orientation", default="horizontal")
        if orientation == "vertical":
            dimensions = dimensions[::-1]

        title = settings.get("title") or "InkyPi debug plugin"
        accent_color = ImageColor.getrgb(settings.get("accentColor") or "#1f7a8c")
        background_color = ImageColor.getrgb(settings.get("backgroundColor") or "#f7f3e8")

        width, height = dimensions
        image = Image.new("RGB", dimensions, background_color)
        draw = ImageDraw.Draw(image)

        self._draw_grid(draw, width, height)
        self._draw_frame(draw, width, height, accent_color)
        self._draw_labels(draw, width, height, title, accent_color, orientation)

        return image

    @staticmethod
    def _draw_grid(draw, width, height):
        grid_color = (210, 205, 192)
        step = max(min(width, height) // 8, 24)

        for x in range(0, width + 1, step):
            draw.line((x, 0, x, height), fill=grid_color, width=1)
        for y in range(0, height + 1, step):
            draw.line((0, y, width, y), fill=grid_color, width=1)

        draw.line((0, 0, width, height), fill=(185, 177, 160), width=2)
        draw.line((0, height, width, 0), fill=(185, 177, 160), width=2)

    @staticmethod
    def _draw_frame(draw, width, height, accent_color):
        border = max(min(width, height) // 40, 8)
        draw.rectangle(
            (border, border, width - border - 1, height - border - 1),
            outline=accent_color,
            width=max(border // 3, 3),
        )

        mark = border * 3
        for x, y in (
            (border, border),
            (width - border, border),
            (border, height - border),
            (width - border, height - border),
        ):
            draw.ellipse(
                (x - mark // 2, y - mark // 2, x + mark // 2, y + mark // 2),
                fill=accent_color,
            )

    @staticmethod
    def _draw_labels(draw, width, height, title, accent_color, orientation):
        dark = (38, 36, 32)
        body_font = get_font("Jost", max(int(width * 0.034), 14))
        small_font = get_font("Jost", max(int(width * 0.025), 12))
        title_font = DebugImage._fit_font(title, width * 0.62, max(int(width * 0.075), 24))

        center = (width / 2, height / 2)
        draw.rounded_rectangle(
            (
                width * 0.14,
                height * 0.34,
                width * 0.86,
                height * 0.66,
            ),
            radius=max(min(width, height) // 48, 6),
            fill=(255, 255, 255),
            outline=accent_color,
            width=max(min(width, height) // 120, 2),
        )

        draw.text((center[0], height * 0.44), title, anchor="mm", fill=dark, font=title_font)
        draw.text(
            (center[0], height * 0.55),
            f"{width} x {height} px | {orientation}",
            anchor="mm",
            fill=accent_color,
            font=body_font,
        )
        draw.text(
            (center[0], height * 0.61),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            anchor="mm",
            fill=(92, 86, 76),
            font=small_font,
        )

    @staticmethod
    def _fit_font(text, max_width, start_size):
        font_size = start_size
        while font_size > 10:
            font = get_font("Jost", font_size, "bold")
            if font.getlength(text) <= max_width:
                return font
            font_size -= 2

        return get_font("Jost", font_size, "bold")
