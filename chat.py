import streamlit as st
import sqlite3
import os
import datetime
import pytz
import requests
import logging
import json

KYIV_TZ = pytz.timezone("Europe/Kyiv")
UPLOAD_DIR = "uploads"


def init_chat_db(db_path="stalzone_stats.db"):
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR, exist_ok=True)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_type TEXT DEFAULT 'general',
            username TEXT NOT NULL,
            message TEXT,
            file_url TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voice_rooms (
            room_id TEXT PRIMARY KEY,
            room_name TEXT NOT NULL,
            creator TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voice_room_members (
            room_id TEXT,
            username TEXT,
            ptt_key TEXT DEFAULT 'Space',
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (room_id, username)
        )
    """)

    cursor.execute("""
        DELETE FROM chat_messages 
        WHERE timestamp < datetime('now', '-30 days')
    """)

    cursor.execute("""
        DELETE FROM voice_rooms 
        WHERE room_id NOT IN (SELECT DISTINCT room_id FROM voice_room_members)
    """)

    conn.commit()
    conn.close()


def upload_to_external_host(uploaded_file):
    file_name_orig = getattr(uploaded_file, 'name', 'file')
    file_size = getattr(uploaded_file, 'size', 0)
    print(f"\n🚀 [UPLOAD] Початок обробки файлу: '{file_name_orig}' (Розмір: {file_size} байт)")

    try:
        bytes_data = uploaded_file.getvalue()
        url = "https://catbox.moe/user/api.php"
        files = {'fileToUpload': (file_name_orig, bytes_data)}
        data = {'reqtype': 'fileupload'}

        response = requests.post(url, data=data, files=files, timeout=25)
        if response.status_code == 200 and response.text.startswith("http"):
            link = response.text.strip()
            print(f"✅ [UPLOAD] Успішно! Згенеровано посилання: {link}")
            return link
    except Exception as e:
        print(f"❌ [UPLOAD] Виняток під час запиту до хостингу: {e}")

    file_name = f"{int(datetime.datetime.now().timestamp())}_{file_name_orig}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    with open(file_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return f"/{file_path}"


def get_key_display_name(code: str) -> str:
    if not code:
        return "Пробіл"
    if code == "Space":
        return "Пробіл"
    if code.startswith("Key"):
        return f"Клавіша {code.replace('Key', '')}"
    if code == "Mouse0":
        return "ЛКМ (Миша)"
    if code == "Mouse1":
        return "СКМ (Миша)"
    if code == "Mouse2":
        return "ПКМ (Миша)"
    if code == "Mouse3":
        return "Бокова 1 (X1)"
    if code == "Mouse4":
        return "Бокова 2 (X2)"
    return code


def render_chat_tab(db, username: str, is_admin: bool):
    init_chat_db(db.db_path)

    for q_key, q_val in list(st.query_params.items()):
        if q_key == "clear_chat" and is_admin:
            c_type = q_val
            conn = sqlite3.connect(db.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages WHERE chat_type = ?", (c_type,))
            conn.commit()
            conn.close()
            del st.query_params[q_key]
            st.rerun()
        elif q_key == "delete_msg" and is_admin:
            m_id = q_val
            conn = sqlite3.connect(db.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages WHERE id = ?", (m_id,))
            conn.commit()
            conn.close()
            del st.query_params[q_key]
            st.rerun()

    st.markdown("""
        <style>
        [data-testid="stFileUploader"] {
            width: 100% !important;
            padding: 0px !important;
            margin: 0px !important;
        }
        [data-testid="stFileUploader"] section {
            padding: 0px !important;
            min-height: 42px !important;
            max-height: 42px !important;
            background-color: #262730 !important;
            border: 1px solid #4B4B4B !important;
            border-radius: 8px !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            overflow: hidden !important;
        }
        [data-testid="stFileUploader"] section * {
            display: none !important;
        }
        [data-testid="stFileUploader"] section::after {
            content: "📎";
            font-size: 1.3rem;
            color: #CCCCCC;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
            cursor: pointer;
        }
        [data-testid="stFormSubmitButton"] button {
            height: 42px !important;
            background-color: #262730 !important;
            border: 1px solid #4B4B4B !important;
            color: #FFFFFF !important;
            border-radius: 8px !important;
            font-size: 1.2rem !important;
            padding: 0px !important;
        }
        [data-testid="stFormSubmitButton"] button:hover {
            border-color: #00E5FF !important;
            color: #00E5FF !important;
        }
        </style>
    """, unsafe_allow_html=True)

    col_chat, col_voice = st.columns([1.3, 1], gap="medium")

    # --- ЛІВА КОЛОНКА: ЧАТ ---
    with col_chat:
        st.markdown("#### 💬 Загальний чат та Баги")

        col_mode, col_admin_btn = st.columns([3, 1])
        with col_mode:
            chat_mode = st.radio(
                "Тип чату",
                options=["🌐 Загальний чат", "🐛 Чат для багів"],
                horizontal=True,
                label_visibility="collapsed"
            )
        chat_type = "general" if "Загальний" in chat_mode else "bugs"

        with col_admin_btn:
            if is_admin:
                if st.button("🗑️ Очистити", key=f"clear_btn_{chat_type}", use_container_width=True, help="Очистити весь чат"):
                    st.query_params["clear_chat"] = chat_type
                    st.rerun()

        conn = sqlite3.connect(db.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if chat_type == "general":
            cursor.execute("SELECT * FROM chat_messages WHERE chat_type = 'general' ORDER BY timestamp DESC LIMIT 100")
        else:
            if is_admin:
                cursor.execute("SELECT * FROM chat_messages WHERE chat_type = 'bugs' ORDER BY timestamp DESC LIMIT 100")
            else:
                cursor.execute(
                    "SELECT * FROM chat_messages WHERE chat_type = 'bugs' AND (username = ? OR username = 'admin') ORDER BY timestamp DESC LIMIT 100",
                    (username,)
                )

        messages = cursor.fetchall()
        conn.close()

        chat_container = st.container(height=520)
        with chat_container:
            if not messages:
                st.info("Повідомлень поки немає. Напишіть першим!")
            for msg in messages:
                msg_id = msg["id"]
                sender = msg["username"]
                text = msg["message"]
                f_url = msg["file_url"]
                t_stamp = msg["timestamp"]

                is_me = (sender == username)
                bg_color = "#2D143A" if is_me else "#1E1E24"

                msg_html = f"""
                                    <div style="background-color: {bg_color}; border-radius: 8px; padding: 10px; margin-bottom: 8px; border: 1px solid #9D4EDD;">
                                        <small style="color: #E0AAFF; font-weight: bold;">{sender} <span style="font-weight: normal; color: #888; font-size: 0.8em;">({t_stamp})</span></small>
                                        <div style="color: #FFF; margin-top: 4px; word-break: break-word;">{text}</div>
                                        {f'<div style="margin-top: 6px;"><a href="{f_url}" target="_blank" style="color: #00E5FF;">📎 Переглянути файл / зображення</a></div>' if f_url else ''}
                                    </div>
                                """

                if is_admin:
                    c_msg, c_del = st.columns([0.92, 0.08])
                    with c_msg:
                        st.markdown(msg_html, unsafe_allow_html=True)
                    with c_del:
                        if st.button("🗑️", key=f"del_{msg_id}", help="Видалити повідомлення"):
                            st.query_params["delete_msg"] = str(msg_id)
                            st.rerun()
                else:
                    st.markdown(msg_html, unsafe_allow_html=True)

        with st.form(key="chat_input_form", clear_on_submit=True):
            col_inp, col_clip, col_send = st.columns([0.80, 0.10, 0.10], vertical_alignment="bottom")

            with col_inp:
                user_msg = st.text_input("Повідомлення...", placeholder="Введіть повідомлення...", label_visibility="collapsed")
            with col_clip:
                uploaded_file = st.file_uploader("📎", type=["png", "jpg", "jpeg", "txt", "pdf", "zip"], label_visibility="collapsed")
            with col_send:
                submitted = st.form_submit_button("➤", use_container_width=True)

            if submitted and (user_msg.strip() or uploaded_file):
                file_url = ""
                if uploaded_file:
                    file_url = upload_to_external_host(uploaded_file)

                conn = sqlite3.connect(db.db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO chat_messages (chat_type, username, message, file_url)
                    VALUES (?, ?, ?, ?)
                """, (chat_type, username, user_msg.strip(), file_url))
                conn.commit()
                conn.close()
                st.rerun()

    # --- ПРАВА КОЛОНКА: ГОЛОСОВІ КІМНАТИ (РАЦІЯ) ---
    with col_voice:
        col_v_title, col_v_btn = st.columns([1, 1], vertical_alignment="center")
        with col_v_title:
            st.markdown("#### 🎙️ Голосовий Чат:")
        with col_v_btn:
            with st.form(key="create_room_form", border=False):
                new_room_name = st.text_input("Назва", placeholder="Назва кімнати...", label_visibility="collapsed")
                create_btn = st.form_submit_button("➕ Створити кімнату", use_container_width=True)
                if create_btn and new_room_name.strip():
                    room_id = f"room_{int(datetime.datetime.now().timestamp())}"
                    conn = sqlite3.connect(db.db_path)
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM voice_room_members WHERE username = ?", (username,))
                    cursor.execute(
                        "INSERT OR IGNORE INTO voice_rooms (room_id, room_name, creator) VALUES (?, ?, ?)",
                        (room_id, new_room_name.strip(), username)
                    )
                    cursor.execute(
                        "INSERT OR REPLACE INTO voice_room_members (room_id, username) VALUES (?, ?)",
                        (room_id, username)
                    )
                    conn.commit()
                    conn.close()
                    st.success(f"Створено та підключено до '{new_room_name.strip()}'!")
                    st.rerun()

        st.divider()

        conn = sqlite3.connect(db.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM voice_rooms")
        rooms = cursor.fetchall()

        cursor.execute("SELECT room_id, ptt_key FROM voice_room_members WHERE username = ?", (username,))
        my_membership = cursor.fetchone()
        my_room_id = my_membership["room_id"] if my_membership else None
        active_user_ptt = my_membership["ptt_key"] if my_membership and my_membership["ptt_key"] else "Space"

        rooms_data = []
        member_names_in_my_room = []
        for r in rooms:
            r_id = r["room_id"]
            r_name = r["room_name"]
            cursor.execute("SELECT username, ptt_key FROM voice_room_members WHERE room_id = ?", (r_id,))
            members = cursor.fetchall()
            rooms_data.append({
                "room_id": r_id,
                "room_name": r_name,
                "members": members
            })
            if r_id == my_room_id:
                member_names_in_my_room = [m["username"] for m in members]

        conn.close()

        empty_room_ids = [r["room_id"] for r in rooms_data if not r["members"]]
        if empty_room_ids:
            conn = sqlite3.connect(db.db_path)
            cursor = conn.cursor()
            for eid in empty_room_ids:
                cursor.execute("DELETE FROM voice_rooms WHERE room_id = ?", (eid,))
            conn.commit()
            conn.close()
            rooms_data = [r for r in rooms_data if r["members"]]

        if not rooms_data:
            st.info("Активних голосових кімнат немає.")

        for r in rooms_data:
            r_id = r["room_id"]
            r_name = r["room_name"]
            members = r["members"]
            member_names = [m["username"] for m in members]

            is_in_this_room = (my_room_id == r_id)
            border_color = "#00C853" if is_in_this_room else "#9D4EDD"

            st.markdown(f"""
                <div style="background-color: #18191e; border: 2px solid {border_color}; border-radius: 10px; padding: 14px; margin-bottom: 12px;">
                    <h5 style="margin: 0 0 6px 0; color: #E0AAFF;">🔊 {r_name}</h5>
                    <div style="font-size: 0.95rem; color: #FFF; margin-bottom: 8px;">
                        {', '.join(member_names) if member_names else 'Нікого'}
                    </div>
                </div>
            """, unsafe_allow_html=True)

            if is_in_this_room:
                current_key = active_user_ptt
                display_key_name = get_key_display_name(current_key)

                col_k1, col_k2 = st.columns([1.3, 0.9], vertical_alignment="center")
                with col_k1:
                    key_options = {
                        "Space": "Пробіл",
                        "KeyO": "Клавіша O",
                        "KeyE": "Клавіша E",
                        "KeyR": "Клавіша R",
                        "KeyF": "Клавіша F",
                        "KeyQ": "Клавіша Q",
                        "KeyC": "Клавіша C",
                        "KeyX": "Клавіша X",
                        "KeyZ": "Клавіша Z",
                        "KeyA": "Клавіша A",
                        "KeyS": "Клавіша S",
                        "KeyD": "Клавіша D",
                        "KeyV": "Клавіша V",
                        "ShiftLeft": "Лівий Shift",
                        "ControlLeft": "Лівий Ctrl",
                        "AltLeft": "Лівий Alt",
                        "Mouse0": "ЛКМ (Миша)",
                        "Mouse2": "ПКМ (Миша)",
                        "Mouse1": "СКМ (Миша)",
                        "Mouse3": "Бокова 1 (X1)",
                        "Mouse4": "Бокова 2 (X2)"
                    }
                    current_idx = list(key_options.keys()).index(current_key) if current_key in key_options else 0
                    selected_label = st.selectbox(
                        f"Вибрано: {display_key_name}",
                        options=list(key_options.values()),
                        index=current_idx,
                        key=f"sel_key_{r_id}"
                    )
                    selected_code = [k for k, v in key_options.items() if v == selected_label][0]
                    if selected_code != current_key:
                        conn = sqlite3.connect(db.db_path)
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE voice_room_members SET ptt_key = ? WHERE room_id = ? AND username = ?",
                            (selected_code, r_id, username)
                        )
                        conn.commit()
                        conn.close()
                        st.rerun()

                with col_k2:
                    st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)
                    if st.button("Покинути кімнату", key=f"leave_{r_id}", use_container_width=True):
                        conn = sqlite3.connect(db.db_path)
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM voice_room_members WHERE room_id = ? AND username = ?",
                                       (r_id, username))
                        cursor.execute(
                            "DELETE FROM voice_rooms WHERE room_id = ? AND room_id NOT IN (SELECT DISTINCT room_id FROM voice_room_members)",
                            (r_id,)
                        )
                        conn.commit()
                        conn.close()
                        st.warning("Ви покинули кімнату.")
                        st.rerun()
            else:
                if st.button("Приєднатись", key=f"join_{r_id}", use_container_width=True):
                    conn = sqlite3.connect(db.db_path)
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM voice_room_members WHERE username = ?", (username,))
                    cursor.execute(
                        "INSERT OR REPLACE INTO voice_room_members (room_id, username) VALUES (?, ?)",
                        (r_id, username)
                    )
                    conn.commit()
                    conn.close()
                    st.success(f"Підключено до кімнати '{r_name}'!")
                    st.rerun()

        if st.button("🔄 Оновити кімнати", use_container_width=True):
            st.rerun()

        if my_room_id:
            display_active_name = get_key_display_name(active_user_ptt)
            st.markdown("---")
            st.markdown(
                f"**🎙️ Статус рації:** Активна. Затисніть **[{display_active_name}]** або кнопку нижче для розмови!")

            members_json = json.dumps(member_names_in_my_room)

            ptt_html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <script src="https://unpkg.com/peerjs@1.5.2/dist/peerjs.min.js"></script>
            </head>
            <body style="background: transparent; margin: 0; padding: 0;">
                <div style="background: #18191e; padding: 12px; border-radius: 8px; text-align: center; border: 1px solid #00E5FF; font-family: sans-serif;">
                    <p id="ptt-status" style="color: #00E5FF; font-weight: bold; margin: 0 0 8px 0; font-size: 13px;">🔴 Підключення до WebRTC серветру...</p>
                    <button id="ptt-btn" style="background: #9D4EDD; color: white; border: none; padding: 10px 20px; font-weight: bold; border-radius: 6px; cursor: pointer; width: 100%; font-size: 14px;">🎙️ НАТИСНІТЬ І ГОВОРІТЬ (PTT)</button>
                </div>
                <div id="audio-container"></div>
                <script>
                    const myUsername = "{username}";
                    const roomId = "{my_room_id}";
                    const members = {members_json};
                    const targetKey = "{active_user_ptt}";
                    const displayKey = "{display_active_name}";

                    const statusEl = document.getElementById('ptt-status');
                    const btnEl = document.getElementById('ptt-btn');
                    const audioContainer = document.getElementById('audio-container');

                    let localStream = null;
                    let isTalking = false;
                    let peer = null;
                    const activeCalls = {{}};

                    navigator.mediaDevices.getUserMedia({{ audio: true, video: false }}).then(stream => {{
                        localStream = stream;
                        localStream.getAudioTracks()[0].enabled = false;
                        statusEl.innerText = "🟢 Рація готова. Затисніть [" + displayKey + "]!";
                        initPeer();
                    }}).catch(err => {{
                        statusEl.innerText = "❌ Помилка мікрофона: " + err.message;
                        statusEl.style.color = "#FF4B4B";
                    }});

                    function initPeer() {{
                        const peerId = "stalzone_" + roomId + "_" + myUsername;
                        peer = new Peer(peerId, {{
                            config: {{
                                'iceServers': [
                                    {{ urls: 'stun:stun.l.google.com:19302' }},
                                    {{ urls: 'stun:stun1.l.google.com:19302' }}
                                ]
                            }}
                        }});

                        peer.on('open', (id) => {{
                            console.log("Peer connected with ID:", id);
                            connectToOthers();
                        }});

                        peer.on('call', (call) => {{
                            call.answer(localStream);
                            handleCall(call);
                        }});

                        peer.on('error', (err) => {{
                            console.error("PeerJS error:", err);
                        }});
                    }}

                    function connectToOthers() {{
                        members.forEach(member => {{
                            if (member !== myUsername) {{
                                const otherPeerId = "stalzone_" + roomId + "_" + member;
                                if (!activeCalls[otherPeerId]) {{
                                    const call = peer.call(otherPeerId, localStream);
                                    if (call) {{
                                        handleCall(call);
                                    }}
                                }}
                            }}
                        }});
                    }}

                    function handleCall(call) {{
                        activeCalls[call.peer] = call;
                        call.on('stream', (remoteStream) => {{
                            playAudioStream(remoteStream, call.peer);
                        }});
                        call.on('close', () => {{
                            removeAudioStream(call.peer);
                            delete activeCalls[call.peer];
                        }});
                    }}

                    function playAudioStream(stream, peerId) {{
                        let audioId = "audio_" + peerId.replace(/[^a-zA-Z0-9]/g, "_");
                        let audioEl = document.getElementById(audioId);
                        if (!audioEl) {{
                            audioEl = document.createElement('audio');
                            audioEl.id = audioId;
                            audioEl.autoplay = true;
                            audioEl.controls = false;
                            audioContainer.appendChild(audioEl);
                        }}
                        audioEl.srcObject = stream;
                        audioEl.play().catch(e => console.log("Audio play error:", e));
                    }}

                    function removeAudioStream(peerId) {{
                        let audioId = "audio_" + peerId.replace(/[^a-zA-Z0-9]/g, "_");
                        let audioEl = document.getElementById(audioId);
                        if (audioEl) {{
                            audioEl.remove();
                        }}
                    }}

                    function startTalking() {{
                        if (localStream && !isTalking) {{
                            isTalking = true;
                            localStream.getAudioTracks()[0].enabled = true;
                            statusEl.innerText = "🔴 ЕФІР АКТИВНИЙ (Ви говорите)...";
                            statusEl.style.color = "#00C853";
                            btnEl.style.background = "#00C853";
                        }}
                    }}

                    function stopTalking() {{
                        if (localStream && isTalking) {{
                            isTalking = false;
                            localStream.getAudioTracks()[0].enabled = false;
                            statusEl.innerText = "🟢 Рація готова. Затисніть [" + displayKey + "]!";
                            statusEl.style.color = "#00E5FF";
                            btnEl.style.background = "#9D4EDD";
                        }}
                    }}

                    btnEl.addEventListener('mousedown', startTalking);
                    btnEl.addEventListener('mouseup', stopTalking);
                    btnEl.addEventListener('touchstart', startTalking);
                    btnEl.addEventListener('touchend', stopTalking);

                    const listenWindow = window.parent || window;

                    if (targetKey.startsWith("Mouse")) {{
                        const targetButton = parseInt(targetKey.replace("Mouse", ""));
                        listenWindow.addEventListener('mousedown', (e) => {{
                            if (e.button === targetButton && !isTalking) {{
                                if (targetButton === 2) e.preventDefault();
                                startTalking();
                            }}
                        }});
                        listenWindow.addEventListener('mouseup', (e) => {{
                            if (e.button === targetButton && isTalking) {{
                                stopTalking();
                            }}
                        }});
                        if (targetButton === 2) {{
                            listenWindow.addEventListener('contextmenu', (e) => {{
                                if (isTalking) e.preventDefault();
                            }});
                        }}
                    }} else {{
                        listenWindow.addEventListener('keydown', (e) => {{
                            if (e.code === targetKey && !isTalking && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {{
                                e.preventDefault();
                                startTalking();
                            }}
                        }});
                        listenWindow.addEventListener('keyup', (e) => {{
                            if (e.code === targetKey && isTalking) {{
                                e.preventDefault();
                                stopTalking();
                            }}
                        }});
                    }}
                </script>
            </body>
            </html>
            """
            st.iframe(ptt_html_content, height=115)
