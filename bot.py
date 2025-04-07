print("--- SCRIPT STARTED ---")
import discord
from discord.ext import commands, tasks
import os
import asyncpg
import asyncio
import aiohttp 
import requests 
from bs4 import BeautifulSoup
import json
import time
from datetime import datetime
import logging
import io 
import re 
from dotenv import load_dotenv
load_dotenv()


BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']
RIOT_API_KEY = os.environ['RIOT_API_KEY']
DATABASE_URL = os.environ['DATABASE_URL']

# --- Constants ---
CHECK_INTERVAL_MINUTES = 2
RATE_LIMIT_DELAY = 1.2 # Seconds delay between Riot API calls per user check

# --- Global Variables ---
db_pool = None
champion_id_map = {}
champion_name_map = {}
latest_ddragon_version = None
# Add this dictionary:
last_notified_game = {} # Stores {discord_id: game_id}

# Region mapping 
REGION_ACCOUNT_MAP = {
    "NA": "americas", "NA1": "americas",
    "BR": "americas", "BR1": "americas",
    "LAN": "americas", "LA1": "americas",
    "LAS": "americas", "LA2": "americas",
    "EUW": "europe", "EUW1": "europe",
    "EUNE": "europe", "EUN1": "europe",
    "TR": "europe", "TR1": "europe",
    "RU": "europe",
    "KR": "asia",
    "JP": "asia", "JP1": "asia",
    "OCE": "sea", "OC1": "sea",
    "PH": "sea", "PH2": "sea",
    "SG": "sea", "SG2": "sea",
    "TH": "sea", "TH2": "sea",
    "TW": "sea", "TW2": "sea",
    "VN": "sea", "VN2": "sea",
    "PBE": "americas", "PBE1": "americas", # Example, adjust if needed
}

# Region mapping (User input -> Riot API platform value for SPECTATOR-V4 etc.)
REGION_PLATFORM_MAP = {
    "NA": "NA1", "NA1": "NA1",
    "BR": "BR1", "BR1": "BR1",
    "LAN": "LA1", "LA1": "LA1",
    "LAS": "LA2", "LA2": "LA2",
    "EUW": "EUW1", "EUW1": "EUW1",
    "EUNE": "EUN1", "EUN1": "EUN1",
    "TR": "TR1", "TR1": "TR1",
    "RU": "RU",
    "KR": "KR",
    "JP": "JP1", "JP1": "JP1",
    "OCE": "OC1", "OC1": "OC1",
    "PH": "PH2", "PH2": "PH2",
    "SG": "SG2", "SG2": "SG2",
    "TH": "TH2", "TH2": "TH2",
    "TW": "TW2", "TW2": "TW2",
    "VN": "VN2", "VN2": "VN2",
    "PBE": "PBE1", "PBE1": "PBE1",
}

# Valid game queue IDs 
VALID_QUEUE_IDS = {
    400,  # 5v5 Draft Pick games
    420,  # 5v5 Ranked Solo games
    430,  # 5v5 Blind Pick games
    440,  # 5v5 Ranked Flex games
    # Add ARAM (450) or others if desired
}

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

