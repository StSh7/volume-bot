import asyncio
import aiohttp
import os
from datetime import datetime

# ═══════════════════════════════════════════════════════════
# Volume + Liquidation Signal Bot
# Таймфрейм: 1 година | Платформа: Railway.app
# ═══════════════════════════════════════════════════════════

# ─── НАЛАШТУВАННЯ ───────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # токен від @BotFather
CHAT_ID        = os.getenv("CHAT_ID")          # ваш chat_id від @userinfobot
COINGLASS_KEY  = os.getenv("COINGLASS_KEY")    # ключ з coinglass.com/api

# Монети для моніторингу
SYMBOLS = ["BTC", "ETH", "SOL", "BNB"]

# Налаштування пікового об'єму
VOLUME_MULTIPLIER = 2.5   # пік = поточний об'єм > середнього в 2.5 рази
LOOKBACK_CANDLES  = 20    # скільки свічок для розрахунку середнього

# Налаштування ліквідацій
LIQ_THRESHOLD_USD   = 5_000_000  # мін. кластер ліквідацій ($5M)
PRICE_PROXIMITY_PCT = 0.5        # скільки % від ціни вважається "поруч"

# Інтервал перевірки — 5 хвилин (таймфрейм 1H)
CHECK_INTERVAL = 300

# ─── URL ────────────────────────────────────────────────────
BINANCE_URL   = "https://fapi.binance.com/fapi/v1/klines"  # ф'ючерси (USDT-M Perpetual)
COINGLASS_URL = "https://open-api.coinglass.com/public/v2/liquidation_history"
TELEGRAM_URL  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# ─── ОТРИМАННЯ СВІЧОК З BINANCE (1H) ────────────────────────
async def get_candles(session, symbol):
    params = {
        "symbol":   f"{symbol}USDT",
        "interval": "1h",           # таймфрейм 1 година
        "limit":    LOOKBACK_CANDLES + 1
    }
    try:
        async with session.get(BINANCE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if not isinstance(data, list):
                print(f"[BINANCE ERROR] {symbol}: {data}")
                return []
            return [{
                "close":  float(c[4]),
                "volume": float(c[5])
            } for c in data]
    except Exception as e:
        print(f"[BINANCE ERROR] {symbol}: {e}")
        return []

# ─── ОТРИМАННЯ ЛІКВІДАЦІЙ З COINGLASS ───────────────────────
async def get_liquidations(session, symbol):
    headers = {"coinglassSecret": COINGLASS_KEY}
    params  = {"symbol": symbol, "interval": "1h"}
    try:
        async with session.get(COINGLASS_URL, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            return data.get("data", [])
    except Exception as e:
        print(f"[COINGLASS ERROR] {symbol}: {e}")
        return []

# ─── ВИЗНАЧЕННЯ ПІКОВОГО ОБ'ЄМУ ─────────────────────────────
def detect_volume_peak(candles):
    if len(candles) < 2:
        return False, 0, 0

    # Поточна свічка (остання, може бути незакрита)
    current = candles[-1]

    # Попередні свічки для розрахунку середнього
    history = candles[:-1]

    avg_vol = sum(c["volume"] for c in history) / len(history)
    is_peak = current["volume"] >= avg_vol * VOLUME_MULTIPLIER

    return is_peak, current["volume"], avg_vol

# ─── ПОШУК КЛАСТЕРІВ ЛІКВІДАЦІЙ ПОРУЧ З ЦІНОЮ ───────────────
def find_nearby_liquidations(liq_data, current_price):
    threshold = current_price * (PRICE_PROXIMITY_PCT / 100)
    clusters  = []

    for item in liq_data:
        try:
            price_level = float(item.get("price", 0))
            liq_usd     = float(item.get("liquidationUsd", 0))
        except (TypeError, ValueError):
            continue

        if liq_usd < LIQ_THRESHOLD_USD:
            continue

        distance = abs(price_level - current_price)
        if distance <= threshold:
            # Лонги ліквідуються коли ціна падає (рівень нижче поточної)
            # Шорти ліквідуються коли ціна росте (рівень вище поточної)
            side = "LONG" if price_level < current_price else "SHORT"
            clusters.append({
                "price":    price_level,
                "usd":      liq_usd,
                "side":     side,
                "distance": distance
            })

    # Сортуємо за розміром — найбільший кластер перший
    return sorted(clusters, key=lambda x: x["usd"], reverse=True)

# ─── ФОРМУВАННЯ ПОВІДОМЛЕННЯ ─────────────────────────────────
def build_message(symbol, price, volume, avg_vol, cluster):
    ratio  = volume / avg_vol if avg_vol > 0 else 0
    side   = cluster["side"]
    liq_m  = cluster["usd"] / 1_000_000
    dist_pct = (cluster["distance"] / price) * 100

    if side == "LONG":
        signal  = "🟢 BUY"
        reason  = "Великий кластер ліквідацій лонгів знизу\nЦіна може відскочити вгору"
    else:
        signal  = "🔴 SELL"
        reason  = "Великий кластер ліквідацій шортів зверху\nЦіна може відскочити вниз"

    # Форматування об'єму
    if volume >= 1_000_000_000:
        vol_str = f"{volume/1_000_000_000:.2f}B"
    elif volume >= 1_000_000:
        vol_str = f"{volume/1_000_000:.2f}M"
    elif volume >= 1_000:
        vol_str = f"{volume/1_000:.1f}K"
    else:
        vol_str = f"{volume:.0f}"

    now = datetime.utcnow().strftime("%H:%M UTC")

    return (
        f"{signal} <b>{symbol}USDT</b> · 1H\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ціна:       <b>${price:,.2f}</b>\n"
        f"📊 Об'єм:      {vol_str} ({ratio:.1f}× середнього)\n"
        f"💥 Ліквідації: ${liq_m:.1f}M на рівні ${cluster['price']:,.2f}\n"
        f"📏 Відстань:   {dist_pct:.2f}% від ціни\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {reason}\n"
        f"🕐 {now}"
    )

# ─── НАДСИЛАННЯ ПОВІДОМЛЕННЯ В TELEGRAM ─────────────────────
async def send_telegram(session, message):
    payload = {
        "chat_id":    CHAT_ID,
        "text":       message,
        "parse_mode": "HTML"
    }
    try:
        async with session.post(TELEGRAM_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            result = await r.json()
            if not result.get("ok"):
                print(f"[TELEGRAM ERROR] {result}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ─── ЗАХИСТ ВІД ДУБЛЮВАННЯ СИГНАЛІВ ─────────────────────────
# Зберігаємо останній сигнал для кожної монети
last_signals = {}

def is_duplicate(symbol, price, side):
    key = f"{symbol}_{round(price, -2)}_{side}"
    if last_signals.get(symbol) == key:
        return True
    last_signals[symbol] = key
    return False

# ─── ПЕРЕВІРКА ОДНІЄЇ МОНЕТИ ─────────────────────────────────
async def check_symbol(session, symbol):
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Перевірка {symbol}...")

    # 1. Отримуємо свічки
    candles = await get_candles(session, symbol)
    if not candles:
        return

    # 2. Перевіряємо піковий об'єм
    is_peak, volume, avg_vol = detect_volume_peak(candles)
    if not is_peak:
        print(f"  {symbol}: об'єм звичайний ({volume/avg_vol:.1f}× середнього)")
        return

    print(f"  {symbol}: ⚡ ПІКОВИЙ ОБ'ЄМ ({volume/avg_vol:.1f}× середнього)")

    # 3. Отримуємо ліквідації
    current_price = candles[-1]["close"]
    liq_data      = await get_liquidations(session, symbol)
    clusters      = find_nearby_liquidations(liq_data, current_price)

    if not clusters:
        print(f"  {symbol}: пік є, але кластерів ліквідацій поруч немає")
        return

    # 4. Беремо найбільший кластер
    top = clusters[0]
    print(f"  {symbol}: знайдено кластер ${top['usd']/1e6:.1f}M ({top['side']}) на ${top['price']:,.2f}")

    # 5. Перевіряємо дублювання
    if is_duplicate(symbol, current_price, top["side"]):
        print(f"  {symbol}: сигнал вже надсилався, пропускаємо")
        return

    # 6. Надсилаємо сигнал
    message = build_message(symbol, current_price, volume, avg_vol, top)
    await send_telegram(session, message)
    print(f"  {symbol}: ✅ Сигнал надіслано!")

# ─── ГОЛОВНИЙ ЦИКЛ ───────────────────────────────────────────
async def main():
    print("=" * 50)
    print("  Volume + Liquidation Signal Bot")
    print(f"  Монети: {', '.join(SYMBOLS)}")
    print(f"  Таймфрейм: 1H")
    print(f"  Перевірка кожні: {CHECK_INTERVAL // 60} хв")
    print(f"  Мін. множник об'єму: {VOLUME_MULTIPLIER}x")
    print(f"  Мін. кластер ліквідацій: ${LIQ_THRESHOLD_USD/1e6:.0f}M")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        # Надсилаємо повідомлення про старт
        await send_telegram(session,
            "🤖 <b>Бот запущено!</b>\n"
            f"Моніторинг: {', '.join(SYMBOLS)}\n"
            f"Таймфрейм: 1H\n"
            f"Перевірка кожні {CHECK_INTERVAL // 60} хв"
        )

        while True:
            print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Початок перевірки...")

            # Перевіряємо всі монети паралельно
            tasks = [check_symbol(session, symbol) for symbol in SYMBOLS]
            await asyncio.gather(*tasks, return_exceptions=True)

            print(f"Наступна перевірка через {CHECK_INTERVAL // 60} хв...\n")
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
