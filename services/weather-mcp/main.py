"""Self-hosted weather MCP server (Open-Meteo, no API key required).

Serves as the template for adding new skills to the voice assistant:
tools defined here are automatically exposed to the agent over MCP.

Transport: streamable HTTP at  http://<host>:<port>/mcp
Tools:
    get_current_weather(location, units)
    get_weather_forecast(location, days, units)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("weather-mcp")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes (https://open-meteo.com/en/docs)
WEATHER_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

DEFAULT_LOCATION = os.getenv("WEATHER_DEFAULT_LOCATION", "")
DEFAULT_UNITS = os.getenv("WEATHER_DEFAULT_UNITS", "metric")

mcp = FastMCP(
    "weather",
    instructions=(
        "Weather lookups via Open-Meteo. Use get_current_weather for current "
        "conditions and get_weather_forecast for multi-day forecasts."
    ),
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8100")),
)

_geocode_cache: dict[str, dict[str, Any] | None] = {}


async def _geocode(client: httpx.AsyncClient, location: str) -> dict[str, Any]:
    if location in _geocode_cache:
        result = _geocode_cache[location]
        if result is None:
            raise ValueError(f"Location '{location}' was not found.")
        return result

    resp = await client.get(
        GEOCODE_URL,
        params={"name": location, "count": 1, "language": "en", "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        _geocode_cache[location] = None
        raise ValueError(f"Location '{location}' was not found.")

    top = results[0]
    place: dict[str, Any] = {
        "name": top.get("name", location),
        "latitude": top["latitude"],
        "longitude": top["longitude"],
        "country": top.get("country", ""),
        "timezone": top.get("timezone", "auto"),
    }
    _geocode_cache[location] = place
    return place


def _unit_params(units: str) -> dict[str, str]:
    units = (units or "").strip().lower() or DEFAULT_UNITS
    if units in {"imperial", "fahrenheit", "mph"}:
        return {
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        }
    return {
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }


def _unit_names(units: str) -> tuple[str, str]:
    if _unit_params(units).get("temperature_unit") == "fahrenheit":
        return ("°F", "mph")
    return ("°C", "km/h")


def _describe(code: Any) -> str:
    try:
        return WEATHER_CODES.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


@mcp.tool()
async def get_current_weather(location: str = "", units: str = "") -> str:
    """Get the current weather for a city.

    Args:
        location: city name, e.g. "Berlin" or "San Francisco". Empty uses the
            home location configured for this assistant.
        units: "metric" or "imperial". Empty uses the configured default.
    """
    place_name = location.strip() or DEFAULT_LOCATION
    if not place_name:
        return "No location given and no default location is configured. Ask the user which city they mean."

    async with httpx.AsyncClient() as client:
        try:
            place = await _geocode(client, place_name)
            resp = await client.get(
                FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": (
                        "temperature_2m,relative_humidity_2m,apparent_temperature,"
                        "weather_code,wind_speed_10m"
                    ),
                    "timezone": place["timezone"],
                    **_unit_params(units),
                },
                timeout=10,
            )
            resp.raise_for_status()
        except ValueError as exc:
            return str(exc)
        except httpx.HTTPError:
            logger.exception("weather lookup failed")
            return "Weather lookup failed. The service may be unreachable."

    current = resp.json().get("current", {})
    deg, spd = _unit_names(units)
    return (
        f"Current weather in {place['name']}"
        + (f", {place['country']}" if place["country"] else "")
        + f": {_describe(current.get('weather_code'))}, "
        f"{current.get('temperature_2m')}{deg}, "
        f"feels like {current.get('apparent_temperature')}{deg}, "
        f"humidity {current.get('relative_humidity_2m')}%, "
        f"wind {current.get('wind_speed_10m')} {spd}."
    )


@mcp.tool()
async def get_weather_forecast(
    location: str = "", days: int = 3, units: str = ""
) -> str:
    """Get a daily weather forecast for a city.

    Args:
        location: city name. Empty uses the home location.
        days: number of days (1-7).
        units: "metric" or "imperial". Empty uses the configured default.
    """
    place_name = location.strip() or DEFAULT_LOCATION
    if not place_name:
        return "No location given and no default location is configured. Ask the user which city they mean."
    days = max(1, min(int(days or 3), 7))

    async with httpx.AsyncClient() as client:
        try:
            place = await _geocode(client, place_name)
            resp = await client.get(
                FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "daily": (
                        "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max"
                    ),
                    "forecast_days": days,
                    "timezone": place["timezone"],
                    **_unit_params(units),
                },
                timeout=10,
            )
            resp.raise_for_status()
        except ValueError as exc:
            return str(exc)
        except httpx.HTTPError:
            logger.exception("forecast lookup failed")
            return "Forecast lookup failed. The service may be unreachable."

    daily = resp.json().get("daily", {})
    deg, _ = _unit_names(units)
    lines: list[str] = [
        f"{days}-day forecast for {place['name']}"
        + (f", {place['country']}" if place["country"] else "")
        + ":"
    ]
    for i, date in enumerate(daily.get("time", [])):
        lines.append(
            f"- {date}: {_describe(daily['weather_code'][i])}, "
            f"{daily['temperature_2m_min'][i]} to {daily['temperature_2m_max'][i]}{deg}, "
            f"precipitation chance "
            f"{daily.get('precipitation_probability_max', ['?'])[i]}%"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="streamable-http")

