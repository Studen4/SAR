import os
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import streamlit as st

try:
    from api_config import GOOGLE_DRIVE_FILE_ID, GCP_SERVICE_ACCOUNT
except ImportError:
    GOOGLE_DRIVE_FILE_ID = st.secrets.get("GOOGLE_DRIVE_FILE_ID", "1MD_8pjlZMIUPExLwFZoFa68URvGftxcg")
    GCP_SERVICE_ACCOUNT = None

logger = logging.getLogger("StalzoneDriveUploader")

SCOPES = [
    'https://www.googleapis.com/auth/drive'
]


def get_drive_service():
    """Створює та повертає Google Drive Service, віддаючи перевагу st.secrets, а потім api_config."""
    creds = None

    if "gcp_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gcp_service_account"])
            if "private_key" in info:
                info["private_key"] = info["private_key"].replace("\\n", "\n")
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            logger.info("🔑 Успішна авторизація Drive через st.secrets['gcp_service_account']")
        except Exception as e:
            logger.error(f"❌ Помилка авторизації з st.secrets['gcp_service_account']: {e}")

    elif "gdrive_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gdrive_service_account"])
            if "private_key" in info:
                info["private_key"] = info["private_key"].replace("\\n", "\n")
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            logger.info("🔑 Успішна авторизація Drive через st.secrets['gdrive_service_account']")
        except Exception as e:
            logger.error(f"❌ Помилка авторизації з st.secrets['gdrive_service_account']: {e}")

    if not creds and GCP_SERVICE_ACCOUNT:
        try:
            info = dict(GCP_SERVICE_ACCOUNT)
            if "private_key" in info:
                info["private_key"] = info["private_key"].replace("\\n", "\n")
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            logger.info("🔑 Успішна авторизація Drive через api_config.GCP_SERVICE_ACCOUNT")
        except Exception as e:
            logger.error(f"❌ Помилка авторизації з api_config.GCP_SERVICE_ACCOUNT: {e}")

    if not creds:
        logger.error("❌ Не знайдено даних авторизації ні в st.secrets, ні в api_config.py!")
        return None

    return build('drive', 'v3', credentials=creds)


def upload_db_to_drive(db_path: str = "stalzone_stats.db", file_id: str = None) -> bool:
    """Оновлює існуючий файл бази даних на Google Drive через service.files().update()."""
    target_file_id = file_id or GOOGLE_DRIVE_FILE_ID

    if not os.path.exists(db_path):
        logger.error(f"❌ Файл бази даних '{db_path}' не знайдено.")
        return False

    service = get_drive_service()
    if not service:
        return False

    try:
        media = MediaFileUpload(
            db_path,
            mimetype='application/x-sqlite3',
            resumable=True
        )

        updated_file = service.files().update(
            fileId=target_file_id,
            media_body=media,
            fields='id, name'
        ).execute()

        logger.info(f"✅ Базу даних успішно оновлено на Google Drive! File ID: {updated_file.get('id')}")
        return True

    except Exception as e:
        logger.error(f"❌ Помилка оновлення бакапу на Google Drive: {e}")
        return False


def download_latest_db_from_drive(db_path: str = "stalzone_stats.db", file_id: str = None) -> bool:
    """Викачує файл бази даних з Google Drive за його ID та замінює ним локальну БД."""
    target_file_id = file_id or GOOGLE_DRIVE_FILE_ID

    service = get_drive_service()
    if not service:
        return False

    try:
        logger.info(f"📥 Завантаження актуальної БД з Drive (ID: {target_file_id})...")

        request = service.files().get_media(fileId=target_file_id)

        with open(db_path, 'wb') as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()

        logger.info(f"✅ БД успішно завантажено та оновлено локально з Google Drive!")
        return True

    except Exception as e:
        logger.error(f"❌ Помилка викачування БД з Google Drive: {e}")
        return False
