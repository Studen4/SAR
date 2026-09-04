import datetime
import logging
import os
import re
import shutil
import sqlite3
import time
import copy

from bs4 import BeautifulSoup
import pandas as pd
import pytz
import streamlit as st
from apscheduler.schedulers.background import BackgroundScheduler

# Перевірка наявності Camoufox
try:
    from camoufox.sync_api import Camoufox

    HAS_CAMOUFOX = True
except ImportError:
    HAS_CAMOUFOX = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("StalzoneParser")

KYIV_TZ = pytz.timezone("Europe/Kyiv")

# ==========================================
# 0. ПЕРЕХОПЛЕННЯ СЕСІЇ ЧЕРЕЗ CAMOUFOX
# ==========================================
USER_DATA_DIR = os.path.join(os.getcwd(), "camoufox_profile")
FAILURE_COUNT_FILE = os.path.join(USER_DATA_DIR, ".cf_failure_count")
MAX_CONSECUTIVE_FAILURES = 3


def _read_failure_count() -> int:
    try:
        with open(FAILURE_COUNT_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_failure_count(n: int):
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    with open(FAILURE_COUNT_FILE, "w") as f:
        f.write(str(n))


def _reset_browser_profile():
    if os.path.exists(USER_DATA_DIR):
        logger.warning("🧹 Очищення профілю camoufox_profile...")
        shutil.rmtree(USER_DATA_DIR, ignore_errors=True)
    _write_failure_count(0)


def capture_session_cookies(target_url: str = "https://stalzonehq.com/") -> tuple[str, str]:
    cf_clearance = ""
    user_agent = ""

    if not HAS_CAMOUFOX:
        logger.error("❌ Camoufox не встановлено! Виконайте: pip install camoufox")
        return "", ""

    if _read_failure_count() >= MAX_CONSECUTIVE_FAILURES:
        _reset_browser_profile()

    try:
        with Camoufox(headless=False) as browser:
            page = browser.new_page()
            logger.info("🌐 Запуск Camoufox... Автоматичний обхід Cloudflare.")

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
            except Exception as e:
                logger.warning(f"⚠️ Початкова навігація перервана: {e}")

            start_time = time.time()
            while time.time() - start_time < 60:
                title = ""
                try:
                    title = page.title().lower()
                except Exception:
                    time.sleep(1)
                    continue

                if title and "just a moment" not in title and "cloudflare" not in title:
                    try:
                        cookies = page.context.cookies()
                        for c in cookies:
                            if c["name"] == "cf_clearance":
                                cf_clearance = c["value"]
                                break
                    except Exception:
                        pass

                    if cf_clearance:
                        try:
                            user_agent = page.evaluate("navigator.userAgent")
                        except Exception:
                            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

                        logger.info("✅ Cookie cf_clearance успішно перехоплено!")
                        break

                time.sleep(1.5)

    except Exception as e:
        logger.error(f"❌ Помилка запуску Camoufox: {e}")

    _write_failure_count(0 if cf_clearance else _read_failure_count() + 1)
    return cf_clearance, user_agent


# ==========================================
# 1. ТОЧНИЙ ПАРСЕР ТАБЛИЦІ СТАТИСТИКИ
# ==========================================
class StalzoneParser:
    BASE_URL = "https://stalzonehq.com/characters"

    def __init__(self, region: str = "EU", cf_clearance: str = "", user_agent: str = ""):
        self.region = region.upper()
        self.cf_clearance = cf_clearance.strip()
        self.user_agent = user_agent.strip() or "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

    def fetch_player_stats(self, nickname: str, is_retry: bool = False) -> dict:
        url = f"{self.BASE_URL}/{self.region}/{nickname}"
        logger.info(f"🌐 Запит до URL: {url}" + (" (Повторна спроба)" if is_retry else ""))

        empty_stats = {
            "nickname": nickname, "status": "Error",
            "kills": 0, "assists": 0, "deaths": 0, "playtime": 0,
            "damage_dealt": 0, "damage_recv": 0, "grenades": 0,
            "headshots": 0, "bodyshots": 0, "limbshots": 0
        }

        if not HAS_CAMOUFOX:
            logger.error("❌ Camoufox не встановлено!")
            return empty_stats

        html_content, status_code = self._fetch_with_camoufox(url)

        if not html_content or status_code != 200:
            if not is_retry:
                time.sleep(1.5)
                return self.fetch_player_stats(nickname, is_retry=True)
            self._print_debug_console(nickname, status_code, empty_stats, is_error=True)
            return empty_stats

        soup = BeautifulSoup(html_content, "html.parser")
        stats = self._parse_table_rows(soup, nickname)

        if (stats["kills"] == 0 and stats["deaths"] == 0 and stats["playtime"] == 0) and not is_retry:
            logger.warning(f"⚠️ Нульові показники для {nickname}. Запуск авто-повтору...")
            time.sleep(1.5)
            return self.fetch_player_stats(nickname, is_retry=True)

        self._print_debug_console(nickname, status_code, stats, is_error=False)
        return stats

    def _fetch_with_camoufox(self, url: str) -> tuple[str, int]:
        try:
            with Camoufox(headless=True) as browser:
                context = browser.new_context()

                if self.cf_clearance:
                    try:
                        context.add_cookies([{
                            "name": "cf_clearance",
                            "value": self.cf_clearance,
                            "domain": ".stalzonehq.com",
                            "path": "/"
                        }])
                    except Exception:
                        pass

                page = context.new_page()
                logger.info(f"⏳ Перехід до {url} через Camoufox...")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=35000)
                except Exception as e:
                    logger.warning(f"⚠️ Перехід через Camoufox перервано навігацією: {e}")

                start_time = time.time()
                stats_found = False

                while time.time() - start_time < 30:
                    try:
                        if page.locator(".character-stat").count() >= 5:
                            stats_found = True
                            logger.info("✅ Статистику успішно завантажено!")
                            break
                    except Exception:
                        pass
                    time.sleep(1)

                try:
                    for c in context.cookies():
                        if c["name"] == "cf_clearance":
                            self.cf_clearance = c["value"]
                            if "cf_clearance" in st.session_state:
                                st.session_state.cf_clearance = c["value"]
                except Exception:
                    pass

                try:
                    new_ua = page.evaluate("navigator.userAgent")
                    if new_ua:
                        self.user_agent = new_ua
                        if "user_agent" in st.session_state:
                            st.session_state.user_agent = new_ua
                except Exception:
                    pass

                content = ""
                try:
                    content = page.content()
                except Exception:
                    pass

                status_code = 200 if stats_found else 403
                return content, status_code

        except Exception as e:
            logger.error(f"❌ Помилка Camoufox: {e}")
            return "", 500

    def _clean_number(self, text: str) -> int:
        if not text:
            return 0
        s = text.replace('\xa0', '').replace('&nbsp;', '').replace(' ', '').strip()
        s = re.sub(r'[\.,]\d{1,2}$', '', s)
        digits = re.sub(r'[^\d]', '', s)
        return int(digits) if digits else 0

    def _parse_playtime(self, text: str) -> int:
        if not text:
            return 0
        h_match = re.search(r'(\d+)\s*(?:hours?|hour|год|ч)', text, re.IGNORECASE)
        m_match = re.search(r'(\d+)\s*(?:minutes?|minute|хв|мин)', text, re.IGNORECASE)

        hours = int(h_match.group(1)) if h_match else 0
        minutes = int(m_match.group(1)) if m_match else 0

        if h_match or m_match:
            return (hours * 60) + minutes
        return self._clean_number(text)

    def _parse_table_rows(self, soup: BeautifulSoup, nickname: str) -> dict:
        stats = {
            "nickname": nickname, "status": "OK",
            "kills": 0, "assists": 0, "deaths": 0, "playtime": 0,
            "damage_dealt": 0, "damage_recv": 0, "grenades": 0,
            "headshots": 0, "bodyshots": 0, "limbshots": 0
        }

        for tr in soup.find_all("tr"):
            stat_span = tr.find(class_=lambda c: c and "character-stat" in c)
            if not stat_span:
                continue

            row_text = tr.text.lower().strip()
            val_text = stat_span.text.strip()

            if any(k in row_text for k in ["kills", "убито"]):
                if not stats["kills"]: stats["kills"] = self._clean_number(val_text)
            elif any(k in row_text for k in ["assists", "помощь", "допомога"]):
                if not stats["assists"]: stats["assists"] = self._clean_number(val_text)
            elif any(k in row_text for k in ["deaths", "смертей"]):
                if not stats["deaths"]: stats["deaths"] = self._clean_number(val_text)
            elif any(k in row_text for k in ["time played", "playtime", "time", "времени", "время", "наиграно", "час"]):
                if not stats["playtime"]: stats["playtime"] = self._parse_playtime(val_text)
            elif any(k in row_text for k in ["damage dealt", "нанесено"]):
                if not stats["damage_dealt"]: stats["damage_dealt"] = self._clean_number(val_text)
            elif any(k in row_text for k in ["damage received", "damage recv", "получено", "отримано"]):
                if not stats["damage_recv"]: stats["damage_recv"] = self._clean_number(val_text)
            elif any(k in row_text for k in ["grenades", "гранат"]):
                if not stats["grenades"]: stats["grenades"] = self._clean_number(val_text)
            elif any(k in row_text for k in ["shots to head", "headshots", "head shots", "голову"]):
                if not stats["headshots"]: stats["headshots"] = self._clean_number(val_text)
            elif any(k in row_text for k in
                     ["shots to torso", "torso", "body shots", "bodyshots", "тело", "туловище", "тіло"]):
                if not stats["bodyshots"]: stats["bodyshots"] = self._clean_number(val_text)
            elif any(k in row_text for k in
                     ["shots to limbs", "limb shots", "limbshots", "limbs", "конечности", "кінцівки"]):
                if not stats["limbshots"]: stats["limbshots"] = self._clean_number(val_text)

        return stats

    def _print_debug_console(self, nickname: str, status_code: int, stats: dict, is_error: bool):
        print("\n" + "=" * 60)
        print(f"🔍 [DEBUG SCAN] Гравець: {nickname} | HTTP Status: {status_code}")
        print("-" * 60)
        if is_error:
            print(f"⚠️ ПОМИЛКА ОТРИМАННЯ ДАНИХ (Статус {status_code})")
        else:
            print("📋 Очищені значення для БД та Streamlit:")
            print(f"   1. Убито игроков (Kills):           {stats['kills']}")
            print(f"   2. Помощь в убийстве (Assists):     {stats['assists']}")
            print(f"   3. Смертей (Deaths):               {stats['deaths']}")
            print(f"   4. Времени в игре (Playtime):      {stats['playtime']} хв.")
            print(f"   5. Нанесено урона (Damage Dealt):   {stats['damage_dealt']}")
            print(f"   6. Получено урона (Damage Recv):    {stats['damage_recv']}")
            print(f"   7. Гранат брошено (Grenades):       {stats['grenades']}")
            print(f"   8. Выстрелов в голову (Headshots):   {stats['headshots']}")
            print(f"   9. Выстрелов в тело (Bodyshots):     {stats['bodyshots']}")
            print(f"   10. Выстрелов в конечности (Limbs):  {stats['limbshots']}")
        print("=" * 60 + "\n")


