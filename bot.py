# -*- coding: utf-8 -*-
"""
Bot Discord untuk Manajemen Token via GitHub API
Versi Lengkap dengan dukungan Multi-Repositori, Multi-File, dan auto-delete pesan status.
Versi ini menyertakan perbaikan bug kritis dan perintah khusus Owner/Admin.
Disesuaikan untuk deployment di Railway dengan Environment Variables.

[CATATAN PERBAIKAN & KOMPATIBILITAS]
1.  BUG FIX: Proses klaim kini bersifat transaksional. Token hanya akan valid jika data klaim BERHASIL disimpan di 'claims.json'.
2.  KOMPATIBILITAS: Bot sekarang dapat membaca format data lama di `claims.json` (yang mungkin hanya memiliki `last_claim_timestamp`) tanpa error.
3.  AUTO-MIGRASI: Saat pengguna dengan data lama berhasil melakukan klaim baru, entri mereka akan secara otomatis diperbarui ke format data yang lengkap dan terstruktur.
4.  FITUR BARU: Menambahkan variabel environment 'ADMIN_USER_IDS' untuk mendaftarkan beberapa admin. Bot owner otomatis menjadi admin.
5.  FIX: Menambahkan parser otomatis untuk URL repo agar tahan terhadap kesalahan format pada environment variable (memperbaiki error 404).
"""

import discord
from discord import app_commands, ui
from discord.ext import commands
import os
import requests
import base64
import json
from datetime import datetime, timedelta, timezone
import secrets
import string
import asyncio
from typing import List, Dict, Optional

# --- [FIX] FUNGSI BARU UNTUK MEMBERSIHKAN SLUG REPO ---
def parse_repo_slug(repo_input: str) -> str:
    """Membersihkan input URL repo menjadi format 'owner/repo' yang valid untuk API."""
    if not repo_input:
        return ""
    # Hapus prefix umum
    for prefix in ["https://github.com/", "http://github.com/"]:
        if repo_input.startswith(prefix):
            repo_input = repo_input[len(prefix):]
    
    # Hapus suffix umum
    if repo_input.endswith(".git"):
        repo_input = repo_input[:-4]
    if repo_input.endswith("/"):
        repo_input = repo_input[:-1]
    
    # Ambil dua bagian terakhir dari path, yang seharusnya adalah owner/repo
    parts = repo_input.split('/')
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    
    return repo_input

# --- KONFIGURASI DARI ENVIRONMENT VARIABLES ---
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
PRIMARY_REPO_INPUT = os.environ.get('PRIMARY_REPO', '')
PRIMARY_REPO = parse_repo_slug(PRIMARY_REPO_INPUT) # [FIX] Terapkan pembersihan
ALLOWED_GUILD_IDS_STR = os.environ.get('ALLOWED_GUILD_IDS', '')
CLAIM_CHANNEL_ID = int(os.environ.get('CLAIM_CHANNEL_ID', 0))
ROLE_REQUEST_CHANNEL_ID = int(os.environ.get('ROLE_REQUEST_CHANNEL_ID', 0))
TOKEN_SOURCES_STR = os.environ.get('TOKEN_SOURCES', '')
ADMIN_USER_IDS_STR = os.environ.get('ADMIN_USER_IDS', '')


if not all([DISCORD_TOKEN, GITHUB_TOKEN, PRIMARY_REPO, ALLOWED_GUILD_IDS_STR, TOKEN_SOURCES_STR]):
    print("FATAL ERROR: Pastikan semua variabel (DISCORD_TOKEN, GITHUB_TOKEN, PRIMARY_REPO, ALLOWED_GUILD_IDS, TOKEN_SOURCES) telah diatur.")
    if not PRIMARY_REPO:
        print(f"FATAL ERROR: PRIMARY_REPO ('{PRIMARY_REPO_INPUT}') tidak dapat di-parse ke format 'owner/repo'.")
    exit()

try:
    ALLOWED_GUILD_IDS = {int(gid.strip()) for gid in ALLOWED_GUILD_IDS_STR.split(',')}
