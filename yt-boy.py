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
from zoneinfo import ZoneInfo

# Download directory from .env
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads/"))

# Load .env
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_API_TOKEN")
JELLYFIN_API_TOKEN = os.getenv("JELLYFIN_API_TOKEN")
DB_HOST = os.getenv("DB_HOST")
DB_URL = os.getenv("DB_URL")
JELLYFIN_URL = os.getenv("JELLYFIN_URL")
JELLYFIN_LIBRARY_ID = os.getenv("JELLYFIN_LIBRARY_ID")

# Parse channel ID's
TARGET_CHANNEL_IDS = [int(x) for x in os.environ.get("TARGET_CHANNEL_IDS", "").split(",") if x]
print("Channel ID[0]: "+f"{TARGET_CHANNEL_IDS[0]}")
print("Channel ID[1]: "+f"{TARGET_CHANNEL_IDS[1]}")

# Discord intents, sorta the permissions of the bot.
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

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
    bot.link_messages = []
    print("bot.link_messages list created!")
    bot.delete_messages = []
    print("bot.delete_messages list created!")
    bot.video_title = ''
    print("bot.video_title has been created!")


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
                await store_link(bot.db, url, message)
            print("ADDING MESSAGE TO LINK_MESSAGES IN ON_MESSAGE!!!")
            bot.link_messages.append({"message": message})
    # Make sure commands still work
    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    if not user.bot:
        try:
            message = reaction.message

            # Deletion of video Logic
            if reaction.emoji == "🖕🏻":
                if(len(bot.delete_messages) != 0):
                    for entry in bot.delete_messages:
                        if entry["message"].id == message.id:
                            await perform_video_deletion(message.channel, entry["title"])
                            for e in bot.delete_messages:
                                await e["message"].delete()
                            bot.delete_messages.clear()
                            break

            # Approval of video Logic
            if reaction.message.channel.id == TARGET_CHANNEL_IDS[1]:
                url_text = reaction.message.content.split()[-1]  # crude way to extract URL
                # Check current status
                current_status = await bot.db.fetchval("SELECT status FROM youtube_links WHERE url=$1", url_text)
                if current_status != "pending_approval":
                    print(f"Reaction ignored; link already {current_status}: {url_text}")
                    await bot.get_channel(message.channel.id).send(f"Link has already been {current_status}...")
                    return
                # Update based on reaction
                if reaction.emoji == "✅":
                    await bot.db.execute("UPDATE youtube_links SET status='approved' WHERE url=$1", url_text)
                    print(f"Link approved: {url_text}")
                elif reaction.emoji == "❌":
                    await bot.db.execute("UPDATE youtube_links SET status='rejected' WHERE url=$1", url_text)
                    print(f"Link rejected: {url_text}")
                    await bot.get_channel(message.channel.id).send(f"Link has been rejected!")
                    await message.delete()
        except Exception as e:
            print(f"An unexpected error occurred in on_reaction_add: {e}")
    else:
        return


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
async def store_link(db, url, message):
    try:
        existing = await bot.db.fetchrow("SELECT status FROM youtube_links WHERE url = $1", url)

        if existing:
            print(f"Link already exists ({existing['status']}), ignoring: {url}")
            await bot.get_channel(message.channel.id).send(f"Link already exists ({existing['status']}), ignoring video...")
            await message.delete()
            return
        
        # Insert into DB
        await db.execute("INSERT INTO youtube_links(url, status) VALUES($1, 'pending_approval') ON CONFLICT(url) DO NOTHING", url)
        print(f"Stored link in DB: {url}")

        # Send to review channel
        await send_to_review_channel(url)
    except Exception as e:
        print("Error storing link:", e)

async def download_approved_videos():
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Fetch approved links
        approved_links = await bot.db.fetch("SELECT url FROM youtube_links WHERE status='approved'")

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
                    bot.video_title = ydl.prepare_filename(info)
                    print("video_title: "+bot.video_title)
                
                # Update DB
                await bot.db.execute("UPDATE youtube_links SET status='downloaded' WHERE url=$1", url)
                await bot.db.execute("UPDATE youtube_links SET title=$2 WHERE url=$1", url, Path(bot.video_title).name)
                print(f"Download complete: {url}")
                await trigger_jellyfin_scan()
                await delete_downloaded_link_channel_messages(url)

            except Exception as e:
                print(f"Error downloading {url}: {e}")

        # Wait before checking again
        await asyncio.sleep(60)  # check every minute

async def delete_downloaded_link_channel_messages(url):
    try:
        for i in range(len(bot.link_messages)):
        #for msg in bot.link_messages:
            if url in bot.link_messages[i]["message"].content:
                print("URL found in message!")
                await bot.link_messages[i]["message"].delete()
        print("Messages deleted!")
    
    except Exception as e:
        print(f"An unexpected error occurred in delete_downloaded_link_channel_messages: {e}")

    for id in TARGET_CHANNEL_IDS:
        channel = bot.get_channel(id)
        await channel.send(f"{Path(bot.video_title).name} downloaded and library scan triggered!")