# --- Global Variables ---
db_pool = None
champion_id_map = {} # Stores {id: name}
champion_name_map = {} # Stores {name: id}
latest_ddragon_version = None

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True # Necessary for reading message content
intents.members = True # Might need for fetching user objects
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Database Setup & Functions ---
async def setup_database():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        logger.info("Database connection pool established.")
        async with db_pool.acquire() as connection:
            # Create users table
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    discord_id BIGINT PRIMARY KEY,
                    riot_game_name TEXT NOT NULL,
                    riot_tag_line TEXT NOT NULL,
                    region TEXT NOT NULL,
                    puuid TEXT UNIQUE NOT NULL,
                    last_registered_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Create champion_data table
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS champion_data (
                    champion_name TEXT PRIMARY KEY,
                    base_stats TEXT,
                    abilities TEXT,
                    last_updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            logger.info("Database tables checked/created.")
    except Exception as e:
        logger.error(f"Error setting up database: {e}", exc_info=True)
        db_pool = None # Ensure pool is None if setup failed

async def register_user_db(discord_id, game_name, tag_line, region, puuid):
    if not db_pool:
        logger.error("Database pool not available for registration.")
        return False
    try:
        async with db_pool.acquire() as connection:
            # UPSERT logic: Insert or update if discord_id already exists
            await connection.execute("""
                INSERT INTO users (discord_id, riot_game_name, riot_tag_line, region, puuid, last_registered_at)
                VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                ON CONFLICT (discord_id) DO UPDATE SET
                    riot_game_name = EXCLUDED.riot_game_name,
                    riot_tag_line = EXCLUDED.riot_tag_line,
                    region = EXCLUDED.region,
                    puuid = EXCLUDED.puuid,
                    last_registered_at = CURRENT_TIMESTAMP;
            """, discord_id, game_name, tag_line, region, puuid)
            # Also update PUUID if it changed for the same discord ID 
            # updates if re-registering.
            logger.info(f"User {discord_id} registered/updated with PUUID {puuid}.")
            return True
    except asyncpg.exceptions.UniqueViolationError as e:
         logger.warning(f"Unique constraint violation during registration for {discord_id} / {puuid}: {e}")
         #  PUUID is already linked to another Discord ID.
         return False 
    except Exception as e:
        logger.error(f"Error registering user {discord_id} in DB: {e}", exc_info=True)
        return False

async def get_registered_users():
    if not db_pool: return []
    try:
        async with db_pool.acquire() as connection:
            users = await connection.fetch("SELECT discord_id, puuid, region FROM users")
            return users
    except Exception as e:
        logger.error(f"Error fetching registered users: {e}", exc_info=True)
        return []

async def get_champion_data_db(champion_name):
    if not db_pool: return None
    try:
        async with db_pool.acquire() as connection:
            data = await connection.fetchrow(
                "SELECT base_stats, abilities FROM champion_data WHERE champion_name = $1",
                champion_name
            )
            return data
    except Exception as e:
        logger.error(f"Error fetching champion data for {champion_name}: {e}", exc_info=True)
        return None

async def cache_champion_data_db(champion_name, base_stats, abilities):
    if not db_pool: return False
    try:
        async with db_pool.acquire() as connection:
            await connection.execute("""
                INSERT INTO champion_data (champion_name, base_stats, abilities, last_updated_at)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                ON CONFLICT (champion_name) DO UPDATE SET
                    base_stats = EXCLUDED.base_stats,
                    abilities = EXCLUDED.abilities,
                    last_updated_at = CURRENT_TIMESTAMP;
            """, champion_name, base_stats, abilities)
            logger.info(f"Cached data for champion: {champion_name}")
            return True
    except Exception as e:
        logger.error(f"Error caching champion data for {champion_name}: {e}", exc_info=True)
        return False

# --- Riot API Functions ---
async def get_riot_account_puuid(session, game_name, tag_line, region_route):
    url = f"https://{region_route}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        async with session.get(url, headers=headers) as response:
            logger.debug(f"Riot Account API request to {url} - Status: {response.status}")
            if response.status == 200:
                data = await response.json()
                return data.get('puuid')
            elif response.status == 404:
                logger.warning(f"Riot ID {game_name}#{tag_line} not found in region route {region_route}.")
                return None
            else:
                logger.error(f"Error fetching PUUID: {response.status} - {await response.text()}")
                return None
    except Exception as e:
        logger.error(f"Exception during PUUID fetch for {game_name}#{tag_line}: {e}", exc_info=True)
        return None

async def get_live_game_data(session, puuid, platform_id):
    url = f"https://{platform_id}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        await asyncio.sleep(RATE_LIMIT_DELAY) 
        async with session.get(url, headers=headers) as response:
            logger.debug(f"Spectator API request for {puuid} on {platform_id} - Status: {response.status}")
            if response.status == 200:
                return await response.json()
            elif response.status == 404:
                # User is not in a game
                return None
            else:
                logger.error(f"Error fetching live game for PUUID {puuid}: {response.status} - {await response.text()}")
                return None
    except Exception as e:
        logger.error(f"Exception during live game fetch for PUUID {puuid}: {e}", exc_info=True)
        return None

# --- Data Dragon & Champion Mapping ---
def fetch_ddragon_version():
    """Fetches the latest Data Dragon version."""
    try:
        response = requests.get("https://ddragon.leagueoflegends.com/api/versions.json")
        response.raise_for_status()
        return response.json()[0] # Get the latest version
    except Exception as e:
        logger.error(f"Failed to fetch DDragon versions: {e}", exc_info=True)
        return None

def fetch_champion_data_ddragon(version):
    """Fetches champion ID to name mapping from Data Dragon."""
    global champion_id_map, champion_name_map
    if not version:
        logger.error("Cannot fetch champion data without DDragon version.")
        return False
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()['data']
        temp_id_map = {}
        temp_name_map = {}
        for key, champ_data in data.items():
            champ_id = int(champ_data['key'])
            champ_name = champ_data['name']
            temp_id_map[champ_id] = champ_name
            temp_name_map[champ_name.lower()] = champ_id # Store lower for easier lookup
        champion_id_map = temp_id_map
        champion_name_map = temp_name_map
        logger.info(f"Successfully loaded {len(champion_id_map)} champions from DDragon version {version}.")
        return True
    except Exception as e:
        logger.error(f"Failed to fetch or parse DDragon champion data: {e}", exc_info=True)
        return False

def get_champion_name(champion_id):
    return champion_id_map.get(champion_id, f"Unknown Champion ID: {champion_id}")

# --- Web Scraping Function ---
async def scrape_champion_data_wiki(champion_name):
   
    # Format name for URL (underscores for spaces)
    formatted_name_url = champion_name.replace(" ", "_")
   

    
    if champion_name == "Nunu & Willump":
        formatted_name_url = "Nunu_%26_Willump" 
        

    
    url = f"https://wiki.leagueoflegends.com/en-us/{formatted_name_url}"
    logger.info(f"Scraping Wiki URL: {url}")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}

    try:
        # Create a session specifically for this scrape
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch wiki page for {champion_name}. Status: {response.status} URL: {url}")
                    return None, None

                html = await response.text()
                soup = BeautifulSoup(html, 'lxml')

                # --- Scrape Base Stats ---
                stats_text = f"Base Stats for {champion_name}:\n"
                stats_map = {}

                
                stat_ids_to_find = {
                    'Health': f'Health_{champion_name}_lvl', # Assumes champion_name matches ID name part
                    'Mana': f'ResourceBar_{champion_name}_lvl', # Handles Mana/Energy etc.
                    'Health regen.': f'HealthRegen_{champion_name}_lvl',
                    'Mana regen.': f'ResourceRegen_{champion_name}_lvl',
                    'Armor': f'Armor_{champion_name}_lvl',
                    'Attack damage': f'AttackDamage_{champion_name}_lvl',
                    'Magic resist.': f'MagicResist_{champion_name}_lvl', # Thresh had _lvl, check others
                    'Move. speed': f'MovementSpeed_{champion_name}', # No _lvl suffix in screenshot
                    'Attack range': f'AttackRange_{champion_name}'  # No _lvl suffix in screenshot
                }

                # Fallback for MR/MS/Range if _lvl version exists for other champs
                stat_ids_to_find_fallback_lvl = {
                    'Magic resist.': f'MagicResist_{champion_name}_lvl',
                    'Move. speed': f'MovementSpeed_{champion_name}_lvl',
                    'Attack range': f'AttackRange_{champion_name}_lvl'
                }


                for label, stat_id in stat_ids_to_find.items():
                    stat_span = soup.find('span', id=stat_id)
                    # If not found, try fallback for those without _lvl in initial list
                    if not stat_span and label in stat_ids_to_find_fallback_lvl:
                         stat_span = soup.find('span', id=stat_ids_to_find_fallback_lvl[label])

                    if stat_span:
                        # Clean the text - remove potential scaling info visually hidden, etc.
                        value = stat_span.get_text(strip=True)
                        # Simple cleaning: take value before potential parenthesis if any (basic stats usually don't have complex text)
                        value = value.split('(')[0].strip()
                        stats_map[label] = value
                    else:
                        logger.warning(f"Could not find stat span with ID {stat_id} (or fallback) for {champion_name}")
                        stats_map[label] = "N/A" # Mark as not found


                # Format the output string
                stats_text += f"  Health: {stats_map.get('Health', 'N/A')}\n"
                stats_text += f"  Mana/Energy/Other: {stats_map.get('Mana', 'N/A')}\n" # Use the generic ResourceBar ID result
                stats_text += f"  Health Regen (per 5s): {stats_map.get('Health regen.', 'N/A')}\n"
                stats_text += f"  Mana/Etc Regen (per 5s): {stats_map.get('Mana regen.', 'N/A')}\n"
                stats_text += f"  Armor: {stats_map.get('Armor', 'N/A')}\n"
                stats_text += f"  Attack Damage: {stats_map.get('Attack damage', 'N/A')}\n"
                stats_text += f"  Magic Resist: {stats_map.get('Magic resist.', 'N/A')}\n"
                stats_text += f"  Movement Speed: {stats_map.get('Move. speed', 'N/A')}\n"
                stats_text += f"  Attack Range: {stats_map.get('Attack range', 'N/A')}\n"

                # --- Scrape Abilities ---
                abilities_text = f"Abilities for {champion_name}:\n"
                # Find all ability sections (Passive, Q, W, E, R)
                # Each seems to be in a div with class starting 'skill skill_' or 'skill skill_innate'
                skill_divs = soup.find_all('div', class_=lambda c: c and c.startswith('skill skill_'))

                skill_map = {'innate':'P', 'q':'Q', 'w':'W', 'e':'E', 'r':'R'}

                for skill_div in skill_divs:
                    skill_key_class = next((c for c in skill_div['class'] if c.startswith('skill_')), None)
                    if not skill_key_class: continue # Skip if class not found
                    skill_key_letter = skill_key_class.split('_')[-1] # Get 'innate', 'q', 'w', etc.
                    ability_letter = skill_map.get(skill_key_letter, '?') # Get P, Q, W, E, R

                    ability_name_tag = skill_div.find('div', class_='ability-info-stats__ability')
                    ability_name = ability_name_tag.get_text(strip=True) if ability_name_tag else "Unknown Name"

                    cost = "N/A"
                    cooldown = "N/A"

                    # Find the stat pairs (Cost, Cooldown, Range, etc.)
                    stat_pairs = skill_div.find_all('div', class_='ability-info-stats__stat')
                    for pair in stat_pairs:
                        label_tag = pair.find('div', class_='ability-info-stats__stat-label')
                        value_tag = pair.find('div', class_='ability-info-stats__stat-value')

                        if label_tag and value_tag:
                            label_text = label_tag.get_text(strip=True).upper() # Normalize label
                            # Use separator for multi-value stats like cooldown/cost
                            value_text = value_tag.get_text(separator='/', strip=True)
                            
                            value_text = re.sub(r'\s?Mana', '', value_text, flags=re.IGNORECASE)
                            value_text = re.sub(r'\s?Energy', '', value_text, flags=re.IGNORECASE)
                            # value_text = value_text.replace('s', '').strip() # Careful, could remove 's' from names if any

                            if "COST" in label_text:
                                cost = value_text if value_text else "None" # Handle empty cost values
                            elif "COOLDOWN" in label_text:
                                cooldown = value_text if value_text else "N/A"

                    abilities_text += f"  {ability_letter} - {ability_name}: Cost {cost} / Cooldown {cooldown}\n"

                if not skill_divs:
                     abilities_text += "  Could not find ability divs (selectors need update).\n"

                logger.info(f"Finished scraping data for {champion_name}.")
                return stats_text, abilities_text

    except aiohttp.ClientError as e:
        logger.error(f"Network error during wiki scrape for {champion_name}: {e}", exc_info=True)
        return None, None
    except Exception as e:
        logger.error(f"General exception during wiki scrape for {champion_name}: {e}", exc_info=True)
        return None, None

