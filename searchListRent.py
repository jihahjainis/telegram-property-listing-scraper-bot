from difflib import get_close_matches
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

from telethon import TelegramClient, events, functions, utils
from extract_listing import extract_listing_fields
from listing_api import submit_listing
from listing_excel import append_listing_row, EXCEL_PATH
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
    'Rental',
    'WTR',
    'Untuk Disewa',
    'Sewa Bulanan'
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
    'listing not available',
    'rent out'
]

TELEGRAM_SOURCE_FOLDERS = [
    'One Group'
]

# Used only when no source folders are entered or configured.
GROUP_NAMES = [
    'Listing Rental Adib'
]

# Your personal Telegram group
TARGET_GROUP = 'Property Leads'

# Used by /search: a lighter one-shot scan that just forwards matches,
# without AI extraction, Excel logging, or DB sync.
QUICK_SEARCH_KEYWORDS = ['for rent', 'rental']
QUICK_SEARCH_TARGET_GROUP = 'Jihah Listings'

# Prevent duplicate forwarding / re-extraction. Persisted to disk so restarting
# the bot doesn't forget what it already processed and re-run (and re-charge
# Claude for) listings it already handled in a prior run.
STATE_PATH = Path(__file__).with_name('processed_state.json')

processed_messages = set()
processed_content_keys = set()
processed_forward_origins = set()

# Separate dedup track for /search, so a message already handled by the main
# pipeline still gets independently forwarded here too (different keyword set,
# different target group, no AI/DB involvement).
quick_search_processed_messages = set()


def _serialize_forward_origin_key(key):
    return '|'.join(str(part) for part in key)


def load_processed_state(path=STATE_PATH):
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(f'Could not load {path.name}: {error}')
        return

    processed_messages.update(data.get('processed_messages', []))
    processed_content_keys.update(data.get('content_keys', []))
    processed_forward_origins.update(data.get('forward_origins', []))
    quick_search_processed_messages.update(data.get('quick_search_processed_messages', []))


def save_processed_state(path=STATE_PATH):
    data = {
        'processed_messages': sorted(processed_messages),
        'content_keys': sorted(processed_content_keys),
        'forward_origins': sorted(processed_forward_origins),
        'quick_search_processed_messages': sorted(quick_search_processed_messages),
    }

    try:
        path.write_text(json.dumps(data))
    except OSError as error:
        print(f'Could not save {path.name}: {error}')


def mark_processed(unique_id):
    processed_messages.add(unique_id)
    save_processed_state()


def mark_quick_search_processed(unique_id):
    quick_search_processed_messages.add(unique_id)
    save_processed_state()


load_processed_state()

# Telegram accepts batches for message deletion.
DELETE_BATCH_SIZE = 200

# Number of latest messages to read from each source chat.
MESSAGE_LIMIT = 200

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


def resolve_source_folders(value):
    value = (value or '').strip()

    if value:
        return parse_comma_separated_values(value)

    return DEFAULT_SOURCE_FOLDERS[:]


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

    source_folders = resolve_source_folders(value)

    if source_folders:
        print(f'Searching Telegram folders: {", ".join(source_folders)}')
    else:
        print(f'Searching fallback groups: {", ".join(GROUP_NAMES)}')

    return source_folders


def resolve_keywords(value):
    value = (value or '').strip()

    if not value:
        keywords = DEFAULT_KEYWORDS[:]
    else:
        keywords = parse_comma_separated_values(value)

    if not keywords:
        keywords = DEFAULT_KEYWORDS[:]

    return keywords


def ask_for_keywords():
    default_text = ', '.join(DEFAULT_KEYWORDS)

    try:
        value = input(
            f'Keywords separated by comma (press Enter for {default_text}): '
        ).strip()
    except EOFError:
        value = ''

    keywords = resolve_keywords(value)

    print(f'Filtering messages by keywords: {", ".join(keywords)}')
    return keywords


def ask_for_extra_filters():
    try:
        value = input(
            'Extra keywords (press Enter to skip): '
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
            'Exclude messages containing this keywords (press Enter to skip): '
        ).strip()
    except EOFError:
        value = ''

    filters = parse_comma_separated_values(value)

    if filters:
        print(f'Skipping messages that contain: {", ".join(filters)}')

    return filters


def resolve_target_group(value):
    value = (value or '').strip().strip('"\'')
    return value or TARGET_GROUP


def ask_for_target_groups():
    default_text = TARGET_GROUP

    while True:
        try:
            value = input(
                f'Target group (press Enter for {default_text}): '
            ).strip().strip('"\'')
        except EOFError:
            value = ''

        if ',' in value:
            print('Please enter only one target group.')
            continue

        target_group = resolve_target_group(value)
        print(f'Forwarding messages to: {target_group}')
        return [target_group]


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


