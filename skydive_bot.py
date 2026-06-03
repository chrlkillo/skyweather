import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

LAT = 54.3201
LON = 40.8796
MORNING_HOUR = 6    # 09:00 МСК
MORNING_MIN = 0
EVENING_HOUR = 14   # 17:00 МСК
EVENING_MIN = 0

MSK = timezone(timedelta(hours=3))
DAYLIGHT_START = 8   # 08:00 МСК
DAYLIGHT_END = 20    # 20:00 МСК

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Статистика использования (сбрасывается при перезапуске)
stats = {
    "users": set(),        # уникальные chat_id
    "requests": 0,         # всего запросов /weather
    "started": datetime.now(tz=MSK),  # время запуска
}

# Ручные правки погоды от админа по отрезкам (8, 11, 14, 17)
# Формат: {8: {"wind": 3.5, "wind120": None, "gusts": None, "clouds": None}, ...}
manual_override = {}  # slot_start -> {param -> value}
VALID_SLOTS = [8, 11, 14, 17]

# Саундтреки по настроению погоды
# Формат: (название, исполнитель, youtube_url)
SOUNDTRACKS = {
    "sunny": [
        ("Here Comes The Sun", "The Beatles", "https://youtu.be/KQetemT1sWc"),
        ("Walking on Sunshine", "Katrina & The Waves", "https://youtu.be/iPUmE-tne5U"),
        ("Good Day Sunshine", "The Beatles", "https://youtu.be/4o_C-YXmFgk"),
        ("Sun Is Shining", "Bob Marley", "https://youtu.be/fbJOmxpkFhY"),
        ("Lovely Day", "Bill Withers", "https://youtu.be/bEeaS6fuUoA"),
    ],
    "cloudy": [
        ("The Sound of Silence", "Simon & Garfunkel", "https://youtu.be/4zLfCnGVeL4"),
        ("Riders on the Storm", "The Doors", "https://youtu.be/iv8GW1GaoIc"),
        ("November Rain", "Guns N Roses", "https://youtu.be/8SbUC-UaAxE"),
        ("Cloud 9", "George Harrison", "https://youtu.be/8EK4fKMzXuA"),
        ("Both Sides Now", "Joni Mitchell", "https://youtu.be/Pbn6a0AFtgM"),
    ],
    "rainy": [
        ("Purple Rain", "Prince", "https://youtu.be/TvnYmWpD_T8"),
        ("Raindrops Keep Fallin", "BJ Thomas", "https://youtu.be/OHhNZakuOLk"),
        ("Have You Ever Seen The Rain", "CCR", "https://youtu.be/Gu2pVPWGYMQ"),
        ("Rain", "The Beatles", "https://youtu.be/tHbe4J3fJkE"),
        ("Singing in the Rain", "Gene Kelly", "https://youtu.be/D1ZYhVpdXbQ"),
    ],
    "windy": [
        ("Blowin in the Wind", "Bob Dylan", "https://youtu.be/vWwgrjjIMXA"),
        ("Dust in the Wind", "Kansas", "https://youtu.be/tH2w6Oxx0kQ"),
        ("Wind of Change", "Scorpions", "https://youtu.be/n4RjJKxsamQ"),
        ("Gone with the Wind", "Jimmy Dorsey", "https://youtu.be/qDc3K4V-2EM"),
        ("The Wind Cries Mary", "Jimi Hendrix", "https://youtu.be/6EEW-9NDM5k"),
    ],
    "perfect": [
        ("Free Fallin", "Tom Petty", "https://youtu.be/1lWJXDG2i0A"),
        ("Jump", "Van Halen", "https://youtu.be/SwYN7mTi6HM"),
        ("Learn to Fly", "Foo Fighters", "https://youtu.be/1VQ_3sBZEm0"),
        ("Sky is the Limit", "Biggie", "https://youtu.be/Bm1n_JnkXnk"),
        ("Fly Away", "Lenny Kravitz", "https://youtu.be/l_mynKNOeJo"),
    ],
}