# --- Bot Commands ---
@bot.command(name='register')
async def register(ctx, riot_id: str, region: str):
    """Registers or updates your Riot ID and region. Usage: !register GameName#TagLine REGION"""
    await ctx.typing() # Show typing indicator

    if '#' not in riot_id:
        await ctx.send("Invalid Riot ID format. Please use `GameName#TagLine`.")
        return

    game_name, tag_line = riot_id.split('#', 1)
    upper_region = region.upper()

    if upper_region not in REGION_ACCOUNT_MAP:
        await ctx.send(f"Invalid region: `{region}`. Valid regions: {', '.join(REGION_ACCOUNT_MAP.keys())}")
        return

    region_route = REGION_ACCOUNT_MAP[upper_region]
    platform_id = REGION_PLATFORM_MAP.get(upper_region) # For spectator check later
    if not platform_id:
         await ctx.send(f"Region `{upper_region}` is supported for account lookup but not for game lookup yet.")
         # Decide if registration should proceed - maybe allow it but warn?
         # For now, we require a valid platform ID as well.
         return

    async with aiohttp.ClientSession() as session:
        puuid = await get_riot_account_puuid(session, game_name, tag_line, region_route)

    if puuid:
        success = await register_user_db(ctx.author.id, game_name, tag_line, upper_region, puuid)
        if success:
            await ctx.send(f"✅ Successfully registered `{game_name}#{tag_line}` for region `{upper_region}`.")
            logger.info(f"User {ctx.author.name} ({ctx.author.id}) registered successfully.")
        else:
            # Check if it was a unique constraint violation (PUUID already registered to someone else)
            # This requires more sophisticated DB checking or letting the UPSERT fail gracefully.
            # For now, assume generic DB error or potential PUUID conflict.
             await ctx.send("⚠️ Registration failed. There might be a database issue, or this Riot Account might already be registered by another user.")
    else:
        await ctx.send(f"❌ Could not find Riot Account `{game_name}#{tag_line}` in the region associated with `{upper_region}` ({region_route}). Please double-check.")

