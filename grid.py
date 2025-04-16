import ccxt
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, filters
import pandas as pd
import numpy as np
import json
import os
import logging
import asyncio
import httpx
import time

# Налаштування логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Читання конфігурації
try:
    if os.getenv('CONFIG_JSON'):
        config = json.loads(os.getenv('CONFIG_JSON'))
    else:
        with open('config.json', 'r') as config_file:
            config = json.load(config_file)
    binance_api_key = config['BINANCE_API_KEY']
    binance_secret = config['BINANCE_SECRET']
    bot_token = config['TELEGRAM_BOT_TOKEN']
except Exception as e:
    logger.error(f"Помилка читання конфігурації: {e}")
    raise SystemExit("Перевірте config.json або змінну CONFIG_JSON")

# Налаштування Binance
binance = ccxt.binance({
    'apiKey': binance_api_key,
    'secret': binance_secret,
    'enableRateLimit': True,
})

# Функція аналізу пар
def analyze_pairs():
    logger.info("Завантажую ринки...")
    binance.load_markets()
    pairs = [pair for pair in binance.markets.keys() 
             if pair.endswith('/USDT:USDT') and binance.markets[pair]['swap'] and pair not in ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'BNB/USDT:USDT', 'TRX/USDT:USDT']]
    logger.info(f"Знайдено {len(pairs)} ф’ючерсних USDT-пар: {pairs[:5]}...")
    
    volumes = []
    for pair in pairs:
        try:
            ticker = binance.fetch_ticker(pair)
            volume = ticker.get('quoteVolume')
            if volume is not None and isinstance(volume, (int, float)):
                volumes.append((pair, volume))
            else:
                logger.warning(f"{pair} пропущено: відсутній або некоректний обсяг")
        except Exception as e:
            logger.error(f"Помилка для {pair} при отриманні обсягу: {e}")
            continue
    volumes.sort(key=lambda x: x[1], reverse=True)
    pairs = [p[0] for p in volumes[:50]]
    logger.info(f"Вибрано топ-50 пар за обсягом: {pairs[:5]}...")
    
    candidates = []
    for pair in pairs:
        try:
            logger.info(f"Аналізую {pair}...")
            ohlcv = binance.fetch_ohlcv(pair, '1h', limit=24)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            df['tr'] = np.maximum(df['high'] - df['low'], 
                                  np.maximum(abs(df['high'] - df['close'].shift()), 
                                             abs(df['low'] - df['close'].shift())))
            atr = df['tr'].mean()
            volatility = (atr / df['close'].iloc[-1]) * 100
            if np.isnan(volatility) or volatility == 0:
                logger.warning(f"{pair} пропущено: некоректна волатильність ({volatility}%)")
                continue
            logger.info(f"Волатильність {pair}: {volatility:.2f}%")
            
            ohlcv_3d = binance.fetch_ohlcv(pair, '4h', limit=18)
            df_3d = pd.DataFrame(ohlcv_3d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            price_range = (df_3d['high'].max() - df_3d['low'].min()) / df_3d['close'].iloc[-1] * 100
            logger.info(f"Діапазон {pair}: {price_range:.2f}%")
            
            ticker = binance.fetch_ticker(pair)
            volume_usd = ticker.get('quoteVolume')
            if volume_usd is None or not isinstance(volume_usd, (int, float)):
                logger.warning(f"{pair} пропущено: відсутні дані про обсяг")
                continue
            logger.info(f"Обсяг {pair}: ${volume_usd:,.2f}")
            
            market = binance.market(pair)
            min_price = market['limits']['price']['min'] if market['limits']['price']['min'] else 0.0001
            
            if (2 <= volatility <= 5 and 
                10 <= price_range <= 40 and 
                volume_usd >= 150_000_000):
                current_price = ticker['last']
                grid_range = price_range * 0.01 * current_price * 1.2
                lower_price = round(current_price - grid_range / 2, 4)
                upper_price = round(current_price + grid_range / 2, 4)
                
                candidates.append({
                    'pair': pair,
                    'volatility': volatility,
                    'current_price': current_price,
                    'lower_price': lower_price,
                    'upper_price': upper_price,
                    'grids': 15,
                    'volume_usd': volume_usd,
                    'min_price': min_price,
                })
                logger.info(f"{pair} додано до кандидатів")
            else:
                logger.info(f"{pair} відсіяно: волатильність={volatility:.2f}%, діапазон={price_range:.2f}%, обсяг=${volume_usd:,.2f}")
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"Помилка для {pair}: {e}")
            continue
    
    candidates = sorted(candidates, key=lambda x: x['volatility'], reverse=True)
    logger.info(f"Знайдено {len(candidates)} пар")
    return candidates