def get_daily_soundtrack(weather_data):
    """Выбирает саундтрек дня на основе погоды. Один для всех на весь день."""
    fl = weather_data["list"]
    today_msk = datetime.now(tz=MSK).date()
    
    # Берём дневные данные для определения настроения
    day_items = [i for i in fl if is_daylight(datetime.fromtimestamp(i["dt"], tz=MSK))
                 and datetime.fromtimestamp(i["dt"], tz=MSK).date() == today_msk]
    if not day_items:
        day_items = fl[:4]
    
    # Определяем погодное настроение
    rain = any("rain" in i or "snow" in i or i["weather"][0]["id"] < 700 for i in day_items)
    avg_wind = sum(i["wind"]["speed"] for i in day_items) / len(day_items)
    avg_clouds = sum(i["clouds"]["all"] for i in day_items) / len(day_items)
    
    # Идеально для прыжков
    aff_ok = calc_aff_prob(day_items) >= 70 if day_items else False
    
    if aff_ok:
        mood = "perfect"
    elif rain:
        mood = "rainy"
    elif avg_wind > 6:
        mood = "windy"
    elif avg_clouds > 60:
        mood = "cloudy"
    else:
        mood = "sunny"
    
    # Выбираем трек детерминированно по дате — один для всех весь день
    tracks = SOUNDTRACKS[mood]
    idx = today_msk.toordinal() % len(tracks)
    name, artist, url = tracks[idx]
    return mood, name, artist, url


async def tg_send(text, chat_id=None):
    target = chat_id if chat_id else CHAT_ID
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/sendMessage", json={
            "chat_id": target, "text": text,         }, timeout=10)


async def tg_get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TG_API}/getUpdates", params=params, timeout=35)
        return r.json()


async def get_weather():
    """Получает погоду с Open-Meteo, модель ECMWF IFS."""
    async with httpx.AsyncClient() as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT,
            "longitude": LON,
            "hourly": "temperature_2m,rain,cloud_cover,cloud_cover_low,cloud_cover_mid,wind_speed_10m,wind_speed_120m,wind_gusts_10m,wind_speed_975hPa,wind_speed_900hPa,wind_speed_850hPa,wind_speed_800hPa,wind_speed_700hPa,wind_speed_600hPa,wind_speed_925hPa,wind_direction_975hPa,wind_direction_925hPa,wind_direction_900hPa,wind_direction_850hPa,wind_direction_800hPa,wind_direction_700hPa,wind_direction_600hPa",
            "models": "ecmwf_ifs",
            "timezone": "Europe/Moscow",
            "wind_speed_unit": "ms",
            "forecast_days": 3,
        }, timeout=10)
        raw = r.json()
        return _parse_open_meteo(raw)


def _wmo_description(code):
    """Расшифровка WMO weather code."""
    codes = {
        0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность",
        3: "пасмурно", 45: "туман", 48: "изморозь",
        51: "лёгкая морось", 53: "морось", 55: "сильная морось",
        61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
        71: "небольшой снег", 73: "снег", 75: "сильный снег",
        77: "снежная крупа", 80: "ливень", 81: "сильный ливень",
        82: "очень сильный ливень", 85: "снегопад", 86: "сильный снегопад",
        95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
    }
    return codes.get(code, "облачно")


def _is_precipitation(code):
    return code >= 51


