import discord
import logging
import os
import detect_links
import asyncio
import asyncpg
import yt_dlp
import asyncio
import requests

from pathlib import Path
from dotenv import load_dotenv
from discord.ext import commands
from zoneinfo import ZoneInfo  # built-in, no extra install needed

# Download directory from .env
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads/"))

# Logging, text output to discord.log file
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# Load .env
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_API_TOKEN")
JELLYFIN_API_TOKEN = os.getenv("JELLYFIN_API_TOKEN")
DB_HOST = os.getenv("DB_HOST")
DB_URL = os.getenv("DB_URL")
JELLYFIN_URL = os.getenv("JELLYFIN_URL")
JELLYFIN_LIBRARY_ID = os.getenv("JELLYFIN_LIBRARY_ID")  # Skate-Movies

# Parse channel ID's
TARGET_CHANNEL_IDS = [int(x) for x in os.environ.get("TARGET_CHANNEL_IDS", "").split(",") if x]
print("Channel ID[0]: "+f"{TARGET_CHANNEL_IDS[0]}")
print("Channel ID[1]: "+f"{TARGET_CHANNEL_IDS[1]}")

# Discord intents, sorta the permissions of the bot.
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)
client = discord.Client(intents=intents)

# Global variables
original_message = ''
review_message = ''
final_title = ''
# yt-dlp options
ydl_opts = {
    "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
    'format': 'bestvideo+bestaudio/best', # Download best video and audio, then merge
    "playlist_items": "1",
    "noplaylist": True,          # <- Prevent playlist downloads
    "quiet": False,
    "no_warnings": False,
}

# Bot is ready to go!
@bot.event
async def on_ready():
    bot.db = await init_db()
    print(f'We have logged in as {bot.user}')
    print(f"Bot ready and connected to Postgres at {DB_HOST}")
    bot.loop.create_task(download_approved_videos())


# On event in the channel, in this case messages.
@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        return

    # Only monitor the source channel
    if message.channel.id == TARGET_CHANNEL_IDS[0]:
        # Detect YouTube links
        matches = detect_links.Regex.search_for_youtube_link(message.content)
        if matches:
            for url in matches:
                global original_message
                original_message = message
                await store_link(bot.db, url)


    # Make sure commands still work
    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return  # ignore bot reactions

    message = reaction.message

    # Deletion of video Logic
    for entry in bot.delete_messages:
        if entry["message"].id == message.id:
            await perform_video_deletion(message.channel, entry["title"])

            for e in bot.delete_messages:
                await e["message"].delete()
            
            bot.delete_messages.clear()
            break

    # if hasattr(bot, "delete_messages") and message.id in bot.delete_messages:
    #     if reaction.emoji == "🖕🏻":
    #         data = bot.delete_messages.pop(message.id)
    #         title = data["title"]
    #         await perform_video_deletion(message.channel, title)

    #         # Delete ALL list messages
    #         for entry in bot.delete_messages.values():
    #             await entry["message"].delete()
            
    #         bot.delete_messages.clear()

    # Approval of video Logic
    if reaction.message.channel.id == TARGET_CHANNEL_IDS[1]:
        url_text = reaction.message.content.split()[-1]  # crude way to extract URL
        # Check current status
        current_status = await bot.db.fetchval(
            "SELECT status FROM youtube_links WHERE url=$1", url_text
        )
        if current_status != "pending_approval":
            print(f"Reaction ignored; link already {current_status}: {url_text}")
            return

        # Update based on reaction
        if reaction.emoji == "✅":
            await bot.db.execute(
                "UPDATE youtube_links SET status='approved' WHERE url=$1", url_text
            )
            print(f"Link approved: {url_text}")
        elif reaction.emoji == "❌":
            await bot.db.execute(
                "UPDATE youtube_links SET status='rejected' WHERE url=$1", url_text
            )
            print(f"Link rejected: {url_text}")


# Initialize postgres database
async def init_db():
    db = await asyncpg.connect(DB_URL)

    # Create table if it doesn't exist
    await db.execute("""
        CREATE TABLE IF NOT EXISTS youtube_links (
            id SERIAL PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            title TEXT UNIQUE,
            added_at TIMESTAMP DEFAULT NOW()
        )
    """)
    print("Database initialized and table ready.")
    return db

# Store link into database
async def store_link(db, url):
    try:
        existing = await bot.db.fetchrow(
            "SELECT status FROM youtube_links WHERE url = $1",
            url
        )

        if existing:
            print(f"Link already exists ({existing['status']}), ignoring: {url}")
            return
        
        # Insert into DB
        await db.execute(
            "INSERT INTO youtube_links(url, status) VALUES($1, 'pending_approval') ON CONFLICT(url) DO NOTHING",
            url
        )
        print(f"Stored link in DB: {url}")

        # Send to review channel
        await send_to_review_channel(url)
        

    except Exception as e:
        print("Error storing link:", e)

