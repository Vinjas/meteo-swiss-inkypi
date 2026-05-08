import csv
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz
import requests
from PIL import Image, ImageColor, ImageDraw

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import get_font, resolve_path


logger = logging.getLogger(__name__)

COLLECTION_URL = "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-local-forecasting"
ASSETS_URL = f"{COLLECTION_URL}/assets"
ITEM_URL = f"{COLLECTION_URL}/items/{{item_id}}"
META_POINT_ASSET = "ogd-local-forecasting_meta_point.csv"
DEFAULT_LATITUDE = 47.384278
DEFAULT_LONGITUDE = 8.120444
DEFAULT_TIMEZONE = "Europe/Zurich"
CACHE_TTL_SECONDS = 3 * 60 * 60
METADATA_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_RETENTION_SECONDS = 3 * 24 * 60 * 60  # Delete forecast cache files older than 3 days.
DEFAULT_BATTERY_STATUS_PATH = "config/battery.json"

# Set this to None to resolve the closest MeteoSwiss point from latitude/longitude instead.
FIXED_METEOSWISS_POINT = {
    "point_id": "550200",
    "point_type_id": "2",
    "point_name": "Hunzenschwil",
    "postal_code": "5502",
    "lat": 47.385344,
    "lon": 8.123061,
    "distance_km": 0.23,
}

PARAMETERS = {
    "temperature_hourly": "tre200h0",
    "weather_hourly": "jww003i0",
    "precip_hourly": "rre150h0",
    "precip_hourly_low": "rreq10h0",
    "precip_hourly_high": "rreq90h0",
    "precip_probability_hourly": "rp0003i0",
    "sunshine_hourly": "sre000h0",
    "wind_speed_hourly": "fu3010h0",
    "wind_gust_hourly": "fu3010h1",
    "wind_direction_hourly": "dkl010h0",
    "temperature_max_daily": "tre200px",
    "temperature_min_daily": "tre200pn",
    "precip_daily": "rka150p0",
    "precip_daily_low": "rreq10p0",
    "precip_daily_high": "rreq90p0",
    "weather_daily": "jp2000d0",
}


