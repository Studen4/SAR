import datetime
import json
import os
import time
import pandas as pd
import streamlit as st
from analytics import AnalyticsEngine
from database import DatabaseManager, KYIV_TZ
from data_fetcher import StalzoneApiClient, API_CLIENT_ID, API_CLIENT_SECRET


def check_authentication() -> bool:
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        auth_file = "users.json"

        if not os.path.exists(auth_file):
            with open(auth_file, "w", encoding="utf-8") as f:
                json.dump({"admin": "admin123", "friend": "stalzone2026"}, f, indent=4)

        try:
            with open(auth_file, "r", encoding="utf-8") as f:
                users_db = json.load(f)
        except Exception:
            users_db = {"admin": "admin123"}

        _, col_center, _ = st.columns([1, 1.2, 1])
        with col_center:
            st.markdown("<h2 style='text-align: center;'>🔐 Вхід до Stalzone Analytic Review</h2>",
                        unsafe_allow_html=True)
            username = st.text_input("Логін")
            password = st.text_input("Пароль", type="password")
            if st.button("Увійти", type="primary", width="stretch"):
                if username in users_db and users_db[username] == password:
                    st.session_state.authenticated = True
                    st.session_state.username = username
                    st.success("Успішний вхід!")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("Невірний логін або пароль")
        return False
    return True


def apply_custom_styles():
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


