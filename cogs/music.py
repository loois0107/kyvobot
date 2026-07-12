import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio
import imageio_ffmpeg
import os

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
    'extractor_args': {
        'youtube': {
            'player_client': ['tvhtml5', 'android', 'mweb']
        }
    }
}

ffmpeg_options = {
    'options': '-vn'
}

if os.path.exists('cookies.txt'):
    ytdl_format_options['cookiefile'] = 'cookies.txt'

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, executable=imageio_ffmpeg.get_ffmpeg_exe(), **ffmpeg_options), data=data)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}

    def check_queue(self, interaction, guild_id):
        try:
            if guild_id in self.queues and self.queues[guild_id]:
                next_track = self.queues[guild_id].pop(0)
                vc = interaction.guild.voice_client
                if vc:
                    vc.play(next_track, after=lambda e: self.check_queue(interaction, guild_id))
                    msg = self.bot.locale_manager.get(str(interaction.locale), "music_now_playing", title=next_track.title)
                    asyncio.run_coroutine_threadsafe(interaction.channel.send(msg), self.bot.loop)
        except Exception:
            pass

    @app_commands.command(name="play", description="Play a song from YouTube.")
    async def play(self, interaction: discord.Interaction, search: str):
        await interaction.response.defer()
        try:
            guild_id = str(interaction.guild_id)
            settings = await self.bot.get_guild_settings(guild_id)
            
            if not settings.get("music_enabled", True):
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_disabled")
                await interaction.followup.send(msg, ephemeral=True)
                return

            if not interaction.user.voice:
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_not_in_voice")
                await interaction.followup.send(msg, ephemeral=True)
                return

            vc = interaction.guild.voice_client
            if not vc:
                vc = await interaction.user.voice.channel.connect()

            player = await YTDLSource.from_url(search, loop=self.bot.loop, stream=True)

            if guild_id not in self.queues:
                self.queues[guild_id] = []

            if vc.is_playing():
                self.queues[guild_id].append(player)
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_added_queue", title=player.title)
                await interaction.followup.send(msg)
            else:
                vc.play(player, after=lambda e: self.check_queue(interaction, guild_id))
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_now_playing", title=player.title)
                await interaction.followup.send(msg)
        except Exception as e:
            msg = self.bot.locale_manager.get(str(interaction.locale), "music_error", error=str(e))
            await interaction.followup.send(msg)

    @app_commands.command(name="skip", description="Skip the current song.")
    async def skip(self, interaction: discord.Interaction):
        try:
            guild_id = str(interaction.guild_id)
            settings = await self.bot.get_guild_settings(guild_id)
            
            if not settings.get("music_enabled", True):
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_disabled")
                await interaction.response.send_message(msg, ephemeral=True)
                return

            vc = interaction.guild.voice_client
            if vc and vc.is_playing():
                vc.stop()
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_skipped")
                await interaction.response.send_message(msg)
            else:
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_no_playing")
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            msg = self.bot.locale_manager.get(str(interaction.locale), "music_error", error=str(e))
            await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="stop", description="Stop music and leave the voice channel.")
    async def stop(self, interaction: discord.Interaction):
        try:
            guild_id = str(interaction.guild_id)
            settings = await self.bot.get_guild_settings(guild_id)
            
            if not settings.get("music_enabled", True):
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_disabled")
                await interaction.response.send_message(msg, ephemeral=True)
                return

            vc = interaction.guild.voice_client
            if vc:
                if guild_id in self.queues:
                    self.queues[guild_id].clear()
                await vc.disconnect()
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_stopped")
                await interaction.response.send_message(msg)
            else:
                msg = self.bot.locale_manager.get(str(interaction.locale), "music_not_connected")
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            msg = self.bot.locale_manager.get(str(interaction.locale), "music_error", error=str(e))
            await interaction.response.send_message(msg, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Music(bot))