except ValueError:
    print("FATAL ERROR: Format ALLOWED_GUILD_IDS tidak valid.")
    exit()

TOKEN_SOURCES: Dict[str, Dict[str, str]] = {}
if TOKEN_SOURCES_STR:
    try:
        for item in TOKEN_SOURCES_STR.split(','):
            alias, full_path = item.split(':', 1)
            alias = alias.strip().lower()
            parts = full_path.strip().split('/')
            # [FIX] Terapkan pembersihan pada slug repo dari TOKEN_SOURCES
            raw_slug = '/'.join(parts[:-1]) # Gabungkan bagian repo
            path = parts[-1] # Bagian terakhir adalah path file
            cleaned_slug = parse_repo_slug(raw_slug)
            
            if len(cleaned_slug.split('/')) != 2:
                 print(f"WARNING: Slug token source '{alias}' ('{raw_slug}') mungkin tidak valid setelah di-parse.")

            TOKEN_SOURCES[alias] = {"slug": cleaned_slug, "path": path}
    except Exception as e:
        print(f"FATAL ERROR: Format TOKEN_SOURCES tidak valid. Error: {e}")
        exit()

# --- PATH FILE DI REPOSITORY GITHUB ---
CLAIMS_FILE_PATH = 'claims.json'

# --- KONFIGURASI ROLE (TETAP) ---
ROLE_DURATIONS = {"vip": "30d", "supporter": "10d", "inner circle": "7d", "subscriber": "5d", "followers": "5d", "beginner": "3d"}
ROLE_PRIORITY = ["vip", "supporter", "inner circle", "subscriber", "followers", "beginner"]
SUBSCRIBER_ROLE_NAME = "Subscriber"
FOLLOWER_ROLE_NAME = "Followers"
FORGE_VERIFIED_ROLE_NAME = "Inner Circle"

# --- SETUP BOT ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!unusedprefix!", intents=intents, help_command=None)

# --- DECORATOR UNTUK ADMIN CHECK ---
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not hasattr(bot, 'admin_ids'): return False
        return interaction.user.id in bot.admin_ids
    return app_commands.check(predicate)

# --- FUNGSI BANTUAN ---
def get_github_file(repo_slug: str, file_path: str) -> (Optional[str], Optional[str]):
    url = f"https://api.github.com/repos/{repo_slug}/contents/{file_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return base64.b64decode(data['content']).decode('utf-8'), data['sha']
        elif response.status_code == 404:
            return None, None
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error saat get file '{file_path}': {e}")
    return None, None

