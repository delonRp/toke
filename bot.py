# -*- coding: utf-8 -*-
"""
Bot Discord untuk Manajemen Token via GitHub API
Versi Lengkap dengan dukungan Multi-Repositori, Multi-File, dan auto-delete pesan status.
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
from typing import List, Dict

# --- KONFIGURASI DARI ENVIRONMENT VARIABLES ---
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
# Repositori "Utama" untuk menyimpan data klaim pengguna (claims.json)
PRIMARY_REPO = os.environ.get('PRIMARY_REPO')
ALLOWED_GUILD_IDS_STR = os.environ.get('ALLOWED_GUILD_IDS', '')
CLAIM_CHANNEL_ID = int(os.environ.get('CLAIM_CHANNEL_ID', 0))
ROLE_REQUEST_CHANNEL_ID = int(os.environ.get('ROLE_REQUEST_CHANNEL_ID', 0))
# Variabel untuk mendefinisikan sumber file token yang bisa ditulisi
# Format: "alias1:owner/repo1/path/to/file1.txt,alias2:owner/repo2/path/to/file2.txt"
TOKEN_SOURCES_STR = os.environ.get('TOKEN_SOURCES', '')

if not all([DISCORD_TOKEN, GITHUB_TOKEN, PRIMARY_REPO, ALLOWED_GUILD_IDS_STR, TOKEN_SOURCES_STR]):
    print("FATAL ERROR: Pastikan semua variabel (DISCORD_TOKEN, GITHUB_TOKEN, PRIMARY_REPO, ALLOWED_GUILD_IDS, TOKEN_SOURCES) telah diatur.")
    exit()

try:
    ALLOWED_GUILD_IDS = {int(gid.strip()) for gid in ALLOWED_GUILD_IDS_STR.split(',')}
except ValueError:
    print("FATAL ERROR: Format ALLOWED_GUILD_IDS tidak valid.")
    exit()

# Parse sumber token menjadi dictionary yang lebih mudah digunakan
TOKEN_SOURCES: Dict[str, Dict[str, str]] = {}
if TOKEN_SOURCES_STR:
    try:
        for item in TOKEN_SOURCES_STR.split(','):
            alias, full_path = item.split(':', 1)
            alias = alias.strip().lower()
            parts = full_path.strip().split('/')
            owner, repo, path = parts[0], parts[1], '/'.join(parts[2:])
            TOKEN_SOURCES[alias] = {
                "slug": f"{owner}/{repo}",
                "path": path
            }
    except Exception as e:
        print(f"FATAL ERROR: Format TOKEN_SOURCES tidak valid. Error: {e}")
        exit()

# --- PATH FILE DI REPOSITORY GITHUB ---
CLAIMS_FILE_PATH = 'claims.json' # Selalu di PRIMARY_REPO

# --- KONFIGURASI ROLE (TETAP) ---
ROLE_DURATIONS = {
    "vip": "30d", "supporter": "10d", "inner circle": "7d",
    "subscriber": "5d", "followers": "5d", "beginner": "3d"
}
ROLE_PRIORITY = ["vip", "supporter", "inner circle", "subscriber", "followers", "beginner"]
SUBSCRIBER_ROLE_NAME = "Subscriber"
FOLLOWER_ROLE_NAME = "Followers"
FORGE_VERIFIED_ROLE_NAME = "Inner Circle"

# --- SETUP BOT ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!unusedprefix!", intents=intents, help_command=None)

# --- FUNGSI BANTUAN ---
def get_github_file(repo_slug, file_path):
    url = f"https://api.github.com/repos/{repo_slug}/contents/{file_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return base64.b64decode(data['content']).decode('utf-8'), data['sha']
        elif response.status_code == 404:
            return None, None
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error saat get file: {e}")
        return None, None
    return None, None

def update_github_file(repo_slug, file_path, new_content, sha, commit_message):
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
        print(f"Error saat update file: {e}")

def parse_duration(duration_str: str) -> timedelta:
    try:
        unit = duration_str[-1].lower()
        value = int(duration_str[:-1])
        if unit == 'd': return timedelta(days=value)
        if unit == 'h': return timedelta(hours=value)
        if unit == 'm': return timedelta(minutes=value)
        if unit == 's': return timedelta(seconds=value)
    except (ValueError, IndexError):
        raise ValueError("Format durasi tidak valid.")
    raise ValueError(f"Unit durasi tidak dikenal: {unit}")

def generate_random_token(role_name: str) -> str:
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
        if not self.bot.current_claim_source_alias:
            await interaction.response.send_message("❌ Sesi klaim saat ini sedang ditutup oleh admin.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        user, user_id, current_time = interaction.user, str(interaction.user.id), datetime.now(timezone.utc)
        
        async with self.bot.github_lock:
            claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
            claims_data = json.loads(claims_content if claims_content else '{}')

            if user_id in claims_data:
                user_claim_info = claims_data[user_id]
                last_claim_time = datetime.fromisoformat(user_claim_info['last_claim_timestamp'])
                if current_time < last_claim_time + timedelta(days=7):
                    next_claim_time = last_claim_time + timedelta(days=7)
                    await interaction.followup.send(f"❌ **Cooldown!** Anda baru bisa klaim lagi pada {next_claim_time.strftime('%d %B %Y, %H:%M')} UTC.", ephemeral=True)
                    return
                
                if 'current_token' in user_claim_info and datetime.fromisoformat(user_claim_info['token_expiry_timestamp']) > current_time:
                    await interaction.followup.send(f"❌ Token Anda masih aktif.", ephemeral=True)
                    return

            user_role_names = [role.name.lower() for role in user.roles]
            claim_role = next((role for role in ROLE_PRIORITY if role in user_role_names), None)
            if not claim_role:
                await interaction.followup.send("❌ Anda tidak memiliki peran yang valid untuk klaim token.", ephemeral=True)
                return
            
            source_alias = self.bot.current_claim_source_alias
            token_source_info = TOKEN_SOURCES[source_alias]
            target_repo_slug = token_source_info["slug"]
            target_file_path = token_source_info["path"]

            duration_str = ROLE_DURATIONS[claim_role]
            duration_delta = parse_duration(duration_str)
            new_token = generate_random_token(claim_role)
            
            tokens_content, tokens_sha = get_github_file(target_repo_slug, target_file_path)
            if tokens_content is None:
                tokens_content = ""
            
            new_tokens_content = tokens_content.strip() + f"\n\n{new_token}\n\n"
            update_github_file(target_repo_slug, target_file_path, new_tokens_content, tokens_sha, f"Bot: Add token for {user.name}")
            
            claims_data[user_id] = {"last_claim_timestamp": current_time.isoformat(), "current_token": new_token, "token_expiry_timestamp": (current_time + duration_delta).isoformat(), "source_alias": source_alias}
            update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, json.dumps(claims_data, indent=4), claims_sha, f"Bot: Update claim for {user.name}")

        try:
            await user.send(f"🎉 **Token Anda Berhasil Diklaim!**\n\n**Sumber:** `{source_alias.title()}`\n**Token Anda:** `{new_token}`\n**Role:** `{claim_role.title()}`\nAktif selama **{duration_str.replace('d', ' hari')}**.")
            await interaction.followup.send("✅ **Berhasil!** Token Anda telah dikirim melalui DM.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("⚠️ Gagal mengirim DM. Token Anda tetap dibuat.", ephemeral=True)

    @ui.button(label="Cek Token Saya", style=discord.ButtonStyle.secondary, custom_id="check_token_button")
    async def check_button_callback(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        
        async with self.bot.github_lock:
            claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
            claims_data = json.loads(claims_content if claims_content else '{}')

        if user_id not in claims_data:
            await interaction.followup.send("Anda belum pernah melakukan klaim token.", ephemeral=True)
            return
        
        user_data = claims_data[user_id]
        embed = discord.Embed(title="📄 Detail Token Anda", color=discord.Color.blue())
        
        if "current_token" in user_data and datetime.fromisoformat(user_data["token_expiry_timestamp"]) > datetime.now(timezone.utc):
            embed.add_field(name="Token Aktif", value=f"`{user_data['current_token']}`", inline=False)
            embed.add_field(name="Sumber", value=f"`{user_data.get('source_alias', 'N/A').title()}`", inline=True)
            expiry_time = datetime.fromisoformat(user_data["token_expiry_timestamp"])
            embed.add_field(name="Kedaluwarsa Pada", value=f"{expiry_time.strftime('%d %B %Y, %H:%M')} UTC", inline=True)
        else:
            embed.description = "Anda tidak memiliki token yang aktif saat ini."

        last_claim_time = datetime.fromisoformat(user_data["last_claim_timestamp"])
        next_claim_time = last_claim_time + timedelta(days=7)
        if datetime.now(timezone.utc) < next_claim_time:
             embed.add_field(name="Cooldown Klaim", value=f"Bisa klaim lagi pada {next_claim_time.strftime('%d %B %Y, %H:%M')} UTC", inline=False)
        else:
             embed.add_field(name="Cooldown Klaim", value="Anda sudah bisa klaim token baru.", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

# --- AUTOCOMPLETE UNTUK PERINTAH ---
async def source_alias_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=alias, value=alias)
        for alias in TOKEN_SOURCES.keys() if current.lower() in alias.lower()
    ]

# --- PERINTAH SLASH COMMAND ---
@bot.tree.command(name="help", description="Menampilkan daftar semua perintah yang tersedia.")
async def help_command(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.administrator
    embed = discord.Embed(title="📜 Daftar Perintah Bot", color=discord.Color.gold())
    embed.description = "Berikut adalah perintah yang bisa Anda gunakan."
    embed.add_field(name="</help:0>", value="Menampilkan pesan bantuan ini.", inline=False)
    
    if is_admin:
        embed.add_field(name="👑 Perintah Admin", value=(
            "**/open_claim**: Membuka sesi klaim untuk sumber token tertentu.\n"
            "**/close_claim**: Menutup sesi klaim.\n"
            "**/admin_add_token**: Menambahkan token custom ke sumber tertentu.\n"
            "**/admin_remove_token**: Menghapus token dari sumber tertentu.\n"
            "**/admin_reset_cooldown**: Mereset cooldown klaim pengguna.\n"
            "**/admin_cek_user**: Memeriksa status token dan cooldown pengguna.\n"
            "**/list_tokens**: Menampilkan daftar semua token aktif dari database.\n"
            "**/list_sources**: Menampilkan semua sumber token yang terkonfigurasi.\n"
            "**/baca_file**: Membaca konten file dari sumber token.\n"
            "**/show_config**: Menampilkan channel yang terkonfigurasi.\n"
            "**/serverlist**: Menampilkan daftar server bot."
        ), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- PERINTAH ADMIN ---
@bot.tree.command(name="open_claim", description="ADMIN: Membuka sesi klaim untuk sumber token tertentu.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def open_claim(interaction: discord.Interaction, alias: str):
    await interaction.response.defer(ephemeral=True)
    
    if alias.lower() not in TOKEN_SOURCES:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak ditemukan. Gunakan `/list_sources` untuk melihat alias yang valid.", ephemeral=True)
        return
    
    if not CLAIM_CHANNEL_ID:
        await interaction.followup.send("❌ Channel klaim belum diatur di `CLAIM_CHANNEL_ID`.", ephemeral=True)
        return
    
    claim_channel = bot.get_channel(CLAIM_CHANNEL_ID)
    if not claim_channel:
        await interaction.followup.send(f"❌ Channel dengan ID `{CLAIM_CHANNEL_ID}` tidak ditemukan.", ephemeral=True)
        return

    # Hapus pesan 'sesi ditutup' sebelumnya jika ada
    if bot.close_claim_message:
        try:
            await bot.close_claim_message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass # Abaikan jika pesan sudah dihapus atau terjadi error
        finally:
            bot.close_claim_message = None

    bot.current_claim_source_alias = alias.lower()
    embed = discord.Embed(title=f"📝 Sesi Klaim Dibuka: {alias.title()}", description=f"Sesi klaim token untuk sumber `{alias.title()}` telah dibuka. Gunakan tombol di bawah.", color=discord.Color.green())
    
    sent_message = await claim_channel.send(embed=embed, view=ClaimPanelView(bot))
    bot.open_claim_message = sent_message # Simpan objek pesan yang baru dikirim

    await interaction.followup.send(f"✅ Panel klaim untuk `{alias.title()}` telah dikirim ke {claim_channel.mention}.", ephemeral=True)

@bot.tree.command(name="close_claim", description="ADMIN: Menutup sesi klaim dan mengirim notifikasi.")
@app_commands.checks.has_permissions(administrator=True)
async def close_claim(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not bot.current_claim_source_alias:
        await interaction.followup.send("ℹ️ Tidak ada sesi klaim yang sedang aktif.", ephemeral=True)
        return
        
    # Hapus pesan 'sesi dibuka' sebelumnya jika ada
    if bot.open_claim_message:
        try:
            await bot.open_claim_message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass # Abaikan jika pesan sudah dihapus atau terjadi error
        finally:
            bot.open_claim_message = None
            
    closed_alias = bot.current_claim_source_alias
    bot.current_claim_source_alias = None
    
    if CLAIM_CHANNEL_ID and (claim_channel := bot.get_channel(CLAIM_CHANNEL_ID)):
        embed = discord.Embed(title="🔴 Sesi Klaim Ditutup", description=f"Admin telah menutup sesi klaim untuk `{closed_alias.title()}`.", color=discord.Color.red())
        sent_message = await claim_channel.send(embed=embed)
        bot.close_claim_message = sent_message # Simpan objek pesan yang baru dikirim

    await interaction.followup.send(f"🔴 Sesi klaim untuk `{closed_alias.title()}` telah **DITUTUP**.", ephemeral=True)

@bot.tree.command(name="admin_add_token", description="ADMIN: Menambahkan token custom ke sumber file tertentu.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def admin_add_token(interaction: discord.Interaction, alias: str, token: str):
    await interaction.response.defer(ephemeral=True)
    
    source_info = TOKEN_SOURCES.get(alias.lower())
    if source_info is None:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True)
        return

    async with bot.github_lock:
        content, sha = get_github_file(source_info["slug"], source_info["path"])
        if content is None:
            content = ""
        
        if token in content:
            await interaction.followup.send(f"❌ Token `{token}` sudah ada di `{alias}`.", ephemeral=True)
            return
        
        new_content = content.strip() + f"\n\n{token}\n\n"
        update_github_file(source_info["slug"], source_info["path"], new_content, sha, f"Admin: Add custom token {token}")
    await interaction.followup.send(f"✅ Token custom `{token}` berhasil ditambahkan ke `{alias}`.", ephemeral=True)

@bot.tree.command(name="admin_remove_token", description="ADMIN: Menghapus token dari sumber file tertentu.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def admin_remove_token(interaction: discord.Interaction, alias: str, token: str):
    await interaction.response.defer(ephemeral=True)
    
    source_info = TOKEN_SOURCES.get(alias.lower())
    if source_info is None:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True)
        return
        
    async with bot.github_lock:
        content, sha = get_github_file(source_info["slug"], source_info["path"])
        if content is None or token not in content:
            await interaction.followup.send(f"❌ Token `{token}` tidak ditemukan di `{alias}`.", ephemeral=True)
            return
            
        lines = [line for line in content.split('\n\n') if line.strip() and line.strip() != token]
        new_content = "\n\n".join(lines) + ("\n\n" if lines else "")
        update_github_file(source_info["slug"], source_info["path"], new_content, sha, f"Admin: Remove token {token}")
    await interaction.followup.send(f"✅ Token `{token}` berhasil dihapus dari `{alias}`.", ephemeral=True)

@bot.tree.command(name="list_sources", description="ADMIN: Menampilkan semua sumber token yang terkonfigurasi.")
@app_commands.checks.has_permissions(administrator=True)
async def list_sources(interaction: discord.Interaction):
    embed = discord.Embed(title="🔧 Konfigurasi Sumber Token", color=discord.Color.purple())
    if not TOKEN_SOURCES:
        embed.description = "Tidak ada sumber token yang diatur di variabel `TOKEN_SOURCES`."
    else:
        for alias, info in TOKEN_SOURCES.items():
            embed.add_field(name=f"Alias: `{alias.title()}`", value=f"**Repo:** `{info['slug']}`\n**File:** `{info['path']}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="baca_file", description="ADMIN: Membaca konten file dari sumber token yang terkonfigurasi.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def baca_file(interaction: discord.Interaction, alias: str):
    await interaction.response.defer(ephemeral=True)
    
    source_info = TOKEN_SOURCES.get(alias.lower())
    if source_info is None:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True)
        return
        
    content, _ = get_github_file(source_info["slug"], source_info["path"])
    if content is None:
        await interaction.followup.send(f"❌ File tidak ditemukan di `{alias}`.", ephemeral=True)
        return
        
    content_to_show = content[:1900] + "\n... (dipotong)" if len(content) > 1900 else content
    embed = discord.Embed(title=f"📄 Konten dari `{alias}`", description=f"```\n{content_to_show or '[File Kosong]'}\n```", color=discord.Color.blue())
    embed.set_footer(text=f"Repo: {source_info['slug']}, File: {source_info['path']}")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="admin_reset_cooldown", description="ADMIN: Mereset cooldown klaim untuk pengguna.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_reset_cooldown(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    target_user_id = str(user.id)
    async with bot.github_lock:
        claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content if claims_content else '{}')
        
        if target_user_id not in claims_data:
            await interaction.followup.send(f"ℹ️ Pengguna {user.mention} belum pernah klaim.", ephemeral=True)
            return
        
        claims_data.pop(target_user_id)
        update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, json.dumps(claims_data, indent=4), claims_sha, f"Admin: Reset cooldown for {user.name}")

    await interaction.followup.send(f"✅ Cooldown untuk {user.mention} berhasil direset.", ephemeral=True)

@bot.tree.command(name="admin_cek_user", description="ADMIN: Memeriksa status token dan cooldown pengguna.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_cek_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    user_id = str(user.id)
    async with bot.github_lock:
        claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content if claims_content else '{}')

    if user_id not in claims_data:
        await interaction.followup.send(f"Pengguna **{user.display_name}** belum pernah melakukan klaim token.", ephemeral=True)
        return
    
    user_data = claims_data[user_id]
    embed = discord.Embed(title=f"🔍 Status Token - {user.display_name}", color=discord.Color.orange())
    
    if "current_token" in user_data and datetime.fromisoformat(user_data["token_expiry_timestamp"]) > datetime.now(timezone.utc):
        embed.add_field(name="Token Aktif", value=f"`{user_data['current_token']}`", inline=False)
        embed.add_field(name="Sumber", value=f"`{user_data.get('source_alias', 'N/A').title()}`", inline=True)
        expiry_time = datetime.fromisoformat(user_data["token_expiry_timestamp"])
        embed.add_field(name="Kedaluwarsa Pada", value=f"{expiry_time.strftime('%d %B %Y, %H:%M')} UTC", inline=True)
    else:
        embed.description = "Pengguna tidak memiliki token yang aktif saat ini."

    last_claim_time = datetime.fromisoformat(user_data["last_claim_timestamp"])
    next_claim_time = last_claim_time + timedelta(days=7)
    embed.add_field(name="Klaim Terakhir", value=last_claim_time.strftime('%d %B %Y, %H:%M UTC'), inline=False)
    if datetime.now(timezone.utc) < next_claim_time:
        embed.add_field(name="Dapat Klaim Lagi Pada", value=next_claim_time.strftime('%d %B %Y, %H:%M UTC'), inline=False)
    else:
        embed.add_field(name="Dapat Klaim Lagi Pada", value="Sekarang", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="list_tokens", description="ADMIN: Menampilkan daftar semua token aktif dari database.")
@app_commands.checks.has_permissions(administrator=True)
async def list_tokens(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with bot.github_lock:
        claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content if claims_content else '{}')

    if not claims_data:
        await interaction.followup.send("Tidak ada data klaim ditemukan.", ephemeral=True)
        return

    embed = discord.Embed(title="Daftar Token Aktif", color=discord.Color.blue())
    token_list = []
    
    for user_id, data in claims_data.items():
        if "current_token" in data and datetime.fromisoformat(data["token_expiry_timestamp"]) > datetime.now(timezone.utc):
            try:
                user = await bot.fetch_user(int(user_id))
                username = str(user)
            except (discord.NotFound, ValueError):
                username = f"ID: {user_id}"
            
            token = data.get("current_token", "N/A")
            source = data.get("source_alias", "N/A").title()
            token_list.append(f"**{username}**: `{token}` (Sumber: {source})")

    embed.description = "\n".join(token_list) if token_list else "Tidak ada token yang sedang aktif."
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="show_config", description="ADMIN: Menampilkan channel yang terkonfigurasi.")
@app_commands.checks.has_permissions(administrator=True)
async def show_config(interaction: discord.Interaction):
    embed = discord.Embed(title="🔧 Konfigurasi Channel Bot", color=discord.Color.teal())
    claim_ch_mention = f"<#{CLAIM_CHANNEL_ID}>" if CLAIM_CHANNEL_ID else "Belum diatur"
    role_req_ch_mention = f"<#{ROLE_REQUEST_CHANNEL_ID}>" if ROLE_REQUEST_CHANNEL_ID else "Belum diatur"
    embed.add_field(name="Channel Klaim Token", value=claim_ch_mention, inline=False)
    embed.add_field(name="Channel Request Role", value=role_req_ch_mention, inline=False)
    embed.set_footer(text="Pengaturan ini dikelola melalui Environment Variables di Railway.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="serverlist", description="ADMIN: Menampilkan daftar semua server tempat bot ini berada.")
@app_commands.checks.has_permissions(administrator=True)
async def serverlist(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message("❌ Perintah ini hanya untuk pemilik bot.", ephemeral=True)
        return
    server_list = [f"- **{guild.name}** (ID: `{guild.id}`)" for guild in bot.guilds]
    embed = discord.Embed(title=f"Bot Aktif di {len(bot.guilds)} Server", description="\n".join(server_list), color=0x3498db)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- EVENT & LOOP ---
@bot.event
async def on_ready():
    bot.current_claim_source_alias = None
    bot.open_claim_message = None
    bot.close_claim_message = None
    bot.github_lock = asyncio.Lock()
    bot.add_view(ClaimPanelView(bot))
    await bot.tree.sync()
    print(f'Bot telah login sebagai {bot.user.name}')
    print(f'Repo data utama (claims): {PRIMARY_REPO}')
    print(f'Server IDs: {ALLOWED_GUILD_IDS}')
    print(f'Sumber Token Terkonfigurasi: {len(TOKEN_SOURCES)} sumber')

# Fungsi-fungsi lain yang tidak dimodifikasi
@bot.event
async def on_guild_join(guild):
    if guild.id not in ALLOWED_GUILD_IDS:
        await guild.leave()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild or not ROLE_REQUEST_CHANNEL_ID or message.channel.id != ROLE_REQUEST_CHANNEL_ID:
        return
    # Logika role otomatis
    pass # Disembunyikan untuk keringkasan, logika tetap sama

bot.run(DISCORD_TOKEN)