async def download_approved_videos():
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Fetch approved links
        approved_links = await bot.db.fetch(
            "SELECT url FROM youtube_links WHERE status='approved'"
        )

        for record in approved_links:
            url = record["url"]
            print(f"Downloading approved video: {url}")

            if "playlist?list=" in url or "&list=" in url:
                print("Playlist URL detected, downloading only first video")
                ydl_opts["playlist_items"] = "1"

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                    info = ydl.extract_info(url, download=True)
                    global video_title
                    video_title = ydl.prepare_filename(info)
                
                # Update DB
                await bot.db.execute(
                    "UPDATE youtube_links SET status='downloaded' WHERE url=$1", url
                )
                await bot.db.execute(
                    "UPDATE youtube_links SET title=$2 WHERE url=$1", url, Path(video_title).name
                )
                print(f"Download complete: {url}")
                await trigger_jellyfin_scan()
                await delete_downloaded_link_channel_messages(original_message, review_message)

            except Exception as e:
                print(f"Error downloading {url}: {e}")

        # Wait before checking again
        await asyncio.sleep(60)  # check every minute

async def delete_downloaded_link_channel_messages(message1, message2):
    try:
        await message1.delete() # delete link message
        await message2.delete()
        print("Messages deleted!")
    except:
        print("Error deleting channel messages...")
    for id in TARGET_CHANNEL_IDS:
        channel = bot.get_channel(id)
        await channel.send(f"{Path(video_title).name} downloaded and library scan triggered!")

async def send_to_review_channel(url: str):
    # Send to review channel
        channel = bot.get_channel(TARGET_CHANNEL_IDS[1])
        if channel:
            msg = await channel.send(f"New YouTube link pending approval: {url}")
            global review_message
            review_message = msg
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")

async def trigger_jellyfin_scan():
    url = f"{JELLYFIN_URL}/Library/Refresh?/LibraryId={JELLYFIN_LIBRARY_ID}"
    headers = {"X-Emby-Token": JELLYFIN_API_TOKEN}
    requests.post(url, headers=headers)
    print("Jellyfin rescan complete!")

def delete_video_from_server(file_path):
    print("file_path: "+file_path)
    try:
        print("attemping deletion of file...")
        os.remove(file_path)                                # Delete file from os.
        print(f"File '{file_path}' has been deleted successfully.")
    except FileNotFoundError:
        print(f"Error: The file '{file_path}' does not exist.")
        raise FileNotFoundError(f"Error: The file '{file_path}' does not exist.")
    except PermissionError:
        print(f"Error: Permission denied to delete the file '{file_path}'. Ensure the file is not open and you have the necessary permissions.")
        raise PermissionError(f"Error: Permission denied to delete the file '{file_path}'. Ensure the file is not open and you have the necessary permissions.")
    except Exception as e:
        print(f"An unexpected error occurred in os.remove: {e}")
        raise Exception(f"An unexpected error occurred in os.remove: {e}")

async def list_downloaded_videos_for_deletion(ctx):
    if not hasattr(bot, "delete_messages"):
        bot.delete_messages = []

    downloaded = await bot.db.fetch(
        "SELECT title FROM youtube_links WHERE status='downloaded'")
    for video in downloaded:
        msg = await ctx.channel.send(video['title'])
        await msg.add_reaction("🖕🏻")
        bot.delete_messages.append({
            "title": video['title'],
            "message": msg
        })
    
async def perform_video_deletion(channel, title: str):
    file_path = str(DOWNLOAD_DIR / title)

    print("<<<ATTEMPTING DELETION>>>")
    try:
        delete_video_from_server(file_path)

        await bot.db.execute(
            "DELETE FROM youtube_links WHERE title=$1",
            title
        )

        await trigger_jellyfin_scan()

        await channel.send(
            f"Video `{title}` deleted and Jellyfin rescan triggered!"
        )
    except FileNotFoundError:
        print("File not found")
        await channel.send(f"The file {title} does not exist...")
    except Exception as e:
        print(f"An unexpected error occurred in perform_video_deletion: {e}")
    

## BOT COMMANDS ##


# @bot.command()
# @commands.has_permissions(manage_messages=True)
# async def test(ctx):
#     try:
#         print("ARG is empty, giving list...")
#         messages = await list_downloaded_videos_for_deletion(ctx)
#         for msg in messages:
#             print("==================================================================")
#             print("MESSAGE ID IN TEST: ")
#             print(msg)
#             print("==================================================================")
#             await asyncio.sleep(0.5)