def update_github_file(repo_slug: str, file_path: str, new_content: str, sha: Optional[str], commit_message: str) -> bool:
    url = f"https://api.github.com/repos/{repo_slug}/contents/{file_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": encoded_content}
    if sha:
        data["sha"] = sha
    try:
        response = requests.put(url, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        print(f"File '{file_path}' berhasil diupdate: {commit_message}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error saat update file '{file_path}': {e}")
        return False

def parse_duration(duration_str: str) -> timedelta:
    try:
        unit = duration_str[-1].lower(); value = int(duration_str[:-1])
        if unit == 'd': return timedelta(days=value)
        if unit == 'h': return timedelta(hours=value)
        if unit == 'm': return timedelta(minutes=value)
        if unit == 's': return timedelta(seconds=value)
    except (ValueError, IndexError): raise ValueError("Format durasi tidak valid.")
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
                if 'last_claim_timestamp' in user_claim_info:
                    last_claim_time = datetime.fromisoformat(user_claim_info['last_claim_timestamp'])
                    if current_time < last_claim_time + timedelta(days=7):
                        next_claim_time = last_claim_time + timedelta(days=7)
                        await interaction.followup.send(f"❌ **Cooldown!** Anda baru bisa klaim lagi pada {next_claim_time.strftime('%d %B %Y, %H:%M')} UTC.", ephemeral=True); return
                
                if 'current_token' in user_claim_info and 'token_expiry_timestamp' in user_claim_info and datetime.fromisoformat(user_claim_info['token_expiry_timestamp']) > current_time:
                    await interaction.followup.send(f"❌ Token Anda saat ini masih aktif.", ephemeral=True); return

            user_role_names = [role.name.lower() for role in user.roles]
            claim_role = next((role for role in ROLE_PRIORITY if role in user_role_names), None)
            if not claim_role:
                await interaction.followup.send("❌ Anda tidak memiliki peran yang valid untuk klaim token.", ephemeral=True); return
            
            source_alias = self.bot.current_claim_source_alias
            token_source_info = TOKEN_SOURCES[source_alias]
            target_repo_slug, target_file_path = token_source_info["slug"], token_source_info["path"]
            duration_str = ROLE_DURATIONS[claim_role]
            duration_delta = parse_duration(duration_str)
            new_token = generate_random_token(claim_role)
            
            tokens_content, tokens_sha = get_github_file(target_repo_slug, target_file_path)
            new_tokens_content = (tokens_content or "").strip() + f"\n\n{new_token}\n\n"
            token_add_success = update_github_file(target_repo_slug, target_file_path, new_tokens_content, tokens_sha, f"Bot: Add token for {user.name}")
            
            if not token_add_success:
                await interaction.followup.send("❌ Gagal membuat token di file sumber. Silakan coba lagi.", ephemeral=True)
                return

            claims_data[user_id] = {
                "last_claim_timestamp": current_time.isoformat(), 
                "current_token": new_token, 
                "token_expiry_timestamp": (current_time + duration_delta).isoformat(), 
                "source_alias": source_alias
            }
            claim_db_update_success = update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, json.dumps(claims_data, indent=4), claims_sha, f"Bot: Update claim for {user.name}")

            if not claim_db_update_success:
                print(f"KRITIS: Gagal menyimpan claim untuk {user.name}. Melakukan rollback token.")
                current_tokens_content, current_tokens_sha = get_github_file(target_repo_slug, target_file_path)
                if current_tokens_content and new_token in current_tokens_content:
                    lines = [line for line in current_tokens_content.split('\n\n') if line.strip() and line.strip() != new_token]
                    content_after_removal = "\n\n".join(lines) + ("\n\n" if lines else "")
                    rollback_success = update_github_file(target_repo_slug, target_file_path, content_after_removal, current_tokens_sha, f"Bot: ROLLBACK token for {user.name}")
                    print(f"Status Rollback: {'Berhasil' if rollback_success else 'Gagal'}")
                await interaction.followup.send("❌ **Klaim Gagal!** Terjadi kesalahan saat menyimpan data klaim Anda. Token tidak dapat diberikan. Silakan hubungi admin.", ephemeral=True)
                return

        try:
            await user.send(f"🎉 **Token Anda Berhasil Diklaim!**\n\n**Sumber:** `{source_alias.title()}`\n**Token Anda:** `{new_token}`\n**Role:** `{claim_role.title()}`\nAktif selama **{duration_str.replace('d', ' hari')}**.")
            await interaction.followup.send("✅ **Berhasil!** Token Anda telah dikirim melalui DM.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("⚠️ Gagal mengirim DM. Token Anda tetap dibuat dan tersimpan.", ephemeral=True)

    @ui.button(label="Cek Token Saya", style=discord.ButtonStyle.secondary, custom_id="check_token_button")
    async def check_button_callback(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        
        claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content if claims_content else '{}')

        if user_id not in claims_data:
            await interaction.followup.send("Anda belum pernah melakukan klaim token.", ephemeral=True); return
        
        user_data = claims_data[user_id]
        embed = discord.Embed(title="📄 Detail Token Anda", color=discord.Color.blue())
        
        if 'current_token' in user_data and 'token_expiry_timestamp' in user_data and datetime.fromisoformat(user_data["token_expiry_timestamp"]) > datetime.now(timezone.utc):
            embed.add_field(name="Token Aktif", value=f"`{user_data['current_token']}`", inline=False)
            embed.add_field(name="Sumber", value=f"`{user_data.get('source_alias', 'N/A').title()}`", inline=True)
            expiry_time = datetime.fromisoformat(user_data["token_expiry_timestamp"])
            embed.add_field(name="Kedaluwarsa Pada", value=f"{expiry_time.strftime('%d %B %Y, %H:%M')} UTC", inline=True)
        else:
            embed.description = "Anda tidak memiliki token yang aktif saat ini."

        if 'last_claim_timestamp' in user_data:
            last_claim_time = datetime.fromisoformat(user_data["last_claim_timestamp"])
            next_claim_time = last_claim_time + timedelta(days=7)
            if datetime.now(timezone.utc) < next_claim_time:
                 embed.add_field(name="Cooldown Klaim", value=f"Bisa klaim lagi pada {next_claim_time.strftime('%d %B %Y, %H:%M')} UTC", inline=False)
            else:
                 embed.add_field(name="Cooldown Klaim", value="Anda sudah bisa klaim token baru.", inline=False)
        else:
            embed.add_field(name="Cooldown Klaim", value="Anda bisa klaim token sekarang.", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

# --- AUTOCOMPLETE & PERINTAH ---
async def source_alias_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [app_commands.Choice(name=alias, value=alias) for alias in TOKEN_SOURCES.keys() if current.lower() in alias.lower()]

@bot.tree.command(name="help", description="Menampilkan daftar semua perintah yang tersedia.")
async def help_command(interaction: discord.Interaction):
    is_bot_admin = interaction.user.id in bot.admin_ids if hasattr(bot, 'admin_ids') else False
    embed = discord.Embed(title="📜 Daftar Perintah Bot", color=discord.Color.gold())
    embed.description = "Berikut adalah perintah yang bisa Anda gunakan."
    embed.add_field(name="</help:0>", value="Menampilkan pesan bantuan ini.", inline=False)
    if is_bot_admin:
        embed.add_field(name="👑 Perintah Admin", value=("**/open_claim**: Membuka sesi klaim.\n"
            "**/close_claim**: Menutup sesi klaim.\n"
            "**/admin_add_token**: Menambah token custom.\n"
            "**/admin_remove_token**: Menghapus token.\n"
            "**/admin_reset_cooldown**: Mereset cooldown pengguna.\n"
            "**/admin_cek_user**: Memeriksa status pengguna.\n"
            "**/admin_add_shared_token**: Menambah token custom dengan durasi.\n"
            "**/list_tokens**: Menampilkan semua token aktif.\n"
            "**/list_sources**: Menampilkan semua sumber token.\n"
            "**/baca_file**: Membaca file dari sumber token.\n"
            "**/show_config**: Menampilkan konfigurasi channel.\n"
            "**/serverlist**: Menampilkan daftar server bot."), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- PERINTAH ADMIN ---
@bot.tree.command(name="open_claim", description="ADMIN: Membuka sesi klaim untuk sumber token tertentu.")
@is_admin()
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def open_claim(interaction: discord.Interaction, alias: str):
    await interaction.response.defer(ephemeral=True)
    if alias.lower() not in TOKEN_SOURCES:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True); return
    if not CLAIM_CHANNEL_ID or not (claim_channel := bot.get_channel(CLAIM_CHANNEL_ID)):
        await interaction.followup.send("❌ `CLAIM_CHANNEL_ID` tidak valid.", ephemeral=True); return

    if bot.close_claim_message:
        try: await bot.close_claim_message.delete()
        except discord.HTTPException: pass
        finally: bot.close_claim_message = None

    bot.current_claim_source_alias = alias.lower()
    embed = discord.Embed(title=f"📝 Sesi Klaim Dibuka: {alias.title()}", description=f"Sesi klaim untuk sumber `{alias.title()}` telah dibuka.", color=discord.Color.green())
    bot.open_claim_message = await claim_channel.send(embed=embed, view=ClaimPanelView(bot))
    await interaction.followup.send(f"✅ Panel klaim untuk `{alias.title()}` dikirim ke {claim_channel.mention}.", ephemeral=True)

@bot.tree.command(name="close_claim", description="ADMIN: Menutup sesi klaim dan mengirim notifikasi.")
@is_admin()
async def close_claim(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not bot.current_claim_source_alias:
        await interaction.followup.send("ℹ️ Tidak ada sesi klaim yang aktif.", ephemeral=True); return
        
    if bot.open_claim_message:
        try: await bot.open_claim_message.delete()
        except discord.HTTPException: pass
        finally: bot.open_claim_message = None
            
    closed_alias = bot.current_claim_source_alias
    bot.current_claim_source_alias = None
    
    if CLAIM_CHANNEL_ID and (claim_channel := bot.get_channel(CLAIM_CHANNEL_ID)):
        embed = discord.Embed(title="🔴 Sesi Klaim Ditutup", description=f"Admin telah menutup sesi klaim untuk `{closed_alias.title()}`.", color=discord.Color.red())
        bot.close_claim_message = await claim_channel.send(embed=embed)
    await interaction.followup.send(f"🔴 Sesi klaim untuk `{closed_alias.title()}` telah ditutup.", ephemeral=True)

@bot.tree.command(name="admin_add_token", description="ADMIN: Menambahkan token custom ke sumber file tertentu.")
@is_admin()
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def admin_add_token(interaction: discord.Interaction, alias: str, token: str):
    await interaction.response.defer(ephemeral=True)
    source_info = TOKEN_SOURCES.get(alias.lower())
    if not source_info:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True); return

    async with bot.github_lock:
        content, sha = get_github_file(source_info["slug"], source_info["path"])
        if token in (content or ""):
            await interaction.followup.send(f"❌ Token `{token}` sudah ada di `{alias}`.", ephemeral=True); return
        
        new_content = (content or "").strip() + f"\n\n{token}\n\n"
        if update_github_file(source_info["slug"], source_info["path"], new_content, sha, f"Admin: Add custom token {token}"):
            await interaction.followup.send(f"✅ Token custom `{token}` ditambahkan ke `{alias}`.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Gagal menambahkan token ke `{alias}`.", ephemeral=True)

@bot.tree.command(name="admin_remove_token", description="ADMIN: Menghapus token dari sumber file tertentu.")
@is_admin()
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def admin_remove_token(interaction: discord.Interaction, alias: str, token: str):
    await interaction.response.defer(ephemeral=True)
    source_info = TOKEN_SOURCES.get(alias.lower())
    if not source_info:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True); return
        
    async with bot.github_lock:
        content, sha = get_github_file(source_info["slug"], source_info["path"])
        if not content or token not in content:
            await interaction.followup.send(f"❌ Token `{token}` tidak ditemukan di `{alias}`.", ephemeral=True); return
            
        lines = [line for line in content.split('\n\n') if line.strip() and line.strip() != token]
        new_content = "\n\n".join(lines) + ("\n\n" if lines else "")
        if update_github_file(source_info["slug"], source_info["path"], new_content, sha, f"Admin: Remove token {token}"):
            await interaction.followup.send(f"✅ Token `{token}` dihapus dari `{alias}`.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Gagal menghapus token dari `{alias}`.", ephemeral=True)

@bot.tree.command(name="admin_add_shared_token", description="ADMIN: Menambahkan token yang bisa dibagikan dengan durasi custom.")
@is_admin()
@app_commands.describe(alias="Alias sumber token.", token="Token yang akan ditambahkan.", durasi="Durasi token (misal: 7d, 24h, 30m).")
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def admin_add_shared_token(interaction: discord.Interaction, alias: str, token: str, durasi: str):
    await interaction.response.defer(ephemeral=True)
    
    # 1. Validasi Input
    source_info = TOKEN_SOURCES.get(alias.lower())
    if not source_info:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True)
        return
        
    try:
        duration_delta = parse_duration(durasi)
    except ValueError as e:
        await interaction.followup.send(f"❌ Format durasi tidak valid: {e}", ephemeral=True)
        return

    # 2. Proses Transaksional
    async with bot.github_lock:
        target_repo_slug = source_info["slug"]
        target_file_path = source_info["path"]
        
        # Langkah 2a: Tambahkan token ke file sumber
        tokens_content, tokens_sha = get_github_file(target_repo_slug, target_file_path)
        if token in (tokens_content or ""):
            await interaction.followup.send(f"❌ Token `{token}` sudah ada di file sumber `{alias}`.", ephemeral=True)
            return
            
        new_tokens_content = (tokens_content or "").strip() + f"\n\n{token}\n\n"
        token_add_success = update_github_file(target_repo_slug, target_file_path, new_tokens_content, tokens_sha, f"Admin: Add shared token {token}")

        if not token_add_success:
            await interaction.followup.send("❌ Gagal menambahkan token ke file sumber. Operasi dibatalkan.", ephemeral=True)
            return

        # Langkah 2b: Tambahkan data token ke claims.json
        claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content if claims_content else '{}')
        
        # Gunakan ID unik untuk token yang bisa dibagikan agar tidak bentrok dengan ID pengguna
        claim_key = f"shared_{token}" 
        if claim_key in claims_data:
            await interaction.followup.send(f"❌ Data untuk token `{token}` sudah ada di database klaim. Hapus manual jika perlu.", ephemeral=True)
            # Rollback karena data sudah ada di claims.json tapi mungkin tidak di tokens.txt
            lines = [line for line in new_tokens_content.split('\\n\\n') if line.strip() and line.strip() != token]
            content_after_removal = "\\n\\n".join(lines) + ("\\n\\n" if lines else "")
            update_github_file(target_repo_slug, target_file_path, content_after_removal, tokens_sha, f"Admin: ROLLBACK shared token {token}")
            return
            
        current_time = datetime.now(timezone.utc)
        expiry_time = current_time + duration_delta
        
        claims_data[claim_key] = {
            "last_claim_timestamp": current_time.isoformat(),
            "current_token": token,
            "token_expiry_timestamp": expiry_time.isoformat(),
            "source_alias": alias.lower(),
            "is_shared": True # Penanda opsional
        }
        
        claim_db_update_success = update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, json.dumps(claims_data, indent=4), claims_sha, f"Admin: Add data for shared token {token}")
        
        # Langkah 2c: Rollback jika penyimpanan database gagal
        if not claim_db_update_success:
            print(f"KRITIS: Gagal menyimpan data klaim untuk token shared '{token}'. Melakukan rollback.")
            current_tokens_content_rb, current_tokens_sha_rb = get_github_file(target_repo_slug, target_file_path)
            if current_tokens_content_rb and token in current_tokens_content_rb:
                lines = [line for line in current_tokens_content_rb.split('\\n\\n') if line.strip() and line.strip() != token]
                content_after_removal = "\\n\\n".join(lines) + ("\\n\\n" if lines else "")
                rollback_success = update_github_file(target_repo_slug, target_file_path, content_after_removal, current_tokens_sha_rb, f"Admin: ROLLBACK shared token {token}")
                print(f"Status Rollback: {'Berhasil' if rollback_success else 'Gagal'}")
            
            await interaction.followup.send("❌ Gagal menyimpan data token ke database. Token di file sumber telah dihapus kembali.", ephemeral=True)
            return

    # 3. Kirim pesan sukses
    await interaction.followup.send(f"✅ Token `{token}` berhasil ditambahkan ke `{alias}` dan akan aktif selama `{durasi}`.", ephemeral=True)