def build_message_link(chat, message):
    message_id = getattr(message, 'id', None)

    if message_id is None:
        return None

    usernames = get_public_usernames(chat)

    if usernames:
        return f'https://t.me/{next(iter(usernames))}/{message_id}'

    chat_id = get_tme_c_chat_id(chat)

    if chat_id is not None:
        return f'https://t.me/c/{chat_id}/{message_id}'

    return None


def get_link_keys_from_message(message):
    keys = set()

    for match in TELEGRAM_MESSAGE_LINK_RE.finditer(get_message_text(message)):
        parts = match.group('path').split('/')

        if parts[0] == 'c' and len(parts) >= 3:
            keys.add(('c', int(parts[1]), int(parts[-1])))
        elif len(parts) >= 2:
            keys.add(('username', parts[0].casefold(), int(parts[-1])))

    return keys


def get_source_telegram_link(message):
    match = TELEGRAM_MESSAGE_LINK_RE.search(get_message_text(message))
    return match.group(0) if match else None


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


async def collect_chat_messages(chat, chat_name):
    messages = []

    async for message in client.iter_messages(chat, limit=MESSAGE_LIMIT):
        if not get_message_text(message):
            continue

        messages.append({
            'chat': chat,
            'chat_name': chat_name,
            'message': message,
        })

    print(f'Collected {len(messages)} messages from "{chat_name}".')
    return messages


async def collect_group_messages(group_name):
    group = await resolve_chat(group_name)
    return await collect_chat_messages(group, group_name)


async def collect_all_messages(target_groups, source_folders):
    all_messages = []
    target_group_ids = {get_peer_id(tg) for tg in target_groups}

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
            if get_peer_id(dialog.entity) in target_group_ids:
                continue

            chat_name = str(get_title(dialog))
            print(f'\nCollecting latest messages from "{chat_name}"...')
            all_messages.extend(await collect_chat_messages(dialog.entity, chat_name))

        all_messages.sort(key=lambda item: item['message'].date)
        return all_messages

    for group_name in GROUP_NAMES:

        print(f'\nCollecting latest messages from "{group_name}"...')

        try:
            all_messages.extend(await collect_group_messages(group_name))

        except ValueError as error:
            print(error)

    all_messages.sort(key=lambda item: item['message'].date)

    return all_messages


