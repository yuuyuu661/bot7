import os
import json
import asyncio
import random
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# ================== 基本設定 ==================
TOKEN = os.getenv("DISCORD_TOKEN")
INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

DB_PATH = "chinchiro_kv.json"
_db_lock = asyncio.Lock()

ADMIN_USER_IDS = {440893662701027328, 716667546241335328}

# ダイス絵文字（アニメ用）
DICE_FACES = {1:"⚀",2:"⚁",3:"⚂",4:"⚃",5:"⚄",6:"⚅"}
def dice_face_str(vals: List[int]) -> str:
    return " ".join(DICE_FACES[v] for v in vals)

# ================== KV（残高など） ==================
def _kv_load() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _kv_save(data: dict):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)

# ================== 役判定 ==================
class HandResult:
    """
    rank: 大きいほど強い
      5: シゴロ(4-5-6)
      4: ゾロ目（1〜6） 6ゾロが最強
      3: 目（1〜6）      目の値で比較
      2: 役なし          （3回振っても役なしなら負け）
      1: ヒフミ(1-2-3)
    """
    def __init__(self, rank: int, value: int, label: str):
        self.rank = rank
        self.value = value
        self.label = label

    def __str__(self):
        return self.label

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

# ================== 状態管理 ==================
class PlayerState:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.balance = 1000

class RoundState:
    def __init__(self, user_id: int, role_label: str):
        self.user_id = user_id
        self.role_label = role_label  # "【親】" or "【子】"
        self.tries = 0
        self.last_roll: Optional[List[int]] = None
        self.final: Optional[HandResult] = None

class GameState:
    def __init__(self, channel_id: int, host_id: int):
        self.channel_id = channel_id
        self.host_id = host_id         # ロビー作成者
        self.lobby_open = True
        self.lobby_message_id: Optional[int] = None

        self.participants: List[int] = []    # 参加者（親決め対象）
        self.parent_id: Optional[int] = None  # 現親
        self.bets: Dict[int, int] = {}        # 子 -> ベット額
        self.children_order: List[int] = []   # 今ラウンドの子順
        self.turn_index = 0
        self.parent_hand: Optional[HandResult] = None
        self.phase: str = "lobby"             # lobby -> choose_parent -> betting -> parent_roll -> children_roll
        self.parent_round: Optional[RoundState] = None
        self.child_round: Optional[RoundState] = None
        self.current_view: Optional[discord.ui.View] = None
        self.lock = asyncio.Lock()

# チャンネルごとのゲーム
GAMES: Dict[int, GameState] = {}
# 残高
PLAYERS: Dict[int, PlayerState] = {}

def get_player_state(uid: int) -> PlayerState:
    ps = PLAYERS.get(uid)
    if not ps:
        ps = PlayerState(uid)
        PLAYERS[uid] = ps
    return ps

async def save_balances():
    async with _db_lock:
        data = _kv_load()
        data["balances"] = {str(uid): ps.balance for uid, ps in PLAYERS.items()}
        _kv_save(data)

def load_balances():
    data = _kv_load()
    balances = data.get("balances", {})
    for k, v in balances.items():
        try:
            uid = int(k)
            ps = get_player_state(uid)
            ps.balance = int(v)
        except Exception:
            continue

# ================== 表示ユーティリティ ==================
def lobby_text(game: GameState) -> str:
    mems = "、".join(f"<@{u}>" for u in game.participants) if game.participants else "—"
    return (
        "🎲 **チンチロ ロビー**\n"
        f"ホスト：<@{game.host_id}>\n"
        f"参加者：{mems}\n\n"
        "Joinで参加、Leaveで退出。ホストは「親を決める」で開始します。"
    )

def build_dice_files(dice: List[int]) -> List[discord.File]:
    files = []
    for i, n in enumerate(dice, start=1):
        path = f"assets/dice/dice_{n}.png"
        files.append(discord.File(path, filename=f"dice_{i}_{n}.png"))
    return files

async def send_dice_result_with_images(channel, who_mention: str, role_label: str, dice: List[int], hand_label: str, tries: int):
    text = f"{role_label} {who_mention} のロール #{tries}\n→ **{hand_label}**"
    await channel.send(content=text, files=build_dice_files(dice))

