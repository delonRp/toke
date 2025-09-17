# -*- coding: utf-8 -*-
"""
Bot Discord untuk Manajemen Token via GitHub API
Disesuaikan untuk deployment di Railway dengan Environment Variables.
"""

import discord
from discord import app_commands, ui
from discord.ext import tasks, commands
import os
import requests
import base64
import json
from datetime import datetime, timedelta, timezone
import secrets
import string
import asyncio

# --- KONFIGURASI DARI ENVIRONMENT VARIABLES ---
# Variabel ini harus diatur di environment Railway Anda.
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
# Format: "owner/repo_name", contoh: "delonRp/my-private-data"
PRIMARY_REPO = os.environ.get('PRIMARY_REPO') 
# Pisahkan ID dengan koma jika lebih dari satu, contoh: "1042742521022926970,987654321098765432"
ALLOWED_GUILD_IDS_STR = os.environ.get('ALLOWED_GUILD_IDS', '')
# Ambil dari environment, default ke 0 jika tidak ada
CLAIM_CHANNEL_ID = int(os.environ.get('CLAIM_CHANNEL_ID', 0))
ROLE_REQUEST_CHANNEL_ID = int(os.environ.get('ROLE_REQUEST_CHANNEL_ID', 0))

# Validasi variabel penting
if not all([DISCORD_TOKEN, GITHUB_TOKEN, PRIMARY_REPO, ALLOWED_GUILD_IDS_STR]):
    print("FATAL ERROR: Pastikan variabel DISCORD_TOKEN, GITHUB_TOKEN, PRIMARY_REPO, dan ALLOWED_GUILD_IDS telah diatur di environment Railway.")
    exit()

# Konversi ALLOWED_GUILD_IDS dari string ke set of integers
try:
    ALLOWED_GUILD_IDS = {int(gid.strip()) for gid in ALLOWED_GUILD_IDS_STR.split(',')}
except ValueError:
    print("FATAL ERROR: Format ALLOWED_GUILD_IDS tidak valid. Harap gunakan angka yang dipisahkan koma.")
    exit()

# --- PATH FILE DI REPOSITORY GITHUB ---
TOKENS_FILE_PATH = 'tokens.txt'
CLAIMS_FILE_PATH = 'claims.json'

# --- KONFIGURASI ROLE (TETAP) ---
# Bisa juga dipindahkan ke environment variables jika ingin lebih dinamis
ROLE_DURATIONS = {
    "vip": "30d", "supporter": "10d", "inner circle": "7d",
    "subscriber": "5d", "followers": "5d", "beginner": "3d"
}
ROLE_PRIORITY = ["vip", "supporter", "inner circle", "subscriber", "followers", "beginner"]

# --- NAMA ROLE UNTUK FITUR OTOMATIS (TETAP) ---
SUBSCRIBER_ROLE_NAME = "Subscriber"
FOLLOWER_ROLE_NAME = "Followers"
FORGE_VERIFIED_ROLE_NAME = "Inner Circle"

# --- SETUP BOT ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!unusedprefix!", intents=intents, help_command=None)

# --- FUNGSI BANTUAN GITHUB API ---
# Fungsi ini sekarang menerima argumen repo_slug untuk mendukung banyak repo
def get_github_file(repo_slug, file_path):
    """Mengambil konten dan SHA dari file di repo GitHub."""
    url = f"https://api.github.com/repos/{repo_slug}/contents/{file_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            content = base64.b64decode(data['content']).decode('utf-8')
            return content, data['sha']
        elif response.status_code == 404:
            # File tidak ada, kembalikan state kosong yang valid
            return "{}", None
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error saat mengambil file dari GitHub: {e}")
        return None, None

def update_github_file(repo_slug, file_path, new_content, sha, commit_message):
    """Membuat atau memperbarui file di repo GitHub."""
    url = f"https://api.github.com/repos/{repo_slug}/contents/{file_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": encoded_content}
    if sha:
        data["sha"] = sha
    try:
        response = requests.put(url, headers=headers, json=data)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error saat memperbarui file di GitHub: {e}")


