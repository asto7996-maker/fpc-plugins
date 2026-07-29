import structlog
from aiogram import Dispatcher, F, types

from app.database.models import User
from app.keyboards.inline import get_support_keyboard
from app.services.support_settings_service import SupportSettingsService
from app.utils.photo_message import edit_or_answer_photo
from app.utils.premium_emoji import inject_premium_emojis
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def show_support_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    support_info = SupportSettingsService.get_support_info_text(db_user.language)
    await edit_or_answer_photo(
        callback=callback,
        caption=inject_premium_emojis(support_info),
        keyboard=get_support_keyboard(db_user.language),
        photo_file_id=None,
        parse_mode='HTML',
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_support_info, F.data == 'menu_support')
