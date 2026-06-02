const TelegramBot = require("node-telegram-bot-api");

// =============================================
// КОНФИГУРАЦИЯ (читается из переменных окружения Railway)
// =============================================
const BOT_TOKEN = process.env.BOT_TOKEN;
const CHAT_ID   = process.env.CHAT_ID;

const WEATHER_URL =
  "https://dubaiweather.dm.ae/ims/web/pages/customer/uae_537/envidb/currentData/currentData?stationName=Jabal%20Ali";

const WIND_THRESHOLD_MS  = 5.5;    // Порог скорости ветра, м/с
const ALERT_DURATION_MIN = 30;     // Сколько минут подряд превышение → тревога
const POLL_INTERVAL_MS   = 60_000; // Опрос каждую минуту

// Если API отдаёт скорость в км/ч — поставьте true
const WIND_UNIT_KMH = false;
// =============================================

if (!BOT_TOKEN) throw new Error("Переменная окружения BOT_TOKEN не задана!");
if (!CHAT_ID)   throw new Error("Переменная окружения CHAT_ID не задана!");

const bot = new TelegramBot(BOT_TOKEN, { polling: true });

// --- Состояние мониторинга ---
let alertActive    = false;
let exceedingSince = null;
let lastWindSpeed  = null;

// --- Возможные названия поля скорости ветра ---
const WIND_KEYS = [
  "windSpeed", "wind_speed", "WindSpeed", "WINDSPEED",
  "ws", "WS", "Ws",
  "meanWindSpeed", "mean_wind_speed",
  "avgWindSpeed",  "avg_wind_speed",
  "windVelocity",  "wind_velocity",
  "speed", "Speed",
];

// =============================================
// Утилиты
// =============================================

function findWindSpeed(obj) {
  if (!obj || typeof obj !== "object") return null;

  for (const key of WIND_KEYS) {
    if (obj[key] !== undefined && obj[key] !== null && obj[key] !== "") {
      return parseFloat(obj[key]);
    }
  }

  for (const val of Object.values(obj)) {
    if (Array.isArray(val)) {
      for (const item of val) {
        const found = findWindSpeed(item);
        if (found !== null) return found;
      }
    } else if (typeof val === "object") {
      const found = findWindSpeed(val);
      if (found !== null) return found;
    }
  }

  return null;
}

function formatDuration(ms) {
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return min > 0 ? `${min} мин ${sec} сек` : `${sec} сек`;
}

function nowStr() {
  return new Date().toLocaleString("ru-RU", {
    timeZone: "Asia/Dubai",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    day: "2-digit", month: "2-digit",
  });
}

// =============================================
// Получение данных
// =============================================

async function fetchWindSpeed() {
  const res = await fetch(WEATHER_URL, {
    headers: {
      "User-Agent": "Mozilla/5.0 (compatible; WindMonitorBot/1.0)",
      Accept: "application/json, */*",
    },
  });

  if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);

  const data = await res.json();
  let speed = findWindSpeed(data);

  if (speed === null) throw new Error("Поле wind speed не найдено в ответе API");
  if (WIND_UNIT_KMH) speed = speed / 3.6;

  return speed;
}

// =============================================
// Основной цикл мониторинга
// =============================================

async function checkWind() {
  let speed;

  try {
    speed = await fetchWindSpeed();
  } catch (err) {
    console.error(`[${nowStr()}] Ошибка получения данных: ${err.message}`);
    return;
  }

  lastWindSpeed = speed;
  const exceeds = speed > WIND_THRESHOLD_MS;
  const alertDurationMs = ALERT_DURATION_MIN * 60 * 1000;

  console.log(`[${nowStr()}] Ветер: ${speed.toFixed(2)} м/с | Превышение: ${exceeds} | Тревога: ${alertActive}`);

  if (exceeds) {
    if (!exceedingSince) {
      exceedingSince = Date.now();
      console.log(`[${nowStr()}] ⚠️  Начало превышения порога`);
    }

    const duration = Date.now() - exceedingSince;

    if (duration >= alertDurationMs && !alertActive) {
      alertActive = true;
      await bot.sendMessage(
        CHAT_ID,
        `🚨 *ВНИМАНИЕ! Сильный ветер*\n\n` +
        `📍 Станция: Jabal Ali, Дубай\n` +
        `💨 Скорость ветра: *${speed.toFixed(2)} м/с*\n` +
        `⚠️ Порог: ${WIND_THRESHOLD_MS} м/с\n` +
        `⏱ Превышение длится: *${formatDuration(duration)}*\n` +
        `🕐 ${nowStr()}`,
        { parse_mode: "Markdown" }
      );
      console.log(`[${nowStr()}] 🚨 Тревога отправлена`);
    }
  } else {
    if (alertActive) {
      const duration = exceedingSince ? Date.now() - exceedingSince : 0;
      await bot.sendMessage(
        CHAT_ID,
        `✅ *Ветер успокоился*\n\n` +
        `📍 Станция: Jabal Ali, Дубай\n` +
        `💨 Текущая скорость: *${speed.toFixed(2)} м/с*\n` +
        `⏱ Превышение длилось: *${formatDuration(duration)}*\n` +
        `🕐 ${nowStr()}`,
        { parse_mode: "Markdown" }
      );
      console.log(`[${nowStr()}] ✅ Отбой отправлен`);
    }

    exceedingSince = null;
    alertActive    = false;
  }
}