def _parse_open_meteo(raw):
    """Конвертирует ответ Open-Meteo (ECMWF IFS) в унифицированный формат."""
    hourly = raw["hourly"]
    times = hourly["time"]
    items = []
    for i, t in enumerate(times):
        dt_msk = datetime.fromisoformat(t).replace(tzinfo=MSK)
        rain = hourly["rain"][i] or 0
        wind10 = hourly["wind_speed_10m"][i] or 0
        wind100 = hourly["wind_speed_120m"][i] or 0
        gust = hourly["wind_gusts_10m"][i] or wind10
        clouds_low = hourly["cloud_cover_low"][i] or 0
        clouds_mid = hourly["cloud_cover_mid"][i] or 0
        # Облачность только до ~5000м (low + mid ярус)
        clouds = max(clouds_low, clouds_mid)
        has_rain = rain > 0
        # Описание погоды на основе дождя и облачности
        if has_rain:
            desc = "дождь" if rain < 2 else "сильный дождь"
            weather_id = 500
        elif clouds > 80:
            desc = "пасмурно"
            weather_id = 804
        elif clouds > 50:
            desc = "облачно с прояснениями"
            weather_id = 803
        elif clouds > 25:
            desc = "переменная облачность"
            weather_id = 802
        elif clouds > 10:
            desc = "небольшая облачность"
            weather_id = 801
        else:
            desc = "ясно"
            weather_id = 800
        dir10  = hourly.get("wind_direction_10m",  [0]*len(times))[i] or 0
        dir120 = hourly.get("wind_direction_120m", [0]*len(times))[i] or 0
        # Pressure levels
        def gw(key): return hourly.get(key, [0]*len(times))[i] or 0
        w600=gw("wind_speed_600hPa");  d600=gw("wind_direction_600hPa")  # ~4200м
        w700=gw("wind_speed_700hPa");  d700=gw("wind_direction_700hPa")  # ~3000м
        w800=gw("wind_speed_800hPa");  d800=gw("wind_direction_800hPa")  # ~1900м
        w850=gw("wind_speed_850hPa");  d850=gw("wind_direction_850hPa")  # ~1500м
        w900=gw("wind_speed_900hPa");  d900=gw("wind_direction_900hPa")  # ~1000м
        w925=gw("wind_speed_925hPa");  d925=gw("wind_direction_925hPa")  # ~800м
        w975=gw("wind_speed_975hPa");  d975=gw("wind_direction_975hPa")  # ~320м
        item = {
            "dt": int(dt_msk.timestamp()),
            "main": {
                "temp": hourly["temperature_2m"][i] or 0,
                "feels_like": hourly["temperature_2m"][i] or 0,
                "humidity": 0,
            },
            "wind": {
                "speed":     round(wind10, 1),   "deg":     round(dir10),   # Земля
                "speed_100m":round(wind100, 1),  "deg_120m":round(dir120),  # ~100м
                "gust":      round(gust, 1),
                "w975": round(w975,1), "d975": round(d975),  # ~320м
                "w925": round(w925,1), "d925": round(d925),  # ~800м
                "w900": round(w900,1), "d900": round(d900),  # ~1000м
                "w850": round(w850,1), "d850": round(d850),  # ~1500м
                "w800": round(w800,1), "d800": round(d800),  # ~1900м
                "w700": round(w700,1), "d700": round(d700),  # ~3000м
                "w600": round(w600,1), "d600": round(d600),  # ~4200м
            },
            "clouds": {"all": int(clouds)},
            "visibility": 10000,
            "weather": [{"id": weather_id, "description": desc}],
            "rain": {} if has_rain else None,
            "snow": None,
            "precip_prob": 0,
        }
        items.append(item)
    return {"list": items}


def apply_override(fl):
    """Применяет ручные правки по отрезкам к прогнозу сегодняшнего дня."""
    if not manual_override:
        return fl
    today = datetime.now(tz=MSK).date()
    result = []
    for item in fl:
        dt_msk = datetime.fromtimestamp(item["dt"], tz=MSK)
        if dt_msk.date() == today:
            h = dt_msk.hour
            # Найти к какому отрезку относится час
            slot = None
            for s in VALID_SLOTS:
                if s <= h < s + 3:
                    slot = s
                    break
            if slot is not None and slot in manual_override:
                ov = manual_override[slot]
                item = dict(item)
                item["wind"] = dict(item["wind"])
                if ov.get("wind") is not None:
                    item["wind"]["speed"] = ov["wind"]
                if ov.get("wind120") is not None:
                    item["wind"]["speed_100m"] = ov["wind120"]
                if ov.get("gusts") is not None:
                    item["wind"]["gust"] = ov["gusts"]
                if ov.get("clouds") is not None:
                    item["clouds"] = {"all": ov["clouds"]}
        result.append(item)
    return result


def is_daylight(dt_msk):
    return DAYLIGHT_START <= dt_msk.hour < DAYLIGHT_END


def filter_day(fl, target_date):
    """Фильтрует прогноз по дате и световому дню (МСК)."""
    result = []
    for item in fl:
        dt_msk = datetime.fromtimestamp(item["dt"], tz=MSK)
        if dt_msk.date() == target_date and is_daylight(dt_msk):
            result.append(item)
    return result


def calc_prob(fl):
    """Вероятность для опытных парашютистов.
    - Облачность: штраф с 45%
    - Ветер у земли: штраф с 8 м/с
    - Порывы у земли: штраф с 11 м/с
    - Ветер на высоте 120м: штраф с 14 м/с
    """
    if not fl:
        return None
    scores = []
    for item in fl:
        s = 100

        # Осадки
        has_rain = item.get("rain") is not None
        if has_rain: s -= 50

        # Облачность — штраф с 45%
        c = item["clouds"]["all"]
        if c > 80: s -= 40
        elif c > 65: s -= 25
        elif c > 45: s -= 10

        # Ветер у земли — штраф с 8 м/с
        w = item["wind"]["speed"]
        if w > 14: s -= 50
        elif w > 11: s -= 30
        elif w > 8: s -= 15

        # Порывы у земли — штраф с 11 м/с
        g = item["wind"].get("gust", 0)
        if g > 18: s -= 40
        elif g > 14: s -= 25
        elif g > 11: s -= 10

        # Ветер на высоте 120м — штраф с 14 м/с
        w120 = item["wind"].get("speed_100m", 0)
        if w120 > 20: s -= 40
        elif w120 > 17: s -= 25
        elif w120 > 14: s -= 10

        # Видимость
        v = item.get("visibility", 10000)
        if v < 3000: s -= 30
        elif v < 5000: s -= 15

        scores.append(max(0, s))
    return round(sum(scores) / len(scores))