# ==========================================
# 2. МЕНЕДЖЕР БД (SQLite)
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
                    nicknames TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_type TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    kills INTEGER, assists INTEGER, deaths INTEGER, playtime INTEGER,
                    damage_dealt INTEGER, damage_recv INTEGER, grenades INTEGER,
                    headshots INTEGER, bodyshots INTEGER, limbshots INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_group(self, name: str, nicknames: list):
        nicks_str = ",".join([n.strip() for n in nicknames if n.strip()])
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO player_groups (group_name, nicknames) VALUES (?, ?)
                ON CONFLICT(group_name) DO UPDATE SET nicknames=excluded.nicknames
            """, (name, nicks_str))
            conn.commit()

    def get_groups(self) -> dict:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT group_name, nicknames FROM player_groups")
            rows = cursor.fetchall()
            return {row[0]: [n.strip() for n in row[1].split(",") if n.strip()] for row in rows}

    def clear_daily_data(self, snapshot_type: str = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if snapshot_type:
                cursor.execute("DELETE FROM daily_snapshots WHERE snapshot_type = ?", (snapshot_type,))
            else:
                cursor.execute("DELETE FROM daily_snapshots")
            conn.commit()

    def save_snapshot(self, snapshot_type: str, stats: dict):
        with self.get_connection() as conn:
            cursor = conn.cursor()
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


# ==========================================
# 3. АНАЛІТИЧНИЙ ДВИГУН
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

        return {
            "nickname": end_stats.get("nickname", "Unknown"),
            "cw_score": f"{d_kills} / {d_deaths} / {d_assists}",
            "kd": kd,
            "kad": k_a_d,
            "grenades": d_grenades,
            "shots": f"Г: {d_head} | Т: {d_body} | К: {d_limb}",
            "accuracy": f"🎯 {acc_head}% / 🧍 {acc_body}% / 🦵 {acc_limb}%",
            "damage": f"+{d_dmg_dealt} / -{d_dmg_recv}",
            "un_up": un_up
        }


# ==========================================
# 4. ПЛАНИРОВЩИК
# ==========================================
class SchedulerManager:
    def __init__(self, db: DatabaseManager, region: str = "EU", cf_clearance: str = "", user_agent: str = ""):
        self.db = db
        self.parser = StalzoneParser(region, cf_clearance, user_agent)
        self.scheduler = BackgroundScheduler(timezone=KYIV_TZ)

    def start(self):
        if not self.scheduler.running:
            logger.info("Планувальник автозамірів вимкнено (активно режим відладки).")


# ==========================================
# 5. STREAMLIT ІНТЕРФЕЙС
# ==========================================
def main():
    st.set_page_config(page_title="Stalzone HQ Stats Parser", layout="wide", initial_sidebar_state="expanded")

    # --- КАСТОМНИЙ CSS ДЛЯ КНОПКИ СКАЛУВАТИ ---
    st.markdown("""
    <style>
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
    </style>
    """, unsafe_allow_html=True)

    if "db" not in st.session_state:
        st.session_state.db = DatabaseManager()

    db = st.session_state.db

    if "cf_clearance" not in st.session_state:
        st.session_state.cf_clearance = ""
    if "user_agent" not in st.session_state:
        st.session_state.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

    # --- КЕШ ДЛЯ МИТТЄВОГО ЗАПИСУ ---
    if "live_stats" not in st.session_state:
        st.session_state.live_stats = {}

    # --- СТАНИ ТА ВІДНОВЛЕННЯ ЕКСТРЕННОГО ЗРІЗУ З БД ---
    snap_c_start = db.get_snapshot("CUSTOM_START")
    snap_c_end = db.get_snapshot("CUSTOM_END")

    st.session_state.custom_start = snap_c_start
    st.session_state.custom_end = snap_c_end

    # Визначаємо чи активне відстеження (початок збережено, а кінця ще немає)
    is_tracking_active = bool(st.session_state.custom_start and not st.session_state.custom_end)

    # ---------------- SIDEMENU ----------------
    with st.sidebar:
        st.header("⚙️ Налаштування та Блоки")
        region = st.selectbox("Регіон", ["EU", "RU", "SEA", "NA", "NEA"], index=0)

        st.divider()
        st.subheader("🦊 Обхід Cloudflare (Camoufox)")

        # ЛІНІЯ 1: Кнопка "Отримати сесію"
        if st.button("🌐 Отримати сесію", type="secondary", use_container_width=True):
            with st.spinner("Відкриваємо Camoufox... Проходження перевірки Cloudflare."):
                cookie_val, ua_val = capture_session_cookies("https://stalzonehq.com/")
                if cookie_val:
                    st.session_state.cf_clearance = cookie_val
                    st.session_state.user_agent = ua_val
                    st.success("✅ Сесію збережено!")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("❌ Cookie не знайдено. Спробуйте ще раз.")

        # ЛІНІЯ 2: Три кнопки (Мітла, Дискета, Екстренний зріз)
        col_btn1, col_btn2, col_btn3 = st.columns(3)

        with col_btn1:
            if st.button("🧹", help="Скинути профіль (очистити браузер)", use_container_width=True):
                _reset_browser_profile()
                st.session_state.cf_clearance = ""
                st.session_state.user_agent = ""
                st.warning("🧹 Профіль очищено!")
                time.sleep(1)
                st.rerun()

        with col_btn2:
            if st.session_state.live_stats:
                df_export = pd.DataFrame.from_dict(st.session_state.live_stats, orient='index')
                csv_data = df_export.to_csv(index_label="Нікнейм").encode('utf-8')
                file_name = f"stalzone_stats_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            else:
                csv_data = ""
                file_name = "empty.csv"

            st.download_button(
                label="💾",
                data=csv_data,
                file_name=file_name,
                mime="text/csv",
                help="Зберегти поточний зріз у CSV",
                use_container_width=True,
                disabled=not bool(st.session_state.live_stats)
            )

        with col_btn3:
            btn_label = "🛑" if is_tracking_active else "⏱️"
            btn_help = "Записати кінець екстренного зрізу" if is_tracking_active else "Записати початок екстренного зрізу"

            if st.button(btn_label, help=btn_help, use_container_width=True):
                if not st.session_state.live_stats:
                    st.warning("Спочатку відскануйте дані (кнопка 'Сканувати зараз')!")
                else:
                    if not is_tracking_active:
                        # Запис ПОЧАТКУ зрізу в БД
                        db.clear_daily_data("CUSTOM_START")
                        db.clear_daily_data("CUSTOM_END")
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("CUSTOM_START", stats)
                        st.success("⏱️ Початок екстренного зрізу зафіксовано в БД!")
                    else:
                        # Запис КІНЦЯ зрізу в БД
                        db.clear_daily_data("CUSTOM_END")
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("CUSTOM_END", stats)
                        st.success("✅ Кінець екстренного зрізу зафіксовано в БД! Дані доступні в Аналітиці.")
                    time.sleep(1.5)
                    st.rerun()

        cf_clearance = st.text_input("Cookie cf_clearance", value=st.session_state.cf_clearance, type="password")
        user_agent = st.text_input("User-Agent браузера", value=st.session_state.user_agent)

        st.session_state.cf_clearance = cf_clearance
        st.session_state.user_agent = user_agent

        if "scheduler" not in st.session_state:
            st.session_state.scheduler = SchedulerManager(db, region, cf_clearance, user_agent)
            st.session_state.scheduler.start()
        else:
            st.session_state.scheduler.parser.cf_clearance = cf_clearance
            st.session_state.scheduler.parser.user_agent = user_agent

        st.divider()
        st.subheader("Управління блоками")

        groups = db.get_groups()
        group_names = list(groups.keys())
        selected_group = st.selectbox("Обрати блок для перегляду", options=["-- Не обрано --"] + group_names)

        active_nicks = groups.get(selected_group, []) if selected_group and selected_group != "-- Не обрано --" else []

        st.markdown("---")
        with st.expander("🛠️ Тестові кнопки замірів (Debug)", expanded=False):
            st.caption("Керування зрізами в БД для ручної перевірки обчислень.")

            if st.button("🗑️ Скинути всі заміри (00:00 / 21:00 / CW_END / Екстренний)", type="secondary",
                         use_container_width=True):
                db.clear_daily_data()
                st.session_state.live_stats = {}
                st.session_state.custom_start = {}
                st.session_state.custom_end = {}
                st.success("🧹 Усі зрізи успішно видалено з БД!")
                time.sleep(1)
                st.rerun()

            # ПЕРЕНЕСЕНА КНОПКА ВИДАЛЕННЯ ЕКСТРЕННОГО ЗРІЗУ
            has_custom_data = bool(st.session_state.custom_start or st.session_state.custom_end)
            if st.button("🗑️⏱️ Видалити тільки екстренний зріз", use_container_width=True,
                         disabled=not has_custom_data):
                db.clear_daily_data("CUSTOM_START")
                db.clear_daily_data("CUSTOM_END")
                st.session_state.custom_start = {}
                st.session_state.custom_end = {}
                st.warning("🗑️ Екстренний зріз видалено!")
                time.sleep(1)
                st.rerun()

            st.divider()

            if st.button("📸 Записати 00:00", use_container_width=True):
                if not active_nicks:
                    st.error("Оберіть блок гравців у списку вище!")
                elif not st.session_state.live_stats:
                    st.warning("Спочатку відскануйте дані (кнопка 'Сканувати зараз')!")
                else:
                    for nick in active_nicks:
                        if nick in st.session_state.live_stats:
                            db.save_snapshot("00:00", st.session_state.live_stats[nick])
                    st.success("✅ Зріз 00:00 збережено миттєво!")
                    time.sleep(1)
                    st.rerun()

            if st.button("📸 Записати 21:00", use_container_width=True):
                if not active_nicks:
                    st.error("Оберіть блок гравців у списку вище!")
                elif not st.session_state.live_stats:
                    st.warning("Спочатку відскануйте дані (кнопка 'Сканувати зараз')!")
                else:
                    for nick in active_nicks:
                        if nick in st.session_state.live_stats:
                            db.save_snapshot("21:00", st.session_state.live_stats[nick])

                    # АВТОМАТИЧНЕ ВИДАЛЕННЯ ЕКСТРЕННОГО ЗРІЗУ ПРИ ЗАПИСІ 21:00
                    db.clear_daily_data("CUSTOM_START")
                    db.clear_daily_data("CUSTOM_END")
                    st.session_state.custom_start = {}
                    st.session_state.custom_end = {}

                    st.success("✅ Зріз 21:00 збережено! Екстренний зріз автоматично очищено.")
                    time.sleep(1)
                    st.rerun()

            if st.button("📸 Записати 21:55 / 22:10 (CW_END)", use_container_width=True):
                if not active_nicks:
                    st.error("Оберіть блок гравців у списку вище!")
                elif not st.session_state.live_stats:
                    st.warning("Спочатку відскануйте дані (кнопка 'Сканувати зараз')!")
                else:
                    for nick in active_nicks:
                        if nick in st.session_state.live_stats:
                            db.save_snapshot("CW_END", st.session_state.live_stats[nick])
                    st.success("✅ Зріз CW_END збережено миттєво!")
                    time.sleep(1)
                    st.rerun()

        with st.expander("➕ Створити / Редагувати блок"):
            new_group_name = st.text_input("Назва блоку (напр. Склад 1)")
            default_nicks = "\n".join(groups.get(new_group_name, [])) if new_group_name in groups else ""
            nicks_input = st.text_area("Нікнейми (по одному на рядок)", value=default_nicks, height=150)

            if st.button("Зберегти блок", use_container_width=True):
                if new_group_name and nicks_input:
                    nicks_list = [n.strip() for n in nicks_input.split("\n") if n.strip()]
                    db.save_group(new_group_name, nicks_list)
                    st.success(f"Блок '{new_group_name}' збережено!")
                    st.rerun()

    # ---------------- ГОЛОВНИЙ ЕКРАН ----------------
    col_mode, col_btn = st.columns([2, 1], vertical_alignment="center")

    with col_mode:
        mode = st.radio(
            "Режим відображення даних:",
            options=["До КВ (Основна статистика)", "Після КВ \ Зріз (Аналітика)"],
            horizontal=True
        )

    with col_btn:
        manual_scan = st.button("🔄 Сканувати зараз", type="primary", use_container_width=True)

    # --- ЛОГІКА СКАНУВАННЯ З ПРОГРЕС-БАРОМ ---
    if manual_scan:
        if not active_nicks:
            st.error("Помилка: Спочатку оберіть блок гравців у лівому меню!")
        else:
            parser = StalzoneParser(region, st.session_state.cf_clearance, st.session_state.user_agent)

            progress_bar = st.progress(0)
            status_text = st.empty()

            total_nicks = len(active_nicks)
            for i, nick in enumerate(active_nicks):
                status_text.markdown(f"**Сканування гравця:** `{nick}` ({i + 1}/{total_nicks}) ... ⏳")
                st.session_state.live_stats[nick] = parser.fetch_player_stats(nick)
                progress_bar.progress((i + 1) / total_nicks)

            status_text.success("✅ Сканування успішно завершено!")
            time.sleep(1.5)
            status_text.empty()
            progress_bar.empty()
            st.rerun()

    snap_00 = db.get_snapshot("00:00")
    snap_21 = db.get_snapshot("21:00")
    snap_cw = db.get_snapshot("CW_END")

    st.divider()

    if not active_nicks:
        st.info("👈 Оберіть або створіть блок гравців у панелі ліворуч.")
        return

    # ---------------- РЕЖИМ 1: ДО КВ (ОСНОВНА СТАТИСТИКА) ----------------
    if mode == "До КВ (Основна статистика)":
        st.subheader("📋 Основна статистика (Розминка / Гра сьогодні)")

        act_rows = []
        for nick in active_nicks:
            base = snap_00.get(nick, {})
            curr = st.session_state.live_stats.get(nick, snap_21.get(nick, {}))

            if not curr and not base:
                act_rows.append({
                    "Нікнейм": nick,
                    "Статус": "⚪ Немає даних",
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

                played_status = "🟢 Розминався" if (d_kills > 0 or d_deaths > 0 or d_time > 0) else "🔴 Не грав"

                act_rows.append({
                    "Нікнейм": nick,
                    "Статус": played_status,
                    "Убито гравців": c_kills,
                    "Помощь": c_assists,
                    "Смертей": c_deaths,
                    "Час у грі": f"{c_playtime} хв.",
                    "Приріст за день (Уб / См)": f"+{d_kills} / +{d_deaths}"
                })

        df_pre = pd.DataFrame(act_rows)
        st.dataframe(df_pre, use_container_width=True, height=450)

    # ---------------- РЕЖИМ 2: ПІСЛЯ КВ \ ЗРІЗ (АНАЛІТИКА) ----------------
    else:
        st.subheader("⚔️ Аналітичний блок (КВ / Екстрений зріз)")

        is_custom_complete = bool(st.session_state.custom_start and st.session_state.custom_end)

        analytics_source = "Стандартний КВ"
        if is_custom_complete:
            col_info, col_choice = st.columns([2, 1])
            with col_info:
                st.info("⏱️ Знайдено збережений **Екстренний зріз** (Початок -> Кінець).")
            with col_choice:
                analytics_source = st.radio(
                    "Джерело аналітики:",
                    options=["⏱️ Екстренний зріз", "⚔️ Стандартний КВ (21:00 -> КВ)"],
                    horizontal=True
                )
        elif is_tracking_active:
            st.warning("⏳ **Екстренний зріз у процесі!** Зробіть повторний скан та натисніть 🛑 для фіксації кінця.")
        else:
            st.info("⚔️ Відображається стандартна статистика КВ (21:00 -> кінець КВ або поточний скан).")

        rows = []
        use_custom = (analytics_source == "⏱️ Екстренний зріз") and is_custom_complete

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

        def apply_colors(val):
            if isinstance(val, (int, float)):
                return AnalyticsEngine.get_color_style(val)
            return ""

        styled_df = (
            df_post.style
            .format("{:.2f}", subset=["У/С", "УП/С", "УН/УП"])
            .map(apply_colors, subset=["У/С", "УП/С", "УН/УП"])
        )
        st.dataframe(styled_df, use_container_width=True, height=450)


if __name__ == "__main__":
    main()