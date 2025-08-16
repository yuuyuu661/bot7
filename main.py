import os
import random
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from PIL import Image

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# サイコロ画像のパス
DICE_FOLDER = "dice_images"  # dice_1.png ~ dice_6.png を入れておくフォルダ
ROLLING_IMAGE = "rolling.gif"  # 回転中アニメーション用のgifなど（任意）

# --- サイコロ画像を合成して保存する関数 ---
def create_dice_image(results, filename="result.png"):
    images = [Image.open(os.path.join(DICE_FOLDER, f"dice_{r}.png")) for r in results]
    width, height = images[0].size
    combined = Image.new("RGBA", (width * len(images), height), (255, 255, 255, 0))
    for i, img in enumerate(images):
        combined.paste(img, (i * width, 0), img)
    combined.save(filename)
    return filename

# --- 親決めコマンド ---
@tree.command(name="oya", description="親を決めるサイコロを振ります")
async def oya(interaction: discord.Interaction):
    await interaction.response.defer()

    # 動作中のサイコロを送信
    rolling_msg = await interaction.followup.send(file=discord.File(ROLLING_IMAGE))

    await asyncio.sleep(2)

    # サイコロ結果（2個）
    results = [random.randint(1, 6), random.randint(1, 6)]
    filename = create_dice_image(results, "oya_result.png")

    # 結果送信
    await interaction.followup.send(f"🎲 親決めサイコロの結果：{sum(results)}！", file=discord.File(filename))

    # ローリング中の画像を削除
    await rolling_msg.delete()

# --- 通常のサイコロコマンド ---
@tree.command(name="dice", description="サイコロを振ります")
async def dice(interaction: discord.Interaction, count: int = 2):
    await interaction.response.defer()

    # 動作中のサイコロを送信
    rolling_msg = await interaction.followup.send(file=discord.File(ROLLING_IMAGE))

    await asyncio.sleep(2)

    # サイコロ結果
    results = [random.randint(1, 6) for _ in range(count)]
    filename = create_dice_image(results, "dice_result.png")

    await interaction.followup.send(
        f"🎲 出目: {', '.join(map(str, results))}（合計 {sum(results)}）",
        file=discord.File(filename)
    )

    # ローリング中の画像を削除
    await rolling_msg.delete()


@bot.event
async def on_ready():
    try:
        synced = await tree.sync()
        print(f"✅ {len(synced)} コマンドを同期しました")
    except Exception as e:
        print(f"❌ コマンド同期エラー: {e}")
    print(f"Bot {bot.user} が起動しました")


bot.run(os.getenv("DISCORD_TOKEN"))
