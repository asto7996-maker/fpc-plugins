"""
Interactive helper to authorise additional Telethon worker sessions.
These session files are used by the Inviter module for inviting/DM.

Usage:
    python session_login.py

Each run creates one new .session file in the sessions/ directory.
You can run this multiple times to add multiple worker accounts.
"""

import asyncio
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


async def create_session() -> None:
    api_id_str = os.getenv("API_ID", "")
    api_hash = os.getenv("API_HASH", "")

    if not api_id_str or not api_hash:
        print("ERROR: API_ID and API_HASH must be set in .env or environment.")
        sys.exit(1)

    api_id = int(api_id_str)
    sessions_dir = Path(os.getenv("SESSIONS_DIR", "sessions"))
    sessions_dir.mkdir(parents=True, exist_ok=True)

    phone = input("Enter phone number (e.g. +79001234567): ").strip()
    if not phone:
        print("Phone number is required.")
        return

    # Derive session name from phone (strip +)
    session_name = "worker_" + phone.replace("+", "").replace(" ", "")
    session_path = str(sessions_dir / session_name)

    print(f"Session will be saved to: {session_path}.session")

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorised as {me.first_name} (@{me.username or 'N/A'})")
        await client.disconnect()
        return

    await client.send_code_request(phone)
    code = input("Enter the confirmation code: ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("Two-step verification password: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"\nSession created successfully!")
    print(f"Account: {me.first_name} (@{me.username or 'N/A'})")
    print(f"Saved to: {session_path}.session")

    await client.disconnect()


def main() -> None:
    print("=== Telegram Session Login Helper ===\n")
    asyncio.run(create_session())
    print("\nDone. Add more accounts by running this script again.")


if __name__ == "__main__":
    main()
