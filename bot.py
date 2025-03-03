import os
import random
import asyncio
import sqlite3
import logging
import re
from datetime import datetime, timedelta
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from web3 import Web3, Account
import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer
from solders.message import Message
from openpyxl import Workbook
from dotenv import load_dotenv
from ratelimit import limits, sleep_and_retry
import json
import hashlib
import pytz
from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.transactions import Payment
from xrpl.utils import xrp_to_drops

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ETH_RPC_URL = os.getenv('ETH_RPC_URL', 'https://mainnet.infura.io/v3/your-infura-key')
BSC_RPC_URL = os.getenv('BSC_RPC_URL', 'https://bsc-dataseed.binance.org/')
SOL_RPC_URL = os.getenv('SOL_RPC_URL', 'https://api.devnet.solana.com')
XRP_RPC_URL = os.getenv('XRP_RPC_URL', 'https://s1.ripple.com:51234/')
ADMIN_ID = os.getenv('ADMIN_ID')
ETH_SENDER_ADDRESS = os.getenv('ETH_SENDER_ADDRESS')
ETH_PRIVATE_KEY = os.getenv('ETH_PRIVATE_KEY')
SOL_SENDER_PRIVATE_KEY = os.getenv('SOL_SENDER_PRIVATE_KEY')
XRP_SENDER_ADDRESS = os.getenv('XRP_SENDER_ADDRESS')
XRP_SENDER_SEED = os.getenv('XRP_SENDER_SEED')
TOKEN_CONTRACT_ADDRESS = os.getenv('TOKEN_CONTRACT_ADDRESS')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'tigerr_airdrop_bot')

# Blockchain Setup
web3_eth = Web3(Web3.HTTPProvider(ETH_RPC_URL))
web3_bsc = Web3(Web3.HTTPProvider(BSC_RPC_URL))
solana_client = requests.Session()
solana_client.headers.update({"Content-Type": "application/json"})
xrp_client = JsonRpcClient(XRP_RPC_URL)

# ERC-20 Token ABI
TOKEN_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}
]
token_contract_eth = web3_eth.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=TOKEN_ABI)
token_contract_bsc = web3_bsc.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=TOKEN_ABI)

