import os
import asyncio
import random
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# ====== 画像（Pillow） ======
from PIL import Image

# ================== 基本設定 ==================
TOKEN = os.getenv("DISCORD_TOKEN")
INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

# ================== 可変設定（ここだけ環境に合わせて） ==================
DICE_ASSET_DIR = "assets/dice"   # dice_1.png … dice_6.png を置くフォルダ
ROLL_ANIM_FRAMES = 12            # アニメのコマ数
ROLL_ANIM_MS = 90                # 1コマ表示ms（= 約11fps）
COMPOSITE_GAP = 16               # 合成PNGのサイコロ間隔(px)

# ▼サーバー通貨Botの送金コマンドテンプレ
#   {payer}={支払う側のメンション}, {payee}={受け取り側のメンション}, {amount}={金額(int)}
#   例) "!pay {payer} {payee} {amount}" / "vc!tip {payee} {amount}" などに変えてください
TRANSFER_TEMPLATE = "!pay {payer} {payee} {amount}"

# ================== ユーティリティ ==================
DICE_FACES = {1:"⚀",2:"⚁",3:"⚂",4:"⚃",5:"⚄",6:"⚅"}  # テキスト表示に使うだけ

def dice_face_str(vals: List[int]) -> str:
    return " ".join(DICE_FACES[v] for v in vals)

# 役
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

# ダイス
def roll_dice() -> List[int]:
    return [random.randint(1,6) for _ in range(3)]

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

def compose_three_dice_image(dice: list[int], gap: int = COMPOSITE_GAP) -> str:
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

def make_roll_animation(frames: int = ROLL_ANIM_FRAMES, duration_ms: int = ROLL_ANIM_MS, gap: int = COMPOSITE_GAP) -> tuple[str, list[int]]:
    """ WebP（失敗時GIF）アニメを作り、末尾見た目の出目も返す """
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

async def send_roll_animation(channel: discord.abc.Messageable, title: str) -> tuple[discord.Message, list[int], str]:
    anim_path, last_visual = make_roll_animation()
    msg = await channel.send(content=title, file=discord.File(anim_path, filename=os.path.basename(anim_path)))
    return msg, last_visual, anim_path

async def send_final_composited_image(channel, who_mention: str, role_label: str, dice: list[int], hand_label: str, tries: int):
    png_path = compose_three_dice_image(dice)
    text = f"{role_label} {who_mention} のロール #{tries}\n→ **{hand_label}**"
    await channel.send(content=text, file=discord.File(png_path, filename=os.path.basename(png_path)))

# ================== 精算（サーバー通貨コマンド出力） ==================
def build_transfer_line(payer_id: int, payee_id: int, amount: int) -> str:
    payer = f"<@{payer_id}>"
    payee = f"<@{payee_id}>"
    try:
        return TRANSFER_TEMPLATE.format(payer=payer, payee=payee, amount=amount)
    except KeyError:
        # テンプレが壊れていたら安全側で読みやすい行を返す
        return f"[TRANSFER] {payer} -> {payee} : {amount}"

async def post_transfers(channel: discord.abc.Messageable, pairs: List[Tuple[int,int,int]], title: str):
    """
    pairs: [(payer_id, payee_id, amount), ...]
    """
    if not pairs:
        await channel.send(f"{title}\n（対象なし）")
        return
    lines = [build_transfer_line(p, r, a) for (p, r, a) in pairs]
    await channel.send(f"{title}\n" + "\n".join(lines))

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
        self.bets: Dict[int, int] = {}  # 子 -> 金額（0可）

        self.turn_index = 0
        self.parent_hand: Optional[HandResult] = None
        self.phase: str = "lobby"  # lobby -> choose_parent -> betting -> parent_roll -> children_roll
        self.parent_round: Optional[RoundState] = None
        self.child_round: Optional[RoundState] = None
        self.lock = asyncio.Lock()

GAMES: Dict[int, GameState] = {}

# ================== ロビービュー ==================
def lobby_text(game: GameState) -> str:
    mems = "、".join(f"<@{u}>" for u in game.participants) if game.participants else "—"
    return (
        "🎲 **チンチロ ロビー**\n"
        f"ホスト：<@{game.host_id}>\n"
        f"参加者：{mems}\n\n"
        "Joinで参加、Leaveで退出。ホストは「親を決める」で開始します。"
    )

