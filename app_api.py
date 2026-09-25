import datetime
import json
import logging
import os
import sqlite3
import threading
import time
import pandas as pd
import pytz
import requests
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed

from clan import render_clan_metrics_tab
from drive_uploader import upload_db_to_drive, download_latest_db_from_drive

# Конфігурація API
try:
    from api_config import (
        API_CLIENT_ID,
        API_CLIENT_SECRET,
        API_BASE_URL,
        API_BEARER_TOKEN_SEMI,
        API_BEARER_TOKEN,
    )
except ImportError:
    API_CLIENT_ID = st.secrets.get("API_CLIENT_ID", "")
    API_CLIENT_SECRET = st.secrets.get("API_CLIENT_SECRET", "")
    API_BASE_URL = st.secrets.get("API_BASE_URL", "https://eapi.stalzone.com")
    API_BEARER_TOKEN_SEMI = st.secrets.get("API_BEARER_TOKEN_SEMI", "")
    API_BEARER_TOKEN = st.secrets.get("API_BEARER_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("StalzoneApiApp")

KYIV_TZ = pytz.timezone("Europe/Kyiv")


# ==========================================
# АВТО-СИНХРОНІЗАЦІЯ ПРИ СТАРТІ / ВІДКАТІ
# ==========================================
def auto_sync_db_on_startup():
    """
    Перевіряє стан локальної БД при старті сесії.
    Якщо локальна база відкатана або порожня, автоматично викачує останню версію з Google Drive.
    """
    if "db_auto_synced" not in st.session_state:
        st.session_state.db_auto_synced = True
        db = DatabaseManager()
        try:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM player_groups")
                groups_cnt = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM daily_snapshots")
                snaps_cnt = cursor.fetchone()[0]

            # Якщо база повністю порожня (Streamlit скинув стан)
            if groups_cnt == 0 and snaps_cnt == 0:
                logger.info("⚠️ Виявлено порожню/відкатану БД. Виконується авто-відновлення з Google Drive...")
                if download_latest_db_from_drive():
                    logger.info("✅ Автоматичне відновлення з Google Drive пройшло успішно.")
        except Exception as e:
            logger.error(f"❌ Помилка при перевірці авто-синхронізації БД: {e}")


# ==========================================
# 0. СИСТЕМА КЕШУВАННЯ API (15-СЕКУНДНИЙ БУФЕР)
# ==========================================
class APICache:
    """Thread-safe кеш з TTL в 15 секунд для запобігання лимитам API."""

    def __init__(self, ttl_seconds: int = 15):
        self.ttl = ttl_seconds
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, nickname: str, region: str):
        key = (nickname.strip().lower(), region.strip().lower())
        with self._lock:
            if key in self._cache:
                ts, data = self._cache[key]
                if time.time() - ts < self.ttl:
                    return data
                else:
                    del self._cache[key]
            return None

    def set(self, nickname: str, region: str, data: dict):
        key = (nickname.strip().lower(), region.strip().lower())
        with self._lock:
            self._cache[key] = (time.time(), data)


global_api_cache = APICache(ttl_seconds=15)


# ==========================================
# 1. СИСТЕМА АВТОРИЗАЦІЇ (AUTH)
# ==========================================
def check_authentication() -> bool:
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
        st.session_state.username = ""
        st.session_state.role = "user"

    if not st.session_state.authenticated:
        auth_file = "users.json"
        users_db = {}

        if os.path.exists(auth_file):
            try:
                with open(auth_file, "r", encoding="utf-8") as f:
                    raw_db = json.load(f)

                for user, data in raw_db.items():
                    if isinstance(data, dict):
                        users_db[user] = data
                    else:
                        role = "admin" if user in ["admin", "1111"] else "user"
                        users_db[user] = {"password": str(data), "role": role}
            except Exception as e:
                st.error(f"Помилка зчитування users.json: {e}")

        elif "users" in st.secrets:
            users_db = dict(st.secrets["users"])

        if not users_db:
            users_db = {
                "guest": {"password": "guestpassword123", "role": "user"}
            }

        _, col_center, _ = st.columns([1, 1.2, 1])
        with col_center:
            st.markdown("<h2 style='text-align: center;'>🔐 Вхід до Stalzone Analytic Review</h2>",
                        unsafe_allow_html=True)
            username = st.text_input("Логін")
            password = st.text_input("Пароль", type="password")

            if st.button("Увійти", type="primary", width='stretch'):
                user_entry = users_db.get(username)

                if user_entry and str(user_entry.get("password")) == str(password):
                    st.session_state.authenticated = True
                    st.session_state.username = username
                    st.session_state.role = user_entry.get("role", "user")
                    st.success("Успішний вхід!")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("Невірний логін або пароль")
        return False
    return True


