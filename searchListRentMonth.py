from difflib import get_close_matches
import calendar
from datetime import datetime, timezone
import hashlib
import os
import re
import unicodedata

from telethon import TelegramClient, functions, utils
from telegram_config import get_telegram_config, load_env_file

load_env_file()
api_id, api_hash, session = get_telegram_config()

client = TelegramClient(
    session,
    api_id,
    api_hash
)

DEFAULT_KEYWORDS = [
    'For Rent',
    'Rental'
]

UNAVAILABLE_KEYWORDS = [
    'rented',
    'taken',
    'booked',
    'closed',
    'unavailable',
    'reserved',
    'sold',
    'tenanted',
    'on hold',
    'listing not available'
]

TELEGRAM_SOURCE_FOLDERS = [
    'Lister Group',
    'Lister Indi'
]

# Used only when no source folders are entered or configured.
GROUP_NAMES = [
    'Listing Rental Adib'
]

# Your personal Telegram group
TARGET_GROUP = 'Property Leads'

# Prevent duplicate forwarding
processed_messages = set()

# Telegram accepts batches for message deletion.
DELETE_BATCH_SIZE = 300

# Number of months back from today to read source messages.
DEFAULT_MONTH_DURATION = 1

TELEGRAM_MESSAGE_LINK_RE = re.compile(
    r'https?://t\.me/(?P<path>(?:c/\d+|[A-Za-z0-9_]+)(?:/\d+)+)'
)


def parse_comma_separated_values(value):
    values = []
    seen = set()

    for item in value.split(','):
        item = item.strip().strip('"\'')

        if not item:
            continue

        key = item.casefold()

        if key in seen:
            continue

        values.append(item)
        seen.add(key)

    return values


def normalize_filter_text(value):
    normalized = unicodedata.normalize('NFKC', value or '')
    normalized = ''.join(
        character
        for character in normalized
        if unicodedata.category(character) != 'Cf'
    )

    return normalized.casefold()


def compact_filter_text(value):
    return ''.join(
        character
        for character in normalize_filter_text(value)
        if character.isalnum()
    )


def normalize_listing_content(value):
    return re.sub(r'\s+', ' ', normalize_filter_text(value)).strip()


def get_listing_content_key(message):
    normalized_text = normalize_listing_content(get_message_text(message))

    if not normalized_text:
        return None

    return hashlib.sha256(normalized_text.encode('utf-8')).hexdigest()


def get_message_text(message):
    return (
        getattr(message, 'raw_text', None)
        or getattr(message, 'text', None)
        or getattr(message, 'message', None)
        or ''
    )


def get_message_datetime(message):
    message_date = getattr(message, 'date', None)

    if message_date is None:
        return None

    if message_date.tzinfo is None:
        return message_date.replace(tzinfo=timezone.utc)

    return message_date.astimezone(timezone.utc)


def get_matching_value(message, values):
    text = normalize_filter_text(get_message_text(message))
    compact_text = compact_filter_text(text)

    for value in values:
        normalized_value = normalize_filter_text(value)

        if normalized_value and normalized_value in text:
            return value

        compact_value = compact_filter_text(value)

        if compact_value and compact_value in compact_text:
            return value

    return None


def message_contains_any(message, values):
    return get_matching_value(message, values) is not None


def get_skip_reason(message, exclude_filters=None):
    unavail_keyword = get_matching_value(message, UNAVAILABLE_KEYWORDS)

    if unavail_keyword:
        return 'sold/unavailable', unavail_keyword

    exclude_filter = get_matching_value(message, exclude_filters or [])

    if exclude_filter:
        return 'excluded', exclude_filter

    return None, None


DEFAULT_SOURCE_FOLDERS = TELEGRAM_SOURCE_FOLDERS[:]


def ask_for_source_folders():
    default_text = ', '.join(DEFAULT_SOURCE_FOLDERS)

    if default_text:
        prompt = (
            'Enter folder name '
            f'(press Enter for {default_text}): '
        )
    else:
        prompt = 'Enter folder name (press Enter for default groups): '

    try:
        value = input(prompt).strip()
    except EOFError:
        value = ''

    if value:
        source_folders = parse_comma_separated_values(value)
    else:
        source_folders = DEFAULT_SOURCE_FOLDERS[:]

    if source_folders:
        print(f'Searching Telegram folders: {", ".join(source_folders)}')
    else:
        print(f'Searching fallback groups: {", ".join(GROUP_NAMES)}')

    return source_folders