class LobbyView(discord.ui.View):
    def __init__(self, game: GameState, timeout: Optional[float] = 3600):
        super().__init__(timeout=timeout)
        self.game = game

    async def update_panel(self, message: discord.Message):
        await message.edit(content=lobby_text(self.game), view=self)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        if not game.lobby_open:
            await interaction.followup.send("ロビーは締め切られています。", ephemeral=True); return
        if uid in game.participants:
            await interaction.followup.send("すでに参加しています。", ephemeral=True); return
        game.participants.append(uid)
        await interaction.followup.send("参加しました。", ephemeral=True)
        await self.update_panel(interaction.message)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        if uid in game.participants:
            game.participants.remove(uid)
            await interaction.followup.send("退出しました。", ephemeral=True)
            await self.update_panel(interaction.message)
        else:
            await interaction.followup.send("参加していません。", ephemeral=True)

    @discord.ui.button(label="親を決める", style=discord.ButtonStyle.primary)
    async def decide_parent_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        await interaction.response.defer()
        if interaction.user.id != game.host_id:
            await interaction.followup.send("ホストのみが開始できます。", ephemeral=True); return
        if len(game.participants) < 2:
            await interaction.followup.send("参加者が2人以上必要です。", ephemeral=True); return
        if not game.lobby_open:
            await interaction.followup.send("すでに開始済みです。", ephemeral=True); return

        game.lobby_open = False
        game.phase = "choose_parent"
        await interaction.followup.send("▶ 親決めを開始します。全員が同時にロールします…")

        best_uid = None
        best_hand: Optional[HandResult] = None
        texts = []
        # 1人ずつアニメ表示（同時だと見づらいので順送り）
        for uid in game.participants:
            user = await bot.fetch_user(uid)
            anim_msg, _, _ = await send_roll_animation(interaction.channel, title=f"【親決め】{user.display_name} のロール中…")
            dice = roll_dice()
            hand = evaluate_hand(dice)
            await anim_msg.edit(content=f"【親決め】{user.display_name} のロール結果\n{dice_face_str(dice)} → **{hand}**")
            texts.append(f"<@{uid}>: {dice_face_str(dice)} → **{hand}**")
            if best_hand is None or compare(best_hand, hand) < 0:
                best_uid, best_hand = uid, hand

        await interaction.channel.send("結果：\n" + "\n".join(texts))
        game.parent_id = best_uid
        game.children_order = [uid for uid in game.participants if uid != best_uid]
        await interaction.channel.send(
            f"👑 親は <@{best_uid}> に決定！ 子は `/chi_bet amount:<金額>` でベットしてください。\n"
            "親は `/chi_parent_roll` で開始できます。"
        )
        game.phase = "betting"

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
        if hand.rank == 5:      # シゴロ → 親の即勝：子全員から徴収
            transfers = []
            for cid in self.game.children_order:
                amt = self.game.bets.get(cid, 0)
                if amt > 0:
                    transfers.append((cid, self.game.parent_id, amt))  # 子→親
            await post_transfers(channel, transfers, "🟢 親の即勝（シゴロ）精算")
            await end_round_and_rotate_parent(channel, self.game)
        elif hand.rank == 1:    # ヒフミ → 親の即負：親が子全員へ支払い
            transfers = []
            for cid in self.game.children_order:
                amt = self.game.bets.get(cid, 0)
                if amt > 0:
                    transfers.append((self.game.parent_id, cid, amt))  # 親→子
            await post_transfers(channel, transfers, "🔴 親の即負（ヒフミ）精算")
            await end_round_and_rotate_parent(channel, self.game)
        elif hand.rank == 4:    # ゾロ目 → 親の即勝
            transfers = []
            for cid in self.game.children_order:
                amt = self.game.bets.get(cid, 0)
                if amt > 0:
                    transfers.append((cid, self.game.parent_id, amt))
            await post_transfers(channel, transfers, "🟢 親の即勝（ゾロ目）精算")
            await end_round_and_rotate_parent(channel, self.game)
        else:
            # 通常役 → 子ターン
            self.game.parent_hand = hand
            await start_children_turns(channel, self.game)

    @discord.ui.button(label="ROLL", style=discord.ButtonStyle.primary)
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.round_state.user_id:
            await interaction.response.send_message("あなたの手番ではありません。", ephemeral=True); return
        if self.working:
            await interaction.response.send_message("処理中です。", ephemeral=True); return
        if self.round_state.final:
            await interaction.response.send_message("すでに確定しています。", ephemeral=True); return
        if self.round_state.tries >= 3:
            await interaction.response.send_message("最大3回までです。", ephemeral=True); return

        async with self.game.lock:
            self.working = True
            self.round_state.tries += 1

            # 1) defer
            await interaction.response.defer()
            # 2) ボタン無効化
            for c in self.children: c.disabled = True
            await interaction.edit_original_response(view=self)

            # 3) 画像アニメ
            title = f"{self.round_state.role_label} {interaction.user.mention} のロール中…"
            anim_msg, _, _ = await send_roll_animation(interaction.channel, title=title)

            # 4) 実サイコロ確定
            dice = roll_dice()
            self.round_state.last_roll = dice
            hand = evaluate_hand(dice)
            if hand.rank != 2 or self.round_state.tries >= 3:
                self.round_state.final = hand

            # 5) アニメのテキスト更新（止まった表示）
            await anim_msg.edit(content=f"{title}\n（…止まりました）")

            # 6) 合成PNGで確定
            await send_final_composited_image(
                interaction.channel,
                who_mention=interaction.user.mention,
                role_label=self.round_state.role_label,
                dice=dice,
                hand_label=hand.label,
                tries=self.round_state.tries
            )

            # 7) 分岐
            if self.round_state.final:
                await interaction.edit_original_response(view=None)
                if self.is_parent:
                    await self._finalize_parent_and_move_on(interaction.channel)
                else:
                    await conclude_child_vs_parent(interaction.channel, self.game, child_id=self.round_state.user_id, child_hand=hand)
            else:
                for c in self.children: c.disabled = False
                await interaction.edit_original_response(view=self)

            self.working = False

    @discord.ui.button(label="STOP", style=discord.ButtonStyle.secondary)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.round_state.user_id:
            await interaction.response.send_message("あなたの手番ではありません。", ephemeral=True); return
        if self.round_state.final:
            await interaction.response.send_message("すでに確定しています。", ephemeral=True); return
        if not self.round_state.last_roll:
            await interaction.response.send_message("まだ1回も振っていません。先にROLLしてください。", ephemeral=True); return

        async with self.game.lock:
            await interaction.response.defer()
            hand = evaluate_hand(self.round_state.last_roll)
            self.round_state.final = hand

            await send_final_composited_image(
                interaction.channel,
                who_mention=interaction.user.mention,
                role_label=self.round_state.role_label,
                dice=self.round_state.last_roll,
                hand_label=f"{hand.label}（STOPで確定）",
                tries=self.round_state.tries
            )
            await interaction.edit_original_response(view=None)

            if self.is_parent:
                await self._finalize_parent_and_move_on(interaction.channel)
            else:
                await conclude_child_vs_parent(interaction.channel, self.game, child_id=self.round_state.user_id, child_hand=hand)

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
    await channel.send(f"🟦 子 <@{cid}> の手番です。最大3回までROLL可能、STOPで確定。", view=view)

