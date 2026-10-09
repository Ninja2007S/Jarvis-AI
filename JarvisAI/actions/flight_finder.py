#flight_finder.py
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from config import is_windows, is_mac, is_linux

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

_MONTH_MAP: dict[str, int] = {

    "january": 1, "february": 2, "march": 3,     "april": 4,
    "may": 5,     "june": 6,     "july": 7,       "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# English fast-path only — Gemini (below) normalizes date expressions in ANY
# language to YYYY-MM-DD, so no other language needs to be hardcoded here.
_RELATIVE_MAP_KEYS = {
    "today",
    "tomorrow",
}


def _parse_date(raw: str) -> str:

    raw   = raw.strip()
    lower = raw.lower()
    today = datetime.now()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    relative = {
        "today": today,
        "tomorrow": today + timedelta(days=1),
    }
    for key, val in relative.items():
        if re.search(rf"\b{key}\b", lower):
            return val.strftime("%Y-%m-%d")

    try:
        from core import gemini
        result = gemini.text(
            f"Today is {today.strftime('%Y-%m-%d')}. "
            f"Convert this date expression to YYYY-MM-DD: '{raw}'. "
            f"Return ONLY the date string, nothing else.",
            tier=gemini.FAST,
        )
        parsed = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", str(result or ""))
        if parsed:
            return datetime.strptime(parsed.group(1), "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Gemini date parse failed: {e}")

    for month_name, month_num in _MONTH_MAP.items():
        if month_name in lower:
            day_match = re.search(r"\d{1,2}", raw)
            if day_match:
                day  = int(day_match.group())
                year = today.year if month_num >= today.month else today.year + 1
                try:
                    return datetime(year, month_num, day).strftime("%Y-%m-%d")
                except ValueError:
                    break

    raise ValueError(
        f"I couldn't interpret '{raw}' as a valid travel date. "
        "Please say a date like 2026-10-20."
    )

def _build_google_flights_url(
    origin:      str,
    destination: str,
    date:        str,
    return_date: str | None = None,
    passengers:  int        = 1,
    cabin:       str        = "economy",
) -> str:
    base = "https://www.google.com/travel/flights"

    # Google Flights accepts a natural-language q query. Keep all itinerary
    # settings in that query: separate guessed URL parameters were ignored on
    # some page versions, leaving passenger/cabin/date settings at defaults.
    trip = f"Flights from {origin} to {destination} on {date}"
    if return_date:
        trip += f" returning {return_date}"
    else:
        trip += " one way"
    cabin_label = {
        "economy": "economy",
        "premium": "premium economy",
        "business": "business class",
        "first": "first class",
    }.get(cabin.lower(), "economy")
    trip += f", {passengers} adult{'s' if passengers != 1 else ''}, {cabin_label}"
    return f"{base}?{urlencode({'q': trip, 'hl': 'en'})}"



def _search_flights_browser(
    origin:      str,
    destination: str,
    date:        str,
    return_date: str | None,
    passengers:  int,
    cabin:       str,
) -> tuple[str, str]:
    import time
    from actions.browser_control import browser_control

    url = _build_google_flights_url(
        origin, destination, date, return_date, passengers, cabin
    )

    print(f"[FlightFinder] 🌐 Opening: {url}")
    opened = browser_control({"action": "go_to", "url": url}) or ""
    if not opened.startswith("Opened"):
        raise RuntimeError(f"Google Flights page did not open: {opened[:180]}")

    # Google Flights hydrates result text asynchronously. Poll instead of
    # scraping after a fixed delay that can be too short on a slow connection.
    deadline = time.monotonic() + 18.0
    raw = ""
    while time.monotonic() < deadline:
        raw = browser_control({"action": "get_text"}) or ""
        lower = raw.casefold()
        has_flight_data = any(token in lower for token in (
            "nonstop", "non-stop", "stop", "depart", "arrival", "per person",
            "round trip", "one way", "airline", "flight duration", "duration",
        )) and bool(re.search(r"(?:\$|€|£|₹|\b(?:USD|LKR|EUR|GBP|Rs\.?|INR)\b|\b\d{1,2}:\d{2}\b)", raw, re.I))
        if (len(raw) >= 350 and has_flight_data
                and "could not get page text" not in lower
                and "access denied" not in lower):
            break
        time.sleep(.75)
    return raw, url

def _parse_flights_with_gemini(
    raw_text:    str,
    origin:      str,
    destination: str,
    date:        str,
) -> list[dict]:
    # Do not ask a model to invent options from a Google Flights page shell,
    # consent screen, or access-denied response.
    has_flight_data = any(token in raw_text.casefold() for token in (
        "nonstop", "non-stop", "stop", "depart", "arrival", "airline",
        "flight duration", "duration",
    )) and bool(re.search(r"(?:\$|€|£|₹|\b(?:USD|LKR|EUR|GBP|Rs\.?|INR)\b|\b\d{1,2}:\d{2}\b)", raw_text, re.I))
    if len(raw_text.strip()) < 350 or not has_flight_data:
        return []

    from google.genai import types

    prompt  = (
        f"Extract flight options from {origin} to {destination} on {date} "
        f"from this Google Flights page text:\n\n{raw_text[:12000]}\n\n"
        f"Return a JSON array of up to 5 flights:\n"
        f'[{{"airline":"...","departure":"HH:MM","arrival":"HH:MM",'
        f'"duration":"Xh Ym","stops":0,"price":"...","currency":"USD"}}]\n'
        f"If no flights found, return: []"
    )

    try:
        from core import gemini
        response = gemini.call(
            prompt,
            tier=gemini.SMART,
            timeout_ms=30_000,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are a flight data extraction expert. "
                    "Extract flight information from raw webpage text. "
                    "Return ONLY valid JSON — no markdown, no explanation."
                )
            ),
        )
        text = str(getattr(response, "text", "") or "").strip()
        text = re.sub(r"```(?:json)?", "", text, flags=re.I).strip().rstrip("`").strip()
        try:
            flights = json.loads(text)
        except json.JSONDecodeError:
            # Models sometimes surround the JSON array with a short preamble.
            start, end = text.find("["), text.rfind("]")
            if start < 0 or end <= start:
                return []
            flights = json.loads(text[start:end + 1])
        if not isinstance(flights, list):
            return []

        normalized = []
        for flight in flights[:5]:
            if not isinstance(flight, dict):
                continue
            stops_raw = flight.get("stops", 0)
            match = re.search(r"\d+", str(stops_raw))
            normalized.append({
                "airline": str(flight.get("airline") or "Unknown airline")[:100],
                "departure": str(flight.get("departure") or "--:--")[:32],
                "arrival": str(flight.get("arrival") or "--:--")[:32],
                "duration": str(flight.get("duration") or "")[:48],
                "stops": int(match.group()) if match else (0 if str(stops_raw).casefold() in {"nonstop", "non-stop", "direct"} else None),
                "price": str(flight.get("price") or "")[:40],
                "currency": str(flight.get("currency") or "")[:8],
            })
        return normalized
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Gemini parse failed: {e}")
        return []