async def forward_property_messages(
    messages,
    target_groups,
    target_group_names,
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
            mark_processed(unique_id)
            continue

        if not has_keyword(message, keywords):
            continue

        if not has_extra_filter(message, extra_filters):
            continue

        listing_content_key = get_listing_content_key(message)

        if listing_content_key is not None and listing_content_key in processed_content_keys:
            print(
                f'Skipping duplicate listing text in message {message.id} '
                f'from "{chat_name}" because this listing content was already '
                'handled in a prior run.'
            )
            mark_processed(unique_id)
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
                processed_content_keys.add(listing_content_key)

            mark_processed(unique_id)
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
                    processed_content_keys.add(listing_content_key)

                mark_processed(unique_id)
                continue

            if _serialize_forward_origin_key(forward_origin_key) in processed_forward_origins:
                print(
                    f'Skipping duplicate forwarded message {message.id} '
                    f'from "{chat_name}" because this Telegram forward origin '
                    'was already handled in a prior run.'
                )
                mark_processed(unique_id)
                continue

        skip_reason, matched_value = get_skip_reason(message, exclude_filters)

        if skip_reason:
            print(
                f'Skipping message {message.id} from "{chat_name}" '
                f'because it is {skip_reason} and contains "{matched_value}".'
            )
            if listing_content_key is not None:
                processed_content_keys.add(listing_content_key)

            mark_processed(unique_id)
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
                processed_content_keys.add(listing_content_key)

            mark_processed(unique_id)
            continue

        print("\n===================")
        print("PROPERTY FOUND")
        print("===================")
        print(f"Date: {message.date}")
        print(f"Chat: {chat_name}")
        print("\n")

        print(get_message_text(message))

        for target_group, target_group_name in zip(target_groups, target_group_names):
            await client.forward_messages(
                target_group,
                message
            )
            print(f"✅ Sent to {target_group_name}")

        listing_fields = None

        try:
            listing_fields = extract_listing_fields(get_message_text(message))
        except Exception as error:
            print(f"Could not extract listing fields: {error}")

        if listing_fields is not None:
            listing_fields['source_telegram_url'] = get_source_telegram_link(message)

            try:
                append_listing_row(listing_fields, build_message_link(chat, message))
                print(f"📊 Logged to {EXCEL_PATH.name}")
            except Exception as error:
                print(f"Could not log listing to Excel: {error}")

            submit_listing(listing_fields, get_message_text(message), build_message_link(chat, message))

        if listing_content_key is not None:
            processed_content_keys.add(listing_content_key)

        if forward_origin_key is not None:
            processed_forward_origins.add(_serialize_forward_origin_key(forward_origin_key))

        mark_processed(unique_id)
        forwarded_count += 1

    target_groups_str = ", ".join(f'"{name}"' for name in target_group_names)
    print(f'\nForwarded {forwarded_count} matching messages to {target_groups_str}.')

    return forwarded_count


async def handle_live_message(
    event,
    target_groups,
    target_group_names,
    keywords,
    extra_filters,
    exclude_filters,
    log=print,
):
    message = event.message
    chat = await event.get_chat()
    chat_name = str(get_title(chat))

    if not get_message_text(message):
        return

    unique_id = f"{chat_name}_{message.id}"

    if unique_id in processed_messages:
        return

    skip_reason, matched_value = get_skip_reason(message, exclude_filters)

    if skip_reason:
        log(
            f'Skipping message {message.id} from "{chat_name}" '
            f'because it is {skip_reason} and contains "{matched_value}".'
        )
        mark_processed(unique_id)
        return

    if not has_keyword(message, keywords):
        return

    if not has_extra_filter(message, extra_filters):
        return

    listing_content_key = get_listing_content_key(message)

    if listing_content_key is not None and listing_content_key in processed_content_keys:
        log(
            f'Skipping duplicate listing text in message {message.id} '
            f'from "{chat_name}" because this listing content was already '
            'handled in a prior run.'
        )
        mark_processed(unique_id)
        return

    forward_origin_key = get_forward_origin_key(message)

    if forward_origin_key is not None and _serialize_forward_origin_key(forward_origin_key) in processed_forward_origins:
        log(
            f'Skipping duplicate forwarded message {message.id} '
            f'from "{chat_name}" because this Telegram forward origin was '
            'already handled in a prior run.'
        )
        mark_processed(unique_id)
        return

    log('\n===================')
    log('PROPERTY FOUND (live)')
    log('===================')
    log(f'Date: {message.date}')
    log(f'Chat: {chat_name}')
    log(get_message_text(message))

    for target_group, target_group_name in zip(target_groups, target_group_names):
        await client.forward_messages(target_group, message)
        log(f'✅ Sent to {target_group_name}')

    listing_fields = None

    try:
        listing_fields = extract_listing_fields(get_message_text(message))
    except Exception as error:
        log(f'Could not extract listing fields: {error}')

    if listing_fields is not None:
        listing_fields['source_telegram_url'] = get_source_telegram_link(message)

        try:
            append_listing_row(listing_fields, build_message_link(chat, message))
            log(f'📊 Logged to {EXCEL_PATH.name}')
        except Exception as error:
            log(f'Could not log listing to Excel: {error}')

        submit_listing(listing_fields, get_message_text(message), build_message_link(chat, message), log=log)

    if listing_content_key is not None:
        processed_content_keys.add(listing_content_key)

    if forward_origin_key is not None:
        processed_forward_origins.add(_serialize_forward_origin_key(forward_origin_key))

    mark_processed(unique_id)


_live_handler = None


async def stop_live_listener(log=print):
    global _live_handler

    if _live_handler is None:
        log('Live listener is not currently running.')
        return False

    client.remove_event_handler(_live_handler)
    _live_handler = None
    log('Live listener stopped. Only /rent will run searches now.')
    return True


async def start_live_listener(
    source_folders=None,
    keywords=None,
    extra_filters=None,
    exclude_filters=None,
    target_group_names=None,
    log=print,
):
    global _live_handler

    if _live_handler is not None:
        log('Live listener is already running.')
        return _live_handler

    if source_folders is None:
        source_folders = DEFAULT_SOURCE_FOLDERS[:]
    if keywords is None:
        keywords = DEFAULT_KEYWORDS[:]
    if extra_filters is None:
        extra_filters = []
    if exclude_filters is None:
        exclude_filters = []
    if target_group_names is None:
        target_group_names = [TARGET_GROUP]

    target_groups = []
    for name in target_group_names:
        try:
            target_groups.append(await resolve_chat(name))
        except ValueError as error:
            log(str(error))
            return None

    target_group_ids = {get_peer_id(tg) for tg in target_groups}
    chats = []

    if source_folders:
        dialogs_by_id = {}

        for folder_name in source_folders:
            try:
                dialogs = await get_folder_dialogs(folder_name)
            except ValueError as error:
                log(str(error))
                continue

            for dialog in dialogs:
                peer_id = get_peer_id(dialog.entity)

                if peer_id is not None:
                    dialogs_by_id[peer_id] = dialog

        chats = [dialog.entity for dialog in dialogs_by_id.values()]
    else:
        for group_name in GROUP_NAMES:
            try:
                chats.append(await resolve_chat(group_name))
            except ValueError as error:
                log(str(error))

    chats = [chat for chat in chats if get_peer_id(chat) not in target_group_ids]

    log(
        f'Live-listening on {len(chats)} chat(s) for source folder(s) '
        f'{", ".join(source_folders) if source_folders else ", ".join(GROUP_NAMES)}.'
    )
    log(f'Keywords: {", ".join(keywords)}')

    @client.on(events.NewMessage(chats=chats))
    async def _on_new_message(event):
        try:
            await handle_live_message(
                event,
                target_groups,
                target_group_names,
                keywords,
                extra_filters,
                exclude_filters,
                log=log,
            )
        except Exception as error:
            log(f'Error handling live message {event.message.id}: {error}')

    _live_handler = _on_new_message
    return _live_handler


async def main():
    print(f'Running script: {os.path.abspath(__file__)}')
    print(f'Built-in unavailable keywords: {", ".join(UNAVAILABLE_KEYWORDS)}')

    source_folders = ask_for_source_folders()
    keywords = ask_for_keywords()
    extra_filters = ask_for_extra_filters()
    exclude_filters = ask_for_exclude_filters()
    target_group_names = ask_for_target_groups()

    target_groups = []
    for target_group_name in target_group_names:
        try:
            target_group = await resolve_chat(target_group_name)
            target_groups.append(target_group)
        except ValueError as error:
            print(error)
            return

    for i, target_group_name in enumerate(target_group_names):
        try:
            await clear_chat(target_groups[i], target_group_name)
        except Exception as error:
            print(f'Could not clear "{target_group_name}": {error}')
            return

    messages = await collect_all_messages(target_groups, source_folders)
    await forward_property_messages(
        messages,
        target_groups,
        target_group_names,
        keywords,
        extra_filters,
        exclude_filters
    )


async def run(
    source_folders=None,
    keywords=None,
    extra_filters=None,
    exclude_filters=None,
    target_group_names=None,
    log=print,
):
    if source_folders is None:
        source_folders = DEFAULT_SOURCE_FOLDERS[:]
    if keywords is None:
        keywords = DEFAULT_KEYWORDS[:]
    if extra_filters is None:
        extra_filters = []
    if exclude_filters is None:
        exclude_filters = []
    if target_group_names is None:
        target_group_names = [TARGET_GROUP]

    log(f'Searching folders: {", ".join(source_folders) if source_folders else ", ".join(GROUP_NAMES)}')
    log(f'Keywords: {", ".join(keywords)}')

    target_groups = []
    for name in target_group_names:
        try:
            target_groups.append(await resolve_chat(name))
        except ValueError as error:
            log(str(error))
            return

    for i, name in enumerate(target_group_names):
        try:
            await clear_chat(target_groups[i], name)
        except Exception as error:
            log(f'Could not clear "{name}": {error}')
            return

    messages = await collect_all_messages(target_groups, source_folders)
    forwarded_count = await forward_property_messages(
        messages,
        target_groups,
        target_group_names,
        keywords,
        extra_filters,
        exclude_filters,
    )

    target_groups_str = ", ".join(f'"{name}"' for name in target_group_names)
    log(f'Forwarded {forwarded_count} matching messages to {target_groups_str}.')


async def run_quick_search(
    source_folders=None,
    keywords=None,
    target_group_name=None,
    log=print,
):
    if source_folders is None:
        source_folders = DEFAULT_SOURCE_FOLDERS[:]
    if keywords is None:
        keywords = QUICK_SEARCH_KEYWORDS[:]
    if target_group_name is None:
        target_group_name = QUICK_SEARCH_TARGET_GROUP

    log(f'Quick search folders: {", ".join(source_folders) if source_folders else ", ".join(GROUP_NAMES)}')
    log(f'Quick search keywords: {", ".join(keywords)}')

    try:
        target_group = await resolve_chat(target_group_name)
    except ValueError as error:
        log(str(error))
        return

    messages = await collect_all_messages([target_group], source_folders)

    forwarded_count = 0

    for item in messages:
        chat_name = item['chat_name']
        message = item['message']
        unique_id = f"{chat_name}_{message.id}"

        if unique_id in quick_search_processed_messages:
            continue

        skip_reason, matched_value = get_skip_reason(message)

        if skip_reason:
            mark_quick_search_processed(unique_id)
            continue

        if not has_keyword(message, keywords):
            continue

        await client.forward_messages(target_group, message)
        log(f'✅ Sent to {target_group_name}: message {message.id} from "{chat_name}"')
        mark_quick_search_processed(unique_id)
        forwarded_count += 1

    log(f'Quick search forwarded {forwarded_count} matching message(s) to "{target_group_name}".')


if __name__ == '__main__':
    with client:
        client.loop.run_until_complete(main())
