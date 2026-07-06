import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import searchListRent as rent
from listing_excel import EXCEL_PATH, reset_listings_file
from telegram_config import get_bot_config

BOT_TOKEN, ALLOWED_USER_IDS = get_bot_config()

FOLDER, KEYWORDS, EXTRA_FILTERS, EXCLUDE_FILTERS, TARGET_GROUP = range(5)

SKIP = '/x'
STEP_FILTER = filters.Regex(rf'^{SKIP}$') | (filters.TEXT & ~filters.COMMAND)


def _is_allowed(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id in ALLOWED_USER_IDS


def _value_or_skip(text: str) -> str:
    return '' if text.strip() == SKIP else text


async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        await update.message.reply_text('Not authorized.')
        return ConversationHandler.END

    default_text = ', '.join(rent.DEFAULT_SOURCE_FOLDERS)
    await update.message.reply_text(
        f'Enter folder name(s), comma separated ({SKIP} for {default_text}):'
    )
    return FOLDER


async def rent_folder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    source_folders = rent.resolve_source_folders(_value_or_skip(update.message.text))
    context.chat_data['source_folders'] = source_folders

    if source_folders:
        await update.message.reply_text(f'Searching folders: {", ".join(source_folders)}')
    else:
        await update.message.reply_text(
            f'Searching fallback groups: {", ".join(rent.GROUP_NAMES)}'
        )

    default_text = ', '.join(rent.DEFAULT_KEYWORDS)
    await update.message.reply_text(
        f'Keywords, comma separated ({SKIP} for {default_text}):'
    )
    return KEYWORDS


async def rent_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keywords = rent.resolve_keywords(_value_or_skip(update.message.text))
    context.chat_data['keywords'] = keywords

    await update.message.reply_text(f'Filtering messages by keywords: {", ".join(keywords)}')
    await update.message.reply_text(f'Extra keywords a message must also contain ({SKIP} to skip):')
    return EXTRA_FILTERS


async def rent_extra_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    extra_filters = rent.parse_comma_separated_values(_value_or_skip(update.message.text))
    context.chat_data['extra_filters'] = extra_filters

    if extra_filters:
        await update.message.reply_text(
            f'Only forwarding messages that also contain: {", ".join(extra_filters)}'
        )

    await update.message.reply_text(f'Keywords to exclude messages containing ({SKIP} to skip):')
    return EXCLUDE_FILTERS


async def rent_exclude_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exclude_filters = rent.parse_comma_separated_values(_value_or_skip(update.message.text))
    context.chat_data['exclude_filters'] = exclude_filters

    if exclude_filters:
        await update.message.reply_text(f'Skipping messages that contain: {", ".join(exclude_filters)}')

    await update.message.reply_text(
        f'Target group to forward to ({SKIP} for "{rent.TARGET_GROUP}"):'
    )
    return TARGET_GROUP


async def rent_target_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text != SKIP and ',' in text:
        await update.message.reply_text('Please enter only one target group.')
        return TARGET_GROUP

    target_group_name = rent.resolve_target_group(_value_or_skip(text))
    await update.message.reply_text(
        f'Forwarding to "{target_group_name}". Starting search, this may take a while...'
    )

    chat_id = update.effective_chat.id

    def log(message):
        context.application.create_task(
            context.bot.send_message(chat_id=chat_id, text=str(message))
        )

    try:
        await rent.run(
            source_folders=context.chat_data['source_folders'],
            keywords=context.chat_data['keywords'],
            extra_filters=context.chat_data['extra_filters'],
            exclude_filters=context.chat_data['exclude_filters'],
            target_group_names=[target_group_name],
            log=log,
        )
    except Exception as error:
        await update.message.reply_text(f'Search failed: {error}')
    else:
        if EXCEL_PATH.exists():
            with open(EXCEL_PATH, 'rb') as excel_file:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=excel_file,
                    filename=EXCEL_PATH.name,
                )

    return ConversationHandler.END


async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('Cancelled.')
    return ConversationHandler.END


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[CommandHandler('rent', rent_start)],
        states={
            FOLDER: [MessageHandler(STEP_FILTER, rent_folder)],
            KEYWORDS: [MessageHandler(STEP_FILTER, rent_keywords)],
            EXTRA_FILTERS: [MessageHandler(STEP_FILTER, rent_extra_filters)],
            EXCLUDE_FILTERS: [MessageHandler(STEP_FILTER, rent_exclude_filters)],
            TARGET_GROUP: [MessageHandler(STEP_FILTER, rent_target_group)],
        },
        fallbacks=[CommandHandler('cancel', rent_cancel)],
    )

    application.add_handler(conversation)
    return application


async def main():
    reset_listings_file()

    async with rent.client:
        application = build_application()

        async with application:
            await application.start()
            await application.updater.start_polling()
            print('Bot is running. Send /rent in Telegram to start a search.')

            try:
                await asyncio.Event().wait()
            finally:
                await application.updater.stop()
                await application.stop()


if __name__ == '__main__':
    asyncio.run(main())