def render_sidebar(db: DatabaseManager, is_tracking_active: bool):
    with st.sidebar:
        st.write(f"👤 Користувач: **{st.session_state.get('username', 'Admin')}**")
        if st.button("Вийти", width="stretch"):
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
                width="stretch",
                disabled=not bool(st.session_state.live_stats)
            )

        with col_btn2:
            btn_label = "🛑 Кінець" if is_tracking_active else "⏱️ Початок"
            btn_help = "Записати кінець екстренного зрізу" if is_tracking_active else "Записати початок екстренного зрізу"

            if st.button(btn_label, help=btn_help, width="stretch"):
                if not st.session_state.live_stats:
                    st.warning("Спочатку отримайте дані через кнопку 'Отримати дані'!")
                else:
                    if not is_tracking_active:
                        db.clear_daily_data("CUSTOM_START")
                        db.clear_daily_data("CUSTOM_END")
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("CUSTOM_START", stats)
                        st.success("⏱️ Початок екстренного зрізу зафіксовано в БД!")
                    else:
                        db.clear_daily_data("CUSTOM_END")
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("CUSTOM_END", stats)
                        st.success("✅ Кінець екстренного зрізу зафіксовано в БД!")
                    time.sleep(1.5)
                    st.rerun()

        st.divider()
        st.subheader("Управління блоками")

        groups = db.get_groups()
        group_names = list(groups.keys())
        selected_group = st.selectbox("Обрати блок для перегляду", options=["-- Не обрано --"] + group_names)

        active_nicks = groups.get(selected_group, []) if selected_group and selected_group != "-- Не обрано --" else []

        st.markdown("---")
        with st.expander("🛠️ Тестові кнопки замірів (Debug)", expanded=False):
            st.caption("Керування зрізами в БД для ручної перевірки обчислень.")

            if st.button("🗑️ Скинути всі заміри", type="secondary", width="stretch"):
                db.clear_daily_data()
                st.session_state.live_stats = {}
                st.session_state.custom_start = {}
                st.session_state.custom_end = {}
                st.success("🧹 Усі зрізи успішно видалено з БД!")
                time.sleep(1)
                st.rerun()

            has_custom_data = bool(st.session_state.custom_start or st.session_state.custom_end)
            if st.button("🗑️⏱️ Видалити екстренний зріз", width="stretch", disabled=not has_custom_data):
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
                if st.button("📸 1 день тому", width="stretch", help="Зберегти поточні дані з датою -1 день"):
                    if not st.session_state.live_stats:
                        st.warning("Спочатку спарсіть/отримайте дані!")
                    else:
                        ts_1d = (datetime.datetime.now(KYIV_TZ) - datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("DAILY_TEST_1D", stats, custom_timestamp=ts_1d)
                        st.success("✅ Записано тестовий зріз за 1 день тому!")

            with col_hist2:
                if st.button("📸 7 днів тому", width="stretch", help="Зберегти поточні дані з датою -7 днів"):
                    if not st.session_state.live_stats:
                        st.warning("Спочатку спарсіть/отримайте дані!")
                    else:
                        ts_7d = (datetime.datetime.now(KYIV_TZ) - datetime.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
                        for nick, stats in st.session_state.live_stats.items():
                            db.save_snapshot("DAILY_TEST_7D", stats, custom_timestamp=ts_7d)
                        st.success("✅ Записано тестовий зріз за 7 днів тому!")

            st.divider()

            if st.button("📸 Записати 00:00", width="stretch"):
                if not active_nicks:
                    st.error("Оберіть блок гравців у списку вище!")
                elif not st.session_state.live_stats:
                    st.warning("Спочатку отримайте дані ('Отримати дані')!")
                else:
                    for nick in active_nicks:
                        if nick in st.session_state.live_stats:
                            db.save_snapshot("00:00", st.session_state.live_stats[nick])
                    st.success("✅ Зріз 00:00 збережено!")
                    time.sleep(1)
                    st.rerun()

            if st.button("📸 Записати 21:00", width="stretch"):
                if not active_nicks:
                    st.error("Оберіть блок гравців у списку вище!")
                elif not st.session_state.live_stats:
                    st.warning("Спочатку отримайте дані ('Отримати дані')!")
                else:
                    for nick in active_nicks:
                        if nick in st.session_state.live_stats:
                            db.save_snapshot("21:00", st.session_state.live_stats[nick])

                    db.clear_daily_data("CUSTOM_START")
                    db.clear_daily_data("CUSTOM_END")
                    st.session_state.custom_start = {}
                    st.session_state.custom_end = {}
                    st.success("✅ Зріз 21:00 збережено!")
                    time.sleep(1)
                    st.rerun()

            if st.button("📸 Записати кінець КВ (CW_END)", width="stretch"):
                if not active_nicks:
                    st.error("Оберіть блок гравців у списку вище!")
                elif not st.session_state.live_stats:
                    st.warning("Спочатку отримайте дані ('Отримати дані')!")
                else:
                    for nick in active_nicks:
                        if nick in st.session_state.live_stats:
                            db.save_snapshot("CW_END", st.session_state.live_stats[nick])
                    st.success("✅ Зріз кінця КВ (CW_END) збережено!")
                    time.sleep(1)
                    st.rerun()

        with st.expander("➕ Створити / Редагувати блок"):
            new_group_name = st.text_input("Назва блоку (напр. Склад 1)")
            default_nicks = "\n".join(groups.get(new_group_name, [])) if new_group_name in groups else ""
            nicks_input = st.text_area("Нікнейми (по одному на рядок)", value=default_nicks, height=120)

            if st.button("Зберегти блок", width="stretch"):
                if new_group_name and nicks_input:
                    nicks_list = [n.strip() for n in nicks_input.split("\n") if n.strip()]
                    db.save_group(new_group_name, nicks_list)
                    st.success(f"Блок '{new_group_name}' збережено!")
                    st.rerun()

            st.divider()
            st.markdown("**🛡️ Отримати блок з Клану**")
            clan_nick_search = st.text_input("Нікнейм гравця клану")
            if st.button("📥 Завантажити склад клану", width="stretch"):
                if clan_nick_search:
                    api_client = StalzoneApiClient(API_CLIENT_ID, API_CLIENT_SECRET, region)
                    with st.spinner(f"Запит клану для {clan_nick_search}..."):
                        c_name, c_members = api_client.fetch_clan_members_by_player(clan_nick_search)
                        if c_name and c_members:
                            db.save_group(c_name, c_members)
                            st.success(f"✅ Клан '{c_name}' додано! Отримано {len(c_members)} гравців.")
                            time.sleep(1.2)
                            st.rerun()
                        else:
                            st.error("Не вдалося знайти клан для цього нікнейму.")

    return region, selected_group, active_nicks


def render_pre_cw_view(selected_group: str, active_nicks: list, snap_00: dict, snap_21: dict):
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
    st.dataframe(df_pre, width="stretch", height=450)


def render_post_cw_view(selected_group: str, active_nicks: list, snap_00: dict, snap_21: dict, snap_cw: dict, is_tracking_active: bool):
    if not active_nicks:
        st.info("👈 Оберіть або створіть блок гравців у панелі ліворуч.")
        return

    st.subheader(f"⚔️ Аналітичний блок: '{selected_group}'")

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
        st.warning("⏳ **Екстренний зріз у процесі!** Натисніть 🛑 Кінець в сайдбарі після закінчення.")
    else:
        st.info("⚔️ Відображається статистика КВ (21:00 -> кінець КВ або поточні дані).")

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
    st.dataframe(styled_df, width="stretch", height=450)


def render_player_cards_view(db: DatabaseManager, selected_group: str, active_nicks: list, snap_00: dict, snap_21: dict, snap_cw: dict):
    if not active_nicks:
        st.warning("⚠️ Оберіть конкретний блок гравців у лівому меню сайдбару!")
        return

    card_sub_mode = st.tabs(["🏆 Тирлист (Tier List)", "👤 Персональна картка гравця"])

    with card_sub_mode[0]:
        st.subheader(f"🏆 Тирлист гравців блоку '{selected_group}'")

        col_tp_m, col_opp = st.columns([2, 1])

        with col_tp_m:
            tp_mode = st.radio(
                "Режим обчислення рейтингу ТР:",
                options=["🔥 Загальний ТР (Total Power)", "⚔️ ТР за КВ / Зріз (Slice TP)"],
                horizontal=True
            )

        tp_multiplier = 1.0
        penalty_pct = 0.0

        if tp_mode == "⚔️ ТР за КВ / Зріз (Slice TP)":
            with col_opp:
                opp_rating = st.number_input(
                    "⚔️ Рейтинг противника (КВ):",
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
                head_pct = AnalyticsEngine.calc_ratio(HS * 100, total_shots) if total_shots > 0 else 0.0

                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("🔥 Total Power (TP)", current_tp)
                c2.metric("🎯 K/D (У/С)", kd)
                c3.metric("🤝 KAD (УП/С)", kad)
                c4.metric("🛡️ УН / УП", un_up)
                c5.metric("🎯 % Влучань у голову", f"{head_pct}%")

                st.divider()
                st.markdown("### 📈 Динаміка змін (Було ➔ Стало)")

                def build_delta_col(title: str, days_ago: int):
                    old_data = db.get_historical_snapshot(selected_player, days_ago)
                    if not old_data:
                        st.caption(f"**{title}**: немає даних")
                        return

                    old_tp = AnalyticsEngine.calc_tp(old_data)
                    old_K = old_data.get("kills", 0)
                    old_D = max(old_data.get("deaths", 0), 1)
                    old_A = old_data.get("assists", 0)
                    old_DD = old_data.get("damage_dealt", 0)
                    old_DR = max(old_data.get("damage_recv", 0), 1)

                    old_HS = old_data.get("headshots", 0)
                    old_total = old_HS + old_data.get("bodyshots", 0) + old_data.get("limbshots", 0)

                    old_kd = AnalyticsEngine.calc_ratio(old_K, old_D)
                    old_kad = AnalyticsEngine.calc_ratio(old_K + old_A, old_D)
                    old_un_up = AnalyticsEngine.calc_ratio(old_DD, old_DR)
                    old_head_pct = AnalyticsEngine.calc_ratio(old_HS * 100, old_total) if old_total > 0 else 0.0

                    tp_diff = round(current_tp - old_tp, 1)
                    kd_diff = round(kd - old_kd, 2)
                    kad_diff = round(kad - old_kad, 2)
                    un_up_diff = round(un_up - old_un_up, 2)
                    head_diff = round(head_pct - old_head_pct, 1)

                    st.markdown(f"#### {title}")
                    st.metric("Зміна TP", f"{current_tp}", delta=f"{tp_diff:+}")
                    st.metric("Зміна K/D", f"{kd}", delta=f"{kd_diff:+}")
                    st.metric("Зміна KAD", f"{kad}", delta=f"{kad_diff:+}")
                    st.metric("Зміна УН/УП", f"{un_up}", delta=f"{un_up_diff:+}")
                    st.metric("Зміна % Голови", f"{head_pct}%", delta=f"{head_diff:+}%")

                col_d1, col_d7, col_d30 = st.columns(3)
                with col_d1:
                    build_delta_col("За 1 День", 1)
                with col_d7:
                    build_delta_col("За 7 Днів (Тиждень)", 7)
                with col_d30:
                    build_delta_col("За 30 Днів (Місяць)", 30)


def render_clan_metrics_view(db: DatabaseManager, selected_group: str, active_nicks: list):
    st.subheader("🛡️ Клановий менеджмент & Конструктор Загонів")

    if not active_nicks:
        st.warning("⚠️ Спочатку оберіть блок/клан у сайдбарі для формування загону!")
        return

    clan_tabs = st.tabs(["⚔️ Конструктор загонів (Squad Builder)", "📊 Загальні метрики клану"])

    with clan_tabs[0]:
        st.markdown("### 🪖 Моделювання бойового загону")

        saved_squads = db.get_squads(selected_group)
        col_sq_1, col_sq_2 = st.columns([1, 2])

        with col_sq_1:
            if saved_squads:
                st.markdown("**📂 Збережені загони:**")
                load_squad_name = st.selectbox("Завантажити загін",
                                               options=["-- Новий загін --"] + list(saved_squads.keys()))
                if load_squad_name != "-- Новий загін --":
                    sq_info = saved_squads[load_squad_name]
                    init_squad_name = load_squad_name
                    init_slots = sq_info["max_slots"]
                    init_members = [m for m in sq_info["members"] if m in active_nicks]
                else:
                    init_squad_name = "Загін Alpha"
                    init_slots = 5
                    init_members = []
            else:
                init_squad_name = "Загін Alpha"
                init_slots = 5
                init_members = []

            squad_name = st.text_input("Назва загону / Пачки", value=init_squad_name)
            max_slots = st.number_input("Кількість місць у загоні", min_value=1, max_value=30, value=init_slots)

            selected_squad_members = st.multiselect(
                f"Оберіть бійців з блоку '{selected_group}':",
                options=sorted(active_nicks),
                default=init_members,
                max_selections=int(max_slots)
            )

            col_sq_b1, col_sq_b2 = st.columns(2)
            with col_sq_b1:
                if st.button("💾 Зберегти загін", width="stretch"):
                    if squad_name and selected_squad_members:
                        db.save_squad(squad_name, selected_group, max_slots, selected_squad_members)
                        st.success(f"Загін '{squad_name}' збережено!")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error("Вкажіть назву та виберіть гравців!")

            with col_sq_b2:
                if saved_squads and squad_name in saved_squads:
                    if st.button("🗑️ Видалити", type="secondary", width="stretch"):
                        db.delete_squad(squad_name)
                        st.warning(f"Загін '{squad_name}' видалено!")
                        time.sleep(1)
                        st.rerun()

        with col_sq_2:
            if not selected_squad_members:
                st.info("👈 Виберіть бійців ліворуч, щоб вирахувати параметри загону.")
            else:
                squad_tp_list = []
                squad_details = []

                for nick in selected_squad_members:
                    curr = st.session_state.live_stats.get(nick, db.get_historical_snapshot(nick, 0))
                    tp = AnalyticsEngine.calc_tp(curr) if curr else 0.0

                    tier = AnalyticsEngine.get_tier_letter(tp)
                    squad_tp_list.append(tp)

                    squad_details.append({
                        "Нікнейм": nick,
                        "Тир": tier,
                        "Total Power (ТР)": tp,
                        "K/D": AnalyticsEngine.calc_ratio(curr.get("kills", 0),
                                                          max(curr.get("deaths", 0), 1)) if curr else 0.0
                    })

                total_squad_tp = round(sum(squad_tp_list), 1)
                avg_squad_tp = round(total_squad_tp / len(squad_tp_list), 1) if squad_tp_list else 0.0
                avg_squad_tier = AnalyticsEngine.get_tier_letter(avg_squad_tp)

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("🔥 Загальна потужність", total_squad_tp)
                m2.metric("📊 Середній ТР бійця", avg_squad_tp)
                m3.metric("🎖️ Усереднений тир", avg_squad_tier)
                m4.metric("👥 Укомплектованість", f"{len(selected_squad_members)} / {max_slots}")

                st.markdown("#### 📜 Склад загону:")
                df_squad = pd.DataFrame(squad_details)
                st.dataframe(df_squad, width="stretch")

    with clan_tabs[1]:
        st.info("💡 У цьому розділі незабаром з'являться порівняльні таблиці між різними кланами та детальна аналітика відвідуваності КВ.")
        