def ask_for_keywords():
    default_text = ', '.join(DEFAULT_KEYWORDS)

    try:
        value = input(
            f'Keywords separated by comma (press Enter for {default_text}): '
        ).strip()
    except EOFError:
        value = ''

    if not value:
        keywords = DEFAULT_KEYWORDS[:]
    else:
        keywords = parse_comma_separated_values(value)

    if not keywords:
        keywords = DEFAULT_KEYWORDS[:]

    print(f'Filtering messages by keywords: {", ".join(keywords)}')
    return keywords


def ask_for_extra_filters():
    try:
        value = input(
            'Extra filters separated by comma (press Enter to skip): '
        ).strip()
    except EOFError:
        value = ''

    filters = parse_comma_separated_values(value)

    if filters:
        print(f'Only forwarding messages that also contain: {", ".join(filters)}')

    return filters


def ask_for_exclude_filters():
    try:
        value = input(
            'Exclude messages containing, separated by comma (press Enter to skip): '
        ).strip()
    except EOFError:
        value = ''

    filters = parse_comma_separated_values(value)

    if filters:
        print(f'Skipping messages that contain: {", ".join(filters)}')

    return filters


def get_month_label(months):
    if months == 1:
        return '1 month'

    return f'{months} months'


def subtract_months(value, months):
    month_index = value.year * 12 + value.month - 1 - months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])

    return value.replace(year=year, month=month, day=day)


def ask_for_month_duration():
    default_text = get_month_label(DEFAULT_MONTH_DURATION)

    while True:
        try:
            value = input(
                'How many months back from today? '
                f'(press Enter for {default_text}): '
            ).strip()
        except EOFError:
            value = ''

        if not value:
            months = DEFAULT_MONTH_DURATION
            break

        try:
            months = int(value)
        except ValueError:
            print('Enter a whole number of months.')
            continue

        if months < 1:
            print('Enter at least 1 month.')
            continue

        break

    local_today = datetime.now().astimezone().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )
    local_cutoff = subtract_months(local_today, months)

    print(
        f'Collecting messages from the last {get_month_label(months)} '
        f'(since {local_cutoff:%Y-%m-%d %H:%M %Z}).'
    )

    return local_cutoff.astimezone(timezone.utc)


def has_extra_filter(message, extra_filters):
    if not extra_filters:
        return True

    return message_contains_any(message, extra_filters)


def has_exclude_filter(message, exclude_filters):
    if not exclude_filters:
        return False

    return message_contains_any(message, exclude_filters)


async def resolve_chat(chat_name):
    try:
        return await client.get_entity(chat_name)
    except ValueError:
        pass

    dialogs = []

    async for dialog in client.iter_dialogs():
        dialogs.append(dialog)

        if dialog.name and dialog.name.casefold() == chat_name.casefold():
            return dialog.entity

    dialog_names = [dialog.name for dialog in dialogs if dialog.name]
    close_matches = get_close_matches(chat_name, dialog_names, n=5, cutoff=0.4)

    message = (
        f'Cannot find Telegram chat "{chat_name}". '
        'Use the exact title, @username, t.me link, or numeric chat ID.'
    )

    if close_matches:
        matches = '\n'.join(f' - {name}' for name in close_matches)
        message = f'{message}\n\nSimilar dialogs found:\n{matches}'

    raise ValueError(message)


async def clear_chat(chat, chat_name):
    print(f'\nClearing "{chat_name}"...')

    message_ids = []
    deleted_count = 0

    async for message in client.iter_messages(chat, limit=None):
        message_ids.append(message.id)

        if len(message_ids) >= DELETE_BATCH_SIZE:
            await client.delete_messages(chat, message_ids, revoke=True)
            deleted_count += len(message_ids)
            message_ids.clear()

    if message_ids:
        await client.delete_messages(chat, message_ids, revoke=True)
        deleted_count += len(message_ids)

    print(f'Deleted {deleted_count} existing messages from "{chat_name}".')


def get_title(value):
    title = getattr(value, 'title', value)
    return getattr(title, 'text', title)


def get_peer_id(value):
    try:
        return utils.get_peer_id(value)
    except Exception:
        return getattr(value, 'id', None)


def get_public_usernames(chat):
    usernames = []
    username = getattr(chat, 'username', None)

    if username:
        usernames.append(username)

    for username_item in getattr(chat, 'usernames', None) or []:
        username = getattr(username_item, 'username', None)

        if username:
            usernames.append(username)

    return {
        username.casefold()
        for username in usernames
        if username
    }