def parse_duration(duration_str: str) -> timedelta:
    """Mengubah string durasi (e.g., '30d') menjadi objek timedelta."""
    try:
        unit = duration_str[-1].lower()
        value = int(duration_str[:-1])
        if unit == 'd': return timedelta(days=value)
        if unit == 'h': return timedelta(hours=value)
        if unit == 'm': return timedelta(minutes=value)
        if unit == 's': return timedelta(seconds=value)
    except (ValueError, IndexError):
        raise ValueError("Format durasi tidak valid. Gunakan format seperti '30d', '12h', '5m'.")
    raise ValueError(f"Unit durasi tidak dikenal: {unit}")

def generate_random_token(role_name: str) -> str:
    """Membuat token acak yang unik berdasarkan nama role."""
    random_part = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    date_part = datetime.now(timezone.utc).strftime('%Y%m%d')
    return f"{role_name.upper().replace(' ', '')}-{random_part}-{date_part}"

# --- KELAS PANEL INTERAKTIF ---
class ClaimPanelView(ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance

    @ui.button(label="Claim Token", style=discord.ButtonStyle.success, custom_id="claim_token_button")
    async def claim_button_callback(self, interaction: discord.Interaction, button: ui.Button):
        if not self.bot.claim_session_open:
            await interaction.response.send_message("❌ Sesi klaim saat ini sedang ditutup oleh admin.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        user_id = str(user.id)
        current_time = datetime.now(timezone.utc)

        async with self.bot.github_lock:
            claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
            if claims_content is None:
                await interaction.followup.send("Gagal terhubung ke database. Coba lagi nanti.", ephemeral=True)
                return
            
            claims_data = json.loads(claims_content)

            # Logika cooldown dan token aktif
            if user_id in claims_data:
                user_claim_info = claims_data[user_id]
                last_claim_time = datetime.fromisoformat(user_claim_info['last_claim_timestamp'])
                if current_time < last_claim_time + timedelta(days=7):
                    next_claim_time = last_claim_time + timedelta(days=7)
                    await interaction.followup.send(f"❌ **Cooldown!** Anda baru bisa klaim lagi pada {next_claim_time.strftime('%d %B %Y, %H:%M')} UTC.", ephemeral=True)
                    return
                
                if 'current_token' in user_claim_info:
                    token_expiry_time = datetime.fromisoformat(user_claim_info['token_expiry_timestamp'])
                    if current_time < token_expiry_time:
                        await interaction.followup.send(f"❌ Token Anda masih aktif hingga {token_expiry_time.strftime('%d %B %Y, %H:%M')} UTC.", ephemeral=True)
                        return
            
            # Tentukan role untuk klaim
            user_role_names = [role.name.lower() for role in user.roles]
            claim_role = next((role for role in ROLE_PRIORITY if role in user_role_names), None)

            if not claim_role:
                await interaction.followup.send("❌ Anda tidak memiliki peran yang valid untuk klaim token.", ephemeral=True)
                return
            
            # Proses pembuatan dan penyimpanan token
            duration_str = ROLE_DURATIONS[claim_role]
            duration_delta = parse_duration(duration_str)
            new_token = generate_random_token(claim_role)
            
            tokens_content, tokens_sha = get_github_file(PRIMARY_REPO, TOKENS_FILE_PATH)
            if tokens_content is None: tokens_content = ""
            
            existing_tokens = [t for t in tokens_content.split('\n\n') if t.strip()]
            existing_tokens.append(new_token)
            new_tokens_content = "\n\n".join(existing_tokens) + "\n\n"
            update_github_file(PRIMARY_REPO, TOKENS_FILE_PATH, new_tokens_content, tokens_sha, f"Bot: Add token for {user.name}")
            
            claims_data[user_id] = {
                "last_claim_timestamp": current_time.isoformat(), 
                "current_token": new_token, 
                "token_expiry_timestamp": (current_time + duration_delta).isoformat()
            }
            new_claims_content = json.dumps(claims_data, indent=4)
            update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, new_claims_content, claims_sha, f"Bot: Update claim for {user.name}")

        try:
            await user.send(f"🎉 **Token Anda Berhasil Diklaim!**\n\nToken Anda: `{new_token}`\nRole: **{claim_role.title()}**\nAktif selama **{duration_str.replace('d', ' hari')}**.\n\nCatatan: Tunggu beberapa menit agar token dapat digunakan. Jika token tidak berhasil, silakan hubungi admin.")
            await interaction.followup.send("✅ **Berhasil!** Token Anda telah dikirim melalui DM.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("⚠️ Gagal mengirim DM. Pastikan DM Anda terbuka untuk bot ini. Token Anda tetap dibuat.", ephemeral=True)

    @ui.button(label="Cek Token Saya", style=discord.ButtonStyle.secondary, custom_id="check_token_button")
    async def check_button_callback(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        
        async with self.bot.github_lock:
            claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
            if claims_content is None:
                await interaction.followup.send("Gagal terhubung ke database. Coba lagi nanti.", ephemeral=True)
                return
            claims_data = json.loads(claims_content)

        if user_id not in claims_data:
            await interaction.followup.send("Anda belum pernah melakukan klaim token.", ephemeral=True)
            return
        
        user_data = claims_data[user_id]
        token = user_data.get("current_token", "Tidak ada")
        embed = discord.Embed(title="📄 Detail Token Anda", color=discord.Color.blue())
        embed.add_field(name="Token Aktif", value=f"`{token}`", inline=False)
        
        if "token_expiry_timestamp" in user_data:
            expiry_time = datetime.fromisoformat(user_data["token_expiry_timestamp"])
            if expiry_time > datetime.now(timezone.utc):
                embed.add_field(name="Akan Kedaluwarsa Pada", value=f"{expiry_time.strftime('%d %B %Y, %H:%M')} UTC", inline=False)
            else:
                 embed.description = "Token Anda saat ini tidak aktif (sudah kedaluwarsa)."

        last_claim_time = datetime.fromisoformat(user_data["last_claim_timestamp"])
        next_claim_time = last_claim_time + timedelta(days=7)
        if datetime.now(timezone.utc) < next_claim_time:
             embed.add_field(name="Cooldown Klaim", value=f"Anda bisa klaim lagi pada {next_claim_time.strftime('%d %B %Y, %H:%M')} UTC", inline=False)
        else:
             embed.add_field(name="Cooldown Klaim", value="Anda sudah bisa melakukan klaim token baru.", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

# --- PERINTAH SLASH COMMAND ---

@bot.tree.command(name="help", description="Menampilkan daftar semua perintah yang tersedia.")
async def help_command(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.administrator
    embed = discord.Embed(title="📜 Daftar Perintah Bot", color=discord.Color.gold())
    embed.description = "Berikut adalah perintah yang bisa Anda gunakan."
    embed.add_field(name="</help:0>", value="Menampilkan pesan bantuan ini.", inline=False)
    
    if is_admin:
        embed.add_field(name="👑 Perintah Admin", value=(
            "**/show_config**: Menampilkan channel yang terkonfigurasi.\n"
            "**/open_claim**: Membuka sesi klaim.\n"
            "**/close_claim**: Menutup sesi klaim.\n"
            "**/list_tokens**: Menampilkan daftar semua token aktif.\n"
            "**/admin_cek_user**: Memeriksa status token dan cooldown pengguna.\n"
            "**/admin_add_token**: Menambahkan token custom.\n"
            "**/admin_remove_token**: Menghapus token apapun.\n"
            "**/admin_add_test_token**: Menambah token tes.\n"
            "**/admin_reset_cooldown**: Mereset cooldown klaim pengguna.\n"
            "**/serverlist**: Menampilkan daftar server bot."
        ), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- PERINTAH ADMIN ---
@bot.tree.command(name="show_config", description="ADMIN: Menampilkan channel yang diatur di environment.")
@app_commands.checks.has_permissions(administrator=True)
async def show_config(interaction: discord.Interaction):
    embed = discord.Embed(title="🔧 Konfigurasi Channel Bot", color=discord.Color.teal())
    claim_ch_mention = f"<#{CLAIM_CHANNEL_ID}>" if CLAIM_CHANNEL_ID else "Belum diatur"
    role_req_ch_mention = f"<#{ROLE_REQUEST_CHANNEL_ID}>" if ROLE_REQUEST_CHANNEL_ID else "Belum diatur"
    
    embed.add_field(name="Channel Klaim Token", value=claim_ch_mention, inline=False)
    embed.add_field(name="Channel Request Role", value=role_req_ch_mention, inline=False)
    embed.set_footer(text="Pengaturan ini dikelola melalui Environment Variables di Railway.")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="open_claim", description="ADMIN: Membuka sesi dan mengirim panel klaim.")
@app_commands.checks.has_permissions(administrator=True)
async def open_claim(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not CLAIM_CHANNEL_ID:
        await interaction.followup.send("❌ Channel klaim belum diatur di environment `CLAIM_CHANNEL_ID`.", ephemeral=True); return
    
    claim_channel = bot.get_channel(CLAIM_CHANNEL_ID)
    if not claim_channel:
        await interaction.followup.send(f"❌ Channel dengan ID `{CLAIM_CHANNEL_ID}` tidak ditemukan.", ephemeral=True); return
    
    bot.claim_session_open = True
    embed = discord.Embed(title="📝 Sesi Klaim Token Dibuka!", description="Sesi klaim token telah dibuka. Gunakan tombol di bawah.", color=discord.Color.green())
    view = ClaimPanelView(bot)
    await claim_channel.send(embed=embed, view=view)
    await interaction.followup.send(f"✅ Panel klaim telah dikirim ke {claim_channel.mention} dan sesi telah **DIBUKA**.", ephemeral=True)

@bot.tree.command(name="close_claim", description="ADMIN: Menutup sesi klaim dan mengirim notifikasi.")
@app_commands.checks.has_permissions(administrator=True)
async def close_claim(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    bot.claim_session_open = False
    
    if CLAIM_CHANNEL_ID:
        claim_channel = bot.get_channel(CLAIM_CHANNEL_ID)
        if claim_channel:
            embed = discord.Embed(title="🔴 Sesi Klaim Token Ditutup", description="Admin telah menutup sesi klaim. Tombol klaim tidak akan berfungsi hingga sesi dibuka kembali.", color=discord.Color.red())
            await claim_channel.send(embed=embed)
            
    await interaction.followup.send("🔴 Sesi klaim token telah **DITUTUP** dan notifikasi telah dikirim.", ephemeral=True)

# ... (Sisa perintah admin tidak berubah banyak, hanya perlu mengganti get/update file)
# Contoh modifikasi untuk satu perintah:
@bot.tree.command(name="admin_cek_user", description="ADMIN: Memeriksa status token dan cooldown pengguna.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="Pengguna yang akan diperiksa.")
async def admin_cek_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    user_id = str(user.id)
    async with bot.github_lock:
        claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content)

    if user_id not in claims_data:
        await interaction.followup.send(f"Pengguna **{user.display_name}** belum pernah melakukan klaim token.", ephemeral=True)
        return
    
    # ... sisa logika sama ...
    user_data = claims_data[user_id]
    embed = discord.Embed(title=f"🔍 Status Token - {user.display_name}", color=discord.Color.orange())
    token = user_data.get("current_token")
    if token:
        expiry_time = datetime.fromisoformat(user_data["token_expiry_timestamp"])
        embed.add_field(name="Token Aktif", value=f"`{token}`", inline=False)
        embed.add_field(name="Kedaluwarsa Pada", value=expiry_time.strftime('%d %B %Y, %H:%M UTC'), inline=False)
    else:
        embed.add_field(name="Token Aktif", value="Tidak ada", inline=False)
    last_claim_time = datetime.fromisoformat(user_data["last_claim_timestamp"])
    next_claim_time = last_claim_time + timedelta(days=7)
    embed.add_field(name="Klaim Terakhir", value=last_claim_time.strftime('%d %B %Y, %H:%M UTC'), inline=False)
    if datetime.now(timezone.utc) < next_claim_time:
        embed.add_field(name="Dapat Klaim Lagi Pada", value=next_claim_time.strftime('%d %B %Y, %H:%M UTC'), inline=False)
        embed.set_footer(text="Pengguna saat ini masih dalam masa cooldown.")
    else:
        embed.add_field(name="Dapat Klaim Lagi Pada", value="Sekarang", inline=False)
        embed.set_footer(text="Pengguna sudah bisa melakukan klaim baru.")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="admin_add_token", description="ADMIN: Menambahkan token custom ke API (tanpa durasi).")
@app_commands.checks.has_permissions(administrator=True)
async def admin_add_token(interaction: discord.Interaction, token: str):
    await interaction.response.defer(ephemeral=True)
    async with bot.github_lock:
        tokens_content, tokens_sha = get_github_file(PRIMARY_REPO, TOKENS_FILE_PATH)
        if tokens_content is None: tokens_content = ""
        existing_tokens = {t.strip() for t in tokens_content.split('\n\n') if t.strip()}
        if token in existing_tokens:
            await interaction.followup.send("❌ Token custom tersebut sudah ada.", ephemeral=True)
            return
        existing_tokens.add(token)
        new_tokens_content = "\n\n".join(sorted(list(existing_tokens))) + "\n\n"
        update_github_file(PRIMARY_REPO, TOKENS_FILE_PATH, new_tokens_content, tokens_sha, f"Admin: Add custom token {token}")
    await interaction.followup.send(f"✅ Token custom `{token}` berhasil ditambahkan.", ephemeral=True)

@bot.tree.command(name="admin_remove_token", description="ADMIN: Menghapus token apapun dari API.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_remove_token(interaction: discord.Interaction, token: str):
    await interaction.response.defer(ephemeral=True)
    async with bot.github_lock:
        tokens_content, tokens_sha = get_github_file(PRIMARY_REPO, TOKENS_FILE_PATH)
        if tokens_content is None:
            await interaction.followup.send("❌ File token kosong atau tidak dapat diakses.", ephemeral=True)
            return
        
        lines = [line for line in tokens_content.split('\n\n') if line.strip() and line.strip() != token]
        
        if len(lines) == len(tokens_content.split('\n\n')):
             await interaction.followup.send(f"❌ Token `{token}` tidak ditemukan.", ephemeral=True)
             return

        new_tokens_content = "\n\n".join(lines) + ("\n\n" if lines else "")
        update_github_file(PRIMARY_REPO, TOKENS_FILE_PATH, new_tokens_content, tokens_sha, f"Admin: Force remove token {token}")
    await interaction.followup.send(f"✅ Token `{token}` berhasil dihapus paksa.", ephemeral=True)

@bot.tree.command(name="admin_reset_cooldown", description="ADMIN: Mereset cooldown klaim untuk pengguna tertentu.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="Pengguna yang cooldown-nya akan direset.")
async def admin_reset_cooldown(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    target_user_id = str(user.id)
    token_to_remove = None

    async with bot.github_lock:
        claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content)
        
        if target_user_id not in claims_data:
            await interaction.followup.send(f"ℹ️ Pengguna {user.mention} belum pernah melakukan klaim.", ephemeral=True)
            return
            
        token_to_remove = claims_data[target_user_id].get('current_token')
        del claims_data[target_user_id]
        new_claims_content = json.dumps(claims_data, indent=4)
        update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, new_claims_content, claims_sha, f"Admin: Reset cooldown for {user.name}")
        
        if token_to_remove:
            tokens_content, tokens_sha = get_github_file(PRIMARY_REPO, TOKENS_FILE_PATH)
            if tokens_content is not None:
                lines = [line for line in tokens_content.split('\n\n') if line.strip() != token_to_remove and line.strip()]
                new_tokens_content = "\n\n".join(lines) + ("\n\n" if lines else "")
                update_github_file(PRIMARY_REPO, TOKENS_FILE_PATH, new_tokens_content, tokens_sha, f"Admin: Remove token for cooldown reset of {user.name}")

    dm_status = ""
    try:
        await user.send("Pemberitahuan: Cooldown klaim token Anda telah direset oleh admin. Anda sekarang dapat melakukan klaim token baru.")
        dm_status = "✅ Notifikasi DM berhasil dikirim ke pengguna."
    except discord.Forbidden:
        dm_status = "⚠️ Gagal mengirim notifikasi DM."
    await interaction.followup.send(f"✅ Cooldown untuk {user.mention} berhasil direset. {dm_status}", ephemeral=True)

@bot.tree.command(name="list_tokens", description="ADMIN: Menampilkan daftar semua token aktif.")
@app_commands.checks.has_permissions(administrator=True)
async def list_tokens(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.github_lock:
        claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content)

    if not claims_data:
        await interaction.followup.send("Tidak ada data klaim ditemukan.", ephemeral=True)
        return

    embed = discord.Embed(title="Daftar Token Aktif", color=discord.Color.blue())
    current_time = datetime.now(timezone.utc)
    token_list = []
    
    for user_id, data in claims_data.items():
        if "admin_test" in user_id or "current_token" not in data:
            continue
        
        expiry_time = datetime.fromisoformat(data.get("token_expiry_timestamp"))
        if expiry_time < current_time:
            continue # Lewati token yang sudah kedaluwarsa

        try:
            user = await bot.fetch_user(int(user_id))
            username = str(user)
        except (discord.NotFound, ValueError):
            username = f"ID: {user_id}"
            
        token = data.get("current_token", "N/A")
        remaining_time = expiry_time - current_time
        days = remaining_time.days
        hours, remainder = divmod(remaining_time.seconds, 3600)
        remaining_str = f"{days} hari {hours} jam"
        
        token_list.append(f"**{username}**: `{token}`\n*Sisa Waktu: {remaining_str}*")

    embed.description = "\n\n".join(token_list) if token_list else "Tidak ada token yang sedang aktif."
    await interaction.followup.send(embed=embed, ephemeral=True)

# ... (Sisa perintah admin dan loop bisa dimasukkan di sini dengan modifikasi serupa)

# --- EVENT & LOOP ---
@tasks.loop(minutes=5) # Diperpanjang interval untuk mengurangi request API
async def check_expirations():
    await bot.wait_until_ready()
    print(f"[{datetime.now()}] Menjalankan pemeriksaan kedaluwarsa...")
    
    async with bot.github_lock:
        claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        tokens_content, tokens_sha = get_github_file(PRIMARY_REPO, TOKENS_FILE_PATH)

        if claims_content is None or tokens_content is None:
            print("Gagal mengambil file dari GitHub, pemeriksaan dibatalkan.")
            return

        claims_data = json.loads(claims_content)
        current_time = datetime.now(timezone.utc)
        
        tokens_to_remove = set()
        claims_data_changed = False
        
        # Iterasi pada salinan untuk modifikasi
        for user_id, data in list(claims_data.items()):
            if 'token_expiry_timestamp' in data:
                expiry_time = datetime.fromisoformat(data['token_expiry_timestamp'])
                if expiry_time < current_time:
                    print(f"Token untuk user {user_id} telah kedaluwarsa.")
                    token = data.pop('current_token')
                    tokens_to_remove.add(token)
                    claims_data_changed = True
                    # Kirim notifikasi kedaluwarsa
                    try:
                        user = await bot.fetch_user(int(user_id))
                        await user.send(f"🔴 Token Anda (`{token}`) telah kedaluwarsa.")
                    except Exception as e:
                        print(f"Gagal notif kedaluwarsa ke user {user_id}: {e}")

        if tokens_to_remove:
            lines = [line for line in tokens_content.split('\n\n') if line.strip() and line.strip() not in tokens_to_remove]
            new_tokens_content = "\n\n".join(lines) + ("\n\n" if lines else "")
            update_github_file(PRIMARY_REPO, TOKENS_FILE_PATH, new_tokens_content, tokens_sha, "Bot: Hapus token kedaluwarsa")

        if claims_data_changed:
            new_claims_content = json.dumps(claims_data, indent=4)
            update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, new_claims_content, claims_sha, "Bot: Update data klaim setelah kedaluwarsa")


@bot.event
async def on_guild_join(guild):
    if guild.id not in ALLOWED_GUILD_IDS:
        print(f"Bot diundang ke server tidak sah: '{guild.name}' (ID: {guild.id}). Bot akan keluar.")
        try:
            if guild.owner:
                 await guild.owner.send(f"Bot '{bot.user.name}' ini bersifat pribadi dan hanya untuk server tertentu.")
        except discord.Forbidden:
            print(f"Tidak bisa mengirim DM ke owner server '{guild.name}'.")
        await guild.leave()
    else:
        print(f"Bot berhasil bergabung dengan server yang diizinkan: '{guild.name}'.")

@bot.event
async def on_message(message: discord.Message):
    # Logika untuk role otomatis tidak berubah
    if message.author.bot or not message.guild: return
    if not ROLE_REQUEST_CHANNEL_ID or message.channel.id != ROLE_REQUEST_CHANNEL_ID: return
    # ... sisa logika sama persis ...
    if not message.attachments: return
    guild = message.guild
    subscriber_role = discord.utils.get(guild.roles, name=SUBSCRIBER_ROLE_NAME)
    follower_role = discord.utils.get(guild.roles, name=FOLLOWER_ROLE_NAME)
    forge_verified_role = discord.utils.get(guild.roles, name=FORGE_VERIFIED_ROLE_NAME)
    if not all([subscriber_role, follower_role, forge_verified_role]):
        print(f"ERROR: Satu atau lebih role ({SUBSCRIBER_ROLE_NAME}, {FOLLOWER_ROLE_NAME}, {FORGE_VERIFIED_ROLE_NAME}) tidak ditemukan."); return
    roles_to_add = set()
    message_content = message.content.lower()
    author_roles = message.author.roles
    if len(message.attachments) >= 2:
        roles_to_add.add(subscriber_role)
        roles_to_add.add(follower_role)
    else:
        has_youtube = "youtube" in message_content
        has_tiktok = "tiktok" in message_content
        if has_youtube or has_tiktok:
            if has_youtube: roles_to_add.add(subscriber_role)
            if has_tiktok: roles_to_add.add(follower_role)
        else:
            if subscriber_role not in author_roles: roles_to_add.add(subscriber_role)
            elif follower_role not in author_roles: roles_to_add.add(follower_role)
    potential_final_roles = set(author_roles).union(roles_to_add)
    if subscriber_role in potential_final_roles and follower_role in potential_final_roles:
        roles_to_add.add(forge_verified_role)
    final_roles_to_add = [role for role in roles_to_add if role not in author_roles]
    if final_roles_to_add:
        try:
            await message.author.add_roles(*final_roles_to_add, reason="Otomatis dari channel request role")
            role_names = ", ".join([f"**{r.name}**" for r in final_roles_to_add])
            await message.reply(f"✅ Halo {message.author.mention}, Anda telah menerima role: {role_names}!", delete_after=25)
            await message.add_reaction('✅')
        except discord.Forbidden: print(f"GAGAL: Bot tidak memiliki izin 'Manage Roles'.")
        except Exception as e: print(f"Terjadi error saat memberikan role: {e}")

@bot.event
async def on_ready():
    bot.claim_session_open = False
    bot.github_lock = asyncio.Lock()
    bot.add_view(ClaimPanelView(bot))
    await bot.tree.sync()
    check_expirations.start()
    print(f'Bot telah login sebagai {bot.user.name}')
    print(f'Terhubung ke repo data: {PRIMARY_REPO}')
    print(f'Beroperasi di server IDs: {ALLOWED_GUILD_IDS}')

bot.run(DISCORD_TOKEN)