# ==========================================
# 2. API КЛІЄНТ ДЛЯ ОТРИМАННЯ ДАНИХ (eAPI)
# ==========================================
class StalzoneApiClient:
    def __init__(self, client_id: str = "", client_secret: str = "", region: str = "EU"):
        self.region = region.lower()
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = API_BEARER_TOKEN_SEMI if API_BEARER_TOKEN_SEMI else None
        self.token_expires_at = time.time() + 31536000 if self.access_token else 0

    def _authenticate(self) -> bool:
        if self.access_token and time.time() < self.token_expires_at - 60:
            return True

        logger.info("🔑 Авторизація в API Stalzone...")
        try:
            res = requests.post(
                f"{API_BASE_URL.rstrip('/')}/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret
                },
                timeout=10
            )

            if res.status_code == 200:
                data = res.json()
                self.access_token = data.get("access_token")
                self.token_expires_at = time.time() + data.get("expires_in", 3600)
                logger.info("✅ API Токен успішно отримано!")
                return True
            else:
                logger.error(f"❌ Помилка авторизації API: HTTP {res.status_code}")
                return False
        except Exception as e:
            logger.error(f"❌ Мережева помилка авторизації: {e}")
            return False

    def fetch_player_stats(self, nickname: str) -> dict:
        cached_result = global_api_cache.get(nickname, self.region)
        if cached_result:
            return cached_result

        empty_stats = {
            "nickname": nickname, "status": "Error",
            "kills": 0, "assists": 0, "deaths": 0, "playtime": 0,
            "damage_dealt": 0, "damage_recv": 0, "grenades": 0,
            "headshots": 0, "bodyshots": 0, "limbshots": 0
        }

        if not self._authenticate():
            return empty_stats

        safe_nick = requests.utils.quote(nickname.strip())
        url = f"{API_BASE_URL.rstrip('/')}/{self.region.upper()}/character/by-name/{safe_nick}/profile"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                stats_raw = data.get("stats", [])
                stats_map = {item["id"]: item.get("value", 0) for item in stats_raw if
                             isinstance(item, dict) and "id" in item}

                playtime_ms = stats_map.get("pla-tim", 0)
                playtime_min = round(playtime_ms / (1000 * 60))

                result = {
                    "nickname": data.get("username", nickname),
                    "status": "OK",
                    "kills": stats_map.get("kil", 0),
                    "assists": stats_map.get("ast", 0),
                    "deaths": stats_map.get("dea", 0),
                    "playtime": playtime_min,
                    "damage_dealt": round(stats_map.get("dam-dea-pla", 0.0), 2),
                    "damage_recv": round(stats_map.get("dam-rec-pla", 0.0), 2),
                    "grenades": stats_map.get("gre-thr", 0),
                    "headshots": stats_map.get("sho-hea", 0),
                    "bodyshots": stats_map.get("sho-bod", 0),
                    "limbshots": stats_map.get("sho-lim", 0)
                }
                global_api_cache.set(nickname, self.region, result)
                return result
            else:
                logger.error(f"⚠️ Помилка отримання профілю {nickname}: HTTP {res.status_code}")
                return empty_stats
        except Exception as e:
            logger.error(f"❌ Мережева помилка для {nickname}: {e}")
            return empty_stats

    def fetch_clan_members_by_player(self, nickname: str) -> tuple[str, list]:
        if not self._authenticate():
            return None, []

        nickname_clean = nickname.strip()
        safe_nick = requests.utils.quote(nickname_clean)
        url = f"{API_BASE_URL.rstrip('/')}/{self.region.upper()}/character/by-name/{safe_nick}/profile"

        headers_full = {
            "Authorization": f"Bearer {API_BEARER_TOKEN if API_BEARER_TOKEN else self.access_token}",
            "Content-Type": "application/json"
        }

        try:
            res = requests.get(url, headers=headers_full, timeout=15)
            if res.status_code != 200:
                return None, []

            data = res.json()
            clan_info = data.get("clan")
            clan_id = None
            clan_name = "Клан"

            if isinstance(clan_info, dict):
                clan_id = clan_info.get("id") or clan_info.get("clan_id") or clan_info.get("clanId")
                clan_name = clan_info.get("name") or clan_info.get("clan_name") or clan_info.get("title") or "Клан"
                if not clan_id and isinstance(clan_info.get("info"), dict):
                    clan_id = clan_info["info"].get("id")
                    clan_name = clan_info["info"].get("name", clan_name)
            elif isinstance(clan_info, str):
                clan_id = clan_info

            if not clan_id:
                clan_id = data.get("clan_id") or data.get("clanId") or data.get("guild_id")
                clan_name = data.get("clan_name") or data.get("clanName") or clan_name

            if not clan_id:
                return None, []

            endpoints_to_try = [
                f"{API_BASE_URL.rstrip('/')}/{self.region.upper()}/clan/{clan_id}/members",
                f"{API_BASE_URL.rstrip('/')}/{self.region.upper()}/clan/info/{clan_id}",
                f"{API_BASE_URL.rstrip('/')}/{self.region.upper()}/clan/{clan_id}/info",
                f"{API_BASE_URL.rstrip('/')}/{self.region.upper()}/clans/{clan_id}/members"
            ]

            for clan_url in endpoints_to_try:
                clan_res = requests.get(clan_url, headers=headers_full, timeout=15)
                if clan_res.status_code == 200:
                    members_data = clan_res.json()
                    if isinstance(members_data, dict):
                        for key in ["members", "data", "items", "players", "characters"]:
                            if key in members_data and isinstance(members_data[key], list):
                                members_data = members_data[key]
                                break

                    members = []
                    if isinstance(members_data, list):
                        for m in members_data:
                            if isinstance(m, str):
                                members.append(m)
                            elif isinstance(m, dict):
                                name = (
                                        m.get("username") or m.get("name") or m.get("nickname") or
                                        m.get("characterName") or
                                        (m.get("character") if isinstance(m.get("character"), str) else None) or
                                        (m.get("character", {}).get("name") if isinstance(m.get("character"),
                                                                                          dict) else None)
                                )
                                if name:
                                    members.append(name)

                    if members:
                        return clan_name, members

            return clan_name, []
        except Exception as e:
            logger.error(f"❌ Помилка отримання складу клану: {e}")
            return None, []

    def fetch_bulk_player_stats(self, nicknames: list, batch_size: int = 10, progress_callback=None) -> dict:
        if not nicknames:
            return {}

        results = {}
        total_count = len(nicknames)
        completed_count = 0
        workers = min(max(1, batch_size), len(nicknames))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_nick = {executor.submit(self.fetch_player_stats, nick): nick for nick in nicknames}

            for future in as_completed(future_to_nick):
                nick = future_to_nick[future]
                try:
                    stats = future.result()
                    results[nick] = stats
                except Exception as e:
                    logger.error(f"❌ Помилка отримання статистики для {nick}: {e}", exc_info=True)
                    results[nick] = {
                        "nickname": nick, "status": "Error",
                        "kills": 0, "assists": 0, "deaths": 0, "playtime": 0,
                        "damage_dealt": 0, "damage_recv": 0, "grenades": 0,
                        "headshots": 0, "bodyshots": 0, "limbshots": 0
                    }

                completed_count += 1

                if progress_callback:
                    try:
                        progress_callback(completed_count, total_count, nick)
                    except Exception as cb_err:
                        logger.error(f"⚠️ Помилка виклику progress_callback для {nick}: {cb_err}")

        return results


# ==========================================
# 3. МЕНЕДЖЕР БД (SQLite)
# ==========================================
class DatabaseManager:
    def __init__(self, db_path: str = "stalzone_stats.db"):
        self.db_path = db_path
        self._init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS player_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT UNIQUE NOT NULL,
                    nicknames TEXT NOT NULL,
                    owner TEXT DEFAULT 'admin'
                )
            """)

            cursor.execute("PRAGMA table_info(player_groups)")
            columns = [col[1] for col in cursor.fetchall()]
            if "owner" not in columns:
                try:
                    cursor.execute("ALTER TABLE player_groups ADD COLUMN owner TEXT DEFAULT 'admin'")
                except sqlite3.OperationalError:
                    pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_type TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    kills INTEGER, assists INTEGER, deaths INTEGER, playtime INTEGER,
                    damage_dealt REAL, damage_recv REAL, grenades INTEGER,
                    headshots INTEGER, bodyshots INTEGER, limbshots INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS squads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    squad_name TEXT UNIQUE NOT NULL,
                    group_name TEXT NOT NULL,
                    max_slots INTEGER NOT NULL,
                    members TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()

    def save_group(self, name: str, nicknames: list, owner: str = "admin"):
        nicks_str = ",".join([n.strip() for n in nicknames if n.strip()])
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO player_groups (group_name, nicknames, owner) VALUES (?, ?, ?)
                ON CONFLICT(group_name) DO UPDATE SET nicknames=excluded.nicknames, owner=excluded.owner
            """, (name, nicks_str, owner))
            conn.commit()

    def delete_group(self, name: str, owner: str = "admin"):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if owner == "admin":
                cursor.execute("DELETE FROM player_groups WHERE group_name = ?", (name,))
            else:
                cursor.execute("DELETE FROM player_groups WHERE group_name = ? AND owner = ?", (name, owner))
            conn.commit()

    def get_groups(self, owner: str = "admin") -> dict:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if owner == "admin":
                cursor.execute("SELECT group_name, nicknames FROM player_groups WHERE owner = 'admin' OR owner IS NULL")
            else:
                cursor.execute("SELECT group_name, nicknames FROM player_groups WHERE owner = ?", (owner,))
            rows = cursor.fetchall()
            return {row[0]: [n.strip() for n in row[1].split(",") if n.strip()] for row in rows}

    def get_global_groups(self) -> dict:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT group_name, nicknames FROM player_groups WHERE owner = 'admin' OR owner IS NULL")
            rows = cursor.fetchall()
            return {row[0]: [n.strip() for n in row[1].split(",") if n.strip()] for row in rows}

    def set_setting(self, key: str, value: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default

    def save_squad(self, squad_name: str, group_name: str, max_slots: int, members: list):
        m_str = ",".join(members)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO squads (squad_name, group_name, max_slots, members) VALUES (?, ?, ?, ?)
                ON CONFLICT(squad_name) DO UPDATE SET group_name=excluded.group_name, max_slots=excluded.max_slots, members=excluded.members
            """, (squad_name, group_name, max_slots, m_str))
            conn.commit()

    def get_squads(self, group_name: str = None) -> dict:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if group_name:
                cursor.execute("SELECT squad_name, max_slots, members FROM squads WHERE group_name = ?", (group_name,))
            else:
                cursor.execute("SELECT squad_name, max_slots, members FROM squads")
            rows = cursor.fetchall()
            res = {}
            for r in rows:
                res[r[0]] = {
                    "max_slots": r[1],
                    "members": [m.strip() for m in r[2].split(",") if m.strip()]
                }
            return res

    def delete_squad(self, squad_name: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM squads WHERE squad_name = ?", (squad_name,))
            conn.commit()

    def clear_daily_data(self, snapshot_type: str = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if snapshot_type:
                cursor.execute("DELETE FROM daily_snapshots WHERE snapshot_type = ?", (snapshot_type,))
            else:
                cursor.execute("DELETE FROM daily_snapshots")
            conn.commit()

    def save_snapshot(self, snapshot_type: str, stats: dict, custom_timestamp: str = None):
        """Зберігає або перезаписує зріз для конкретного гравця та типу зрізу."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Гарантуємо, що для одного і того ж типу зрізу і гравця не буде дублікатів
            cursor.execute(
                "DELETE FROM daily_snapshots WHERE snapshot_type = ? AND nickname = ?",
                (snapshot_type, stats["nickname"])
            )
            if custom_timestamp:
                cursor.execute("""
                    INSERT INTO daily_snapshots 
                    (snapshot_type, nickname, kills, assists, deaths, playtime, damage_dealt, damage_recv, grenades, headshots, bodyshots, limbshots, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    snapshot_type, stats["nickname"], stats["kills"], stats["assists"],
                    stats["deaths"], stats["playtime"], stats["damage_dealt"],
                    stats["damage_recv"], stats["grenades"], stats["headshots"],
                    stats["bodyshots"], stats["limbshots"], custom_timestamp
                ))
            else:
                cursor.execute("""
                    INSERT INTO daily_snapshots 
                    (snapshot_type, nickname, kills, assists, deaths, playtime, damage_dealt, damage_recv, grenades, headshots, bodyshots, limbshots)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    snapshot_type, stats["nickname"], stats["kills"], stats["assists"],
                    stats["deaths"], stats["playtime"], stats["damage_dealt"],
                    stats["damage_recv"], stats["grenades"], stats["headshots"],
                    stats["bodyshots"], stats["limbshots"]
                ))
            conn.commit()

    def get_snapshot(self, snapshot_type: str) -> dict:
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM daily_snapshots WHERE snapshot_type = ?", (snapshot_type,))
            rows = cursor.fetchall()
            return {row["nickname"]: dict(row) for row in rows}

    def get_historical_snapshot(self, nickname: str, days_ago: int = 0) -> dict:
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if days_ago == 0:
                cursor.execute("SELECT * FROM daily_snapshots WHERE nickname = ? ORDER BY timestamp DESC LIMIT 1",
                               (nickname,))
            else:
                target_date = (datetime.datetime.now(KYIV_TZ) - datetime.timedelta(days=days_ago)).strftime(
                    '%Y-%m-%d %H:%M:%S')
                cursor.execute("""
                    SELECT * FROM daily_snapshots 
                    WHERE nickname = ? AND snapshot_type = '00:00' AND timestamp <= ?
                    ORDER BY timestamp DESC LIMIT 1
                """, (nickname, target_date))
            row = cursor.fetchone()
            return dict(row) if row else {}


