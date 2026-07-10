import os
import discord
from discord.ext import commands
from quart import Quart
import asyncio
import re
import sys
import traceback
from drive_utils import GoogleDriveManager

# --- INITS & CORE SETUP ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

try:
    drive_manager = GoogleDriveManager()
except Exception as e:
    print(f"Failed to initialize Google Drive Manager: {e}")
    drive_manager = None

FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")

# --- LEAN STATE STORAGE ---
class JukeboxSession:
    def __init__(self):
        self.catalog = []            
        self.current_track_id = None 
        self.manual_skip = False     

    def get_current_track(self):
        if not self.current_track_id or not self.catalog:
            return None
        for track in self.catalog:
            if track['id'] == self.current_track_id:
                return track
        return None

    def get_current_index(self):
        current_track = self.get_current_track()
        if current_track and current_track in self.catalog:
            return self.catalog.index(current_track)
        return -1

sessions = {}

def get_session(guild_id) -> JukeboxSession:
    if guild_id not in sessions:
        sessions[guild_id] = JukeboxSession()
    return sessions[guild_id]

# --- CORE STREAM PROCESS ENGINE ---
async def start_track_stream(ctx, seek_time=None):
    if not ctx.guild: return
    session = get_session(ctx.guild.id)
    
    target_file = session.get_current_track()
    if not target_file: return

    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await ctx.send("You must be in a voice channel to stream music!")

    await ctx.send(f"Processing Track: `{target_file['name']}`" + (f" (Seeking to {seek_time}s)..." if seek_time else "..."))

    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        session.manual_skip = True
        ctx.voice_client.stop()
        await asyncio.sleep(0.1)

    loop = asyncio.get_event_loop()
    local_path, success = await loop.run_in_executor(
        None, drive_manager.get_or_download_track, target_file['id'], target_file['name']
    )

    if success and local_path:
        try:
            ffmpeg_options = f"-ss {seek_time}" if seek_time else None
            audio_source = discord.FFmpegPCMAudio(local_path, options=ffmpeg_options)
            
            session.manual_skip = False
            ctx.voice_client.play(
                audio_source, 
                after=lambda e: bot.loop.create_task(handle_autoplay_next(ctx, e))
            )
            await ctx.send(f"🎶 Now playing: `{target_file['name']}`")
        except Exception as e:
            await ctx.send(f"Failed to load stream via FFmpeg process: {e}")
    else:
        await ctx.send("Could not retrieve track files from Google Drive cloud layout storage.")

async def handle_autoplay_next(ctx, error):
    if not ctx.guild: return
    session = get_session(ctx.guild.id)
        
    if error: print(f"Autoplay process stream error: {error}")
    if not ctx.voice_client or not ctx.voice_client.is_connected(): return
    if session.manual_skip:
        session.manual_skip = False
        return

    curr_idx = session.get_current_index()
    if curr_idx != -1 and curr_idx + 1 < len(session.catalog):
        next_track = session.catalog[curr_idx + 1]
        session.current_track_id = next_track['id']
        await start_track_stream(ctx)
    else:
        await ctx.send("🏁 Reached the end of your Google Drive folder playlist loop.")

# --- COMMANDS ---

@bot.event
async def on_ready():
    print(f"ID-Stabilized Jukebox Live as: {bot.user.name}")
    print("------")

@bot.event
async def on_message(message):
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    print(f"❌ [ERROR] {ctx.command.name if ctx.command else 'Unknown'}: {error}", file=sys.stderr)
    await ctx.send(f"⚠️ App Exception raised: `{error}`")

@bot.command(name="join")
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client: await ctx.voice_client.move_to(channel)
        else: await channel.connect()
        await ctx.send(f"Connected to **{channel.name}**.")
    else:
        await ctx.send("You need to enter a voice room first!")

@bot.command(name="leave")
async def leave(ctx):
    if not ctx.guild: return
    session = get_session(ctx.guild.id)
    if ctx.voice_client:
        session.manual_skip = True
        await ctx.voice_client.disconnect()
        session.current_track_id = None
        await ctx.send("Left voice room and reset playback states.")
        sessions.pop(ctx.guild.id, None)
    else:
        await ctx.send("I am not actively in a voice room right now.")

