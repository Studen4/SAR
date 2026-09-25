import io
import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
import streamlit as st
from PIL import Image
from google import genai
from google.genai import types
import calendar
from datetime import datetime, timedelta

# ==========================================
# 0. КОНФІГУРАЦІЯ ТА КЛІЄНТ API
# ==========================================
GEMINI_API_KEY = None
API_CLIENT_ID = None
API_CLIENT_SECRET = None
API_BASE_URL = "https://eapi.stalzone.com"
API_BEARER_TOKEN_SEMI = None
API_BEARER_TOKEN = None

try:
    from api_config import (
        GEMINI_API_KEY,
        API_CLIENT_ID,
        API_CLIENT_SECRET,
        API_BASE_URL,
        API_BEARER_TOKEN_SEMI,
        API_BEARER_TOKEN,
    )
except ImportError:
    pass

logger = logging.getLogger("StalzoneWorkspace")


def clean_val(val, default="?"):
    """Очищення значень від None, NaN та порожніх стрічок."""
    if val is None or pd.isna(val):
        return default
    s = str(val).strip()
    if s.lower() in ["nan", "none", "null", ""]:
        return default
    return s


class StalzoneApiClient:
    def __init__(self, client_id: str = "", client_secret: str = "", region: str = "EU"):
        self.region = str(region).lower()
        self.client_id = client_id or API_CLIENT_ID
        self.client_secret = client_secret or API_CLIENT_SECRET
        self.base_url = API_BASE_URL.rstrip('/') if API_BASE_URL else "https://eapi.stalzone.com"
        self.access_token = API_BEARER_TOKEN_SEMI if API_BEARER_TOKEN_SEMI else None
        self.token_expires_at = time.time() + 31536000 if self.access_token else 0

    def _authenticate(self) -> bool:
        if self.access_token and time.time() < self.token_expires_at - 60:
            return True

        try:
            res = requests.post(
                f"{self.base_url}/oauth/token",
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
                return True
        except Exception as e:
            logger.error(f"❌ Мережева помилка авторизації OAuth: {e}")
        return False

    def fetch_player_stats(self, nickname: str) -> dict:
        empty_stats = {
            "nickname": nickname, "status": "Error",
            "kills": 0, "assists": 0, "deaths": 0, "playtime": 0,
            "damage_dealt": 0, "damage_recv": 0, "grenades": 0,
            "headshots": 0, "bodyshots": 0, "limbshots": 0
        }

        if not nickname or not nickname.strip():
            return empty_stats

        if not self._authenticate():
            return empty_stats

        clean_nick = nickname.strip()
        safe_nick = requests.utils.quote(clean_nick)
        url = f"{self.base_url}/{self.region.upper()}/character/by-name/{safe_nick}/profile"
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

                return {
                    "nickname": data.get("username", clean_nick),
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
        except Exception as e:
            logger.error(f"❌ Помилка API-запиту для '{clean_nick}': {e}")

        return empty_stats


def get_stalzone_client(region="EU"):
    import sys
    for mod_name in ["app_api", "__main__"]:
        if mod_name in sys.modules and hasattr(sys.modules[mod_name], "StalzoneApiClient"):
            ApiClientClass = getattr(sys.modules[mod_name], "StalzoneApiClient")
            return ApiClientClass(API_CLIENT_ID, API_CLIENT_SECRET, region)

    return StalzoneApiClient(API_CLIENT_ID, API_CLIENT_SECRET, region)


# ==========================================
# 1. АНАЛІТИЧНИЙ РУШІЙ (ANALYTICS ENGINE)
# ==========================================
class AnalyticsEngine:
    @staticmethod
    def _safe_float(val, default: float = 0.0) -> float:
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @classmethod
    def calc_tp(cls, stats_dict) -> float:
        if not stats_dict:
            return 0.0

        if isinstance(stats_dict, (int, float)):
            return round(float(stats_dict), 1)

        if not isinstance(stats_dict, dict):
            return 0.0

        for tp_key in ["tp", "TP", "total_power", "Total Power"]:
            if tp_key in stats_dict and stats_dict[tp_key] is not None:
                val = cls._safe_float(stats_dict[tp_key], None)
                if val is not None:
                    return round(val, 1)

        K = cls._safe_float(stats_dict.get("kills"), 0.0)
        A = cls._safe_float(stats_dict.get("assists"), 0.0)
        D = max(cls._safe_float(stats_dict.get("deaths"), 1.0), 1.0)
        DD = cls._safe_float(stats_dict.get("damage_dealt"), 0.0)
        DR = max(cls._safe_float(stats_dict.get("damage_recv"), 1.0), 1.0)
        HS = cls._safe_float(stats_dict.get("headshots"), 0.0)
        BS = cls._safe_float(stats_dict.get("bodyshots"), 0.0)
        LS = cls._safe_float(stats_dict.get("limbshots"), 0.0)

        total_shots = HS + BS + LS
        R1 = max(((K + A) / D) / 1.1, 0.0001)
        R2 = max((K / D) / 0.8, 0.0001)
        R3 = max((DD / DR) / 1.0, 0.0001)

        if total_shots > 0:
            ch, cb, cl = 2.0, 1.0, 0.5
            baseline = ch * 0.3 + cb * 0.5 + cl * 0.2
            R4 = max((ch * HS + cb * BS + cl * LS) / total_shots / baseline, 0.0001)
        else:
            R4 = 1.0

        w1, w2, w3, w4 = 0.35, 0.15, 0.30, 0.20
        tp = round(100 * (R1 ** w1) * (R2 ** w2) * (R3 ** w3) * (R4 ** w4), 1)
        return tp

    @classmethod
    def calc_ratio(cls, kills, deaths) -> float:
        k = cls._safe_float(kills, 0.0)
        d = cls._safe_float(deaths, 1.0)
        if d <= 0:
            d = 1.0
        return round(k / d, 2)

    @staticmethod
    def get_tier_letter(tp: float) -> str:
        if tp >= 150:
            return "S"
        elif 140 <= tp < 150:
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

    @staticmethod
    def parse_tabs(tab_urls: list[str], our_clan_name: str, max_tabs: int = 3) -> list[dict]:
        extracted_stages = []
        system_prompt = f"""
                Ти — експерт з OCR та комп'ютерного зору для аналізу скріншотів таблиць (табів) з гри STALCRAFT.
                ЗАВДАННЯ: Проаналізуй надане зображення табу. Наш клан: "{our_clan_name}".
                ФОРМАТ ВІДПОВІДІ (ТІЛЬКИ JSON):
                {{
                    "our_clan": "{our_clan_name}",
                    "our_score": 0,
                    "enemy_clan": "НазваВорога",
                    "enemy_score": 0,
                    "online": 0,
                    "players": ["Nick1", "Nick2"]
                }}
                """

        valid_urls = [u.strip() for u in tab_urls if u.strip()][:max_tabs]
        FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.5-flash"]
        client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()

        for idx, clean_url in enumerate(valid_urls):
            if idx > 0:
                time.sleep(1.5)
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                response = requests.get(clean_url, headers=headers, timeout=10)
                response.raise_for_status()

                image = Image.open(io.BytesIO(response.content))
                stage_result = None

                for model_name in FALLBACK_MODELS:
                    try:
                        api_response = client.models.generate_content(
                            model=model_name,
                            contents=[system_prompt, image],
                            config=types.GenerateContentConfig(response_mime_type="application/json")
                        )
                        raw_text = api_response.text.strip() if api_response.text else ""
                        clean_json_str = re.sub(r"^```json\s*|\s*```$", "", raw_text, flags=re.MULTILINE)
                        stage_data = json.loads(clean_json_str)

                        raw_players = [str(p).strip() for p in stage_data.get("players", []) if str(p).strip()]

                        stage_result = {
                            "our_clan": stage_data.get("our_clan", our_clan_name),
                            "our_score": int(stage_data.get("our_score", 0)),
                            "enemy_clan": str(stage_data.get("enemy_clan", "Unknown")),
                            "enemy_score": int(stage_data.get("enemy_score", 0)),
                            "online": int(stage_data.get("online", len(raw_players))),
                            "players": raw_players[:25]
                        }
                        break
                    except Exception:
                        continue

                if stage_result is not None:
                    extracted_stages.append(stage_result)
            except Exception as e:
                logger.error(f"Помилка завантаження зображення {clean_url}: {e}")

        return extracted_stages


# ==========================================
# 2. ДОПОМІЖНІ ФУНКЦІЇ БД ТА WORKSPACE
# ==========================================
def init_clan_db(db):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS player_equip (
                nickname TEXT PRIMARY KEY,
                group_name TEXT,
                bio_armor TEXT DEFAULT '?',
                assault_armor TEXT DEFAULT '?',
                assault_rifle TEXT DEFAULT '?',
                sniper_rifle TEXT DEFAULT '?',
                build TEXT DEFAULT '?',
                otryad_num INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS squad_titles (
                group_name TEXT,
                otryad_num INTEGER,
                title TEXT,
                PRIMARY KEY (group_name, otryad_num)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workspace_events (
                event_date TEXT PRIMARY KEY,
                title TEXT,
                online_count INTEGER,
                maps TEXT,
                enemy_clans TEXT,
                attendance_list TEXT,
                tab_urls TEXT,
                enemy_tab_urls TEXT,
                replays_urls TEXT,
                results TEXT,
                comments TEXT
            )
        """)
        conn.commit()


def clear_all_clan_squads(db, group_name: str):
    """Очищення всіх збережених отрядів та скидання номерів отрядів для клану."""
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE player_equip SET otryad_num = 0 WHERE group_name = ?", (group_name,))
        cursor.execute("DELETE FROM squad_titles WHERE group_name = ?", (group_name,))
        conn.commit()
    if hasattr(db, "clear_squads"):
        db.clear_squads(group_name)


def get_player_equip(db, nickname: str) -> dict:
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM player_equip WHERE nickname = ?", (nickname,))
        row = cursor.fetchone()
        if row:
            if isinstance(row, tuple):
                return {
                    "nickname": row[0], "group_name": row[1],
                    "bio_armor": clean_val(row[2]),
                    "assault_armor": clean_val(row[3]),
                    "assault_rifle": clean_val(row[4]),
                    "sniper_rifle": clean_val(row[5]),
                    "build": clean_val(row[6]),
                    "otryad_num": int(row[7]) if row[7] is not None and str(row[7]).isdigit() else 0
                }
            r_dict = dict(row)
            return {
                "nickname": r_dict.get("nickname", nickname),
                "group_name": r_dict.get("group_name", ""),
                "bio_armor": clean_val(r_dict.get("bio_armor")),
                "assault_armor": clean_val(r_dict.get("assault_armor")),
                "assault_rifle": clean_val(r_dict.get("assault_rifle")),
                "sniper_rifle": clean_val(r_dict.get("sniper_rifle")),
                "build": clean_val(r_dict.get("build")),
                "otryad_num": int(r_dict.get("otryad_num", 0)) if str(r_dict.get("otryad_num", 0)).isdigit() else 0
            }
        return {
            "nickname": nickname, "group_name": "", "bio_armor": "?",
            "assault_armor": "?", "assault_rifle": "?", "sniper_rifle": "?",
            "build": "?", "otryad_num": 0
        }


def save_player_equip(db, nickname: str, group_name: str, data: dict):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO player_equip (nickname, group_name, bio_armor, assault_armor, assault_rifle, sniper_rifle, build, otryad_num)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nickname) DO UPDATE SET
                group_name=excluded.group_name,
                bio_armor=excluded.bio_armor,
                assault_armor=excluded.assault_armor,
                assault_rifle=excluded.assault_rifle,
                sniper_rifle=excluded.sniper_rifle,
                build=excluded.build,
                otryad_num=excluded.otryad_num
        """, (
            nickname, group_name,
            clean_val(data.get("bio_armor")), clean_val(data.get("assault_armor")),
            clean_val(data.get("assault_rifle")), clean_val(data.get("sniper_rifle")),
            clean_val(data.get("build")), int(data.get("otryad_num", 0))
        ))
        conn.commit()


def get_squad_title(db, group_name: str, otryad_num: int) -> str:
    if otryad_num == 0:
        return "Резерв"
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT title FROM squad_titles WHERE group_name = ? AND otryad_num = ?",
                       (group_name, otryad_num))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
    return f"Отряд {otryad_num}"


def save_squad_title(db, group_name: str, otryad_num: int, title: str):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO squad_titles (group_name, otryad_num, title)
            VALUES (?, ?, ?)
            ON CONFLICT(group_name, otryad_num) DO UPDATE SET title=excluded.title
        """, (group_name, otryad_num, title))
        conn.commit()


def get_workspace_events(db) -> dict:
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM workspace_events")
        rows = cursor.fetchall()
        events = {}
        for r in rows:
            events[r[0]] = {
                "event_date": r[0], "title": r[1], "online_count": r[2],
                "maps": r[3], "enemy_clans": r[4], "attendance_list": r[5],
                "tab_urls": r[6], "enemy_tab_urls": r[7], "replays_urls": r[8],
                "results": r[9], "comments": r[10]
            }
        return events


def save_workspace_event(db, data: dict):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO workspace_events 
            (event_date, title, online_count, maps, enemy_clans, attendance_list, tab_urls, enemy_tab_urls, replays_urls, results, comments)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_date) DO UPDATE SET
                title=excluded.title, online_count=excluded.online_count,
                maps=excluded.maps, enemy_clans=excluded.enemy_clans,
                attendance_list=excluded.attendance_list, tab_urls=excluded.tab_urls,
                enemy_tab_urls=excluded.enemy_tab_urls, replays_urls=excluded.replays_urls,
                results=excluded.results, comments=excluded.comments
        """, (
            data["event_date"], data.get("title", ""), data.get("online_count", 0),
            data.get("maps", ""), data.get("enemy_clans", ""), data.get("attendance_list", ""),
            data.get("tab_urls", ""), data.get("enemy_tab_urls", ""), data.get("replays_urls", ""),
            data.get("results", ""), data.get("comments", "")
        ))
        conn.commit()


def parse_enemy_roster_tags(enemy_text: str) -> dict:
    enemy_rosters = {}
    current_clan = None
    for line in enemy_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_clan = line[1:-1].strip()
            if current_clan not in enemy_rosters:
                enemy_rosters[current_clan] = []
        elif current_clan:
            enemy_rosters[current_clan].append(line)
    return enemy_rosters


def process_workspace_scan(
        extracted_stages: list[dict],
        parsed_enemy_rosters: dict,
        our_clan_name: str,
        db,
        api_client,
        engine=AnalyticsEngine,
) -> dict:
    if "live_stats" not in st.session_state:
        st.session_state["live_stats"] = {}

    all_extracted_nicks = set()
    results_lines = []
    low_tp_detected = []
    detected_enemies = []

    our_nicks = set()
    for stg in extracted_stages:
        stg_players = stg.get("players", [])[:25]
        all_extracted_nicks.update(stg_players)
        our_nicks.update(stg_players)

    enemy_nicks = set()
    for clan_k, r_list in parsed_enemy_rosters.items():
        enemy_nicks.update(r_list)

    our_uncached = [n for n in sorted(our_nicks) if n not in st.session_state["live_stats"]]
    enemy_uncached = [n for n in sorted(enemy_nicks) if n not in st.session_state["live_stats"]]
    uncached_nicks = list(dict.fromkeys(our_uncached + enemy_uncached))

    if api_client and uncached_nicks:
        total_uncached = len(uncached_nicks)
        status_box = st.status(
            f"🌐 Отримання даних з API ({total_uncached} нових гравців)...",
            expanded=True,
        )

        if enemy_uncached:
            max_workers = 2
            status_box.write("⚙️ Режим сканування: 2 потоки (Наші гравці + Вороги)...")
        else:
            max_workers = max(1, (total_uncached + 9) // 10)
            status_box.write(f"⚙️ Режим сканування: {max_workers} поток(ів) (10 осіб на 1 поток)...")

        completed_count = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_nick = {
                executor.submit(api_client.fetch_player_stats, nick): nick
                for nick in uncached_nicks
            }
            for future in as_completed(future_to_nick):
                nick = future_to_nick[future]
                completed_count += 1
                try:
                    p_stat = future.result()
                    if p_stat and isinstance(p_stat, dict):
                        st_status = str(p_stat.get("status", "")).upper()
                        if st_status in ["OK", "200", "SUCCESS", "TRUE"] or any(
                                k in p_stat for k in ["kills", "tp", "total_power", "damage_dealt"]
                        ):
                            st.session_state["live_stats"][nick] = p_stat
                            player_tp = engine.calc_tp(p_stat)
                            status_box.write(
                                f"✅ [{completed_count}/{total_uncached}] ТР `{nick}`: **{player_tp} TP**"
                            )
                except Exception as err:
                    status_box.write(f"❌ Помилка запиту для `{nick}`: {err}")

        status_box.update(label="✅ Усі API-запити виконано!", state="complete", expanded=False)

    live_cache = st.session_state.get("live_stats", {})

    for stg in extracted_stages:
        stg_players = stg.get("players", [])[:25]
        our_tp_sum = 0.0

        for nick in stg_players:
            curr = live_cache.get(nick)
            if not curr and hasattr(db, "get_historical_snapshot"):
                try:
                    curr = db.get_historical_snapshot(nick, 0)
                except Exception:
                    curr = {}

            tp = engine.calc_tp(curr if curr else {})
            our_tp_sum += tp

            if tp < 10.0:
                low_tp_detected.append(nick)

        enemy_clan_name = stg.get("enemy_clan", "Unknown")
        if (
                enemy_clan_name
                and enemy_clan_name.lower() != our_clan_name.lower()
                and enemy_clan_name not in detected_enemies
        ):
            detected_enemies.append(enemy_clan_name)

        enemy_tp_str = "?"
        if enemy_clan_name in parsed_enemy_rosters:
            en_tp_sum = 0.0
            for en_nick in parsed_enemy_rosters[enemy_clan_name]:
                en_curr = live_cache.get(en_nick)
                if not en_curr and hasattr(db, "get_historical_snapshot"):
                    try:
                        en_curr = db.get_historical_snapshot(en_nick, 0)
                    except Exception:
                        en_curr = {}

                en_tp_val = engine.calc_tp(en_curr if en_curr else {})
                if en_tp_val < 10.0:
                    low_tp_detected.append(en_nick)
                en_tp_sum += en_tp_val

            if en_tp_sum > 0:
                enemy_tp_str = str(round(en_tp_sum))

        our_score = stg.get("our_score", 0)
        enemy_score = stg.get("enemy_score", 0)
        match_outcome = "WIN" if our_score >= enemy_score else "LOSS"
        score_diff = abs(our_score - enemy_score)
        diff_tag = "(100p+)" if score_diff >= 100 else "(100p-)"

        results_lines.append(
            f"{our_clan_name} ({round(our_tp_sum)}) VS {enemy_clan_name} ({enemy_tp_str}) - {match_outcome} {diff_tag}"
        )

    max_online = (
        max([stg.get("online", 0) for stg in extracted_stages])
        if extracted_stages
        else 0
    )

    return {
        "online": max_online,
        "enemies": "\n".join(detected_enemies),
        "results": "\n".join(results_lines),
        "nicks": sorted(list(all_extracted_nicks)),
        "low_tp": list(set(low_tp_detected)),
        "stages": extracted_stages,
        "enemy_rosters": parsed_enemy_rosters,
    }


# ==========================================
# 3. ДОПОМІЖНІ ФУНКЦІЇ ДЛЯ СИНХРОНІЗАЦІЇ
# ==========================================
def swap_players_in_db(db, group_name: str, p1: str, p2: str):
    """Обмін місцями двох бійців між отрядами та збереженими складами."""
    eq1 = get_player_equip(db, p1)
    eq2 = get_player_equip(db, p2)
    o1 = eq1.get("otryad_num", 0)
    o2 = eq2.get("otryad_num", 0)

    eq1["otryad_num"] = o2
    eq2["otryad_num"] = o1
    save_player_equip(db, p1, group_name, eq1)
    save_player_equip(db, p2, group_name, eq2)

    saved_squads = db.get_squads(group_name) if hasattr(db, "get_squads") else {}
    for sq_name, sq_data in saved_squads.items():
        members = list(sq_data.get("members", []))
        max_s = sq_data.get("max_slots", 5)

        in_1 = p1 in members
        in_2 = p2 in members

        if in_1 and not in_2:
            members = [p2 if m == p1 else m for m in members]
            db.save_squad(sq_name, group_name, max_s, members)
        elif in_2 and not in_1:
            members = [p1 if m == p2 else m for m in members]
            db.save_squad(sq_name, group_name, max_s, members)


def move_player_to_squad_db(db, group_name: str, player: str, target_o_num: int, target_sq_name: str = None):
    """Переміщення гравця в новий отряд із синхронізацією."""
    eq = get_player_equip(db, player)
    eq["otryad_num"] = target_o_num
    save_player_equip(db, player, group_name, eq)

    saved_squads = db.get_squads(group_name) if hasattr(db, "get_squads") else {}
    for sq_name, sq_data in saved_squads.items():
        members = list(sq_data.get("members", []))
        max_s = sq_data.get("max_slots", 5)

        if target_sq_name and sq_name == target_sq_name:
            if player not in members:
                members.append(player)
                db.save_squad(sq_name, group_name, max_s, members)
        else:
            if player in members:
                members.remove(player)
                db.save_squad(sq_name, group_name, max_s, members)


# ==========================================
# 4. ГОЛОВНИЙ МОДУЛЬ УПРАВЛІННЯ КЛАНОМ
# ==========================================
def render_clan_metrics_tab(db, selected_group: str, active_nicks: list, engine=AnalyticsEngine):
    init_clan_db(db)

    st.markdown("""
    <style>
    button[help*="Редагувати"] {
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        padding: 0px 2px !important;
        color: #888 !important;
    }
    button[help*="Редагувати"]:hover {
        color: #00e5ff !important;
    }
    div[data-testid="stColumn"] button {
        margin-bottom: 4px !important;
    }
    </style>
    """, unsafe_allow_html=True)

    st.subheader("🛡️ Клановий менеджмент")

    if not active_nicks:
        st.warning("⚠️ Спочатку оберіть блок/клан у сайдбарі для формування отрядів!")
        return

    clan_tabs = st.tabs([
        "⚔️ Редактор отрядів (Squad Builder)",
        "📌 Активний склад (Статус отрядів)",
        "📅 Робоча зона (Workspace)",
        "📊 Загальні метрики клану"
    ])

    # ---------------------------------------------------------
    # Вкладка 1: РЕДАКТОР ОТРЯДІВ
    # ---------------------------------------------------------
    with clan_tabs[0]:
        st.markdown("### 🪖 Моделювання бойового отряду")
        col_left, col_right = st.columns([1.2, 1.8])

        with col_left:
            saved_squads = db.get_squads(selected_group) if hasattr(db, "get_squads") else {}
            st.markdown("**📂 Збережені отряди:**")
            col_l1, col_l2 = st.columns([2, 1])
            with col_l1:
                load_squad_name = st.selectbox(
                    "Завантажити отряд",
                    options=["-- Новий отряд --"] + list(saved_squads.keys()),
                    key="sb_load_otryad"
                )
            with col_l2:
                uploaded_csv = st.file_uploader("Завантажити CSV", type=["csv"], help="Імпорт складів з CSV")

            if uploaded_csv is not None:
                csv_file_key = f"csv_processed_{uploaded_csv.file_id}"
                if not st.session_state.get(csv_file_key, False):
                    try:
                        df_csv = pd.read_csv(uploaded_csv)
                        if "Нікнейм" in df_csv.columns:
                            squad_members_map = {}
                            squad_titles_map = {}

                            title_to_num_auto = {}
                            auto_num_counter = 1

                            for _, row in df_csv.iterrows():
                                n_nick = clean_val(row.get("Нікнейм"), "")
                                if not n_nick or n_nick == "?":
                                    continue

                                bio_a = clean_val(row.get("Біо-броня"))
                                ass_a = clean_val(row.get("Штурм-броня"))
                                ass_r = clean_val(row.get("Штурм-гвинтівка"))
                                sni_r = clean_val(row.get("Снайперська гвинтівка"))
                                build_v = clean_val(row.get("Збірка"))

                                raw_o_num = str(row.get("Номер отряду", "0")).strip()
                                title_v = clean_val(row.get("Назва отряду"), "")

                                try:
                                    o_num = int(float(raw_o_num))
                                except (ValueError, TypeError):
                                    o_num = 0

                                if o_num == 0 and title_v and title_v != "?":
                                    if title_v not in title_to_num_auto:
                                        title_to_num_auto[title_v] = auto_num_counter
                                        auto_num_counter += 1
                                    o_num = title_to_num_auto[title_v]

                                if not title_v or title_v == "?":
                                    title_v = f"Отряд {o_num}" if o_num > 0 else "Резерв"

                                save_player_equip(db, n_nick, selected_group, {
                                    "bio_armor": bio_a,
                                    "assault_armor": ass_a,
                                    "assault_rifle": ass_r,
                                    "sniper_rifle": sni_r,
                                    "build": build_v,
                                    "otryad_num": o_num
                                })

                                if o_num > 0:
                                    if o_num not in squad_members_map:
                                        squad_members_map[o_num] = []
                                    squad_members_map[o_num].append(n_nick)
                                    squad_titles_map[o_num] = title_v

                            for o_num, members in squad_members_map.items():
                                s_title = squad_titles_map.get(o_num, f"Отряд {o_num}")
                                save_squad_title(db, selected_group, o_num, s_title)
                                if hasattr(db, "save_squad"):
                                    db.save_squad(s_title, selected_group, max(len(members), 5), members)

                            st.session_state[csv_file_key] = True
                            st.toast("✅ CSV успішно завантажено та збережено!", icon="🎉")
                            st.rerun()
                        else:
                            st.error("У CSV файлі обов'язково повинна бути колонка 'Нікнейм'.")
                    except Exception as e:
                        st.error(f"Помилка зчитування CSV: {e}")

            if load_squad_name != "-- Новий отряд --" and load_squad_name in saved_squads:
                sq_info = saved_squads[load_squad_name]
                init_squad_name = load_squad_name
                init_slots = sq_info["max_slots"]
                init_members = [m for m in sq_info["members"] if m in active_nicks]
            else:
                init_squad_name = "Отряд Alpha"
                init_slots = 5
                init_members = []

            squad_name = st.text_input("Назва отряду / Пачки", value=init_squad_name)
            max_slots = st.number_input("Кількість місць у отряді", min_value=1, max_value=30, value=init_slots)

            st.markdown("#### 👤 Налаштування гравця:")
            selected_edit_player = st.selectbox("Оберіть бійця для редагування:", options=sorted(active_nicks))

            if selected_edit_player:
                p_eq = get_player_equip(db, selected_edit_player)
                eq_c1, eq_c2, eq_c3 = st.columns(3)
                with eq_c1:
                    sh_arm = st.text_input("Штурм б.", value=p_eq.get("assault_armor", "?"), key="sh_arm")
                    bio_arm = st.text_input("Біо б.", value=p_eq.get("bio_armor", "?"), key="bio_arm")
                with eq_c2:
                    sh_rif = st.text_input("Штурм г.", value=p_eq.get("assault_rifle", "?"), key="sh_rif")
                    sn_rif = st.text_input("Снайперська г.", value=p_eq.get("sniper_rifle", "?"), key="sn_rif")
                with eq_c3:
                    build_val = st.text_input("Збірка", value=p_eq.get("build", "?"), key="build_val")
                    otryad_n = st.number_input("Номер отряду (0 = Резерв)", min_value=0, max_value=20,
                                               value=int(p_eq.get("otryad_num", 0)), key="otr_num")

                if st.button("💾 Оновити спорядження", width='stretch'):
                    save_player_equip(db, selected_edit_player, selected_group, {
                        "assault_armor": sh_arm, "bio_armor": bio_arm,
                        "assault_rifle": sh_rif, "sniper_rifle": sn_rif,
                        "build": build_val, "otryad_num": otryad_n
                    })
                    st.success(f"Спорядження для {selected_edit_player} оновлено!")
                    time.sleep(0.3)
                    st.rerun()

            st.markdown("---")
            selected_squad_members = st.multiselect(
                f"Оберіть бійців з блоку '{selected_group}':",
                options=sorted(active_nicks),
                default=init_members,
                max_selections=int(max_slots)
            )

            col_sq_b1, col_sq_b2 = st.columns(2)
            with col_sq_b1:
                if st.button("💾 Зберегти отряд", width='stretch'):
                    if squad_name and selected_squad_members:
                        if hasattr(db, "save_squad"):
                            db.save_squad(squad_name, selected_group, max_slots, selected_squad_members)
                        try:
                            o_num_idx = int(re.search(r'\d+', squad_name).group())
                        except Exception:
                            o_num_idx = 1
                        for m in selected_squad_members:
                            eq_m = get_player_equip(db, m)
                            eq_m["otryad_num"] = o_num_idx
                            save_player_equip(db, m, selected_group, eq_m)

                        save_squad_title(db, selected_group, o_num_idx, squad_name)
                        st.success(f"Отряд '{squad_name}' збережено!")
                        time.sleep(0.3)
                        st.rerun()
                    else:
                        st.error("Вкажіть назву та виберіть гравців!")

            with col_sq_b2:
                if saved_squads and squad_name in saved_squads:
                    if st.button("🗑️ Видалити", type="secondary", width='stretch'):
                        if hasattr(db, "delete_squad"):
                            db.delete_squad(squad_name)
                        st.warning(f"Отряд '{squad_name}' видалено!")
                        time.sleep(0.3)
                        st.rerun()

        with col_right:
            if not selected_squad_members:
                st.info("👈 Виберіть бійців ліворуч, щоб вирахувати параметри отряду.")
            else:
                squad_tp_list = []
                squad_details = []

                for nick in selected_squad_members:
                    curr = st.session_state.get("live_stats", {}).get(nick)
                    if not curr and hasattr(db, "get_historical_snapshot"):
                        curr = db.get_historical_snapshot(nick, 0)
                    tp = engine.calc_tp(curr if curr else {})
                    tier = engine.get_tier_letter(tp)
                    squad_tp_list.append(tp)

                    eq_info = get_player_equip(db, nick)
                    kills = curr.get("kills", 0) if isinstance(curr, dict) else 0
                    deaths = curr.get("deaths", 1) if isinstance(curr, dict) else 1

                    squad_details.append({
                        "Нікнейм": nick,
                        "Тир": tier,
                        "Total Power (ТР)": tp,
                        "K/D": engine.calc_ratio(kills, deaths),
                        "Штурм-б.": eq_info.get("assault_armor", "?"),
                        "Штурм-г.": eq_info.get("assault_rifle", "?"),
                        "Збірка": eq_info.get("build", "?"),
                        "№ Отряду": eq_info.get("otryad_num", 0)
                    })

                total_squad_tp = round(sum(squad_tp_list), 1)
                avg_squad_tp = round(total_squad_tp / len(squad_tp_list), 1) if squad_tp_list else 0.0
                avg_squad_tier = engine.get_tier_letter(avg_squad_tp)

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("🔥 Загальна потужність", total_squad_tp)
                m2.metric("📊 Середній ТР бійця", avg_squad_tp)
                m3.metric("🎖️ Усереднений тир", avg_squad_tier)
                m4.metric("👥 Укомплектованість", f"{len(selected_squad_members)} / {max_slots}")

                st.markdown("#### 📜 Склад отряду та спорядження:")
                df_squad = pd.DataFrame(squad_details)
                st.dataframe(df_squad, width='stretch')

    # ---------------------------------------------------------
    # Вкладка 2: АКТИВНИЙ СКЛАД
    # ---------------------------------------------------------
    with clan_tabs[1]:
        st.markdown("### 📌 Активний склад & Стан бойових отрядів")

        c_act1, c_act2 = st.columns([3, 1])
        with c_act2:
            if st.button("🗑️ Очистити всі отряди", help="Скидає номери отрядів для всіх гравців у резерв"):
                clear_all_clan_squads(db, selected_group)
                st.toast("Всі отряди скинуті в Резерв!")
                st.rerun()

        if "selected_swap_player" not in st.session_state:
            st.session_state.selected_swap_player = None

        saved_squads = db.get_squads(selected_group) if hasattr(db, "get_squads") else {}

        active_squads_display = {}
        assigned_players = set()

        if saved_squads:
            for sq_name, sq_data in saved_squads.items():
                m_list = [m for m in sq_data.get("members", []) if m in active_nicks]
                active_squads_display[sq_name] = m_list
                assigned_players.update(m_list)

        for nick in active_nicks:
            eq = get_player_equip(db, nick)
            o_num = eq.get("otryad_num", 0)
            if o_num > 0 and nick not in assigned_players:
                title_val = get_squad_title(db, selected_group, o_num)
                if title_val not in active_squads_display:
                    active_squads_display[title_val] = []
                active_squads_display[title_val].append(nick)
                assigned_players.add(nick)

        reserve_players = [n for n in active_nicks if n not in assigned_players]

        active_tp_total = 0.0
        active_count = 0
        for sq_name, members in active_squads_display.items():
            for m in members:
                curr = st.session_state.get("live_stats", {}).get(m)
                if not curr and hasattr(db, "get_historical_snapshot"):
                    curr = db.get_historical_snapshot(m, 0)
                active_tp_total += engine.calc_tp(curr if curr else {})
                active_count += 1

        active_tp_total = round(active_tp_total, 1)
        avg_active_tp = round(active_tp_total / active_count, 1) if active_count > 0 else 0.0

        top_m1, top_m2, top_m3, top_m4 = st.columns(4)
        top_m1.metric("🔥 Загальний ТР активу", active_tp_total if active_count > 0 else "?")
        top_m2.metric("📊 Середній ТР бійця", avg_active_tp if active_count > 0 else "?")
        top_m3.metric("⚔️ Активних отрядів", len(active_squads_display))
        top_m4.metric("🪖 Всього в резерві", len(reserve_players))

        st.divider()

        selected_p = st.session_state.selected_swap_player
        if selected_p:
            st.info(
                f"🔄 **Обрано для заміни:** `{selected_p}`. Натисніть на іншого бійця або скористайтесь кнопкою 'Перемістити сюди'.")
            if st.button("❌ Скасувати вибір"):
                st.session_state.selected_swap_player = None
                st.rerun()

        st.markdown("#### ⚔️ Бойові отряди")
        sq_keys = list(active_squads_display.keys()) if active_squads_display else ["Отряд 1", "Отряд 2", "Отряд 3",
                                                                                    "Отряд 4", "Отряд 5"]

        grid_cols = st.columns(min(max(len(sq_keys), 1), 5))

        for idx, sq_name in enumerate(sq_keys):
            members = active_squads_display.get(sq_name, [])
            col_target = grid_cols[idx % 5]

            o_tp_list = []
            for m in members:
                curr_m = st.session_state.get("live_stats", {}).get(m)
                if not curr_m and hasattr(db, "get_historical_snapshot"):
                    curr_m = db.get_historical_snapshot(m, 0)
                o_tp_list.append(engine.calc_tp(curr_m if curr_m else {}))

            o_tp = sum(o_tp_list)
            o_tier = engine.get_tier_letter(o_tp / len(members)) if members else "?"

            with col_target:
                with st.popover(f"⚙️ {sq_name}"):
                    st.markdown("**Налаштування отряду**")
                    new_sq_title = st.text_input("Змінити назву:", value=sq_name, key=f"title_in_{sq_name}_{idx}")

                    if saved_squads:
                        selected_squad_bind = st.selectbox(
                            "Обрати отряд з Редактора:",
                            options=["-- Обрати зі збережених --"] + list(saved_squads.keys()),
                            key=f"bind_sq_{sq_name}_{idx}"
                        )
                        if selected_squad_bind != "-- Обрати зі збережених --":
                            s_data = saved_squads[selected_squad_bind]
                            s_members = s_data.get("members", [])
                            for sm in s_members:
                                eq_sm = get_player_equip(db, sm)
                                eq_sm["otryad_num"] = idx + 1
                                save_player_equip(db, sm, selected_group, eq_sm)
                            save_squad_title(db, selected_group, idx + 1, selected_squad_bind)
                            st.success("Отряд успішно завантажено!")
                            time.sleep(0.3)
                            st.rerun()

                    if st.button("Зберегти назву", key=f"save_stitle_{sq_name}_{idx}"):
                        save_squad_title(db, selected_group, idx + 1, new_sq_title)
                        st.success("Назву збережено!")
                        time.sleep(0.3)
                        st.rerun()

                st.markdown(f"""
                <div style="background-color: #1e1f26; border: 1px solid #333444; border-radius: 8px; padding: 10px; margin-bottom: 10px;">
                    <div style="font-weight: bold; font-size: 0.95rem; color: #9D4EDD; border-bottom: 1px solid #444; padding-bottom: 6px; margin-bottom: 8px;">
                        {sq_name.upper()} <span style="font-size: 0.8rem; color: #888;">({len(members)} б.)</span>
                    </div>
                """, unsafe_allow_html=True)

                for m_idx, member_nick in enumerate(members):
                    is_commander = (m_idx == 0)
                    role = "Командир" if is_commander else "Боєць"
                    is_selected = (selected_p == member_nick)

                    if is_selected:
                        btn_label = f"⚡ {member_nick} ({role}) [ОБРАНО]"
                        btn_type = "primary"
                    elif is_commander:
                        btn_label = f"👑 {member_nick} ({role})"
                        btn_type = "secondary"
                    else:
                        btn_label = f"👤 {member_nick} ({role})"
                        btn_type = "secondary"

                    if st.button(btn_label, key=f"card_btn_{member_nick}", width='stretch', type=btn_type):
                        if not selected_p:
                            st.session_state.selected_swap_player = member_nick
                            st.rerun()
                        elif selected_p == member_nick:
                            st.session_state.selected_swap_player = None
                            st.rerun()
                        else:
                            swap_players_in_db(db, selected_group, selected_p, member_nick)
                            st.session_state.selected_swap_player = None
                            st.success(f"Поміняли місцями {selected_p} та {member_nick}!")
                            time.sleep(0.3)
                            st.rerun()

                if selected_p and selected_p not in members:
                    if st.button(f"➕ Перемістити сюди", key=f"move_to_squad_{sq_name}_{idx}", width='stretch'):
                        move_player_to_squad_db(db, selected_group, selected_p, idx + 1, sq_name)
                        st.session_state.selected_swap_player = None
                        st.success(f"{selected_p} переведено у {sq_name}!")
                        time.sleep(0.3)
                        st.rerun()

                st.markdown(f"""
                    <div style="margin-top: 8px; padding-top: 6px; border-top: 1px dashed #444; font-size: 0.8rem; color: #00e5ff;">
                        🔥 ТР: <b>{round(o_tp, 1) if o_tp > 0 else '?'}</b> | Тир: <b>{o_tier}</b>
                    </div>
                </div>
                """, unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("#### 🛡️ Резерв Клана (Номер отряду = 0)")

        res_cols = st.columns(5)
        for r_idx, r_nick in enumerate(reserve_players):
            with res_cols[r_idx % 5]:
                is_selected = (selected_p == r_nick)
                if is_selected:
                    res_btn_label = f"⚡ #{r_idx + 1} {r_nick} [ОБРАНО]"
                    res_btn_type = "primary"
                else:
                    res_btn_label = f"🛡️ #{r_idx + 1} {r_nick}"
                    res_btn_type = "secondary"

                if st.button(res_btn_label, key=f"res_card_btn_{r_nick}", width='stretch', type=res_btn_type):
                    if not selected_p:
                        st.session_state.selected_swap_player = r_nick
                        st.rerun()
                    elif selected_p == r_nick:
                        st.session_state.selected_swap_player = None
                        st.rerun()
                    else:
                        swap_players_in_db(db, selected_group, selected_p, r_nick)
                        st.session_state.selected_swap_player = None
                        st.success(f"Поміняли місцями {selected_p} та {r_nick}!")
                        time.sleep(0.3)
                        st.rerun()

    # ---------------------------------------------------------
    # Вкладка 3: РОБОЧА ЗОНА (WORKSPACE)
    # ---------------------------------------------------------
    with clan_tabs[2]:
        st.markdown("### 📅 Робоча зона (Workspace) - Календар КВ")

        events_data = get_workspace_events(db)

        if "active_edit_date" in st.session_state and st.session_state.active_edit_date:
            edit_date = st.session_state.active_edit_date
            st.markdown(f"### 📝 Редагування запису Workspace за **{edit_date}**")

            cur_ev = events_data.get(edit_date, {})

            # 1. ІНІЦІАЛІЗАЦІЯ КЛЮЧІВ У SESSION STATE (Завжди першою справою)
            if f"title_{edit_date}" not in st.session_state:
                st.session_state[f"title_{edit_date}"] = cur_ev.get("title", f"Кланвар {edit_date}")
            if f"online_{edit_date}" not in st.session_state:
                st.session_state[f"online_{edit_date}"] = int(cur_ev.get("online_count", 0))
            if f"maps_{edit_date}" not in st.session_state:
                st.session_state[f"maps_{edit_date}"] = cur_ev.get("maps", "")
            if f"enemies_{edit_date}" not in st.session_state:
                st.session_state[f"enemies_{edit_date}"] = cur_ev.get("enemy_clans", "")
            if f"results_{edit_date}" not in st.session_state:
                st.session_state[f"results_{edit_date}"] = cur_ev.get("results", "")
            if f"tabs_{edit_date}" not in st.session_state:
                st.session_state[f"tabs_{edit_date}"] = cur_ev.get("tab_urls", "")
            if f"enemy_tabs_{edit_date}" not in st.session_state:
                st.session_state[f"enemy_tabs_{edit_date}"] = cur_ev.get("enemy_tab_urls", "")
            if f"replays_{edit_date}" not in st.session_state:
                st.session_state[f"replays_{edit_date}"] = cur_ev.get("replays_urls", "")
            if f"comments_{edit_date}" not in st.session_state:
                st.session_state[f"comments_{edit_date}"] = cur_ev.get("comments", "")

            if "temp_extracted_nicks" not in st.session_state or st.session_state.get(
                    "editing_date_for_nicks") != edit_date:
                att_str = cur_ev.get("attendance_list", "")
                st.session_state.temp_extracted_nicks = [a.strip() for a in att_str.splitlines() if a.strip()]
                st.session_state.editing_date_for_nicks = edit_date

            # 2. АВТОМАТИЧНЕ ЗАСТОСУВАННЯ ШІ-СКАНУВАННЯ
            if f"pending_ai_{edit_date}" in st.session_state:
                ai_data = st.session_state[f"pending_ai_{edit_date}"]
                if not ai_data.get("low_tp"):
                    popped_ai = st.session_state.pop(f"pending_ai_{edit_date}")
                    st.session_state[f"online_{edit_date}"] = popped_ai["online"]
                    st.session_state[f"enemies_{edit_date}"] = popped_ai["enemies"]
                    st.session_state[f"results_{edit_date}"] = popped_ai["results"]

                    # Оновлюємо і тимчасовий список, і безпосередньо ключ віджета multiselect:
                    st.session_state.temp_extracted_nicks = popped_ai["nicks"]
                    st.session_state["ms_extracted_nicks"] = popped_ai["nicks"]

                    st.session_state.editing_date_for_nicks = edit_date
                    st.success(f"✅ Сканування завершено! Витягнуто гравців: {len(popped_ai['nicks'])}")

            # 3. ОСНОВНІ ПОЛЯ ВВЕДЕННЯ (Рендеримо СПОЧАТКУ, щоб зберегти віджети в циклі Streamlit)
            e_col1, e_col2 = st.columns(2)
            with e_col1:
                st.text_input("Назва КВ", key=f"title_{edit_date}")
                st.number_input("Онлайн (макс. за 3 етапи)", min_value=0, max_value=100, key=f"online_{edit_date}")
                st.text_area("Карти (по 1 в рядок)", key=f"maps_{edit_date}")
                st.text_area("Клани вороги (по 1 в рядок)", key=f"enemies_{edit_date}")
                st.text_area("Результати", key=f"results_{edit_date}")

            with e_col2:
                e_tabs = st.text_area("Посилання на наші таби (до 3 URL, по 1 на рядок)", key=f"tabs_{edit_date}")
                e_enemy_tabs = st.text_area("Посилання/Стовпчик ніків ворогів ([CLAN_NAME])",
                                            key=f"enemy_tabs_{edit_date}")
                st.text_area("Посилання на відкати", key=f"replays_{edit_date}")
                st.text_input("Коментар", key=f"comments_{edit_date}")

                if st.button("🤖 Витягнути та запустити скан (Gemini + ТР)", type="primary", width='stretch'):
                    if not e_tabs.strip():
                        st.error("Вставте хоча б одне посилання на таб!")
                    else:
                        with st.spinner("ШІ витягує нікнейми та здійснює скан через Stalzone API..."):
                            our_clan_name = selected_group
                            raw_tab_lines = [url.strip() for url in e_tabs.splitlines() if url.strip()]
                            extracted_stages_data = engine.parse_tabs(raw_tab_lines, our_clan_name)
                            api_client = get_stalzone_client("EU")
                            parsed_enemy_rosters = parse_enemy_roster_tags(e_enemy_tabs)

                            scan_res = process_workspace_scan(
                                extracted_stages=extracted_stages_data,
                                parsed_enemy_rosters=parsed_enemy_rosters,
                                our_clan_name=our_clan_name,
                                db=db,
                                api_client=api_client,
                                engine=engine
                            )

                            st.session_state[f"pending_ai_{edit_date}"] = scan_res
                            st.rerun()

            # 4. БЛОК ВИПРАВЛЕННЯ НІКНЕЙМІВ (Розміщено ПІСЛЯ полів введення, щоб st.rerun не прала session_state)
            if f"pending_ai_{edit_date}" in st.session_state:
                ai_data = st.session_state[f"pending_ai_{edit_date}"]
                if ai_data.get("low_tp"):
                    st.error(f"⚠️ Виявлено гравців із ТР < 10: {', '.join(ai_data['low_tp'])}")
                    fix_nicks_map = {}
                    for bad_nick in ai_data["low_tp"]:
                        fix_nicks_map[bad_nick] = st.text_input(
                            f"Виправити нікнейм '{bad_nick}':",
                            value=bad_nick,
                            key=f"fix_tp_{bad_nick}"
                        )

                    col_act1, col_act2 = st.columns(2)
                    with col_act1:
                        if st.button("🔄 Оновити виправлених гравців через API", type="primary", width='stretch'):
                            api_client = get_stalzone_client("EU")
                            for old_n, new_n in fix_nicks_map.items():
                                clean_new_n = new_n.strip()
                                if clean_new_n:
                                    if api_client:
                                        fresh_st = api_client.fetch_player_stats(clean_new_n)
                                        if fresh_st and isinstance(fresh_st, dict):
                                            st.session_state["live_stats"][clean_new_n] = fresh_st

                                    for stg in ai_data["stages"]:
                                        stg["players"] = [clean_new_n if p == old_n else p for p in
                                                          stg.get("players", [])]

                                    for clan_k, r_list in ai_data["enemy_rosters"].items():
                                        ai_data["enemy_rosters"][clan_k] = [clean_new_n if p == old_n else p for p in
                                                                            r_list]

                            recalc_results = process_workspace_scan(
                                extracted_stages=ai_data["stages"],
                                parsed_enemy_rosters=ai_data["enemy_rosters"],
                                our_clan_name=selected_group,
                                db=db,
                                api_client=api_client,
                                engine=engine
                            )

                            st.session_state[f"pending_ai_{edit_date}"] = recalc_results
                            st.success("✅ Дані оновлено!")
                            st.rerun()

                    with col_act2:
                        if st.button("⚠️ Пропустити перевірку ТР та зберегти", width='stretch'):
                            ai_data["low_tp"] = []
                            st.session_state[f"pending_ai_{edit_date}"] = ai_data
                            st.rerun()

            # 5. СПИСОК ВИТЯГНУТИХ НІКНЕЙМІВ ТА ЗБЕРЕЖЕННЯ
            if st.session_state.get("temp_extracted_nicks") is not None:
                st.markdown("#### 📋 Список витягнутих нікнеймів наших бійців:")

                # Якщо ключ ще не ініціалізовано у сесії, задаємо його
                if "ms_extracted_nicks" not in st.session_state:
                    st.session_state["ms_extracted_nicks"] = st.session_state.temp_extracted_nicks

                edited_nicks = st.multiselect(
                    "Ви можете відкоригувати список присутніх бійців вручну:",
                    options=sorted(list(set(active_nicks + st.session_state.temp_extracted_nicks))),
                    key="ms_extracted_nicks"
                )
                st.session_state.temp_extracted_nicks = edited_nicks

            btn_save_c1, btn_save_c2 = st.columns(2)
            with btn_save_c1:
                if st.button("💾 Зберегти день", type="primary", width='stretch'):
                    save_workspace_event(db, {
                        "event_date": edit_date,
                        "title": st.session_state[f"title_{edit_date}"],
                        "online_count": st.session_state[f"online_{edit_date}"],
                        "maps": st.session_state[f"maps_{edit_date}"],
                        "enemy_clans": st.session_state[f"enemies_{edit_date}"],
                        "attendance_list": "\n".join(st.session_state.get("temp_extracted_nicks", [])),
                        "tab_urls": st.session_state[f"tabs_{edit_date}"],
                        "enemy_tab_urls": st.session_state[f"enemy_tabs_{edit_date}"],
                        "replays_urls": st.session_state[f"replays_{edit_date}"],
                        "results": st.session_state[f"results_{edit_date}"],
                        "comments": st.session_state[f"comments_{edit_date}"]
                    })
                    # Очищення тимчасового стану редагування
                    for k in [f"title_{edit_date}", f"online_{edit_date}", f"maps_{edit_date}",
                              f"enemies_{edit_date}", f"results_{edit_date}", f"tabs_{edit_date}",
                              f"enemy_tabs_{edit_date}", f"replays_{edit_date}", f"comments_{edit_date}"]:
                        st.session_state.pop(k, None)
                    st.session_state.pop("ms_extracted_nicks", None)
                    st.session_state.pop("temp_extracted_nicks", None)
                    st.session_state.pop("editing_date_for_nicks", None)
                    st.session_state.active_edit_date = None
                    st.success("Запис успішно збережено!")
                    time.sleep(0.3)
                    st.rerun()

            with btn_save_c2:
                if st.button("❌ Відмінити", width='stretch'):
                    for k in [f"title_{edit_date}", f"online_{edit_date}", f"maps_{edit_date}",
                              f"enemies_{edit_date}", f"results_{edit_date}", f"tabs_{edit_date}",
                              f"enemy_tabs_{edit_date}", f"replays_{edit_date}", f"comments_{edit_date}"]:
                        st.session_state.pop(k, None)
                    st.session_state.pop("ms_extracted_nicks", None)
                    st.session_state.pop("temp_extracted_nicks", None)
                    st.session_state.pop("editing_date_for_nicks", None)
                    st.session_state.active_edit_date = None
                    st.rerun()

            st.divider()

        months_map = {
            "Вересень 2026": (2026, 9),
            "Жовтень 2026": (2026, 10),
            "Листопад 2026": (2026, 11),
            "Грудень 2026": (2026, 12),
            "Січень 2027": (2027, 1),
            "Лютий 2027": (2027, 2),
            "Березень 2027": (2027, 3),
            "Квітень 2027": (2027, 4),
            "Травень 2027": (2027, 5),
            "Червень 2027": (2027, 6),
            "Липень 2027": (2027, 7),
            "Серпень 2027": (2027, 8),
            "Вересень 2027": (2027, 9),
        }

        col_search_1, col_search_2 = st.columns([1, 3])
        with col_search_1:
            selected_month_label = st.selectbox("Місяць", list(months_map.keys()), index=0)
        with col_search_2:
            search_query = st.text_input("🔍 Пошук по ключовим словам / противнику:",
                                         placeholder="Наприклад: Phobos або Хвойний").strip()

        st.markdown("---")

        year_val, month_val = months_map[selected_month_label]
        _, days_in_month = calendar.monthrange(year_val, month_val)

        cols_per_row = 7
        days_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

        h_cols = st.columns(cols_per_row)
        for idx, d_name in enumerate(days_names):
            h_cols[idx].markdown(f"**{d_name}**")

        for day_num in range(1, days_in_month + 1):
            if (day_num - 1) % cols_per_row == 0:
                row_cols = st.columns(cols_per_row)

            col_idx = (day_num - 1) % cols_per_row
            date_str = f"{year_val:04d}-{month_val:02d}-{day_num:02d}"
            day_event = events_data.get(date_str, None)

            matches_search = True
            if search_query:
                if day_event:
                    full_text = json.dumps(day_event, ensure_ascii=False).lower()
                    matches_search = search_query.lower() in full_text
                else:
                    matches_search = False

            is_dimmed = search_query and not matches_search
            is_highlighted = search_query and matches_search and day_event

            with row_cols[col_idx]:
                with st.container(height=310, border=True):
                    c_day_head, c_btn_head = st.columns([3, 1])
                    with c_day_head:
                        st.markdown(
                            f"<span style='font-weight: bold; color: #888; font-size: 0.85rem;'>{day_num}</span>",
                            unsafe_allow_html=True)
                    with c_btn_head:
                        if st.button("✏️", key=f"btn_edit_{date_str}", help=f"Редагувати {date_str}"):
                            st.session_state.active_edit_date = date_str
                            st.rerun()

                    if day_event:
                        title_txt = day_event.get('title', '')
                        online_txt = day_event.get('online_count', 0)
                        maps_txt = day_event.get('maps', '')
                        enemy_txt = day_event.get('enemy_clans', '')
                        res_txt = day_event.get('results', '')
                        att_txt = day_event.get('attendance_list', '')
                        enemy_att_txt = day_event.get('enemy_tab_urls', '')
                        replays_txt = day_event.get('replays_urls', '')

                        event_lines = []

                        if title_txt:
                            event_lines.append(
                                f"<div style='font-size: 0.75rem; font-weight: bold; color: #C77DFF; line-height: 1.2; margin-bottom: 2px;'>• {title_txt}</div>"
                            )

                        if online_txt:
                            event_lines.append(
                                f"<div style='font-size: 0.7rem; color: #00F5D4; line-height: 1.2;'>• Онлайн: <b>{online_txt}</b></div>"
                            )

                        def format_with_slashes(text, item_color):
                            if not text:
                                return ""
                            raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
                            items = []
                            for line in raw_lines:
                                if "," in line:
                                    items.extend([x.strip() for x in line.split(",") if x.strip()])
                                else:
                                    items.append(line)
                            sep = "<span style='color: #555770; font-weight: bold; margin: 0 4px;'>/</span>"
                            item_spans = [f"<span style='color: {item_color}; font-weight: 600;'>{item}</span>" for item
                                          in items]
                            return sep.join(item_spans)

                        if maps_txt:
                            formatted_maps = format_with_slashes(maps_txt, "#E2E8F0")
                            event_lines.append(
                                f"<div style='font-size: 0.7rem; line-height: 1.3; margin-top: 2px;'>• {formatted_maps}</div>"
                            )

                        if enemy_txt:
                            formatted_enemies = format_with_slashes(enemy_txt, "#FF70A6")
                            event_lines.append(
                                f"<div style='font-size: 0.7rem; line-height: 1.3; margin-top: 2px;'>• {formatted_enemies}</div>"
                            )

                        if res_txt:
                            res_lines = [r.strip() for r in res_txt.splitlines() if r.strip()]
                            for r_line in res_lines:
                                is_win = "WIN" in r_line.upper()
                                is_loss = "LOSS" in r_line.upper()
                                color = "#38B000" if is_win else ("#FF4D6D" if is_loss else "#FFC6FF")
                                event_lines.append(
                                    f"<div style='font-size: 0.68rem; color: {color}; line-height: 1.25; margin-top: 2px; font-weight: 500;'>• {r_line}</div>"
                                )

                        if att_txt:
                            att_list = [a.strip() for a in att_txt.splitlines() if a.strip()]
                            if att_list:
                                att_html = "<br>".join(att_list)
                                event_lines.append(
                                    f"<details style='margin-top: 4px;'><summary style='cursor: pointer; font-size: 0.7rem; color: #9D4EDD;'>📋 Відвідуваність ({len(att_list)})</summary>"
                                    f"<div style='font-size: 0.65rem; color: #D8F3DC; max-height: 100px; overflow-y: auto; padding: 4px; background: #181920; border-radius: 4px; margin-top: 2px;'>{att_html}</div></details>"
                                )

                        if enemy_att_txt:
                            enemy_lines = [e.strip() for e in enemy_att_txt.splitlines() if
                                           e.strip() and not e.strip().startswith('http')]
                            enemy_nicks = [e for e in enemy_lines if not (e.startswith('[') and e.endswith(']'))]
                            if enemy_nicks:
                                formatted_enemy = []
                                for el in enemy_lines:
                                    if el.startswith('[') and el.endswith(']'):
                                        formatted_enemy.append(f"<b style='color: #FF70A6;'>{el}</b>")
                                    else:
                                        formatted_enemy.append(el)
                                enemy_att_html = "<br>".join(formatted_enemy)
                                event_lines.append(
                                    f"<details style='margin-top: 3px;'><summary style='cursor: pointer; font-size: 0.7rem; color: #FF9E00;'>⚔️ Відвідуваність ворога ({len(enemy_nicks)})</summary>"
                                    f"<div style='font-size: 0.65rem; color: #FFE5EC; max-height: 100px; overflow-y: auto; padding: 4px; background: #181920; border-radius: 4px; margin-top: 2px;'>{enemy_att_html}</div></details>"
                                )

                        if replays_txt:
                            rep_list = [r.strip() for r in replays_txt.splitlines() if r.strip()]
                            if rep_list:
                                rep_links = []
                                for idx_r, r_url in enumerate(rep_list):
                                    rep_links.append(
                                        f"<a href='{r_url}' target='_blank' style='color: #48CAE4; text-decoration: underline;'>Відкат #{idx_r + 1}</a>"
                                    )
                                rep_html = "<br>".join(rep_links)
                                event_lines.append(
                                    f"<details style='margin-top: 3px;'><summary style='cursor: pointer; font-size: 0.7rem; color: #FF70A6;'>🎬 Відкати ({len(rep_list)})</summary>"
                                    f"<div style='font-size: 0.65rem; color: #ccc; max-height: 80px; overflow-y: auto; padding: 4px; background: #181920; border-radius: 4px; margin-top: 2px;'>{rep_html}</div></details>"
                                )

                        content_style = ""
                        if is_dimmed:
                            content_style = "opacity: 0.3;"
                        elif is_highlighted:
                            content_style = "border-left: 2px solid #00F5D4; padding-left: 4px;"

                        st.markdown(f"<div style='{content_style}'>" + "".join(event_lines) + "</div>",
                                    unsafe_allow_html=True)

    # ---------------------------------------------------------
    # Вкладка 4: ЗАГАЛЬНІ МЕТРИКИ КЛАНУ (ГЛОБАЛЬНА СТАТИСТИКА)
    # ---------------------------------------------------------
    with clan_tabs[3]:
        c_head, c_select = st.columns([2.5, 1])
        with c_head:
            st.markdown("### 📊 Загальні глобальні метрики клану")
        with c_select:
            period_days = st.selectbox(
                "Період аналізу (дні):",
                options=[1, 3, 7, 30],
                format_func=lambda x: f"{x} дн." if x > 1 else "1 день",
                key="global_period_select"
            )

        events_data = get_workspace_events(db)

        # Фільтрація подій за обраний період (1, 3, 7, 30 днів)
        sorted_dates = sorted(events_data.keys(), reverse=True)
        period_events = []
        if sorted_dates:
            try:
                latest_date = datetime.strptime(sorted_dates[0], "%Y-%m-%d").date()
            except ValueError:
                latest_date = datetime.now().date()

            for d_str in sorted_dates:
                try:
                    ev_date = datetime.strptime(d_str, "%Y-%m-%d").date()
                    if (latest_date - ev_date).days < period_days:
                        period_events.append(events_data[d_str])
                except ValueError:
                    continue

        if not period_events:
            period_events = list(events_data.values())

        # 1. СЕРЕДНІЙ ОНЛАЙН ТА ДЕЛЬТА
        online_list = [e.get("online_count", 0) for e in period_events if e.get("online_count", 0) > 0]
        avg_online_num = round(sum(online_list) / len(online_list), 1) if online_list else 0.0
        avg_online_val = round(avg_online_num) if online_list else "?"

        latest_online = period_events[0].get("online_count", 0) if period_events else 0
        online_diff = round(latest_online - avg_online_num, 1) if online_list else 0.0
        online_delta_str = f"+{online_diff}" if online_diff > 0 else f"{online_diff}"

        # 2. СЕРЕДНЯ КІЛЬКІСТЬ ГРАНАТ ТА ДЕЛЬТА
        grenades_list = []
        for nick in active_nicks:
            st_data = st.session_state.get("live_stats", {}).get(nick)
            if st_data and isinstance(st_data, dict) and "grenades" in st_data:
                grenades_list.append(engine._safe_float(st_data.get("grenades"), 0.0))

        current_avg_grenades = (sum(grenades_list) / len(grenades_list)) if grenades_list else 0.0

        # Розрахунок історичного середнього за період з БД
        hist_grenades_sums = []
        for nick in active_nicks:
            snap = db.get_historical_snapshot(nick, period_days) if hasattr(db, "get_historical_snapshot") else {}
            if snap and "grenades" in snap:
                hist_grenades_sums.append(engine._safe_float(snap.get("grenades"), 0.0))

        hist_avg_grenades = (
                    sum(hist_grenades_sums) / len(hist_grenades_sums)) if hist_grenades_sums else current_avg_grenades
        grenades_diff = round(current_avg_grenades - hist_avg_grenades, 1)
        avg_grenades_val = st.session_state.get("avg_grenades_val", "?")
        grenades_delta_str = st.session_state.get("grenades_delta", 0)

        # 3. ВІНРЕЙТ ТА ДЕЛЬТА, НАЙБІЛЬША ПЕРЕМОГА
        total_wins = 0
        total_games = 0
        map_stats = {}
        attendance_counter = Counter()

        best_win_clan = "?"
        max_rating = -1

        for ev in period_events:
            res_text = ev.get("results", "")
            res_lines = [r.strip() for r in res_text.split("\n") if r.strip()]
            map_lines = [m.strip() for m in ev.get("maps", "").split("\n") if m.strip()]

            # Зчитування перемог/поразок та статистку по картах
            for idx, res_str in enumerate(res_lines):
                res_upper = res_str.upper()
                is_win = "WIN" in res_upper or res_upper.startswith("W")
                is_loss = "LOSS" in res_upper or res_upper.startswith("L")

                if is_win or is_loss:
                    total_games += 1
                    if is_win:
                        total_wins += 1

                    if idx < len(map_lines):
                        m_name = map_lines[idx]
                        if m_name not in map_stats:
                            map_stats[m_name] = {"wins": 0, "total": 0}
                        map_stats[m_name]["total"] += 1
                        if is_win:
                            map_stats[m_name]["wins"] += 1

            # Облік відвідуваності
            att_players = [p.strip() for p in ev.get("attendance_list", "").split("\n") if p.strip()]
            for p in att_players:
                attendance_counter[p] += 1

            # ВИПРАВЛЕНИЙ БЛОК: Пошук найбільшої перемоги безпосередньо з рядків results
            for line in res_text.splitlines():
                line = line.strip()
                if not line or "WIN" not in line.upper():
                    continue

                # Приклад рядка: "Second Army (2869) VS Over Heaven (2900) - WIN (100p+)"
                match = re.search(r'VS\s+(.+?)\s*\((?:TP\s*)?(\d+)\)\s*-\s*WIN', line, re.IGNORECASE)
                if match:
                    c_name = match.group(1).strip()
                    c_rating = int(match.group(2))

                    if c_rating > max_rating:
                        max_rating = c_rating
                        best_win_clan = f"{c_name} ({c_rating})"

        winrate_pct = round((total_wins / total_games) * 100) if total_games > 0 else 0
        winrate_val = f"{winrate_pct}%" if total_games > 0 else "?"

        # Обчислення дельти вінрейту порівняно з найновішим днем
        latest_res_lines = [r.strip() for r in period_events[0].get("results", "").split("\n") if
                            r.strip()] if period_events else []
        latest_wins = sum(1 for r in latest_res_lines if "WIN" in r.upper())
        latest_total = len(latest_res_lines)
        latest_wr = round((latest_wins / latest_total) * 100) if latest_total > 0 else winrate_pct
        wr_diff = round(latest_wr - winrate_pct, 1)
        winrate_delta_str = f"+{wr_diff}%" if wr_diff > 0 else f"{wr_diff}%"

        # 4. ТР КЛАНУ ТА ДЕЛЬТА ЗА ПЕРІОД
        active_tp_sum = 0.0
        active_count = 0
        for nick in active_nicks:
            eq = get_player_equip(db, nick)
            if eq.get("otryad_num", 0) > 0:
                curr = st.session_state.get("live_stats", {}).get(nick)
                if not curr and hasattr(db, "get_historical_snapshot"):
                    curr = db.get_historical_snapshot(nick, 0)
                active_tp_sum += engine.calc_tp(curr if curr else {})
                active_count += 1

        clan_tp_val = round(active_tp_sum) if active_count > 0 else "?"
        biggest_win_str = best_win_clan if max_rating != -1 else "?"

        def get_clan_tp_at_days(days_ago):
            total_tp = 0.0
            count = 0
            for nick in active_nicks:
                eq = get_player_equip(db, nick)
                if eq.get("otryad_num", 0) > 0:
                    snap = db.get_historical_snapshot(nick, days_ago) if hasattr(db, "get_historical_snapshot") else {}
                    if snap:
                        total_tp += engine.calc_tp(snap)
                    else:
                        curr = st.session_state.get("live_stats", {}).get(nick, {})
                        total_tp += engine.calc_tp(curr)
                    count += 1
            return round(total_tp, 1) if count > 0 else 0.0

        current_clan_tp_num = float(clan_tp_val) if isinstance(clan_tp_val, (int, float)) else 0.0
        tp_period_ago = get_clan_tp_at_days(period_days)
        tp_diff = round(current_clan_tp_num - tp_period_ago, 1) if tp_period_ago > 0 else 0.0

        if tp_diff > 0:
            tp_delta_str = f"+{tp_diff}"
        elif tp_diff < 0:
            tp_delta_str = f"{tp_diff}"
        else:
            tp_delta_str = "0"

        # ВЕРХНІ КАРТКИ ГЛОБАЛЬНОЇ СТАТИСТИКИ
        st.markdown(f"""
        <div style="display: flex; gap: 15px; margin-bottom: 25px; flex-wrap: wrap;">
            <div style="flex: 1; background: #16171d; border: 2px solid #333; border-radius: 8px; padding: 20px; text-align: center;">
                <div style="font-weight: bold; font-size: 1.1rem; color: #fff;">Середній онлайн:</div>
                <div style="font-size: 3rem; font-weight: bold; margin-top: 10px; color: #fff;">{avg_online_val}</div>
                <div style="font-size: 0.85rem; color: #00F5D4; margin-top: 5px;">Δ {online_delta_str}</div>
            </div>
            <div style="flex: 1; background: #16171d; border: 2px solid #333; border-radius: 8px; padding: 20px; text-align: center;">
                <div style="font-weight: bold; font-size: 1.1rem; color: #fff;">Середня кількість гранат:</div>
                <div style="font-size: 3rem; font-weight: bold; margin-top: 10px; color: #fff;">{avg_grenades_val}</div>
                <div style="font-size: 0.85rem; color: #00F5D4; margin-top: 5px;">Δ {grenades_delta_str}</div>
            </div>
            <div style="flex: 1; background: #16171d; border: 2px solid #333; border-radius: 8px; padding: 20px; text-align: center;">
                <div style="font-weight: bold; font-size: 1.1rem; color: #fff;">Вінрейт клану:</div>
                <div style="font-size: 3rem; font-weight: bold; margin-top: 10px; color: #fff;">{winrate_val}</div>
                <div style="font-size: 0.85rem; color: #00F5D4; margin-top: 5px;">Δ {winrate_delta_str}</div>
            </div>
            <div style="flex: 1; background: #16171d; border: 2px solid #333; border-radius: 8px; padding: 20px; text-align: center;">
                <div style="font-weight: bold; font-size: 1.1rem; color: #fff;">ТР клану:</div>
                <div style="font-size: 3rem; font-weight: bold; margin-top: 10px; color: #fff;">{clan_tp_val}</div>
                <div style="font-size: 0.85rem; color: #00F5D4; margin-top: 5px;">Δ {tp_delta_str}</div>
            </div>
            <div style="flex: 1; background: #16171d; border: 2px solid #333; border-radius: 8px; padding: 20px; text-align: center;">
                <div style="font-weight: bold; font-size: 1.1rem; color: #fff;">Найбільша перемога:</div>
                <div style="font-size: 1.5rem; font-weight: bold; margin-top: 15px; color: #fff;">{biggest_win_str}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.divider()

        m_bottom_left, m_bottom_right = st.columns([1, 1.5])

        with m_bottom_left:
            all_players = attendance_counter.most_common()
            if all_players:
                list_items = []
                for idx, (p_name, p_days) in enumerate(all_players, 1):
                    if p_days % 10 == 1 and p_days % 100 != 11:
                        days_str = "День"
                    elif 2 <= p_days % 10 <= 4 and not (12 <= p_days % 100 <= 14):
                        days_str = "Дні"
                    else:
                        days_str = "Днів"

                    if idx <= 3:
                        style = "color: #FFD700; font-weight: bold;"
                    elif 4 <= idx <= 10:
                        style = "color: #4CAF50; font-weight: normal;"
                    else:
                        style = "color: #FFFFFF; font-weight: normal;"

                    list_items.append(
                        f"<li style='margin-bottom: 8px; font-size: 0.95rem; {style}'>"
                        f"{p_name} - {p_days} {days_str} присутності"
                        f"</li>"
                    )

                players_html = "".join(list_items)

                players_card_html = (
                    '<div style="background: #16171d; border: 2px solid #333; border-radius: 8px; padding: 20px; min-height: 230px;">'
                    '<h4 style="color: #fff; margin-top: 0; margin-bottom: 15px; font-weight: bold; font-size: 1.15rem;">'
                    'Топ гравців по активності:'
                    '</h4>'
                    '<div style="max-height: 180px; overflow-y: auto; padding-right: 5px;">'
                    '<ol style="margin: 0; padding-left: 20px;">'
                    f'{players_html}'
                    '</ol>'
                    '</div>'
                    '</div>'
                )
                st.markdown(players_card_html, unsafe_allow_html=True)
            else:
                st.info("Немає даних про відвідуваність у Workspace")

        with m_bottom_right:
            if map_stats:
                top_maps = list(map_stats.items())[:5]
                map_items_html = []

                for m_name, m_data in top_maps:
                    if m_data and m_data["total"] > 0:
                        wr = round((m_data["wins"] / m_data["total"]) * 100)
                        bg_gradient = f"conic-gradient(#1e90ff 0% {wr}%, #ff4b4b {wr}% 100%)"
                        text_disp = f"{wr}%"
                    else:
                        bg_gradient = "conic-gradient(#444 0% 100%)"
                        text_disp = "?"

                    map_items_html.append(
                        '<div style="display: flex; flex-direction: column; align-items: center; text-align: center; width: 80px;">'
                        f'<div style="width: 65px; height: 65px; border-radius: 50%; background: {bg_gradient}; display: flex; align-items: center; justify-content: center; margin: 0 auto;">'
                        '<div style="width: 47px; height: 47px; border-radius: 50%; background-color: #16171d; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 0.9rem; color: #fff;">'
                        f'{text_disp}'
                        '</div>'
                        '</div>'
                        f'<div style="margin-top: 10px; font-weight: bold; color: #fff; font-size: 0.85rem; word-break: break-word; line-height: 1.2;">'
                        f'{m_name}'
                        '</div>'
                        '</div>'
                    )

                maps_card_html = (
                    '<div style="background: #16171d; border: 2px solid #333; border-radius: 8px; padding: 20px; min-height: 230px;">'
                    '<h4 style="color: #fff; margin-top: 0; margin-bottom: 20px; text-align: center; font-weight: bold; font-size: 1.15rem;">'
                    'Вінрейт на картах'
                    '</h4>'
                    '<div style="display: flex; justify-content: space-around; align-items: flex-start; gap: 10px; flex-wrap: wrap;">'
                    f'{"".join(map_items_html)}'
                    '</div>'
                    '</div>'
                )
                st.markdown(maps_card_html, unsafe_allow_html=True)
            else:
                st.info("Немає даних про зіграні карти у Workspace.")
