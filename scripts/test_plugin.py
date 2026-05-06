import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from PIL import Image

from plugins.plugin_registry import get_plugin_instance, load_plugins


DEFAULT_RESOLUTIONS = [
    [400, 300],  # Inky wHAT
    [640, 400],  # Inky Impression 4"
    [600, 448],  # Inky Impression 5.7"
    [800, 480],  # Inky Impression 7.3"
]
ORIENTATIONS = ["horizontal", "vertical"]


def parse_args():
    parser = argparse.ArgumentParser(description="Render an InkyPi plugin preview on a PC.")
    parser.add_argument("plugin_id", nargs="?", default="debug_image")
    parser.add_argument(
        "--settings",
        default="{}",
        help="JSON object with plugin settings, for example: '{\"title\":\"Hello\"}'",
    )
    parser.add_argument("--title", help="Shortcut for debug_image.title")
    parser.add_argument("--accent-color", help="Shortcut for debug_image.accentColor")
    parser.add_argument("--background-color", help="Shortcut for debug_image.backgroundColor")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "scripts" / "output" / "plugin_preview.png",
    )
    return parser.parse_args()


def read_plugin_configs():
    configs = []
    for info_file in sorted((SRC_DIR / "plugins").glob("*/plugin-info.json")):
        with info_file.open(encoding="utf-8") as file:
            configs.append(json.load(file))
    return configs


def change_orientation(image, orientation):
    if orientation == "horizontal":
        return image
    if orientation == "vertical":
        return image.rotate(90, expand=1)
    raise ValueError(f"Unsupported orientation: {orientation}")


def resize_image(image, desired_size, image_settings=None):
    image_settings = image_settings or []
    img_width, img_height = image.size
    desired_width, desired_height = [int(value) for value in desired_size]
    desired_ratio = desired_width / desired_height
    keep_width = "keep-width" in image_settings

    x_offset = 0
    y_offset = 0
    new_width = img_width
    new_height = img_height

    if img_width / img_height > desired_ratio:
        new_width = int(img_height * desired_ratio)
        if not keep_width:
            x_offset = (img_width - new_width) // 2
    else:
        new_height = int(img_width / desired_ratio)
        if not keep_width:
            y_offset = (img_height - new_height) // 2

    image = image.crop((x_offset, y_offset, x_offset + new_width, y_offset + new_height))
    return image.resize((desired_width, desired_height), Image.LANCZOS)


def render_preview(plugin_config, settings, output_path):
    load_plugins([plugin_config])
    plugin_instance = get_plugin_instance(plugin_config)

    total_height = sum(max(resolution) for resolution in DEFAULT_RESOLUTIONS)
    total_width = max(max(resolution) for resolution in DEFAULT_RESOLUTIONS) * len(ORIENTATIONS)
    composite = Image.new("RGB", (total_width, total_height), color=(128, 128, 128))

    mock_device_config = MagicMock()
    y = 0
    for resolution in DEFAULT_RESOLUTIONS:
        width, height = resolution
        for index, orientation in enumerate(ORIENTATIONS):
            mock_device_config.get_resolution.return_value = resolution
            mock_device_config.get_config.side_effect = lambda key=None, default=None, value=orientation: (
                value if key == "orientation" else default
            )

            image = plugin_instance.generate_image(dict(settings), mock_device_config)
            image = change_orientation(image, orientation)
            image = resize_image(image, resolution, plugin_config.get("image_settings", []))

            if orientation == "vertical":
                image = image.rotate(-90, expand=1)

            x = int(total_width / len(ORIENTATIONS)) * index
            composite.paste(image.convert("RGB"), (x, y))

        y += max(width, height)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    composite.save(output_path)
    return output_path


def main():
    args = parse_args()
    settings = json.loads(args.settings)
    if args.title:
        settings["title"] = args.title
    if args.accent_color:
        settings["accentColor"] = args.accent_color
    if args.background_color:
        settings["backgroundColor"] = args.background_color
    plugin_configs = read_plugin_configs()
    plugin_config = next((config for config in plugin_configs if config.get("id") == args.plugin_id), None)

    if not plugin_config:
        available_plugins = ", ".join(config["id"] for config in plugin_configs)
        raise SystemExit(f"Plugin '{args.plugin_id}' not found. Available plugins: {available_plugins}")

    output_path = render_preview(plugin_config, settings, args.output)
    print(f"Rendered {args.plugin_id} preview: {output_path}")


if __name__ == "__main__":
    main()
