"""Five-day Open-Meteo forecast rendered as native JARVIS HUD cards."""
from __future__ import annotations

import requests


_WEATHER_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Dense drizzle", 56: "Freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
    67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Rain showers", 81: "Heavy showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}


def _forecast(city: str) -> dict:
    headers = {"User-Agent": "JARVIS Desktop Assistant/1.0"}
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        headers=headers, timeout=10,
    )
    geo.raise_for_status()
    places = geo.json().get("results") or []
    if not places:
        raise ValueError(f"No matching location found for {city}.")
    place = places[0]
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
            "forecast_days": 5, "timezone": "auto",
        },
        headers=headers, timeout=12,
    )
    response.raise_for_status()
    daily = response.json().get("daily", {})
    days = []
    count = len(daily.get("time", []))
    for i in range(count):
        code = daily.get("weather_code", [None] * count)[i]
        high = daily.get("temperature_2m_max", [None] * count)[i]
        low = daily.get("temperature_2m_min", [None] * count)[i]
        rain = daily.get("precipitation_probability_max", [None] * count)[i]
        wind = daily.get("wind_speed_10m_max", [None] * count)[i]
        extras = []
        if rain is not None:
            extras.append(f"Rain chance {rain}%")
        if wind is not None:
            extras.append(f"Wind up to {wind:g} km/h")
        high_text = f"{high:g}°C" if high is not None else "—"
        low_text = f"{low:g}°C" if low is not None else "—"
        days.append({
            "day": daily.get("time", [""] * count)[i],
            "summary": _WEATHER_CODES.get(code, "Forecast"),
            "detail": f"High {high_text} · low {low_text}" + (" · " + " · ".join(extras) if extras else ""),
        })
    return {"location": ", ".join(x for x in (place.get("name"), place.get("admin1"), place.get("country")) if x), "days": days}


def weather_action(parameters: dict, player=None, session_memory=None) -> str:
    city = str((parameters or {}).get("city", "")).strip()
    if not city:
        return "Please give me a city for the forecast."
    try:
        report = _forecast(city)
        if player and hasattr(player, "show_weather"):
            player.show_weather(report)
        days = report["days"]
        spoken = "; ".join(
            f"{day['day']}: {day['summary']}, {day['detail']}" for day in days[:3]
        )
        return f"Five-day forecast for {report['location']}. {spoken}"
    except Exception as exc:
        return f"I couldn't retrieve the HUD forecast for {city}: {exc}"


TOOL = {
    "name": "weather_report",
    "description": "Fetches a five-day weather forecast and shows it as cards inside the JARVIS HUD.",
    "parameters": {
        "type": "OBJECT",
        "properties": {"city": {"type": "STRING", "description": "City and optionally country, for example Colombo, Sri Lanka"}},
        "required": ["city"],
    },
    "handler": weather_action,
}
