import os
import sys
import streamlit.web.cli as stcli


def resolve_path(relative_path: str) -> str:
    """Отримує абсолютний шлях до ресурсів (працює як для звичайного запуску, так і для PyInstaller)"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


if __name__ == "__main__":
    # Перевірка та завантаження брайзера Camoufox при першому запуску
    try:
        from camoufox.pkg import fetch

        fetch()
    except Exception as e:
        print(f"Camoufox fetch warning: {e}")

    # Вказуємо шлях до основного файлу Streamlit
    app_path = resolve_path("app.py")

    # Конфігурація аргументів запуску Streamlit
    sys.argv = [
        "streamlit",
        "run",
        app_path,
        "--global.developmentMode=false",
        "--server.headless=false",
        "--browser.gatherUsageStats=false"
    ]

    sys.exit(stcli.main())
