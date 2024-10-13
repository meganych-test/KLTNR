import os
import time
import requests
import ccxt
import pandas as pd
from dotenv import load_dotenv
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

# Load environment variables
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Initialize Binance API
binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
})

# Define trading parameters
TRADING_PAIRS = ['FTT/USDT', 'ALPACA/USDT', 'PEPE/USDT', 'TIA/USDT']
RSI_PERIOD = 10
EMA_PERIOD = 10
ATR_PERIOD = 14
ATR_MULTIPLIER = 2

# Capital variables
INITIAL_CAPITAL = 500  # Set initial capital to 500 USDT
INITIAL_TRADE_AMOUNT = 10  # First buy is fixed at $10
FIRST_SAFETY_ORDER_AMOUNT = 30  # First safety order amount is fixed at $30
SAFETY_ORDER_MULTIPLIER = 2  # Each safety order is double the previous one
SAFETY_ORDER_COUNT = 4  # Number of safety orders to place
PROFIT_TARGET = 0.01  # Profit target (1%)
INITIAL_SAFETY_ORDER_DROP = 0.02  # Initial price drop percentage for safety orders (2%)

# Fetch Binance markets to get minimum notional values
markets = binance.load_markets()
min_notional_values = {pair: market['limits']['cost']['min'] for pair, market in markets.items() if
                       pair in TRADING_PAIRS}


# Function to send messages to Telegram
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': message
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send message to Telegram: {e}")


# Function to fetch the historical price data and calculate RSI and Keltner Channel
def fetch_data(pair):
    ohlcv = binance.fetch_ohlcv(pair, timeframe='3m', limit=RSI_PERIOD + ATR_PERIOD + 1)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = df['close'].astype(float)

    if df['close'].eq(0).all():
        print(f"Error: All close prices are 0 for {pair}. Skipping calculations.")
        return None, None, None, None

    rsi = RSIIndicator(df['close'], window=RSI_PERIOD).rsi().iloc[-1]
    ema = EMAIndicator(df['close'], window=EMA_PERIOD).ema_indicator().iloc[-1]
    atr = AverageTrueRange(df['high'], df['low'], df['close'], window=ATR_PERIOD).average_true_range().iloc[-1]

    upper_band = ema + (atr * ATR_MULTIPLIER)
    lower_band = ema - (atr * ATR_MULTIPLIER)

    return round(rsi, 2), round(ema, 8), round(upper_band, 8), round(lower_band, 8)


# Function to place orders
def place_order(pair, order_type, amount=None):
    try:
        current_price = binance.fetch_ticker(pair)['last']
        print(f"Current price for {pair}: {current_price}")

        if order_type == 'buy':
            print(f"Attempting to place {order_type} order for {pair}")
            print(f"Dollar amount: ${amount}")

            quantity = amount / current_price

            if amount < min_notional_values.get(pair, 0):
                return None, f"Order not placed: Amount ${amount:.2f} is below the minimum required ${min_notional_values.get(pair, 0):.2f} for {pair}."

            order = binance.create_market_buy_order(pair, quantity)
            message = f"Placed BUY order for ${amount:.2f} of {pair} ({quantity:.8f} coins) at {current_price:.2f}."

            # Place take profit order
            tp_price = current_price * (1 + PROFIT_TARGET)
            tp_order = binance.create_limit_sell_order(pair, quantity, tp_price)
            message += f" Take profit order placed at ${tp_price:.8f}."

            return order, tp_order, message

        elif order_type == 'sell':
            balance = binance.fetch_balance()
            position_size = balance[pair.split('/')[0]]['free']
            print(f"Current position size for {pair}: {position_size}")

            order = binance.create_market_sell_order(pair, position_size)
            sell_amount = position_size * current_price
            message = f"Placed SELL order for entire position of {position_size} {pair} (${sell_amount:.2f}) at {current_price:.2f}."

            return order, None, message

    except Exception as e:
        print(f"An error occurred while placing {order_type} order for {pair}: {e}")
        return None, None, f"Error placing {order_type} order: {e}"