# ==========================================
# 4. АВТОМАТИЧНИЙ ФОНОВИЙ ПЛАНУВАЛЬНИК
# ==========================================
def run_background_scheduler():
    logger.info("⏰ Фоновий автозбір даних за розкладом запущено!")
    executed_tasks = {}

    while True:
        try:
            now_kyiv = datetime.datetime.now(KYIV_TZ)
            today_str = now_kyiv.strftime("%Y-%m-%d")
            hour = now_kyiv.hour
            minute = now_kyiv.minute
            weekday = now_kyiv.weekday()

            target_cw_hour, target_cw_min = (22, 0) if weekday in [6, 0, 1, 2] else (22, 15)

            db = DatabaseManager()
            groups = db.get_global_groups()
            sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

            if sorted_groups:
                api_client = StalzoneApiClient(API_CLIENT_ID, API_CLIENT_SECRET, "EU")

                if hour == 0 and minute == 0 and (today_str, "00:00") not in executed_tasks:
                    logger.info("⏰ [SCHEDULE] Виконання зрізу 00:00...")
                    db.clear_daily_data("00:00")
                    for g_name, g_nicks in sorted_groups:
                        for nick in g_nicks:
                            stats = api_client.fetch_player_stats(nick)
                            if stats.get("status") == "OK":
                                db.save_snapshot("00:00", stats)
                    executed_tasks[(today_str, "00:00")] = True
                    logger.info("✅ [SCHEDULE] Зріз 00:00 завершено.")

                cw_blocks_str = db.get_setting("cw_scan_blocks", "")
                cw_blocks = json.loads(cw_blocks_str) if cw_blocks_str else []
                if not cw_blocks and sorted_groups:
                    cw_blocks = [sorted_groups[0][0]]

                # --- ПОЧАТОК НОВОГО КВ ЦИКЛУ (20:59 / 21:00) ---
                if hour == 20 and minute == 59 and (today_str, "21:00") not in executed_tasks:
                    logger.info("⏰ [SCHEDULE] Запуск нового КВ циклу: очищення старих КВ зрізів і збір 21:00...")
                    # Очищаємо попередні зрізи КВ (21:00 та CW_END), щоб почати новий день начисто
                    db.clear_daily_data("21:00")
                    db.clear_daily_data("CW_END")
                    db.clear_daily_data("CUSTOM_START")
                    db.clear_daily_data("CUSTOM_END")

                    for g_name in cw_blocks:
                        if g_name in groups:
                            for nick in groups[g_name]:
                                stats = api_client.fetch_player_stats(nick)
                                if stats.get("status") == "OK":
                                    db.save_snapshot("21:00", stats)
                    executed_tasks[(today_str, "21:00")] = True
                    logger.info("✅ [SCHEDULE] Новий зріз 21:00 успішно збережено!")

                target_cw_total_mins = target_cw_hour * 60 + target_cw_min - 1
                curr_total_mins = hour * 60 + minute

                # --- КІНЕЦЬ КВ ЦИКЛУ (CW_END) ---
                if curr_total_mins == target_cw_total_mins and (today_str, "CW_END") not in executed_tasks:
                    logger.info(f"⏰ [SCHEDULE] Виконання зрізу CW_END (запуск о {hour}:{minute:02d})...")
                    db.clear_daily_data("CW_END")
                    for g_name in cw_blocks:
                        if g_name in groups:
                            for nick in groups[g_name]:
                                stats = api_client.fetch_player_stats(nick)
                                if stats.get("status") == "OK":
                                    db.save_snapshot("CW_END", stats)
                    executed_tasks[(today_str, "CW_END")] = True
                    logger.info("✅ [SCHEDULE] Зріз CW_END завершено. Статистика актуальна до наступного КВ.")

            # --- АВТОМАТИЧНЕ ЗБЕРЕЖЕННЯ ТА ПЕРЕДАЧА НА GOOGLE DRIVE О 23:00 ZA КИЄВОМ ---
            if hour == 23 and minute == 0 and (today_str, "23:00_DRIVE") not in executed_tasks:
                logger.info("⏰ [SCHEDULE] Запуск автоматичного збереження та передачі БД на Google Drive (23:00 Kyiv)...")
                if upload_db_to_drive():
                    executed_tasks[(today_str, "23:00_DRIVE")] = True
                    logger.info("✅ [SCHEDULE] Резервну копію БД успішно відправлено на Google Drive!")
                else:
                    logger.error("❌ [SCHEDULE] Помилка відправки резервної копії БД на Google Drive.")

            cutoff = (now_kyiv - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
            for key in list(executed_tasks.keys()):
                if key[0] < cutoff:
                    del executed_tasks[key]

            time.sleep(30)
        except Exception as e:
            logger.error(f"❌ Помилка у фоновому планувальнику: {e}")
            time.sleep(30)


@st.cache_resource
def start_scheduler():
    t = threading.Thread(target=run_background_scheduler, daemon=True)
    t.start()
    return True


# ==========================================
# 5. АНАЛІТИЧНИЙ ДВИГУН ТА ТР (TOTAL POWER)
# ==========================================
class AnalyticsEngine:
    @staticmethod
    def calc_ratio(num: float, den: float) -> float:
        if den == 0:
            return float(num) if num > 0 else 0.0
        return round(num / den, 2)

    @staticmethod
    def get_color_style(val: float) -> str:
        if val < 0.8:
            return "color: #ff4b4b; font-weight: bold;"
        elif 0.8 <= val <= 1.5:
            return "color: #00c853; font-weight: bold;"
        else:
            return "color: #00e5ff; font-weight: bold;"

    @staticmethod
    def get_tp_diff_style(val: float) -> str:
        if val > 0:
            return "color: #00c853; font-weight: bold;"
        elif val < 0:
            return "color: #ff4b4b; font-weight: bold;"
        return "color: #888888;"

    @staticmethod
    def calc_tp(s: dict) -> float:
        if not s:
            return 0.0
        K, A = s.get("kills", 0), s.get("assists", 0)
        D = max(s.get("deaths", 0), 1)
        DD = s.get("damage_dealt", 0)
        DR = max(s.get("damage_recv", 0), 1)
        HS, BS, LS = s.get("headshots", 0), s.get("bodyshots", 0), s.get("limbshots", 0)

        total = HS + BS + LS
        R1 = max(((K + A) / D) / 1.1, 0.0001)
        R2 = max((K / D) / 0.8, 0.0001)
        R3 = max((DD / DR) / 1.0, 0.0001)

        if total > 0:
            ch, cb, cl = 2.0, 1.0, 0.5
            baseline = ch * 0.3 + cb * 0.5 + cl * 0.2
            R4 = max((ch * HS + cb * BS + cl * LS) / total / baseline, 0.0001)
        else:
            R4 = 1.0

        w1, w2, w3, w4 = 0.35, 0.15, 0.30, 0.20
        tp = round(100 * (R1 ** w1) * (R2 ** w2) * (R3 ** w3) * (R4 ** w4), 1)
        return tp

    @classmethod
    def get_tier_letter(cls, tp: float) -> str:
        if tp > 150:
            return "S"
        elif 140 <= tp <= 150:
            return "A"
        elif 130 <= tp < 140:
            return "B"
        elif 120 <= tp < 130:
            return "C"
        elif 110 <= tp < 120:
            return "D"
        elif 90 <= tp < 110:
            return "E"
        else:
            return "F"

    @classmethod
    def compute_cw_metrics(cls, start_stats: dict, end_stats: dict) -> dict:
        d_kills = max(0, end_stats.get("kills", 0) - start_stats.get("kills", 0))
        d_assists = max(0, end_stats.get("assists", 0) - start_stats.get("assists", 0))
        d_deaths = max(0, end_stats.get("deaths", 0) - start_stats.get("deaths", 0))

        kd = cls.calc_ratio(d_kills, d_deaths)
        k_a_d = cls.calc_ratio(d_kills + d_assists, d_deaths)

        d_grenades = max(0, end_stats.get("grenades", 0) - start_stats.get("grenades", 0))

        d_head = max(0, end_stats.get("headshots", 0) - start_stats.get("headshots", 0))
        d_body = max(0, end_stats.get("bodyshots", 0) - start_stats.get("bodyshots", 0))
        d_limb = max(0, end_stats.get("limbshots", 0) - start_stats.get("limbshots", 0))
        total_shots = d_head + d_body + d_limb

        acc_head = cls.calc_ratio(d_head * 100, total_shots)
        acc_body = cls.calc_ratio(d_body * 100, total_shots)
        acc_limb = cls.calc_ratio(d_limb * 100, total_shots)

        d_dmg_dealt = max(0, end_stats.get("damage_dealt", 0) - start_stats.get("damage_dealt", 0))
        d_dmg_recv = max(0, end_stats.get("damage_recv", 0) - start_stats.get("damage_recv", 0))
        un_up = cls.calc_ratio(d_dmg_dealt, d_dmg_recv)

        delta_stats = {
            "nickname": end_stats.get("nickname", "Unknown"),
            "kills": d_kills,
            "assists": d_assists,
            "deaths": d_deaths,
            "damage_dealt": d_dmg_dealt,
            "damage_recv": d_dmg_recv,
            "headshots": d_head,
            "bodyshots": d_body,
            "limbshots": d_limb
        }
        slice_tp = cls.calc_tp(delta_stats)
        total_tp = cls.calc_tp(end_stats)

        tp_diff_pct = round(((slice_tp - total_tp) / total_tp) * 100, 1) if total_tp > 0 else 0.0

        return {
            "nickname": end_stats.get("nickname", "Unknown"),
            "cw_score": f"{d_kills} / {d_deaths} / {d_assists}",
            "slice_tp": slice_tp,
            "total_tp": total_tp,
            "tp_diff_pct": tp_diff_pct,
            "kd": kd,
            "kad": k_a_d,
            "grenades": d_grenades,
            "shots": f"Г: {d_head} | Т: {d_body} | К: {d_limb}",
            "accuracy": f"🎯 {acc_head}% / 🧍 {acc_body}% / 🦵 {acc_limb}%",
            "damage": f"+{round(d_dmg_dealt, 1)} / -{round(d_dmg_recv, 1)}",
            "un_up": un_up
        }

    @staticmethod
    def calculate_clan_avg_grenades(db: DatabaseManager, group_nicks: list, live_stats: dict = None) -> float:
        snap_custom_start = db.get_snapshot("CUSTOM_START")
        snap_custom_end = db.get_snapshot("CUSTOM_END")

        snap_00 = db.get_snapshot("00:00")
        snap_21 = db.get_snapshot("21:00")
        snap_cw = db.get_snapshot("CW_END")

        use_custom = bool(snap_custom_start and snap_custom_end)
        active_players_data = {}

        for nick in group_nicks:
            if use_custom:
                start = snap_custom_start.get(nick, {})
                end = snap_custom_end.get(nick, {})
            else:
                start = snap_21.get(nick) or snap_00.get(nick) or {}
                end = (live_stats.get(nick) if live_stats else None) or snap_cw.get(nick) or {}

            if not start or not end:
                continue

            m = AnalyticsEngine.compute_cw_metrics(start, end)
            slice_tp = m.get("slice_tp", 0.0)
            d_grenades = m.get("grenades", 0)

            if slice_tp > 10.0:
                active_players_data[nick] = (slice_tp, d_grenades)

        sorted_active = sorted(active_players_data.items(), key=lambda x: x[1][0], reverse=True)[:25]

        if not sorted_active:
            return 0.0

        player_grenades = {nick: data[1] for nick, data in sorted_active}
        total_grenades = sum(player_grenades.values())
        players_count = len(player_grenades)
        avg_grenades = round(total_grenades / players_count, 1)

        return avg_grenades


# ==========================================
# 6. STREAMLIT ІНТЕРФЕЙС
# ==========================================
def main():
    st.set_page_config(page_title="SAR", layout="wide", initial_sidebar_state="expanded")

    if not check_authentication():
        return

    # Автоматичне відновлення актуальної бакап-бази з Google Drive у разі відкату
    auto_sync_db_on_startup()

    username = st.session_state.get("username", "admin")
    user_role = st.session_state.get("role", "user")
    is_admin = (user_role == "admin")

    start_scheduler()

    st.markdown("""
    <style>
    div[data-testid="stRadio"] > label { display: none; }

    .nav-header {
        font-size: 1.1rem;
        font-weight: bold;
        margin-bottom: 8px;
        color: #9D4EDD;
    }

    button[kind="primary"] {
        background-color: #2D143A !important;
        color: #FFFFFF !important;
        border: 2px solid #9D4EDD !important;
        border-radius: 6px !important;
        font-weight: bold !important;
        transition: all 0.3s ease-in-out;
    }
    button[kind="primary"]:hover {
        background-color: #3C1A4D !important;
        border-color: #E0AAFF !important;
        color: #FFFFFF !important;
    }

    span[data-baseweb="tag"] {
        background-color: #2D143A !important;
        border: 1px solid #9D4EDD !important;
    }
    span[data-baseweb="tag"] span {
        color: #FFFFFF !important;
        font-weight: bold !important;
    }
    </style>
    """, unsafe_allow_html=True)

    if "db" not in st.session_state:
        st.session_state.db = DatabaseManager()

    db = st.session_state.db

    if "live_stats" not in st.session_state:
        st.session_state.live_stats = {}

    snap_c_start = db.get_snapshot("CUSTOM_START")
    snap_c_end = db.get_snapshot("CUSTOM_END")

    st.session_state.custom_start = snap_c_start
    st.session_state.custom_end = snap_c_end

    is_tracking_active = bool(st.session_state.custom_start and not st.session_state.custom_end)

    # --- БІЧНА ПАНЕЛЬ ---
    with st.sidebar:
        role_label = "🛡️ Адміністратор / Клан" if is_admin else "👤 Персональний акаунт"
        st.write(f"👤 Користувач: **{username}**")
        st.caption(f"Тип профілю: **{role_label}**")

        if st.button("Вийти", width='stretch'):
            st.session_state.authenticated = False
            st.rerun()

        st.divider()
        st.header("⚙️ Налаштування та Блоки")
        region = st.selectbox("Регіон", ["EU", "RU", "SEA", "NA", "NEA"], index=0)

        st.divider()

        col_btn1, col_btn2 = st.columns(2)

        with col_btn1:
            if st.session_state.live_stats:
                df_export = pd.DataFrame.from_dict(st.session_state.live_stats, orient='index')
                csv_data = df_export.to_csv(index_label="Нікнейм").encode('utf-8')
                file_name = f"stalzone_stats_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            else:
                csv_data = ""
                file_name = "empty.csv"

            st.download_button(
                label="💾 CSV",
                data=csv_data,
                file_name=file_name,
                mime="text/csv",
                help="Зберегти поточний зріз у CSV",
                width='stretch',
                disabled=not bool(st.session_state.live_stats)
            )

        with col_btn2:
            btn_label = "🛑 Кінець" if is_tracking_active else "⏱️ Початок"
            btn_help = "Записати кінець екстренного зрізу" if is_tracking_active else "Записати початок екстренного зрізу"

            if st.button(btn_label, help=btn_help, width='stretch'):
                if not st.session_state.live_stats:
                    st.warning("Спочатку отримайте дані через кнопку 'Отримати дані'!")
                else:
                    if not is_tracking_active:
                        db.clear_daily_data("CUSTOM_START")
                        db.clear_daily_data("CUSTOM_END")
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("CUSTOM_START", stats)
                        st.success("⏱️ Початок зрізу зафіксовано в БД!")
                    else:
                        db.clear_daily_data("CUSTOM_END")
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("CUSTOM_END", stats)
                        st.success("✅ Кінець зрізу зафіксовано в БД!")
                    time.sleep(1.5)
                    st.rerun()

        st.divider()
        st.subheader("Управління блоками")

        groups = db.get_groups(owner="admin" if is_admin else username)
        group_names = list(groups.keys())

        col_grp1, col_grp2 = st.columns([3, 1])
        with col_grp1:
            selected_group = st.selectbox("Обрати блок для перегляду", options=["-- Не обрано --"] + group_names)

        with col_grp2:
            if selected_group and selected_group != "-- Не обрано --":
                if st.button("🗑️", help="Видалити цей блок повністю"):
                    db.delete_group(selected_group, owner="admin" if is_admin else username)
                    st.success(f"Блок '{selected_group}' видалено!")
                    time.sleep(1)
                    st.rerun()

        active_nicks = groups.get(selected_group, []) if selected_group and selected_group != "-- Не обрано --" else []

        st.markdown("---")

        if is_admin:
            with st.expander("🛠️ Тестові кнопки замірів (Debug)", expanded=False):
                st.caption("Керування зрізами в БД для ручної перевірки обчислень.")

                st.markdown("**⚔️ Сканування КВ (21:00 / CW_END):**")
                all_global_groups = db.get_global_groups()
                sorted_g_names = [g[0] for g in
                                  sorted(all_global_groups.items(), key=lambda x: len(x[1]), reverse=True)]

                saved_cw_str = db.get_setting("cw_scan_blocks", "")
                saved_cw_blocks = json.loads(saved_cw_str) if saved_cw_str else (
                    [sorted_g_names[0]] if sorted_g_names else [])

                selected_cw_blocks = st.multiselect(
                    "Блоки для авто-скану на КВ:",
                    options=sorted_g_names,
                    default=[b for b in saved_cw_blocks if b in sorted_g_names],
                    help="По дефолту сканується 1 найбiльший блок (клан). Можна додати ще блок суперника."
                )
                if st.button("💾 Зберегти блоки КВ", width='stretch'):
                    db.set_setting("cw_scan_blocks", json.dumps(selected_cw_blocks))
                    st.success("✅ Налаштування КВ блоків збережено!")
                    time.sleep(1)
                    st.rerun()

                st.divider()

                # ==================== ДЕБАГГЕР: ХМАРНЕ ЗБЕРЕЖЕННЯ GOOGLE DRIVE ====================
                st.caption("☁️ Резервне копіювання Google Drive")
                col_gd1, col_gd2 = st.columns(2)

                with col_gd1:
                    if st.button("☁️ Зберегти БД на Drive", width='stretch', help="Миттєво зберегти стан БД у хмару"):
                        with st.spinner("Завантаження бакапу на Google Drive..."):
                            if upload_db_to_drive():
                                st.success("✅ Стан БД збережено на Google Drive!")
                            else:
                                st.error("❌ Не вдалося зберегти БД на Drive.")

                with col_gd2:
                    if st.button("📥 Відновити БД з Drive", width='stretch', help="Завантажити останій актуальний бакап"):
                        with st.spinner("Викачування бакапу з Google Drive..."):
                            if download_latest_db_from_drive():
                                st.success("✅ БД оновлено з Google Drive!")
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error("❌ Не вдалося викачати бакап.")

                if os.path.exists("stalzone_stats.db"):
                    with open("stalzone_stats.db", "rb") as fp:
                        st.download_button(
                            label="💾 Скачати stalzone_stats.db на ПК",
                            data=fp,
                            file_name="stalzone_stats.db",
                            mime="application/x-sqlite3",
                            width='stretch'
                        )

                st.divider()

                if st.button("🗑️ Скинути всі заміри", type="secondary", width='stretch'):
                    db.clear_daily_data()
                    st.session_state.live_stats = {}
                    st.session_state.custom_start = {}
                    st.session_state.custom_end = {}
                    st.success("🧹 Усі зрізи успішно видалено з БД!")
                    time.sleep(1)
                    st.rerun()

                has_custom_data = bool(st.session_state.custom_start or st.session_state.custom_end)
                if st.button("🗑️⏱️ Видалити екстренний зріз", width='stretch', disabled=not has_custom_data):
                    db.clear_daily_data("CUSTOM_START")
                    db.clear_daily_data("CUSTOM_END")
                    st.session_state.custom_start = {}
                    st.session_state.custom_end = {}
                    st.warning("🗑️ Екстренний зріз видалено!")
                    time.sleep(1)
                    st.rerun()

                st.divider()
                st.caption("🧪 Запис картки гравця:")

                col_hist1, col_hist2 = st.columns(2)
                with col_hist1:
                    if st.button("📸 1 день тому", width='stretch', help="Зберегти дані за -1 день"):
                        if not st.session_state.live_stats:
                            st.warning("Спочатку отримайте дані!")
                        else:
                            ts_1d = (datetime.datetime.now(KYIV_TZ) - datetime.timedelta(days=1)).strftime(
                                '%Y-%m-%d %H:%M:%S')
                            for nick, stats in st.session_state.live_stats.items():
                                db.save_snapshot("DAILY_TEST_1D", stats, custom_timestamp=ts_1d)
                            st.success("✅ Записано тестовий зріз за 1 день тому!")

                with col_hist2:
                    if st.button("📸 3 дні тому", width='stretch', help="Зберегти дані за -3 дні"):
                        if not st.session_state.live_stats:
                            st.warning("Спочатку отримайте дані!")
                        else:
                            ts_3d = (datetime.datetime.now(KYIV_TZ) - datetime.timedelta(days=3)).strftime(
                                '%Y-%m-%d %H:%M:%S')
                            for nick, stats in st.session_state.live_stats.items():
                                db.save_snapshot("DAILY_TEST_3D", stats, custom_timestamp=ts_3d)
                            st.success("✅ Записано тестовий зріз за 3 дні тому!")

                st.divider()

                if st.button("📸 Записати 00:00", width='stretch'):
                    if not active_nicks:
                        st.error("Оберіть блок гравців у списку вище!")
                    elif not st.session_state.live_stats:
                        st.warning("Спочатку отримайте дані!")
                    else:
                        db.clear_daily_data("00:00")
                        for nick in active_nicks:
                            if nick in st.session_state.live_stats:
                                db.save_snapshot("00:00", st.session_state.live_stats[nick])
                        st.success("✅ Зріз 00:00 збережено (старий перезаписано)!")
                        time.sleep(1)
                        st.rerun()

                if st.button("📸 Записати 21:00 (Новий КВ цикл)", width='stretch'):
                    if not active_nicks:
                        st.error("Оберіть блок гравців у списку вище!")
                    elif not st.session_state.live_stats:
                        st.warning("Спочатку отримайте дані!")
                    else:
                        # Початок нового КВ циклу: очищаємо старий 21:00 та старий CW_END
                        db.clear_daily_data("21:00")
                        db.clear_daily_data("CW_END")
                        db.clear_daily_data("CUSTOM_START")
                        db.clear_daily_data("CUSTOM_END")
                        st.session_state.custom_start = {}
                        st.session_state.custom_end = {}
                        for nick in active_nicks:
                            if nick in st.session_state.live_stats:
                                db.save_snapshot("21:00", st.session_state.live_stats[nick])
                        st.success("✅ Зріз 21:00 збережено! Новий КВ цикл розпочато.")
                        time.sleep(1)
                        st.rerun()

                if st.button("📸 Записати кінець КВ (CW_END)", width='stretch'):
                    if not active_nicks:
                        st.error("Оберіть блок гравців у списку вище!")
                    elif not st.session_state.live_stats:
                        st.warning("Спочатку отримайте дані!")
                    else:
                        db.clear_daily_data("CW_END")
                        for nick in active_nicks:
                            if nick in st.session_state.live_stats:
                                db.save_snapshot("CW_END", st.session_state.live_stats[nick])
                        st.success("✅ Зріз кінця КВ (CW_END) збережено (старий перезаписано)!")
                        time.sleep(1)
                        st.rerun()

                # ==================== БЛОК ІНСПЕКТОРА БАЗИ ДАНИХ ====================
                st.divider()
                st.caption("🗄️ Перегляд та інспекція таблиць БД")

                try:
                    db_path = getattr(db, 'db_path', 'stalzone_stats.db')
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()

                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                    tables = [t[0] for t in cursor.fetchall() if not t[0].startswith("sqlite_")]

                    if tables:
                        selected_table = st.selectbox("Оберіть таблицю БД:", options=tables,
                                                      key="debug_db_table_select")

                        if selected_table:
                            limit = st.number_input("Ліміт рядків:", min_value=10, max_value=5000, value=100, step=50,
                                                    key="debug_db_limit")

                            df_table = pd.read_sql_query(f"SELECT * FROM {selected_table} LIMIT {limit}", conn)

                            st.markdown(f"**Записи в `{selected_table}` (показано {len(df_table)}):**")
                            st.dataframe(df_table, width='stretch')
                    else:
                        st.warning("В базі даних не знайдено жодної таблиці.")

                    conn.close()
                except Exception as e:
                    st.error(f"Помилка зчитування БД: {e}")

        with st.expander("➕ Створити / Редагувати блок"):
            new_group_name = st.text_input("Назва блоку (напр. Мій загін)")
            default_nicks = "\n".join(groups.get(new_group_name, [])) if new_group_name in groups else ""
            nicks_input = st.text_area("Нікнейми (по одному на рядок)", value=default_nicks, height=120)

            if st.button("Зберегти блок", width='stretch'):
                if new_group_name and nicks_input:
                    nicks_list = [n.strip() for n in nicks_input.split("\n") if n.strip()]
                    block_owner = "admin" if is_admin else username
                    db.save_group(new_group_name, nicks_list, owner=block_owner)
                    st.success(f"Блок '{new_group_name}' збережено!")
                    st.rerun()

            if is_admin:
                st.divider()
                st.markdown("**🛡️ Отримати блок з Клану**")
                clan_nick_search = st.text_input("Нікнейм гравця клану")
                if st.button("📥 Завантажити склад клану", width='stretch'):
                    if clan_nick_search:
                        api_client = StalzoneApiClient(API_CLIENT_ID, API_CLIENT_SECRET, region)
                        with st.spinner(f"Запит клану для {clan_nick_search}..."):
                            c_name, c_members = api_client.fetch_clan_members_by_player(clan_nick_search)
                            if c_name and c_members:
                                db.save_group(c_name, c_members, owner="admin")
                                st.success(f"✅ Клан '{c_name}' додано! Отримано {len(c_members)} гравців.")
                                time.sleep(1.2)
                                st.rerun()
                            else:
                                st.error("Не вдалося знайти клан для цього нікнейму.")

    # ---------------- ГОЛОВНА НАВІГАЦІЯ ----------------
    st.markdown("<div class='nav-header'>🧭 Розділи системи аналітики</div>", unsafe_allow_html=True)

    if is_admin:
        main_menu_options = [
            "📋 До КВ (Основна статистика)",
            "⚔️ Після КВ \\ Зріз (Аналітика)",
            "🎴 Картка гравців",
            "🛡️ Кланові метрики"
        ]
    else:
        main_menu_options = [
            "📋 Основна статистика",
            "⚔️ Зріз (аналітика)",
            "🎴 Картка гравців"
        ]

    if hasattr(st, "segmented_control"):
        mode = st.segmented_control("Головне меню", options=main_menu_options, default=main_menu_options[0],
                                    label_visibility="collapsed")
    else:
        mode = st.radio("Головне меню", options=main_menu_options, horizontal=True)

    col_btn_space, col_btn = st.columns([3, 1], vertical_alignment="center")

    with col_btn:
        fetch_data_btn = st.button("📥 Отримати дані", type="primary", width='stretch')

    if fetch_data_btn:
        if not active_nicks:
            st.error("Помилка: Спочатку оберіть блок гравців у лівому меню!")
        else:
            api_client = StalzoneApiClient(API_CLIENT_ID, API_CLIENT_SECRET, region)
            progress_bar = st.progress(0)
            status_text = st.empty()

            def update_progress(current, total, last_nick):
                status_text.markdown(f"**Паралельне сканування:** `{last_nick}` ({current}/{total}) ... ⏳")
                progress_bar.progress(current / total)

            results = api_client.fetch_bulk_player_stats(
                active_nicks,
                batch_size=10,
                progress_callback=update_progress
            )
            st.session_state.live_stats.update(results)

            status_text.success("✅ Дані успішно отримані через eAPI!")
            time.sleep(1.2)
            status_text.empty()
            progress_bar.empty()
            st.rerun()

    snap_00 = db.get_snapshot("00:00")
    snap_21 = db.get_snapshot("21:00")
    snap_cw = db.get_snapshot("CW_END")

    st.divider()

    # ---------------- РЕЖИМ 1: ОСНОВНА СТАТИСТИКА ----------------
    if mode in ["📋 До КВ (Основна статистика)", "📋 Основна статистика"]:
        if not active_nicks:
            st.info("👈 Оберіть або створіть блок гравців у панелі ліворуч.")
            return

        st.subheader(f"📋 Основна статистика блоку: '{selected_group}'")

        act_rows = []
        for nick in active_nicks:
            base = snap_00.get(nick, {})
            curr = st.session_state.live_stats.get(nick, snap_21.get(nick, {}))

            if not curr and not base:
                act_rows.append({
                    "Нікнейм": nick,
                    "Статус": "⚪ Немає даних",
                    "TP (Сила)": 0.0,
                    "Убито гравців": 0,
                    "Помощь": 0,
                    "Смертей": 0,
                    "Час у грі": "0 хв.",
                    "Приріст за день (Уб / См)": "+0 / +0"
                })
            else:
                c_kills = curr.get("kills", base.get("kills", 0))
                c_assists = curr.get("assists", base.get("assists", 0))
                c_deaths = curr.get("deaths", base.get("deaths", 0))
                c_playtime = curr.get("playtime", base.get("playtime", 0))

                b_kills = base.get("kills", c_kills)
                b_deaths = base.get("deaths", c_deaths)
                b_playtime = base.get("playtime", c_playtime)

                d_kills = max(0, c_kills - b_kills)
                d_deaths = max(0, c_deaths - b_deaths)
                d_time = max(0, c_playtime - b_playtime)

                played_status = "🟢 Розминався" if d_time >= 30 else "🔴 Не грав"
                player_tp = AnalyticsEngine.calc_tp(curr if curr else base)

                act_rows.append({
                    "Нікнейм": nick,
                    "Статус": played_status,
                    "TP (Сила)": player_tp,
                    "Убито гравців": c_kills,
                    "Помощь": c_assists,
                    "Смертей": c_deaths,
                    "Час у грі": f"{c_playtime} хв.",
                    "Приріст за день (Уб / См)": f"+{d_kills} / +{d_deaths}"
                })

        df_pre = pd.DataFrame(act_rows)
        st.dataframe(df_pre, width='stretch', height=450)

    # ---------------- РЕЖИМ 2: ЗРІЗ (АНАЛІТИКА) ----------------
    elif mode in ["⚔️ Після КВ \\ Зріз (Аналітика)", "⚔️ Зріз (аналітика)"]:
        if not active_nicks:
            st.info("👈 Оберіть або створіть блок гравців у панелі ліворуч.")
            return

        st.subheader(f"⚔️ Аналітичний блок: '{selected_group}'")

        is_custom_complete = bool(st.session_state.custom_start and st.session_state.custom_end)

        analytics_source = "Стандартний КВ"
        if is_custom_complete:
            col_info, col_choice = st.columns([2, 1])
            with col_info:
                st.info("⏱️ Знайдено збережений **Екстренний/Ручний зріз** (Початок -> Кінець).")
            with col_choice:
                analytics_source = st.radio(
                    "Джерело аналітики:",
                    options=["⏱️ Ручний зріз", "⚔️ Стандартний КВ (21:00 -> КВ)"],
                    horizontal=True
                )
        elif is_tracking_active:
            st.warning("⏳ **Зріз у процесі!** Натисніть 🛑 Кінець в сайдбарі після закінчення.")
        else:
            st.info("⚔️ Відображається статистика за наявними даними КВ.")

        rows = []
        use_custom = (analytics_source == "⏱️ Ручний зріз") and is_custom_complete

        for nick in active_nicks:
            if use_custom:
                start = st.session_state.custom_start.get(nick, {})
                end = st.session_state.custom_end.get(nick, {})
            else:
                start = snap_21.get(nick, snap_00.get(nick, {}))
                end = st.session_state.live_stats.get(nick, snap_cw.get(nick, {}))

            if not start or not end:
                rows.append({
                    "Нік": nick,
                    "ТР за період": 0.0,
                    "Різниця з загальним (%)": 0.0,
                    "Настріл (У/С/П)": "Немає даних",
                    "У/С": 0.0,
                    "УП/С": 0.0,
                    "Гранат": 0,
                    "Постріли (Г/Т/К)": "-",
                    "Точність (%)": "-",
                    "Шкода (УН / УП)": "-",
                    "УН/УП": 0.0
                })
            else:
                m = AnalyticsEngine.compute_cw_metrics(start, end)
                rows.append({
                    "Нік": m["nickname"],
                    "ТР за період": m["slice_tp"],
                    "Різниця з загальним (%)": m["tp_diff_pct"],
                    "Настріл (У/С/П)": m["cw_score"],
                    "У/С": m["kd"],
                    "УП/С": m["kad"],
                    "Гранат": m["grenades"],
                    "Постріли (Г/Т/К)": m["shots"],
                    "Точність (%)": m["accuracy"],
                    "Шкода (УН / УП)": m["damage"],
                    "УН/УП": m["un_up"]
                })

        df_post = pd.DataFrame(rows)

        styled_df = (
            df_post.style
            .format("{:.1f}", subset=["ТР за період"])
            .format("{:+.1f}%", subset=["Різниця з загальним (%)"])
            .format("{:.2f}", subset=["У/С", "УП/С", "УН/УП"])
            .map(AnalyticsEngine.get_color_style, subset=["У/С", "УП/С", "УН/УП"])
            .map(AnalyticsEngine.get_tp_diff_style, subset=["Різниця з загальним (%)"])
        )
        st.dataframe(styled_df, width='stretch', height=450)

    # ---------------- РЕЖИМ 3: 🎴 КАРАТКА ГРАВЦІВ ----------------
    elif mode == "🎴 Картка гравців":
        if not active_nicks:
            st.warning("⚠️ Оберіть конкретний блок гравців у лівому меню сайдбару!")
            return

        card_sub_mode = st.tabs(["🏆 Тирлист (Tier List)", "👤 Персональна картка гравця"])

        # 🏆 ТИРЛИСТ (TIER LIST)
        with card_sub_mode[0]:
            st.subheader(f"🏆 Тирлист гравців блоку '{selected_group}'")

            col_tp_m, col_opp = st.columns([2, 1])

            with col_tp_m:
                tp_mode = st.radio(
                    "Режим обчислення рейтингу ТР:",
                    options=["🔥 Загальний ТР (Total Power)", "⚔️ ТР за Зріз (Slice TP)"],
                    horizontal=True
                )

            tp_multiplier = 1.0
            penalty_pct = 0.0

            if tp_mode == "⚔️ ТР за Зріз (Slice TP)":
                with col_opp:
                    opp_rating = st.number_input(
                        "⚔️ Рейтинг противника:",
                        min_value=100,
                        max_value=3000,
                        value=1500,
                        step=25,
                        help="Дефолт 1500. Якщо > 1500 — бонус, якщо < 1500 — штраф."
                    )

                    diff_factor = (opp_rating - 1500) / 1500.0
                    penalty_pct = diff_factor * 0.5
                    tp_multiplier = 1.0 + penalty_pct

                    if penalty_pct > 0:
                        st.success(f"🔥 Сильний противник ({opp_rating}): Бонус до ТР +{round(penalty_pct * 100, 1)}%")
                    elif penalty_pct < 0:
                        st.warning(f"⚠️ Слабкий противник ({opp_rating}): Штраф до ТР {round(penalty_pct * 100, 1)}%")

            is_custom_complete = bool(st.session_state.custom_start and st.session_state.custom_end)

            tiers = {
                "S": {"color": "#FFD700", "label": "S Tier (ТР > 150)", "players": []},
                "A": {"color": "#FF4B4B", "label": "A Tier (ТР 140 - 150)", "players": []},
                "B": {"color": "#9D4EDD", "label": "B Tier (ТР 130 - 140)", "players": []},
                "C": {"color": "#1E90FF", "label": "C Tier (ТР 120 - 130)", "players": []},
                "D": {"color": "#00C853", "label": "D Tier (ТР 110 - 120)", "players": []},
                "E": {"color": "#808080", "label": "E Tier (ТР 90 - 110)", "players": []},
                "F": {"color": "#FFFFFF", "label": "F Tier (ТР < 90)", "players": []},
            }

            for nick in sorted(active_nicks):
                tp_val = 0.0

                if tp_mode == "🔥 Загальний ТР (Total Power)":
                    p_data = st.session_state.live_stats.get(nick, db.get_historical_snapshot(nick, 0))
                    if p_data:
                        tp_val = AnalyticsEngine.calc_tp(p_data)
                else:
                    if is_custom_complete:
                        start = st.session_state.custom_start.get(nick, {})
                        end = st.session_state.custom_end.get(nick, {})
                    else:
                        start = snap_21.get(nick, snap_00.get(nick, {}))
                        end = st.session_state.live_stats.get(nick, snap_cw.get(nick, {}))

                    if start and end:
                        m = AnalyticsEngine.compute_cw_metrics(start, end)
                        tp_val = round(m["slice_tp"] * tp_multiplier, 1)

                if tp_val < 10.0:
                    continue

                if tp_val > 150:
                    tiers["S"]["players"].append((nick, tp_val))
                elif 140 <= tp_val <= 150:
                    tiers["A"]["players"].append((nick, tp_val))
                elif 130 <= tp_val < 140:
                    tiers["B"]["players"].append((nick, tp_val))
                elif 120 <= tp_val < 130:
                    tiers["C"]["players"].append((nick, tp_val))
                elif 110 <= tp_val < 120:
                    tiers["D"]["players"].append((nick, tp_val))
                elif 90 <= tp_val < 110:
                    tiers["E"]["players"].append((nick, tp_val))
                else:
                    tiers["F"]["players"].append((nick, tp_val))

            for key, tier in tiers.items():
                tier["players"].sort(key=lambda x: x[1], reverse=True)

                player_badges = " ".join([
                    f"<span style='background-color: rgba(255,255,255,0.06); border: 1px solid {tier['color']}; "
                    f"padding: 5px 10px; border-radius: 6px; font-weight: bold; margin-right: 8px; display: inline-block; margin-bottom: 8px;'>"
                    f"{p[0]} <span style='color: {tier['color']}; margin-left: 4px;'>({p[1]})</span></span>"
                    for p in tier["players"]
                ]) if tier["players"] else "<i style='color: #777777;'>Немає гравців у цьому тирі</i>"

                st.markdown(f"""
                <div style="border-left: 6px solid {tier['color']}; background: #18191e; padding: 14px 18px; border-radius: 8px; margin-bottom: 14px;">
                    <h4 style="margin: 0 0 10px 0; color: {tier['color']}; font-size: 1.1rem;">{tier['label']} &nbsp; <span style='font-size: 0.9rem; opacity: 0.8;'>(Гравців: {len(tier['players'])})</span></h4>
                    <div>{player_badges}</div>
                </div>
                """, unsafe_allow_html=True)

        # 👤 ПЕРСОНАЛЬНА КАРАТКА ГРАВЦЯ
        with card_sub_mode[1]:
            st.subheader(f"🎴 Персональна картка аналітики")

            selected_player = st.selectbox("Обрати гравця з блоку", sorted(active_nicks))

            if selected_player:
                curr_data = st.session_state.live_stats.get(selected_player,
                                                            db.get_historical_snapshot(selected_player, 0))

                if not curr_data:
                    st.error("Для цього гравця відсутні дані в базі. Натисніть 'Отримати дані'.")
                else:
                    current_tp = AnalyticsEngine.calc_tp(curr_data)

                    K = curr_data.get("kills", 0)
                    D = max(curr_data.get("deaths", 0), 1)
                    A = curr_data.get("assists", 0)
                    DD = curr_data.get("damage_dealt", 0)
                    DR = max(curr_data.get("damage_recv", 0), 1)

                    HS = curr_data.get("headshots", 0)
                    BS = curr_data.get("bodyshots", 0)
                    LS = curr_data.get("limbshots", 0)
                    total_shots = HS + BS + LS

                    kd = AnalyticsEngine.calc_ratio(K, D)
                    kad = AnalyticsEngine.calc_ratio(K + A, D)
                    un_up = AnalyticsEngine.calc_ratio(DD, DR)
                    curr_head_pct = round((HS * 100 / total_shots), 2) if total_shots > 0 else 0.0

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("🔥 Total Power (TP)", current_tp)
                    c2.metric("🎯 K/D (У/С)", kd)
                    c3.metric("🤝 KAD (УП/С)", kad)
                    c4.metric("🛡️ УН / УП", un_up)
                    c5.metric("🎯 % Влучань у голову", f"{curr_head_pct:.2f}%")

                    st.divider()
                    st.markdown("### 📈 Динаміка змін (Було ➔ Стало)")

                    def build_delta_col(title: str, days_ago: int):
                        old_data = db.get_historical_snapshot(selected_player, days_ago)
                        if not old_data:
                            st.caption(f"**{title}**: немає даних в БД")
                            return

                        old_tp = AnalyticsEngine.calc_tp(old_data)
                        old_K = old_data.get("kills", 0)
                        old_D = max(old_data.get("deaths", 0), 1)
                        old_A = old_data.get("assists", 0)
                        old_DD = old_data.get("damage_dealt", 0)
                        old_DR = max(old_data.get("damage_recv", 0), 1)

                        old_HS = old_data.get("headshots", 0)
                        old_BS = old_data.get("bodyshots", 0)
                        old_LS = old_data.get("limbshots", 0)
                        old_total = old_HS + old_BS + old_LS

                        old_kd = AnalyticsEngine.calc_ratio(old_K, old_D)
                        old_kad = AnalyticsEngine.calc_ratio(old_K + old_A, old_D)
                        old_un_up = AnalyticsEngine.calc_ratio(old_DD, old_DR)
                        old_head_pct = round((old_HS * 100 / old_total), 2) if old_total > 0 else 0.0

                        tp_diff = round(current_tp - old_tp, 1)
                        kd_diff = round(kd - old_kd, 2)
                        kad_diff = round(kad - old_kad, 2)
                        un_up_diff = round(un_up - old_un_up, 2)
                        head_diff = round(curr_head_pct - old_head_pct, 2)

                        st.markdown(f"#### {title}")
                        st.metric("Зміна TP", f"{current_tp}", delta=f"{tp_diff:+}")

                        st.markdown(f"**Кіли**: `{old_K}` ➔ `{K}` (`{K - old_K:+}`)")
                        st.markdown(f"**Смерті**: `{old_D}` ➔ `{D}` (`{D - old_D:+}`)")
                        st.markdown(f"**Асисти**: `{old_A}` ➔ `{A}` (`{A - old_A:+}`)")

                        st.metric("Зміна K/D", f"{old_kd} ➔ {kd}", delta=f"{kd_diff:+}")
                        st.metric("Зміна KAD", f"{old_kad} ➔ {kad}", delta=f"{kad_diff:+}")

                        st.markdown(f"**Шкода**: `{int(old_DD):,}` / `{int(old_DR):,}` ➔ `{int(DD):,}` / `{int(DR):,}`")
                        st.metric("Зміна УН/УП", f"{old_un_up} ➔ {un_up}", delta=f"{un_up_diff:+}")

                        st.metric("Зміна % Голови", f"{old_head_pct:.2f}% ➔ {curr_head_pct:.2f}%",
                                  delta=f"{head_diff:+.2f}%")

                    col_d1, col_d3, col_d7, col_d30 = st.columns(4)
                    with col_d1:
                        build_delta_col("За 1 День", 1)
                    with col_d3:
                        build_delta_col("За 3 Дні", 3)
                    with col_d7:
                        build_delta_col("За 7 Днів (Тиждень)", 7)
                    with col_d30:
                        build_delta_col("За 30 Днів (Місяць)", 30)

    # ---------------- РЕЖИМ 4: 🛡️ КЛАНОВІ МЕТРИКИ (ТІЛЬКИ АДМІН) ----------------
    elif mode == "🛡️ Кланові метрики" and is_admin:
        avg_val = AnalyticsEngine.calculate_clan_avg_grenades(
            db, active_nicks, st.session_state.get("live_stats")
        )
        delta_val = 0

        st.session_state["avg_grenades_val"] = avg_val
        st.session_state["grenades_delta"] = delta_val

        render_clan_metrics_tab(db, selected_group, active_nicks)


if __name__ == "__main__":
    main()
