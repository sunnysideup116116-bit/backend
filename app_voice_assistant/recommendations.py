"""Resolve spoken place references against actual Public Ayue results."""
import re
from .contracts import VoiceProposal


def resolve_calendar_recommendation(proposal, places):
    if proposal.intent not in {'calendar.create', 'calendar.update'} or not places:
        return proposal, None
    title = str(proposal.arguments.get('title') or '')
    location = str(proposal.arguments.get('location') or '')
    query = title + ' ' + location
    normalize = lambda value: re.sub(r'[\s，,。·・—－-]', '', value.lower()).replace('臺', '台')
    exact = [p for p in places if normalize(p['name']) in normalize(query)]
    keywords = {
        'cafe': ('咖啡廳', '咖啡店', '咖啡館', '喝咖啡', 'cafe', 'coffee'),
        'restaurant': ('餐廳', '餐館', '吃飯', 'restaurant'),
    }
    matches = exact
    generic = re.sub(r'(?:剛剛|推薦的|那家|那間|去|到|的|喝咖啡)', '', title).strip().lower()
    generic_names = {'', '咖啡', '咖啡廳', '咖啡店', '咖啡館', '餐廳', '餐館', '吃飯',
                     'cafe', 'coffee', 'coffee shop', 'restaurant'}
    generic_location = normalize(location) in {normalize(value) for value in generic_names}
    if not matches:
        if generic not in generic_names:
            return proposal, None
        category = next((key for key, terms in keywords.items() if any(t in query.lower() for t in terms)), None)
        if category:
            matches = [p for p in places if p.get('category') == category
                       or any(t in p['name'].lower() for t in keywords[category])]
            if not generic_location:
                matches = [p for p in matches if normalize(location) in normalize(
                    p['name'] + ' ' + p.get('address_summary', ''))]
    # Repeated cards for the same place are one candidate; branches differ by address.
    matches = list({(p['name'], p.get('address_summary', '')): p for p in matches}.values())
    if len(matches) > 1:
        return None, '你指的是哪一家：' + '、'.join(p['name'] for p in matches[:5]) + '？'
    if not matches:
        return proposal, None
    place = matches[0]
    args = dict(proposal.arguments)
    if not exact:
        # An explicit different venue must never be replaced by a prior recommendation.
        args['title'] = place['name']
    if generic_location or (not exact and place.get('address_summary')):
        # Name plus source area helps Places disambiguate without inventing an address.
        args['location'] = place.get('address_summary') or place['name']
    return VoiceProposal(proposal.intent, args, proposal.reply, proposal.base_revision), None