async def send_to_review_channel(url: str):
    # Send to review channel
        channel = bot.get_channel(TARGET_CHANNEL_IDS[1])
        if channel:
            msg = await channel.send(f"New YouTube link pending approval: {url}")
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            bot.link_messages.append({"message": msg})

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

    downloaded = await bot.db.fetch("SELECT title FROM youtube_links WHERE status='downloaded'")
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
        await bot.db.execute("DELETE FROM youtube_links WHERE title=$1", title) # Delete from db.
        await trigger_jellyfin_scan()
        await channel.send(f"Video `{title}` deleted and Jellyfin rescan triggered!")
    except FileNotFoundError:
        print("File not found")
        await channel.send(f"The file {title} does not exist...")
    except Exception as e:
        print(f"An unexpected error occurred in perform_video_deletion: {e}")


## BOT COMMANDS ##
@bot.command()
@commands.has_permissions(manage_messages=True)
async def get_links(ctx):
    channel = ctx.channel
    if(channel.id == TARGET_CHANNEL_IDS[0]):
        async for msg in channel.history(limit=None):
            matches = detect_links.Regex.search_for_youtube_link(msg.content)
            if matches:
                for url in matches:
                    await store_link(bot.db, url)
                    print("Link stored from get link.")
  
@bot.command()
@commands.has_permissions(manage_messages=True)
async def get_pending(ctx):
    pending_approval = await bot.db.fetch("SELECT url FROM youtube_links WHERE status='pending_approval'")
    for url in pending_approval:
        await send_to_review_channel(url["url"])

@bot.command()
@commands.has_permissions(manage_messages=True)
async def delete_video(ctx, arg: str = None):
    try:
        if(arg == None):
            print("ARG is empty, giving list...")
            await list_downloaded_videos_for_deletion(ctx)
        elif(arg.startswith("https://")):
            print("arg is a link, finding title...")
            title = await bot.db.fetchrow("SELECT title FROM youtube_links WHERE url=$1", arg) 
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
async def get_link_messages(ctx):
    print("================================================================================")
    print("::::::LINK_MESSAGES::::::")
    for msg in bot.link_messages:
        print("::::::MESSAGE OBJECT::::::")
        print(msg)
        print("::::::MESSAGE CONTENT::::::")
        print(msg['message'].content)
    print("================================================================================")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def get_db(ctx, arg: str):
    channel = ctx.channel
    if(arg == "downloaded" or arg == "pending_approval" or arg == "approved" or arg == "rejected"):
        links = await bot.db.fetch("SELECT url FROM youtube_links WHERE status=$1", arg)
        print("list of db links collected!")
        print(links)
        with open ("links.txt", 'w') as f:
            for link in links:
                url = link['url']
                f.write(f"{url}\n")
                await asyncio.sleep(0.1)
        await channel.send(f"URLS of {arg} videos...", file=discord.File("links.txt"))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def status(ctx):
    try:    
        status_count = []
        print("Obtaining status...")
        pending = await bot.db.fetch("SELECT id FROM youtube_links WHERE status='pending_approval'")
        status_count.append(len(pending))
        approved = await bot.db.fetch("SELECT id FROM youtube_links WHERE status='approved'")
        status_count.append(len(approved))
        rejected = await bot.db.fetch("SELECT id FROM youtube_links WHERE status='rejected'")
        status_count.append(len(rejected))
        downloaded = await bot.db.fetch("SELECT id FROM youtube_links WHERE status='downloaded'")
        status_count.append(len(downloaded))
        await ctx.send(f":STATUS: Pending_Approval[{status_count[0]}] :: Approved[{status_count[1]}] :: Rejected[{status_count[2]}] :: Downloaded[{status_count[3]}]")
        status_count.clear()
    except Exception as e:
        print(f"An unexpected error occurred in status: {e}")

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

@bot.command()
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
        f"channel={ctx.channel.id}")
    await ctx.send("Printed location to logs!")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def delete_youtube_links(ctx):
    channel = ctx.channel
    eastern = ZoneInfo("America/New_York")
    async for message in channel.history(limit=None):
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
        rows = await db.fetch("SELECT url FROM youtube_links WHERE status = 'rejected'")
        if not rows:
            await ctx.send("No rejected links to reinstate.")
            return
        for row in rows:
            await db.execute("UPDATE youtube_links SET status='pending_approval' WHERE url=$1", row["url"])
            await send_to_review_channel(row["url"])
        await ctx.send(f"Reinstated {len(rows)} rejected link(s).")
        return
    # Case 2: Reinstate a single URL
    result = await db.execute("UPDATE youtube_links SET status='pending_approval' WHERE url=$1 AND status='rejected'", url)
    if result.endswith("0"):
        await ctx.send("That link is not in the rejected list.")
        return
    await send_to_review_channel(url)
    await ctx.send("Link reinstated and sent for approval.")

# Start the bot with the bot TOKEN and log level as debug
bot.run(DISCORD_TOKEN, root_logger=True)



## postgres commands to view database ##

# psql -h localhost -U ytbot -d ytbot_db //enter the database, password='secretpassword'

# SELECT * FROM youtube_links;        // View full table
# SELECT * FROM youtube_links WHERE status='approved'; // View approved
# SELECT * FROM youtube_links WHERE status='rejected'; // View rejected
# DELETE FROM youtube_links WHERE url='https://youtu.be/dQw4w9WgXcQ'; // Delete specific entry
# DELETE FROM youtube_links; // Delete all entries