def get_tme_c_chat_id(chat):
    chat_id = getattr(chat, 'id', None)

    if chat_id is not None:
        return int(chat_id)

    peer_id = get_peer_id(chat)

    if peer_id is None:
        return None

    peer_id_text = str(abs(int(peer_id)))

    if peer_id_text.startswith('100') and len(peer_id_text) > 3:
        return int(peer_id_text[3:])

    return int(peer_id_text)


def get_message_link_keys(chat, message):
    message_id = getattr(message, 'id', None)

    if message_id is None:
        return set()

    keys = set()

    for username in get_public_usernames(chat):
        keys.add(('username', username, message_id))

    tme_c_chat_id = get_tme_c_chat_id(chat)

    if tme_c_chat_id is not None:
        keys.add(('c', tme_c_chat_id, message_id))

    return keys


def get_link_keys_from_message(message):
    keys = set()

    for match in TELEGRAM_MESSAGE_LINK_RE.finditer(get_message_text(message)):
        parts = match.group('path').split('/')

        if parts[0] == 'c' and len(parts) >= 3:
            keys.add(('c', int(parts[1]), int(parts[-1])))
        elif len(parts) >= 2:
            keys.add(('username', parts[0].casefold(), int(parts[-1])))

    return keys


def get_forward_origin_key(message):
    fwd_from = getattr(message, 'fwd_from', None)

    if fwd_from is None:
        return None

    for peer_attribute, message_id_attribute in [
        ('from_id', 'channel_post'),
        ('saved_from_peer', 'saved_from_msg_id'),
    ]:
        peer = getattr(fwd_from, peer_attribute, None)
        message_id = getattr(fwd_from, message_id_attribute, None)
        peer_id = get_peer_id(peer)

        if peer_id is not None and message_id is not None:
            return peer_attribute, peer_id, message_id

    return None


def get_folder_peer_ids(folder, attribute):
    peer_ids = set()

    for peer in getattr(folder, attribute, None) or []:
        peer_id = get_peer_id(peer)

        if peer_id is not None:
            peer_ids.add(peer_id)

    return peer_ids


def is_group_dialog(dialog):
    return bool(getattr(dialog, 'is_group', False))


def is_bot_dialog(dialog):
    return bool(getattr(dialog.entity, 'bot', False))


def is_contact_dialog(dialog):
    return bool(
        getattr(dialog, 'is_user', False)
        and not is_bot_dialog(dialog)
        and getattr(dialog.entity, 'contact', False)
    )


def is_non_contact_dialog(dialog):
    return bool(
        getattr(dialog, 'is_user', False)
        and not is_bot_dialog(dialog)
        and not getattr(dialog.entity, 'contact', False)
    )


def is_broadcast_dialog(dialog):
    entity = dialog.entity
    return bool(
        getattr(dialog, 'is_channel', False)
        and not getattr(entity, 'megagroup', False)
        and not getattr(entity, 'gigagroup', False)
    )


def dialog_matches_folder(dialog, folder):
    dialog_peer_id = get_peer_id(dialog.entity)
    included_peer_ids = (
        get_folder_peer_ids(folder, 'pinned_peers')
        | get_folder_peer_ids(folder, 'include_peers')
    )
    excluded_peer_ids = get_folder_peer_ids(folder, 'exclude_peers')

    if dialog_peer_id in excluded_peer_ids:
        return False

    if getattr(folder, 'exclude_archived', False) and getattr(dialog, 'archived', False):
        return False

    if getattr(folder, 'exclude_read', False) and not getattr(dialog, 'unread_count', 0):
        return False

    if dialog_peer_id in included_peer_ids:
        return True

    return any([
        getattr(folder, 'groups', False) and is_group_dialog(dialog),
        getattr(folder, 'contacts', False) and is_contact_dialog(dialog),
        getattr(folder, 'non_contacts', False) and is_non_contact_dialog(dialog),
        getattr(folder, 'broadcasts', False) and is_broadcast_dialog(dialog),
        getattr(folder, 'bots', False) and is_bot_dialog(dialog),
    ])