@bot.command(name="list")
async def list_tracks(ctx):
    if not drive_manager: return await ctx.send("Google Drive configurations are missing.")
    if not ctx.guild: return
    session = get_session(ctx.guild.id)
    
    await ctx.send("Scanning your Google Drive folder layout sequence...")
    files = drive_manager.list_audio_files(FOLDER_ID)
    if not files: return await ctx.send("No audio files detected inside your specified Drive directory.")
    
    session.catalog = files  
    active_track = session.get_current_track()
    
    # Split message into chunks to avoid 4000 limit
    response = "**📁 Google Drive Jukebox Playlist Tracker:**\n\n"
    
    for idx, f in enumerate(files, 1):
        is_playing = (active_track and f['id'] == active_track['id'] and ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()))
        line = f"▶️ **{idx}. {f['name']}** *(Now Playing)*\n" if is_playing else f"{idx}. `{f['name']}`\n"
        
        if len(response) + len(line) > 3000:
            await ctx.send(response)
            response = ""
        response += line
        
    if response:
        await ctx.send(response)

@bot.command(name="play")
async def play(ctx, *, user_input: str = None):
    if not ctx.guild: return
    session = get_session(ctx.guild.id)

    if not session.catalog:
        session.catalog = drive_manager.list_audio_files(FOLDER_ID)
        if not session.catalog: return await ctx.send("Your Google Drive media directory seems to be empty.")

    if user_input is None:
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            return await ctx.send("▶️ Resumed track playback.")
        if not session.current_track_id:
            session.current_track_id = session.catalog[0]['id']
        await start_track_stream(ctx)
        return

    if user_input.isdigit():
        idx = int(user_input) - 1
        if 0 <= idx < len(session.catalog):
            session.current_track_id = session.catalog[idx]['id']
            await start_track_stream(ctx)
        else:
            await ctx.send(f"Invalid item position index. Select 1 to {len(session.catalog)}.")
    else:
        query = user_input.lower().strip()
        matches = [f for f in session.catalog if re.search(rf"\b{re.escape(query)}\b", f['name'].lower())]
        
        if not matches: return await ctx.send(f"🔍 No tracks found matching: `{user_input}`")
        if len(matches) > 1:
            response = "🔍 Multiple tracks matched your query. Use the specific track number:\n\n"
            for f in matches:
                orig_idx = session.catalog.index(f) + 1
                response += f"**[{orig_idx}]** {f['name']}\n"
            return await ctx.send(response)
        
        session.current_track_id = matches[0]['id']
        await start_track_stream(ctx)

@bot.command(name="pause")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused playback.")

@bot.command(name="resume")
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed playback.")

@bot.command(name="seek")
async def seek(ctx, seconds: int):
    if not ctx.voice_client or (not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused()):
        return await ctx.send("There is no active music stream to manipulate timestamps on.")
    await start_track_stream(ctx, seek_time=seconds)

@bot.command(name="help")
async def help_menu(ctx):
    embed = discord.Embed(
        title="🎵 Multi-Server StreamJukebox Help Menu",
        description="Stream media sequentially direct from your Google Drive folder mapping layout. Prefix: `!`",
        color=discord.Color.blue()
    )
    embed.add_field(name="📁 Catalog Navigation", value="`!list` - Lists all tracks and shows what's playing.", inline=False)
    embed.add_field(name="▶️ Flow Controls", value="`!play` - Resumes/starts playback.\n`!play <number>` - Jumps to track number.\n`!play <keyword>` - Searches tracks.", inline=False)
    embed.add_field(name="🎛️ Timeline Modifiers", value="`!pause`/`!resume` - Flow states.\n`!seek <seconds>` - Set timestamp.", inline=False)
    embed.add_field(name="🔌 Utility States", value="`!join`/`!leave` - Voice room control.", inline=False)
    await ctx.send(embed=embed)

web_app = Quart(__name__)

@web_app.route('/')
async def home():
    return "Multi-Server Jukebox Server State: ONLINE"

async def main():
    port = int(os.getenv("PORT", 10000))
    loop = asyncio.get_event_loop()
    loop.create_task(web_app.run_task(host="0.0.0.0", port=port))
    
    token = os.getenv('DISCORD_TOKEN')
    if not token: raise ValueError("Critical Error: 'DISCORD_TOKEN' environment variable is missing!")
    async with bot: await bot.start(token)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Process terminated locally.")