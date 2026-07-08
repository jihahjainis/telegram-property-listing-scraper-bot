import json

from anthropic import Anthropic

MODEL = 'claude-haiku-4-5'

PROPERTY_TYPES = ['condo', 'apartment', 'service_residence', 'landed', 'studio', 'room']

MALAYSIAN_STATES = [
    'Johor', 'Kedah', 'Kelantan', 'Melaka', 'Negeri Sembilan', 'Pahang',
    'Penang', 'Perak', 'Perlis', 'Sabah', 'Sarawak', 'Selangor',
    'Terengganu', 'Kuala Lumpur', 'Labuan', 'Putrajaya',
]

EXTRACTION_SCHEMA = {
    'type': 'object',
    'properties': {
        'property_name': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'area': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'state': {'anyOf': [{'type': 'string', 'enum': MALAYSIAN_STATES}, {'type': 'null'}]},
        'property_type': {'type': 'string', 'enum': PROPERTY_TYPES},
        'size_sqft': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'rental_price': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'bedrooms': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'bathrooms': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'parking': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'sorting_price': {'anyOf': [{'type': 'number'}, {'type': 'null'}]},
        'contact_name': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'contact_phone': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'listing_url': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'open_to_foreigners': {'anyOf': [{'type': 'boolean'}, {'type': 'null'}]}
    },
    'required': [
        'property_name', 'area', 'state', 'property_type', 'size_sqft', 'rental_price',
        'bedrooms', 'bathrooms', 'parking', 'sorting_price', 'contact_name', 'contact_phone',
        'listing_url', 'open_to_foreigners'
    ],
    'additionalProperties': False,
}

EXTRACTION_PROMPT = (
    'Extract the property name, area, Malaysian state, property type, size_sqft, rental price, number of '
    'bedrooms, number of bathrooms, number of parking spaces, sorting price, contact name, contact phone '
    'number, listing url and foreigner availability from this rental listing.\n'
    f'For property type, choose the closest match from exactly these options: {", ".join(PROPERTY_TYPES)}. '
    'Never leave it null; pick the closest match even if the listing is not explicit (e.g. "room for rent" is '
    '"room", a house or "banglo" is "landed").\n'
    f'For state, choose the closest match from exactly these Malaysian states/territories: {", ".join(MALAYSIAN_STATES)}. '
    'If the state is not explicitly mentioned, infer it from the area, address, or city named in the listing '
    '(e.g. "Petaling Jaya" or "Subang Jaya" implies "Selangor", "Bangsar" or "Cheras" implies "Kuala Lumpur"). '
    'Only use null if there is truly no location information to infer from.\n'
    'For sorting price, convert every rental price into a numeric value (e.g., "RM1,200" becomes 1200, RM2,400 / month becomes 2400). '
    'For contact name and contact phone, extract the listing owner/agent\'s name and phone number if mentioned. '
    'For listing_url, extract the Telegram link or any direct listing URL if provided.\n'
    'For open_to_foreigners, extract a boolean (true/false) based on the listing\'s stated eligibility. '
    'Look for phrases like "Open for All Locals & Foreigners", "Locals Only", "Foreigners Welcome", etc. '
    'If the listing explicitly states "All Locals & Foreigners" or similar, set to true. '
    'If it says "Locals Only" or excludes foreigners, set to false. '
    'If eligibility is not mentioned, default to true. '
    'Use null for any other field that is not mentioned or unclear. Do not guess beyond what is instructed above.\n\n'
)

_client = None


def _get_client():
    global _client

    if _client is None:
        _client = Anthropic()

    return _client


def extract_listing_fields(text):
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=256,
        output_config={'format': {'type': 'json_schema', 'schema': EXTRACTION_SCHEMA}},
        messages=[{
            'role': 'user',
            'content': EXTRACTION_PROMPT + text,
        }],
    )

    return json.loads(response.content[0].text)