# --- Background Task: Check Live Games ---
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_live_games():
    if not db_pool:
        logger.warning("Database pool not ready, skipping live game check.")
        return
    if not champion_id_map:
        logger.warning("Champion map not ready, skipping live game check.")
        return

    logger.info("Starting periodic live game check...")
    registered_users = await get_registered_users()
    if not registered_users:
        logger.info("No registered users found.")
        return

    async with aiohttp.ClientSession() as session:
        for user_record in registered_users:
            discord_id = user_record['discord_id']
            puuid = user_record['puuid']
            region = user_record['region']
            platform_id = REGION_PLATFORM_MAP.get(region)

            if not platform_id:
                logger.warning(f"User {discord_id} has region {region} with no valid platform ID mapping. Skipping.")
                continue

            logger.debug(f"Checking live game for user {discord_id} (PUUID: {puuid}, Region: {platform_id})")
            game_data = await get_live_game_data(session, puuid, platform_id)

            if game_data:
                game_queue_id = game_data.get('gameQueueConfigId')
                if game_queue_id in VALID_QUEUE_IDS:
                    logger.info(f"User {discord_id} is in a valid live game (Queue ID: {game_queue_id})!")

                    # --- Add logic to prevent re-notification ---
                    current_game_id = game_data.get('gameId')
                    previously_notified_game_id = last_notified_game.get(discord_id)

                    if current_game_id != previously_notified_game_id:
                        logger.info(f"New game detected ({current_game_id}) for user {discord_id}. Notifying.")


                    # Identify all champions in the match
                        participants = game_data.get('participants', [])
                        champion_ids_in_game = {p['championId'] for p in participants} # Use a set to avoid duplicates

                    # Trigger data processing and notification
                        asyncio.create_task(process_and_notify(discord_id, champion_ids_in_game, current_game_id)) # Pass game_id

                    # Remember this game ID for this user
                        last_notified_game[discord_id] = current_game_id

                    else:
                        logger.info(f"User {discord_id} still in game {current_game_id}. Notification already sent.")

                else:
                    logger.info(f"User {discord_id} is in a game (Queue ID: {game_queue_id}), but it's not a tracked type (e.g., custom game). Skipping.")
            # else: No game found or error (already logged in get_live_game_data)
            elif discord_id in last_notified_game:
         # User is no longer in the game we knew about (or API failed)
                logger.info(f"User {discord_id} no longer detected in a game or check failed. Clearing last notified game ID.")
                del last_notified_game[discord_id]
    logger.info("Finished periodic live game check.")


