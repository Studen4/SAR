import sqlite3
import datetime
import pytz

KYIV_TZ = pytz.timezone("Europe/Kyiv")

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
        with self.get_connection() as conn:
            cursor = conn.cursor()
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
        