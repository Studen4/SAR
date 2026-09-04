import os
import time
from playwright.sync_api import sync_playwright

USER_DATA_DIR = os.path.join(os.getcwd(), "browser_profile")
TARGET_URL = "https://stalzonehq.com/characters/EU/Sobakyl"


def run_inspector():
    print("🚀 Запуск оновленого інспектора елементів StalzoneHQ (Запис через ПКМ)...")

    if not os.path.exists(USER_DATA_DIR):
        print("❌ Профіль браузера не знайдено. Спочатку запустіть Streamlit і витягніть кукі.")
        return

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = context.pages[0] if context.pages else context.new_page()

        print(f"🌐 Перехід на: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="networkidle")
        time.sleep(3)

        # 1. Зберігаємо повний HTML
        html_content = page.content()
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html_content)

        # 2. Перехоплюємо ТІЛЬКИ ПКМ (contextmenu), ЛКМ залишається для взаємодії
        page.evaluate("""
                      window.clickedElements = [];
                      document.addEventListener('contextmenu', (e) => {
                          e.preventDefault(); // Блокуємо стандартне меню ПКМ

                          const el = e.target;

                          // Підсвічуємо обраний елемент
                          el.style.border = '2px solid red';
                          el.style.backgroundColor = 'rgba(255, 0, 0, 0.2)';

                          const info = {
                              text: el.innerText ? el.innerText.trim() : '',
                              tag: el.tagName,
                              class: el.className,
                              outerHTML: el.outerHTML,
                              parentText: el.parentElement ? el.parentElement.innerText.trim() : '',
                              parentHTML: el.parentElement ? el.parentElement.outerHTML : ''
                          };

                          window.clickedElements.push(info);
                          console.log('ПКМ записано:', info);
                      }, true);
                      """)

        print("\n" + "=" * 60)
        print("📋 ІНСТРУКЦІЯ З ЗБОРУ СТРУКТУРИ (ПКМ):")
        print("1. Використовуйте ЛІВУ кнопку миші (ЛКМ), щоб закрити кукі та модалки.")
        print("2. Натискайте ПРАВОЮ кнопкою миші (ПКМ) по 10 значеннях по черзі:")
        print("   1) Убито игроков (Kills)")
        print("   2) Помощь в убийстве (Assists)")
        print("   3) Смертей (Deaths)")
        print("   4) Времени в игре (Time played)")
        print("   5) Нанесено урона игрокам (Damage dealt)")
        print("   6) Получено урона от игроков (Damage received)")
        print("   7) Гранат брошено (Grenades thrown)")
        print("   8) Выстрелов в голову (Headshots)")
        print("   9) Выстрелов в тело (Bodyshots)")
        print("   10) Выстрелов в конечности (Limb shots)")
        print("3. Після 10 правих кліків поверніться в консоль і натисніть ENTER.")
        print("=" * 60 + "\n")

        input("👉 Натисніть ENTER після завершення кліків...")

        clicks = page.evaluate("window.clickedElements")

        with open("debug_clicks.txt", "w", encoding="utf-8") as f:
            f.write(f"Всього записано ПКМ-кліків: {len(clicks)}\n\n")
            for idx, item in enumerate(clicks, 1):
                f.write(f"=== КЛІК #{idx} ===\n")
                f.write(f"Текст елемента: '{item['text']}'\n")
                f.write(f"Тег: <{item['tag']}> | Класи: '{item['class']}'\n")
                f.write(f"Текст батьківського блоку: '{item['parentText']}'\n")
                f.write(f"HTML батьківського блоку:\n{item['parentHTML']}\n")
                f.write("-" * 50 + "\n\n")

        print(f"✅ Записано {len(clicks)} ПКМ-кліків у файл 'debug_clicks.txt'.")
        context.close()


if __name__ == "__main__":
    run_inspector()