import json

from anthropic import Anthropic

MODEL = 'claude-haiku-4-5'

EXTRACTION_SCHEMA = {
    'type': 'object',
    'properties': {
        'property_name': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'area': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'property_type': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'size_sqft': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'rental_price': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'bedrooms': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'bathrooms': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'parking': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'sorting_price': {'anyOf': [{'type': 'number'}, {'type': 'null'}]},
    },
    'required': ['property_name', 'area', 'property_type', 'size_sqft', 'rental_price', 'bedrooms', 'bathrooms', 'parking', 'sorting_price'],
    'additionalProperties': False,
}

EXTRACTION_PROMPT = (
    'Extract the property name, area, property type, size_sqft,rental price, number of bedrooms, number of '
    'bathrooms, number of parking spaces, and sorting price from this rental listing. '
    'For sorting price, convert every rental price into a numeric value (e.g., "RM1,200" becomes 1200, RM2,400 / month becomes 2400). '
    'Use null for any field that is not mentioned or unclear. Do not guess.\n\n'
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