async def resolve_source_folder(folder_name):
    result = await client(functions.messages.GetDialogFiltersRequest())
    folders = getattr(result, 'filters', result)
    titled_folders = [
        folder for folder in folders
        if getattr(folder, 'title', None) is not None
    ]

    for folder in titled_folders:
        if str(get_title(folder)).casefold() == folder_name.casefold():
            return folder

    folder_names = [str(get_title(folder)) for folder in titled_folders]
    close_matches = get_close_matches(folder_name, folder_names, n=5, cutoff=0.4)
    message = f'Cannot find Telegram folder "{folder_name}".'

    if close_matches:
        matches = '\n'.join(f' - {name}' for name in close_matches)
        message = f'{message}\n\nSimilar folders found:\n{matches}'

    raise ValueError(message)


async def get_folder_dialogs(folder_name):
    folder = await resolve_source_folder(folder_name)
    dialogs_by_id = {}

    def add_dialog(dialog):
        peer_id = get_peer_id(dialog.entity)

        if peer_id is not None:
            dialogs_by_id[peer_id] = dialog

    async for dialog in client.iter_dialogs():
        if dialog_matches_folder(dialog, folder):
            add_dialog(dialog)

    dialogs = list(dialogs_by_id.values())
    dialogs.sort(key=lambda dialog: str(get_title(dialog)).casefold())

    print(f'Found {len(dialogs)} chats in Telegram folder "{folder_name}".')
    return dialogs


def has_keyword(message, keywords):
    return message_contains_any(message, keywords)


def has_unavailable_keyword(message):
    return message_contains_any(message, UNAVAILABLE_KEYWORDS)


def get_replied_message_id(message):
    reply_to_msg_id = getattr(message, 'reply_to_msg_id', None)

    if reply_to_msg_id is not None:
        return reply_to_msg_id

    reply_to = getattr(message, 'reply_to', None)

    if reply_to is None:
        return None

    return getattr(reply_to, 'reply_to_msg_id', None)


def get_status_replies_by_listing(messages):
    status_replies = {}

    for item in messages:
        message = item['message']

        if not has_unavailable_keyword(message):
            continue

        replied_message_id = get_replied_message_id(message)

        if replied_message_id is None:
            continue

        chat_id = get_peer_id(item['chat'])
        status_replies[(chat_id, replied_message_id)] = message

    return status_replies


def get_unavailable_reason(unavailable_keyword, unavailable_reply):
    reason_parts = []

    if unavailable_reply is not None:
        reason_parts.append(f'has status reply {unavailable_reply.id}')

    if unavailable_keyword:
        reason_parts.append(f'contains unavailable keyword "{unavailable_keyword}"')

    if not reason_parts:
        return None

    return ' and '.join(reason_parts)


def get_unavailable_forward_origins(messages, status_replies_by_listing):
    unavailable_forward_origins = {}

    for item in messages:
        chat = item['chat']
        message = item['message']
        forward_origin_key = get_forward_origin_key(message)

        if forward_origin_key is None:
            continue

        chat_id = get_peer_id(chat)
        unavailable_keyword = get_matching_value(message, UNAVAILABLE_KEYWORDS)
        unavailable_reply = status_replies_by_listing.get((chat_id, message.id))
        reason = get_unavailable_reason(unavailable_keyword, unavailable_reply)

        if reason is None:
            continue

        unavailable_forward_origins[forward_origin_key] = {
            'item': item,
            'reason': reason,
        }

    return unavailable_forward_origins


def get_unavailable_message_links(messages, status_replies_by_listing):
    unavailable_message_links = {}

    for item in messages:
        chat = item['chat']
        message = item['message']
        chat_id = get_peer_id(chat)
        unavailable_keyword = get_matching_value(message, UNAVAILABLE_KEYWORDS)
        unavailable_reply = status_replies_by_listing.get((chat_id, message.id))
        reason = get_unavailable_reason(unavailable_keyword, unavailable_reply)

        if reason is None:
            continue

        for link_key in get_message_link_keys(chat, message):
            unavailable_message_links[link_key] = {
                'item': item,
                'reason': reason,
            }

    return unavailable_message_links


def get_unavailable_link_match(message, unavailable_message_links):
    for link_key in get_link_keys_from_message(message):
        match = unavailable_message_links.get(link_key)

        if match is not None:
            return match

    return None


def get_reply_count(message):
    replies = getattr(message, 'replies', None)

    if not replies or getattr(replies, 'replies', 0) <= 0:
        return 0

    return replies.replies


