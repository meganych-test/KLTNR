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
TRADING_PAIRS = ['FTT/USDT', 'SHIB/USDT', 'PEPE/USDT', 'TIA/USDT']
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

    # Check for valid price data
    if df['close'].eq(0).all():
        print(f"Error: All close prices are 0 for {pair}. Skipping calculations.")
        return None, None, None, None  # Skip this pair due to invalid data

    # Perform calculations if data is valid
    rsi = RSIIndicator(df['close'], window=RSI_PERIOD).rsi().iloc[-1]
    ema = EMAIndicator(df['close'], window=EMA_PERIOD).ema_indicator().iloc[-1]
    atr = AverageTrueRange(df['high'], df['low'], df['close'], window=ATR_PERIOD).average_true_range().iloc[-1]

    upper_band = ema + (atr * ATR_MULTIPLIER)
    lower_band = ema - (atr * ATR_MULTIPLIER)

    return round(rsi, 2), round(ema, 8), round(upper_band, 8), round(lower_band, 8)

# Function to place orders
def place_order(pair, order_type, amount):
    try:
        current_price = binance.fetch_ticker(pair)['last']
        notional_value = current_price * amount

        if notional_value < min_notional_values[pair]:
            return None, f"Order not placed: Notional value {notional_value:.2f} is below the minimum required for {pair}."

        if order_type == 'buy':
            order = binance.create_market_buy_order(pair, amount)
            message = f"Placed BUY order for ${amount} {pair} at {order['average']:.2f}."
            return order, message
        elif order_type == 'sell':
            order = binance.create_market_sell_order(pair, amount)
            message = f"Placed SELL order for ${amount} {pair} at {order['average']:.2f}."
            return order, message
    except Exception as e:
        print(f"An error occurred while placing {order_type} order: {e}")
        return None, f"Error placing {order_type} order: {e}"

# Function to set take profit
def set_take_profit(entry_price, order_type):
    if order_type == 'buy':
        tp_price = entry_price * (1 + PROFIT_TARGET)
    elif order_type == 'sell':
        tp_price = entry_price * (1 - PROFIT_TARGET)
    else:
        return None
    return tp_price

# Function to check for open positions
def check_open_positions(pair):
    positions = binance.fetch_open_orders(pair)
    return positions

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
    take_profits = {pair: None for pair in TRADING_PAIRS}
    capital = INITIAL_CAPITAL  # Initialize capital with the initial amount

    # Check for existing open positions on bot start
    for pair in TRADING_PAIRS:
        open_positions = check_open_positions(pair)
        if open_positions:
            for position in open_positions:
                order_type = 'buy' if position['side'] == 'buy' else 'sell'
                amount = position['remaining']
                price = position['price']
                message = f"Existing {order_type.upper()} position found for {amount} {pair} at {price:.2f}."
                send_telegram_message(message)

                # Update the positions dictionary with the open position
                positions[pair] = {
                    'side': order_type,
                    'amount': amount,
                    'average': price,  # Assuming 'price' is the entry price
                    'filled': position['amount'],
                    'cost': price * position['amount']
                }

                # Set the take profit target based on the existing position
                tp_price = set_take_profit(price, order_type)
                take_profits[pair] = tp_price
                send_telegram_message(f"Take profit set @ {tp_price:.8f} for {pair}.")

    while True:
        for pair in TRADING_PAIRS:
            try:
                # Fetch the RSI and Keltner Channel data for the trading pair
                rsi, ema, upper_band, lower_band = fetch_data(pair)

                # If fetch_data returns None, skip processing for that pair
                if rsi is None:
                    send_telegram_message(f"Skipping {pair} due to invalid data.")
                    continue

                current_price = binance.fetch_ticker(pair)['last']

                print(f"{pair} $$$: {current_price:.8f}")

                # Check for buy signal based on both RSI < 30 and price below lower Keltner Channel
                if rsi <= 30 and current_price < lower_band and positions[pair] is None:
                    print(f"\033[92mCondition to BUY for {pair}: met (RSI: {rsi}, Price: {current_price} < Lower Band: {lower_band})\033[0m")
                    # Place the first buy order
                    order, message = place_order(pair, 'buy', INITIAL_TRADE_AMOUNT)

                    # Handle case where order is None (failed order)
                    if order is None:
                        send_telegram_message(message)
                        continue  # Skip to next iteration

                    # Record the position and set the take profit target
                    positions[pair] = order
                    tp_price = set_take_profit(order['average'], 'buy')
                    take_profits[pair] = tp_price
                    message += f" TP set @ {tp_price:.8f}."
                    send_telegram_message(message)
                else:
                    print(f"\033[91mCondition to BUY for {pair}: not met (RSI: {rsi}, Price: {current_price} >= Lower Band: {lower_band})\033[0m")

                # Check for sell signal or safety orders
                if positions[pair] is not None:
                    current_price = binance.fetch_ticker(pair)['last']
                    purchase_price = positions[pair]['average']

                    # Check if profit target is met
                    if current_price >= take_profits[pair]:
                        print(f"\033[92mCondition to SELL for {pair}: met (Price: {current_price} >= Take Profit: {take_profits[pair]})\033[0m")
                        order, message = place_order(pair, 'sell', positions[pair]['filled'])

                        # Handle case where sell order fails
                        if order is None:
                            send_telegram_message(message)
                            continue

                        profit = (current_price - purchase_price) * positions[pair]['filled']
                        capital += profit  # Reinvest the profit by adding it to capital
                        positions[pair] = None
                        take_profits[pair] = None
                        safety_orders[pair] = []  # Reset safety orders
                        message += f" Profit: ${profit:.2f}. Total capital: ${capital:.2f}."
                        send_telegram_message(message)
                    else:
                        print(f"\066[91mCondition to SELL for {pair}: not met (Price: {current_price} < Take Profit: {take_profits[pair]})\066[0m")

                    # Implement safety orders with doubling drop percentage
                    safety_order_number = len(safety_orders[pair])
                    if safety_order_number < SAFETY_ORDER_COUNT:
                        safety_order_drop = calculate_safety_order_drop(safety_order_number)
                        if current_price <= purchase_price * (1 - safety_order_drop):
                            safety_order_amount = calculate_safety_order_amount(safety_order_number)
                            order, message = place_order(pair, 'buy', safety_order_amount)

                            # Handle case where safety order fails
                            if order is None:
                                send_telegram_message(message)
                                continue

                            safety_orders[pair].append(order)

                            # Recalculate average position price and update take profit
                            total_quantity = positions[pair]['filled'] + order['filled']
                            total_cost = positions[pair]['cost'] + order['cost']
                            new_average_price = total_cost / total_quantity
                            positions[pair]['average'] = new_average_price
                            positions[pair]['filled'] = total_quantity
                            positions[pair]['cost'] = total_cost

                            take_profits[pair] = set_take_profit(new_average_price, 'buy')
                            message += f" Safety order {safety_order_number + 1} added. New average price: {new_average_price:.8f}. TP moved to {take_profits[pair]:.8f}."
                            send_telegram_message(message)

            except Exception as e:
                print(f"An error occurred for {pair}: {e}")

        time.sleep(60)  # Wait for the next interval

if __name__ == '__main__':
    main()