async def conclude_child_vs_parent(channel: discord.abc.Messageable, game: GameState, child_id: int, child_hand: HandResult):
    parent_hand = game.parent_hand
    assert parent_hand is not None
    bet = game.bets.get(child_id, 0)
    res = compare(parent_hand, child_hand)

    if res == 0:
        await channel.send(f"🔸 引き分け：親 **{parent_hand}** vs 子 **{child_hand}**（精算なし）")
    elif res > 0:
        # 子勝ち → 親 -> 子
        await post_transfers(channel, [(game.parent_id, child_id, bet)], "🟢 子の勝ち 精算")
    else:
        # 子負け → 子 -> 親
        await post_transfers(channel, [(child_id, game.parent_id, bet)], "🔴 子の負け 精算")

    game.turn_index += 1
    await prompt_next_child(channel, game)

async def end_round_and_rotate_parent(channel: discord.abc.Messageable, game: GameState):
    if not game.participants:
        await channel.send("参加者がいないため終了します。"); GAMES.pop(game.channel_id, None); return
    candidates = [uid for uid in game.participants if uid != game.parent_id] or game.participants[:]
    next_parent = random.choice(candidates)
    await channel.send(f"✅ ラウンド終了。次の親はランダム選出 → <@{next_parent}>")

    game.parent_id = next_parent
    game.parent_hand = None
    game.children_order = [uid for uid in game.participants if uid != game.parent_id]
    game.bets = {}
    game.turn_index = 0
    game.parent_round = None
    game.child_round = None
    game.phase = "betting"
    await channel.send(f"▶ 新ラウンド開始。親：<@{game.parent_id}>。子は `/chi_bet amount:<金額>` でベットしてください。親は `/chi_parent_roll` で開始できます。")