async def get_unavailable_reply(chat, message):
    if get_reply_count(message) <= 0:
        return None

    try:
        async for reply in client.iter_messages(chat, reply_to=message.id):
            if has_unavailable_keyword(reply):
                return reply
    except Exception as error:
        print(f'Could not read replies for listing {message.id}: {error}')

    return None


async def collect_chat_messages(chat, chat_name, cutoff_datetime):
    messages = []

    async for message in client.iter_messages(chat, limit=None):
        message_datetime = get_message_datetime(message)

        if message_datetime is not None and message_datetime < cutoff_datetime:
            break

        if not get_message_text(message):
            continue

        messages.append({
            'chat': chat,
            'chat_name': chat_name,
            'message': message,
        })

    print(f'Collected {len(messages)} messages from "{chat_name}".')
    return messages


async def collect_group_messages(group_name, cutoff_datetime):
    group = await resolve_chat(group_name)
    return await collect_chat_messages(group, group_name, cutoff_datetime)


async def collect_all_messages(target_group, source_folders, cutoff_datetime):
    all_messages = []
    target_group_id = get_peer_id(target_group)

    if source_folders:
        dialogs_by_id = {}

        for folder_name in source_folders:
            try:
                dialogs = await get_folder_dialogs(folder_name)
            except ValueError as error:
                print(error)
                continue

            for dialog in dialogs:
                peer_id = get_peer_id(dialog.entity)

                if peer_id is not None:
                    dialogs_by_id[peer_id] = dialog

        for dialog in dialogs_by_id.values():
            if get_peer_id(dialog.entity) == target_group_id:
                continue

            chat_name = str(get_title(dialog))
            print(f'\nCollecting messages from "{chat_name}"...')
            all_messages.extend(
                await collect_chat_messages(
                    dialog.entity,
                    chat_name,
                    cutoff_datetime
                )
            )

        all_messages.sort(key=lambda item: item['message'].date)
        return all_messages

    for group_name in GROUP_NAMES:

        print(f'\nCollecting messages from "{group_name}"...')

        try:
            all_messages.extend(
                await collect_group_messages(group_name, cutoff_datetime)
            )

        except ValueError as error:
            print(error)

    all_messages.sort(key=lambda item: item['message'].date)

    return all_messages


