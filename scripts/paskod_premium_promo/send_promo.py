"""Send the Paskod premium promo DM (run inside remnawave_bot container)."""

from __future__ import annotations

import asyncio
import json
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.handlers.paskod_premium_promo import PROMO_MESSAGE_HTML, build_keyboard, load_state


# @xei1y
DEFAULT_CHAT_ID = 7835556726


async def main(chat_id: int = DEFAULT_CHAT_ID) -> None:
    token = os.environ['BOT_TOKEN']
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    state = load_state()
    kb = build_keyboard(state['claimed'], state['max_claims'])
    msg = await bot.send_message(
        chat_id=chat_id,
        text=PROMO_MESSAGE_HTML,
        reply_markup=kb,
        parse_mode='HTML',
        disable_web_page_preview=True,
    )
    print(
        json.dumps(
            {
                'ok': True,
                'message_id': msg.message_id,
                'chat_id': msg.chat.id,
                'button': kb.inline_keyboard[0][0].text,
                'style': getattr(kb.inline_keyboard[0][0], 'style', None),
                'claimed': state['claimed'],
                'max': state['max_claims'],
            },
            ensure_ascii=False,
        )
    )
    await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