@bot.tree.command(name="list_sources", description="ADMIN: Menampilkan semua sumber token yang terkonfigurasi.")
@is_admin()
async def list_sources(interaction: discord.Interaction):
    embed = discord.Embed(title="🔧 Konfigurasi Sumber Token", color=discord.Color.purple())
    if not TOKEN_SOURCES:
        embed.description = "Variabel `TOKEN_SOURCES` belum diatur."
    else:
        for alias, info in TOKEN_SOURCES.items():
            embed.add_field(name=f"Alias: `{alias.title()}`", value=f"**Repo:** `{info['slug']}`\n**File:** `{info['path']}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="baca_file", description="ADMIN: Membaca konten file dari sumber token.")
@is_admin()
@app_commands.autocomplete(alias=source_alias_autocomplete)
async def baca_file(interaction: discord.Interaction, alias: str):
    await interaction.response.defer(ephemeral=True)
    source_info = TOKEN_SOURCES.get(alias.lower())
    if not source_info:
        await interaction.followup.send(f"❌ Alias `{alias}` tidak valid.", ephemeral=True); return
        
    content, _ = get_github_file(source_info["slug"], source_info["path"])
    if content is None:
        await interaction.followup.send(f"❌ File tidak ditemukan di `{alias}`.", ephemeral=True); return
        
    content_to_show = content[:1900] + "\n... (dipotong)" if len(content) > 1900 else content
    embed = discord.Embed(title=f"📄 Konten dari `{alias}`", description=f"```\n{content_to_show or '[File Kosong]'}\n```", color=discord.Color.blue())
    embed.set_footer(text=f"Repo: {source_info['slug']}, File: {source_info['path']}")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="admin_reset_cooldown", description="ADMIN: Mereset seluruh data klaim untuk pengguna.")
