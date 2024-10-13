from flask import Flask
import threading
import os
from bot import main  # Import your main bot function

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_bot():
    main()  # This should be the main function of your trading bot

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