#     except Exception as e:
#         print(f"An unexpected error occurred in test: {e}")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def delete_video(ctx, arg: str = None):
    try:
        if(arg == None):
            print("ARG is empty, giving list...")
            await list_downloaded_videos_for_deletion(ctx)

        elif(arg.startswith("https://")):
            print("arg is a link, finding title...")
            title = await bot.db.fetchrow(                                 # Delete from db.
            "SELECT title FROM youtube_links WHERE url=$1", arg)
            print("THIS IS THE TITLE FROM URL: ")
            print(title[0])
            title = title[0]
            await perform_video_deletion(ctx.channel, title)

        else:
            print("THIS IS THE TITLE FROM ARG: "+arg)
            await perform_video_deletion(ctx.channel, arg)

    except Exception as e:
        print(f"An unexpected error occurred in delete_video: {e}")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def get_db(ctx):
    channel = ctx.channel
    downloaded = await bot.db.fetch(
            "SELECT url FROM youtube_links WHERE status='downloaded'"
    )
    print("list of db links collected!")
    print(downloaded)
    with open ("downloaded-links.txt", 'w') as f:
        for link in downloaded:
            url = link['url']
            f.write(f"{url}\n")
            await asyncio.sleep(0.1)
    await channel.send("URLS of downloaded videos...", file=discord.File("downloaded-links.txt"))

@bot.command()
@commands.has_permissions(manage_messages=True,read_message_history=True)
async def delete_all_chats(ctx):
    
    print('Attempting to delete history...')
    try:
        deleted = await ctx.channel.purge(bulk=True)
        await ctx.channel.send(f'Deleted {len(deleted)} message(s)')

    except:
        print("couldn't delete channel message history...")

@bot.command()
async def scan_chat_history(ctx):
    channel = ctx.channel

    eastern = ZoneInfo("America/New_York")

    async for message in channel.history(limit=None):
        est_time = message.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(eastern)
        print(f"[{est_time.isoformat()}] {message.author}: {message.content}")
        await asyncio.sleep(0.5)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def scan_to_textfile(ctx):
    channel = ctx.channel
    eastern = ZoneInfo("America/New_York")

    with open ("chat_history.txt", 'w') as f:
        async for message in channel.history(limit=None):
            est_time = message.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(eastern)
            #print(f"[{est_time.isoformat()}] {message.author}: {message.content}", file=f)
            f.write(f"[{est_time.isoformat()}] {message.author}: {message.content}\n")
            await asyncio.sleep(0.1)
    await channel.send("Chat history in a textfile!", file=discord.File("chat_history.txt"))

@bot.command() # Delete this bot's messages
@commands.has_permissions(manage_messages=True)
async def delete_bot_chats(ctx):
    channel = ctx.channel
    eastern = ZoneInfo("America/New_York")

    async for message in channel.history(limit=None):
        if message.author.bot:
            est_time = message.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(eastern)
            print(f"Deleting [{est_time.isoformat()}] {message.author}: {message.content}")
            await message.delete()
            await asyncio.sleep(0.5)  # avoid hitting rate limits

@bot.command()
@commands.has_permissions(manage_messages=True)
async def hello(ctx):
    channel = ctx.channel
    await channel.send("Hello World!")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def whereami(ctx):
    print(
        f"Seen message in guild={ctx.guild.id}, "
        f"channel={ctx.channel.id}"
    )
    await ctx.send("Printed location to logs!")

# Delete Youtube links
@bot.command()
@commands.has_permissions(manage_messages=True)
async def delete_youtube_links(ctx):
    channel = ctx.channel
    eastern = ZoneInfo("America/New_York")

    async for message in channel.history(limit=None):
        # Check for YouTube links using your existing regex function
        matches = detect_links.Regex.search_for_youtube_link(message.content)

        if matches:
            est_time = message.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(eastern)
            print(f"Deleting YouTube link [{est_time.isoformat()}] {message.author}: {message.content}")
            await message.delete()
            await asyncio.sleep(0.5)  # avoid rate limits

@bot.command()
async def reinstate_video(ctx, url: str = None):
    db = bot.db

    # Case 1: Reinstate ALL rejected links
    if url is None:
        rows = await db.fetch(
            "SELECT url FROM youtube_links WHERE status = 'rejected'"
        )

        if not rows:
            await ctx.send("No rejected links to reinstate.")
            return

        for row in rows:
            await db.execute(
                "UPDATE youtube_links SET status='pending_approval' WHERE url=$1",
                row["url"]
            )
            await send_to_review_channel(row["url"])

        await ctx.send(f"Reinstated {len(rows)} rejected link(s).")
        return

    # Case 2: Reinstate a single URL
    result = await db.execute(
        "UPDATE youtube_links SET status='pending_approval' WHERE url=$1 AND status='rejected'",
        url
    )

    if result.endswith("0"):
        await ctx.send("That link is not in the rejected list.")
        return

    await send_to_review_channel(url)
    await ctx.send("Link reinstated and sent for approval.")


# Start the bot with the bot TOKEN and log level as debug
bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.DEBUG, root_logger=True)



## postgres commands to view database ##

# psql -h localhost -U ytbot -d ytbot_db //enter the database, password='secretpassword'

# SELECT * FROM youtube_links;        // View full table
# SELECT * FROM youtube_links WHERE status='approved'; // View approved
# SELECT * FROM youtube_links WHERE status='rejected'; // View rejected
# DELETE FROM youtube_links WHERE url='https://youtu.be/dQw4w9WgXcQ'; // Delete specific entry
# DELETE FROM youtube_links; // Delete all entries