async def process_and_notify(discord_id, champion_ids, game_id):
    """
    Fetches/scrapes data for champions sequentially with delays,
    caches, formats, and sends DM.
    """
    logger.info(f"Processing game data for user {discord_id}. Champions: {champion_ids} Game ID: {game_id}")
    output_lines = []
    scrape_delay = 1.5 # Seconds delay between scraping attempts (adjust if needed)

    logger.info(f"Starting sequential scraping for {len(champion_ids)} champions with a {scrape_delay}s delay...")

    
    for champ_id in champion_ids:
        champion_name = get_champion_name(champ_id)
        if "Unknown" in champion_name:
             logger.warning(f"Champion ID {champ_id} mapping not found.")
             output_lines.append(f"Champion ID: {champ_id} - Name not found in mapping.")
             # Optional: Add a small delay even if skipping?
             # await asyncio.sleep(0.2)
             continue # Skip to the next champion in the loop

        # 1. Check Cache (DB)
        cached_data = await get_champion_data_db(champion_name)
        if cached_data:
            logger.debug(f"Cache hit for {champion_name}.")
            formatted_data = f"Champion: {champion_name}\n{cached_data['base_stats']}\n{cached_data['abilities']}\n"
            output_lines.append(formatted_data)
            
            continue # Skip to the next champion in the loop

        # --- If not cached, proceed to scrape ---
        logger.info(f"Cache miss for {champion_name}. Scraping Wiki...")
        
        base_stats_str, abilities_str = await scrape_champion_data_wiki(champion_name)

        if base_stats_str is not None and abilities_str is not None: # Check for None specifically
            # 3. Cache Scraped Data
            logger.info(f"Successfully scraped data for {champion_name}. Caching...")
            await cache_champion_data_db(champion_name, base_stats_str, abilities_str)
            formatted_data = f"Champion: {champion_name}\n{base_stats_str}\n{abilities_str}\n"
            output_lines.append(formatted_data)
        else:
            # Logged already in scrape_champion_data_wiki if it failed
            logger.warning(f"Failed to scrape data for {champion_name}.")
            # Updated failure message
            output_lines.append(f"Champion: {champion_name}\n  Could not retrieve data (Scraping failed or requires update).\n")

        # --- Add Delay AFTER each scraping attempt (success or fail) ---
        logger.debug(f"Waiting {scrape_delay}s before next scrape...")
        await asyncio.sleep(scrape_delay)
        # --- End Delay ---

    # --- Scraping loop finished ---
    logger.info(f"Finished sequential scraping for game {game_id}.")

    # Compile into a single string
    final_output = "------------------------------------\n".join(output_lines)

    # Prepare the text file
    file_content = final_output.encode('utf-8')
    file_to_send = discord.File(io.BytesIO(file_content), filename="champion_data.txt")

    # Send DM to the user
    try:
        user = await bot.fetch_user(discord_id) # Fetch user object
        if user:
            await user.send("🎮 You're in a live League of Legends game! Here's the champion data for your match:", file=file_to_send)
            logger.info(f"Sent champion data file to user {discord_id} for game {game_id}.")
        else:
            logger.warning(f"Could not find user with ID {discord_id} to send DM.")
    except discord.errors.Forbidden:
        logger.warning(f"Cannot send DM to user {discord_id}. They might have DMs disabled or blocked the bot.")
    except Exception as e:
        logger.error(f"Error sending DM to user {discord_id}: {e}", exc_info=True)

