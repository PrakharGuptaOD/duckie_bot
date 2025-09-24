# Discord Music Bot with YouTube and SoundCloud Support
# Required packages:
# pip install discord.py youtube_dl pynacl ffmpeg-python
# Also need FFmpeg installed on system: https://ffmpeg.org/download.html

import discord
from discord.ext import commands
from discord import app_commands
import youtube_dl
import asyncio
import os
from collections import deque
from typing import Optional

# YouTube DL options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': True
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')
        self.requester = data.get('requester')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False, requester=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            # Take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        data['requester'] = requester
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

class MusicQueue:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.is_playing = False
        self.loop = False
        self.loop_queue = False
        
    def add(self, song):
        self.queue.append(song)
    
    def get_next(self):
        if self.loop and self.current:
            return self.current
        elif self.loop_queue and self.current:
            self.queue.append(self.current)
        
        if len(self.queue) > 0:
            self.current = self.queue.popleft()
            return self.current
        return None
    
    def clear(self):
        self.queue.clear()
        self.current = None
        self.is_playing = False

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        
        super().__init__(command_prefix='!', intents=intents)
        self.music_queues = {}
    
    async def setup_hook(self):
        # Sync slash commands
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")
    
    def get_queue(self, guild_id):
        if guild_id not in self.music_queues:
            self.music_queues[guild_id] = MusicQueue()
        return self.music_queues[guild_id]

bot = MusicBot()

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Game(name="Music 🎵"))

@bot.tree.command(name="play", description="Play a song from YouTube or SoundCloud")
@app_commands.describe(url="The URL or search query for the song")
async def play(interaction: discord.Interaction, url: str):
    # Check if user is in voice channel
    if not interaction.user.voice:
        await interaction.response.send_message("You need to be in a voice channel to use this command!", ephemeral=True)
        return
    
    voice_channel = interaction.user.voice.channel
    
    # Defer the response as loading might take time
    await interaction.response.defer()
    
    # Get or create queue for this guild
    queue = bot.get_queue(interaction.guild.id)
    
    # Join voice channel if not already connected
    voice_client = interaction.guild.voice_client
    if not voice_client:
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
    
    try:
        # Create player source
        player = await YTDLSource.from_url(url, loop=bot.loop, stream=True, requester=interaction.user)
        
        # Add to queue
        queue.add(player)
        
        embed = discord.Embed(
            title="Added to Queue",
            description=f"**{player.title}**",
            color=discord.Color.green()
        )
        embed.add_field(name="Duration", value=f"{player.duration // 60}:{player.duration % 60:02d}" if player.duration else "Unknown", inline=True)
        embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)
        embed.add_field(name="Position", value=len(queue.queue), inline=True)
        if player.thumbnail:
            embed.set_thumbnail(url=player.thumbnail)
        
        await interaction.followup.send(embed=embed)
        
        # Start playing if not already playing
        if not voice_client.is_playing() and not queue.is_playing:
            await play_next(interaction.guild.id, voice_client, interaction.channel)
            
    except Exception as e:
        await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

async def play_next(guild_id, voice_client, text_channel):
    queue = bot.get_queue(guild_id)
    
    if voice_client.is_playing():
        return
    
    next_song = queue.get_next()
    if next_song:
        queue.is_playing = True
        
        def after_playing(error):
            if error:
                print(f"Player error: {error}")
            
            # Schedule playing next song
            asyncio.run_coroutine_threadsafe(
                play_next(guild_id, voice_client, text_channel),
                bot.loop
            )
        
        voice_client.play(next_song, after=after_playing)
        
        # Send now playing embed
        embed = discord.Embed(
            title="Now Playing 🎵",
            description=f"**{next_song.title}**",
            color=discord.Color.blue()
        )
        if next_song.duration:
            embed.add_field(name="Duration", value=f"{next_song.duration // 60}:{next_song.duration % 60:02d}", inline=True)
        embed.add_field(name="Requested by", value=next_song.requester.mention, inline=True)
        if next_song.thumbnail:
            embed.set_thumbnail(url=next_song.thumbnail)
        
        asyncio.run_coroutine_threadsafe(
            text_channel.send(embed=embed),
            bot.loop
        )
    else:
        queue.is_playing = False
        # Leave after 1 minute of inactivity
        await asyncio.sleep(60)
        if not voice_client.is_playing() and not queue.is_playing:
            await voice_client.disconnect()

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    
    if not voice_client or not voice_client.is_playing():
        await interaction.response.send_message("No song is currently playing!", ephemeral=True)
        return
    
    voice_client.stop()
    await interaction.response.send_message("⏭️ Skipped current song!")

