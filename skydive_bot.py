import os
import asyncio
import logging
from datetime import datetime
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENWEATHER_API_KEY = os.environ["OPENWEATHER_API_KEY"]
CHAT_ID = os.environ["CHAT_ID"]

LAT = 54.5167
LON = 40.9333
MORNING_HOUR = 6
MORNING_MIN = 0
EVENING_HOUR = 14
EVENING_MIN = 0

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
            "units": "metric", "lang": "ru", "cnt": 8
        }, timeout=10)
        return r.json()


def calc_prob(fl):
    scores = []
    for item in fl[:4]:
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
    return round(sum(scores) / len(scores)) if scores else 0


def wind_dir(deg):
    return ["С","СВ","В","ЮВ","Ю","ЮЗ","З","СЗ"][round(deg/45)%8]


def emoji(p):
    return "🟢" if p>=80 else "🟡" if p>=60 else "🟠" if p>=40 else "🔴"


async def make_message(label):
    data = await get_weather()
    fl = data["list"]
    prob = calc_prob(fl)
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
    lines = []
    for item in fl[:4]:
        t = datetime.utcfromtimestamp(item["dt"]).strftime("%H:%M")
        lines.append(f"  {t} | {round(item['main']['temp'])}°C | 💨{item['wind']['speed']}м/с | ☁️{item['clouds']['all']}% | {item['weather'][0]['description']}")
    return (
        f"🪂 *Прогноз — {label}*\n📍 Крутицы, Рязанская обл.\n\n"
        f"{emoji(prob)} *Вероятность: {prob}%*\n\n"
        f"🌤 {desc}\n🌡 {temp}°C (ощущается {feels}°C)\n"
        f"💨 {ws}м/с ({wdir}), порывы {gust:.1f}м/с\n"
        f"☁️ {clouds}% | 💧{hum}% | 👁{vis}км\n\n"
        f"⏱ *Ближайшие 12 часов:*\n" + "\n".join(lines) +
        f"\n\n_{datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC_"
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
                        "/weather — прогноз прямо сейчас"
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
