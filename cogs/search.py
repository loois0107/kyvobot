import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

class Search(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="search", description="Search information from Wikipedia.")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        lang = "ko" if "ko" in str(interaction.locale).lower() else "en"
        url = f"https://{lang}.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "prop": "extracts",
            "exintro": True,
            "explaintext": True,
            "titles": query,
            "redirects": 1
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        pages = data.get("query", {}).get("pages", {})
                        page_id = list(pages.keys())[0]
                        if page_id == "-1":
                            msg = self.bot.locale_manager.get(str(interaction.locale), "search_not_found")
                            await interaction.followup.send(msg)
                            return
                        page_data = pages[page_id]
                        title = page_data.get("title")
                        extract = page_data.get("extract", "")
                        if len(extract) > 1024:
                            extract = extract[:1021] + "..."
                        embed = discord.Embed(title=title, description=extract, color=discord.Color.blue())
                        await interaction.followup.send(embed=embed)
                    else:
                        msg = self.bot.locale_manager.get(str(interaction.locale), "search_error")
                        await interaction.followup.send(msg)
        except Exception:
            msg = self.bot.locale_manager.get(str(interaction.locale), "search_error")
            await interaction.followup.send(msg)

async def setup(bot):
    await bot.add_cog(Search(bot))
