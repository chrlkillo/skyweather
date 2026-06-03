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
            "chat_id": target, "text": text, "parse_mode": "Markdown"
        }, timeout=10)


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
            "hourly": "temperature_2m,rain,cloud_cover,cloud_cover_low,cloud_cover_mid,wind_speed_10m,wind_speed_120m,wind_gusts_10m",
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
        item = {
            "dt": int(dt_msk.timestamp()),
            "main": {
                "temp": hourly["temperature_2m"][i] or 0,
                "feels_like": hourly["temperature_2m"][i] or 0,
                "humidity": 0,
            },
            "wind": {
                "speed": round(wind10, 1),
                "speed_100m": round(wind100, 1),
                "gust": round(gust, 1),
                "deg": 0,
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
    """Вероятность для опытных парашютистов."""
    if not fl:
        return None
    scores = []
    for item in fl:
        s = 100
        c = item["clouds"]["all"]
        if c > 80: s -= 40
        elif c > 50: s -= 20
        elif c > 30: s -= 10
        if "rain" in item or "snow" in item: s -= 50
        if item["weather"][0]["id"] < 700: s -= 40
        w = item["wind"]["speed"]
        if w > 10: s -= 40
        elif w > 7: s -= 25
        elif w > 5: s -= 10
        g = item["wind"].get("gust", 0)
        if g > 12: s -= 20
        elif g > 8: s -= 10
        v = item.get("visibility", 10000)
        if v < 3000: s -= 30
        elif v < 5000: s -= 15
        scores.append(max(0, s))
    return round(sum(scores) / len(scores))


def calc_aff_prob(fl):
    """Вероятность для AFF студентов: ветер/порывы <=7м/с, облачность <30%."""
    if not fl:
        return None
    scores = []
    for item in fl:
        s = 100
        c = item["clouds"]["all"]
        if c >= 30: s -= 60
        elif c >= 20: s -= 20
        elif c >= 10: s -= 10
        if "rain" in item or "snow" in item:
            s = 0
        elif item["weather"][0]["id"] < 700:
            s = 0
        else:
            w = item["wind"]["speed"]
            if w > 7: s = 0
            elif w > 5: s -= 40
            elif w > 3: s -= 15
            g = item["wind"].get("gust", 0)
            if g > 7: s = 0
            elif g > 5: s -= 30
            v = item.get("visibility", 10000)
            if v < 5000: s -= 50
            elif v < 8000: s -= 20
        scores.append(max(0, s))
    return round(sum(scores) / len(scores))


def wind_dir(deg):
    return ["С","СВ","В","ЮВ","Ю","ЮЗ","З","СЗ"][round(deg/45)%8]


def emoji(p):
    if p is None: return "⚫️"
    return "🟢" if p>=80 else "🟡" if p>=60 else "🟠" if p>=40 else "🔴"


def prob_str(p):
    return f"{p}%" if p is not None else "нет данных"


async def make_message(label):
    data = await get_weather()
    fl = data["list"]

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
    prob_today = calc_prob(today_fl)
    aff_today = calc_aff_prob(today_fl)

    # Вероятности завтра
    prob_tmrw = calc_prob(tomorrow_fl)
    aff_tmrw = calc_aff_prob(tomorrow_fl)

    # Вероятности послезавтра
    prob_da = calc_prob(day_after_fl)
    aff_da = calc_aff_prob(day_after_fl)

    def hour_lines(items):
        result = []
        for item in items:
            dt_msk = datetime.fromtimestamp(item["dt"], tz=MSK)
            t = dt_msk.strftime("%H:%M")
            w = item["wind"]["speed"]
            g = item["wind"].get("gust", w)
            c = item["clouds"]["all"]
            d = item["weather"][0]["description"]
            ind = emoji(calc_aff_prob([item]))
            w100 = item["wind"].get("speed_100m", 0)  # 120м в источнике
            result.append(f"{ind} {t} | {round(item['main']['temp'])}°C | 💨{w}м/с↕{w100:.1f}м/с (пор.{g:.1f}) | ☁️{c}% | {d}")
        return result

    today_str = today.strftime("%d.%m")
    tomorrow_str = tomorrow.strftime("%d.%m")
    day_after_str = day_after.strftime("%d.%m")

    _, s_name, s_artist, s_url = get_daily_soundtrack(data)

    def day_section(title, prob, aff, items):
        s = f"*{title}:*\n"
        s += f"{emoji(prob)} Опытные: *{prob_str(prob)}*\n"
        s += f"{emoji(aff)} AFF студенты: *{prob_str(aff)}*\n"
        lines = hour_lines(items)
        if lines:
            s += "\n".join(lines) + "\n"
        return s

    return (
        f"🪂 *Прогноз — {label}*\n📍 Крутицы, Рязанская обл.\n\n"
        + day_section(f"Сегодня {today_str}", prob_today, aff_today, today_fl)
        + "\n"
        + day_section(f"Завтра {tomorrow_str}", prob_tmrw, aff_tmrw, tomorrow_fl)
        + "\n"
        + day_section(f"Послезавтра {day_after_str}", prob_da, aff_da, day_after_fl)
        + f"\n🌤 {desc}\n🌡 {temp}°C (ощущается {feels}°C)\n"
        + f"💨 {ws}м/с ({wdir}), порывы {gust:.1f}м/с\n"
        + f"☁️ {clouds}% | 💧{hum}% | 👁{vis}км\n\n"
        + f"🎵 *Саундтрек дня:* [{s_name} — {s_artist}]({s_url})\n\n"
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
                if text == "/start":
                    await tg_send(
                        "🪂 *Бот прогноза для прыжков*\n\n"
                        "Прогноз дважды в день:\n🌅 09:00 МСК\n🌆 17:00 МСК\n\n"
                        "Команды:\n/weather — прогноз прямо сейчас\n\n"
                        "Показывает вероятности прыжков для AFF студентов 🎓",
                        chat_id=user_chat_id
                    )
                elif text == "/weather":
                    await tg_send("⏳ Загружаю прогноз...", chat_id=user_chat_id)
                    try:
                        msg_text = await make_message("сейчас")
                        await tg_send(msg_text, chat_id=user_chat_id)
                    except Exception as e:
                        await tg_send(f"⚠️ Ошибка: {e}", chat_id=user_chat_id)
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