# ================== アニメ演出 ==================
async def animate_roll(channel: discord.abc.Messageable, title: str, frames: int = 8, interval: float = 0.15) -> Tuple[discord.Message, List[int]]:
    cur = [random.randint(1,6) for _ in range(3)]
    msg = await channel.send(f"{title}\n{dice_face_str(cur)}")
    for _ in range(frames-1):
        await asyncio.sleep(interval)
        cur = [random.randint(1,6) for _ in range(3)]
        await msg.edit(content=f"{title}\n{dice_face_str(cur)}")
    await asyncio.sleep(interval)
    cur = [random.randint(1,6) for _ in range(3)]
    await msg.edit(content=f"{title}\n{dice_face_str(cur)}")
    return msg, cur

def roll_dice() -> List[int]:
    return [random.randint(1,6) for _ in range(3)]

# ================== ロビービュー（参加ボタン） ==================
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
            await interaction.followup.send("ロビーは締め切られています。", ephemeral=True)
            return
        if uid in game.participants:
            await interaction.followup.send("すでに参加しています。", ephemeral=True)
            return
        game.participants.append(uid)
        get_player_state(uid)
        await save_balances()
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
        if interaction.user.id not in (game.host_id, *ADMIN_USER_IDS):
            await interaction.followup.send("ホストのみが開始できます。", ephemeral=True)
            return
        if not game.participants or len(game.participants) < 2:
            await interaction.followup.send("参加者が2人以上必要です。", ephemeral=True)
            return
        if not game.lobby_open:
            await interaction.followup.send("すでに開始済みです。", ephemeral=True)
            return

        # 締切
        game.lobby_open = False
        game.phase = "choose_parent"
        await interaction.followup.send("▶ 親決めを開始します。全員が同時にロールします…")

        best_uid = None
        best_hand: Optional[HandResult] = None
        texts = []
        for uid in game.participants:
            user = await bot.fetch_user(uid)
            _, dice = await animate_roll(interaction.channel, title=f"【親決め】{user.display_name} のロール中…", frames=6, interval=0.12)
            hand = evaluate_hand(dice)
            texts.append(f"<@{uid}>: {dice_face_str(dice)} → **{hand}**")
            if best_hand is None:
                best_uid, best_hand = uid, hand
            else:
                cmp = compare(best_hand, hand)
                if cmp < 0:
                    best_uid, best_hand = uid, hand

        await interaction.channel.send("結果：\n" + "\n".join(texts))
        game.parent_id = best_uid
        game.children_order = [uid for uid in game.participants if uid != best_uid]
        await interaction.channel.send(
            f"👑 親は <@{best_uid}> に決定！ 子は `/chi_bet amount:<金額>` でベットしてください。"
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
        if hand.rank == 5:      # シゴロ
            await conclude_parent_auto_win(channel, self.game, reason="シゴロ")
        elif hand.rank == 1:    # ヒフミ
            await conclude_parent_auto_loss(channel, self.game, reason="ヒフミ")
        elif hand.rank == 4:    # ゾロ目
            await conclude_parent_auto_win(channel, self.game, reason="ゾロ目")
        else:
            self.game.parent_hand = hand
            await start_children_turns(channel, self.game)

    @discord.ui.button(label="ROLL", style=discord.ButtonStyle.primary)
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.round_state.user_id:
            await interaction.response.send_message("あなたの手番ではありません。", ephemeral=True)
            return
        if self.working:
            await interaction.response.send_message("処理中です。", ephemeral=True)
            return
        if self.round_state.final:
            await interaction.response.send_message("すでに確定しています。", ephemeral=True)
            return
        if self.round_state.tries >= 3:
            await interaction.response.send_message("最大3回までです。", ephemeral=True)
            return

        async with self.game.lock:
            self.working = True
            self.round_state.tries += 1

            # 1) 最初に defer
            await interaction.response.defer()

            # 2) ボタン無効化
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(view=self)

            # 3) 演出（テキストアニメを followup メッセージで）
            title = f"{self.round_state.role_label} {interaction.user.mention} のロール中…"
            anim_msg, _ = await animate_roll(interaction.channel, title=title, frames=8, interval=0.15)

            # 4) 実サイコロ確定
            dice = roll_dice()
            self.round_state.last_roll = dice
            hand = evaluate_hand(dice)
            if hand.rank != 2 or self.round_state.tries >= 3:
                self.round_state.final = hand

            # 5) アニメの最終テキスト更新
            base = f"{self.round_state.role_label} {interaction.user.mention} のロール #{self.round_state.tries}\n{dice_face_str(dice)} → **{hand.label}**"
            if self.round_state.final and hand.rank == 2:
                base += "（役なし確定）"
            await anim_msg.edit(content=base)

            # 6) 画像3枚で確定結果を別送
            await send_dice_result_with_images(
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
                for child in self.children:
                    child.disabled = False
                await interaction.edit_original_response(view=self)

            self.working = False

    @discord.ui.button(label="STOP", style=discord.ButtonStyle.secondary)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.round_state.user_id:
            await interaction.response.send_message("あなたの手番ではありません。", ephemeral=True)
            return
        if self.round_state.final:
            await interaction.response.send_message("すでに確定しています。", ephemeral=True)
            return
        if not self.round_state.last_roll:
            await interaction.response.send_message("まだ1回も振っていません。先にROLLしてください。", ephemeral=True)
            return

        async with self.game.lock:
            await interaction.response.defer()
            hand = evaluate_hand(self.round_state.last_roll)
            self.round_state.final = hand

            await send_dice_result_with_images(
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
async def conclude_parent_auto_win(channel: discord.abc.Messageable, game: GameState, reason: str):
    await channel.send(f"🟢 親の**即勝**（{reason}）！ 子全員のベット分を親が受け取ります。")
    parent_ps = get_player_state(game.parent_id)
    for cid in game.children_order:
        amt = game.bets.get(cid, 0)
        if amt > 0:
            get_player_state(cid).balance -= amt
            parent_ps.balance += amt
    await save_balances()
    await end_round_and_rotate_parent(channel, game)

async def conclude_parent_auto_loss(channel: discord.abc.Messageable, game: GameState, reason: str):
    await channel.send(f"🔴 親の**即負**（{reason}）… 子全員にベット分を支払います。")
    parent_ps = get_player_state(game.parent_id)
    for cid in game.children_order:
        amt = game.bets.get(cid, 0)
        if amt > 0:
            parent_ps.balance -= amt
            get_player_state(cid).balance += amt
    await save_balances()
    await end_round_and_rotate_parent(channel, game)

async def start_children_turns(channel: discord.abc.Messageable, game: GameState):
    game.phase = "children_roll"
    game.turn_index = 0
    await channel.send(f"▶ 親の役：**{game.parent_hand}**。子のターンに入ります。")
    await prompt_next_child(channel, game)

async def prompt_next_child(channel: discord.abc.Messageable, game: GameState):
    if game.turn_index >= len(game.children_order):
        await end_round_and_rotate_parent(channel, game)
        return
    cid = game.children_order[game.turn_index]
    game.child_round = RoundState(user_id=cid, role_label="【子】")
    view = RollView(game, round_state=game.child_round, is_parent=False)
    game.current_view = view
    await channel.send(f"🟦 子 <@{cid}> の手番です。最大3回までROLL可能、STOPで確定。", view=view)

async def conclude_child_vs_parent(channel: discord.abc.Messageable, game: GameState, child_id: int, child_hand: HandResult):
    parent_hand = game.parent_hand
    assert parent_hand is not None
    parent_ps = get_player_state(game.parent_id)
    child_ps = get_player_state(child_id)
    bet = game.bets.get(child_id, 0)

    res = compare(parent_hand, child_hand)
    if res == 0:
        await channel.send(f"🔸 引き分け：親 **{parent_hand}** vs 子 **{child_hand}**（精算なし）")
    elif res > 0:
        child_ps.balance += bet
        parent_ps.balance -= bet
        await channel.send(f"🟢 子 <@{child_id}> の勝ち！ 親 **{parent_hand}** / 子 **{child_hand}** → 子 +{bet}")
        await save_balances()
    else:
        child_ps.balance -= bet
        parent_ps.balance += bet
        await channel.send(f"🔴 子 <@{child_id}> の負け… 親 **{parent_hand}** / 子 **{child_hand}** → 子 -{bet}")
        await save_balances()

    game.turn_index += 1
    await prompt_next_child(channel, game)

async def end_round_and_rotate_parent(channel: discord.abc.Messageable, game: GameState):
    if not game.participants:
        await channel.send("参加者がいないため終了します。")
        GAMES.pop(game.channel_id, None)
        return
    candidates = [uid for uid in game.participants if uid != game.parent_id]
    if not candidates:
        candidates = game.participants[:]
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
        await interaction.response.send_message("このチャンネルには既にロビーがあります。", ephemeral=True)
        return

    game = GameState(channel_id=cid, host_id=interaction.user.id)
    GAMES[cid] = game

    view = LobbyView(game)
    await interaction.response.send_message(lobby_text(game), view=view)
    sent = await interaction.original_response()
    game.lobby_message_id = sent.id
    game.current_view = view

@tree.command(name="chi_panel", description="（ホスト）参加パネルを再送")
async def chi_panel(interaction: discord.Interaction):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game:
        await interaction.response.send_message("このチャンネルにロビー/ゲームはありません。", ephemeral=True)
        return
    if interaction.user.id not in (game.host_id, *ADMIN_USER_IDS):
        await interaction.response.send_message("ホストのみ実行できます。", ephemeral=True)
        return
    view = LobbyView(game)
    await interaction.response.send_message(lobby_text(game), view=view)
    sent = await interaction.original_response()
    game.lobby_message_id = sent.id
    game.current_view = view

@tree.command(name="chi_bet", description="（子）今回の親に対する賭け金を設定（親のロール開始まで）")
@app_commands.describe(amount="ベット額（0可）")
async def chi_bet(interaction: discord.Interaction, amount: app_commands.Range[int, 0, 1_000_000]):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game or game.phase not in ("betting",):
        await interaction.response.send_message("現在はベットできません。", ephemeral=True)
        return
    if interaction.user.id == game.parent_id:
        await interaction.response.send_message("親はベットできません。", ephemeral=True)
        return
    if interaction.user.id not in game.participants:
        await interaction.response.send_message("今回のゲームの参加者ではありません。", ephemeral=True)
        return
    ps = get_player_state(interaction.user.id)
    if ps.balance < amount:
        await interaction.response.send_message(f"残高不足です。現在残高：{ps.balance}", ephemeral=True)
        return
    game.bets[interaction.user.id] = amount
    await interaction.response.send_message(f"✅ ベットを **{amount}** に設定しました。")

@tree.command(name="chi_parent_roll", description="（親）ロールを開始（子のベット締切）")
async def chi_parent_roll(interaction: discord.Interaction):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game or game.phase not in ("betting", "parent_roll"):
        await interaction.response.send_message("今は親のロールフェーズではありません。", ephemeral=True)
        return
    if interaction.user.id != game.parent_id:
        await interaction.response.send_message("親のみが開始できます。", ephemeral=True)
        return

    game.phase = "parent_roll"
    game.parent_round = RoundState(user_id=game.parent_id, role_label="【親】")
    view = RollView(game, round_state=game.parent_round, is_parent=True)
    await interaction.response.send_message(f"🟨 親 <@{game.parent_id}> の手番です。最大3回までROLL可能、STOPで確定。", view=view)

@tree.command(name="chi_status", description="状態と残高を表示")
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

    balances = []
    ids = set(game.participants) if game else {interaction.user.id}
    for uid in ids:
        balances.append(f"<@{uid}>: {get_player_state(uid).balance}")

    await interaction.response.send_message("【状態】\n" + "\n".join(lines) + "\n\n【残高】\n" + "\n".join(balances))

@tree.command(name="chi_end", description="ゲームを終了（親/ホスト/管理者）")
async def chi_end(interaction: discord.Interaction):
    cid = interaction.channel_id
    game = GAMES.get(cid)
    if not game:
        await interaction.response.send_message("ゲームはありません。", ephemeral=True)
        return
    if interaction.user.id not in (game.parent_id, game.host_id, *ADMIN_USER_IDS):
        await interaction.response.send_message("終了権限がありません。", ephemeral=True)
        return
    GAMES.pop(cid, None)
    await interaction.response.send_message("🛑 ゲームを終了しました。")

@tree.command(name="chi_add_balance", description="残高を加算（管理者）")
@app_commands.describe(user="対象ユーザー", amount="加算額（負数で減算）")
async def chi_add_balance(interaction: discord.Interaction, user: discord.User, amount: int):
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message("このコマンドは管理者のみ使用可能です。", ephemeral=True)
        return
    ps = get_player_state(user.id)
    ps.balance += amount
    await save_balances()
    await interaction.response.send_message(f"✅ {user.mention} の残高を {amount:+}。現在：{ps.balance}")

# ================== 起動 ==================
@bot.event
async def on_ready():
    load_balances()
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
