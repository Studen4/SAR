import datetime
import logging
import threading
import time
import requests
import streamlit as st
from database import DatabaseManager, KYIV_TZ

# Конфігурація API
try:
    from api_config import API_CLIENT_ID, API_CLIENT_SECRET, API_BASE_URL, API_BEARER_TOKEN_SEMI, API_BEARER_TOKEN
except ImportError:
    API_CLIENT_ID = ""
    API_CLIENT_SECRET = ""
    API_BASE_URL = "https://eapi.stalzone.com"
    API_BEARER_TOKEN_SEMI = ""
    API_BEARER_TOKEN = ""

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("StalzoneApiApp")


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
        empty_stats = {
            "nickname": nickname, "status": "Error",
            "kills": 0, "assists": 0, "deaths": 0, "playtime": 0,
            "damage_dealt": 0, "damage_recv": 0, "grenades": 0,
            "headshots": 0, "bodyshots": 0, "limbshots": 0
        }

        if not self._authenticate():
            return empty_stats

        url = f"{API_BASE_URL.rstrip('/')}/{self.region.upper()}/character/by-name/{nickname}/profile"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                stats_raw = data.get("stats", [])
                stats_map = {item["id"]: item.get("value", 0) for item in stats_raw if isinstance(item, dict) and "id" in item}

                playtime_ms = stats_map.get("pla-tim", 0)
                playtime_min = round(playtime_ms / (1000 * 60))

                return {
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
                                    (m.get("character", {}).get("name") if isinstance(m.get("character"), dict) else None)
                                )
                                if name:
                                    members.append(name)

                    if members:
                        return clan_name, members

            return clan_name, []
        except Exception as e:
            logger.error(f"❌ Помилка отримання складу клану: {e}")
            return None, []


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
            groups = db.get_groups()
            all_nicks = set()
            for group_nicks in groups.values():
                all_nicks.update(group_nicks)

            if all_nicks:
                api_client = StalzoneApiClient(API_CLIENT_ID, API_CLIENT_SECRET, "EU")

                # 1. Запит о 00:00 (Початок дня)
                if hour == 0 and minute == 0 and (today_str, "00:00") not in executed_tasks:
                    logger.info("⏰ [SCHEDULE] Виконання автоматичного зрізу 00:00 для всіх блоків...")
                    for nick in all_nicks:
                        stats = api_client.fetch_player_stats(nick)
                        if stats.get("status") == "OK":
                            db.save_snapshot("00:00", stats)
                    executed_tasks[(today_str, "00:00")] = True
                    logger.info("✅ [SCHEDULE] Зріз 00:00 завершено.")

                # 2. Запит о 21:00 (Початок КВ)
                if hour == 21 and minute == 0 and (today_str, "21:00") not in executed_tasks:
                    logger.info("⏰ [SCHEDULE] Виконання автоматичного зрізу 21:00 для всіх блоків...")
                    for nick in all_nicks:
                        stats = api_client.fetch_player_stats(nick)
                        if stats.get("status") == "OK":
                            db.save_snapshot("21:00", stats)
                    db.clear_daily_data("CUSTOM_START")
                    db.clear_daily_data("CUSTOM_END")
                    executed_tasks[(today_str, "21:00")] = True
                    logger.info("✅ [SCHEDULE] Зріз 21:00 завершено.")

                # 3. Запит після КВ (22:00 або 22:15)
                if hour == target_cw_hour and minute == target_cw_min and (today_str, "CW_END") not in executed_tasks:
                    logger.info(f"⏰ [SCHEDULE] Виконання автоматичного зрізу CW_END ({target_cw_hour}:{target_cw_min:02d}) для всіх блоків...")
                    for nick in all_nicks:
                        stats = api_client.fetch_player_stats(nick)
                        if stats.get("status") == "OK":
                            db.save_snapshot("CW_END", stats)
                    executed_tasks[(today_str, "CW_END")] = True
                    logger.info("✅ [SCHEDULE] Зріз CW_END завершено.")

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