def calc_aff_prob(fl):
    """Вероятность для AFF студентов:
    - Ветер у земли (10м) <= 7 м/с — жёсткий запрет
    - Ветер на высоте 120м <= 12 м/с — жёсткий запрет
    - Облачность до 5000м: штраф при >30%
    - Осадки — запрет
    - Порывы — только штраф
    """
    if not fl:
        return None
    scores = []
    for item in fl:
        s = 100

        # Осадки — запрет
        has_rain = item.get("rain") is not None
        if has_rain:
            scores.append(0)
            continue

        # Ветер у земли >7 м/с — запрет
        w10 = item["wind"]["speed"]
        if w10 > 7:
            scores.append(0)
            continue

        # Ветер на 120м >12 м/с — запрет
        w120 = item["wind"].get("speed_100m", 0)
        if w120 > 12:
            scores.append(0)
            continue

        # Облачность до 5000м — плавный штраф
        c = item["clouds"]["all"]
        if c >= 50: s -= 60
        elif c >= 40: s -= 40
        elif c >= 30: s -= 20
        elif c >= 20: s -= 10

        # Ветер у земли — плавный штраф
        if w10 > 5: s -= 25
        elif w10 > 3: s -= 10

        # Ветер 120м — плавный штраф
        if w120 > 10: s -= 30
        elif w120 > 7: s -= 15

        # Порывы — только штраф
        g = item["wind"].get("gust", 0)
        if g > 12: s -= 30
        elif g > 9: s -= 15
        elif g > 6: s -= 5

        scores.append(max(0, s))
    return round(sum(scores) / len(scores))


def wind_dir(deg):
    return ["С","СВ","В","ЮВ","Ю","ЮЗ","З","СЗ"][round(deg/45)%8]


def emoji(p):
    if p is None: return "⚫️"
    return "🟢" if p>=80 else "🟡" if p>=60 else "🟠" if p>=40 else "🔴"


def prob_str(p):
    return f"{p}%" if p is not None else "нет данных"


def day_prob_from_slots(items, calc_fn):
    """Считает вероятность дня как долю отрезков где вероятность >= 50%."""
    fixed_slots = [8, 11, 14, 17]
    slots = {s: [] for s in fixed_slots}
    for item in items:
        dt_msk = datetime.fromtimestamp(item["dt"], tz=MSK)
        h = dt_msk.hour
        for slot_start in fixed_slots:
            if slot_start <= h < slot_start + 3:
                slots[slot_start].append(item)
                break
    good = sum(1 for s in fixed_slots if slots[s] and calc_fn(slots[s]) >= 70)
    total = sum(1 for s in fixed_slots if slots[s])
    return round(good / total * 100) if total else None