# Function to move take profit order
def move_take_profit(pair, new_tp_price, current_tp_order):
    try:
        # Cancel existing take profit order
        binance.cancel_order(current_tp_order['id'], pair)

        # Place new take profit order
        quantity = current_tp_order['amount']
        new_tp_order = binance.create_limit_sell_order(pair, quantity, new_tp_price)

        message = f"Take profit moved from ${current_tp_order['price']:.8f} to ${new_tp_price:.8f} for {pair}"
        return new_tp_order, message
    except Exception as e:
        return None, f"Error moving take profit for {pair}: {e}"


# Function to calculate safety order drop
def calculate_safety_order_drop(order_number):
    return INITIAL_SAFETY_ORDER_DROP * (2 ** order_number)


# Function to calculate safety order amount based on the strategy
def calculate_safety_order_amount(order_number):
    if order_number == 0:
        return FIRST_SAFETY_ORDER_AMOUNT
    else:
        return FIRST_SAFETY_ORDER_AMOUNT * (SAFETY_ORDER_MULTIPLIER ** (order_number - 1))


# Main trading loop
def main():
    positions = {pair: None for pair in TRADING_PAIRS}
    safety_orders = {pair: [] for pair in TRADING_PAIRS}
    capital = INITIAL_CAPITAL

    while True:
        for pair in TRADING_PAIRS:
            try:
                rsi, ema, upper_band, lower_band = fetch_data(pair)

                if rsi is None:
                    send_telegram_message(f"Skipping {pair} due to invalid data.")
                    continue

                current_price = binance.fetch_ticker(pair)['last']
                print(f"{pair} $$$: {current_price:.8f}")

                if rsi <= 30 and current_price < lower_band and positions[pair] is None:
                    print(
                        f"\033[93mCondition to BUY for {pair}: met (RSI: {rsi}, Price: {current_price} < Lower Band: {lower_band})\033[0m")
                    order, tp_order, message = place_order(pair, 'buy', INITIAL_TRADE_AMOUNT)

                    if order is None:
                        send_telegram_message(message)
                        continue

                    positions[pair] = {'main_order': order, 'tp_order': tp_order, 'average_price': order['average']}
                    send_telegram_message(message)
                else:
                    print(
                        f"\033[94mCondition to BUY for {pair}: not met (RSI: {rsi}, Price: {current_price} >= Lower Band: {lower_band})\033[0m")

                if positions[pair] is not None:
                    # Check if take profit order has been filled
                    tp_order_status = binance.fetch_order(positions[pair]['tp_order']['id'], pair)
                    if tp_order_status['status'] == 'closed':
                        profit = (tp_order_status['price'] - positions[pair]['average_price']) * tp_order_status[
                            'filled']
                        capital += profit
                        positions[pair] = None
                        safety_orders[pair] = []
                        message = f"Take profit order filled for {pair}. Profit: ${profit:.2f}. Total capital: ${capital:.2f}."
                        send_telegram_message(message)
                        continue

                    # Check for safety orders
                    safety_order_number = len(safety_orders[pair])
                    if safety_order_number < SAFETY_ORDER_COUNT:
                        safety_order_drop = calculate_safety_order_drop(safety_order_number)
                        if current_price <= positions[pair]['average_price'] * (1 - safety_order_drop):
                            safety_order_amount = calculate_safety_order_amount(safety_order_number)
                            so_order, _, message = place_order(pair, 'buy', safety_order_amount)

                            if so_order is None:
                                send_telegram_message(message)
                                continue

                            safety_orders[pair].append(so_order)

                            # Recalculate average position price
                            total_quantity = positions[pair]['main_order']['filled'] + sum(
                                so['filled'] for so in safety_orders[pair])
                            total_cost = positions[pair]['main_order']['cost'] + sum(
                                so['cost'] for so in safety_orders[pair])
                            new_average_price = total_cost / total_quantity
                            positions[pair]['average_price'] = new_average_price

                            # Move take profit order
                            new_tp_price = new_average_price * (1 + PROFIT_TARGET)
                            new_tp_order, tp_message = move_take_profit(pair, new_tp_price, positions[pair]['tp_order'])

                            if new_tp_order:
                                positions[pair]['tp_order'] = new_tp_order
                                message += f" {tp_message}"
                            else:
                                message += f" Failed to move TP order: {tp_message}"

                            send_telegram_message(message)

            except Exception as e:
                print(f"An error occurred for {pair}: {e}")

        time.sleep(60)  # Wait for the next interval


if __name__ == '__main__':
    main()
