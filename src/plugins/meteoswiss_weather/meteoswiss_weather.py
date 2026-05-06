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
            weather = self.load_weather(settings, tz)
        except Exception as exc:
            logger.exception("MeteoSwiss weather failed: %s", exc)
            raise RuntimeError("MeteoSwiss weather request failure, please check logs.")

        return self.render_weather(dimensions, weather, settings, tz)

    def load_weather(self, settings, tz):
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
        forecast = self.merge_daily(rows, now.date(), int(settings.get("forecastDays") or 5))

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
            "hourly": hourly,
            "forecast": forecast,
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

        path = self.download_file(asset["href"], f"{item['item_id']}_{parameter}.csv")
        return self.cached_filtered_parameter(path, item["item_id"], parameter, point_id, point_type_id, tz)

    def cached_filtered_parameter(self, csv_path, item_id, parameter, point_id, point_type_id, tz):
        filtered_path = self.cache_dir() / f"{item_id}_{parameter}_{point_type_id}_{point_id}.json"
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
        for name, asset in item.get("assets", {}).items():
            if name.endswith(suffix):
                return asset
        return None

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
            if time < now - timedelta(hours=1):
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

        if settings.get("displayRefreshTime", "true") == "true":
            draw.text(
                (width - margin, margin),
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
        strip_box = (margin + current_width + gap, content_top, width - margin, content_top + int(height * 0.135))
        chart_box = (margin + current_width + gap, strip_box[3] + gap, width - margin, chart_bottom)
        forecast_box = (margin, chart_bottom + gap, width - margin, height - margin * 2)

        if width < height:
            current_box = (margin, content_top, width - margin, content_top + int(height * 0.28))
            strip_box = (margin, current_box[3] + gap, width - margin, current_box[3] + gap + int(height * 0.12))
            chart_box = (margin, strip_box[3] + gap, width - margin, chart_bottom)
            forecast_box = (margin, chart_bottom + gap, width - margin, height - margin * 2)

        self.draw_current_dark(draw, image, weather, current_box, temp_font, metric_font, small_font, ink, muted, panel, accent, rain_blue)
        self.draw_weather_strip(draw, image, weather["hourly"], strip_box, small_font, ink, muted, panel, panel_soft)
        self.draw_metric_tables(draw, weather["hourly"], chart_box, small_font, ink, muted, grid, rain_blue, sun_gold)
        self.draw_forecast_dark(draw, image, weather["forecast"][1:], forecast_box, metric_font, small_font, ink, muted, panel, accent)
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
        rain = self.format_number(weather["current_precip"], decimals=1)
        wind = self.format_number(weather["current_wind"], decimals=0)
        gust = self.format_number(weather["current_gust"], decimals=0)
        pop = self.format_number(weather["current_pop"], decimals=0)
        wind_arrow = self.wind_arrow(weather["current_wind_dir"])

        draw.text((metrics_x, metrics_y), f"Lluvia {rain} mm", anchor="la", fill=rain_blue, font=metric_font)
        wind_text = f"Viento {wind} km/h {wind_arrow}" if wind != "-" else "Viento -"
        line_gap = metric_font.size * 1.35
        draw.text((metrics_x, metrics_y + line_gap), wind_text, anchor="la", fill=ink, font=small_font)
        if gust != "-":
            draw.text((metrics_x, metrics_y + line_gap * 1.85), f"Racha {gust} km/h", anchor="la", fill=muted, font=small_font)
        if pop != "-":
            draw.text((metrics_x, metrics_y + line_gap * 2.7), f"Prob. lluvia {pop}%", anchor="la", fill=muted, font=small_font)

    def draw_weather_strip(self, draw, image, hourly, box, font, ink, muted, panel, panel_soft):
        if not hourly:
            return

        left, top, right, bottom = box
        draw.rounded_rectangle(box, radius=8, fill=panel, outline=(55, 73, 89), width=1)
        sample = hourly[:24:3]
        if not sample:
            return

        cell_width = (right - left) / len(sample)
        icon_size = int(min((bottom - top) * 0.50, cell_width * 0.46))
        for index, hour in enumerate(sample):
            cx = left + cell_width * (index + 0.5)
            if self.starts_new_day(sample, index):
                separator_x = left + cell_width * index
                draw.line((separator_x, top + 6, separator_x, bottom - 6), fill=(86, 105, 121), width=1)
            self.paste_icon(image, hour["icon"], (int(cx - icon_size / 2), int(top + (bottom - top) * 0.14)), icon_size)
            time_label = hour["time"].strftime("%H:00")
            draw.text((cx, bottom - font.size * 0.92), time_label, anchor="mm", fill=muted, font=font)

    def draw_metric_tables(self, draw, hourly, box, font, ink, muted, grid, rain_blue, sun_gold):
        if not hourly:
            return

        left, top, right, bottom = box
        draw.rounded_rectangle(box, radius=8, fill=(25, 37, 49), outline=(59, 77, 94), width=1)

        samples = hourly[:24:3]
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

        max_sun = 60.0
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

    def draw_forecast_dark(self, draw, image, forecast, box, body_font, small_font, ink, muted, panel, accent):
        if not forecast:
            return
        left, top, right, bottom = box
        gap = 6
        day_count = min(len(forecast), 5)
        while day_count > 3 and (right - left - gap * (day_count - 1)) / day_count < 82:
            day_count -= 1

        days = forecast[:day_count]
        card_width = (right - left - gap * (len(days) - 1)) / len(days)
        for index, day in enumerate(days):
            x1 = left + index * (card_width + gap)
            x2 = x1 + card_width
            day_font = get_font("Jost", min(body_font.size, max(int(card_width * 0.18), 12)))
            value_font = get_font("Jost", min(small_font.size, max(int(card_width * 0.13), 10)))
            draw.rounded_rectangle((x1, top, x2, bottom), radius=8, fill=panel, outline=(55, 73, 89), width=1)
            draw.text(((x1 + x2) / 2, top + 8), day["day"], anchor="mt", fill=ink, font=day_font)
            icon_size = int((bottom - top) * 0.34)
            self.paste_icon(image, day["icon"], (int((x1 + x2 - icon_size) / 2), int(top + (bottom - top) * 0.31)), icon_size)
            high = self.format_number(day["high"])
            low = self.format_number(day["low"])
            rain = self.format_number(day["precip"], decimals=1)
            draw.text(((x1 + x2) / 2, bottom - value_font.size * 2.4), f"{high}/{low} C", anchor="mm", fill=ink, font=value_font)
            draw.text(((x1 + x2) / 2, bottom - value_font.size * 1.0), f"{rain} mm", anchor="mm", fill=accent, font=value_font)

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