# Logging Setup
logging.basicConfig(filename='airdrop_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# SQLite Setup with Schema Migration
conn = sqlite3.connect('airdrop.db', check_same_thread=False)
cursor = conn.cursor()

cursor.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, username TEXT, language TEXT, referral_code TEXT, referred_by TEXT,
        kyc_status TEXT DEFAULT 'pending', agreed_terms INTEGER, momo_balance REAL DEFAULT 0,
        kyc_telegram_link TEXT, kyc_x_link TEXT, kyc_wallet TEXT, kyc_chain TEXT, kyc_submission_time TEXT,
        has_seen_menu INTEGER DEFAULT 0, joined_groups INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS captchas (user_id TEXT PRIMARY KEY, captcha INTEGER, timestamp TEXT);
    CREATE TABLE IF NOT EXISTS submissions (user_id TEXT PRIMARY KEY, wallet TEXT, chain TEXT, timestamp TEXT);
    CREATE TABLE IF NOT EXISTS eligible (user_id TEXT PRIMARY KEY, wallet TEXT, chain TEXT, tier INTEGER, verified INTEGER, token_balance REAL, social_tasks_completed INTEGER);
    CREATE TABLE IF NOT EXISTS distributions (user_id TEXT PRIMARY KEY, wallet TEXT, chain TEXT, amount REAL, status TEXT, tx_hash TEXT, vesting_end TEXT);
    CREATE TABLE IF NOT EXISTS referrals (referrer_id TEXT, referee_id TEXT PRIMARY KEY, timestamp TEXT, status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS blacklist (wallet TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS whitelist (wallet TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, start_date TEXT, end_date TEXT, total_tokens REAL, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS daily_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT, reward REAL DEFAULT 10, active INTEGER DEFAULT 1, mandatory INTEGER DEFAULT 0, task_link TEXT);
    CREATE TABLE IF NOT EXISTS task_completions (user_id TEXT, task_id INTEGER, completion_date TEXT, username TEXT, status TEXT DEFAULT 'pending', PRIMARY KEY (user_id, task_id, completion_date));
''')

# Add kyc_x_link if not already present
try:
    cursor.execute("ALTER TABLE users ADD COLUMN kyc_x_link TEXT")
except sqlite3.OperationalError:
    pass
conn.commit()

# Config Initialization
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("total_supply", "1000000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("tier_1_amount", "1000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("tier_2_amount", "2000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("tier_3_amount", "5000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("referral_bonus", "15"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("min_token_balance", "100"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("vesting_period_days", "30"))
conn.commit()

# Sample Campaign
cursor.execute("INSERT OR IGNORE INTO campaigns (name, start_date, end_date, total_tokens, active) VALUES (?, ?, ?, ?, ?)",
               ("Launch Airdrop", datetime.utcnow().isoformat(), (datetime.utcnow() + timedelta(days=7)).isoformat(), 1000000, 1))
conn.commit()

# Daily Tasks with Mandatory Settings and Links
daily_tasks = [
    ("Watch YouTube Video", 10, 0, "https://youtube.com/example"),
    ("Watch Facebook Video", 10, 0, "https://facebook.com/example"),
    ("Visit Website", 10, 0, "https://example.com"),
    ("Join Telegram", 10, 1, "https://t.me/examplegroup"),
    ("Subscribe Telegram Channel", 10, 1, "https://t.me/examplechannel"),
    ("Subscribe YouTube Channel", 10, 0, "https://youtube.com/channel/example"),
    ("Follow Twitter", 10, 0, "https://twitter.com/example"),
    ("Follow Facebook", 10, 0, "https://facebook.com/examplepage")
]
for description, reward, mandatory, task_link in daily_tasks:
    cursor.execute("INSERT OR IGNORE INTO daily_tasks (description, reward, mandatory, task_link) VALUES (?, ?, ?, ?)",
                   (description, reward, mandatory, task_link))
conn.commit()

# Multi-Language Support
LANGUAGES = {
    "en": {
        "welcome": "🌟 Welcome to the Momo Coin Airdrop Bot! 🌟\n\nWe’re thrilled to have you join us on this exciting journey in the world of crypto! 🚀\n\nAs a part of our community, you’re eligible for exclusive airdrop rewards. To get started, simply follow the steps below and secure your spot in the Momo Coin Airdrop. 💰✨\n\n🔑 How to Participate:\n\n- Complete your KYC verification to ensure eligibility.\n- Join our campaign and get ready for rewards.\n- Refer your friends and unlock even more bonuses! 🎁\n\nNeed help? Feel free to reach out to our support team anytime. We’re here to make your experience smooth and rewarding! 💬\n\nLet’s get started and make some Momo Coin magic happen! 🌐\n\nBalance: {balance} Momo Coins\nReferral Link: {ref_link}",
        "mandatory_rules": "📢 Mandatory Airdrop Rules:\n\n🔹 Join @successcrypto2\n🔹 Join @successcryptoboss\n\nMust Complete All Tasks & Click On [Continue] To Proceed",
        "confirm_groups": "Please confirm you have joined both groups by clicking below:",
        "menu": "Choose an action:",
        "terms": "Terms & Conditions:\n- Participate fairly\n- No multiple accounts\n- Tokens vest for {vesting_days} days",
        "usage": "Select chain (ETH, BSC, SOL, XRP) and enter wallet:",
        "captcha": "Solve: {captcha} + 5 = ?",
        "verified": "Wallet verified! Tier {tier}.",
        "blacklisted": "This wallet is blacklisted.",
        "invalid_address": "Invalid {chain} address (e.g., ETH: 0x..., SOL: SoL..., XRP: r...).",
        "no_assets": "No qualifying assets found.",
        "already_submitted": "Wallet already submitted.",
        "admin_only": "Admin only.",
        "sent_tokens": "Sent {amount} tokens to {wallet} (Tx: {tx_hash})",
        "failed_tokens": "Failed to send {amount} tokens to {wallet}: {error}",
        "referral_bonus": "🎉 Congratulations! Your referral for {referee} has been approved! You’ve earned a {bonus} Momo Coin bonus!",
        "referral_pending": "Referral submitted for {referee}. Awaiting admin approval.",
        "referral_duplicate": "This user has already been referred or is a duplicate.",
        "referral_notification": "New referral submission:\nReferrer ID: {referrer_id}\nReferee ID: {referee_id}\nReferee Username: {referee_name}\nTime: {time}",
        "referral_approved": "Your referral for {referee} has been approved!",
        "referral_rejected": "Your referral for {referee} has been rejected.",
        "kyc_pending": "KYC verification pending.",
        "tasks": "Tasks:\n1. Follow @MomoCoin\n2. Retweet pinned post",
        "daily_tasks": "*Daily Tasks*\nComplete these tasks and submit your username as proof:\n\n{daily_tasks}\n\n*Submission Format*: Enter task ID and username (e.g., '1 @username')",
        "claim": "Claim your {amount} Momo Coins!",
        "balance": "Your Momo Coin balance: {balance}",
        "task_completed": "Task '{task_description}' submitted! Awaiting admin approval.",
        "task_approved": "Task '{task_description}' approved! +10 Momo Coins",
        "task_rejected": "Task '{task_description}' rejected.",
        "join_airdrop": "Join the airdrop below (mandatory: Join Telegram, Subscribe Telegram Channel, KYC):",
        "eligibility": "Eligibility: {status}",
        "leaderboard": "Leaderboard (Top Momo Coin Earners):\n{leaders}",
        "mandatory_missing": "Complete mandatory tasks (Join Telegram, Subscribe Telegram Channel) and KYC to join airdrop.",
        "campaign_set": "Campaign '{name}' set! Start: {start}, End: {end}, Tokens: {tokens}",
        "campaign_edit": "Campaign '{name}' updated! Start: {start}, End: {end}, Tokens: {tokens}",
        "kyc_start": "Please provide your Telegram link (e.g., @username or https://t.me/username) to start KYC verification:",
        "kyc_telegram_invalid": "Invalid Telegram link. Please provide a valid Telegram handle or link (e.g., @username or https://t.me/username):",
        "kyc_telegram": "Telegram link received: {telegram}. Now provide your X link (e.g., @username or https://x.com/username):",
        "kyc_x_link_invalid": "Invalid X link. Please provide a valid X handle or link (e.g., @username or https://x.com/username):",
        "kyc_wallet_invalid": "Invalid wallet address format. Please submit wallet again (e.g., 'ETH 0x...' or 'XRP r...'):",
        "kyc_complete": "KYC submitted successfully! Awaiting admin verification.\nDetails:\nTelegram: {telegram}\nX: {x_link}\nWallet: {wallet} ({chain})",
        "kyc_status": "Your KYC status: {status}",
        "kyc_notification": "New KYC submission:\nUser ID: {user_id}\nTelegram: {telegram}\nX: {x_link}\nWallet: {wallet} ({chain})\nTime: {time}",
        "kyc_approved": "Your KYC has been approved!",
        "kyc_rejected": "Your KYC has been rejected. Please resubmit."
    }
}

# Rate Limiting
CALLS_PER_MINUTE = 10
PERIOD = 60

@sleep_and_retry
@limits(calls=CALLS_PER_MINUTE, period=PERIOD)
def rate_limited_request(url, payload):
    return requests.post(url, json=payload).json()

# Helper Functions
def is_admin(user_id):
    return str(user_id) == ADMIN_ID

def generate_referral_code(user_id):
    return f"https://t.me/{BOT_USERNAME}?start={user_id}"

def get_user_language(user_id: str) -> str:
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result and result[0] in LANGUAGES else "en"

def get_user_balance(user_id: str) -> float:
    cursor.execute("SELECT momo_balance FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0.0

def update_user_balance(user_id: str, amount: float):
    cursor.execute("UPDATE users SET momo_balance = momo_balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

def is_valid_telegram_link(link: str) -> bool:
    return bool(re.match(r"^(@[a-zA-Z0-9_]{5,32}|https://t\.me/[a-zA-Z0-9_]{5,32})$", link))

def is_valid_x_link(link: str) -> bool:
    return bool(re.match(r"^(@[a-zA-Z0-9_]{1,15}|https://x\.com/[a-zA-Z0-9_]{1,15})$", link))

def is_valid_address(wallet: str, chain: str) -> bool:
    if chain in ["ETH", "BSC"] and wallet.startswith("0x") and len(wallet) == 42:
        return web3_eth.is_address(wallet)
    if chain == "SOL" and 43 <= len(wallet) <= 44:
        try:
            Pubkey.from_string(wallet)
            return True
        except:
            return False
    if chain == "XRP" and 25 <= len(wallet) <= 35 and wallet.startswith("r"):
        try:
            from xrpl.core import addresscodec
            return addresscodec.is_valid_classic_address(wallet)
        except:
            return False
    return False

def check_mandatory_tasks(user_id: str) -> bool:
    cursor.execute("SELECT id FROM daily_tasks WHERE mandatory = 1")
    mandatory_tasks = [row[0] for row in cursor.fetchall()]
    for task_id in mandatory_tasks:
        cursor.execute("SELECT status FROM task_completions WHERE user_id = ? AND task_id = ? AND status = 'approved'", (user_id, task_id))
        if not cursor.fetchone():
            return False
    return True

def check_kyc_status(user_id: str) -> str:
    cursor.execute("SELECT kyc_status FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else "pending"

def has_seen_menu(user_id: str) -> bool:
    cursor.execute("SELECT has_seen_menu FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] == 1 if result else False

def has_joined_groups(user_id: str) -> bool:
    cursor.execute("SELECT joined_groups FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] == 1 if result else False

def get_leaderboard(lang: str) -> str:
    cursor.execute("SELECT username, momo_balance FROM users ORDER BY momo_balance DESC LIMIT 10")
    leaders = [f"{i+1}. {row[0]} - {row[1]} Momo Coins" for i, row in enumerate(cursor.fetchall())]
    return LANGUAGES[lang]["leaderboard"].format(leaders="\n".join(leaders) if leaders else "No leaders yet.")

async def check_eligibility(wallet: str, chain: str) -> tuple[int, float]:
    try:
        token_balance = 0.0
        tier = 0
        if chain == "ETH":
            nft_contract_address = "your-nft-contract-address"  # Replace
            nft_abi = []  # Replace with NFT ABI
            nft_contract = web3_eth.eth.contract(address=nft_contract_address, abi=nft_abi)
            nft_balance = nft_contract.functions.balanceOf(wallet).call()
            token_balance = token_contract_eth.functions.balanceOf(wallet).call() / 10**18
            tier = min(3, max(1, nft_balance // 2))
        elif chain == "BSC":
            nft_contract_address = "your-nft-contract-address"  # Replace
            nft_abi = []  # Replace with NFT ABI
            nft_contract = web3_bsc.eth.contract(address=nft_contract_address, abi=nft_abi)
            nft_balance = nft_contract.functions.balanceOf(wallet).call()
            token_balance = token_contract_bsc.functions.balanceOf(wallet).call() / 10**18
            tier = min(3, max(1, nft_balance // 2))
        elif chain == "SOL":
            tier, token_balance = 1, 0.0  # Placeholder
        elif chain == "XRP":
            response = xrp_client.request({"method": "account_info", "params": [{"account": wallet}]})
            if "error" in response.result:
                tier, token_balance = 0, 0.0
            else:
                xrp_balance = float(response.result["account_data"]["Balance"]) / 10**6  # Drops to XRP
                tier = min(3, max(1, int(xrp_balance // 10)))  # 10 XRP = tier 1, etc.
                token_balance = xrp_balance
        min_balance = float(cursor.execute("SELECT value FROM config WHERE key = 'min_token_balance'").fetchone()[0])
        return tier if tier > 0 or token_balance >= min_balance else 0, token_balance
    except Exception as e:
        logger.error(f"Eligibility check failed for {wallet} on {chain}: {str(e)}")
        return 0, 0.0

# Commands
async def start(update: Update, context):
    user_id = str(update.message.from_user.id)
    user_name = update.message.from_user.first_name
    lang = get_user_language(user_id)

    referral_code = generate_referral_code(user_id)
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, language, referral_code, kyc_status, agreed_terms, has_seen_menu, joined_groups) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (user_id, user_name, lang, referral_code, "pending", 0, 0, 0))
    conn.commit()

    if context.args and context.args[0].startswith("start="):
        referrer_id = context.args[0].split("=")[1]
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
        referrer = cursor.fetchone()
        if referrer and referrer[0] != user_id:
            cursor.execute("SELECT referee_id FROM referrals WHERE referee_id = ?", (user_id,))
            if cursor.fetchone():
                await context.bot.send_message(chat_id=user_id, text=LANGUAGES[lang]["referral_duplicate"])
            else:
                cursor.execute("INSERT OR IGNORE INTO referrals (referrer_id, referee_id, timestamp) VALUES (?, ?, ?)",
                               (referrer[0], user_id, datetime.utcnow().isoformat()))
                cursor.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer[0], user_id))
                conn.commit()
                await context.bot.send_message(chat_id=referrer[0], text=LANGUAGES[lang]["referral_pending"].format(referee=user_name))
                if ADMIN_ID:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=LANGUAGES[lang]["referral_notification"].format(
                        referrer_id=referrer[0], referee_id=user_id, referee_name=user_name, time=datetime.utcnow().isoformat()))
                    logger.info(f"Admin notified of referral: {referrer[0]} -> {user_id}")

    if not has_seen_menu(user_id):
        keyboard = [[InlineKeyboardButton("Continue", callback_data="check_groups")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(LANGUAGES[lang]["mandatory_rules"], reply_markup=reply_markup, parse_mode='Markdown')
    else:
        balance = get_user_balance(user_id)
        reply_markup = get_main_menu(user_id, lang)
        await update.message.reply_text(LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code),
                                        reply_markup=reply_markup, parse_mode='Markdown')
    logger.info(f"User {user_name} ({user_id}) started the bot")

async def join_airdrop(update: Update, context):
    user_id = str(update.message.from_user.id)
    lang = get_user_language(user_id)
    keyboard = [[InlineKeyboardButton("Check Eligibility", callback_data="check_eligibility")],
                [InlineKeyboardButton("Back to Menu", callback_data="start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(LANGUAGES[lang]["join_airdrop"], reply_markup=reply_markup)

def get_main_menu(user_id, lang):
    keyboard = [
        [InlineKeyboardButton("Join Airdrop", callback_data="join_airdrop")],
        [InlineKeyboardButton("Check Balance", callback_data="balance"),
         InlineKeyboardButton("Terms", callback_data="terms")],
        [InlineKeyboardButton("KYC", callback_data="kyc_start"),
         InlineKeyboardButton("Submit Wallet", callback_data="submit_wallet")],
        [InlineKeyboardButton("Tasks", callback_data="tasks"),
         InlineKeyboardButton("Daily Tasks", callback_data="daily_tasks")],
        [InlineKeyboardButton("Refer", callback_data="refer"),
         InlineKeyboardButton("Claim Tokens", callback_data="claim_tokens")],
        [InlineKeyboardButton("Leaderboard", callback_data="leaderboard"),
         InlineKeyboardButton("KYC Status", callback_data="kyc_status")]
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("Admin: Start Distribution", callback_data="start_distribution"),
                         InlineKeyboardButton("Admin: Export Data", callback_data="export_data")])
        keyboard.append([InlineKeyboardButton("Admin: Blacklist", callback_data="blacklist"),
                         InlineKeyboardButton("Admin: Whitelist", callback_data="whitelist")])
        keyboard.append([InlineKeyboardButton("Admin: Set Config", callback_data="set_config"),
                         InlineKeyboardButton("Admin: Approve Tasks", callback_data="approve_tasks")])
        keyboard.append([InlineKeyboardButton("Admin: Approve KYC", callback_data="approve_kyc"),
                         InlineKeyboardButton("Admin: Approve Referrals", callback_data="approve_referrals")])
        keyboard.append([InlineKeyboardButton("Admin: Set Campaign", callback_data="set_campaign"),
                         InlineKeyboardButton("Admin: Edit Campaign", callback_data="edit_campaign")])
        keyboard.append([InlineKeyboardButton("Admin: Add Task", callback_data="add_daily_task"),
                         InlineKeyboardButton("Admin: Delete Task", callback_data="delete_daily_task")])
    return InlineKeyboardMarkup(keyboard)

async def button_handler(update: Update, context):
    query = update.callback_query
    user_id = str(query.from_user.id)
    lang = get_user_language(user_id)

    if query.data == "start":
        if not has_seen_menu(user_id):
            keyboard = [[InlineKeyboardButton("Continue", callback_data="check_groups")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(LANGUAGES[lang]["mandatory_rules"], reply_markup=reply_markup, parse_mode='Markdown')
        else:
            balance = get_user_balance(user_id)
            referral_code = generate_referral_code(user_id)
            reply_markup = get_main_menu(user_id, lang)
            await query.edit_message_text(LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code),
                                          reply_markup=reply_markup, parse_mode='Markdown')
        context.user_data.clear()

    elif query.data == "check_groups":
        if has_joined_groups(user_id):
            cursor.execute("UPDATE users SET has_seen_menu = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            balance = get_user_balance(user_id)
            referral_code = generate_referral_code(user_id)
            reply_markup = get_main_menu(user_id, lang)
            await query.edit_message_text(LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code),
                                          reply_markup=reply_markup, parse_mode='Markdown')
            logger.info(f"User {user_id} confirmed groups and saw main menu")
        else:
            keyboard = [[InlineKeyboardButton("I’ve Joined Both Groups", callback_data="confirm_groups")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(LANGUAGES[lang]["confirm_groups"], reply_markup=reply_markup, parse_mode='Markdown')

    elif query.data == "confirm_groups":
        cursor.execute("UPDATE users SET joined_groups = 1, has_seen_menu = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        balance = get_user_balance(user_id)
        referral_code = generate_referral_code(user_id)
        reply_markup = get_main_menu(user_id, lang)
        await query.edit_message_text(LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code),
                                      reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"User {user_id} confirmed group membership and proceeded to main menu")

    elif query.data == "join_airdrop":
        if not check_mandatory_tasks(user_id) or check_kyc_status(user_id) != "verified":
            keyboard = [[InlineKeyboardButton("Daily Tasks", callback_data="daily_tasks")],
                        [InlineKeyboardButton("KYC", callback_data="kyc_start")],
                        [InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(LANGUAGES[lang]["mandatory_missing"], reply_markup=reply_markup)
        else:
            keyboard = [[InlineKeyboardButton("Check Eligibility", callback_data="check_eligibility")],
                        [InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(LANGUAGES[lang]["join_airdrop"], reply_markup=reply_markup)

    elif query.data == "check_eligibility":
        cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
        submission = cursor.fetchone()
        if not submission:
            keyboard = [[InlineKeyboardButton("Submit Wallet", callback_data="submit_wallet")],
                        [InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Please submit your wallet first.", reply_markup=reply_markup)
        else:
            wallet, chain = submission[0], submission[1]
            tier, token_balance = await check_eligibility(wallet, chain)
            status = "Eligible" if tier > 0 and check_mandatory_tasks(user_id) and check_kyc_status(user_id) == "verified" else "Not Eligible"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(LANGUAGES[lang]["eligibility"].format(status=status), reply_markup=reply_markup)

    elif query.data == "balance":
        balance = get_user_balance(user_id)
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(LANGUAGES[lang]["balance"].format(balance=balance), reply_markup=reply_markup)

    elif query.data == "terms":
        vesting_days = cursor.execute("SELECT value FROM config WHERE key = 'vesting_period_days'").fetchone()[0]
        keyboard = [[InlineKeyboardButton(" Agree", callback_data="agree_terms")],
                    [InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(LANGUAGES[lang]["terms"].format(vesting_days=vesting_days), reply_markup=reply_markup)

    elif query.data == "agree_terms":
        cursor.execute("UPDATE users SET agreed_terms = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Terms agreed! Proceed with other actions.", reply_markup=reply_markup)

    elif query.data == "kyc_start":
        if check_kyc_status(user_id) == "verified":
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Your KYC is already verified!", reply_markup=reply_markup)
        else:
            context.user_data['kyc_step'] = "telegram"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(LANGUAGES[lang]["kyc_start"], reply_markup=reply_markup)

    elif query.data == "kyc_status":
        status = check_kyc_status(user_id)
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(LANGUAGES[lang]["kyc_status"].format(status=status), reply_markup=reply_markup)

    elif query.data == "submit_wallet":
        keyboard = [
            [InlineKeyboardButton("ETH", callback_data="wallet_eth"),
             InlineKeyboardButton("BSC", callback_data="wallet_bsc"),
             InlineKeyboardButton("SOL", callback_data="wallet_sol"),
             InlineKeyboardButton("XRP", callback_data="wallet_xrp")],
            [InlineKeyboardButton("Back to Menu", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.user_data['awaiting_wallet'] = True
        await query.edit_message_text(LANGUAGES[lang]["usage"], reply_markup=reply_markup)

    elif query.data.startswith("wallet_"):
        chain = query.data.split("_")[1].upper()
        context.user_data['chain'] = chain
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Enter your {chain} wallet address (e.g., 0x... or SoL... or r...):", reply_markup=reply_markup)

    elif query.data == "tasks":
        keyboard = [
            [InlineKeyboardButton("Task 1: Follow", callback_data="submit_task_1"),
             InlineKeyboardButton("Task 2: Retweet", callback_data="submit_task_2")],
            [InlineKeyboardButton("Back to Menu", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(LANGUAGES[lang]["tasks"], reply_markup=reply_markup)

    elif query.data.startswith("submit_task_"):
        task_id = query.data.split("_")[2]
        context.user_data['task_id'] = task_id
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Submit your Twitter proof link (e.g., https://twitter.com/...):", reply_markup=reply_markup)

    elif query.data == "daily_tasks":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cursor.execute("SELECT id, description, mandatory, task_link FROM daily_tasks WHERE active = 1")
        tasks = cursor.fetchall()
        task_list = "\n".join([f"ID: {task[0]} | {task[1]}{' (Mandatory)' if task[2] else ''}\nLink: {task[3]}" for task in tasks])
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(LANGUAGES[lang]["daily_tasks"].format(daily_tasks=task_list), reply_markup=reply_markup, parse_mode='Markdown')

    elif query.data == "refer":
        referral_code = generate_referral_code(user_id)
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Your referral link: {referral_code}\nShare this with friends!", reply_markup=reply_markup)

    elif query.data == "claim_tokens":
        cursor.execute("SELECT amount, vesting_end FROM distributions WHERE user_id = ? AND status = 'claimable'", (user_id,))
        distribution = cursor.fetchone()
        if not distribution:
            await query.edit_message_text("No claimable Momo Coins found.")
        else:
            amount, vesting_end = distribution
            if datetime.utcnow() < datetime.fromisoformat(vesting_end):
                await query.edit_message_text(f"Momo Coins are locked until {vesting_end}.")
            else:
                cursor.execute("UPDATE distributions SET status = 'claimed' WHERE user_id = ?", (user_id,))
                update_user_balance(user_id, amount)
                conn.commit()
                await query.edit_message_text(f"Successfully claimed {amount} Momo Coins! Check balance.")
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_reply_markup(reply_markup=reply_markup)

    elif query.data == "leaderboard":
        leaderboard_text = get_leaderboard(lang)
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(leaderboard_text, reply_markup=reply_markup)

    elif query.data == "start_distribution" and is_admin(user_id):
        await calculate_airdrop(1)
        cursor.execute("SELECT user_id, wallet, chain, amount FROM distributions WHERE status = 'pending'")
        distributions = cursor.fetchall()
        for dist_user_id, wallet, chain, amount in distributions:
            try:
                if chain == "ETH":
                    tx = token_contract_eth.functions.transfer(wallet, int(amount * 10**18)).build_transaction({
                        "from": ETH_SENDER_ADDRESS, "nonce": web3_eth.eth.get_transaction_count(ETH_SENDER_ADDRESS),
                        "gas": 100000, "gasPrice": web3_eth.eth.gas_price
                    })
                    signed_tx = web3_eth.eth.account.sign_transaction(tx, ETH_PRIVATE_KEY)
                    tx_hash = web3_eth.eth.send_raw_transaction(signed_tx.rawTransaction).hex()
                    cursor.execute("UPDATE distributions SET status = 'claimable', tx_hash = ? WHERE user_id = ?", (tx_hash, dist_user_id))
                elif chain == "XRP":
                    sender_wallet = Wallet.from_seed(XRP_SENDER_SEED)
                    payment = Payment(
                        account=sender_wallet.classic_address,
                        destination=wallet,
                        amount=xrp_to_drops(amount)
                    )
                    response = await asyncio.get_event_loop().run_in_executor(None, lambda: xrp_client.submit_and_wait(payment, sender_wallet))
                    tx_hash = response.result["tx_json"]["hash"]
                    cursor.execute("UPDATE distributions SET status = 'claimable', tx_hash = ? WHERE user_id = ?", (tx_hash, dist_user_id))
                elif chain == "SOL":
                    tx_hash = "placeholder_sol_tx_hash"  # Placeholder
                    cursor.execute("UPDATE distributions SET status = 'claimable', tx_hash = ? WHERE user_id = ?", (tx_hash, dist_user_id))
                elif chain == "BSC":
                    tx = token_contract_bsc.functions.transfer(wallet, int(amount * 10**18)).build_transaction({
                        "from": ETH_SENDER_ADDRESS, "nonce": web3_bsc.eth.get_transaction_count(ETH_SENDER_ADDRESS),
                        "gas": 100000, "gasPrice": web3_bsc.eth.gas_price
                    })
                    signed_tx = web3_bsc.eth.account.sign_transaction(tx, ETH_PRIVATE_KEY)
                    tx_hash = web3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction).hex()
                    cursor.execute("UPDATE distributions SET status = 'claimable', tx_hash = ? WHERE user_id = ?", (tx_hash, dist_user_id))
                conn.commit()
                await context.bot.send_message(chat_id=dist_user_id, text=LANGUAGES[lang]["sent_tokens"].format(amount=amount, wallet=wallet, tx_hash=tx_hash))
            except Exception as e:
                logger.error(f"Failed to send {amount} to {wallet} on {chain}: {e}")
                await context.bot.send_message(chat_id=dist_user_id, text=LANGUAGES[lang]["failed_tokens"].format(amount=amount, wallet=wallet, error=str(e)))
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Airdrop distribution started!", reply_markup=reply_markup)
        logger.info("Airdrop distribution initiated")

    elif query.data == "export_data" and is_admin(user_id):
        wb = Workbook()
        ws = wb.active
        ws.append(["User ID", "Wallet", "Chain", "Amount", "Status", "Tx Hash", "Vesting End"])
        cursor.execute("SELECT user_id, wallet, chain, amount, status, tx_hash, vesting_end FROM distributions")
        for row in cursor.fetchall():
            ws.append(row)
        wb.save("airdrop_log.xlsx")
        await context.bot.send_document(chat_id=query.message.chat_id, document=open("airdrop_log.xlsx", "rb"))
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Data exported!", reply_markup=reply_markup)
        logger.info("Exported airdrop data to Excel")

    elif query.data == "blacklist" and is_admin(user_id):
        context.user_data['awaiting_blacklist'] = True
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Enter wallet to blacklist:", reply_markup=reply_markup)

    elif query.data == "whitelist" and is_admin(user_id):
        context.user_data['awaiting_whitelist'] = True
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Enter wallet to whitelist:", reply_markup=reply_markup)

    elif query.data == "set_config" and is_admin(user_id):
        context.user_data['awaiting_config'] = True
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Enter config key and value (e.g., total_supply 2000000):", reply_markup=reply_markup)

    elif query.data == "approve_tasks" and is_admin(user_id):
        cursor.execute("SELECT user_id, task_id, username, completion_date FROM task_completions WHERE status = 'pending' LIMIT 10")
        pending = cursor.fetchall()
        if not pending:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("No pending task submissions.", reply_markup=reply_markup)
        else:
            keyboard = []
            for task in pending:
                user_id, task_id, username, date = task
                keyboard.append([InlineKeyboardButton(f"Approve {user_id} - Task {task_id} ({username})",
                                                      callback_data=f"approve_task_{user_id}_{task_id}_{date}"),
                                 InlineKeyboardButton(f"Reject {user_id} - Task {task_id}",
                                                      callback_data=f"reject_task_{user_id}_{task_id}_{date}")])
            keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Pending task submissions:", reply_markup=reply_markup)

    elif query.data.startswith("approve_task_") and is_admin(user_id):
        task_user_id, task_id, completion_date = query.data.split("_")[2], query.data.split("_")[3], query.data.split("_")[4]
        cursor.execute("UPDATE task_completions SET status = 'approved' WHERE user_id = ? AND task_id = ? AND completion_date = ?",
                       (task_user_id, task_id, completion_date))
        update_user_balance(task_user_id, 10)
        conn.commit()
        cursor.execute("SELECT description FROM daily_tasks WHERE id = ?", (task_id,))
        task_description = cursor.fetchone()[0]
        await context.bot.send_message(chat_id=task_user_id, text=LANGUAGES[lang]["task_approved"].format(task_description=task_description))
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Task {task_id} for user {task_user_id} approved!", reply_markup=reply_markup)

    elif query.data.startswith("reject_task_") and is_admin(user_id):
        task_user_id, task_id, completion_date = query.data.split("_")[2], query.data.split("_")[3], query.data.split("_")[4]
        cursor.execute("UPDATE task_completions SET status = 'rejected' WHERE user_id = ? AND task_id = ? AND completion_date = ?",
                       (task_user_id, task_id, completion_date))
        conn.commit()
        cursor.execute("SELECT description FROM daily_tasks WHERE id = ?", (task_id,))
        task_description = cursor.fetchone()[0]
        await context.bot.send_message(chat_id=task_user_id, text=LANGUAGES[lang]["task_rejected"].format(task_description=task_description))
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Task {task_id} for user {task_user_id} rejected!", reply_markup=reply_markup)

    elif query.data == "approve_kyc" and is_admin(user_id):
        cursor.execute("SELECT user_id, kyc_telegram_link, kyc_x_link, kyc_wallet, kyc_chain, kyc_submission_time FROM users WHERE kyc_status = 'submitted' LIMIT 10")
        pending = cursor.fetchall()
        if not pending:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("No pending KYC submissions.", reply_markup=reply_markup)
        else:
            keyboard = []
            for kyc in pending:
                user_id, telegram, x_link, wallet, chain, time = kyc
                keyboard.append([InlineKeyboardButton(f"Approve {user_id} (TG: {telegram})",
                                                      callback_data=f"approve_kyc_{user_id}"),
                                 InlineKeyboardButton(f"Reject {user_id}",
                                                      callback_data=f"reject_kyc_{user_id}")])
            keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Pending KYC submissions:", reply_markup=reply_markup)

    elif query.data.startswith("approve_kyc_") and is_admin(user_id):
        kyc_user_id = query.data.split("_")[2]
        cursor.execute("UPDATE users SET kyc_status = 'verified' WHERE user_id = ?", (kyc_user_id,))
        conn.commit()
        await context.bot.send_message(chat_id=kyc_user_id, text=LANGUAGES[lang]["kyc_approved"])
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"KYC for user {kyc_user_id} approved!", reply_markup=reply_markup)
        logger.info(f"Admin {user_id} approved KYC for {kyc_user_id}")

    elif query.data.startswith("reject_kyc_") and is_admin(user_id):
        kyc_user_id = query.data.split("_")[2]
        cursor.execute("UPDATE users SET kyc_status = 'rejected' WHERE user_id = ?", (kyc_user_id,))
        conn.commit()
        await context.bot.send_message(chat_id=kyc_user_id, text=LANGUAGES[lang]["kyc_rejected"])
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"KYC for user {kyc_user_id} rejected!", reply_markup=reply_markup)
        logger.info(f"Admin {user_id} rejected KYC for {kyc_user_id}")

    elif query.data == "approve_referrals" and is_admin(user_id):
        cursor.execute("SELECT referrer_id, referee_id, timestamp FROM referrals WHERE status = 'pending' LIMIT 10")
        pending = cursor.fetchall()
        if not pending:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("No pending referral submissions.", reply_markup=reply_markup)
        else:
            keyboard = []
            for ref in pending:
                referrer_id, referee_id, timestamp = ref
                cursor.execute("SELECT username FROM users WHERE user_id = ?", (referee_id,))
                referee_name = cursor.fetchone()[0] if cursor.fetchone() else "Unknown"
                keyboard.append([InlineKeyboardButton(f"Approve {referrer_id} -> {referee_id} ({referee_name})",
                                                      callback_data=f"approve_ref_{referrer_id}_{referee_id}"),
                                 InlineKeyboardButton(f"Reject {referrer_id} -> {referee_id}",
                                                      callback_data=f"reject_ref_{referrer_id}_{referee_id}")])
            keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Pending referral submissions:", reply_markup=reply_markup)

    elif query.data.startswith("approve_ref_") and is_admin(user_id):
        referrer_id, referee_id = query.data.split("_")[2], query.data.split("_")[3]
        cursor.execute("UPDATE referrals SET status = 'approved' WHERE referrer_id = ? AND referee_id = ?", (referrer_id, referee_id))
        update_user_balance(referrer_id, 15)
        conn.commit()
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (referee_id,))
        referee_name = cursor.fetchone()[0] if cursor.fetchone() else "Unknown"
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(chat_id=referrer_id, text=LANGUAGES[lang]["referral_bonus"].format(bonus=15, referee=referee_name),
                                       reply_markup=reply_markup)
        await context.bot.send_message(chat_id=referee_id, text=LANGUAGES[lang]["referral_approved"].format(referee=referee_name))
        await query.edit_message_text(f"Referral from {referrer_id} to {referee_id} approved!", reply_markup=reply_markup)
        logger.info(f"Admin {user_id} approved referral from {referrer_id} to {referee_id}")

    elif query.data.startswith("reject_ref_") and is_admin(user_id):
        referrer_id, referee_id = query.data.split("_")[2], query.data.split("_")[3]
        cursor.execute("UPDATE referrals SET status = 'rejected' WHERE referrer_id = ? AND referee_id = ?", (referrer_id, referee_id))
        conn.commit()
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (referee_id,))
        referee_name = cursor.fetchone()[0] if cursor.fetchone() else "Unknown"
        await context.bot.send_message(chat_id=referee_id, text=LANGUAGES[lang]["referral_rejected"].format(referee=referee_name))
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Referral from {referrer_id} to {referee_id} rejected!", reply_markup=reply_markup)
        logger.info(f"Admin {user_id} rejected referral from {referrer_id} to {referee_id}")

    elif query.data == "set_campaign" and is_admin(user_id):
        context.user_data['awaiting_campaign'] = True
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Enter campaign details (name start_date end_date total_tokens, e.g., 'Summer 2025-03-01 2025-03-15 500000'):",
                                      reply_markup=reply_markup)

    elif query.data == "edit_campaign" and is_admin(user_id):
        cursor.execute("SELECT id, name FROM campaigns WHERE active = 1")
        campaigns = cursor.fetchall()
        if not campaigns:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("No active campaigns.", reply_markup=reply_markup)
        else:
            keyboard = [[InlineKeyboardButton(f"Edit {camp[1]} (ID: {camp[0]})", callback_data=f"edit_campaign_{camp[0]}")] for camp in campaigns]
            keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Select campaign to edit:", reply_markup=reply_markup)

    elif query.data.startswith("edit_campaign_") and is_admin(user_id):
        campaign_id = query.data.split("_")[2]
        context.user_data['awaiting_campaign_edit'] = campaign_id
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Enter new campaign details (name start_date end_date total_tokens, e.g., 'Summer 2025-03-01 2025-03-15 500000'):",
                                      reply_markup=reply_markup)

    elif query.data == "add_daily_task" and is_admin(user_id):
        context.user_data['awaiting_task_add'] = True
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Enter task details (description link mandatory, e.g., 'Watch Video https://youtube.com/example 0'):",
                                      reply_markup=reply_markup)

    elif query.data == "delete_daily_task" and is_admin(user_id):
        cursor.execute("SELECT id, description FROM daily_tasks WHERE active = 1")
        tasks = cursor.fetchall()
        if not tasks:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("No active tasks.", reply_markup=reply_markup)
        else:
            keyboard = [[InlineKeyboardButton(f"Delete {task[1]} (ID: {task[0]})", callback_data=f"delete_task_{task[0]}")] for task in tasks]
            keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Select task to delete:", reply_markup=reply_markup)

    elif query.data.startswith("delete_task_") and is_admin(user_id):
        task_id = query.data.split("_")[2]
        cursor.execute("UPDATE daily_tasks SET active = 0 WHERE id = ?", (task_id,))
        conn.commit()
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Task {task_id} deleted!", reply_markup=reply_markup)

    await query.answer()

async def handle_message(update: Update, context):
    user_id = str(update.message.from_user.id)
    lang = get_user_language(user_id)
    text = update.message.text.strip()

    # KYC Steps
    if context.user_data.get('kyc_step') == "telegram":
        if not is_valid_telegram_link(text):
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["kyc_telegram_invalid"], reply_markup=reply_markup)
            return
        context.user_data['kyc_telegram_link'] = text
        context.user_data['kyc_step'] = "x_link"
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(LANGUAGES[lang]["kyc_telegram"].format(telegram=text), reply_markup=reply_markup)
        return

    elif context.user_data.get('kyc_step') == "x_link":
        if not is_valid_x_link(text):
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["kyc_x_link_invalid"], reply_markup=reply_markup)
            return
        context.user_data['kyc_x_link'] = text
        context.user_data['kyc_step'] = "wallet"
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("X link received: {}. Now provide your wallet address (e.g., 'ETH 0x...' or 'XRP r...'):".format(text), reply_markup=reply_markup)
        return

    elif context.user_data.get('kyc_step') == "wallet":
        try:
            chain, wallet = text.split(maxsplit=1)
            chain = chain.upper()
            if chain not in ["ETH", "BSC", "SOL", "XRP"] or not is_valid_address(wallet, chain):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(LANGUAGES[lang]["kyc_wallet_invalid"], reply_markup=reply_markup)
                return
            context.user_data['kyc_wallet'] = wallet
            context.user_data['kyc_chain'] = chain
            submission_time = datetime.utcnow().isoformat()
            cursor.execute("UPDATE users SET kyc_telegram_link = ?, kyc_x_link = ?, kyc_wallet = ?, kyc_chain = ?, kyc_status = 'submitted', kyc_submission_time = ? WHERE user_id = ?",
                           (context.user_data['kyc_telegram_link'], context.user_data['kyc_x_link'], wallet, chain, submission_time, user_id))
            cursor.execute("INSERT OR IGNORE INTO submissions (user_id, wallet, chain, timestamp) VALUES (?, ?, ?, ?)",
                           (user_id, wallet, chain, submission_time))
            conn.commit()
            if ADMIN_ID:
                await context.bot.send_message(chat_id=ADMIN_ID, text=LANGUAGES[lang]["kyc_notification"].format(
                    user_id=user_id, telegram=context.user_data['kyc_telegram_link'], x_link=context.user_data['kyc_x_link'], wallet=wallet, chain=chain, time=submission_time))
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["kyc_complete"].format(
                telegram=context.user_data['kyc_telegram_link'], x_link=context.user_data['kyc_x_link'], wallet=wallet, chain=chain), reply_markup=reply_markup)
            context.user_data.clear()
        except ValueError:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["kyc_wallet_invalid"], reply_markup=reply_markup)
        return

    if context.user_data.get('awaiting_wallet'):
        wallet = text
        chain = context.user_data.get('chain')
        if not is_valid_address(wallet, chain):
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["invalid_address"].format(chain=chain), reply_markup=reply_markup)
            context.user_data['awaiting_wallet'] = False
            return
        cursor.execute("SELECT wallet FROM blacklist WHERE wallet = ?", (wallet,))
        if cursor.fetchone():
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["blacklisted"], reply_markup=reply_markup)
            context.user_data['awaiting_wallet'] = False
            return
        cursor.execute("SELECT wallet FROM submissions WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["already_submitted"], reply_markup=reply_markup)
            context.user_data['awaiting_wallet'] = False
            return
        captcha = random.randint(1, 10)
        cursor.execute("REPLACE INTO captchas (user_id, captcha, timestamp) VALUES (?, ?, ?)",
                       (user_id, captcha, datetime.utcnow().isoformat()))
        cursor.execute("REPLACE INTO submissions (user_id, wallet, chain, timestamp) VALUES (?, ?, ?, ?)",
                       (user_id, wallet, chain, datetime.utcnow().isoformat()))
        conn.commit()
        context.user_data['awaiting_wallet'] = False
        context.user_data['awaiting_captcha'] = True
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(LANGUAGES[lang]["captcha"].format(captcha=captcha), reply_markup=reply_markup)

    elif context.user_data.get('awaiting_captcha'):
        try:
            user_answer = int(text)
            cursor.execute("SELECT captcha FROM captchas WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            if not result:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("No CAPTCHA found.", reply_markup=reply_markup)
                return
            if user_answer == result[0] + 5:
                await verify_wallet(user_id, update.message.chat_id, update.message.reply_text, lang)
            else:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("Wrong answer. Try submitting wallet again.", reply_markup=reply_markup)
        except ValueError:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Please enter a number.", reply_markup=reply_markup)
        context.user_data['awaiting_captcha'] = False

    elif context.user_data.get('awaiting_task_add'):
        try:
            description, task_link, mandatory = text.split(maxsplit=2)
            mandatory = int(mandatory)
            cursor.execute("INSERT INTO daily_tasks (description, reward, mandatory, task_link) VALUES (?, 10, ?, ?)",
                           (description, mandatory, task_link))
            conn.commit()
            context.user_data['awaiting_task_add'] = False
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(f"Added daily task: {description} with link {task_link}", reply_markup=reply_markup)
            logger.info(f"Admin {user_id} added daily task: {description}")
        except ValueError:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Format: description link mandatory (e.g., 'Watch Video https://youtube.com/example 0')", reply_markup=reply_markup)

    elif context.user_data.get('awaiting_blacklist'):
        wallet = text
        cursor.execute("INSERT OR IGNORE INTO blacklist (wallet) VALUES (?)", (wallet,))
        conn.commit()
        context.user_data['awaiting_blacklist'] = False
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"{wallet} blacklisted.", reply_markup=reply_markup)
        logger.info(f"Blacklisted wallet: {wallet}")

    elif context.user_data.get('awaiting_whitelist'):
        wallet = text
        cursor.execute("INSERT OR IGNORE INTO whitelist (wallet) VALUES (?)", (wallet,))
        conn.commit()
        context.user_data['awaiting_whitelist'] = False
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"{wallet} whitelisted.", reply_markup=reply_markup)
        logger.info(f"Whitelisted wallet: {wallet}")

    elif context.user_data.get('awaiting_config'):
        try:
            key, value = text.split()
            cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
            context.user_data['awaiting_config'] = False
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(f"Set {key} = {value}", reply_markup=reply_markup)
            logger.info(f"Config updated: {key} = {value}")
        except ValueError:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Format: key value", reply_markup=reply_markup)

    elif context.user_data.get('awaiting_campaign'):
        try:
            name, start_date, end_date, total_tokens = text.split()
            total_tokens = float(total_tokens)
            cursor.execute("INSERT INTO campaigns (name, start_date, end_date, total_tokens) VALUES (?, ?, ?, ?)",
                           (name, start_date, end_date, total_tokens))
            conn.commit()
            context.user_data['awaiting_campaign'] = False
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["campaign_set"].format(name=name, start=start_date, end=end_date, tokens=total_tokens),
                                            reply_markup=reply_markup)
            logger.info(f"Admin {user_id} set campaign: {name}")
        except ValueError:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Format: name start_date end_date total_tokens (e.g., 'Summer 2025-03-01 2025-03-15 500000')", reply_markup=reply_markup)

    elif context.user_data.get('awaiting_campaign_edit'):
        campaign_id = context.user_data['awaiting_campaign_edit']
        try:
            name, start_date, end_date, total_tokens = text.split()
            total_tokens = float(total_tokens)
            cursor.execute("UPDATE campaigns SET name = ?, start_date = ?, end_date = ?, total_tokens = ? WHERE id = ?",
                           (name, start_date, end_date, total_tokens, campaign_id))
            conn.commit()
            context.user_data['awaiting_campaign_edit'] = None
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["campaign_edit"].format(name=name, start=start_date, end=end_date, tokens=total_tokens),
                                            reply_markup=reply_markup)
            logger.info(f"Admin {user_id} edited campaign {campaign_id}: {name}")
        except ValueError:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Format: name start_date end_date total_tokens (e.g., 'Summer 2025-03-01 2025-03-15 500000')", reply_markup=reply_markup)

    elif context.user_data.get('task_id'):
        task_id = context.user_data['task_id']
        username = text
        if task_id in ["1", "2"]:
            cursor.execute("UPDATE eligible SET social_tasks_completed = social_tasks_completed + 1 WHERE user_id = ?", (user_id,))
            update_user_balance(user_id, 10)
            conn.commit()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["task_completed"].format(task_description=f"Task {task_id}"), reply_markup=reply_markup)
        else:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Invalid task ID.", reply_markup=reply_markup)
        context.user_data['task_id'] = None

    else:
        try:
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError("Invalid format")
            task_id, username = parts
            task_id = int(task_id)
            today = datetime.utcnow().strftime("%Y-%m-%d")
            cursor.execute("SELECT id, description FROM daily_tasks WHERE id = ? AND active = 1", (task_id,))
            task = cursor.fetchone()
            if not task:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("Invalid or inactive task ID.", reply_markup=reply_markup)
                return
            cursor.execute("SELECT completion_date FROM task_completions WHERE user_id = ? AND task_id = ? AND completion_date = ?",
                           (user_id, task_id, today))
            if cursor.fetchone():
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("You’ve already submitted this task today.", reply_markup=reply_markup)
                return
            cursor.execute("INSERT INTO task_completions (user_id, task_id, completion_date, username) VALUES (?, ?, ?, ?)",
                           (user_id, task_id, today, username))
            conn.commit()
            task_description = task[1]
            if ADMIN_ID:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"User {user_id} submitted username '{username}' for task '{task_description}' on {today}")
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(LANGUAGES[lang]["task_completed"].format(task_description=task_description), reply_markup=reply_markup)
        except ValueError:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Invalid format. Use: task_id username (e.g., '1 @username')", reply_markup=reply_markup)

async def verify_wallet(user_id, chat_id, reply_func, lang):
    cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if result:
        wallet, chain = result
        tier, token_balance = await check_eligibility(wallet, chain)
        if tier > 0:
            cursor.execute("REPLACE INTO eligible (user_id, wallet, chain, tier, verified, token_balance, social_tasks_completed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (user_id, wallet, chain, tier, 1, token_balance, 0))
            conn.commit()
            await reply_func(LANGUAGES[lang]["verified"].format(tier=tier))
        else:
            await reply_func(LANGUAGES[lang]["no_assets"])
    else:
        await reply_func("No wallet submission found.")

async def calculate_airdrop(campaign_id):
    cursor.execute("SELECT total_tokens FROM campaigns WHERE id = ? AND active = 1", (campaign_id,))
    total_tokens = cursor.fetchone()[0]
    cursor.execute("SELECT user_id, tier FROM eligible WHERE verified = 1")
    eligible_users = cursor.fetchall()
    total_tiers = sum(user[1] for user in eligible_users)
    if total_tiers == 0:
        return
    token_per_tier = total_tokens / total_tiers
    vesting_days = int(cursor.execute("SELECT value FROM config WHERE key = 'vesting_period_days'").fetchone()[0])
    vesting_end = (datetime.utcnow() + timedelta(days=vesting_days)).isoformat()
    for user_id, tier in eligible_users:
        amount = token_per_tier * tier
        cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
        wallet, chain = cursor.fetchone()
        cursor.execute("REPLACE INTO distributions (user_id, wallet, chain, amount, status, vesting_end) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, wallet, chain, amount, "pending", vesting_end))
    conn.commit()

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    dp = application

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("join_airdrop", join_airdrop))
    dp.add_handler(CallbackQueryHandler(button_handler))
    dp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Momo Coin Airdrop bot started")
    application.run_polling()

if __name__ == "__main__":
    main()