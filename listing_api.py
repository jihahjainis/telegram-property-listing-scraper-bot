import requests

from telegram_config import get_therooma_config

REQUEST_TIMEOUT = 10

_warned_not_configured = False


def _build_title(fields):
    property_name = fields.get('property_name')

    if property_name:
        return property_name

    property_type = (fields.get('property_type') or 'condo').replace('_', ' ').title()
    area = fields.get('area')

    if area:
        return f'{property_type} in {area}'

    return 'Rental Listing'


def _build_payload(fields, message_text, message_link):
    return {
        'title': _build_title(fields),
        'description': message_text,
        'propertyType': fields.get('property_type') or 'condo',
        'state': fields.get('state'),
        'area': fields.get('area'),
        'price': fields.get('sorting_price'),
        'bedrooms': fields.get('bedrooms'),
        'bathrooms': fields.get('bathrooms'),
        'parking': fields.get('parking'),
        'sizeSqft': fields.get('size_sqft'),
        'contactName': fields.get('contact_name'),
        'contactPhone': fields.get('contact_phone'),
        'telegramMessageUrl': message_link,
        'status': 'active',
    }


def submit_listing(fields, message_text, message_link, log=print):
    global _warned_not_configured

    api_url, api_key = get_therooma_config()

    if not api_url or not api_key:
        if not _warned_not_configured:
            log('THEROOMA_API_URL/THEROOMA_API_KEY not configured; skipping website sync.')
            _warned_not_configured = True
        return

    if fields.get('sorting_price') is None:
        log('Skipping website sync: no price could be extracted for this listing.')
        return

    if not fields.get('state'):
        log('Skipping website sync: no state could be determined for this listing.')
        return

    payload = _build_payload(fields, message_text, message_link)

    try:
        response = requests.post(
            api_url,
            json=payload,
            headers={'X-API-Key': api_key},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        listing_id = response.json().get('id')
        log(f'🌐 Synced to therooma.my (listing id {listing_id})')
    except requests.RequestException as error:
        log(f'Could not sync listing to therooma.my: {error}')