// =============================================
// Команды бота
// =============================================

bot.onText(/\/start/, async (msg) => {
  await bot.sendMessage(
    msg.chat.id,
    `👋 *Wind Monitor Bot*\n\n` +
    `Слежу за скоростью ветра на станции *Jabal Ali* (Дубай).\n\n` +
    `⚠️ Уведомление приходит, если ветер превышает *${WIND_THRESHOLD_MS} м/с* на протяжении *${ALERT_DURATION_MIN} минут*.\n\n` +
    `📋 Команды:\n` +
    `/wind — текущая скорость ветра\n` +
    `/status — состояние мониторинга\n` +
    `/mychatid — узнать свой Chat ID`,
    { parse_mode: "Markdown" }
  );
});

bot.onText(/\/mychatid/, (msg) => {
  bot.sendMessage(msg.chat.id, `Ваш Chat ID: \`${msg.chat.id}\``, { parse_mode: "Markdown" });
});

bot.onText(/\/wind/, async (msg) => {
  const chatId = msg.chat.id;
  const loading = await bot.sendMessage(chatId, "⏳ Получаю данные...");

  try {
    const speed = await fetchWindSpeed();
    lastWindSpeed = speed;
    const statusIcon = speed > WIND_THRESHOLD_MS ? "⚠️ Выше порога!" : "✅ В норме";

    await bot.editMessageText(
      `💨 *Скорость ветра — Jabal Ali*\n\n` +
      `Скорость: *${speed.toFixed(2)} м/с*\n` +
      `Порог: ${WIND_THRESHOLD_MS} м/с\n` +
      `Статус: ${statusIcon}\n` +
      `🕐 ${nowStr()}`,
      { chat_id: chatId, message_id: loading.message_id, parse_mode: "Markdown" }
    );
  } catch (err) {
    await bot.editMessageText(`❌ Ошибка: ${err.message}`, {
      chat_id: chatId, message_id: loading.message_id,
    });
  }
});

bot.onText(/\/status/, (msg) => {
  const windStr   = lastWindSpeed !== null ? `${lastWindSpeed.toFixed(2)} м/с` : "нет данных";
  const exceedStr = exceedingSince ? `Да, ${formatDuration(Date.now() - exceedingSince)}` : "Нет";

  bot.sendMessage(
    msg.chat.id,
    `📊 *Статус мониторинга*\n\n` +
    `💨 Последнее значение: *${windStr}*\n` +
    `⚠️ Порог: ${WIND_THRESHOLD_MS} м/с\n` +
    `📍 Превышение сейчас: ${exceedStr}\n` +
    `🚨 Тревога активна: ${alertActive ? "Да" : "Нет"}\n` +
    `🔄 Интервал опроса: каждые ${POLL_INTERVAL_MS / 1000} сек\n` +
    `🕐 ${nowStr()}`,
    { parse_mode: "Markdown" }
  );
});

// =============================================
// Запуск
// =============================================

checkWind();
setInterval(checkWind, POLL_INTERVAL_MS);

console.log(`✅ Бот запущен`);
console.log(`📡 Опрос каждые ${POLL_INTERVAL_MS / 1000} сек`);
console.log(`⚠️  Порог: ${WIND_THRESHOLD_MS} м/с на протяжении ${ALERT_DURATION_MIN} мин`);