@is_admin()
async def admin_reset_cooldown(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    user_id = str(user.id)
    async with bot.github_lock:
        claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        claims_data = json.loads(claims_content if claims_content else '{}')
        if user_id not in claims_data:
            await interaction.followup.send(f"ℹ️ {user.mention} belum pernah klaim.", ephemeral=True); return
        
        del claims_data[user_id]
            
        if update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, json.dumps(claims_data, indent=4), claims_sha, f"Admin: Reset data for {user.name}"):
            await interaction.followup.send(f"✅ Seluruh data klaim untuk {user.mention} berhasil direset.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Gagal mereset data untuk {user.mention}.", ephemeral=True)

@bot.tree.command(name="admin_cek_user", description="ADMIN: Memeriksa status token dan cooldown pengguna.")
@is_admin()
async def admin_cek_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
    claims_data = json.loads(claims_content if claims_content else '{}')

    if str(user.id) not in claims_data:
        await interaction.followup.send(f"**{user.display_name}** belum pernah klaim.", ephemeral=True); return
    
    user_data = claims_data[str(user.id)]
    embed = discord.Embed(title=f"🔍 Status Token - {user.display_name}", color=discord.Color.orange())
    
    if 'current_token' in user_data and 'token_expiry_timestamp' in user_data and datetime.fromisoformat(user_data["token_expiry_timestamp"]) > datetime.now(timezone.utc):
        embed.add_field(name="Token Aktif", value=f"`{user_data['current_token']}`", inline=False)
        embed.add_field(name="Sumber", value=f"`{user_data.get('source_alias', 'N/A').title()}`", inline=True)
        expiry_time = datetime.fromisoformat(user_data["token_expiry_timestamp"])
        embed.add_field(name="Kedaluwarsa", value=f"{expiry_time.strftime('%d %b %Y, %H:%M')} UTC", inline=True)
    else:
        embed.description = "Pengguna tidak memiliki token aktif."

    if 'last_claim_timestamp' in user_data:
        last_claim_time = datetime.fromisoformat(user_data["last_claim_timestamp"])
        next_claim_time = last_claim_time + timedelta(days=7)
        embed.add_field(name="Klaim Terakhir", value=last_claim_time.strftime('%d %b %Y, %H:%M UTC'), inline=False)
        if datetime.now(timezone.utc) < next_claim_time:
            embed.add_field(name="Bisa Klaim Lagi", value=next_claim_time.strftime('%d %b %Y, %H:%M UTC'), inline=False)
        else:
            embed.add_field(name="Bisa Klaim Lagi", value="Sekarang", inline=False)
    else:
        embed.add_field(name="Cooldown Klaim", value="Pengguna tidak dalam masa cooldown.", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="list_tokens", description="ADMIN: Menampilkan daftar semua token aktif dari database.")
