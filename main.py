# main.py
import os
import random
import asyncio
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# 画像処理
from PIL import Image

# ================== 基本設定 ==================
TOKEN = os.getenv("DISCORD_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
# Slash/ボタン運用だけなら message_content は不要

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

# ================== 可変設定 ==================
# ダイス画像格納先（dice_1.png ... dice_6.png を置く）
DICE_ASSET_DIR = "assets/dice"

# ロール中アニメ（生成ファイルは WebP 失敗時 GIF）
ROLL_ANIM_FRAMES = 12         # コマ数
ROLL_ANIM_MS = 90             # 1コマms（約11fps）
COMPOSITE_GAP = 16            # 合成PNGでのダイス間隔px
DELETE_ANIM_AFTER_RESULT = True

# ベットUI
BET_STEP = 100
MAX_BET = 1_000_000

# サーバー通貨ボット向けの送金テンプレ
# {payer} = 支払う側メンション, {payee} = 受取側メンション, {amount} = 金額
# 例: "!pay {payer} {payee} {amount}" / "vc!tip {payee} {amount}"
TRANSFER_TEMPLATE = "!pay {payer} {payee} {amount}"

# 管理用：即時ギルド同期コマンドを使いたい場合のみ設定（未設定でOK）
SYNC_ALLOWED_FOR_ADMINS = True

# ================== ユーティリティ ==================
DICE_FACES = {1:"⚀",2:"⚁",3:"⚂",4:"⚃",5:"⚄",6:"⚅"}

def dice_face_str(vals: List[int]) -> str:
    return " ".join(DICE_FACES[v] for v in vals)

async def ack(inter: discord.Interaction):
    """Slash開始時のack（defer）。二重応答を防ぐための共通関数。"""
    if not inter.response.is_done():
        await inter.response.defer()

# ===== 役判定 =====
class HandResult:
    """
    rank: 大きいほど強い
      5: シゴロ(4-5-6)
      4: ゾロ目（1〜6）
      3: 目（1〜6）値で比較
      2: 役なし
      1: ヒフミ(1-2-3)
    """
    def __init__(self, rank: int, value: int, label: str):
        self.rank = rank
        self.value = value
        self.label = label
    def __str__(self): return self.label

def evaluate_hand(dice: List[int]) -> HandResult:
    d = sorted(dice)
    a, b, c = d
    if d == [1,2,3]:
        return HandResult(1, 0, "ヒフミ（即負）")
    if d == [4,5,6]:
        return HandResult(5, 0, "シゴロ（即勝）")
    if a == b == c:
        return HandResult(4, a, f"{a}ゾロ（即勝）")
    if a == b != c:
        return HandResult(3, c, f"{c}の目")
    if b == c != a:
        return HandResult(3, a, f"{a}の目")
    return HandResult(2, 0, "役なし")

def compare(parent: HandResult, child: HandResult) -> int:
    """ 親 vs 子 → 1:子勝 / -1:子負 / 0:引分 """
    if parent.rank != child.rank:
        return 1 if child.rank > parent.rank else -1
    if parent.value != child.value:
        return 1 if child.value > parent.value else -1
    return 0

def roll_dice() -> List[int]:
    return [random.randint(1,6) for _ in range(3)]

# ===== 送金出力 =====
def build_transfer_line(payer_id: int, payee_id: int, amount: int) -> str:
    payer = f"<@{payer_id}>"
    payee = f"<@{payee_id}>"
    try:
        return TRANSFER_TEMPLATE.format(payer=payer, payee=payee, amount=amount)
    except Exception:
        return f"[TRANSFER] {payer} -> {payee} : {amount}"

async def post_transfers(channel: discord.abc.Messageable, pairs: List[Tuple[int,int,int]], title: str):
    if not pairs:
        await channel.send(f"{title}\n（対象なし）")
        return
    lines = [build_transfer_line(p, r, a) for (p, r, a) in pairs]
    await channel.send(f"{title}\n" + "\n".join(lines))

# ================== 画像生成（Pillow） ==================
_DICE_CACHE: dict[int, Image.Image] = {}

def _load_die(n: int) -> Image.Image:
    img = _DICE_CACHE.get(n)
    if img is None:
        path = os.path.join(DICE_ASSET_DIR, f"dice_{n}.png")
        img = Image.open(path).convert("RGBA")
        _DICE_CACHE[n] = img
    return img

def _make_canvas(w: int, h: int, bg=(255,255,255,0)) -> Image.Image:
    return Image.new("RGBA", (w, h), bg)

def compose_three_dice_image(dice: List[int], gap: int = COMPOSITE_GAP) -> str:
    faces = [_load_die(n) for n in dice]
    die_w, die_h = faces[0].size
    W = die_w * 3 + gap * 2
    H = die_h
    canvas = _make_canvas(W, H)
    x = 0
    for img in faces:
        canvas.alpha_composite(img, (x, 0))
        x += die_w + gap
    out_path = f"/tmp/dice_{dice[0]}{dice[1]}{dice[2]}_{random.randint(0,99999)}.png"
    canvas.save(out_path, format="PNG")
    return out_path

def make_roll_animation(frames: int = ROLL_ANIM_FRAMES, duration_ms: int = ROLL_ANIM_MS, gap: int = COMPOSITE_GAP) -> tuple[str, List[int]]:
    """WebP（失敗時GIF）アニメを作り、末尾見た目の出目も返す"""
    sample = _load_die(1)
    die_w, die_h = sample.size
    W = die_w * 3 + gap * 2
    H = die_h
    seq = []
    last_dice = [1,1,1]
    for i in range(frames):
        cur = [random.randint(1,6) for _ in range(3)]
        last_dice = cur[:]
        canvas = _make_canvas(W,H)
        jitter = ((frames - i) % 3) - 1
        x = 0
        for n in cur:
            img = _load_die(n)
            scale = 0.94 + 0.06 * (i % 2)
            nw, nh = int(die_w*scale), int(die_h*scale)
            img2 = img.resize((nw, nh), Image.LANCZOS)
            y = max(0, (H - nh)//2 + jitter)
            canvas.alpha_composite(img2, (x + (die_w - nw)//2, y))
            x += die_w + gap
        seq.append(canvas)
    tmp_id = random.randint(0,99999)
    webp_path = f"/tmp/roll_{tmp_id}.webp"
    gif_path  = f"/tmp/roll_{tmp_id}.gif"
    try:
        seq[0].save(webp_path, save_all=True, append_images=seq[1:], duration=duration_ms, loop=0, disposal=2, format="WEBP")
        return webp_path, last_dice
    except Exception:
        seq[0].save(gif_path, save_all=True, append_images=seq[1:], duration=duration_ms, loop=0, disposal=2, format="GIF")
        return gif_path, last_dice

async def send_roll_animation(channel: discord.abc.Messageable, title: str) -> tuple[discord.Message, List[int], str]:
    anim_path, last_visual = make_roll_animation()
    msg = await channel.send(content=title, file=discord.File(anim_path, filename=os.path.basename(anim_path)))
    return msg, last_visual, anim_path

async def send_final_composited_image(channel, who_mention: str, role_label: str, dice: List[int], hand_label: str, tries: int):
    png_path = compose_three_dice_image(dice)
    text = f"{role_label} {who_mention} のロール #{tries}\n→ **{hand_label}**"
    await channel.send(content=text, file=discord.File(png_path, filename=os.path.basename(png_path)))

# ================== 状態管理 ==================
class RoundState:
    def __init__(self, user_id: int, role_label: str):
        self.user_id = user_id
        self.role_label = role_label
        self.tries = 0
        self.last_roll: Optional[List[int]] = None
        self.final: Optional[HandResult] = None

class GameState:
    def __init__(self, channel_id: int, host_id: int):
        self.channel_id = channel_id
        self.host_id = host_id

        self.lobby_open = True
        self.lobby_message_id: Optional[int] = None

        self.participants: List[int] = []
        self.parent_id: Optional[int] = None
        self.children_order: List[int] = []

        self.bets: Dict[int, int] = {}           # 確定ベット
        self.temp_bets: Dict[int, int] = {}      # クリック中の一時ベット
        self.bet_panel_message_id: Optional[int] = None

        self.turn_index = 0
        self.parent_hand: Optional[HandResult] = None
        self.phase: str = "lobby"                # lobby -> choose_parent -> betting -> parent_roll -> children_roll
        self.parent_round: Optional[RoundState] = None
        self.child_round: Optional[RoundState] = None
        self.lock = asyncio.Lock()

GAMES: Dict[int, GameState] = {}

# ================== 表示ヘルパ ==================
def lobby_text(game: GameState) -> str:
    mems = "、".join(f"<@{u}>" for u in game.participants) if game.participants else "—"
    return (
        "🎲 **チンチロ ロビー**\n"
        f"ホスト：<@{game.host_id}>\n"
        f"参加者：{mems}\n\n"
        "Joinで参加、Leaveで退出。ホストは「親を決める」で開始します。"
    )

def bet_panel_text(game: GameState) -> str:
    lines = ["💰 **ベット受付中**（+100/-100 で調整 → ✅確定）"]
    if not game.children_order:
        lines.append("子がいません。")
    else:
        for uid in game.children_order:
            cur = game.temp_bets.get(uid, game.bets.get(uid, 0))
            lines.append(f"・<@{uid}>：{cur}")
    lines.append("\n※ 親が開始すると締切になります。")
    return "\n".join(lines)

# ================== ロビー（参加）ビュー ==================
class LobbyView(discord.ui.View):
    def __init__(self, game: GameState, timeout: Optional[float] = 3600):
        super().__init__(timeout=timeout)
        self.game = game

    async def _refresh(self, message: discord.Message):
        await message.edit(content=lobby_text(self.game), view=self)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        if not self.game.lobby_open:
            await inter.followup.send("ロビーは締め切られています。", ephemeral=True); return
        uid = inter.user.id
        if uid in self.game.participants:
            await inter.followup.send("すでに参加しています。", ephemeral=True); return
        self.game.participants.append(uid)
        await inter.followup.send("参加しました。", ephemeral=True)
        await self._refresh(inter.message)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger)
    async def leave_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        uid = inter.user.id
        if uid in self.game.participants:
            self.game.participants.remove(uid)
            await inter.followup.send("退出しました。", ephemeral=True)
            await self._refresh(inter.message)
        else:
            await inter.followup.send("参加していません。", ephemeral=True)

    @discord.ui.button(label="親を決める", style=discord.ButtonStyle.primary)
    async def decide_parent_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.defer()
        if inter.user.id != self.game.host_id:
            await inter.followup.send("ホストのみが開始できます。", ephemeral=True); return
        if len(self.game.participants) < 2:
            await inter.followup.send("参加者が2人以上必要です。", ephemeral=True); return
        if not self.game.lobby_open:
            await inter.followup.send("すでに開始済みです。", ephemeral=True); return

        self.game.lobby_open = False
        self.game.phase = "choose_parent"
        await inter.followup.send("▶ 親決めを開始します。順番にロールします…")

        best_uid = None
        best_hand: Optional[HandResult] = None
        logs = []

        # 1人ずつ：アニメ送信→確定（合成PNG）→アニメ削除
        for uid in self.game.participants:
            user = await bot.fetch_user(uid)
            anim_msg, _, _ = await send_roll_animation(inter.channel, title=f"【親決め】{user.display_name} のロール中…")
            dice = roll_dice()
            hand = evaluate_hand(dice)

            # アニメ「止まった」表現＆確定画像
            try:
                await anim_msg.edit(content=f"【親決め】{user.display_name} のロール中…\n（…止まりました）")
            except Exception:
                pass
            await send_final_composited_image(inter.channel, who_mention=f"<@{uid}>", role_label="【親決め】", dice=dice, hand_label=str(hand), tries=1)
            if DELETE_ANIM_AFTER_RESULT:
                try: await anim_msg.delete()
                except Exception: pass

            logs.append(f"<@{uid}>: {dice_face_str(dice)} → **{hand}**")
            if best_hand is None or compare(best_hand, hand) > 0:
                best_uid, best_hand = uid, hand

        # 結果発表
        await inter.channel.send("結果：\n" + "\n".join(logs))
        self.game.parent_id = best_uid
        self.game.children_order = [u for u in self.game.participants if u != best_uid]
        await inter.channel.send(
            f"👑 親は <@{best_uid}> に決定！\n"
            "このあとベットパネルが出ます。親は開始準備ができたら `/chi_parent_roll` を実行してください。"
        )
        self.game.phase = "betting"
        await send_bet_panel(inter.channel, self.game)

# ================== ベットビュー ==================
class BetView(discord.ui.View):
    def __init__(self, game: GameState, timeout: Optional[float] = 600):
        super().__init__(timeout=timeout)
        self.game = game

    async def _ensure_child(self, inter: discord.Interaction) -> bool:
        uid = inter.user.id
        if self.game.phase != "betting":
            await inter.response.send_message("いまはベット受付時間ではありません。", ephemeral=True)
            return False
        if uid not in self.game.children_order:
            await inter.response.send_message("今回ラウンドの子ではありません。", ephemeral=True)
            return False
        return True

    async def _refresh_panel(self, inter: discord.Interaction):
        if self.game.bet_panel_message_id:
            try:
                msg = await inter.channel.fetch_message(self.game.bet_panel_message_id)
                await msg.edit(content=bet_panel_text(self.game), view=self)
            except Exception:
                pass

    async def _bump(self, inter: discord.Interaction, delta: int):
        if not await self._ensure_child(inter): return
        await inter.response.defer(ephemeral=True)
        uid = inter.user.id
        cur = self.game.temp_bets.get(uid, self.game.bets.get(uid, 0))
        cur = max(0, min(MAX_BET, cur + delta))
        self.game.temp_bets[uid] = cur
        await self._refresh_panel(inter)

    @discord.ui.button(label="+100", style=discord.ButtonStyle.success)
    async def plus_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._bump(inter, BET_STEP)

    @discord.ui.button(label="-100", style=discord.ButtonStyle.danger)
    async def minus_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._bump(inter, -BET_STEP)

    @discord.ui.button(label="クリア(0)", style=discord.ButtonStyle.secondary)
    async def clear_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_child(inter): return
        await inter.response.defer(ephemeral=True)
        uid = inter.user.id
        self.game.temp_bets[uid] = 0
        await self._refresh_panel(inter)

    @discord.ui.button(label="✅ 確定", style=discord.ButtonStyle.primary)
    async def confirm_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_child(inter): return
        await inter.response.defer(ephemeral=True)
        uid = inter.user.id
        amt = self.game.temp_bets.get(uid, self.game.bets.get(uid, 0))
        self.game.bets[uid] = amt
        self.game.temp_bets.pop(uid, None)
        await self._refresh_panel(inter)
        await inter.followup.send(f"あなたのベットを **{amt}** に確定しました。", ephemeral=True)

async def send_bet_panel(channel: discord.abc.Messageable, game: GameState):
    view = BetView(game)
    msg = await channel.send(bet_panel_text(game), view=view)
    game.bet_panel_message_id = msg.id

# ================== ROLL/STOP ビュー ==================
class RollView(discord.ui.View):
    def __init__(self, game: GameState, round_state: RoundState, is_parent: bool, timeout: Optional[float] = 120):
        super().__init__(timeout=timeout)
        self.game = game
        self.round_state = round_state
        self.is_parent = is_parent
        self.working = False

    async def _finalize_parent_and_move_on(self, channel: discord.abc.Messageable):
        hand = self.round_state.final
        assert hand is not None
        if hand.rank == 5:      # シゴロ → 親即勝：子→親
            transfers = [(cid, self.game.parent_id, self.game.bets.get(cid, 0)) for cid in self.game.children_order if self.game.bets.get(cid, 0) > 0]
            await post_transfers(channel, transfers, "🟢 親の即勝（シゴロ）精算")
            await end_round_and_rotate_parent(channel, self.game)
        elif hand.rank == 1:    # ヒフミ → 親即負：親→子
            transfers = [(self.game.parent_id, cid, self.game.bets.get(cid, 0)) for cid in self.game.children_order if self.game.bets.get(cid, 0) > 0]
            await post_transfers(channel, transfers, "🔴 親の即負（ヒフミ）精算")
            await end_round_and_rotate_parent(channel, self.game)
        elif hand.rank == 4:    # ゾロ目 → 親即勝：子→親
            transfers = [(cid, self.game.parent_id, self.game.bets.get(cid, 0)) for cid in self.game.children_order if self.game.bets.get(cid, 0) > 0]
            await post_transfers(channel, transfers, "🟢 親の即勝（ゾロ目）精算")
            await end_round_and_rotate_parent(channel, self.game)
        else:
            self.game.parent_hand = hand
            await start_children_turns(channel, self.game)

    @discord.ui.button(label="ROLL", style=discord.ButtonStyle.primary)
    async def roll_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if inter.user.id != self.round_state.user_id:
            await inter.response.send_message("あなたの手番ではありません。", ephemeral=True); return
        if self.working:
            await inter.response.send_message("処理中です。", ephemeral=True); return
        if self.round_state.final:
            await inter.response.send_message("すでに確定しています。", ephemeral=True); return
        if self.round_state.tries >= 3:
            await inter.response.send_message("ROLLしてください", ephemeral=True); return

        async with self.game.lock:
            self.working = True
            self.round_state.tries += 1

            await inter.response.defer()
            for c in self.children: c.disabled = True
            await inter.edit_original_response(view=self)

            # 画像アニメ → 実サイコロ確定 → 合成PNG → アニメ削除
            title = f"{self.round_state.role_label} {inter.user.mention} のロール中…"
            anim_msg, _, _ = await send_roll_animation(inter.channel, title=title)

            dice = roll_dice()
            self.round_state.last_roll = dice
            hand = evaluate_hand(dice)
            if hand.rank != 2 or self.round_state.tries >= 3:
                self.round_state.final = hand

            try:
                await anim_msg.edit(content=f"{title}\n（…止まりました）")
            except Exception:
                pass

            await send_final_composited_image(
                inter.channel,
                who_mention=inter.user.mention,
                role_label=self.round_state.role_label,
                dice=dice,
                hand_label=hand.label,
                tries=self.round_state.tries
            )

            if DELETE_ANIM_AFTER_RESULT:
                try: await anim_msg.delete()
                except Exception: pass

            if self.round_state.final:
                await inter.edit_original_response(view=None)
                if self.is_parent:
                    await self._finalize_parent_and_move_on(inter.channel)
                else:
                    await conclude_child_vs_parent(inter.channel, self.game, child_id=self.round_state.user_id, child_hand=hand)
            else:
                for c in self.children: c.disabled = False
                await inter.edit_original_response(view=self)

            self.working = False

    @discord.ui.button(label="STOP", style=discord.ButtonStyle.secondary)
    async def stop_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if inter.user.id != self.round_state.user_id:
            await inter.response.send_message("あなたの手番ではありません。", ephemeral=True); return
        if self.round_state.final:
            await inter.response.send_message("すでに確定しています。", ephemeral=True); return
        if not self.round_state.last_roll:
            await inter.response.send_message("まだ1回も振っていません。先にROLLしてください。", ephemeral=True); return

        async with self.game.lock:
            await inter.response.defer()
            hand = evaluate_hand(self.round_state.last_roll)
            self.round_state.final = hand

            await send_final_composited_image(
                inter.channel,
                who_mention=inter.user.mention,
                role_label=self.round_state.role_label,
                dice=self.round_state.last_roll,
                hand_label=f"{hand.label}（STOPで確定）",
                tries=self.round_state.tries
            )
            await inter.edit_original_response(view=None)

            if self.is_parent:
                await self._finalize_parent_and_move_on(inter.channel)
            else:
                await conclude_child_vs_parent(inter.channel, self.game, child_id=self.round_state.user_id, child_hand=hand)

# ================== 進行ユーティリティ ==================
async def start_children_turns(channel: discord.abc.Messageable, game: GameState):
    game.phase = "children_roll"
    game.turn_index = 0
    await channel.send(f"▶ 親の役：**{game.parent_hand}**。子のターンに入ります。")
    await prompt_next_child(channel, game)

async def prompt_next_child(channel: discord.abc.Messageable, game: GameState):
    if game.turn_index >= len(game.children_order):
        await end_round_and_rotate_parent(channel, game); return
    cid = game.children_order[game.turn_index]
    game.child_round = RoundState(user_id=cid, role_label="【子】")
    view = RollView(game, round_state=game.child_round, is_parent=False)
    await channel.send(f"🟦 子 <@{cid}> の手番です。役無しの場合は3回までROLL可能、STOPで確定。", view=view)

async def conclude_child_vs_parent(channel: discord.abc.Messageable, game: GameState, child_id: int, child_hand: HandResult):
    parent_hand = game.parent_hand
    assert parent_hand is not None
    bet = game.bets.get(child_id, 0)
    res = compare(parent_hand, child_hand)

    if res == 0:
        await channel.send(f"🔸 引き分け：親 **{parent_hand}** vs 子 **{child_hand}**（精算なし）")
    elif res > 0:
        await post_transfers(channel, [(game.parent_id, child_id, bet)], "🟢 子の勝ち 精算")
    else:
        await post_transfers(channel, [(child_id, game.parent_id, bet)], "🔴 子の負け 精算")

    game.turn_index += 1
    await prompt_next_child(channel, game)

async def end_round_and_rotate_parent(channel: discord.abc.Messageable, game: GameState):
    if not game.participants:
        await channel.send("参加者がいないため終了します。")
        GAMES.pop(game.channel_id, None)
        return
    candidates = [uid for uid in game.participants if uid != game.parent_id] or game.participants[:]
    next_parent = random.choice(candidates)
    await channel.send(f"✅ ラウンド終了。次の親はランダム選出 → <@{next_parent}>")

    game.parent_id = next_parent
    game.parent_hand = None
    game.children_order = [uid for uid in game.participants if uid != game.parent_id]
    game.bets = {}
    game.temp_bets = {}
    game.turn_index = 0
    game.parent_round = None
    game.child_round = None
    game.phase = "betting"
    await channel.send(f"▶ 新ラウンド開始。親：<@{game.parent_id}>。これからベットを設定してください。")
    await send_bet_panel(channel, game)

# ================== Slash Commands ==================
@tree.command(name="chi_ready", description="チンチロのロビーを作成（ボタンで参加）")
async def chi_ready(inter: discord.Interaction):
    await ack(inter)
    cid = inter.channel_id
    if cid in GAMES and GAMES[cid].lobby_open:
        await inter.followup.send("このチャンネルには既にロビーがあります。", ephemeral=True); return
    game = GameState(channel_id=cid, host_id=inter.user.id)
    GAMES[cid] = game
    view = LobbyView(game)
    msg = await inter.followup.send(lobby_text(game), view=view)
    game.lobby_message_id = (await inter.original_response()).id

@tree.command(name="chi_panel", description="（ホスト）参加パネルを再送")
async def chi_panel(inter: discord.Interaction):
    await ack(inter)
    cid = inter.channel_id
    game = GAMES.get(cid)
    if not game:
        await inter.followup.send("このチャンネルにロビー/ゲームはありません。", ephemeral=True); return
    if inter.user.id != game.host_id:
        await inter.followup.send("ホストのみ実行できます。", ephemeral=True); return
    view = LobbyView(game)
    msg = await inter.followup.send(lobby_text(game), view=view)
    game.lobby_message_id = (await inter.original_response()).id

@tree.command(name="chi_parent_roll", description="（親）ロールを開始（子のベット締切）")
async def chi_parent_roll(inter: discord.Interaction):
    await ack(inter)
    cid = inter.channel_id
    game = GAMES.get(cid)
    if not game or game.phase not in ("betting", "parent_roll"):
        await inter.followup.send("今は親のロールフェーズではありません。", ephemeral=True); return
    if inter.user.id != game.parent_id:
        await inter.followup.send("親のみが開始できます。", ephemeral=True); return

    game.phase = "parent_roll"

    # ベット締切：パネルを閉じる
    if game.bet_panel_message_id:
        try:
            panel_msg = await inter.channel.fetch_message(game.bet_panel_message_id)
            await panel_msg.edit(content="⛔ ベットは締め切りました。", view=None)
        except Exception:
            pass

    game.parent_round = RoundState(user_id=game.parent_id, role_label="【親】")
    view = RollView(game, round_state=game.parent_round, is_parent=True)
    await inter.followup.send(f"🟨 親 <@{game.parent_id}> の手番です。役無しの場合は3回までROLL可能、STOPで確定。", view=view)

@tree.command(name="chi_status", description="状態を表示")
async def chi_status(inter: discord.Interaction):
    await ack(inter)
    cid = inter.channel_id
    game = GAMES.get(cid)
    lines = []
    if game:
        lines.append(f"フェーズ：{game.phase}")
        lines.append(f"ホスト：<@{game.host_id}>")
        lines.append(f"参加者：{'、'.join(f'<@{u}>' for u in game.participants) if game.participants else '—'}")
        lines.append(f"親：{f'<@{game.parent_id}>' if game.parent_id else '—'}")
        if game.children_order:
            lines.append(f"子順：{' → '.join(f'<@{u}>' for u in game.children_order)}")
        if game.bets or game.temp_bets:
            lines.append("ベット（確定/暫定）：" + "、".join(
                f"<@{u}>:{game.bets.get(u, game.temp_bets.get(u, 0))}" for u in game.children_order
            ))
        if game.parent_hand:
            lines.append(f"親の役：{game.parent_hand}")
    else:
        lines.append("このチャンネルにゲームはありません。")
    await inter.followup.send("【状態】\n" + "\n".join(lines))

@tree.command(name="chi_end", description="ゲームを終了（ホストまたは親）")
async def chi_end(inter: discord.Interaction):
    await ack(inter)
    cid = inter.channel_id
    game = GAMES.get(cid)
    if not game:
        await inter.followup.send("ゲームはありません。", ephemeral=True); return
    if inter.user.id not in (game.host_id, game.parent_id):
        await inter.followup.send("終了権限がありません。", ephemeral=True); return
    GAMES.pop(cid, None)
    await inter.followup.send("🛑 ゲームを終了しました。")

# （任意）即時ギルド同期
if SYNC_ALLOWED_FOR_ADMINS:
    @tree.command(name="chi_sync", description="（管理者）このサーバーにSlashコマンドを即時同期")
    async def chi_sync(inter: discord.Interaction):
        if not inter.user.guild_permissions.administrator:
            await inter.response.send_message("管理者のみ実行できます。", ephemeral=True); return
        await inter.response.defer(ephemeral=True)
        guild = discord.Object(id=inter.guild_id)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        await inter.followup.send(f"✅ このサーバーに {len(synced)} 件のコマンドを同期しました。", ephemeral=True)

# ================== 起動 ==================
@bot.event
async def on_ready():
    try:
        await tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    print(f"✅ コマンド同期完了 / Bot {bot.user} 起動")

if __name__ == "__main__":
    if not TOKEN:
        print("環境変数 DISCORD_TOKEN が設定されていません。")
    else:
        bot.run(TOKEN)




