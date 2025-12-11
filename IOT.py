import logging
import requests
import time
from datetime import datetime, time as dtime
import pandas as pd
from functools import wraps
import numpy as np 

from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue

BOT_TOKEN = "8328351698:AAGPPsfmVrYpk144e98Atc20W-GfBnM7X-A"        
OWNER_CHAT_ID = 8520721768                      
ROOMMATE_CHAT_ID = None                        

THINGSPEAK_CHANNEL_ID = "3190313"      
THINGSPEAK_READ_KEY = "4C49H2S39TDG51OD"      

TS_FETCH_N = 100 # number of samples to fetch

# Greeting time (24h) — set to 8:00
DAILY_HOUR = 8
DAILY_MINUTE = 0

LAST_WINDOW_OPEN_STATE = False 

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)


# fetch & process ThingSpeak data & compute metrics
def _fetch_raw_thingspeak(n=TS_FETCH_N):
    """Fetch last n feeds from ThingSpeak channel as a raw pandas DataFrame."""
    try:
        url = f"https://api.thingspeak.com/channels/{THINGSPEAK_CHANNEL_ID}/feeds.json"
        params = {"api_key": THINGSPEAK_READ_KEY, "results": n}

        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()

        data = r.json()
        feeds = data.get("feeds", [])
        if not feeds:
            return None

        df = pd.DataFrame(feeds)
        df["created_at"] = pd.to_datetime(df["created_at"])
        
        for i in range(1, 5):
            col_data = df.get(f"field{i}") 
            if col_data is not None:
                df[f"field{i}"] = pd.to_numeric(col_data, errors="coerce")

        df = df.rename(columns={
            "field1": "indoor_temp",
            "field2": "indoor_humidity",
            "field3": "outdoor_temp",
            "field4": "outdoor_humidity"
        })
        return df[["created_at", "indoor_temp", "indoor_humidity", "outdoor_temp", "outdoor_humidity"]].copy()
    except Exception as e:
        logger.error("Error fetching ThingSpeak: %s", e)
        return None

def get_processed_data(n=TS_FETCH_N):
    """Fetches raw data and applies smoothing, THI, and anomaly analysis."""
    df = _fetch_raw_thingspeak(n=n)
    if df is None or df.empty:
        return None

    df = df.sort_values("created_at")
    
    # Cleaned up: Using .ffill() 
    df = df.ffill() 

    df["delta_temp"] = df["indoor_temp"] - df["outdoor_temp"]
    
    # Rolling average (5-min window =  values if data is 1/min)
    df["indoor_temp_smooth"] = df["indoor_temp"].rolling(window=5, min_periods=1).mean()
    df["indoor_humidity_smooth"] = df["indoor_humidity"].rolling(window=5, min_periods=1).mean()
    
    # Thermal Comfort Index
    df["THI"] = df["indoor_temp_smooth"] - (0.55 - 0.0055 * df["indoor_humidity_smooth"]) * (df["indoor_temp_smooth"] - 14.5)

    # Anomaly Detection 
    df["humidity_change"] = df["indoor_humidity"].diff()
    df["temp_change"] = df["indoor_temp"].diff()

    last_humid_change = df["humidity_change"].iloc[-1] if df["humidity_change"].notna().any() else 0
    last_temp_change = df["temp_change"].iloc[-1] if df["temp_change"].notna().any() else 0

    df["humidity_spike"] = last_humid_change > 10
    df["temp_spike"] = abs(last_temp_change) > 2
    
    df["last_humid_change"] = last_humid_change
    df["last_temp_change"] = last_temp_change
    
    # Correlation Analysis
    df_clean = df.dropna(subset=["indoor_temp", "outdoor_temp", "indoor_humidity", "outdoor_humidity"])

    if not df_clean.empty:
        corr_temp = df_clean["indoor_temp"].corr(df_clean["outdoor_temp"])
        corr_hum  = df_clean["indoor_humidity"].corr(df_clean["outdoor_humidity"])
    else:
        corr_temp = np.nan
        corr_hum = np.nan
        
    df["corr_temp"] = corr_temp
    df["corr_hum"] = corr_hum
    
    return df

