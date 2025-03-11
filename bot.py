import os
import random
import asyncio
import sqlite3
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Union
import discord
from discord.ext import commands as discord_commands
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
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
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
    CREATE TABLE IF NOT EXISTS admin_states (
        user_id TEXT PRIMARY KEY,
        state TEXT,
        task_id TEXT,
        timestamp TEXT
    );
''')

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

# Sample Campaign and Daily Tasks
cursor.execute("INSERT OR IGNORE INTO campaigns (name, start_date, end_date, total_tokens, active) VALUES (?, ?, ?, ?, ?)",
               ("Launch Airdrop", datetime.utcnow().isoformat(), (datetime.utcnow() + timedelta(days=7)).isoformat(), 1000000, 1))
conn.commit()

cursor.executescript("DELETE FROM daily_tasks")  # Reset for consistency
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
    cursor.execute("INSERT OR IGNORE INTO daily_tasks (description, reward, mandatory, task_link, active) VALUES (?, ?, ?, ?, 1)",
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
        "kyc_rejected": "Your KYC has been rejected. Please resubmit.",
        "edit_task_prompt": "Enter new task details (task_id description reward mandatory link, e.g., '1 Watch Video 10 0 https://youtube.com/example'):",
        "task_edited": "Task ID {task_id} updated: {description}, Reward: {reward}, Mandatory: {mandatory}, Link: {task_link}"
    }
}

# Rate Limiting
CALLS_PER_MINUTE = 10
PERIOD = 60

@sleep_and_retry
@limits(calls=CALLS_PER_MINUTE, period=PERIOD)
def rate_limited_request(url, payload):
    return requests.post(url, json=payload).json()

# Unified Context Class
class BotContext:
    def __init__(self, platform: str, user_data: dict = None):
        self.platform = platform
        self.user_data = user_data or {}
        self.bot = None

    async def send_message(self, chat_id: str, text: str, reply_markup=None):
        try:
            if self.platform == "telegram":
                # Add rate limiting for Telegram
                await asyncio.sleep(0.1)  # Small delay to avoid rate limits
                await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
            elif self.platform == "discord":
                channel = self.bot.get_channel(int(chat_id)) if chat_id.isdigit() else await self.bot.fetch_user(int(chat_id))
                if not channel and isinstance(channel, discord.User):
                    logger.error(f"Cannot find channel or user for chat_id: {chat_id}")
                    raise Exception(f"Invalid chat_id: {chat_id}")
                if reply_markup:
                    text += "\n\nOptions:\n" + "\n".join([f"- {btn[0].text} (!Birdz {btn[0].callback_data})" for btn in reply_markup.inline_keyboard])
                # Add rate limiting for Discord
                await asyncio.sleep(0.1)  # Small delay to avoid rate limits
                await (channel.send(text) if isinstance(channel, discord.abc.Messageable) else channel.send(text))
        except Exception as e:
            logger.error(f"Error in send_message (platform: {self.platform}, chat_id: {chat_id}): {str(e)}")
            raise

    async def send_document(self, chat_id: str, document):
        if self.platform == "telegram":
            await self.bot.send_document(chat_id=chat_id, document=document)
        elif self.platform == "discord":
            channel = self.bot.get_channel(int(chat_id)) if chat_id.isdigit() else await self.bot.fetch_user(int(chat_id))
            if channel or isinstance(channel, discord.User):
                await (channel.send(file=discord.File(document)) if isinstance(channel, discord.abc.Messageable) else channel.send(file=discord.File(document)))

# Helper Functions
def is_admin(user_id):
    return str(user_id) == ADMIN_ID

def generate_referral_code(user_id):
    return f"https://t.me/{BOT_USERNAME}?start={user_id}" if BOT_USERNAME else f"!start {user_id}"

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
                xrp_balance = float(response.result["account_data"]["Balance"]) / 10**6
                tier = min(3, max(1, int(xrp_balance // 10)))
                token_balance = xrp_balance
        min_balance = float(cursor.execute("SELECT value FROM config WHERE key = 'min_token_balance'").fetchone()[0])
        return tier if tier > 0 or token_balance >= min_balance else 0, token_balance
    except Exception as e:
        logger.error(f"Eligibility check failed for {wallet} on {chain}: {str(e)}")
        return 0, 0.0

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
        keyboard.extend([
            [InlineKeyboardButton("Admin: Start Distribution", callback_data="start_distribution"),
             InlineKeyboardButton("Admin: Export Data", callback_data="export_data")],
            [InlineKeyboardButton("Admin: Blacklist", callback_data="blacklist"),
             InlineKeyboardButton("Admin: Whitelist", callback_data="whitelist")],
            [InlineKeyboardButton("Admin: Set Config", callback_data="set_config"),
             InlineKeyboardButton("Admin: Approve Tasks", callback_data="approve_tasks")],
            [InlineKeyboardButton("Admin: Approve KYC", callback_data="approve_kyc"),
             InlineKeyboardButton("Admin: Approve Referrals", callback_data="approve_referrals")],
            [InlineKeyboardButton("Admin: Set Campaign", callback_data="set_campaign"),
             InlineKeyboardButton("Admin: Edit Campaign", callback_data="edit_campaign")],
            [InlineKeyboardButton("Admin: Add Task", callback_data="add_daily_task"),
             InlineKeyboardButton("Admin: Edit Task", callback_data="edit_daily_task")],
            [InlineKeyboardButton("Admin: Delete Task", callback_data="delete_daily_task")],
            [InlineKeyboardButton("Admin: Test Message", callback_data="test_message")]  # Added for debugging
        ])
    return InlineKeyboardMarkup(keyboard)

# Core Bot Logic
class AirdropBot:
    def __init__(self):
        self.telegram_app = None
        self.discord_bot = None

    async def start(self, update: Union[Update, discord.Message], context: BotContext):
        user_id = str(update.message.from_user.id if context.platform == "telegram" else update.author.id)
        user_name = update.message.from_user.first_name if context.platform == "telegram" else update.author.name
        lang = get_user_language(user_id)
        chat_id = str(update.message.chat_id if context.platform == "telegram" else update.channel.id)

        referral_code = generate_referral_code(user_id)
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username, language, referral_code, kyc_status, agreed_terms, has_seen_menu, joined_groups) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (user_id, user_name, lang, referral_code, "pending", 0, 0, 0))
        conn.commit()

        args = update.message.text.split() if context.platform == "telegram" else update.content.split()
        if len(args) > 1 and args[1].startswith("start="):
            referrer_id = args[1].split("=")[1]
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
            referrer = cursor.fetchone()
            if referrer and referrer[0] != user_id:
                cursor.execute("SELECT referee_id FROM referrals WHERE referee_id = ?", (user_id,))
                if cursor.fetchone():
                    await context.send_message(chat_id, LANGUAGES[lang]["referral_duplicate"])
                else:
                    cursor.execute("INSERT OR IGNORE INTO referrals (referrer_id, referee_id, timestamp) VALUES (?, ?, ?)",
                                   (referrer[0], user_id, datetime.utcnow().isoformat()))
                    cursor.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer[0], user_id))
                    conn.commit()
                    await context.send_message(referrer[0], LANGUAGES[lang]["referral_pending"].format(referee=user_name))
                    if ADMIN_ID:
                        await context.send_message(ADMIN_ID, LANGUAGES[lang]["referral_notification"].format(
                            referrer_id=referrer[0], referee_id=user_id, referee_name=user_name, time=datetime.utcnow().isoformat()))
                        logger.info(f"Admin notified of referral: {referrer[0]} -> {user_id}")

        if not has_seen_menu(user_id):
            keyboard = [[InlineKeyboardButton("Continue", callback_data="check_groups")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["mandatory_rules"], reply_markup)
        else:
            balance = get_user_balance(user_id)
            reply_markup = get_main_menu(user_id, lang)
            await context.send_message(chat_id, LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code), reply_markup)
        logger.info(f"User {user_name} ({user_id}) started the bot")

    async def join_airdrop(self, update: Union[Update, discord.Message], context: BotContext):
        user_id = str(update.message.from_user.id if context.platform == "telegram" else update.author.id)
        lang = get_user_language(user_id)
        chat_id = str(update.message.chat_id if context.platform == "telegram" else update.channel.id)
        keyboard = [[InlineKeyboardButton("Check Eligibility", callback_data="check_eligibility")],
                    [InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.send_message(chat_id, LANGUAGES[lang]["join_airdrop"], reply_markup)

    async def button_handler(self, update: Union[Update, discord.Message], context: BotContext):
        user_id = str(update.callback_query.from_user.id if context.platform == "telegram" else update.author.id)
        lang = get_user_language(user_id)
        chat_id = str(update.callback_query.message.chat_id if context.platform == "telegram" else update.channel.id)
        data = update.callback_query.data if context.platform == "telegram" else update.content.split()[1] if len(update.content.split()) > 1 else ""

        if data == "start":
            if not has_seen_menu(user_id):
                keyboard = [[InlineKeyboardButton("Continue", callback_data="check_groups")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["mandatory_rules"], reply_markup)
            else:
                balance = get_user_balance(user_id)
                referral_code = generate_referral_code(user_id)
                reply_markup = get_main_menu(user_id, lang)
                await context.send_message(chat_id, LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code), reply_markup)
            context.user_data.clear()

        elif data == "check_groups":
            if has_joined_groups(user_id):
                cursor.execute("UPDATE users SET has_seen_menu = 1 WHERE user_id = ?", (user_id,))
                conn.commit()
                balance = get_user_balance(user_id)
                referral_code = generate_referral_code(user_id)
                reply_markup = get_main_menu(user_id, lang)
                await context.send_message(chat_id, LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code), reply_markup)
            else:
                keyboard = [[InlineKeyboardButton("I’ve Joined Both Groups", callback_data="confirm_groups")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["confirm_groups"], reply_markup)

        elif data == "confirm_groups":
            cursor.execute("UPDATE users SET joined_groups = 1, has_seen_menu = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            balance = get_user_balance(user_id)
            referral_code = generate_referral_code(user_id)
            reply_markup = get_main_menu(user_id, lang)
            await context.send_message(chat_id, LANGUAGES[lang]["welcome"].format(balance=balance, ref_link=referral_code), reply_markup)

        elif data == "join_airdrop":
            if not check_mandatory_tasks(user_id) or check_kyc_status(user_id) != "verified":
                keyboard = [[InlineKeyboardButton("Daily Tasks", callback_data="daily_tasks")],
                            [InlineKeyboardButton("KYC", callback_data="kyc_start")],
                            [InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["mandatory_missing"], reply_markup)
            else:
                keyboard = [[InlineKeyboardButton("Check Eligibility", callback_data="check_eligibility")],
                            [InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["join_airdrop"], reply_markup)

        elif data == "check_eligibility":
            cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
            submission = cursor.fetchone()
            if not submission:
                keyboard = [[InlineKeyboardButton("Submit Wallet", callback_data="submit_wallet")],
                            [InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Please submit your wallet first.", reply_markup)
            else:
                wallet, chain = submission
                tier, token_balance = await check_eligibility(wallet, chain)
                status = "Eligible" if tier > 0 and check_mandatory_tasks(user_id) and check_kyc_status(user_id) == "verified" else "Not Eligible"
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["eligibility"].format(status=status), reply_markup)

        elif data == "balance":
            balance = get_user_balance(user_id)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["balance"].format(balance=balance), reply_markup)

        elif data == "terms":
            vesting_days = cursor.execute("SELECT value FROM config WHERE key = 'vesting_period_days'").fetchone()[0]
            keyboard = [[InlineKeyboardButton(" Agree", callback_data="agree_terms")],
                        [InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["terms"].format(vesting_days=vesting_days), reply_markup)

        elif data == "agree_terms":
            cursor.execute("UPDATE users SET agreed_terms = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Terms agreed! Proceed with other actions.", reply_markup)

        elif data == "kyc_start":
            if check_kyc_status(user_id) == "verified":
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Your KYC is already verified!", reply_markup)
            else:
                context.user_data['kyc_step'] = "telegram"
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["kyc_start"], reply_markup)

        elif data == "kyc_status":
            status = check_kyc_status(user_id)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["kyc_status"].format(status=status), reply_markup)

        elif data == "submit_wallet":
            keyboard = [
                [InlineKeyboardButton("ETH", callback_data="wallet_eth"),
                 InlineKeyboardButton("BSC", callback_data="wallet_bsc"),
                 InlineKeyboardButton("SOL", callback_data="wallet_sol"),
                 InlineKeyboardButton("XRP", callback_data="wallet_xrp")],
                [InlineKeyboardButton("Back to Menu", callback_data="start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data['awaiting_wallet'] = True
            await context.send_message(chat_id, LANGUAGES[lang]["usage"], reply_markup)

        elif data.startswith("wallet_"):
            chain = data.split("_")[1].upper()
            context.user_data['chain'] = chain
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Enter your {chain} wallet address (e.g., 0x... or SoL... or r...):", reply_markup)

        elif data == "tasks":
            keyboard = [
                [InlineKeyboardButton("Task 1: Follow", callback_data="submit_task_1"),
                 InlineKeyboardButton("Task 2: Retweet", callback_data="submit_task_2")],
                [InlineKeyboardButton("Back to Menu", callback_data="start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["tasks"], reply_markup)

        elif data.startswith("submit_task_"):
            task_id = data.split("_")[2]
            context.user_data['task_id'] = task_id
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Submit your Twitter proof link (e.g., https://twitter.com/...):", reply_markup)

        elif data == "daily_tasks":
            logger.info(f"Daily tasks requested by user {user_id}")
            today = datetime.utcnow().strftime("%Y-%m-%d")
            cursor.execute("SELECT id, description, mandatory, task_link FROM daily_tasks WHERE active = 1")
            tasks = cursor.fetchall()
            logger.info(f"Found {len(tasks)} active tasks")
            if not tasks:
                task_list = "No active tasks available at this time."
            else:
                task_list = "\n".join([f"ID: {task[0]} | {task[1]}{' (Mandatory)' if task[2] else ''}\nLink: {task[3]}" for task in tasks])
                if context.platform == "discord" and len(task_list) > 1900:
                    task_list = task_list[:1900] + "\n... (Truncated, see full list on Telegram)"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await context.send_message(chat_id, LANGUAGES[lang]["daily_tasks"].format(daily_tasks=task_list), reply_markup)
                logger.info(f"Sent daily tasks to user {user_id}")
            except Exception as e:
                logger.error(f"Failed to send daily tasks to {user_id}: {str(e)}")
                await context.send_message(chat_id, "Error retrieving tasks. Please try again later.", reply_markup)

        elif data == "refer":
            referral_code = generate_referral_code(user_id)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Your referral link: {referral_code}\nShare this with friends!", reply_markup)

        elif data == "claim_tokens":
            cursor.execute("SELECT amount, vesting_end FROM distributions WHERE user_id = ? AND status = 'claimable'", (user_id,))
            distribution = cursor.fetchone()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            if not distribution:
                await context.send_message(chat_id, "No claimable Momo Coins found.", reply_markup)
            else:
                amount, vesting_end = distribution
                if datetime.utcnow() < datetime.fromisoformat(vesting_end):
                    await context.send_message(chat_id, f"Momo Coins are locked until {vesting_end}.", reply_markup)
                else:
                    cursor.execute("UPDATE distributions SET status = 'claimed' WHERE user_id = ?", (user_id,))
                    update_user_balance(user_id, amount)
                    conn.commit()
                    await context.send_message(chat_id, f"Successfully claimed {amount} Momo Coins! Check balance.", reply_markup)

        elif data == "leaderboard":
            leaderboard_text = get_leaderboard(lang)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, leaderboard_text, reply_markup)

        elif data == "start_distribution" and is_admin(user_id):
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
                    await context.send_message(dist_user_id, LANGUAGES[lang]["sent_tokens"].format(amount=amount, wallet=wallet, tx_hash=tx_hash))
                except Exception as e:
                    logger.error(f"Failed to send {amount} to {wallet} on {chain}: {e}")
                    await context.send_message(dist_user_id, LANGUAGES[lang]["failed_tokens"].format(amount=amount, wallet=wallet, error=str(e)))
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Airdrop distribution started!", reply_markup)

        elif data == "export_data" and is_admin(user_id):
            wb = Workbook()
            ws = wb.active
            ws.append(["User ID", "Wallet", "Chain", "Amount", "Status", "Tx Hash", "Vesting End"])
            cursor.execute("SELECT user_id, wallet, chain, amount, status, tx_hash, vesting_end FROM distributions")
            for row in cursor.fetchall():
                ws.append(row)
            wb.save("airdrop_log.xlsx")
            await context.send_document(chat_id, open("airdrop_log.xlsx", "rb"))
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Data exported!", reply_markup)

        elif data == "blacklist" and is_admin(user_id):
            context.user_data['awaiting_blacklist'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter wallet to blacklist:", reply_markup)

        elif data == "whitelist" and is_admin(user_id):
            context.user_data['awaiting_whitelist'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter wallet to whitelist:", reply_markup)

        elif data == "set_config" and is_admin(user_id):
            context.user_data['awaiting_config'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter config key and value (e.g., total_supply 2000000):", reply_markup)

        elif data == "approve_tasks" and is_admin(user_id):
            page = context.user_data.get('approve_tasks_page', 1)
            items_per_page = 10
            offset = (page - 1) * items_per_page
            cursor.execute("SELECT COUNT(*) FROM task_completions WHERE status = 'pending'")
            total_tasks = cursor.fetchone()[0]
            total_pages = (total_tasks + items_per_page - 1) // items_per_page
            cursor.execute("SELECT user_id, task_id, username, completion_date FROM task_completions WHERE status = 'pending' LIMIT ? OFFSET ?", (items_per_page, offset))
            pending = cursor.fetchall()
            if not pending:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "No pending task submissions.", reply_markup)
            else:
                keyboard = []
                for task in pending:
                    user_id, task_id, username, date = task
                    keyboard.append([InlineKeyboardButton(f"Approve {user_id} - Task {task_id} ({username})",
                                                          callback_data=f"approve_task_{user_id}_{task_id}_{date}"),
                                     InlineKeyboardButton(f"Reject {user_id} - Task {task_id}",
                                                          callback_data=f"reject_task_{user_id}_{task_id}_{date}")])
                nav_buttons = []
                if page > 1:
                    nav_buttons.append(InlineKeyboardButton("Previous", callback_data=f"approve_tasks_page_{page-1}"))
                if page < total_pages:
                    nav_buttons.append(InlineKeyboardButton("Next", callback_data=f"approve_tasks_page_{page+1}"))
                if nav_buttons:
                    keyboard.append(nav_buttons)
                keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Pending task submissions (Page {page}/{total_pages}, {total_tasks} total):", reply_markup)

        elif data.startswith("approve_tasks_page_") and is_admin(user_id):
            page = int(data.split("_")[3])
            context.user_data['approve_tasks_page'] = page
            await self.button_handler(update, context)

        elif data.startswith("approve_task_") and is_admin(user_id):
            task_user_id, task_id, completion_date = data.split("_")[2:]
            cursor.execute("UPDATE task_completions SET status = 'approved' WHERE user_id = ? AND task_id = ? AND completion_date = ?",
                           (task_user_id, task_id, completion_date))
            update_user_balance(task_user_id, 10)
            conn.commit()
            cursor.execute("SELECT description FROM daily_tasks WHERE id = ?", (task_id,))
            task_description = cursor.fetchone()[0]
            await context.send_message(task_user_id, LANGUAGES[lang]["task_approved"].format(task_description=task_description))
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Task {task_id} for user {task_user_id} approved!", reply_markup)

        elif data.startswith("reject_task_") and is_admin(user_id):
            task_user_id, task_id, completion_date = data.split("_")[2:]
            cursor.execute("UPDATE task_completions SET status = 'rejected' WHERE user_id = ? AND task_id = ? AND completion_date = ?",
                           (task_user_id, task_id, completion_date))
            conn.commit()
            cursor.execute("SELECT description FROM daily_tasks WHERE id = ?", (task_id,))
            task_description = cursor.fetchone()[0]
            await context.send_message(task_user_id, LANGUAGES[lang]["task_rejected"].format(task_description=task_description))
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Task {task_id} for user {task_user_id} rejected!", reply_markup)

        elif data == "approve_kyc" and is_admin(user_id):
            cursor.execute("SELECT user_id, kyc_telegram_link, kyc_x_link, kyc_wallet, kyc_chain, kyc_submission_time FROM users WHERE kyc_status = 'submitted' LIMIT 10")
            pending = cursor.fetchall()
            if not pending:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "No pending KYC submissions.", reply_markup)
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
                await context.send_message(chat_id, "Pending KYC submissions:", reply_markup)

        elif data.startswith("approve_kyc_") and is_admin(user_id):
            kyc_user_id = data.split("_")[2]
            cursor.execute("UPDATE users SET kyc_status = 'verified' WHERE user_id = ?", (kyc_user_id,))
            conn.commit()
            await context.send_message(kyc_user_id, LANGUAGES[lang]["kyc_approved"])
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"KYC for user {kyc_user_id} approved!", reply_markup)

        elif data.startswith("reject_kyc_") and is_admin(user_id):
            kyc_user_id = data.split("_")[2]
            cursor.execute("UPDATE users SET kyc_status = 'rejected' WHERE user_id = ?", (kyc_user_id,))
            conn.commit()
            await context.send_message(kyc_user_id, LANGUAGES[lang]["kyc_rejected"])
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"KYC for user {kyc_user_id} rejected!", reply_markup)

        elif data == "approve_referrals" and is_admin(user_id):
            cursor.execute("SELECT referrer_id, referee_id, timestamp FROM referrals WHERE status = 'pending' LIMIT 10")
            pending = cursor.fetchall()
            if not pending:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "No pending referral submissions.", reply_markup)
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
                await context.send_message(chat_id, "Pending referral submissions:", reply_markup)

        elif data.startswith("approve_ref_") and is_admin(user_id):
            referrer_id, referee_id = data.split("_")[2], data.split("_")[3]
            cursor.execute("UPDATE referrals SET status = 'approved' WHERE referrer_id = ? AND referee_id = ?", (referrer_id, referee_id))
            update_user_balance(referrer_id, 15)
            conn.commit()
            cursor.execute("SELECT username FROM users WHERE user_id = ?", (referee_id,))
            referee_name = cursor.fetchone()[0] if cursor.fetchone() else "Unknown"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(referrer_id, LANGUAGES[lang]["referral_bonus"].format(bonus=15, referee=referee_name), reply_markup)
            await context.send_message(referee_id, LANGUAGES[lang]["referral_approved"].format(referee=referee_name))
            await context.send_message(chat_id, f"Referral from {referrer_id} to {referee_id} approved!", reply_markup)

        elif data.startswith("reject_ref_") and is_admin(user_id):
            referrer_id, referee_id = data.split("_")[2], data.split("_")[3]
            cursor.execute("UPDATE referrals SET status = 'rejected' WHERE referrer_id = ? AND referee_id = ?", (referrer_id, referee_id))
            conn.commit()
            cursor.execute("SELECT username FROM users WHERE user_id = ?", (referee_id,))
            referee_name = cursor.fetchone()[0] if cursor.fetchone() else "Unknown"
            await context.send_message(referee_id, LANGUAGES[lang]["referral_rejected"].format(referee=referee_name))
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Referral from {referrer_id} to {referee_id} rejected!", reply_markup)

        elif data == "set_campaign" and is_admin(user_id):
            context.user_data['awaiting_campaign'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter campaign details (name start_date end_date total_tokens, e.g., 'Summer 2025-03-01 2025-03-15 500000'):", reply_markup)

        elif data == "edit_campaign" and is_admin(user_id):
            cursor.execute("SELECT id, name FROM campaigns WHERE active = 1")
            campaigns = cursor.fetchall()
            if not campaigns:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "No active campaigns.", reply_markup)
            else:
                keyboard = [[InlineKeyboardButton(f"Edit {camp[1]} (ID: {camp[0]})", callback_data=f"edit_campaign_{camp[0]}")] for camp in campaigns]
                keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Select campaign to edit:", reply_markup)

        elif data.startswith("edit_campaign_") and is_admin(user_id):
            campaign_id = data.split("_")[2]
            context.user_data['awaiting_campaign_edit'] = campaign_id
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter new campaign details (name start_date end_date total_tokens, e.g., 'Summer 2025-03-01 2025-03-15 500000'):", reply_markup)

        elif data == "add_daily_task" and is_admin(user_id):
            cursor.execute("SELECT COUNT(*) FROM daily_tasks WHERE active = 1")
            active_task_count = cursor.fetchone()[0]
            if active_task_count >= 10:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Task limit reached (10 active tasks). Delete or edit an existing task first.", reply_markup)
            else:
                context.user_data['awaiting_task_add'] = True
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Enter task details (description link mandatory, e.g., 'Watch Video https://youtube.com/example 0'):", reply_markup)

        elif data == "edit_daily_task" and is_admin(user_id):
            logger.info(f"Edit daily task triggered by admin {user_id}, Chat ID: {chat_id}, Platform: {context.platform}")
            cursor.execute("SELECT id, description FROM daily_tasks WHERE active = 1")
            tasks = cursor.fetchall()
            logger.info(f"Tasks available for edit: {tasks}")
            if not tasks:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "No active tasks to edit.", reply_markup)
            else:
                keyboard = [[InlineKeyboardButton(f"Edit {task[1]} (ID: {task[0]})", callback_data=f"edit_task_{task[0]}")] for task in tasks]
                keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                try:
                    await context.send_message(chat_id, "Select task to edit:", reply_markup)
                    logger.info(f"Task list sent to admin {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send task list to {user_id}: {str(e)}")
                    await context.send_message(chat_id, "Error displaying tasks.", reply_markup)

        elif data.startswith("edit_task_") and is_admin(user_id):
            task_id = data.split("_")[2]
            logger.info(f"Admin {user_id} selected task {task_id} to edit")
            # Store state in database
            cursor.execute("REPLACE INTO admin_states (user_id, state, task_id, timestamp) VALUES (?, ?, ?, ?)",
                           (user_id, "awaiting_task_edit", task_id, datetime.utcnow().isoformat()))
            conn.commit()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                # Log chat_id and platform for debugging
                logger.info(f"Sending edit prompt to chat_id: {chat_id}, Platform: {context.platform}")
                # Test with a simple message first
                await context.send_message(chat_id, "Test prompt", reply_markup)
                # If the test succeeds, send the actual prompt
                await context.send_message(chat_id, LANGUAGES[lang]["edit_task_prompt"], reply_markup)
                logger.info(f"Edit prompt sent for task {task_id} to {user_id}")
            except Exception as e:
                logger.error(f"Failed to send edit prompt to {user_id} (chat_id: {chat_id}, platform: {context.platform}): {str(e)}")
                # Attempt to send the error message to the same chat_id
                try:
                    await context.send_message(chat_id, "Error prompting for edit. Please try again or check bot permissions.", reply_markup)
                except Exception as e2:
                    logger.error(f"Failed to send error message to {user_id} (chat_id: {chat_id}, platform: {context.platform}): {str(e2)}")

        elif data == "delete_daily_task" and is_admin(user_id):
            cursor.execute("SELECT id, description FROM daily_tasks WHERE active = 1")
            tasks = cursor.fetchall()
            if not tasks:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "No active tasks.", reply_markup)
            else:
                keyboard = [[InlineKeyboardButton(f"Delete {task[1]} (ID: {task[0]})", callback_data=f"delete_task_{task[0]}")] for task in tasks]
                keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="start")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Select task to delete:", reply_markup)

        elif data.startswith("delete_task_") and is_admin(user_id):
            task_id = data.split("_")[2]
            cursor.execute("UPDATE daily_tasks SET active = 0 WHERE id = ?", (task_id,))
            conn.commit()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Task {task_id} deleted!", reply_markup)

        elif data == "test_message" and is_admin(user_id):
            logger.info(f"Testing message to chat_id: {chat_id}, Platform: {context.platform}")
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await context.send_message(chat_id, "This is a test message.", reply_markup)
                logger.info(f"Test message sent to {user_id}")
            except Exception as e:
                logger.error(f"Failed to send test message to {user_id}: {str(e)}")
                await context.send_message(chat_id, "Error sending test message.", reply_markup)

        if context.platform == "telegram":
            await update.callback_query.answer()

    async def handle_message(self, update: Union[Update, discord.Message], context: BotContext):
        user_id = str(update.message.from_user.id if context.platform == "telegram" else update.author.id)
        lang = get_user_language(user_id)
        chat_id = str(update.message.chat_id if context.platform == "telegram" else update.channel.id)
        text = update.message.text.strip() if context.platform == "telegram" else update.content.strip()

        if context.user_data.get('kyc_step') == "telegram":
            if not is_valid_telegram_link(text):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["kyc_telegram_invalid"], reply_markup)
                return
            context.user_data['kyc_telegram_link'] = text
            context.user_data['kyc_step'] = "x_link"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["kyc_telegram"].format(telegram=text), reply_markup)
            return

        elif context.user_data.get('kyc_step') == "x_link":
            if not is_valid_x_link(text):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["kyc_x_link_invalid"], reply_markup)
                return
            context.user_data['kyc_x_link'] = text
            context.user_data['kyc_step'] = "wallet"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "X link received: {}. Now provide your wallet address (e.g., 'ETH 0x...' or 'XRP r...'):".format(text), reply_markup)
            return

        elif context.user_data.get('kyc_step') == "wallet":
            try:
                chain, wallet = text.split(maxsplit=1)
                chain = chain.upper()
                if chain not in ["ETH", "BSC", "SOL", "XRP"] or not is_valid_address(wallet, chain):
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, LANGUAGES[lang]["kyc_wallet_invalid"], reply_markup)
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
                    await context.send_message(ADMIN_ID, LANGUAGES[lang]["kyc_notification"].format(
                        user_id=user_id, telegram=context.user_data['kyc_telegram_link'], x_link=context.user_data['kyc_x_link'], wallet=wallet, chain=chain, time=submission_time))
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["kyc_complete"].format(
                    telegram=context.user_data['kyc_telegram_link'], x_link=context.user_data['kyc_x_link'], wallet=wallet, chain=chain), reply_markup)
                context.user_data.clear()
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["kyc_wallet_invalid"], reply_markup)
            return

        elif context.user_data.get('awaiting_wallet'):
            wallet = text
            chain = context.user_data.get('chain')
            if not is_valid_address(wallet, chain):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["invalid_address"].format(chain=chain), reply_markup)
                context.user_data['awaiting_wallet'] = False
                return
            cursor.execute("SELECT wallet FROM blacklist WHERE wallet = ?", (wallet,))
            if cursor.fetchone():
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["blacklisted"], reply_markup)
                context.user_data['awaiting_wallet'] = False
                return
            cursor.execute("SELECT wallet FROM submissions WHERE user_id = ?", (user_id,))
            if cursor.fetchone():
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["already_submitted"], reply_markup)
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
            await context.send_message(chat_id, LANGUAGES[lang]["captcha"].format(captcha=captcha), reply_markup)

        elif context.user_data.get('awaiting_captcha'):
            try:
                user_answer = int(text)
                cursor.execute("SELECT captcha FROM captchas WHERE user_id = ?", (user_id,))
                result = cursor.fetchone()
                if not result:
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, "No CAPTCHA found.", reply_markup)
                    return
                if user_answer == result[0] + 5:
                    await self.verify_wallet(user_id, chat_id, context, lang)
                else:
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, "Wrong answer. Try submitting wallet again.", reply_markup)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Please enter a number.", reply_markup)
            context.user_data['awaiting_captcha'] = False

        elif context.user_data.get('awaiting_task_add'):
            cursor.execute("SELECT COUNT(*) FROM daily_tasks WHERE active = 1")
            active_task_count = cursor.fetchone()[0]
            if active_task_count >= 10:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Task limit reached (10 active tasks). Delete or edit an existing task first.", reply_markup)
                context.user_data['awaiting_task_add'] = False
            else:
                try:
                    description, task_link, mandatory = text.split(maxsplit=2)
                    mandatory = int(mandatory)
                    cursor.execute("INSERT INTO daily_tasks (description, reward, mandatory, task_link) VALUES (?, 10, ?, ?)",
                                   (description, mandatory, task_link))
                    conn.commit()
                    context.user_data['awaiting_task_add'] = False
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, f"Added daily task: {description} with link {task_link}", reply_markup)
                    logger.info(f"Admin {user_id} added task: {description}, link: {task_link}, mandatory: {mandatory}")
                except ValueError:
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, "Format: description link mandatory (e.g., 'Watch Video https://youtube.com/example 0')", reply_markup)

        elif context.user_data.get('awaiting_blacklist'):
            wallet = text
            cursor.execute("INSERT OR IGNORE INTO blacklist (wallet) VALUES (?)", (wallet,))
            conn.commit()
            context.user_data['awaiting_blacklist'] = False
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"{wallet} blacklisted.", reply_markup)

        elif context.user_data.get('awaiting_whitelist'):
            wallet = text
            cursor.execute("INSERT OR IGNORE INTO whitelist (wallet) VALUES (?)", (wallet,))
            conn.commit()
            context.user_data['awaiting_whitelist'] = False
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"{wallet} whitelisted.", reply_markup)

        elif context.user_data.get('awaiting_config'):
            try:
                key, value = text.split()
                cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
                conn.commit()
                context.user_data['awaiting_config'] = False
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Set {key} = {value}", reply_markup)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Format: key value", reply_markup)

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
                await context.send_message(chat_id, LANGUAGES[lang]["campaign_set"].format(name=name, start=start_date, end=end_date, tokens=total_tokens), reply_markup)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Format: name start_date end_date total_tokens (e.g., 'Summer 2025-03-01 2025-03-15 500000')", reply_markup)

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
                await context.send_message(chat_id, LANGUAGES[lang]["campaign_edit"].format(name=name, start=start_date, end=end_date, tokens=total_tokens), reply_markup)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Format: name start_date end_date total_tokens (e.g., 'Summer 2025-03-01 2025-03-15 500000')", reply_markup)

        elif context.user_data.get('task_id'):
            task_id = context.user_data['task_id']
            username = text
            if task_id in ["1", "2"]:
                cursor.execute("UPDATE eligible SET social_tasks_completed = social_tasks_completed + 1 WHERE user_id = ?", (user_id,))
                update_user_balance(user_id, 10)
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["task_completed"].format(task_description=f"Task {task_id}"), reply_markup)
            else:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Invalid task ID.", reply_markup)
            context.user_data['task_id'] = None

        else:
            cursor.execute("SELECT state, task_id FROM admin_states WHERE user_id = ?", (user_id,))
            state_result = cursor.fetchone()
            if state_result and state_result[0] == "awaiting_task_edit":
                task_id = state_result[1]
                logger.info(f"Admin {user_id} submitted edit for task {task_id}: {text}")
                try:
                    parts = text.split(maxsplit=4)
                    if len(parts) != 5:
                        raise ValueError("Invalid format")
                    task_id_input, description, reward, mandatory, task_link = parts
                    if task_id_input != task_id:
                        raise ValueError("Task ID mismatch")
                    reward = float(reward)
                    mandatory = int(mandatory)
                    if mandatory not in [0, 1]:
                        raise ValueError("Mandatory must be 0 or 1")
                    cursor.execute("UPDATE daily_tasks SET description = ?, reward = ?, mandatory = ?, task_link = ? WHERE id = ?",
                                   (description, reward, mandatory, task_link, task_id))
                    conn.commit()
                    cursor.execute("DELETE FROM admin_states WHERE user_id = ?", (user_id,))
                    conn.commit()
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, LANGUAGES[lang]["task_edited"].format(
                        task_id=task_id, description=description, reward=reward, mandatory=mandatory, task_link=task_link), reply_markup)
                    logger.info(f"Task {task_id} edited by admin {user_id}: {description}, {reward}, {mandatory}, {task_link}")
                except ValueError as e:
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, f"Error: {str(e)}. Format: {task_id} description reward mandatory link (e.g., '{task_id} New Task 15 1 https://newlink.com')", reply_markup)
                except Exception as e:
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, f"Failed to edit task: {str(e)}", reply_markup)
                    logger.error(f"Task edit failed for task {task_id} by admin {user_id}: {str(e)}")
                return

            # Default message handling (if no specific state matches)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Unknown command. Use the menu options.", reply_markup)

            # Task submission handling (outside of admin states)
            try:
                parts = text.split(maxsplit=1)
                if len(parts) == 2 and parts[0].isdigit():
                    task_id = parts[0]
                    username = parts[1]
                    today = datetime.utcnow().strftime("%Y-%m-%d")
                    cursor.execute("SELECT id, description FROM daily_tasks WHERE id = ? AND active = 1", (task_id,))
                    task = cursor.fetchone()
                    if not task:
                        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await context.send_message(chat_id, "Task not found or inactive.", reply_markup)
                        return
                    cursor.execute("SELECT COUNT(*) FROM task_completions WHERE user_id = ? AND task_id = ? AND completion_date = ?",
                                   (user_id, task_id, today))
                    if cursor.fetchone()[0] > 0:
                        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await context.send_message(chat_id, "You’ve already submitted this task today.", reply_markup)
                        return
                    cursor.execute("INSERT INTO task_completions (user_id, task_id, completion_date, username) VALUES (?, ?, ?, ?)",
                                   (user_id, task_id, today, username))
                    conn.commit()
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, LANGUAGES[lang]["task_completed"].format(task_description=task[1]), reply_markup)
                    logger.info(f"User {user_id} submitted task {task_id}: {username}")
                    if ADMIN_ID:
                        await context.send_message(ADMIN_ID, f"New task submission:\nUser ID: {user_id}\nTask ID: {task_id}\nUsername: {username}\nTime: {today}")
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Format: task_id username (e.g., '1 @username')", reply_markup)
            except Exception as e:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Error processing task: {str(e)}", reply_markup)
                logger.error(f"Unexpected error in task submission for user {user_id}: {str(e)}")
            return

    async def verify_wallet(self, user_id, chat_id, context: BotContext, lang):
        cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        if result:
            wallet, chain = result
            tier, token_balance = await check_eligibility(wallet, chain)
            if tier > 0:
                cursor.execute("REPLACE INTO eligible (user_id, wallet, chain, tier, verified, token_balance, social_tasks_completed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (user_id, wallet, chain, tier, 1, token_balance, 0))
                conn.commit()
                await context.send_message(chat_id, LANGUAGES[lang]["verified"].format(tier=tier))
            else:
                await context.send_message(chat_id, LANGUAGES[lang]["no_assets"])
        else:
            await context.send_message(chat_id, "No wallet submission found.")

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

# Telegram Setup
async def setup_telegram(bot: AirdropBot):
    bot.telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    context = BotContext("telegram")
    context.bot = bot.telegram_app.bot

    bot.telegram_app.add_handler(CommandHandler("start", lambda u, c: bot.start(u, context)))
    bot.telegram_app.add_handler(CommandHandler("join_airdrop", lambda u, c: bot.join_airdrop(u, context)))
    bot.telegram_app.add_handler(CallbackQueryHandler(lambda u, c: bot.button_handler(u, context)))
    bot.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: bot.handle_message(u, context)))

    await bot.telegram_app.initialize()
    await bot.telegram_app.start()
    await bot.telegram_app.updater.start_polling()

# Discord Setup
discord_bot = discord_commands.Bot(command_prefix="!", intents=discord.Intents.all())

@discord_bot.event
async def on_ready():
    logger.info(f"Discord bot logged in as {discord_bot.user}")

@discord_bot.command(name="start")
async def discord_start(ctx: discord_commands.Context, *, args: Optional[str] = None):
    update = ctx
    update.content = f"!start {args}" if args else "!start"
    bot_context = BotContext("discord")
    bot_context.bot = discord_bot
    await airdrop_bot.start(update, bot_context)

@discord_bot.command(name="join_airdrop")
async def discord_join_airdrop(ctx: discord_commands.Context):
    update = ctx
    update.content = "!join_airdrop"
    bot_context = BotContext("discord")
    bot_context.bot = discord_bot
    await airdrop_bot.join_airdrop(update, bot_context)

@discord_bot.command(name="Birdz")
async def discord_birdz(ctx: discord_commands.Context, callback_data: str):
    update = ctx
    update.content = f"!Birdz {callback_data}"
    bot_context = BotContext("discord")
    bot_context.bot = discord_bot
    await airdrop_bot.button_handler(update, bot_context)

@discord_bot.event
async def on_message(message: discord.Message):
    if message.author == discord_bot.user:
        return
    bot_context = BotContext("discord")
    bot_context.bot = discord_bot
    await airdrop_bot.handle_message(message, bot_context)
    await discord_bot.process_commands(message)

# Main Execution
airdrop_bot = AirdropBot()

async def main():
    # Setup and start Telegram bot as a task
    telegram_task = asyncio.create_task(setup_telegram(airdrop_bot))
    
    # Start Discord bot in the main loop
    await discord_bot.start(DISCORD_TOKEN)

    # Wait for Telegram task to complete (it won't, unless there's an error)
    await telegram_task

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        # Ensure proper shutdown
        if airdrop_bot.telegram_app:
            asyncio.run(airdrop_bot.telegram_app.stop())
            asyncio.run(airdrop_bot.telegram_app.shutdown())
        if airdrop_bot.discord_bot:
            asyncio.run(discord_bot.close())
        conn.close()