@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
        await interaction.response.send_message("Bot is not connected to a voice channel!", ephemeral=True)
        return
    
    if voice_client.is_paused():
        await interaction.response.send_message("Playback is already paused!", ephemeral=True)
        return
    
    voice_client.pause()
    await interaction.response.send_message("⏸️ Paused playback!")

@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
        await interaction.response.send_message("Bot is not connected to a voice channel!", ephemeral=True)
        return
    
    if not voice_client.is_paused():
        await interaction.response.send_message("Playback is not paused!", ephemeral=True)
        return
    
    voice_client.resume()
    await interaction.response.send_message("▶️ Resumed playback!")

@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
        await interaction.response.send_message("Bot is not connected to a voice channel!", ephemeral=True)
        return
    
    queue = bot.get_queue(interaction.guild.id)
    queue.clear()
    voice_client.stop()
    await voice_client.disconnect()
    
    await interaction.response.send_message("⏹️ Stopped playback and cleared queue!")

@bot.tree.command(name="queue", description="Show the current queue")
async def show_queue(interaction: discord.Interaction):
    queue = bot.get_queue(interaction.guild.id)
    
    if not queue.current and len(queue.queue) == 0:
        await interaction.response.send_message("Queue is empty!", ephemeral=True)
        return
    
    embed = discord.Embed(title="Music Queue 🎵", color=discord.Color.purple())
    
    if queue.current and interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        embed.add_field(
            name="Now Playing",
            value=f"**{queue.current.title}**\nRequested by {queue.current.requester.mention}",
            inline=False
        )
    
    if len(queue.queue) > 0:
        queue_list = []
        for i, song in enumerate(list(queue.queue)[:10], 1):
            queue_list.append(f"{i}. **{song.title}**\n   Requested by {song.requester.mention}")
        
        embed.add_field(
            name=f"Up Next ({len(queue.queue)} songs)",
            value="\n".join(queue_list),
            inline=False
        )
        
        if len(queue.queue) > 10:
            embed.set_footer(text=f"And {len(queue.queue) - 10} more songs...")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="loop", description="Toggle loop for current song")
async def loop(interaction: discord.Interaction):
    queue = bot.get_queue(interaction.guild.id)
    queue.loop = not queue.loop
    
    status = "enabled" if queue.loop else "disabled"
    await interaction.response.send_message(f"🔁 Loop {status} for current song!")

@bot.tree.command(name="loopqueue", description="Toggle loop for entire queue")
async def loopqueue(interaction: discord.Interaction):
    queue = bot.get_queue(interaction.guild.id)
    queue.loop_queue = not queue.loop_queue
    
    status = "enabled" if queue.loop_queue else "disabled"
    await interaction.response.send_message(f"🔁 Queue loop {status}!")

@bot.tree.command(name="volume", description="Set the volume (0-100)")
@app_commands.describe(volume="Volume level (0-100)")
async def volume(interaction: discord.Interaction, volume: int):
    if not 0 <= volume <= 100:
        await interaction.response.send_message("Volume must be between 0 and 100!", ephemeral=True)
        return
    
    voice_client = interaction.guild.voice_client
    if not voice_client:
        await interaction.response.send_message("Bot is not connected to a voice channel!", ephemeral=True)
        return
    
    queue = bot.get_queue(interaction.guild.id)
    if queue.current:
        queue.current.volume = volume / 100
    
    await interaction.response.send_message(f"🔊 Volume set to {volume}%")

@bot.tree.command(name="disconnect", description="Disconnect the bot from voice channel")
async def disconnect(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
        await interaction.response.send_message("Bot is not connected to a voice channel!", ephemeral=True)
        return
    
    queue = bot.get_queue(interaction.guild.id)
    queue.clear()
    await voice_client.disconnect()
    
    await interaction.response.send_message("👋 Disconnected from voice channel!")

# Run the bot
if __name__ == "__main__":
    # Replace with your bot token
    
    bot.run(DISCORD_TOKEN)