def compute_thi(T, H):
    try:
        return T - (0.55 - 0.0055 * H) * (T - 14.5)
    except Exception:
        return None

# WINDOW INFERENCE
def infer_window_open(df: pd.DataFrame) -> tuple[bool, dict]:
    if df is None or len(df) < 6:
        return False, {}

    last_6 = df.dropna(subset=["indoor_temp", "indoor_humidity", "outdoor_temp", "outdoor_humidity"]).iloc[-6:]
    if len(last_6) < 6:
        return False, {}
        
    curr = last_6.iloc[-1] 
    prev_5m = last_6.iloc[0]
    
    # 5-minute Change
    temp_change = curr["indoor_temp"] - prev_5m["indoor_temp"]
    humid_change = curr["indoor_humidity"] - prev_5m["indoor_humidity"]
    
    # Current Gaps
    temp_gap_dir = curr["indoor_temp"] - curr["outdoor_temp"] 
    humid_gap_dir = curr["indoor_humidity"] - curr["outdoor_humidity"]

    #Thresholds
    MIN_GAP_TEMP = 3.0      # Minimum required temperature difference (°C)
    MIN_CHANGE_TEMP = 1.0   # Minimum required 5-min temp change (°C)
    MIN_CHANGE_HUMID = 3.0  # Minimum required 5-min humidity change (%)
    MIN_HUMID_GAP = 5.0     # Minimum required humidity gap for reliable check (%)
    
    likely_open = False
    
    # Default metric values
    is_temp_converging = False
    is_humid_converging = False
    prev_temp_gap_abs = None
    curr_temp_gap_abs = None

    # Sufficient Initial Temperature Gap
    if abs(temp_gap_dir) >= MIN_GAP_TEMP:
        
        # Check for a rapid change in indoor conditions
        if abs(temp_change) >= MIN_CHANGE_TEMP or abs(humid_change) >= MIN_CHANGE_HUMID:
            
            # Condition 3: Check if the change is **converging** (moving toward outdoor state)
            is_temp_converging = np.sign(temp_gap_dir) != np.sign(temp_change)

            is_humid_converging = True 
            if abs(humid_change) >= MIN_CHANGE_HUMID and abs(humid_gap_dir) > MIN_HUMID_GAP:
                 is_humid_converging = np.sign(humid_gap_dir) != np.sign(humid_change)

            if is_temp_converging and is_humid_converging:
                 likely_open = True
                 
    # Ensure the change reduced the indoor-outdoor gap
    if likely_open:
        prev_temp_gap_abs = abs(prev_5m["indoor_temp"] - prev_5m["outdoor_temp"])
        curr_temp_gap_abs = abs(temp_gap_dir)
        
        # The new gap must be smaller than the old gap to confirm convergence
        if curr_temp_gap_abs >= prev_temp_gap_abs:
             likely_open = False 

    metrics = {
        "temp_change": round(float(temp_change), 2),
        "humid_change": round(float(humid_change), 2),
        "temp_gap_dir": round(float(temp_gap_dir), 2),
        "humid_gap_dir": round(float(humid_gap_dir), 2),
        "is_temp_converging": is_temp_converging,
        "is_humid_converging": is_humid_converging,
        "prev_temp_gap_abs": round(float(prev_temp_gap_abs), 2) if prev_temp_gap_abs is not None else None,
        "curr_temp_gap_abs": round(float(curr_temp_gap_abs), 2) if curr_temp_gap_abs is not None else None,
    }
    return bool(likely_open), metrics