def _format_spoken(
    flights:     list[dict],
    origin:      str,
    destination: str,
    date:        str,
) -> str:
    if not flights:
        return (
            f"I couldn't find any flights from {origin} to {destination} "
            f"on {date}, sir. The page may not have loaded correctly."
        )

    lines = [f"Here are the top flights from {origin} to {destination} on {date}, sir."]

    for i, f in enumerate(flights[:5], 1):
        airline   = f.get("airline",   "Unknown airline")
        departure = f.get("departure", "--:--")
        arrival   = f.get("arrival",   "--:--")
        duration  = f.get("duration",  "")
        stops     = f.get("stops",     0)
        price     = f.get("price",     "")
        currency  = f.get("currency",  "")

        stop_str  = ("non-stop" if stops == 0 else
                     f"{stops} stop{'s' if stops != 1 else ''}" if isinstance(stops, int)
                     else "stops unavailable")
        price_str = f"{price} {currency}".strip() if price else "price unavailable"
        dur_str   = f", {duration}" if duration else ""

        lines.append(
            f"Option {i}: {airline}, departing {departure}, "
            f"arriving {arrival}{dur_str}, {stop_str}, {price_str}."
        )

    # Cheapest — strip non-digits for comparison
    priced = [f for f in flights if f.get("price")]
    if priced:
        cheapest = min(
            priced,
            key=lambda x: int(re.sub(r"[^\d]", "", str(x["price"])) or "999999"),
        )
        lines.append(
            f"The cheapest option is {cheapest.get('airline')} "
            f"at {cheapest.get('price')} {cheapest.get('currency', '')}."
        )

    return " ".join(lines)


