import time
import streamlit as st
from database import DatabaseManager
from data_fetcher import StalzoneApiClient, start_scheduler, API_CLIENT_ID, API_CLIENT_SECRET
from ui import (
    check_authentication,
    apply_custom_styles,
    render_sidebar,
    render_pre_cw_view,
    render_post_cw_view,
    render_player_cards_view,
    render_clan_metrics_view
)


def main():
    st.set_page_config(page_title="SAR", layout="wide", initial_sidebar_state="expanded")

    if not check_authentication():
        return

    # Запуск фонового автозбору
    start_scheduler()

    # Застосування кастомних CSS-стилів
    apply_custom_styles()

    # Ініціалізація баз даних та сесій
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

    # Відображення сайдбару
    region, selected_group, active_nicks = render_sidebar(db, is_tracking_active)

    # Навігаційне меню
    st.markdown("<div class='nav-header'>🧭 Розділи системи аналітики</div>", unsafe_allow_html=True)

    main_menu_options = [
        "📋 До КВ (Основна статистика)",
        "⚔️ Після КВ \\ Зріз (Аналітика)",
        "🎴 Картка гравців",
        "🛡️ Кланові метрики"
    ]

    if hasattr(st, "segmented_control"):
        mode = st.segmented_control("Головне меню", options=main_menu_options, default=main_menu_options[0],
                                    label_visibility="collapsed")
    else:
        mode = st.radio("Головне меню", options=main_menu_options, horizontal=True)

    col_btn_space, col_btn = st.columns([3, 1], vertical_alignment="center")

    with col_btn:
        fetch_data_btn = st.button("📥 Отримати дані", type="primary", width="stretch")

    if fetch_data_btn:
        if not active_nicks:
            st.error("Помилка: Спочатку оберіть блок гравців у лівому меню!")
        else:
            api_client = StalzoneApiClient(API_CLIENT_ID, API_CLIENT_SECRET, region)
            progress_bar = st.progress(0)
            status_text = st.empty()

            total_nicks = len(active_nicks)
            for i, nick in enumerate(active_nicks):
                status_text.markdown(f"**Запит API для гравця:** `{nick}` ({i + 1}/{total_nicks}) ... ⏳")
                st.session_state.live_stats[nick] = api_client.fetch_player_stats(nick)
                progress_bar.progress((i + 1) / total_nicks)

            status_text.success("✅ Дані успішно отримані через eAPI!")
            time.sleep(1.2)
            status_text.empty()
            progress_bar.empty()
            st.rerun()

    snap_00 = db.get_snapshot("00:00")
    snap_21 = db.get_snapshot("21:00")
    snap_cw = db.get_snapshot("CW_END")

    st.divider()

    # Відображення обраного режиму
    if mode == "📋 До КВ (Основна статистика)":
        render_pre_cw_view(selected_group, active_nicks, snap_00, snap_21)

    elif mode == "⚔️ Після КВ \\ Зріз (Аналітика)":
        render_post_cw_view(selected_group, active_nicks, snap_00, snap_21, snap_cw, is_tracking_active)

    elif mode == "🎴 Картка гравців":
        render_player_cards_view(db, selected_group, active_nicks, snap_00, snap_21, snap_cw)

    elif mode == "🛡️ Кланові метрики":
        render_clan_metrics_view(db, selected_group, active_nicks)


if __name__ == "__main__":
    main()
