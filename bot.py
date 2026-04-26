import asyncio
import aiohttp
import json
import os
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════
# Volume + Liquidation Signal Bot
# Таймфрейм: 1 година | Біржа: Bybit Futures
# ═══════════════════════════════════════════════════════════

# ─── НАЛАШТУВАННЯ ───────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
COINGLASS_KEY  = os.getenv("COINGLASS_KEY")

SYMBOLS = ["BTC", "ETH", "SOL", "BNB"]

VOLUME_MULTIPLIER   = 2.5
LOOKBACK_CANDLES    = 20
LIQ_THRESHOLD_USD   = 5_000_000
PRICE_PROXIMITY_PCT = 0.5
CHECK_INTERVAL      = 300

# ─── URL ────────────────────────────────────────────────────
BYBIT_URL     = "https://api.bybit.com/v5/market/kline"
COINGLASS_URL = "https://open-api.coinglass.com/public/v2/liquidation_history"

# ─── ОТРИМАННЯ СВІЧОК З BYBIT ───────────────────────────────
async def get_candles(session, symbol):
    params = {
        "category": "linear",
        "symbol":   f"{symbol}USDT",
        "interval": "60",
        "limit":    str(LOOKBACK_CANDLES + 1)
    }
    try:
        async with session.get(BYBIT_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
            data = json.loads(text)

            if data.get("retCode") != 0:
                print(f"[BYBIT ERROR] {symbol}: {data.get('retMsg')}")
                return []

            candles = data.get("result", {}).get("list", [])
            candles = list(reversed(candles))
            return [{
                "close":  float(c[4]),
                "volume": float(c[5])
            } for c in candles]

    except Exception as e:
        print(f"[BYBIT ERROR] {symbol}: {e}")
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
    current = candles[-1]
    history = candles[:-1]
    avg_vol = sum(c["volume"] for c in history) / len(history)
    is_peak = current["volume"] >= avg_vol * VOLUME_MULTIPLIER
    return is_peak, current["volume"], avg_vol

# ─── ПОШУК КЛАСТЕРІВ ЛІКВІДАЦІЙ ─────────────────────────────
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
            side = "LONG" if price_level < current_price else "SHORT"
            clusters.append({
                "price":    price_level,
                "usd":      liq_usd,
                "side":     side,
                "distance": distance
            })
    return sorted(clusters, key=lambda x: x["usd"], reverse=True)

# ─── ФОРМУВАННЯ ПОВІДОМЛЕННЯ ─────────────────────────────────
def build_message(symbol, price, volume, avg_vol, cluster):
    ratio    = volume / avg_vol if avg_vol > 0 else 0
    side     = cluster["side"]
    liq_m    = cluster["usd"] / 1_000_000
    dist_pct = (cluster["distance"] / price) * 100

    signal = "🟢 BUY"  if side == "LONG"  else "🔴 SELL"
    reason = "Кластер ліквідацій лонгів знизу — відскок вгору" \
             if side == "LONG" else \
             "Кластер ліквідацій шортів зверху — відскок вниз"

    if volume >= 1_000_000_000:
        vol_str = f"{volume/1_000_000_000:.2f}B"
    elif volume >= 1_000_000:
        vol_str = f"{volume/1_000_000:.2f}M"
    elif volume >= 1_000:
        vol_str = f"{volume/1_000:.1f}K"
    else:
        vol_str = f"{volume:.0f}"

    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return (
        f"{signal} <b>{symbol}USDT</b> · 1H · Bybit\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ціна:       <b>${price:,.2f}</b>\n"
        f"📊 Об'єм:      {vol_str} ({ratio:.1f}× середнього)\n"
        f"💥 Ліквідації: ${liq_m:.1f}M на ${cluster['price']:,.2f}\n"
        f"📏 Відстань:   {dist_pct:.2f}% від ціни\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {reason}\n"
        f"🕐 {now}"
    )

# ─── НАДСИЛАННЯ В TELEGRAM ───────────────────────────────────
async def send_telegram(session, message):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            result = await r.json()
            if not result.get("ok"):
                print(f"[TELEGRAM ERROR] {result}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ─── ЗАХИСТ ВІД ДУБЛЮВАННЯ ───────────────────────────────────
last_signals = {}

def is_duplicate(symbol, price, side):
    key = f"{symbol}_{round(price, -2)}_{side}"
    if last_signals.get(symbol) == key:
        return True
    last_signals[symbol] = key
    return False

# ─── ПЕРЕВІРКА ОДНІЄЇ МОНЕТИ ─────────────────────────────────
async def check_symbol(session, symbol):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now}] Перевірка {symbol}...")

    candles = await get_candles(session, symbol)
    if not candles:
        return

    is_peak, volume, avg_vol = detect_volume_peak(candles)
    ratio = volume / avg_vol if avg_vol > 0 else 0

    if not is_peak:
        print(f"  {symbol}: звичайний об'єм ({ratio:.1f}x)")
        return

    print(f"  {symbol}: ПІКОВИЙ ОБ'ЄМ ({ratio:.1f}x середнього)")

    current_price = candles[-1]["close"]
    liq_data      = await get_liquidations(session, symbol)
    clusters      = find_nearby_liquidations(liq_data, current_price)

    if not clusters:
        print(f"  {symbol}: пік є, кластерів ліквідацій поруч немає")
        return

    top = clusters[0]
    print(f"  {symbol}: кластер ${top['usd']/1e6:.1f}M ({top['side']}) на ${top['price']:,.2f}")

    if is_duplicate(symbol, current_price, top["side"]):
        print(f"  {symbol}: сигнал вже надсилався, пропускаємо")
        return

    message = build_message(symbol, current_price, volume, avg_vol, top)
    await send_telegram(session, message)
    print(f"  {symbol}: сигнал надіслано!")

# ─── ГОЛОВНИЙ ЦИКЛ ───────────────────────────────────────────
async def main():
    print("=" * 50)
    print("  Volume + Liquidation Signal Bot")
    print(f"  Монети:    {', '.join(SYMBOLS)}")
    print(f"  Біржа:     Bybit Futures (Linear)")
    print(f"  Таймфрейм: 1H")
    print(f"  Перевірка: кожні {CHECK_INTERVAL // 60} хв")
    print(f"  Множник:   {VOLUME_MULTIPLIER}x")
    print(f"  Мін. ліквідації: ${LIQ_THRESHOLD_USD/1e6:.0f}M")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        await send_telegram(session,
            "🤖 <b>Бот запущено!</b>\n"
            f"Біржа: Bybit Futures\n"
            f"Монети: {', '.join(SYMBOLS)}\n"
            f"Таймфрейм: 1H\n"
            f"Перевірка кожні {CHECK_INTERVAL // 60} хв"
        )

        while True:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{now}] Початок перевірки...")
            tasks = [check_symbol(session, s) for s in SYMBOLS]
            await asyncio.gather(*tasks, return_exceptions=True)
            print(f"Наступна перевірка через {CHECK_INTERVAL // 60} хв...")
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