def calc_jumprun(items_now):
    """Рассчитывает снос и точку выброски."""
    import math
    if not items_now:
        return None
    item = items_now[0]
    w = item["wind"]

    # Слои: (высота_label, скорость, направление, время_сек)
    # Свободное падение: 4200→1000м ~60с (делим по слоям)
    # Купол: 1000→0м ~240с
    ff_layers = [
        ("4200м", w.get("w600",0), w.get("d600",0), 13),  # 4200-3000м ~13с
        ("3000м", w.get("w700",0), w.get("d700",0), 13),  # 3000-1900м ~13с
        ("1900м", w.get("w800",0), w.get("d800",0), 10),  # 1900-1500м ~10с
        ("1500м", w.get("w850",0), w.get("d850",0), 10),  # 1500-1000м ~10с
        ("1000м", w.get("w900",0), w.get("d900",0), 14),  # 1000м — раскрытие
    ]
    cp_layers = [
        (" 800м", w.get("w925",0), w.get("d925",0), 30),   # купол высоко
        (" 320м", w.get("w975",0), w.get("d975",0), 60),   # купол средне
        (" 100м", w["speed_100m"], w.get("deg_120m",0), 60), # купол низко
        ("Земля", w["speed"],      w.get("deg",0),     90),  # финал
    ]

    def drift(layers):
        x, y = 0.0, 0.0
        for _, spd, deg, t in layers:
            r = math.radians(deg)
            x += spd * math.sin(r) * t
            y += spd * math.cos(r) * t
        return x, y

    ff_x, ff_y = drift(ff_layers)
    cp_x, cp_y = drift(cp_layers)
    tx, ty = ff_x + cp_x, ff_y + cp_y
    total = round(math.sqrt(tx**2 + ty**2))
    ff_dist = round(math.sqrt(ff_x**2 + ff_y**2))
    cp_dist = round(math.sqrt(cp_x**2 + cp_y**2))

    drift_deg = round(math.degrees(math.atan2(tx, ty)) % 360)
    exit_deg  = (drift_deg + 180) % 360

    # Рекомендация по отводу
    if total < 300:
        offset = "0м — выброска над точкой"
    elif total < 600:
        offset = f"500м до точки"
    elif total < 850:
        offset = f"700м до точки"
    elif total < 1200:
        offset = f"1000м до точки"
    else:
        offset = f"1500м до точки"

    # Интервал между парашютистами по ветру на 4200м
    w_exit = w.get("w600", 0)
    if w_exit <= 10:
        interval = 5
    elif w_exit <= 15:
        interval = 7
    elif w_exit <= 20:
        interval = 9
    elif w_exit <= 25:
        interval = 11
    else:
        interval = 16

    # Курс захода — против ветра на 4200м
    approach_deg = (w.get("d600", 0) + 180) % 360

    return {
        "ff_layers": ff_layers,
        "cp_layers": cp_layers,
        "ff_dist": ff_dist,
        "cp_dist": cp_dist,
        "total": total,
        "drift_deg": drift_deg,
        "exit_deg": exit_deg,
        "offset": offset,
        "interval": interval,
        "approach_deg": approach_deg,
        "w_exit": w_exit,
        "item": item,
    }


def format_jumprun(jr, today_fl):
    if jr is None:
        return "⚠️ Нет данных для расчёта."
    w = jr["item"]["wind"]

    # Таблица ветров по высотам
    all_layers = [
        ("4200м", w.get("w600",0), w.get("d600",0)),
        ("3000м", w.get("w700",0), w.get("d700",0)),
        ("1900м", w.get("w800",0), w.get("d800",0)),
        ("1500м", w.get("w850",0), w.get("d850",0)),
        ("1000м", w.get("w900",0), w.get("d900",0)),
        (" 800м", w.get("w925",0), w.get("d925",0)),
        (" 320м", w.get("w975",0), w.get("d975",0)),
        (" 100м", w["speed_100m"], w.get("deg_120m",0)),
        ("Земля", w["speed"],      w.get("deg",0)),
    ]
    wind_lines = "\n".join(f"  {lbl}: {spd:.1f} м/с  {int(deg)}°" for lbl, spd, deg in all_layers)

    # Облачность по часам
    cloud_lines = []
    for item in today_fl:
        dt_msk = datetime.fromtimestamp(item["dt"], tz=MSK)
        if DAYLIGHT_START <= dt_msk.hour < DAYLIGHT_END:
            c = item["clouds"]["all"]
            filled = round(c / 10)
            bar = "█" * filled + "░" * (10 - filled)
            cloud_lines.append(f"  {dt_msk.strftime('%H:%M')} [{bar}] {c}%")
    cloud_section = "\n".join(cloud_lines) if cloud_lines else "  нет данных"

    return (
        f"✈️ Jumprun — Крутицы\n"
        f"{datetime.now(tz=MSK).strftime('%d.%m.%Y %H:%M')} МСК\n\n"
        f"💨 Ветер по высотам:\n{wind_lines}\n\n"
        f"📐 Снос:\n"
        f"  Свободное падение (4200→1000м): {jr['ff_dist']}м\n"
        f"  Купол (1000→0м): {jr['cp_dist']}м\n"
        f"  Итого: {jr['total']}м  в направлении {jr['drift_deg']}°\n\n"
        f"🎯 Выброска: {jr['offset']}  курс {jr['exit_deg']}°\n"
        f"✈️ Заход самолёта: {jr['approach_deg']}° (против ветра на 4200м, {jr['w_exit']:.1f} м/с)\n\n"
        f"⏱ Интервал между парашютистами: {jr['interval']} сек\n\n"
        f"☁️ Облачность сегодня (до 5000м):\n{cloud_section}"
    )

