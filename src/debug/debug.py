import sys
from pathlib import Path

# Añadimos src al path para poder importar plugins de InkyPi
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


class FakeDeviceConfig:
    """
    Simula el device_config de InkyPi.
    Suficiente para muchos plugins que solo necesitan resolución,
    orientación, timezone o valores de config.
    """

    def __init__(self, width=800, height=480):
        self.width = width
        self.height = height

    def get_config(self, key, default=None):
        values = {
            "display_width": self.width,
            "display_height": self.height,
            "width": self.width,
            "height": self.height,
            "resolution": (self.width, self.height),
            "orientation": "landscape",
            "timezone": "Europe/Zurich",
        }
        return values.get(key, default)

    def get(self, key, default=None):
        return self.get_config(key, default)


def main():
    # Cambia estos imports por el plugin que quieras probar
    # Ejemplo inventado:
    # from plugins.clock.clock import Clock
    # plugin = Clock()

    from plugins.clock.clock import Clock
    plugin = Clock()

    settings = {
        # Aquí van los valores que normalmente vendrían del formulario settings.html
        # De momento puedes dejarlo vacío y añadir claves si el plugin se queja.
    }

    device_config = FakeDeviceConfig(width=800, height=480)

    image = plugin.generate_image(settings=settings, device_config=device_config)

    output_path = ROOT / "debug" / "preview.png"
    image.save(output_path)

    print(f"Preview generado en: {output_path}")
    print(f"Tamaño: {image.size}")


if __name__ == "__main__":
    main()
