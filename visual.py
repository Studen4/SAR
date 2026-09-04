import streamlit as st


def apply_custom_theme():
    """Застосовує додаткові кастомні CSS-стилі для покращення інтерфейсу."""
    custom_css = """
    <style>
    /* Кастомізація кнопок (уніфікований стиль та hover-ефекти) */
    div.stButton > button {
        border: 1px solid #4a4a4a;
        border-radius: 6px;
        transition: all 0.2s ease-in-out;
    }
    div.stButton > button:hover {
        border-color: #00e5ff;
        box-shadow: 0 0 8px rgba(0, 229, 255, 0.3);
    }

    /* Збільшення відступів у сайдбарі для кращого сприйняття */
    [data-testid="stSidebar"] {
        padding-top: 2rem;
    }

    /* Стилізація dataframe для кращого контрасту */
    [data-testid="stDataFrame"] {
        background-color: #2a2a2a;
        border-radius: 8px;
        padding: 10px;
    }
    </style>
    """
    st.markdown(custom_css, unsafe_allow_html=True)