# ================== Slash Commands ==================
@tree.command(name="chi_ready", description="チンチロのロビーを作成（ボタンで参加）")
async def chi_ready(interaction: discord.Interaction):
    cid = interaction.channel_id
    if cid in GAMES and GAMES[cid].lobby_open:
        await interaction.response.send_message("このチャンネルには既にロビーがあります。", ephemeral=True); return
    game = GameState(channel_id=cid, host_id=interaction.user.id)
    GAMES[cid] = game
    view = LobbyView(game)
    await interaction.response.send_message(lobby_text(game), view=view)
    sent = await interaction.original_response()
    game.lobby_message_id = sent.id

@tree.command(name="chi_panel", description="（ホスト）参加パネルを再送")
async def chi_panel(interaction: discord.Interaction):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game:
        await interaction.response.send_message("このチャンネルにロビー/ゲームはありません。", ephemeral=True); return
    if interaction.user.id != game.host_id:
        await interaction.response.send_message("ホストのみ実行できます。", ephemeral=True); return
    view = LobbyView(game)
    await interaction.response.send_message(lobby_text(game), view=view)
    sent = await interaction.original_response()
    game.lobby_message_id = sent.id

@tree.command(name="chi_bet", description="（子）今回の親に対する賭け金を設定（整数・0可）")
@app_commands.describe(amount="ベット額（0可）")
async def chi_bet(interaction: discord.Interaction, amount: app_commands.Range[int, 0, 1_000_000]):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game or game.phase not in ("betting",):
        await interaction.response.send_message("現在はベットできません。", ephemeral=True); return
    if interaction.user.id == game.parent_id:
        await interaction.response.send_message("親はベットできません。", ephemeral=True); return
    if interaction.user.id not in game.participants:
        await interaction.response.send_message("今回のゲームの参加者ではありません。", ephemeral=True); return
    game.bets[interaction.user.id] = amount
    await interaction.response.send_message(f"✅ ベットを **{amount}** に設定しました。")

@tree.command(name="chi_parent_roll", description="（親）ロールを開始（子のベット締切）")
async def chi_parent_roll(interaction: discord.Interaction):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game or game.phase not in ("betting", "parent_roll"):
        await interaction.response.send_message("今は親のロールフェーズではありません。", ephemeral=True); return
    if interaction.user.id != game.parent_id:
        await interaction.response.send_message("親のみが開始できます。", ephemeral=True); return

    game.phase = "parent_roll"
    game.parent_round = RoundState(user_id=game.parent_id, role_label="【親】")
    view = RollView(game, round_state=game.parent_round, is_parent=True)
    await interaction.response.send_message(f"🟨 親 <@{game.parent_id}> の手番です。最大3回までROLL可能、STOPで確定。", view=view)

@tree.command(name="chi_status", description="状態を表示")
async def chi_status(interaction: discord.Interaction):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    lines = []
    if game:
        lines.append(f"フェーズ：{game.phase}")
        lines.append(f"ホスト：<@{game.host_id}>")
        lines.append(f"参加者：{'、'.join(f'<@{u}>' for u in game.participants) if game.participants else '—'}")
        lines.append(f"親：{f'<@{game.parent_id}>' if game.parent_id else '—'}")
        if game.children_order:
            lines.append(f"子順：{' → '.join(f'<@{u}>' for u in game.children_order)}")
        if game.bets:
            lines.append("ベット：" + "、".join(f"<@{u}>:{amt}" for u, amt in game.bets.items()))
        if game.parent_hand:
            lines.append(f"親の役：{game.parent_hand}")
    else:
        lines.append("このチャンネルにゲームはありません。")
    await interaction.response.send_message("【状態】\n" + "\n".join(lines))

@tree.command(name="chi_end", description="ゲームを終了（ホストまたは親）")
async def chi_end(interaction: discord.Interaction):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game:
        await interaction.response.send_message("ゲームはありません。", ephemeral=True); return
    if interaction.user.id not in (game.host_id, game.parent_id):
        await interaction.response.send_message("終了権限がありません。", ephemeral=True); return
    GAMES.pop(cid, None)
    await interaction.response.send_message("🛑 ゲームを終了しました。")

# ================== 起動 ==================
@bot.event
async def on_ready():
    try:
        await tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    print(f"Bot connected as {bot.user}")

if __name__ == "__main__":
    if not TOKEN:
        print("環境変数 DISCORD_TOKEN が設定されていません。")
    else:
        bot.run(TOKEN)
