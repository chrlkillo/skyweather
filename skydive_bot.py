import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENWEATHER_API_KEY = os.environ["OPENWEATHER_API_KEY"]
CHAT_ID = os.environ["CHAT_ID"]

LAT = 54.5167
LON = 40.9333
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


async def tg_send(text):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/sendMessage", json={
            "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"
        }, timeout=10)


async def tg_get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TG_API}/getUpdates", params=params, timeout=35)
        return r.json()


async def get_weather():
    async with httpx.AsyncClient() as client:
        r = await client.get("https://api.openweathermap.org/data/2.5/forecast", params={
            "lat": LAT, "lon": LON, "appid": OPENWEATHER_API_KEY,
            "units": "metric", "lang": "ru", "cnt": 24
        }, timeout=10)
        return r.json()


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

    # Почасовой прогноз — только световой день
    daylight_items = [i for i in fl if is_daylight(datetime.fromtimestamp(i["dt"], tz=MSK))][:6]
    lines = []
    for item in daylight_items:
        dt_msk = datetime.fromtimestamp(item["dt"], tz=MSK)
        t = dt_msk.strftime("%H:%M")
        w = item["wind"]["speed"]
        g = item["wind"].get("gust", w)
        c = item["clouds"]["all"]
        d = item["weather"][0]["description"]
        aff_item = calc_aff_prob([item])
        aff_ind = emoji(aff_item)
        lines.append(f"{aff_ind} {t} | {round(item['main']['temp'])}°C | 💨{w}м/с (пор.{g:.1f}) | ☁️{c}% | {d}")

    today_str = today.strftime("%d.%m")
    tomorrow_str = tomorrow.strftime("%d.%m")

    return (
        f"🪂 *Прогноз — {label}*\n📍 Крутицы, Рязанская обл.\n\n"
        f"*Сегодня {today_str}:*\n"
        f"{emoji(prob_today)} Опытные: *{prob_str(prob_today)}*\n"
        f"{emoji(aff_today)} AFF студенты: *{prob_str(aff_today)}*\n\n"
        f"*Завтра {tomorrow_str}:*\n"
        f"{emoji(prob_tmrw)} Опытные: *{prob_str(prob_tmrw)}*\n"
        f"{emoji(aff_tmrw)} AFF студенты: *{prob_str(aff_tmrw)}*\n\n"
        f"🌤 {desc}\n🌡 {temp}°C (ощущается {feels}°C)\n"
        f"💨 {ws}м/с ({wdir}), порывы {gust:.1f}м/с\n"
        f"☁️ Облачность: {clouds}% | 💧{hum}% | 👁{vis}км\n\n"
        f"⏱ *Прогноз по часам:*\n" + "\n".join(lines) +
        f"\n\n_{now_msk.strftime('%d.%m.%Y %H:%M')} МСК_"
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
                if text == "/start":
                    await tg_send(
                        "🪂 *Бот прогноза для парашютистов*\n\n"
                        "Прогноз дважды в день:\n🌅 09:00 МСК\n🌆 17:00 МСК\n\n"
                        "Команды:\n/weather — прогноз прямо сейчас\n\n"
                        "Показывает вероятности для опытных и AFF студентов на сегодня и завтра 🎓"
                    )
                elif text == "/weather":
                    await tg_send("⏳ Загружаю прогноз...")
                    try:
                        msg_text = await make_message("сейчас")
                        await tg_send(msg_text)
                    except Exception as e:
                        await tg_send(f"⚠️ Ошибка: {e}")
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