def _format_text_report(
    flights:     list[dict],
    origin:      str,
    destination: str,
    date:        str,
    return_date: str | None,
    page_url:    str,
) -> str:
    lines = [
        "JARVIS — Flight Search Results",
        "─" * 50,
        f"Route     : {origin} → {destination}",
        f"Date      : {date}",
    ]
    if return_date:
        lines.append(f"Return    : {return_date}")
    lines += [
        f"Searched  : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Source    : {page_url}",
        "─" * 50,
        "",
    ]

    if not flights:
        lines.append("No flights found.")
    else:
        for i, f in enumerate(flights, 1):
            stops    = f.get("stops")
            stop_str = ("Non-stop" if stops == 0 else
                        f"{stops} stop(s)" if isinstance(stops, int)
                        else "Unknown")
            lines += [
                f"Flight {i}:",
                f"  Airline   : {f.get('airline',   'N/A')}",
                f"  Departure : {f.get('departure', 'N/A')}",
                f"  Arrival   : {f.get('arrival',   'N/A')}",
                f"  Duration  : {f.get('duration',  'N/A')}",
                f"  Stops     : {stop_str}",
                f"  Price     : {f.get('price', 'N/A')} {f.get('currency', '')}",
                "",
            ]

    return "\n".join(lines)

def _save_to_desktop(content: str, origin: str, destination: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"flights_{origin}_{destination}_{ts}.txt".replace(" ", "_")
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename

    filepath.write_text(content, encoding="utf-8")
    print(f"[FlightFinder] 💾 Saved: {filepath}")

    try:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(filepath)])
        elif is_mac():
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Could not open text editor: {e}")

    return str(filepath)


def flight_finder(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}

    origin      = str(params.get("origin") or "").strip()
    destination = str(params.get("destination") or "").strip()
    date_raw    = str(params.get("date") or "").strip()
    return_raw  = str(params.get("return_date") or "").strip()
    try:
        passengers = max(1, min(9, int(params.get("passengers", 1))))
    except (TypeError, ValueError):
        return "Please provide a passenger count from 1 to 9, sir."
    cabin       = str(params.get("cabin") or "economy").strip().lower()
    save_raw    = params.get("save", False)
    save        = save_raw is True or str(save_raw).strip().casefold() in {"yes", "true", "1"}

    if not origin or not destination:
        return "Please provide both origin and destination, sir."
    if not date_raw:
        return "Please provide a departure date, sir."

    # Normalise cabin value
    if cabin not in _CABIN_CODE:
        cabin = "economy"

    try:
        date = _parse_date(date_raw)
        return_date = _parse_date(return_raw) if return_raw else None
    except ValueError as exc:
        return str(exc)
    if return_date and datetime.strptime(return_date, "%Y-%m-%d") <= datetime.strptime(date, "%Y-%m-%d"):
        return "The return date must be after the departure date, sir."

    if player:
        player.write_log(f"[FlightFinder] {origin} → {destination} on {date}")

    if speak:
        speak(f"Searching flights from {origin} to {destination} on {date}, sir.")

    print(
        f"[FlightFinder] ▶️ {origin} → {destination} | {date}"
        f"{' → ' + return_date if return_date else ''}"
        f" | {cabin} | {passengers} pax"
    )

    try:
        raw_text, page_url = _search_flights_browser(
            origin, destination, date, return_date, passengers, cabin
        )

        if not raw_text:
            return "Could not retrieve flight data, sir. The page may not have loaded."

        if speak:
            speak("Analysing the results now, sir.")

        flights = _parse_flights_with_gemini(raw_text, origin, destination, date)
        spoken  = _format_spoken(flights, origin, destination, date)

        if player and hasattr(player, "show_flight_cards"):
            player.show_flight_cards(flights, origin, destination, date)

        if speak:
            speak(spoken)

        result = spoken

        if save and flights:
            report     = _format_text_report(flights, origin, destination, date, return_date, page_url)
            saved_path = _save_to_desktop(report, origin, destination)
            result    += f" Results saved to Desktop: {saved_path}"

        return result

    except Exception as e:
        print(f"[FlightFinder] ❌ {e}")
        return f"Flight search failed, sir: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "flight_finder",
    "description": "Searches Google Flights and speaks the best options.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "origin": {
                "type": "STRING",
                "description": "Departure city or airport code"
            },
            "destination": {
                "type": "STRING",
                "description": "Arrival city or airport code"
            },
            "date": {
                "type": "STRING",
                "description": "Departure date (any format)"
            },
            "return_date": {
                "type": "STRING",
                "description": "Return date for round trips"
            },
            "passengers": {
                "type": "INTEGER",
                "description": "Number of passengers (default: 1)"
            },
            "cabin": {
                "type": "STRING",
                "description": "economy | premium | business | first"
            },
            "save": {
                "type": "BOOLEAN",
                "description": "Save results to Notepad"
            }
        },
        "required": [
            "origin",
            "destination",
            "date"
        ]
    },
    "handler": flight_finder,
}