async def make_message(label):
    data = await get_weather()
    fl = apply_override(data["list"])

    now_msk = datetime.now(tz=MSK)
    today = now_msk.date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    today_fl = filter_day(fl, today)
    tomorrow_fl = filter_day(fl, tomorrow)
    day_after_fl = filter_day(fl, day_after)

    # Текущие условия (первый элемент прогноза)
    cur = fl[0]
    temp = round(cur["main"]["temp"])
    feels = round(cur["main"]["feels_like"])
    ws = cur["wind"]["speed"]
    gust = cur["wind"].get("gust", ws)
    wdir = wind_dir(cur["wind"]["deg"])
    clouds = cur["clouds"]["all"]
    hum = cur["main"]["humidity"]
    vis = cur.get("visibility", 10000) // 1000
    desc = cur["weather"][0]["description"].capitalize()

    # Вероятности сегодня
    # Вероятность дня = доля отрезков >= 50%, AFF всегда <= опытных
    prob_today = day_prob_from_slots(today_fl, calc_prob) if today_fl else None
    aff_today_raw = day_prob_from_slots(today_fl, calc_aff_prob) if today_fl else None
    aff_today = min(aff_today_raw, prob_today) if (aff_today_raw is not None and prob_today is not None) else aff_today_raw

    prob_tmrw = day_prob_from_slots(tomorrow_fl, calc_prob) if tomorrow_fl else None
    aff_tmrw_raw = day_prob_from_slots(tomorrow_fl, calc_aff_prob) if tomorrow_fl else None
    aff_tmrw = min(aff_tmrw_raw, prob_tmrw) if (aff_tmrw_raw is not None and prob_tmrw is not None) else aff_tmrw_raw

    prob_da = day_prob_from_slots(day_after_fl, calc_prob) if day_after_fl else None
    aff_da_raw = day_prob_from_slots(day_after_fl, calc_aff_prob) if day_after_fl else None
    aff_da = min(aff_da_raw, prob_da) if (aff_da_raw is not None and prob_da is not None) else aff_da_raw

    def make_3h_blocks(items):
        """Группирует часы в фиксированные 3-часовые отрезки: 08-11, 11-14, 14-17, 17-20."""
        # Фиксированные отрезки
        fixed_slots = [8, 11, 14, 17]
        slots = {s: [] for s in fixed_slots}
        for item in items:
            dt_msk = datetime.fromtimestamp(item["dt"], tz=MSK)
            h = dt_msk.hour
            # Найти к какому отрезку относится час
            assigned = None
            for slot_start in fixed_slots:
                if slot_start <= h < slot_start + 3:
                    assigned = slot_start
                    break
            if assigned is not None:
                slots[assigned].append(item)

        lines = []
        good_slots = 0
        total_slots = 0
        for slot_start in sorted(slots.keys()):
            slot_items = slots[slot_start]
            slot_end = slot_start + 3
            aff_p = calc_aff_prob(slot_items)
            exp_p = calc_prob(slot_items)
            ind = emoji(aff_p)
            # Берём средние значения по отрезку
            w_avg = round(sum(i["wind"]["speed"] for i in slot_items) / len(slot_items), 1)
            g_avg = round(sum(i["wind"].get("gust", 0) for i in slot_items) / len(slot_items), 1)
            w100_avg = round(sum(i["wind"].get("speed_100m", 0) for i in slot_items) / len(slot_items), 1)
            c_avg = round(sum(i["clouds"]["all"] for i in slot_items) / len(slot_items))
            t_avg = round(sum(i["main"]["temp"] for i in slot_items) / len(slot_items))
            d = slot_items[len(slot_items)//2]["weather"][0]["description"]
            lines.append(
                f"{ind} {slot_start:02d}:00-{slot_end:02d}:00 | {t_avg}°C | "
                f"💨{w_avg}м/с (пор.{g_avg}) | ☁️{c_avg}% {d}\n"
                f"   Опытные: {exp_p}% | AFF: {aff_p}%"
            )
            total_slots += 1
            if aff_p >= 70:
                good_slots += 1

        # Вероятность дня = доля хороших отрезков (AFF всегда <= опытных)
        day_aff_pct = round(good_slots / total_slots * 100) if total_slots else 0
        return lines, day_aff_pct, total_slots

    today_str = today.strftime("%d.%m")
    tomorrow_str = tomorrow.strftime("%d.%m")
    day_after_str = day_after.strftime("%d.%m")

    _, s_name, s_artist, s_url = get_daily_soundtrack(data)

    def day_section(title, prob, aff, items):
        if not items:
            s = f"{title}:\n"
            s += f"{emoji(prob)} Опытные: {prob_str(prob)}\n"
            s += f"{emoji(aff)} AFF студенты: {prob_str(aff)}\n"
            return s
        blocks, day_aff_pct, total = make_3h_blocks(items)
        day_ind = emoji(day_aff_pct)
        s = f"{title}:\n"
        s += f"{emoji(prob)} Опытные: {prob_str(prob)}\n"
        s += f"{day_ind} AFF студенты: {day_aff_pct}%\n"
        s += "\n".join(blocks) + "\n"
        return s

    return (
        f"🪂 Прогноз — {label}\n📍 Крутицы, Рязанская обл.\n\n"
        + day_section(f"Сегодня {today_str}", prob_today, aff_today, today_fl)
        + "\n"
        + day_section(f"Завтра {tomorrow_str}", prob_tmrw, aff_tmrw, tomorrow_fl)
        + "\n"
        + day_section(f"Послезавтра {day_after_str}", prob_da, aff_da, day_after_fl)
        + f"\n🎵 Саундтрек дня: [{s_name} — {s_artist}]({s_url})\n\n"
        + f"_{now_msk.strftime('%d.%m.%Y %H:%M')} МСК_"
    )


async def send_forecast(label):
    try:
        msg = await make_message(label)
        await tg_send(msg)
        logger.info(f"Отправлен прогноз: {label}")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await tg_send(f"⚠️ Ошибка получения погоды: {e}")


async def poll_updates():
    offset = None
    logger.info("Начинаю polling...")
    while True:
        try:
            data = await tg_get_updates(offset)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                user_chat_id = msg.get("chat", {}).get("id")
                user_name = msg.get("from", {}).get("first_name", "")
                user_username = msg.get("from", {}).get("username", "")

                # Трекинг пользователей
                if user_chat_id:
                    stats["users"].add(user_chat_id)

                if text == "/start":
                    stats["users"].add(user_chat_id)
                    await tg_send(
                        "🪂 Бот прогноза для прыжков\n\n"
                        "Прогноз дважды в день:\n🌅 09:00 МСК\n🌆 17:00 МСК\n\n"
                        "Команды:\n/weather — прогноз прямо сейчас\n/stats — статистика бота\n\n"
                        "Показывает вероятности прыжков для AFF студентов 🎓",
                        chat_id=user_chat_id
                    )
                elif text == "/jumprun":
                    try:
                        data = await get_weather()
                        fl = apply_override(data["list"])
                        now_msk = datetime.now(tz=MSK)
                        today = now_msk.date()
                        # Текущий час
                        now_items = [i for i in fl
                                     if datetime.fromtimestamp(i["dt"], tz=MSK).replace(minute=0, second=0, microsecond=0)
                                     == now_msk.replace(minute=0, second=0, microsecond=0)]
                        if not now_items:
                            now_items = [fl[0]]
                        today_fl = [i for i in fl
                                    if datetime.fromtimestamp(i["dt"], tz=MSK).date() == today
                                    and is_daylight(datetime.fromtimestamp(i["dt"], tz=MSK))]
                        jr = calc_jumprun(now_items)
                        msg_text = format_jumprun(jr, today_fl)
                        await tg_send(msg_text, chat_id=user_chat_id)
                    except Exception as e:
                        await tg_send(f"⚠️ Ошибка: {e}", chat_id=user_chat_id)
                elif text == "/weather":
                    stats["requests"] += 1
                    await tg_send("⏳ Загружаю прогноз...", chat_id=user_chat_id)
                    try:
                        msg_text = await make_message("сейчас")
                        await tg_send(msg_text, chat_id=user_chat_id)
                    except Exception as e:
                        await tg_send(f"⚠️ Ошибка: {e}", chat_id=user_chat_id)
                elif text and text.startswith("/set") and str(user_chat_id) == str(CHAT_ID):
                    parts = text.strip().split()
                    # /set clear — сбросить всё
                    if len(parts) == 2 and parts[1] == "clear":
                        manual_override.clear()
                        await tg_send("✅ Все ручные правки сброшены.", chat_id=user_chat_id)
                    # /set 11 clear — сбросить конкретный отрезок
                    elif len(parts) == 3 and parts[2] == "clear":
                        try:
                            slot = int(parts[1])
                            if slot in manual_override:
                                del manual_override[slot]
                                await tg_send(f"✅ Правки для отрезка {slot:02d}:00 сброшены.", chat_id=user_chat_id)
                            else:
                                await tg_send(f"Правок для отрезка {slot:02d}:00 нет.", chat_id=user_chat_id)
                        except ValueError:
                            await tg_send("❌ Неверный формат. Пример: /set 11 clear", chat_id=user_chat_id)
                    # /set wind 3.5 — установить правку для текущего отрезка
                    elif len(parts) == 3:
                        try:
                            param = parts[1]
                            val = float(parts[2])
                            # Определить текущий отрезок
                            now_h = datetime.now(tz=MSK).hour
                            current_slot = None
                            for s in VALID_SLOTS:
                                if s <= now_h < s + 3:
                                    current_slot = s
                                    break
                            if current_slot is None:
                                await tg_send("⛔️ Сейчас не световой день (08:00-20:00). Правки недоступны.", chat_id=user_chat_id)
                            elif param not in ["wind", "wind120", "gusts", "clouds"]:
                                await tg_send(f"❌ Неизвестный параметр: {param}\nДоступны: wind, wind120, gusts, clouds", chat_id=user_chat_id)
                            else:
                                if current_slot not in manual_override:
                                    manual_override[current_slot] = {}
                                manual_override[current_slot][param] = int(val) if param == "clouds" else val
                                ov = manual_override[current_slot]
                                parts_ov = []
                                if ov.get("wind") is not None: parts_ov.append(f"💨{ov['wind']}м/с")
                                if ov.get("wind120") is not None: parts_ov.append(f"↕{ov['wind120']}м/с")
                                if ov.get("gusts") is not None: parts_ov.append(f"пор.{ov['gusts']}")
                                if ov.get("clouds") is not None: parts_ov.append(f"☁️{ov['clouds']}%")
                                await tg_send(
                                    f"✅ Правка для {current_slot:02d}:00-{current_slot+3:02d}:00:\n"
                                    + " ".join(parts_ov)
                                    + "\n\n/set clear — сбросить все",
                                    chat_id=user_chat_id
                                )
                        except ValueError:
                            await tg_send("❌ Неверный формат. Пример: /set wind 3.5", chat_id=user_chat_id)
                    else:
                        now_h = datetime.now(tz=MSK).hour
                        current_slot = next((s for s in VALID_SLOTS if s <= now_h < s + 3), None)
                        slot_str = f"{current_slot:02d}:00-{current_slot+3:02d}:00" if current_slot else "нет активного отрезка"
                        await tg_send(
                            f"📝 Правка текущего отрезка ({slot_str}):\n\n"
                            "/set wind 3.5 — ветер у земли м/с\n"
                            "/set wind120 8.0 — ветер на 120м м/с\n"
                            "/set gusts 5.0 — порывы м/с\n"
                            "/set clouds 15 — облачность %\n"
                            "/set clear — сбросить все правки",
                            chat_id=user_chat_id
                        )
                elif text and text.startswith("/set"):
                    await tg_send("⛔️ Команда только для администратора", chat_id=user_chat_id)
                elif text == "/stats":
                    if str(user_chat_id) == str(CHAT_ID):
                        uptime = datetime.now(tz=MSK) - stats["started"]
                        hours = int(uptime.total_seconds() // 3600)
                        minutes = int((uptime.total_seconds() % 3600) // 60)
                        stats_msg = (
                            "📊 Статистика бота\n\n"
                            f"👥 Уникальных пользователей: {len(stats['users'])}\n"
                            f"📨 Запросов /weather: {stats['requests']}\n"
                            f"⏱ Работает: {hours}ч {minutes}мин\n"
                            f"🕐 Запущен: {stats['started'].strftime('%d.%m.%Y %H:%M')} МСК"
                        )
                        await tg_send(stats_msg, chat_id=user_chat_id)
                    else:
                        await tg_send("⛔️ Команда только для администратора", chat_id=user_chat_id)
        except Exception as e:
            logger.error(f"Polling ошибка: {e}")
            await asyncio.sleep(5)


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.ensure_future(send_forecast("утро ☀️")),
                      "cron", hour=MORNING_HOUR, minute=MORNING_MIN)
    scheduler.add_job(lambda: asyncio.ensure_future(send_forecast("вечер 🌆")),
                      "cron", hour=EVENING_HOUR, minute=EVENING_MIN)
    scheduler.start()
    logger.info("Бот запущен!")
    await poll_updates()


if __name__ == "__main__":
    asyncio.run(main())