async def forward_property_messages(
    messages,
    target_group,
    keywords,
    extra_filters,
    exclude_filters
):
    forwarded_count = 0
    status_replies_by_listing = get_status_replies_by_listing(messages)
    unavailable_forward_origins = get_unavailable_forward_origins(
        messages,
        status_replies_by_listing
    )
    unavailable_message_links = get_unavailable_message_links(
        messages,
        status_replies_by_listing
    )
    forwarded_forward_origins = {}
    processed_listing_content_keys = {}

    if unavailable_forward_origins:
        print(
            f'Found {len(unavailable_forward_origins)} unavailable '
            'forwarded original messages.'
        )

    if unavailable_message_links:
        print(
            f'Found {len(unavailable_message_links)} unavailable '
            'Telegram message links.'
        )

    for item in messages:
        chat = item['chat']
        chat_name = item['chat_name']
        message = item['message']
        forward_origin_key = get_forward_origin_key(message)

        unique_id = f"{chat_name}_{message.id}"

        if unique_id in processed_messages:
            continue

        skip_reason, matched_value = get_skip_reason(message)

        if skip_reason:
            print(
                f'Skipping message {message.id} from "{chat_name}" '
                f'because it is {skip_reason} and contains "{matched_value}".'
            )
            processed_messages.add(unique_id)
            continue

        if not has_keyword(message, keywords):
            continue

        if not has_extra_filter(message, extra_filters):
            continue

        listing_content_key = get_listing_content_key(message)

        if listing_content_key is not None:
            previous_item = processed_listing_content_keys.get(listing_content_key)

            if previous_item is not None:
                previous_message = previous_item['message']
                previous_chat_name = previous_item['chat_name']

                print(
                    f'Skipping duplicate listing text in message {message.id} '
                    f'from "{chat_name}" because message {previous_message.id} '
                    f'from "{previous_chat_name}" already handled the same '
                    'listing content.'
                )
                processed_messages.add(unique_id)
                continue

        unavailable_link_match = get_unavailable_link_match(
            message,
            unavailable_message_links
        )

        if unavailable_link_match is not None:
            unavailable_item = unavailable_link_match['item']
            unavailable_message = unavailable_item['message']
            unavailable_chat_name = unavailable_item['chat_name']

            print(
                f'Skipping message {message.id} from "{chat_name}" because '
                f'it links to unavailable message {unavailable_message.id} '
                f'from "{unavailable_chat_name}" '
                f'({unavailable_link_match["reason"]}).'
            )
            if listing_content_key is not None:
                processed_listing_content_keys[listing_content_key] = item

            processed_messages.add(unique_id)
            continue

        if forward_origin_key is not None:
            unavailable_origin = unavailable_forward_origins.get(forward_origin_key)

            if unavailable_origin is not None:
                unavailable_item = unavailable_origin['item']
                unavailable_message = unavailable_item['message']
                unavailable_chat_name = unavailable_item['chat_name']

                print(
                    f'Skipping message {message.id} from "{chat_name}" '
                    f'because forwarded original message {unavailable_message.id} '
                    f'from "{unavailable_chat_name}" '
                    f'{unavailable_origin["reason"]}.'
                )
                if listing_content_key is not None:
                    processed_listing_content_keys[listing_content_key] = item

                processed_messages.add(unique_id)
                continue

            previous_item = forwarded_forward_origins.get(forward_origin_key)

            if previous_item is not None:
                previous_message = previous_item['message']
                previous_chat_name = previous_item['chat_name']

                print(
                    f'Skipping duplicate forwarded message {message.id} '
                    f'from "{chat_name}" because message {previous_message.id} '
                    f'from "{previous_chat_name}" used the same Telegram '
                    'forward origin.'
                )
                processed_messages.add(unique_id)
                continue

        skip_reason, matched_value = get_skip_reason(message, exclude_filters)

        if skip_reason:
            print(
                f'Skipping message {message.id} from "{chat_name}" '
                f'because it is {skip_reason} and contains "{matched_value}".'
            )
            if listing_content_key is not None:
                processed_listing_content_keys[listing_content_key] = item

            processed_messages.add(unique_id)
            continue

        reply_count = get_reply_count(message)
        chat_id = get_peer_id(chat)
        unavailable_reply = status_replies_by_listing.get((chat_id, message.id))

        if unavailable_reply is None:
            unavailable_reply = await get_unavailable_reply(chat, message)

        if reply_count > 0 or unavailable_reply:
            print("\n===================")
            print("PROPERTY SKIPPED")
            print("===================")
            print(f"Date: {message.date}")
            print(f"Chat: {chat_name}")
            print(f"Listing message: {message.id}")
            print(f"Replies: {reply_count}")

            if unavailable_reply:
                print(f"Status reply: {unavailable_reply.id}")
            else:
                print("Status reply: no status keyword matched")

            print("STATUS: NOT AVAILABLE")
            if listing_content_key is not None:
                processed_listing_content_keys[listing_content_key] = item

            processed_messages.add(unique_id)
            continue

        print("\n===================")
        print("PROPERTY FOUND")
        print("===================")
        print(f"Date: {message.date}")
        print(f"Chat: {chat_name}")
        print("\n")

        print(get_message_text(message))

        await client.forward_messages(
            target_group,
            message
        )

        print(f"✅ Sent to {TARGET_GROUP}")

        processed_messages.add(unique_id)
        if listing_content_key is not None:
            processed_listing_content_keys[listing_content_key] = item

        if forward_origin_key is not None:
            forwarded_forward_origins[forward_origin_key] = item

        forwarded_count += 1

    print(f'\nForwarded {forwarded_count} matching messages to "{TARGET_GROUP}".')


async def main():
    print(f'Running script: {os.path.abspath(__file__)}')
    print(f'Built-in unavailable keywords: {", ".join(UNAVAILABLE_KEYWORDS)}')

    source_folders = ask_for_source_folders()
    keywords = ask_for_keywords()
    extra_filters = ask_for_extra_filters()
    exclude_filters = ask_for_exclude_filters()
    cutoff_datetime = ask_for_month_duration()

    try:
        target_group = await resolve_chat(TARGET_GROUP)
    except ValueError as error:
        print(error)
        return

    try:
        await clear_chat(target_group, TARGET_GROUP)
    except Exception as error:
        print(f'Could not clear "{TARGET_GROUP}": {error}')
        return

    messages = await collect_all_messages(
        target_group,
        source_folders,
        cutoff_datetime
    )
    await forward_property_messages(
        messages,
        target_group,
        keywords,
        extra_filters,
        exclude_filters
    )


with client:
    client.loop.run_until_complete(main())
