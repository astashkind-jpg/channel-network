"""Получение TG_SESSION для репоста видео из чужих каналов.

Запускать ОДИН РАЗ у себя на компьютере (не на GitHub):
    pip install telethon
    python make_session.py

1. На https://my.telegram.org → API development tools создайте приложение, возьмите api_id и api_hash.
2. Скрипт попросит телефон, код из Telegram и облачный пароль (если включён).
3. Выведенную строку положите в секрет TG_SESSION. Никому её не показывайте: это полный доступ к аккаунту.
"""
from getpass import getpass

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

print("=== Получение TG_SESSION ===\n")
api_id = int(input("1) Вставьте api_id (число) и нажмите Enter: ").strip())
api_hash = input("2) Вставьте api_hash и нажмите Enter: ").strip()

client = TelegramClient(StringSession(), api_id, api_hash)
client.start(
    phone=lambda: input("3) Номер телефона аккаунта в формате +79991234567: ").strip(),
    code_callback=lambda: input("4) Код, который пришёл в Telegram (в чат «Telegram»): ").strip(),
    password=lambda: getpass("5) Облачный пароль (при вводе символы не видны): "),
)
me = client.get_me()
session = client.session.save()
client.disconnect()

print(f"\nГотово! Вход выполнен как: {me.first_name or ''} (@{me.username or 'без юзернейма'})")
print("\nВаш TG_SESSION — скопируйте ВСЮ строку ниже целиком:\n")
print(session)
print("\nЭто полный доступ к аккаунту: не пересылайте её никому и не сохраняйте в файлах.")
input("\nНажмите Enter, чтобы закрыть окно…")