def clothing_suggestion(out_temp_c, out_hum, bias="normal"):
    """Return a short clothing recommendation string.
    bias: 'normal', 'warm', 'active' (choose level)"""
    if out_temp_c is None:
        return "No data"

    t = out_temp_c
    if t < 0:
        suggestion = "Warm coat, layers, scarf"
    elif t < 5:
        suggestion = "Coat + jumper"
    elif t < 12:
        suggestion = "Jacket or warm sweater"
    elif t < 18:
        suggestion = "Long sleeve + light jacket"
    elif t < 24:
        suggestion = "T-shirt + light layer"
    else:
        suggestion = "Very light clothes"

    if out_hum is not None and out_hum > 85 and t >= 12:
        suggestion += " (it will feel muggy)"
    if bias == "warm":
        suggestion += " — wear warmer layers"
    return suggestion

def restricted(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        sender = update.effective_chat.id
        allowed = {OWNER_CHAT_ID}
        if ROOMMATE_CHAT_ID:
            allowed.add(ROOMMATE_CHAT_ID)
        if sender not in allowed:
            update.message.reply_text("Sorry — you are not authorised to use this bot.")
            return
        return func(update, context, *args, **kwargs)
    return wrapper


def get_latest_metrics():
    df = get_processed_data(n=TS_FETCH_N) 
    if df is None or df.empty:
        return None

    last = df.dropna(subset=["indoor_temp_smooth", "indoor_humidity_smooth", "outdoor_temp", "outdoor_humidity", "THI"]).tail(1)
    if last.shape[0] == 0:
        return None
    
    row = last.iloc[0]
    
    indoor_t = float(row["indoor_temp_smooth"])
    indoor_h = float(row["indoor_humidity_smooth"])
    outdoor_t = float(row["outdoor_temp"])
    outdoor_h = float(row["outdoor_humidity"])
    thi = float(row["THI"]) 

    comfort = "Comfortable" # Default
    if thi > 27:
        comfort = "Sweaty hot"
    elif thi > 20:
        comfort = "Warm, but still nice!"
    elif thi < 5:
        comfort = "Trun on the f***ing heater!"
    elif thi < 15:
        comfort = "A bit chilly, innit?"

    is_humidity_spike = bool(row["humidity_spike"])
    is_temp_spike = bool(row["temp_spike"])

    corr_temp = float(row["corr_temp"]) if pd.notna(row["corr_temp"]) else None
    corr_hum = float(row["corr_hum"]) if pd.notna(row["corr_hum"]) else None

    likely_open, wmetrics = infer_window_open(df)

    if likely_open:
        temp_change = wmetrics['temp_change']
        humid_change = wmetrics['humid_change']

        if abs(temp_change) > abs(humid_change):
             window_text = f"Likely OPEN (ΔT/5m={temp_change}°, ΔH/5m={humid_change}%) - Temp Driven"
        else:
             window_text = f"Likely OPEN (ΔT/5m={temp_change}°, ΔH/5m={humid_change}%) - Humid Driven"
             
    else:
        window_text = "Likely closed"

    vent_text = "Everything looks good, no action needed!"
    if is_humidity_spike:
        vent_text = f"⚠️ **RAPID HUMIDITY SPIKE!** Ventillation Please!!! (last ΔH: {row['last_humid_change']:.1f}%)"
    elif is_temp_spike:
        vent_text = f"⚠️ **RAPID TEMPERATURE CHANGE!** Check window/heater (last ΔT: {row['last_temp_change']:.1f}°C)"
    elif indoor_h > 70 and outdoor_h < 65 and (indoor_t - outdoor_t) > 0:
        vent_text = "Open window for 10-15 min (outdoor drier)"
    elif indoor_h > 75 and outdoor_h >= 65:
        vent_text = "Welcome to London, everywhere is humid. Try mechanical ventilation?"
    elif indoor_t - outdoor_t > 6 and outdoor_t < 5:
        vent_text = "It's freezing outside!! Close the window please~~~"

    clothes = clothing_suggestion(outdoor_t, outdoor_h)

    metrics = {
        "indoor_temp": round(indoor_t, 2),
        "indoor_hum": round(indoor_h, 1),
        "outdoor_temp": round(outdoor_t, 2),
        "outdoor_hum": round(outdoor_h, 1),
        "thi": thi,
        "comfort": comfort,
        "window": likely_open,
        "window_text": window_text,
        "vent_text": vent_text,
        "clothes": clothes,
        "wmetrics": wmetrics, 
        "corr_temp": corr_temp, 
        "corr_hum": corr_hum    
    }
    return metrics


def cmd_status(update: Update, context: CallbackContext):
    metrics = get_latest_metrics()
    if metrics is None:
        update.message.reply_text("No recent data available.")
        return
    text = (f"🏠 <b>Indoor</b>: {metrics['indoor_temp']}°C, {metrics['indoor_hum']}% RH\n"
            f"🌤 <b>Outdoor</b>: {metrics['outdoor_temp']}°C, {metrics['outdoor_hum']}% RH\n"
            f"📊 <b>THI</b>: {metrics['thi']:.1f} — {metrics['comfort']}")
    update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
def cmd_recommend(update: Update, context: CallbackContext):
    metrics = get_latest_metrics()
    if metrics is None:
        update.message.reply_text("No recent data available.")
        return
    text = (f"{metrics['vent_text']}\n\n"
            f"👕: {metrics['clothes']}\n\n"
            f"Window status: {metrics['window_text']}")
    update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
def cmd_analyse(update: Update, context: CallbackContext):
    metrics = get_latest_metrics()
    if metrics is None or metrics["corr_temp"] is None or metrics["corr_hum"] is None:
        update.message.reply_text("No recent data available for correlation analysis.")
        return

    corr_t = metrics["corr_temp"]
    corr_h = metrics["corr_hum"]

    # Insulation Analysis
    if corr_t > 0.6:
        insulation_text = "🔴 Poor Insulation: Indoor temp is highly correlated with outdoor temp. You might be losing a lot of heat/cooling."
    elif corr_t > 0.3:
        insulation_text = "🟡 Fair Insulation: There is a moderate correlation, suggesting some heat loss/gain through the building envelope."
    else:
        insulation_text = "🟢 Good Insulation: Indoor temp is well-decoupled from outdoor temp, suggesting good insulation."

    # Ventilation Analysis
    if corr_h > 0.6:
        vent_analysis_text = "🟡 High Air Exchange: Indoor humidity is highly correlated with outdoor humidity. This is good for flushing air, but problematic if outdoor air is highly humid."
    elif corr_h > 0.3:
        vent_analysis_text = "🟢 Normal Ventilation: A healthy amount of air exchange is likely occurring."
    else:
        vent_analysis_text = "🔴 Low Air Exchange: Indoor humidity is decoupled from outdoor air. This may lead to high internal humidity/CO2 if not actively ventilated."

    text = (f"🔬 <b>Room Performance Analysis</b>\n\n"
            f"<i>(Based on correlation over the last ~{TS_FETCH_N} minutes)</i>\n\n"
            f"🌡️ <b>Temperature Correlation (Insulation)</b>\n"
            f"Index: <code>{corr_t:.2f}</code>\n"
            f"{insulation_text}\n\n"
            f"💧 <b>Humidity Correlation (Air Exchange)</b>\n"
            f"Index: <code>{corr_h:.2f}</code>\n"
            f"{vent_analysis_text}")
    
    update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
def cmd_notify_roommate(update: Update, context: CallbackContext):
    if ROOMMATE_CHAT_ID is None:
        update.message.reply_text("Roommate chat ID not configured.")
        return
    metrics = get_latest_metrics()
    if metrics is None:
        update.message.reply_text("No recent data available.")
        return

    msg = (f"Hi — quick request:\n"
           f"Room humidity is {metrics['indoor_hum']}% and THI={metrics['thi']:.1f} ({metrics['comfort']}).\n"
           f"Recommendation: {metrics['vent_text']}\n"
           f"Please could you open/close the window? Thanks! 😊")
    # send to roommate
    context.bot.send_message(chat_id=ROOMMATE_CHAT_ID, text=msg)
    update.message.reply_text("Roommate notified.")

@restricted
def cmd_stop_bot(update: Update, context: CallbackContext, updater): # Accepts 'updater'
    update.message.reply_text("👋 <b>Shutting down...</b> The bot will stop polling in a moment.", parse_mode=ParseMode.HTML)

    context.job_queue.stop() 

    updater.stop()
    
    logger.info("Bot stop command received. Updater shutting down.")


def morning_greeting(context: CallbackContext):
    metrics = get_latest_metrics()
    if metrics is None:
        context.bot.send_message(chat_id=OWNER_CHAT_ID, text="No data available for morning greeting.")
        return
    text = (f"🌅 <b>Morning!</b>\n\n"
            f"Indoor: {metrics['indoor_temp']}°C, {metrics['indoor_hum']}% RH\n"
            f"Outdoor: {metrics['outdoor_temp']}°C, {metrics['outdoor_hum']}% RH\n"
            f"THI: {metrics['thi']:.1f} ({metrics['comfort']})\n"
            f"What to wear: {metrics['clothes']}\n\n"
            f"Have a good day!")
    context.bot.send_message(chat_id=OWNER_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

def check_window_status(context: CallbackContext):

    global LAST_WINDOW_OPEN_STATE
    
    df = get_processed_data() 
    metrics = get_latest_metrics()
    
    if metrics is None or df is None or len(df) < 6:
        logger.warning("Automated window check failed: Insufficient data or metrics.")
        return

    curr = df.iloc[-1]
    prev_5m = df.iloc[-6] 
    
    temp_gap_dir = curr["indoor_temp"] - curr["outdoor_temp"]
    humid_gap_dir = curr["indoor_humidity"] - curr["outdoor_humidity"]
    temp_change = curr["indoor_temp"] - prev_5m["indoor_temp"]
    humid_change = curr["indoor_humidity"] - prev_5m["indoor_humidity"]

    
    # Check for Convergence
    is_converging = metrics["window"] 
    
    
    # Check for Divergence 
    MIN_DIVERGENCE_CHANGE = 1.0 # Minimum change to confirm divergence over 5 mins
    MIN_DIVERGENCE_GAP = 10.0    # Must have a reasonable gap to detect divergence from

    MIN_DIVERGENCE_CHANGE_H = 2.0 # Minimum change to confirm divergence over 5 mins
    MIN_DIVERGENCE_GAP_H = 20.0    # Must have a reasonable gap to detect divergence from
    
    # Temperature Divergence
    is_diverging_temp = (np.sign(temp_gap_dir) == np.sign(temp_change)) and \
                        (abs(temp_change) >= MIN_DIVERGENCE_CHANGE) and \
                        (abs(temp_gap_dir) >= MIN_DIVERGENCE_GAP)

    # Humidity Divergence
    is_diverging_humid = (np.sign(humid_gap_dir) == np.sign(humid_change)) and \
                         (abs(humid_change) >= MIN_DIVERGENCE_CHANGE_H) and \
                         (abs(humid_gap_dir) >= MIN_DIVERGENCE_GAP_H)

    is_diverging = is_diverging_temp or is_diverging_humid

    current_state = LAST_WINDOW_OPEN_STATE
    next_state = current_state

    if current_state and is_diverging:
        next_state = False
        alert_msg = (
            f"✅ **WINDOW CLOSED ALERT!** \n\n"
            f"The room assistant infers the window was just closed due to **divergence**.\n"
            f"The indoor environment is starting to move away from the outdoor state.\n"
            f"Debugging Metrics:\n"
            f"ΔT/5m: {temp_change:.2f}°C, ΔH/5m: {humid_change:.2f}%\n"
            f"T Gap: {temp_gap_dir:.2f}°C, H Gap: {humid_gap_dir:.2f}%"
        )
        context.bot.send_message(chat_id=OWNER_CHAT_ID, text=alert_msg, parse_mode=ParseMode.HTML)
        logger.info("Window closed alert sent.")

    elif not current_state and is_converging:
        next_state = True

    if next_state:
        alert_type = "NEW OPENING DETECTED" if (next_state and not current_state) else "WINDOW IS OPEN (Plateau Check)"
        
        alert_msg = (
            f"⚠️ **WINDOW OPEN ALERT!** ⚠️\n\n"
            f"The room assistant infers the window is currently OPEN.\n"
            f"Status: {alert_type}\n"
            f"Current Stats:\n"
            f"🏠 Indoor: {metrics['indoor_temp']}°C, {metrics['indoor_hum']}% RH (Smooth)\n"
            f"🌤 Outdoor: {metrics['outdoor_temp']}°C, {metrics['outdoor_hum']}% RH\n\n"
            f"Ventilation Recommendation: {metrics['vent_text']}\n\n"
            f"Debugging Metrics:\n"
            f"ΔT/5m: {metrics['wmetrics']['temp_change']}°C, ΔH/5m: {metrics['wmetrics']['humid_change']}%\n"
            f"Temp Gap (abs) Prev/Curr: {metrics['wmetrics']['prev_temp_gap_abs']}°C / {metrics['wmetrics']['curr_temp_gap_abs']}°C"
        )
        context.bot.send_message(chat_id=OWNER_CHAT_ID, text=alert_msg, parse_mode=ParseMode.HTML)
        logger.info(f"Window opened alert sent. State: {alert_type}")

    LAST_WINDOW_OPEN_STATE = next_state


def cmd_start(update: Update, context: CallbackContext):
    txt = ("Hi — I'm your Room Assistant bot. Commands:\n"
           "/status — show current mini-dashboard\n"
           "/recommend — ventilation & clothes advice\n"
           "/analyse — room insulation and ventilation performance analysis\n"
           "/notify_roommate — send a request to roommate (if configured)\n"
           "/stop_bot — **STOP** the bot")

    update.message.reply_text(txt)

def send_startup_manual(context: CallbackContext):
    """JobQueue task: sends the start message once upon bot initialization."""
    txt = ("👋 <b>Room Assistant Activated!</b>\n\n" # Bolding changed to <b>
           "Hello I'm SB, your smart butler. Please command, Boss\n"
           "/status — show current mini-dashboard\n"
           "/recommend — ventilation & clothes advice\n"
           "/analyse — room insulation and ventilation performance analysis\n"
           "/notify_roommate — send a request to roommate (if configured)\n"
           "/stop_bot — **STOP** the bot")
    try:
        context.bot.send_message(chat_id=OWNER_CHAT_ID, text=txt, parse_mode=ParseMode.HTML) 
        logger.info("Startup manual sent to OWNER_CHAT_ID.")
    except Exception as e:
        logger.error(f"Failed to send startup manual to owner: {e}")

def main():

    updater = Updater(BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher
    jq: JobQueue = updater.job_queue

    # Command handlers
    dispatcher.add_handler(CommandHandler("start", cmd_start))
    dispatcher.add_handler(CommandHandler("status", cmd_status))
    dispatcher.add_handler(CommandHandler("recommend", cmd_recommend))
    dispatcher.add_handler(CommandHandler("notify_roommate", cmd_notify_roommate))
    dispatcher.add_handler(CommandHandler("analyse", cmd_analyse))
    dispatcher.add_handler(
            CommandHandler(
                "stop_bot", 
                lambda update, context: cmd_stop_bot(update, context, updater), 
            )
        )

    # Schedule daily morning job
    jq.run_daily(morning_greeting, time=dtime(hour=DAILY_HOUR, minute=DAILY_MINUTE))
    logger.info(f"Greeting scheduled for {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} every day.")

    # Schedule recurring window status check
    CHECK_INTERVAL_SECONDS = 5 * 60 
    jq.run_repeating(check_window_status, interval=CHECK_INTERVAL_SECONDS, first=0)
    logger.info(f"Window status check scheduled every {CHECK_INTERVAL_SECONDS} seconds.")

    jq.run_once(send_startup_manual, when=1) 
    logger.info("Startup manual scheduled to run once.")

    # Start the bot
    updater.start_polling()
    logger.info("Bot started (polling). Press Ctrl+C to stop.")
    updater.idle()


if __name__ == "__main__":
    main()