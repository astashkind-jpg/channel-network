"""Получение TG_SESSION для репоста видео из чужих каналов.

Запускать ОДИН РАЗ у себя на компьютере (не на GitHub):
    pip install telethon
    python scripts/make_session.py

1. На https://my.telegram.org → API development tools создайте приложение, возьмите api_id и api_hash.
2. Скрипт попросит телефон и код из Telegram.
3. Выведенную строку положите в секрет TG_SESSION. Никому её не показывайте: это полный доступ к аккаунту.

Совет: используйте отдельный аккаунт Telegram (вторая SIM), а не личный, и подпишите его
на каналы-источники видео.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("api_id: ").strip())
api_hash = input("api_hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nВаш TG_SESSION (скопируйте целиком):\n")
    print(client.session.save())