# Створення грид-бота
def create_grid_bot(pair, current_price, lower_price, upper_price, grids, leverage=10, capital=50):
    try:
        logger.info(f"Налаштування грид-бота для {pair}...")
        binance.set_leverage(leverage, pair)
        
        grid_levels = np.linspace(lower_price, upper_price, grids)
        position_size = capital * leverage / grids
        
        for level in grid_levels:
            try:
                if level < current_price:
                    order = binance.create_limit_buy_order(pair, position_size / level, level)
                    logger.info(f"Розміщено лонг-ордер: {pair} на ${level:.4f}, розмір: {position_size / level:.4f}")
                elif level > current_price:
                    order = binance.create_limit_sell_order(pair, position_size / level, level)
                    logger.info(f"Розміщено шорт-ордер: {pair} на ${level:.4f}, розмір: {position_size / level:.4f}")
            except Exception as e:
                logger.error(f"Помилка для ордера {pair} на ${level:.4f}: {e}")
                continue
        
        logger.info(f"Грид-бот для {pair} налаштовано: {grids} сіток")
        return True
    except Exception as e:
        logger.error(f"Помилка створення грид-бота для {pair}: {e}")
        return False

# Обробник /start
async def start(update, context):
    keyboard = [
        [InlineKeyboardButton("🔄 Оновити топ", callback_data='update_top')],
        [InlineKeyboardButton("ℹ️ Статус", callback_data='status')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👋 Вітаю! Готовий знайти пари для торгівлі? Вибери дію:", reply_markup=reply_markup)
    logger.info("Отримано команду /start")

# Обробник кнопок
async def button(update, context):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'update_top':
        await query.message.reply_text("🔍 Запускаю аналіз пар...")
        pairs = await send_top_pairs(query.message, context)
        if pairs:
            context.user_data['pairs'] = pairs
            keyboard = [[InlineKeyboardButton(f"📈 {p['pair'].replace(':USDT', '')}", callback_data=f"trade_{i}")]
                       for i, p in enumerate(pairs)]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text("Вибери пару для грид-бота:", reply_markup=reply_markup)
    elif query.data == 'status':
        await query.message.reply_text("🟢 Бот активний! Готовий до роботи.")
    elif query.data.startswith('trade_'):
        index = int(query.data.split('_')[1])
        pairs = context.user_data.get('pairs', [])
        if index < len(pairs):
            pair_data = pairs[index]
            pair = pair_data['pair']
            success = create_grid_bot(
                pair,
                pair_data['current_price'],
                pair_data['lower_price'],
                pair_data['upper_price'],
                pair_data['grids'],
            )
            if success:
                await query.message.reply_text(f"✅ Грид-бот для {pair.replace(':USDT', '')} запущено!")
            else:
                await query.message.reply_text(f"❌ Помилка запуску грид-бота для {pair.replace(':USDT', '')}.")
        else:
            await query.message.reply_text("⚠️ Пара не знайдена.")

# Функція відправки топ-пар
async def send_top_pairs(message, context):
    for attempt in range(3):
        try:
            pairs = analyze_pairs()
            if not pairs:
                await message.reply_text("⚠️ Не знайдено пар за критеріями: волатильність 2-5%, обсяг >$150M, діапазон 10-40%.")
                logger.warning("Не знайдено пар")
                return []
            
            response = f"📊 Топ-пари для Grid Trading (10x) ({len(pairs)}):\n\n"
            for i, p in enumerate(pairs, 1):
                pair_name = p['pair'].replace(':USDT', '')
                response += (f"{i}️⃣ {pair_name} 📈\n"
                            f"   💰 Ціна: ${p['current_price']:.4f}\n"
                            f"   ⚡ Волатильність: {p['volatility']:.2f}%\n"
                            f"   📏 Діапазон: ${p['lower_price']:.4f}-${p['upper_price']:.4f}\n"
                            f"   ⚠️ Мін. ціна: ${p['min_price']:.4f}\n\n")
            
            keyboard = [[InlineKeyboardButton("🔄 Оновити ще раз", callback_data='update_top')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await message.reply_text(response, reply_markup=reply_markup)
            logger.info(f"Відправлено {len(pairs)} пар")
            return pairs
        except httpx.ReadError as e:
            logger.error(f"Мережева помилка, спроба {attempt + 1}: {e}")
            await asyncio.sleep(2)
    await message.reply_text("⚠️ Помилка мережі. Спробуй ще раз пізніше.")
    logger.error("Помилка мережі після 3 спроб")
    return []

# Налаштування бота
def main():
    logger.info("Запускаю бот...")
    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.run_polling()

if __name__ == '__main__':
    main()