class MeteoSwissWeather(BasePlugin):
    def generate_image(self, settings, device_config):
        self.cleanup_cache()

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        tz_name = device_config.get_config("timezone", default=DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
        try:
            tz = pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone(DEFAULT_TIMEZONE)

        try:
            weather = self.load_weather(settings, tz, device_config)
        except Exception as exc:
            logger.exception("MeteoSwiss weather failed: %s", exc)
            raise RuntimeError("MeteoSwiss weather request failure, please check logs.")

        return self.render_weather(dimensions, weather, settings, tz)

    def load_weather(self, settings, tz, device_config):
        lat = float(settings.get("latitude") or DEFAULT_LATITUDE)
        lon = float(settings.get("longitude") or DEFAULT_LONGITUDE)
        point_id = (settings.get("pointId") or "").strip()
        point_type_id = (settings.get("pointTypeId") or "").strip()

        point = self.resolve_point(point_id, point_type_id, lat, lon)
        item = self.get_forecast_item()
        rows = {}
        for name, parameter in PARAMETERS.items():
            rows[name] = self.read_parameter_rows(item, parameter, point["point_id"], point["point_type_id"], tz)

        now = datetime.now(tz)
        current_temp = self.current_value(rows["temperature_hourly"], now)
        current_icon_code = self.current_value(rows["weather_hourly"], now)
        current_precip = self.current_value(rows["precip_hourly"], now, default=0)
        current_pop = self.current_value(rows["precip_probability_hourly"], now, default=None)
        current_wind = self.current_value(rows["wind_speed_hourly"], now, default=None)
        current_gust = self.current_value(rows["wind_gust_hourly"], now, default=None)
        current_wind_dir = self.current_value(rows["wind_direction_hourly"], now, default=None)

        hourly = self.merge_hourly(rows, now)
        forecast = self.merge_daily(rows, now.date(), int(settings.get("forecastDays") or 7))
        battery = self.load_battery_status(settings, device_config, now)

        return {
            "title": self.format_spanish_date(now),
            "location": point["point_name"],
            "point": point,
            "updated": datetime.now(tz),
            "current_date": now.strftime("%H:%M"),
            "current_temperature": current_temp,
            "current_icon": self.icon_path(current_icon_code, is_day=True),
            "current_precip": current_precip,
            "current_pop": current_pop,
            "current_wind": current_wind,
            "current_gust": current_gust,
            "current_wind_dir": current_wind_dir,
            "current_high": forecast[0].get("high") if forecast else None,
            "current_low": forecast[0].get("low") if forecast else None,
            "current_precip_low": forecast[0].get("precip_low") if forecast else None,
            "current_precip_high": forecast[0].get("precip_high") if forecast else None,
            "hourly": hourly,
            "forecast": forecast,
            "battery": battery,
        }

    def load_battery_status(self, settings, device_config, now):
        if settings.get("displayBattery", "true") != "true":
            return None

        path = (settings.get("batteryStatusPath") or DEFAULT_BATTERY_STATUS_PATH).strip()
        if not path:
            return None

        status_path = Path(path)
        if not status_path.is_absolute():
            base_dir = getattr(device_config, "BASE_DIR", None)
            if not isinstance(base_dir, (str, Path)):
                base_dir = Path(__file__).resolve().parents[2]
            status_path = Path(base_dir) / status_path
        if not status_path.exists():
            return None

        try:
            with status_path.open(encoding="utf-8-sig") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read battery status from %s: %s", status_path, exc)
            return None

        vin = self.safe_float(data.get("vin"))
        if vin is None:
            return None

        percent = self.safe_float(data.get("percent"))
        if percent is None:
            percent = self.estimate_lipo_percent(vin)

        updated = data.get("updated")
        age_minutes = None
        if updated:
            try:
                updated_dt = datetime.fromisoformat(str(updated)).astimezone(now.tzinfo)
                age_minutes = max(int((now - updated_dt).total_seconds() // 60), 0)
            except (TypeError, ValueError):
                age_minutes = None

        return {
            "vin": vin,
            "percent": max(0, min(100, int(round(percent)))),
            "charging": bool(data.get("charging", False)),
            "age_minutes": age_minutes,
        }

    def resolve_point(self, point_id, point_type_id, lat, lon):
        if not point_id and not point_type_id and FIXED_METEOSWISS_POINT:
            return dict(FIXED_METEOSWISS_POINT)

        if point_id and point_type_id:
            point = self.find_point_by_id(point_id, point_type_id)
            if point:
                return point
            return {
                "point_id": point_id,
                "point_type_id": point_type_id,
                "point_name": f"Point {point_id}",
                "distance_km": None,
            }

        points = self.read_points()
        nearest = min(
            points,
            key=lambda point: self.haversine_km(lat, lon, point["lat"], point["lon"]),
        )
        nearest["distance_km"] = self.haversine_km(lat, lon, nearest["lat"], nearest["lon"])
        return nearest

    def find_point_by_id(self, point_id, point_type_id):
        for point in self.read_points():
            if point["point_id"] == str(point_id) and point["point_type_id"] == str(point_type_id):
                return point
        return None

    def read_points(self):
        path = self.download_collection_asset(META_POINT_ASSET)
        points = []
        with open(path, newline="", encoding="latin1") as file:
            reader = csv.DictReader(file, delimiter=";")
            for row in reader:
                try:
                    points.append(
                        {
                            "point_id": row["point_id"],
                            "point_type_id": row["point_type_id"],
                            "point_name": row.get("point_name") or row.get("station_abbr") or "",
                            "postal_code": row.get("postal_code") or "",
                            "lat": float(row["point_coordinates_wgs84_lat"]),
                            "lon": float(row["point_coordinates_wgs84_lon"]),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        if not points:
            raise RuntimeError("MeteoSwiss point metadata is empty.")
        return points

    def get_forecast_item(self):
        last_error = None
        for offset in range(0, 4):
            day = datetime.now(timezone.utc).date() - timedelta(days=offset)
            item_id = f"{day:%Y%m%d}-ch"
            cached = self.read_cached_item(item_id)
            if cached and self.is_fresh(self.cache_dir() / f"{item_id}.json", CACHE_TTL_SECONDS):
                return cached
            try:
                response = requests.get(ITEM_URL.format(item_id=item_id), timeout=30)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                item = response.json()
                item["item_id"] = item_id
                return item
            except requests.RequestException as exc:
                last_error = exc
                cached = self.read_cached_item(item_id)
                if cached:
                    return cached
        raise RuntimeError(f"No MeteoSwiss forecast item available: {last_error}")

    def read_cached_item(self, item_id):
        path = self.cache_dir() / f"{item_id}.json"
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as file:
            item = json.load(file)
        item["item_id"] = item_id
        return item

    def cache_item(self, item):
        item_id = item.get("item_id") or item.get("id")
        if not item_id:
            return
        path = self.cache_dir() / f"{item_id}.json"
        with path.open("w", encoding="utf-8") as file:
            json.dump(item, file)

    def read_parameter_rows(self, item, parameter, point_id, point_type_id, tz):
        self.cache_item(item)
        asset = self.find_parameter_asset(item, parameter)
        if not asset:
            logger.warning("MeteoSwiss parameter %s not available in %s", parameter, item.get("item_id"))
            return []

        path = self.download_file(asset["href"], Path(asset["href"]).name)
        return self.cached_filtered_parameter(path, item["item_id"], parameter, point_id, point_type_id, tz)

    def cached_filtered_parameter(self, csv_path, item_id, parameter, point_id, point_type_id, tz):
        filtered_path = self.cache_dir() / f"{csv_path.stem}_{point_type_id}_{point_id}.json"
        if filtered_path.exists() and filtered_path.stat().st_mtime >= csv_path.stat().st_mtime:
            with filtered_path.open(encoding="utf-8") as file:
                cached_rows = json.load(file)
            return [
                {"time": datetime.fromisoformat(row["time"]).astimezone(tz), "value": row["value"]}
                for row in cached_rows
            ]

        rows = self.filter_parameter_csv(csv_path, parameter, point_id, point_type_id, tz)
        with filtered_path.open("w", encoding="utf-8") as file:
            json.dump(
                [{"time": row["time"].isoformat(), "value": row["value"]} for row in rows],
                file,
            )
        return rows

    @staticmethod
    def find_parameter_asset(item, parameter):
        suffix = f".{parameter}.csv"
        matching_assets = [
            (name, asset)
            for name, asset in item.get("assets", {}).items()
            if name.endswith(suffix)
        ]
        if not matching_assets:
            return None
        return max(
            matching_assets,
            key=lambda entry: (
                entry[1].get("updated") or entry[1].get("created") or "",
                entry[0],
            ),
        )[1]

    def download_collection_asset(self, asset_name):
        cached_path = self.cache_dir() / asset_name
        if cached_path.exists() and self.is_fresh(cached_path, METADATA_TTL_SECONDS):
            return cached_path

        response = requests.get(ASSETS_URL, timeout=30)
        response.raise_for_status()
        assets = {asset["id"]: asset for asset in response.json().get("assets", [])}
        asset = assets.get(asset_name)
        if not asset:
            raise RuntimeError(f"MeteoSwiss collection asset not found: {asset_name}")
        return self.download_file(asset["href"], asset_name)

    def download_file(self, url, filename):
        path = self.cache_dir() / filename
        if path.exists() and self.is_fresh(path, CACHE_TTL_SECONDS):
            return path

        etag_path = path.with_suffix(path.suffix + ".etag")
        headers = {}
        if path.exists() and etag_path.exists():
            headers["If-None-Match"] = etag_path.read_text(encoding="utf-8").strip()

        try:
            response = requests.get(url, headers=headers, stream=True, timeout=60)
            if response.status_code == 304 and path.exists():
                return path
            response.raise_for_status()

            temp_path = path.with_suffix(path.suffix + ".tmp")
            with temp_path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        file.write(chunk)
            temp_path.replace(path)

            etag = response.headers.get("ETag")
            if etag:
                etag_path.write_text(etag, encoding="utf-8")
            return path
        except requests.RequestException:
            if path.exists():
                logger.warning("Using cached MeteoSwiss file after download failure: %s", path)
                return path
            raise

    def filter_parameter_csv(self, path, parameter, point_id, point_type_id, tz):
        rows = []
        with open(path, newline="", encoding="latin1") as file:
            reader = csv.DictReader(file, delimiter=";")
            for row in reader:
                if row.get("point_id") != str(point_id) or row.get("point_type_id") != str(point_type_id):
                    continue
                raw_value = row.get(parameter)
                if raw_value in (None, ""):
                    continue
                try:
                    value = float(raw_value)
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    value = raw_value

                rows.append({"time": self.parse_meteoswiss_time(row["Date"], tz), "value": value})
        return rows

    @staticmethod
    def parse_meteoswiss_time(raw_time, tz):
        dt = datetime.strptime(raw_time, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)

    @staticmethod
    def current_value(rows, now, default=None):
        if not rows:
            return default

        previous = rows[0]
        for row in rows:
            if row["time"] <= now:
                previous = row
            else:
                return previous["value"]
        return previous["value"]

    def merge_hourly(self, rows, now):
        temp_by_time = {row["time"]: row["value"] for row in rows["temperature_hourly"]}
        icon_by_time = {row["time"]: row["value"] for row in rows["weather_hourly"]}
        precip_by_time = {row["time"]: row["value"] for row in rows["precip_hourly"]}
        precip_low_by_time = {row["time"]: row["value"] for row in rows["precip_hourly_low"]}
        precip_high_by_time = {row["time"]: row["value"] for row in rows["precip_hourly_high"]}
        pop_by_time = {row["time"]: row["value"] for row in rows["precip_probability_hourly"]}
        sunshine_by_time = {row["time"]: row["value"] for row in rows["sunshine_hourly"]}
        wind_by_time = {row["time"]: row["value"] for row in rows["wind_speed_hourly"]}
        gust_by_time = {row["time"]: row["value"] for row in rows["wind_gust_hourly"]}
        wind_dir_by_time = {row["time"]: row["value"] for row in rows["wind_direction_hourly"]}

        hourly = []
        for time in sorted(temp_by_time):
            if time < now:
                continue
            hourly.append(
                {
                    "time": time,
                    "label": time.strftime("%H:%M"),
                    "temperature": temp_by_time.get(time),
                    "icon": self.icon_path(icon_by_time.get(time), is_day=7 <= time.hour <= 19),
                    "precip": precip_by_time.get(time, 0),
                    "precip_low": precip_low_by_time.get(time),
                    "precip_high": precip_high_by_time.get(time),
                    "pop": pop_by_time.get(time),
                    "sunshine": sunshine_by_time.get(time, 0),
                    "wind": wind_by_time.get(time),
                    "gust": gust_by_time.get(time),
                    "wind_dir": wind_dir_by_time.get(time),
                }
            )
            if len(hourly) >= 24:
                break
        return hourly

    def merge_daily(self, rows, today, forecast_days):
        highs = self.rows_by_date(rows["temperature_max_daily"])
        lows = self.rows_by_date(rows["temperature_min_daily"])
        rain = self.rows_by_date(rows["precip_daily"])
        rain_low = self.rows_by_date(rows["precip_daily_low"])
        rain_high = self.rows_by_date(rows["precip_daily_high"])
        icons = self.rows_by_date(rows["weather_daily"])

        forecast = []
        for day_offset in range(0, forecast_days + 1):
            day = today + timedelta(days=day_offset)
            if day not in highs and day not in lows and day not in icons:
                continue
            forecast.append(
                {
                    "date": day,
                    "day": self.spanish_day_abbr(day),
                    "high": highs.get(day),
                    "low": lows.get(day),
                    "precip": rain.get(day, 0),
                    "precip_low": rain_low.get(day),
                    "precip_high": rain_high.get(day),
                    "icon": self.icon_path(icons.get(day), is_day=True),
                }
            )
        return forecast

    @staticmethod
    def rows_by_date(rows):
        return {row["time"].date(): row["value"] for row in rows}

    def icon_path(self, code, is_day=True):
        icon = self.map_meteoswiss_icon(code, is_day)
        return resolve_path(os.path.join("plugins", "weather", "icons", f"{icon}.png"))

    @staticmethod
    def map_meteoswiss_icon(code, is_day=True):
        try:
            code = int(code)
        except (TypeError, ValueError):
            return "01d" if is_day else "01n"

        if code in {1, 101}:
            return "01d" if is_day else "01n"
        if code in {2, 102, 3, 103}:
            return "022d" if is_day else "022n"
        if code in {4, 104, 5, 105}:
            return "02d" if is_day else "02n"
        if code in {6, 106, 7, 107, 8, 108}:
            return "04d"
        if code in {9, 109, 10, 110, 11, 111}:
            return "50d"
        if code in {12, 112, 13, 113, 14, 114, 15, 115, 16, 116}:
            return "51d"
        if code in {17, 117, 18, 118, 19, 119, 20, 120, 21, 121}:
            return "10d" if is_day else "10n"
        if code in {22, 122, 23, 123, 24, 124, 25, 125, 26, 126}:
            return "13d"
        if code in {27, 127, 28, 128, 29, 129, 30, 130, 31, 131}:
            return "11d"
        return "03d"

    @staticmethod
    def spanish_day_abbr(day):
        names = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]
        return names[day.weekday()]

    @staticmethod
    def format_spanish_date(dt):
        weekdays = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        months = [
            "Enero",
            "Febrero",
            "Marzo",
            "Abril",
            "Mayo",
            "Junio",
            "Julio",
            "Agosto",
            "Septiembre",
            "Octubre",
            "Noviembre",
            "Diciembre",
        ]
        return f"{weekdays[dt.weekday()]} {dt.day:02d} de {months[dt.month - 1]}"

    def render_weather(self, dimensions, weather, settings, tz):
        width, height = dimensions
        bg = ImageColor.getrgb(settings.get("backgroundColor") or "#111820")
        panel = (31, 45, 57)
        panel_soft = (36, 53, 67)
        ink = ImageColor.getrgb(settings.get("textColor") or "#f4f7fb")
        muted = (163, 176, 188)
        grid = (86, 105, 121)
        accent = ImageColor.getrgb(settings.get("accentColor") or "#ff5a52")
        rain_blue = (73, 139, 202)
        sun_gold = (177, 139, 31)
        image = Image.new("RGB", dimensions, bg)
        draw = ImageDraw.Draw(image)

        margin = max(int(min(width, height) * 0.035), 10)
        gap = max(int(min(width, height) * 0.018), 6)
        title_font = self.fit_font(weather["title"], width * 0.70, max(int(height * 0.065), 20), "bold")
        date_font = get_font("Jost", max(int(height * 0.03), 12), "bold")
        temp_font = get_font("Jost", max(int(height * 0.14), 42), "bold")
        metric_font = get_font("Jost", max(int(height * 0.034), 14), "bold")
        small_font = get_font("Jost", max(int(height * 0.024), 10), "bold")

        draw.text((margin, margin), weather["title"], anchor="lt", fill=ink, font=title_font)
        draw.text((margin, margin + title_font.size * 1.05), weather["location"], anchor="lt", fill=muted, font=date_font)

        self.draw_battery_indicator(draw, weather.get("battery"), (width - margin, margin), small_font, ink, muted)

        if settings.get("displayRefreshTime", "true") == "true":
            status_y = margin + small_font.size * 1.75 if weather.get("battery") else margin
            draw.text(
                (width - margin, status_y),
                weather["updated"].strftime("%H:%M"),
                anchor="rt",
                fill=muted,
                font=small_font,
            )

        content_top = margin + title_font.size + date_font.size + gap * 2
        forecast_height = max(int(height * 0.22), 82)
        chart_bottom = height - margin - forecast_height - gap
        current_width = int(width * 0.25)

        current_box = (margin, content_top, margin + current_width, chart_bottom)
        chart_box = (margin + current_width + gap, content_top, width - margin, chart_bottom)
        forecast_box = (margin, chart_bottom + gap, width - margin, height - margin * 2)

        if width < height:
            current_box = (margin, content_top, width - margin, content_top + int(height * 0.28))
            chart_box = (margin, current_box[3] + gap, width - margin, chart_bottom)
            forecast_box = (margin, chart_bottom + gap, width - margin, height - margin * 2)

        self.draw_current_dark(draw, image, weather, current_box, temp_font, metric_font, small_font, ink, muted, panel, accent, rain_blue)
        self.draw_forecast_charts(draw, image, weather["hourly"], chart_box, small_font, ink, muted, grid, rain_blue, sun_gold)
        self.draw_forecast_dark(draw, image, weather["forecast"][1:], forecast_box, metric_font, small_font, ink, muted, panel, rain_blue)
        return image

    def draw_current_dark(self, draw, image, weather, box, temp_font, metric_font, small_font, ink, muted, panel, accent, rain_blue):
        left, top, right, bottom = box
        width = right - left
        height = bottom - top
        radius = 8
        draw.rounded_rectangle(box, radius=radius, fill=panel, outline=(55, 73, 89), width=1)

        icon_size = int(min(width * 0.54, height * 0.30))
        self.paste_icon(image, weather["current_icon"], (int(left + width * 0.10), int(top + height * 0.09)), icon_size)

        temp = self.format_number(weather["current_temperature"])
        temp_text = f"{temp} C"
        temp_draw_font = self.fit_font(temp_text, width * 0.82, min(temp_font.size, int(height * 0.28)), "bold")
        draw.text((left + width * 0.10, top + height * 0.38), temp_text, anchor="la", fill=ink, font=temp_draw_font)

        metrics_x = left + width * 0.10
        metrics_y = top + height * 0.66
        gust = self.format_number(weather["current_gust"], decimals=0)
        pop = self.format_number(weather["current_pop"], decimals=0)
        high = self.format_number(weather.get("current_high"), decimals=0)
        low = self.format_number(weather.get("current_low"), decimals=0)
        rain_range = self.format_precip_range(weather.get("current_precip_low"), weather.get("current_precip_high"))
        rain = rain_range or f"{self.format_number(weather['current_precip'], decimals=1)} mm"

        draw.text((metrics_x, metrics_y), f"Lluvia {rain}", anchor="la", fill=rain_blue, font=metric_font)
        line_gap = metric_font.size * 1.35
        temp_range = f"{high}/{low} C" if high != "-" and low != "-" else "-"
        draw.text((metrics_x, metrics_y + line_gap), temp_range, anchor="la", fill=ink, font=small_font)
        if gust != "-":
            draw.text((metrics_x, metrics_y + line_gap * 1.85), f"Racha {gust} km/h", anchor="la", fill=muted, font=small_font)
        if pop != "-":
            draw.text((metrics_x, metrics_y + line_gap * 2.7), f"Prob. lluvia {pop}%", anchor="la", fill=muted, font=small_font)

    def draw_battery_indicator(self, draw, battery, anchor, font, ink, muted):
        if not battery:
            return

        right, top = anchor
        percent = battery.get("percent")
        if percent is None:
            return

        color = self.battery_color(battery, ink)
        label = f"{int(percent)}%"
        label_width = int(font.getlength(label))
        body_w = max(int(font.size * 1.8), 22)
        body_h = max(int(font.size * 0.95), 10)
        nub_w = max(int(body_w * 0.12), 3)
        gap = max(int(font.size * 0.35), 4)
        total_w = body_w + nub_w + gap + label_width
        left = right - total_w
        y = top + max(int(font.size * 0.12), 1)
        body = (left, y, left + body_w, y + body_h)
        nub = (left + body_w, y + body_h * 0.30, left + body_w + nub_w, y + body_h * 0.70)

        draw.rounded_rectangle(body, radius=2, outline=color, width=2)
        draw.rectangle(nub, fill=color)
        fill_pad = 3
        fill_w = max(int((body_w - fill_pad * 2) * max(0, min(100, percent)) / 100), 1)
        draw.rectangle(
            (left + fill_pad, y + fill_pad, left + fill_pad + fill_w, y + body_h - fill_pad),
            fill=color,
        )
        if battery.get("charging"):
            draw.text((left + body_w / 2, y + body_h / 2), "+", anchor="mm", fill=ink, font=font)
        draw.text((left + body_w + nub_w + gap, y + body_h / 2), label, anchor="lm", fill=color, font=font)

    def draw_weather_strip(self, draw, image, hourly, box, font, ink, muted, panel, panel_soft):
        if not hourly:
            return

        left, top, right, bottom = box
        draw.rounded_rectangle(box, radius=8, fill=panel, outline=(55, 73, 89), width=1)
        sample = self.hourly_samples(hourly)
        if not sample:
            return

        cell_width = (right - left) / len(sample)
        icon_size = int(min((bottom - top) * 0.48, cell_width * 0.46))
        for index, hour in enumerate(sample):
            cx = left + cell_width * (index + 0.5)
            if self.starts_new_day(sample, index):
                separator_x = left + cell_width * index
                draw.line((separator_x, top + 6, separator_x, bottom - 6), fill=(86, 105, 121), width=1)
            self.paste_icon(image, hour["icon"], (int(cx - icon_size / 2), int(top + (bottom - top) * 0.08)), icon_size)
            time_label = hour["time"].strftime("%H:00")
            draw.text((cx, bottom - font.size * 1.45), time_label, anchor="mm", fill=muted, font=font)
            temp = self.format_number(hour.get("temperature"))
            temp_label = f"{temp} C" if temp != "-" else "-"
            draw.text((cx, bottom - font.size * 0.52), temp_label, anchor="mm", fill=ink, font=font)

    def draw_forecast_charts(self, draw, image, hourly, box, font, ink, muted, grid, rain_blue, sun_gold):
        if not hourly:
            return

        samples = hourly[:16]
        if not samples:
            return

        left, top, right, bottom = box
        draw.rounded_rectangle(box, radius=8, fill=(25, 37, 49), outline=(59, 77, 94), width=1)

        width = right - left
        height = bottom - top
        pad = max(int(min(width, height) * 0.025), 6)
        label_width = max(int(width * 0.075), 34)
        right_axis_width = max(int(width * 0.045), 20)
        plot_left = left + label_width
        plot_right = right - pad - right_axis_width
        plot_width = max(plot_right - plot_left, 1)
        cell_width = plot_width / len(samples)

        icon_row_h = max(int(height * 0.14), font.size * 2.8)
        chart_top = top + pad + icon_row_h + font.size * 0.65
        plot_bottom = bottom - pad
        gap = 0
        chart_height = max(plot_bottom - chart_top - gap * 2, 1)
        band_h = chart_height / 3
        precip_top = chart_top
        sun_top = precip_top + band_h + gap
        wind_top = sun_top + band_h + gap

        icon_samples = samples[::2]
        icon_cell_width = plot_width / max(len(icon_samples), 1)
        icon_size = int(min(icon_row_h * 0.58, icon_cell_width * 0.48))
        for index, hour in enumerate(icon_samples):
            sample_index = index * 2
            cx = plot_left + cell_width * (sample_index + 0.5)
            self.paste_icon(image, hour["icon"], (int(cx - icon_size / 2), int(top + pad)), icon_size)
            pop = self.format_number(hour.get("pop"), decimals=0)
            pop_text = f"{pop}%" if pop != "-" else "-"
            draw.text((cx, top + pad + icon_size + font.size * 0.65), pop_text, anchor="mm", fill=rain_blue, font=font)

        for band_top in (precip_top, sun_top, wind_top):
            draw.line((left + pad, band_top, right - pad, band_top), fill=grid, width=1)

        self.draw_time_axis(draw, samples, plot_left, plot_right, chart_top - font.size * 0.6, font, muted)
        self.draw_time_grid(draw, samples, plot_left, cell_width, precip_top, precip_top + band_h, grid)
        self.draw_time_grid(draw, samples, plot_left, cell_width, sun_top, sun_top + band_h, grid)
        self.draw_time_grid(draw, samples, plot_left, cell_width, wind_top, wind_top + band_h, grid)

        temps = [float(hour.get("temperature") or 0) for hour in samples if hour.get("temperature") is not None]
        min_temp = math.floor(min(temps) - 1) if temps else 0
        max_temp = math.ceil(max(temps) + 1) if temps else 1
        if max_temp <= min_temp:
            max_temp = min_temp + 1

        max_rain = max([float(hour.get("precip") or 0) for hour in samples] + [0.5])
        max_rain_axis = max(1.0, math.ceil(max_rain))
        rain_base = precip_top + band_h
        bar_width = max(int(cell_width * 0.58), 3)
        temp_points = []
        temp_axis_values = [min_temp, (min_temp + max_temp) / 2, max_temp]
        temp_plot_top = precip_top + band_h * 0.12
        temp_base = precip_top + band_h * 0.82
        rain_base = precip_top + band_h
        self.draw_horizontal_grid(draw, plot_left, plot_right, temp_plot_top, temp_base, temp_axis_values)
        self.draw_y_axis(draw, plot_left, temp_plot_top, temp_base, temp_axis_values, font, muted, side="left", color=muted, suffix="")
        rain_axis_top = precip_top + font.size * 0.35
        rain_axis_bottom = rain_base - font.size * 0.35
        self.draw_y_axis(draw, plot_right, rain_axis_top, rain_axis_bottom, [0, max_rain_axis / 2, max_rain_axis], font, muted, side="right", color=rain_blue, suffix="")
        for index, hour in enumerate(samples):
            cx = plot_left + cell_width * (index + 0.5)
            rain = float(hour.get("precip") or 0)
            if rain > 0:
                rain_top = rain_base - band_h * 0.78 * rain / max_rain_axis
                draw.rectangle((cx - bar_width / 2, rain_top, cx + bar_width / 2, rain_base), fill=rain_blue)
            else:
                zero_y = rain_base - 2
                draw.line((cx - bar_width / 2, zero_y, cx + bar_width / 2, zero_y), fill=(96, 174, 238), width=1)

            temp = hour.get("temperature")
            if temp is not None:
                ty = temp_base - (temp_base - temp_plot_top) * (float(temp) - min_temp) / (max_temp - min_temp)
                temp_points.append((cx, ty))

        if len(temp_points) > 1:
            draw.line(temp_points, fill=(255, 86, 94), width=max(3, int(font.size * 0.28)))
        draw.text((plot_right, precip_top + band_h - font.size * 0.25), "mm/h", anchor="rb", fill=rain_blue, font=font)

        max_sun = 60.0
        sun_base = sun_top + band_h
        self.draw_horizontal_grid(draw, plot_left, plot_right, sun_top, sun_top + band_h, [0, 30, 60])
        for index, hour in enumerate(samples):
            cx = plot_left + cell_width * (index + 0.5)
            sun = min(float(hour.get("sunshine") or 0), max_sun)
            bar_top = sun_base - band_h * 0.78 * sun / max_sun
            draw.rectangle((cx - bar_width / 2, bar_top, cx + bar_width / 2, sun_base), fill=(232, 198, 42))

        wind_plot_top = wind_top + band_h * 0.12
        wind_base = wind_top + band_h * 0.88
        wind_values = [float(hour.get("wind") or 0) for hour in samples]
        gust_values = [float(hour.get("gust") or 0) for hour in samples]
        max_wind = max(gust_values + wind_values + [10.0])
        max_wind_axis = max(10, int(math.ceil(max_wind / 10.0) * 10))
        wind_points = []
        gust_points = []
        wind_axis_values = [0, max_wind_axis / 2, max_wind_axis]
        self.draw_horizontal_grid(draw, plot_left, plot_right, wind_plot_top, wind_base, wind_axis_values)
        self.draw_y_axis(draw, plot_left, wind_plot_top, wind_base, wind_axis_values, font, muted, side="left", color=muted, suffix="")

        for index, hour in enumerate(samples):
            cx = plot_left + cell_width * (index + 0.5)
            wind_y = wind_base - (wind_base - wind_plot_top) * float(hour.get("wind") or 0) / max_wind_axis
            gust_y = wind_base - (wind_base - wind_plot_top) * float(hour.get("gust") or 0) / max_wind_axis
            wind_points.append((cx, wind_y))
            gust_points.append((cx, gust_y))

        if len(gust_points) > 1:
            polygon = [(plot_left + cell_width * 0.5, wind_base)] + gust_points + [(plot_left + cell_width * (len(samples) - 0.5), wind_base)]
            draw.polygon(polygon, fill=(55, 58, 92))
            draw.line(gust_points, fill=(150, 139, 255), width=max(2, int(font.size * 0.16)))
        if len(wind_points) > 1:
            draw.line(wind_points, fill=(212, 139, 237), width=max(2, int(font.size * 0.14)))

    @staticmethod
    def draw_time_axis(draw, samples, plot_left, plot_right, y, font, muted):
        if not samples:
            return
        cell_width = (plot_right - plot_left) / len(samples)
        for index, hour in enumerate(samples):
            if index % 2 != 0:
                continue
            if index >= len(samples) - 1:
                continue
            cx = plot_left + cell_width * (index + 0.5)
            draw.text((cx, y), hour["time"].strftime("%H:00"), anchor="mm", fill=muted, font=font)

    @staticmethod
    def draw_time_grid(draw, samples, plot_left, cell_width, top, bottom, grid):
        for index in range(len(samples)):
            if index % 2 != 0:
                continue
            x = plot_left + cell_width * (index + 0.5)
            draw.line((x, top + 2, x, bottom - 2), fill=(58, 75, 91), width=1)

    @staticmethod
    def draw_horizontal_grid(draw, left, right, top, bottom, values):
        if not values:
            return
        v_min = min(values)
        v_max = max(values)
        if v_max <= v_min:
            v_max = v_min + 1
        for value in values:
            y = bottom - (bottom - top) * (float(value) - v_min) / (v_max - v_min)
            draw.line((left, y, right, y), fill=(49, 64, 78), width=1)

    @staticmethod
    def draw_y_axis(draw, axis_x, top, bottom, values, font, muted, side="left", color=None, suffix=""):
        if not values:
            return
        color = color or muted
        v_min = min(values)
        v_max = max(values)
        if v_max <= v_min:
            v_max = v_min + 1
        for value in values:
            y = bottom - (bottom - top) * (float(value) - v_min) / (v_max - v_min)
            label = str(int(round(value))) if float(value).is_integer() else f"{value:.1f}"
            if suffix:
                label = f"{label}{suffix}"
            if side == "right":
                draw.line((axis_x, y, axis_x + 3, y), fill=color, width=1)
                draw.text((axis_x + 5, y), label, anchor="lm", fill=color, font=font)
            else:
                draw.line((axis_x - 3, y, axis_x, y), fill=color, width=1)
                draw.text((axis_x - 5, y), label, anchor="rm", fill=color, font=font)

    @staticmethod
    def draw_day_separators(draw, samples, plot_left, cell_width, top, bottom, grid):
        for index in range(1, len(samples)):
            if samples[index]["time"].date() == samples[index - 1]["time"].date():
                continue
            separator_x = plot_left + cell_width * index
            draw.line((separator_x, top, separator_x, bottom), fill=grid, width=1)

    def draw_metric_tables(self, draw, hourly, box, font, ink, muted, grid, rain_blue, sun_gold):
        if not hourly:
            return

        left, top, right, bottom = box
        draw.rounded_rectangle(box, radius=8, fill=(25, 37, 49), outline=(59, 77, 94), width=1)

        samples = self.metric_samples(hourly)
        if not samples:
            return

        labels_width = max(int((right - left) * 0.15), 52)
        plot_left = left + labels_width
        plot_right = right - 10
        header_height = max(font.size * 1.8, 18)
        plot_top = top + header_height
        plot_bottom = bottom - 8
        plot_height = max(plot_bottom - plot_top, 1)
        headers_y = top + header_height * 0.52

        max_sun = 120.0
        max_gust = max([float(h.get("gust") or 0) for h in samples] + [1.0])
        max_rain = max([float(h.get("precip") or 0) for h in samples] + [1.0])
        cell_width = (plot_right - plot_left) / len(samples)

        band_gap = max(int(plot_height * 0.04), 3)
        band_height = (plot_height - band_gap * 2) / 3
        sun_top = plot_top
        gust_top = sun_top + band_height + band_gap
        rain_top = gust_top + band_height + band_gap

        draw.text((left + 10, sun_top + band_height * 0.50), "Sol", anchor="lm", fill=ink, font=font)
        draw.text((left + 10, gust_top + band_height * 0.50), "Rachas", anchor="lm", fill=ink, font=self.fit_font("Rachas", labels_width - 16, font.size, "bold"))
        draw.text((left + 10, rain_top + band_height * 0.50), "Lluvia", anchor="lm", fill=ink, font=font)

        for y in (sun_top, gust_top, rain_top):
            draw.line((left + 8, y, right - 8, y), fill=grid, width=1)

        for index, hour in enumerate(samples):
            cx = plot_left + cell_width * (index + 0.5)
            draw.text((cx, headers_y), hour["time"].strftime("%H:00"), anchor="mm", fill=muted, font=font)
            if self.starts_new_day(samples, index):
                separator_x = plot_left + cell_width * index
                draw.line((separator_x, top + 6, separator_x, bottom - 6), fill=grid, width=1)

            bar_width = max(int(cell_width * 0.26), 3)
            sun = min(float(hour.get("sunshine") or 0), max_sun)
            sun_base = sun_top + band_height * 0.58
            sun_bar_top = sun_base - band_height * 0.34 * sun / max_sun
            draw.rectangle((cx - bar_width, sun_bar_top, cx + bar_width, sun_base), fill=sun_gold)
            draw.text((cx, sun_top + band_height * 0.78), f"{int(round(sun))} min", anchor="mm", fill=muted, font=font)

            gust = float(hour.get("gust") or 0)
            gust_base = gust_top + band_height * 0.58
            gust_bar_top = gust_base - band_height * 0.34 * gust / max_gust
            draw.rectangle((cx - bar_width, gust_bar_top, cx + bar_width, gust_base), fill=(230, 238, 245))
            draw.text((cx, gust_top + band_height * 0.78), f"{int(round(gust))} km/h", anchor="mm", fill=muted, font=font)

            rain = float(hour.get("precip") or 0)
            rain_base = rain_top + band_height * 0.62
            rain_bar_top = rain_base - band_height * 0.42 * rain / max_rain
            draw.rectangle((cx - bar_width, rain_bar_top, cx + bar_width, rain_base), fill=rain_blue)
            rain_text = self.format_hourly_metric_value(hour, rain, "precip")
            draw.text((cx, rain_top + band_height * 0.84), rain_text, anchor="mm", fill=muted, font=font)

    def draw_forecast_dark(self, draw, image, forecast, box, body_font, small_font, ink, muted, panel, rain_blue):
        if not forecast:
            return
        left, top, right, bottom = box
        gap = 6
        day_count = min(len(forecast), 7)
        while day_count > 3 and (right - left - gap * (day_count - 1)) / day_count < 64:
            day_count -= 1

        days = forecast[:day_count]
        card_width = (right - left - gap * (len(days) - 1)) / len(days)
        for index, day in enumerate(days):
            x1 = left + index * (card_width + gap)
            x2 = x1 + card_width
            day_font = get_font("Jost", min(body_font.size, max(int(card_width * 0.18), 12)))
            value_font = get_font("Jost", min(small_font.size, max(int(card_width * 0.13), 10)), "bold")
            rain_font = get_font("Jost", min(small_font.size + 1, max(int(card_width * 0.14), 11)), "bold")
            draw.rounded_rectangle((x1, top, x2, bottom), radius=8, fill=panel, outline=(55, 73, 89), width=1)
            draw.text(((x1 + x2) / 2, top + 8), day["day"], anchor="mt", fill=ink, font=day_font)
            icon_size = int((bottom - top) * 0.34)
            self.paste_icon(image, day["icon"], (int((x1 + x2 - icon_size) / 2), int(top + (bottom - top) * 0.31)), icon_size)
            high = self.format_number(day["high"])
            low = self.format_number(day["low"])
            rain = self.format_number(day["precip"], decimals=1)
            draw.text(((x1 + x2) / 2, bottom - value_font.size * 2.4), f"{high}/{low} C", anchor="mm", fill=ink, font=value_font)
            draw.text(((x1 + x2) / 2, bottom - rain_font.size * 0.95), f"{rain} mm", anchor="mm", fill=rain_blue, font=rain_font)

    @staticmethod
    def paste_icon(image, path, position, size):
        try:
            with Image.open(path) as icon:
                icon = icon.convert("RGBA")
                icon.thumbnail((size, size), Image.LANCZOS)
                image.paste(icon, position, icon)
        except Exception as exc:
            logger.warning("Failed to paste icon %s: %s", path, exc)

    @staticmethod
    def format_number(value, decimals=0):
        if value is None:
            return "-"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return str(value)
        if decimals == 0:
            return str(int(round(value)))
        return f"{value:.{decimals}f}"

    @staticmethod
    def format_hourly_metric_value(hour, value, key):
        if key == "precip":
            precip_range = MeteoSwissWeather.format_precip_range(hour.get("precip_low"), hour.get("precip_high"))
            if precip_range:
                return precip_range
            return f"{value:.1f} mm"
        return str(int(round(value)))

    @staticmethod
    def hourly_samples(hourly):
        return hourly[:16:2]

    @staticmethod
    def metric_samples(hourly):
        samples = []
        for index in range(0, min(len(hourly), 16), 2):
            window = hourly[index : index + 2]
            if not window:
                continue
            sample = dict(window[0])
            sample["sunshine"] = sum(float(hour.get("sunshine") or 0) for hour in window)
            sample["gust"] = max(float(hour.get("gust") or 0) for hour in window)
            sample["precip"] = sum(float(hour.get("precip") or 0) for hour in window)
            if any(hour.get("precip_low") is not None for hour in window):
                sample["precip_low"] = sum(float(hour.get("precip_low") or 0) for hour in window)
            if any(hour.get("precip_high") is not None for hour in window):
                sample["precip_high"] = sum(float(hour.get("precip_high") or 0) for hour in window)
            samples.append(sample)
        return samples

    @staticmethod
    def format_precip_range(low, high):
        if low is None or high is None:
            return ""
        try:
            low_value = int(round(float(low)))
            high_value = int(round(float(high)))
        except (TypeError, ValueError):
            return ""
        return f"{low_value}-{high_value} mm"

    @staticmethod
    def safe_float(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def estimate_lipo_percent(vin):
        voltage_curve = [
            (4.20, 100),
            (4.10, 90),
            (4.00, 75),
            (3.90, 58),
            (3.80, 40),
            (3.70, 22),
            (3.60, 10),
            (3.50, 3),
            (3.40, 0),
        ]
        if vin >= voltage_curve[0][0]:
            return voltage_curve[0][1]
        for index in range(1, len(voltage_curve)):
            high_v, high_pct = voltage_curve[index - 1]
            low_v, low_pct = voltage_curve[index]
            if vin >= low_v:
                span = high_v - low_v
                if span <= 0:
                    return low_pct
                ratio = (vin - low_v) / span
                return low_pct + ratio * (high_pct - low_pct)
        return 0

    @staticmethod
    def format_battery_label(battery, compact=False):
        if not battery:
            return ""
        percent = battery.get("percent")
        vin = battery.get("vin")
        if percent is None or vin is None:
            return ""
        prefix = "Bat"
        charging = " +" if battery.get("charging") else ""
        if compact:
            return f"{prefix} {int(percent)}%{charging}"
        return f"{prefix} {int(percent)}% · {vin:.2f}V{charging}"

    @staticmethod
    def battery_color(battery, default_color):
        if not battery:
            return default_color
        percent = battery.get("percent")
        if percent is None:
            return default_color
        if percent <= 15:
            return (255, 112, 112)
        if percent <= 30:
            return (232, 198, 42)
        return default_color

    @staticmethod
    def starts_new_day(samples, index):
        if index <= 0:
            return False
        return samples[index]["time"].date() != samples[index - 1]["time"].date()

    @staticmethod
    def wind_arrow(degrees):
        if degrees is None:
            return ""
        arrows = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
        try:
            index = int(((float(degrees) % 360) + 22.5) // 45) % 8
            return arrows[index]
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def fit_font(text, max_width, start_size, weight="normal"):
        font_size = start_size
        while font_size > 10:
            font = get_font("Jost", font_size, weight)
            if font.getlength(text) <= max_width:
                return font
            font_size -= 2
        return get_font("Jost", font_size, weight)

    def cache_dir(self):
        path = Path(self.get_plugin_dir("cache"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_cache(self):
        cache_dir = self.cache_dir()
        now = datetime.now().timestamp()
        protected_files = {META_POINT_ASSET}

        for path in cache_dir.iterdir():
            if not path.is_file() or path.name in protected_files:
                continue

            age = now - path.stat().st_mtime
            if age < CACHE_RETENTION_SECONDS:
                continue

            try:
                path.unlink()
                logger.info("Deleted old MeteoSwiss cache file: %s", path)
            except OSError as exc:
                logger.warning("Failed to delete old MeteoSwiss cache file %s: %s", path, exc)

    @staticmethod
    def is_fresh(path, ttl_seconds):
        if not path.exists():
            return False
        age = datetime.now().timestamp() - path.stat().st_mtime
        return age < ttl_seconds

    @staticmethod
    def haversine_km(lat1, lon1, lat2, lon2):
        radius = 6371.0
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