# --- Bot Events ---
@bot.event
async def on_ready():
    global latest_ddragon_version
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info('------')

    # Setup Database
    await setup_database()
    if not db_pool:
        logger.error("CRITICAL: Database connection failed. Bot features requiring DB will not work.")
        

    # Fetch Data Dragon data
    logger.info("Fetching initial Data Dragon info...")
    latest_ddragon_version = fetch_ddragon_version()
    if latest_ddragon_version:
        fetch_success = fetch_champion_data_ddragon(latest_ddragon_version)
        if not fetch_success:
             logger.error("CRITICAL: Failed to load champion data. Champion name lookups will fail.")
             # Bot can continue but functionality is impaired
    else:
        logger.error("CRITICAL: Failed to get DDragon version. Champion data cannot be loaded.")

    # Start background task only if DB and essential data are ready
    if db_pool and champion_id_map:
        check_live_games.start()
        logger.info(f"Live game check task started. Interval: {CHECK_INTERVAL_MINUTES} minutes.")
    else:
        logger.warning("Live game check task NOT started due to initialization issues.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❌ Unknown command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `!help {ctx.command.name}` for usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Bad argument provided. Use `!help {ctx.command.name}` for usage.")
    else:
        logger.error(f"Unhandled command error in '{ctx.command}': {error}", exc_info=True)
        await ctx.send("😕 An unexpected error occurred while running that command.")


# --- Run the Bot ---
if __name__ == "__main__":
    if not BOT_TOKEN or not RIOT_API_KEY or not DATABASE_URL:
        logger.error("CRITICAL ERROR: Bot Token, Riot API Key, or Database URL environment variable not set.")
    else:
        try:
            bot.run(BOT_TOKEN)
        except discord.LoginFailure:
            logger.error("CRITICAL ERROR: Invalid Discord Bot Token.")
        except Exception as e:
            logger.error(f"CRITICAL ERROR: Failed to start bot - {e}", exc_info=True)