@is_admin()
async def list_tokens(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    claims_content, _ = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
    claims_data = json.loads(claims_content if claims_content else '{}')

    if not claims_data:
        await interaction.followup.send("Tidak ada data klaim.", ephemeral=True); return

    embed = discord.Embed(title="Daftar Token Aktif", color=discord.Color.blue())
    active_tokens = []
    for user_id, data in claims_data.items():
        if 'current_token' in data and 'token_expiry_timestamp' in data and datetime.fromisoformat(data["token_expiry_timestamp"]) > datetime.now(timezone.utc):
            try: 
                user = await bot.fetch_user(int(user_id))
                username = str(user)
            except (discord.NotFound, ValueError): 
                username = f"ID: {user_id}"
            active_tokens.append(f"**{username}**: `{data['current_token']}` (Sumber: {data.get('source_alias', 'N/A').title()})")
    embed.description = "\n".join(active_tokens) if active_tokens else "Tidak ada token yang sedang aktif."
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="show_config", description="ADMIN: Menampilkan channel yang terkonfigurasi.")
@is_admin()
async def show_config(interaction: discord.Interaction):
    embed = discord.Embed(title="🔧 Konfigurasi Channel Bot", color=discord.Color.teal())
    embed.add_field(name="Channel Klaim", value=f"<#{CLAIM_CHANNEL_ID}>" if CLAIM_CHANNEL_ID else "Belum diatur", inline=False)
    embed.add_field(name="Channel Role", value=f"<#{ROLE_REQUEST_CHANNEL_ID}>" if ROLE_REQUEST_CHANNEL_ID else "Belum diatur", inline=False)
    embed.set_footer(text="Diatur melalui Environment Variables di Railway.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="serverlist", description="ADMIN: Menampilkan daftar semua server tempat bot ini berada.")
@is_admin()
async def serverlist(interaction: discord.Interaction):
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

    app_info = await bot.application_info()
    bot.owner_id = app_info.owner.id
    try:
        bot.admin_ids = {int(uid.strip()) for uid in ADMIN_USER_IDS_STR.split(',')} if ADMIN_USER_IDS_STR else set()
        bot.admin_ids.add(bot.owner_id) 
    except ValueError:
        print("FATAL ERROR: Format ADMIN_USER_IDS tidak valid. Pastikan hanya angka dan koma.")
        exit()
    
    async with bot.github_lock:
        print("Mengecek kesehatan claims.json...")
        claims_content, claims_sha = get_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH)
        if claims_content is None:
            print("claims.json tidak ditemukan, membuat file baru...")
            update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, "{}", None, "Bot: Initialize claims.json")
        else:
            try:
                if not claims_content.strip(): raise json.JSONDecodeError("File is empty", claims_content, 0)
                json.loads(claims_content)
            except json.JSONDecodeError:
                print("claims.json rusak atau kosong, menginisialisasi ulang file...")
                update_github_file(PRIMARY_REPO, CLAIMS_FILE_PATH, "{}", claims_sha, "Bot: Re-initialize corrupted claims.json")
    print("Health check selesai, claims.json siap digunakan.")

    bot.add_view(ClaimPanelView(bot))
    await bot.tree.sync()
    print(f'Bot telah login sebagai {bot.user.name}')
    print(f'Owner ID: {bot.owner_id}')
    print(f'Daftar Admin ID: {bot.admin_ids}')
    print(f'Repo data utama (claims): {PRIMARY_REPO}')
    print(f'Server IDs: {ALLOWED_GUILD_IDS}')
    print(f'Sumber Token Terkonfigurasi: {TOKEN_SOURCES}')

@bot.event
async def on_guild_join(guild):
    if guild.id not in ALLOWED_GUILD_IDS:
        print(f"Bot otomatis keluar dari server tidak sah: {guild.name} ({guild.id})")
        await guild.leave()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ **Akses Ditolak!** Perintah ini hanya untuk admin bot.", ephemeral=True)
    else:
        print(f"Error tidak terduga pada perintah '{interaction.command.name}': {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Terjadi error internal saat menjalankan perintah.", ephemeral=True)

# Logika on_message untuk role otomatis (tidak diubah)
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild: return
    if not ROLE_REQUEST_CHANNEL_ID or message.channel.id != ROLE_REQUEST_CHANNEL_ID: return
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

bot.run(DISCORD_TOKEN)


