"""End-to-end test for the GoHighLevel no-show rebooking agent against MOCK APIs.

No real GoHighLevel account, Claude account or Slack workspace is used. The script:
  1. starts mock GoHighLevel, Anthropic and Slack APIs on this machine,
  2. imports and activates a temporary copy of workflow/error-alert-slack.json (n8n only runs an active error
     workflow), then a temporary copy of the no-show workflow that reports to it (API hosts pointed at the mocks,
     fake keys). In this copy the two schedule triggers are swapped for webhooks, so the test decides when the
     "every 15 minutes" run and the daily summary happen,
  3. sends the exact requests the native GoHighLevel workflows in ghl/native-workflows.md send (Custom Webhook with
     a Bearer token, as JSON and as form fields, plus the standard Webhook action's payload) and plays the patients'
     answers, outages and repeat deliveries. To let time pass (follow-ups after 24 and 72 hours, an offered time that
     has gone), it edits the workflow's static data through the n8n API,
  4. checks every request the workflow made, the node each run ended on, and that every failed run raised exactly
     one error alert linked to it,
  5. imports a copy with broken settings (the no-show webhook must answer 500 and name the problems) and a copy with
     the real schedule trigger and a no-show policy text,
  6. prints PASS or FAIL per check and deletes every temporary copy, also after a crash.
Temporary copies are named "TEST ghl-no-show-rebooking-agent (temporary)". Copies with exactly that name left by an
interrupted run are deleted at the start. No other workflow is touched.

Requirements: Python 3.9+, a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
  SAVE_REQUESTS  optional file path; every request the workflow made to the mocks is saved there as JSON
                 (method, path, query, body and the mock's status code; headers are left out)
Usage: python tests/e2e_mock_test.py
"""
import copy, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8806
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
WORKFLOW = Path(__file__).resolve().parent.parent / 'workflow' / 'ghl-no-show-rebooking-agent.json'
SLUG = 'ghl-no-show-rebooking-agent'
TEMP_NAME = f'TEST {SLUG} (temporary)'

# ----------------------------------------------------------------------------------------------------------------------
# Time zones. Test patients use fixed offset zones (Etc/GMT...), so the quiet hours checks do not depend on the clock.
# ----------------------------------------------------------------------------------------------------------------------
NOW = datetime.now(timezone.utc)


def etc_zone(local_hour):
    """An Etc/GMT zone in which it is local_hour o'clock right now. Note Etc/GMT-4 means UTC+4."""
    offset = ((local_hour - NOW.hour + 12) % 24) - 12
    return 'Etc/GMT' if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def tz_minutes(name):
    m = re.fullmatch(r'Etc/GMT(?:([+-])(\d{1,2}))?', name or '')
    if not m:
        return None
    return 0 if not m.group(1) else (-1 if m.group(1) == '+' else 1) * int(m.group(2)) * 60


IN_TZ = etc_zone(13)    # 1 PM there: inside the 9 AM to 7 PM texting window. Also the clinic's own time zone.
OUT_TZ = etc_zone(3)    # 3 AM there: quiet hours
CLINIC = timezone(timedelta(minutes=tz_minutes(IN_TZ)))
# GoHighLevel documents the contact appointment list with times like "2021-07-16 11:00:00" and does not say in which
# time zone. The mock writes them in a zone 5 hours off the clinic's, so the test proves the workflow does not depend on it.
LIST_ZONE = timezone(timedelta(minutes=tz_minutes(IN_TZ) + 300))
LOC, CAL, OTHER_CAL, PIPE = 'loc-harbor-1', 'cal-visits', 'cal-classes', 'pipe-patients'
STAGES = {'stage-new', 'stage-booked', 'stage-no-show', 'stage-rebooked', 'stage-lost'}
STAFF = {'usr-maya', 'usr-omar', 'usr-lena'}
CAPACITY = 3   # three physios, so each open time can be booked three times
_TODAY = datetime.now(timezone(timedelta(minutes=tz_minutes(etc_zone(13))))).date()
EVENING_DAYS = {(_TODAY + timedelta(days=3)).weekday(), (_TODAY + timedelta(days=5)).weekday()}
ASKED_DAY = (_TODAY + timedelta(days=3)).strftime('%A')           # for example "Thursday"
ASKED_KEY = ASKED_DAY[:3].lower()                                  # "thu", as in the tool schema
GHL_KEY = 'pit-mock-key'
SECRET = 'test-webhook-secret'

ENV = {
    'GHL_API_KEY': GHL_KEY, 'GHL_LOCATION_ID': LOC, 'GHL_CALENDAR_ID': CAL, 'GHL_PIPELINE_ID': PIPE,
    'GHL_NO_SHOW_STAGE_ID': 'stage-no-show', 'GHL_REBOOKED_STAGE_ID': 'stage-rebooked', 'GHL_FROM_NUMBER': '',
    'GHL_APP_URL': 'https://app.gohighlevel.com', 'GHL_WEBHOOK_SECRET': SECRET,
    'ANTHROPIC_API_KEY': 'mock-anthropic-key', 'ANTHROPIC_MODEL': 'claude-sonnet-5',
    'BUSINESS_NAME': 'Harbor Physio Clinic', 'BUSINESS_ABOUT': 'a physiotherapy clinic', 'VISIT_NAME': 'physio appointment',
    'NO_SHOW_POLICY_TEXT': '', 'TAG_PREFIX': 'no-show', 'SAME_STAFF': 'true', 'SLOT_SEARCH_DAYS': '7',
    'SEND_HOUR_START': '9', 'SEND_HOUR_END': '19', 'BUSINESS_TZ': IN_TZ, 'FOLLOW_UP_HOURS': '24,72', 'CLOSE_AFTER_HOURS': '120',
    'MAX_AI_REPLIES': '3', 'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
}
HOSTS = {'https://services.leadconnectorhq.com': MOCK, 'https://api.anthropic.com': MOCK}
ACTIVE, REBOOKED, DECLINED, NO_REPLY = 'no-show', 'no-show-rebooked', 'no-show-declined', 'no-show-no-reply'
HUMAN, STOPPED, FLAG = 'no-show-needs-human', 'no-show-opted-out', 'no-show-agent-booked'
HOLD = 'Thanks {first}! Someone from the clinic will text you back shortly.'


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def clinic_today(hour, minute=0, days=0):
    d = datetime.now(CLINIC).date() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=CLINIC)


def label(iso_text):
    """The label the workflow builds with Intl.DateTimeFormat('en-US'), for example 'Tue, Oct 27, 10:00 AM'."""
    t = datetime.fromisoformat(iso_text)
    return f"{t.strftime('%a')}, {t.strftime('%b')} {t.day}, {(t.hour % 12) or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


# ----------------------------------------------------------------------------------------------------------------------
# Mock state
# ----------------------------------------------------------------------------------------------------------------------
LOCK = threading.RLock()
CONTACTS, CONVERSATIONS, MESSAGES, APPOINTMENTS, OPPORTUNITIES = {}, {}, {}, {}, {}
TAKEN = set()
FAIL = {'tag': set(), 'sms': set(), 'sms_429_once': set(), 'messages': set(), 'put_contact': set(), 'get_appointment': set(),
        'slots': False, 'opportunity_put': set()}
LOG = []          # dict(service, method, path, query, headers, body, status, response, t)
COUNTER = {'n': 0}


def next_id(prefix):
    COUNTER['n'] += 1
    return f'{prefix}{COUNTER["n"]:04d}'


def add_contact(cid, first, last, phone, tz=IN_TZ, tags=(), dnd=False, dnd_settings=None):
    CONTACTS[cid] = {
        'id': cid, 'locationId': LOC, 'firstName': first, 'lastName': last, 'name': f'{first} {last}',
        'email': f'{first.lower()}.{last.lower()}@example.com', 'phone': phone, 'timezone': tz, 'tags': list(tags),
        'dnd': dnd, 'dndSettings': dnd_settings or {}, 'type': 'lead', 'source': 'booking widget',
        'dateAdded': iso(NOW - timedelta(days=200)), 'dateUpdated': iso(NOW - timedelta(days=1)),
    }
    conv = 'conv-' + cid
    CONVERSATIONS[cid] = conv
    MESSAGES[conv] = []
    return cid


def add_message(cid, direction, body, minutes_ago=0.0):
    conv = CONVERSATIONS[cid]
    mid = next_id('msg-')
    MESSAGES[conv].append({
        'id': mid, 'type': 2, 'messageType': 'TYPE_SMS', 'locationId': LOC, 'contactId': cid, 'conversationId': conv,
        'dateAdded': iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)), 'body': body, 'direction': direction,
        'status': 'delivered', 'contentType': 'text/plain', 'source': 'api' if direction == 'outbound' else None,
    })
    return mid


def add_appointment(aid, cid, start, minutes=30, status='noshow', cal=CAL, staff=''):
    APPOINTMENTS[aid] = {'id': aid, 'calendarId': cal, 'contactId': cid, 'start': start, 'end': start + timedelta(minutes=minutes),
                         'status': status, 'assignedUserId': staff, 'title': f'Physio visit: {CONTACTS[cid]["name"]}'}
    return aid


def event_dto(a):
    """GET /calendars/events/appointments/{eventId}: CalendarEventDTO, times with an offset as in the docs' example."""
    return {'id': a['id'], 'title': a['title'], 'calendarId': a['calendarId'], 'locationId': LOC, 'contactId': a['contactId'],
            'groupId': 'grp-harbor', 'appointmentStatus': a['status'], 'assignedUserId': a['assignedUserId'],
            'users': [a['assignedUserId']] if a['assignedUserId'] else [], 'notes': '', 'description': '', 'isRecurring': False,
            'address': '', 'startTime': a['start'].astimezone(CLINIC).isoformat(), 'endTime': a['end'].astimezone(CLINIC).isoformat(),
            'dateAdded': iso(NOW - timedelta(days=10)), 'dateUpdated': iso(NOW)}


def contact_event(a):
    """GET /contacts/{contactId}/appointments: GetEventSchema. The docs show times as "2021-07-16 11:00:00" (no offset,
    no zone named); the mock sends them in LIST_ZONE, 5 hours off the clinic's zone."""
    fmt = lambda t: t.astimezone(LIST_ZONE).strftime('%Y-%m-%d %H:%M:%S')
    return {'id': a['id'], 'calendarId': a['calendarId'], 'status': 'booked' if a['status'] in ('new', 'confirmed') else a['status'],
            'title': a['title'], 'assignedUserId': a['assignedUserId'], 'notes': '', 'startTime': fmt(a['start']), 'endTime': fmt(a['end']),
            'address': '', 'locationId': LOC, 'contactId': a['contactId'], 'groupId': 'grp-harbor', 'appointmentStatus': a['status'],
            'users': [], 'dateAdded': fmt(NOW - timedelta(days=10)), 'dateUpdated': fmt(NOW), 'assignedResources': []}


def open_instants(start_ms, end_ms):
    """The clinic's open times (clinic time): 9:00 and 14:00 every day, plus 18:00 on two weekdays (EVENING_DAYS),
    from tomorrow on, without times that are fully booked (CAPACITY) or taken by someone else (TAKEN)."""
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    out, day = [], datetime.now(CLINIC).date() + timedelta(days=1)
    booked = [a['start'] for a in APPOINTMENTS.values() if a['status'] in ('new', 'confirmed')]
    while day <= end.astimezone(CLINIC).date():
        hours = (9, 14, 18) if day.weekday() in EVENING_DAYS else (9, 14)
        for h in hours:
            t = datetime(day.year, day.month, day.day, h, tzinfo=CLINIC)
            if start <= t <= end and t not in TAKEN and booked.count(t) < CAPACITY:
                out.append(t)
        day += timedelta(days=1)
    return out


# ---- Claude ----
# What the mock Claude answers, keyed by the patient's newest message. A number is an HTTP error.
SCRIPT = {
    '2 works for me': dict(intent='picked_time', slot_choice=2, summary='Picked the second time'),
    f'Do you have anything {ASKED_DAY} evening?': dict(intent='other_times', preferred_days=[ASKED_KEY], preferred_time='evening',
                                                   reply='Sure, here is what we have:', summary='Wants a Thursday evening'),
    '1': dict(intent='picked_time', slot_choice=1),
    'the first one': dict(intent='picked_time', slot_choice=1),
    '2': dict(intent='picked_time', slot_choice=2),
    '3': dict(intent='picked_time', slot_choice=3),
    'Can someone call me? I have a question about my knee': dict(intent='call_or_question', reply='Of course! Someone from the clinic will call you soon.',
                                                                 summary='Wants a call about their knee'),
    "No thanks, I'll book when I'm back from my trip": dict(intent='not_rebooking', reply='No problem at all, Hal. Safe travels, and text us any time you want to rebook.'),
    'please stop texting me': dict(intent='stop'),
    'Is there parking near you?': 529,
    'Please stop sending these': 529,
    'hmm ok??': 'text-only',
    'I need to think about it and maybe': 'max-tokens',
    'what?': dict(intent='unclear', reply='Sorry for the confusion! Would you like to pick a new time for your visit?'),
    'Is the clinic open on Saturdays?': dict(intent='unclear', reply='Would you like me to look for a weekend time for you?'),
}
TOOL_FIELDS = {'intent', 'slot_choice', 'preferred_days', 'preferred_time', 'reply', 'summary'}
NO_FORCED_TOOL = re.compile(r'^claude-(fable-5-1|mythos-5-1|opus-5-5)\b')
NO_THINKING_OFF = re.compile(r'^claude-(fable|mythos|opus-5-5)\b')
# Top level fields of a Messages API request. The real API rejects anything else ("Extra inputs are not permitted").
CLAUDE_REQUEST_KEYS = {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice', 'thinking', 'metadata', 'stop_sequences',
                       'stream', 'temperature', 'top_p', 'top_k', 'output_config', 'service_tier', 'container', 'mcp_servers',
                       'context_management', 'inference_geo'}
TOOL_KEYS = {'name', 'description', 'input_schema', 'strict', 'cache_control', 'type'}


def claude_problems(headers, body):
    """The checks the real Messages API would fail this request on."""
    p = []
    if headers.get('x-api-key') != ENV['ANTHROPIC_API_KEY']:
        p.append('missing or wrong x-api-key')
    if headers.get('anthropic-version') != '2023-06-01':
        p.append('missing anthropic-version')
    if not str(headers.get('content-type', '')).startswith('application/json'):
        p.append('content-type must be application/json')
    model = str(body.get('model', ''))
    if not model or not isinstance(body.get('max_tokens'), int) or body.get('max_tokens', 0) < 1:
        p.append('model and max_tokens are required')
    for key in sorted(set(body) - CLAUDE_REQUEST_KEYS):
        p.append(f'{key}: Extra inputs are not permitted')
    for key in ('temperature', 'top_p', 'top_k'):
        if key in body:
            p.append(f'{key} is not supported on this model')
    thinking = body.get('thinking')
    if thinking is not None and thinking not in ({'type': 'disabled'}, {'type': 'adaptive'}):
        p.append('thinking must be disabled or adaptive (budget_tokens is not supported)')
    forced = isinstance(body.get('tool_choice'), dict) and body['tool_choice'].get('type') in ('tool', 'any')
    if forced and NO_FORCED_TOOL.search(model):
        p.append('tool_choice: type "tool" and "any" are not supported for this model.')
    if thinking == {'type': 'disabled'} and NO_THINKING_OFF.search(model):
        p.append('thinking: type "disabled" is not supported for this model.')
    # Claude Sonnet 5 and Opus 5 think by default when "thinking" is left out, and thinking cannot be combined with a
    # forced tool call. Haiku 4.5 does not think unless asked.
    if forced and not model.startswith('claude-haiku') and thinking != {'type': 'disabled'}:
        p.append('Thinking may not be enabled when tool_choice forces tool use.')
    if not isinstance(body.get('system'), str) or not body['system'].strip():
        p.append('system must be a non empty string')
    msgs = body.get('messages') or []
    if not msgs or msgs[0].get('role') != 'user' or msgs[-1].get('role') != 'user':
        p.append('messages must start and end with the user')
    if any(a.get('role') == b.get('role') for a, b in zip(msgs, msgs[1:])):
        p.append('roles must alternate')
    for m in msgs:
        content = m.get('content')
        blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
        if not isinstance(blocks, list) or not blocks or any(b.get('type') != 'text' or not str(b.get('text', '')).strip() for b in blocks):
            p.append('every message needs non empty text content')
    tools = body.get('tools') or [{}]
    for key in sorted(set(tools[0]) - TOOL_KEYS):
        p.append(f'tools.0.{key}: Extra inputs are not permitted')
    schema = tools[0].get('input_schema', {})
    if (schema.get('type') != 'object' or not set(schema.get('required', [])) <= set(schema.get('properties', {}))
            or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', tools[0].get('name', ''))):
        p.append('tool needs a name and an object input_schema whose required fields exist')
    if tools[0].get('strict') is True and schema.get('additionalProperties') is not False:
        p.append('strict tools need additionalProperties: false')
    if body.get('tool_choice') != {'type': 'tool', 'name': tools[0].get('name')}:
        p.append('tool must be forced')
    return p


# ----------------------------------------------------------------------------------------------------------------------
# Mock GoHighLevel API (v2, services.leadconnectorhq.com)
# ----------------------------------------------------------------------------------------------------------------------
GHL_ERR_401 = {'statusCode': 401, 'message': 'Invalid token: access token is invalid', 'error': 'Unauthorized'}
GHL_500 = {'statusCode': 500, 'message': 'Internal server error'}


def unprocessable(*messages):
    return 422, {'statusCode': 422, 'message': list(messages), 'error': 'Unprocessable Entity'}


def bad_request(message):
    return 400, {'statusCode': 400, 'message': message}


# Body properties each endpoint accepts, from GoHighLevel's OpenAPI files. Like the real API, the mock answers
# "property X should not exist" for anything else.
UPDATE_CONTACT_KEYS = {'firstName', 'lastName', 'name', 'email', 'phone', 'address1', 'city', 'state', 'postalCode', 'website', 'timezone',
                       'dnd', 'dndSettings', 'inboundDndSettings', 'tags', 'customFields', 'source', 'dateOfBirth', 'country', 'assignedTo'}
SEND_MESSAGE_KEYS = {'type', 'subType', 'contactId', 'appointmentId', 'attachments', 'emailFrom', 'emailCc', 'emailBcc', 'html', 'message',
                     'subject', 'replyMessageId', 'templateId', 'threadId', 'scheduledTimestamp', 'conversationProviderId', 'emailTo',
                     'customSubtypeId', 'emailReplyMode', 'fromNumber', 'toNumber', 'forward', 'status', 'usesNativeSchedulingAi', 'optimizationPeriod'}
APPOINTMENT_KEYS = {'title', 'meetingLocationType', 'meetingLocationId', 'overrideLocationConfig', 'appointmentStatus', 'assignedUserId',
                    'description', 'address', 'ignoreDateRange', 'toNotify', 'ignoreFreeSlotValidation', 'rrule', 'calendarId', 'locationId',
                    'contactId', 'startTime', 'endTime'}
CREATE_OPPORTUNITY_KEYS = {'pipelineId', 'locationId', 'name', 'pipelineStageId', 'status', 'contactId', 'monetaryValue', 'assignedTo', 'customFields'}
UPDATE_OPPORTUNITY_KEYS = {'pipelineId', 'name', 'pipelineStageId', 'status', 'monetaryValue', 'assignedTo', 'customFields'}
FREE_SLOT_QUERY = {'startDate', 'endDate', 'timezone', 'userId', 'userIds'}
DND_CHANNELS = {'Call', 'Email', 'SMS', 'WhatsApp', 'GMB', 'FB'}
MESSAGE_TYPES = {'SMS', 'RCS', 'Email', 'WhatsApp', 'IG', 'FB', 'Custom', 'Live_Chat', 'TIKTOK'}


def unknown_keys(body, allowed):
    extra = sorted(set(body) - allowed)
    return unprocessable(*[f'property {k} should not exist' for k in extra]) if extra else None


def ghl(method, path, query, headers, body):
    """Returns (status, json). Validates like the real GoHighLevel API v2 and keeps state."""
    if headers.get('authorization') != 'Bearer ' + GHL_KEY:
        return 401, GHL_ERR_401
    version = '2021-04-15' if path.startswith(('/conversations', '/calendars')) else '2021-07-28'
    if headers.get('version') != version:
        return bad_request(f'Version header must be {version} for this endpoint')
    if method in ('POST', 'PUT', 'DELETE') and body is not None and not str(headers.get('content-type', '')).startswith('application/json'):
        return bad_request('Content-Type must be application/json')
    q = {k: v[0] for k, v in query.items()}

    m = re.fullmatch(r'/contacts/([^/]+)(/tags|/appointments)?', path)
    if m:
        c = CONTACTS.get(m.group(1))
        if not c:
            return bad_request('Contact not found')
        if m.group(2) == '/appointments':
            if method != 'GET':
                return 404, {'statusCode': 404, 'message': f'Cannot {method} {path}'}
            return 200, {'events': [contact_event(a) for a in APPOINTMENTS.values() if a['contactId'] == c['id']]}
        if m.group(2) == '/tags':
            if method not in ('POST', 'DELETE'):
                return 404, {'statusCode': 404, 'message': f'Cannot {method} {path}'}
            body = body or {}
            if unknown_keys(body, {'tags'}):
                return unknown_keys(body, {'tags'})
            tags = body.get('tags')
            if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
                return unprocessable('tags must be a non empty array of strings')
            if c['id'] in FAIL['tag']:
                return 500, GHL_500
            if method == 'POST':
                for t in tags:
                    if t.lower() not in c['tags']:
                        c['tags'].append(t.lower())
                return 201, {'tags': c['tags']}
            c['tags'] = [t for t in c['tags'] if t not in {x.lower() for x in tags}]
            return 200, {'tags': c['tags']}
        if method == 'GET':
            return 200, {'contact': copy.deepcopy(c)}
        if method == 'PUT':
            if c['id'] in FAIL['put_contact']:
                return 500, GHL_500
            if unknown_keys(body, UPDATE_CONTACT_KEYS):
                return unknown_keys(body, UPDATE_CONTACT_KEYS)
            if 'dndSettings' in body:
                d = body['dndSettings']
                if not isinstance(d, dict) or set(d) - DND_CHANNELS or any(
                        not isinstance(v, dict) or v.get('status') not in ('active', 'inactive', 'permanent') for v in d.values()):
                    return unprocessable('dndSettings must map channels to { status: active | inactive | permanent }')
                c['dndSettings'] = copy.deepcopy(d)   # replaces the whole object, so callers must merge
            return 200, {'succeded': True, 'contact': copy.deepcopy(c)}

    m = re.fullmatch(r'/calendars/events/appointments/([^/]+)', path)
    if m and method == 'GET':
        a = APPOINTMENTS.get(m.group(1))
        if a and a['id'] in FAIL['get_appointment']:
            return 500, GHL_500
        if not a:
            return bad_request('Appointment not found')
        return 200, {'event': event_dto(a)}

    m = re.fullmatch(r'/calendars/([^/]+)/free-slots', path)
    if method == 'GET' and m:
        if m.group(1) != CAL:
            return bad_request('Calendar not found')
        extra = sorted(set(q) - FREE_SLOT_QUERY)
        if extra:
            return unprocessable(*[f'property {k} should not exist' for k in extra])
        if not re.fullmatch(r'\d{12,14}', q.get('startDate', '')) or not re.fullmatch(r'\d{12,14}', q.get('endDate', '')):
            return unprocessable('startDate and endDate must be timestamps in milliseconds')
        start, end = int(q['startDate']), int(q['endDate'])
        if end <= start or end - start > 31 * 86400000:
            return bad_request('Date range cannot be more than 31 days')
        if q.get('timezone') and tz_minutes(q['timezone']) is None:
            return unprocessable('timezone is not valid')
        if 'userId' in q and q['userId'] not in STAFF:
            return bad_request('User is not part of this calendar')
        if FAIL['slots']:
            return 503, {'statusCode': 503, 'message': 'Service Unavailable'}
        zone = timezone(timedelta(minutes=tz_minutes(q.get('timezone')) or 0))
        out = {}
        for t in open_instants(start, end):
            local = t.astimezone(zone)
            out.setdefault(local.date().isoformat(), {'slots': []})['slots'].append(local.isoformat())
        return 200, out

    if method == 'POST' and path == '/calendars/events/appointments':
        if unknown_keys(body, APPOINTMENT_KEYS):
            return unknown_keys(body, APPOINTMENT_KEYS)
        missing = [k for k in ('calendarId', 'locationId', 'contactId', 'startTime') if not body.get(k)]
        if missing:
            return unprocessable(*[f'{k} should not be empty' for k in missing])
        if body['calendarId'] != CAL or body['locationId'] != LOC or body['contactId'] not in CONTACTS:
            return bad_request('Calendar, location or contact not found')
        try:
            start = datetime.fromisoformat(body['startTime'])
            end = datetime.fromisoformat(body['endTime']) if body.get('endTime') else start + timedelta(minutes=30)
        except ValueError:
            return unprocessable('startTime and endTime must be ISO 8601 dates')
        if start.tzinfo is None or end <= start:
            return unprocessable('startTime needs a time zone offset and endTime must be after it')
        if body.get('appointmentStatus') not in (None, 'new', 'confirmed', 'cancelled', 'showed', 'noshow', 'invalid'):
            return unprocessable('appointmentStatus is not valid')
        if body.get('assignedUserId') and body['assignedUserId'] not in STAFF:
            return bad_request('Assigned user is not part of this calendar')
        if start not in open_instants(int(start.timestamp() * 1000) - 1, int(start.timestamp() * 1000) + 1):
            return bad_request('The slot you have selected is no longer available.')
        aid = next_id('appt-')
        APPOINTMENTS[aid] = {'id': aid, 'calendarId': CAL, 'contactId': body['contactId'], 'start': start, 'end': end,
                             'status': body.get('appointmentStatus', 'new'), 'assignedUserId': body.get('assignedUserId', ''), 'title': body.get('title', '')}
        return 200, {'id': aid, 'calendarId': CAL, 'locationId': LOC, 'contactId': body['contactId'], 'startTime': body['startTime'],
                     'endTime': body.get('endTime'), 'title': body.get('title', ''), 'appointmentStatus': body.get('appointmentStatus', 'new'),
                     'assignedUserId': body.get('assignedUserId', ''), 'isRecurring': False}

    if method == 'POST' and path == '/conversations/messages':
        if unknown_keys(body, SEND_MESSAGE_KEYS):
            return unknown_keys(body, SEND_MESSAGE_KEYS)
        if body.get('type') not in MESSAGE_TYPES:
            return unprocessable('type must be one of ' + ', '.join(sorted(MESSAGE_TYPES)))
        c = CONTACTS.get(body.get('contactId'))
        if not c:
            return bad_request('Contact not found')
        if body['type'] == 'SMS':
            if not isinstance(body.get('message'), str) or not body['message'].strip():
                return unprocessable('message should not be empty')
            if not c.get('phone'):
                return bad_request('Contact has no phone number')
            if c.get('dnd') or (c.get('dndSettings', {}).get('SMS', {}).get('status') in ('active', 'permanent')):
                return bad_request('Cannot send message as DND is active for SMS.')
            if c['id'] in FAIL['sms_429_once']:
                FAIL['sms_429_once'].discard(c['id'])
                return 429, {'statusCode': 429, 'message': 'Too Many Requests'}
            if c['id'] in FAIL['sms']:
                return bad_request('The phone number is not a valid mobile number.')
            mid = add_message(c['id'], 'outbound', body['message'])
            return 200, {'conversationId': CONVERSATIONS[c['id']], 'messageId': mid, 'status': 'delivered'}

    if method == 'GET' and path == '/conversations/search':
        if q.get('locationId') != LOC:
            return unprocessable('locationId should not be empty')
        rows = [{'id': conv, 'contactId': cid, 'locationId': LOC, 'lastMessageBody': (MESSAGES[conv][-1]['body'] if MESSAGES[conv] else ''),
                 'lastMessageType': 'TYPE_SMS', 'type': 'TYPE_PHONE', 'unreadCount': 0, 'fullName': CONTACTS[cid]['name'],
                 'contactName': CONTACTS[cid]['name'], 'email': CONTACTS[cid]['email'], 'phone': CONTACTS[cid]['phone']}
                for cid, conv in CONVERSATIONS.items() if not q.get('contactId') or q['contactId'] == cid]
        return 200, {'conversations': rows[:int(q.get('limit', 20))], 'total': len(rows)}

    m = re.fullmatch(r'/conversations/([^/]+)/messages', path)
    if method == 'GET' and m:
        conv = m.group(1)
        if conv not in MESSAGES:
            return bad_request('Conversation not found')
        if conv in FAIL['messages']:
            return 500, GHL_500
        limit = int(q.get('limit', 20))
        if not 1 <= limit <= 100:
            return unprocessable('limit must be from 1 to 100')
        newest = list(reversed(MESSAGES[conv]))[:limit]
        return 200, {'messages': {'lastMessageId': newest[-1]['id'] if newest else '', 'nextPage': len(MESSAGES[conv]) > limit, 'messages': newest},
                     'traceId': 'mock-trace'}

    if method == 'GET' and path == '/opportunities/search':
        if q.get('location_id') != LOC:
            return unprocessable('location_id should not be empty')
        if q.get('status', 'all') not in ('open', 'won', 'lost', 'abandoned', 'all'):
            return unprocessable('status must be one of open, won, lost, abandoned, all')
        if not re.fullmatch(r'\d{1,3}', q.get('limit', '20')) or not 1 <= int(q.get('limit', '20')) <= 100:
            return unprocessable('limit must be a number from 1 to 100')
        rows = [o for o in OPPORTUNITIES.values() if (not q.get('contact_id') or o['contactId'] == q['contact_id'])
                and (not q.get('pipeline_id') or o['pipelineId'] == q['pipeline_id']) and q.get('status', 'all') in ('all', o['status'])]
        return 200, {'opportunities': copy.deepcopy(rows[:int(q.get('limit', 20))]),
                     'meta': {'total': len(rows), 'currentPage': 1, 'nextPage': None, 'prevPage': None}, 'aggregations': {}}

    m = re.fullmatch(r'/opportunities/([^/]+)', path)
    if method == 'PUT' and m:
        o = OPPORTUNITIES.get(m.group(1))
        if not o:
            return bad_request('Opportunity not found')
        if o['contactId'] in FAIL['opportunity_put']:
            return 500, GHL_500
        if unknown_keys(body, UPDATE_OPPORTUNITY_KEYS):
            return unknown_keys(body, UPDATE_OPPORTUNITY_KEYS)
        if body.get('pipelineId', o['pipelineId']) != PIPE or body.get('pipelineStageId', o['pipelineStageId']) not in STAGES:
            return bad_request('Pipeline stage does not belong to the pipeline')
        if body.get('status', 'open') not in ('open', 'won', 'lost', 'abandoned'):
            return unprocessable('status is not valid')
        o.update(body)
        return 200, {'opportunity': copy.deepcopy(o)}

    if method == 'POST' and path == '/opportunities/':
        if unknown_keys(body, CREATE_OPPORTUNITY_KEYS):
            return unknown_keys(body, CREATE_OPPORTUNITY_KEYS)
        missing = [k for k in ('pipelineId', 'locationId', 'name', 'status', 'contactId') if not body.get(k)]
        if missing:
            return unprocessable(*[f'{k} should not be empty' for k in missing])
        if body['pipelineId'] != PIPE or body.get('pipelineStageId') not in STAGES:
            return bad_request('Pipeline stage does not belong to the pipeline')
        # GoHighLevel's default: one opportunity per contact per pipeline, whatever its status ("Allow Multiple
        # Opportunities per Contact" is off unless the sub-account turns it on).
        if any(o['contactId'] == body['contactId'] and o['pipelineId'] == body['pipelineId'] for o in OPPORTUNITIES.values()):
            return bad_request('Can not create duplicate opportunity for the contact.')
        oid = next_id('opp-')
        OPPORTUNITIES[oid] = {'id': oid, 'name': body['name'], 'pipelineId': PIPE, 'pipelineStageId': body['pipelineStageId'], 'status': body['status'],
                              'contactId': body['contactId'], 'locationId': LOC, 'monetaryValue': 0, 'updatedAt': iso(datetime.now(timezone.utc))}
        return 201, {'opportunity': copy.deepcopy(OPPORTUNITIES[oid])}

    return 404, {'statusCode': 404, 'message': f'Cannot {method} {path}'}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return self._send(400, {'statusCode': 400, 'message': 'Body is not valid JSON'})
        path = url.path
        # /slack/hook is the workflow's own Slack messages, /slack/errors the error alert workflow
        service = ('claude' if path == '/v1/messages' else 'alert' if path.startswith('/slack/errors')
                   else 'slack' if path.startswith('/slack') else 'ghl')
        with LOCK:
            if service == 'claude':
                status, resp = self._claude(headers, body or {})
            elif service in ('slack', 'alert'):
                ok = method == 'POST' and isinstance(body, dict) and isinstance(body.get('text'), str) and body['text'].strip()
                status, resp = (200, 'ok') if ok else (400, 'invalid_payload')
            else:
                status, resp = ghl(method, path, parse_qs(url.query), headers, body)
            LOG.append({'service': service, 'method': method, 'path': path, 'query': {k: v[0] for k, v in parse_qs(url.query).items()},
                        'headers': headers, 'body': body, 'status': status, 'response': resp, 't': time.time()})
        return self._send(status, resp)

    def _claude(self, headers, body):
        problems = claude_problems(headers, body)
        if problems:
            return 400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '; '.join(problems)}}
        text = body['messages'][-1]['content'][-1]['text']
        m = re.search(r'<newest_message>\n(.*)\n</newest_message>', text, re.S)
        newest = m.group(1) if m else ''
        answer = SCRIPT.get(newest)
        if isinstance(answer, int):
            return answer, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}
        base = {'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': body['model'], 'stop_sequence': None}
        if answer is None or answer == 'text-only':
            return 200, dict(base, content=[{'type': 'text', 'text': 'I am not sure what to say.'}], stop_reason='end_turn',
                             usage={'input_tokens': 900, 'output_tokens': 10})
        if answer == 'max-tokens':
            return 200, dict(base, content=[{'type': 'tool_use', 'id': 'toolu_mock', 'name': body['tools'][0]['name'], 'input': {'intent': 'unclear'}}],
                             stop_reason='max_tokens', usage={'input_tokens': 900, 'output_tokens': 1024})
        tool_input = {'intent': 'unclear', 'slot_choice': 0, 'preferred_days': [], 'preferred_time': 'any', 'reply': '', 'summary': 'Replied', **answer}
        assert set(tool_input) == TOOL_FIELDS
        return 200, dict(base, content=[{'type': 'tool_use', 'id': 'toolu_mock', 'name': body['tools'][0]['name'], 'input': tool_input}],
                         stop_reason='tool_use', usage={'input_tokens': 1300, 'output_tokens': 90})

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')


CONNECTION_ERRORS = []


class MockServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        CONNECTION_ERRORS.append(repr(sys.exc_info()[1]))


# ----------------------------------------------------------------------------------------------------------------------
# n8n helpers
# ----------------------------------------------------------------------------------------------------------------------
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


HOOKS = {'No-Show Webhook': 'noshow', 'Reply Webhook': 'reply', 'Schedule: Every 15 Minutes': 'tick', 'Schedule: Daily Summary': 'summary'}


def test_copy(env, suffix='', real_schedule=False):
    """The workflow with $env values replaced by test values, API hosts pointed at the mocks and test webhook paths.
    Unless real_schedule is set, the two schedule triggers become webhooks, so the test decides when they run."""
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        path = f'{SLUG}-e2e-{HOOKS.get(node["name"], "x")}{suffix}'
        if node['type'] == 'n8n-nodes-base.webhook':
            node['webhookId'] = str(uuid.uuid4())
            node['parameters']['path'] = path
        elif node['type'] == 'n8n-nodes-base.scheduleTrigger':
            if real_schedule:
                if node['name'] == 'Schedule: Every 15 Minutes':
                    node['parameters']['rule'] = {'interval': [{'field': 'minutes', 'minutesInterval': 1}]}   # so the test does not wait
            else:
                node.update({'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'webhookId': str(uuid.uuid4()),
                             'parameters': {'httpMethod': 'POST', 'path': path, 'responseMode': 'onReceived', 'options': {}}})
    return wf


def import_active(env, suffix, error_wid, real_schedule=False):
    """Imports and activates a test copy that reports failures to the temporary error alert workflow."""
    wf = test_copy(env, suffix, real_schedule)
    settings = {**wf.get('settings', {}), 'errorWorkflow': error_wid}
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': settings})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    time.sleep(2)
    return wid


def import_error_workflow():
    """The repo's error alert workflow, posting to the mock Slack. n8n only runs an error workflow that is active."""
    wf = json.loads((WORKFLOW.parent / 'error-alert-slack.json').read_text(encoding='utf-8'))
    for node in wf['nodes']:
        node['parameters'] = json.loads(json.dumps(node.get('parameters', {})).replace('$env.SLACK_WEBHOOK_URL', json.dumps(MOCK + '/slack/errors').replace('"', '\\"')))
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': wf.get('settings', {})})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    return wid


def remove(wid):
    try:
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
    finally:
        api('DELETE', f'/api/v1/workflows/{wid}')


def remove_leftovers():
    """Deletes temporary copies left behind by an earlier, interrupted run of this test (exact name match only)."""
    cursor, found = None, []
    while True:
        page = api('GET', '/api/v1/workflows?limit=250' + (f'&cursor={cursor}' if cursor else ''))
        found += [w['id'] for w in page.get('data', []) if w.get('name') == TEMP_NAME]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    for wid in found:
        remove(wid)
    return len(found)


def alerts_for(ids, timeout=90):
    """Error workflow alerts that name these executions. Alerts arrive after the failed run has finished."""
    end = time.time() + timeout
    while True:
        got = [e for e in LOG if e['service'] == 'alert']
        linked = {i: [e for e in got if re.search(rf'/executions/{i}(?!\d)', e['body']['text'])] for i in ids}
        if all(linked.values()) or time.time() > end:
            return linked, got
        time.sleep(1)


class Runner:
    def __init__(self, wid, suffix=''):
        self.wid, self.suffix, self.sent, self.results, self.mark = wid, suffix, 0, [], 0
        self.expected_failures = {}   # execution id -> what failed, for the error alert check

    def executions(self):
        out, cursor = [], None
        while True:
            page = api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250' + (f'&cursor={cursor}' if cursor else ''))
            out += page.get('data', [])
            cursor = page.get('nextCursor')
            if not cursor:
                return out

    def wait(self, timeout=120):
        """Waits until every webhook call sent so far has a finished run, and returns the newest one."""
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.executions() if e.get('mode') == 'webhook' and e.get('stoppedAt')]
            if len(done) >= self.sent:
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def post(self, which, payload, headers=None, form=False):
        """POST to one of the webhooks. Returns (status, response body, finished execution)."""
        self.mark = len(LOG)
        if form:
            data, ctype = urllib.parse.urlencode(payload).encode(), 'application/x-www-form-urlencoded'
        else:
            data, ctype = json.dumps(payload).encode(), 'application/json'
        req = urllib.request.Request(f'{N8N}/webhook/{SLUG}-e2e-{which}{self.suffix}', method='POST', data=data,
                                     headers={'Content-Type': ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        self.sent += 1
        return status, text, self.wait()

    def stopped_at(self, ex):
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def expect_failure(self, ex, what):
        self.expected_failures[ex['id']] = what

    def new(self, service=None, method=None, path=None, contact=None):
        """Requests to the mocks since the last webhook was sent. Error workflow alerts are left out (they arrive
        late, after the run that failed) and are checked on their own with alerts_for()."""
        out = []
        for e in LOG[self.mark:]:
            if e['service'] == 'alert' and service != 'alert':
                continue
            if service and e['service'] != service or method and e['method'] != method:
                continue
            if path and not re.fullmatch(path, e['path']):
                continue
            if contact and contact not in json.dumps([e['path'], e['body'], e['query']]):
                continue
            out.append(e)
        return out

    def check(self, scenario, label_text, ok, detail=''):
        self.results.append((scenario, label_text, bool(ok), detail))


def sms(entries):
    return [e for e in entries if e['service'] == 'ghl' and e['path'] == '/conversations/messages' and e['method'] == 'POST']


def delivered(entries):
    return [e for e in sms(entries) if e['status'] == 200]


def tag_calls(entries, cid=None, method='POST'):
    return [e for e in entries if e['service'] == 'ghl' and e['method'] == method and re.fullmatch(r'/contacts/[^/]+/tags', e['path'])
            and (cid is None or f'/contacts/{cid}/' in e['path'])]


def slack_texts(r):
    return [e['body']['text'] for e in r.new('slack')]


AUTH = {'Authorization': f'Bearer {SECRET}'}


def no_show_payload(cid):
    """The Custom Webhook body from ghl/native-workflows.md (workflow 2): event, contact id and location id."""
    return {'event': 'no_show', 'contactId': cid, 'locationId': LOC}


def standard_no_show_payload(cid, aid):
    """What the free Webhook action sends for an Appointment Status trigger, per HighLevel's published schema: contact
    fields, a "calendar" object with the appointment id, location and workflow, plus the custom data item "secret"."""
    c, a = CONTACTS[cid], APPOINTMENTS[aid]
    fmt = lambda t: t.astimezone(CLINIC).strftime('%Y-%m-%dT%H:%M:%S')
    return {'contact_id': cid, 'first_name': c['firstName'], 'last_name': c['lastName'], 'full_name': c['name'], 'email': c['email'],
            'phone': c['phone'], 'tags': ','.join(c['tags']), 'timezone': c['timezone'],
            'calendar': {'id': a['calendarId'], 'calendarName': 'Physio visits', 'title': a['title'], 'selectedTimezone': IN_TZ,
                         'appointmentId': aid, 'startTime': fmt(a['start']), 'endTime': fmt(a['end']), 'status': 'noshow', 'appoinmentStatus': 'noshow'},
            'location': {'id': LOC, 'name': 'Harbor Physio Clinic'}, 'workflow': {'id': 'wf-mock-2', 'name': 'No-show to rebooking agent'},
            'customData': {'secret': SECRET}}


def reply_payload(cid, text):
    """The Custom Webhook body from ghl/native-workflows.md (workflow 3)."""
    return {'event': 'reply', 'contactId': cid, 'locationId': LOC, 'body': text, 'messageType': 'SMS', 'direction': 'inbound'}


def no_show(r, cid, shape='custom', aid=None, headers=None):
    if shape == 'standard':
        return r.post('noshow', standard_no_show_payload(cid, aid), {})
    return r.post('noshow', no_show_payload(cid), AUTH if headers is None else headers, form=(shape == 'form'))


def says(r, cid, text, form=False, duplicate=False, stored=True):
    """The patient texts back: GoHighLevel stores the message, then the native workflow calls the reply webhook."""
    if stored and not duplicate:
        add_message(cid, 'inbound', text)
    return r.post('reply', reply_payload(cid, text), AUTH, form=form)


def claude_calls(r):
    return [e for e in r.new('claude') if e['status'] == 200]


def options_from(slot_entry, avoid=(), notbefore=None):
    """The 3 times the workflow should pick from a free slots response: one per day, earliest first."""
    resp = slot_entry['response']
    seen, out = set(), []
    for day in sorted(resp):
        for s in resp[day]['slots']:
            if s in avoid or day in seen:
                continue
            seen.add(day)
            out.append(s)
            break
    return out[:3]


def edit_static(r, change):
    """Changes the workflow's static data through the n8n API (PUT keeps the workflow active). Used to let time pass."""
    wf = api('GET', f'/api/v1/workflows/{r.wid}')
    data = wf.get('staticData') or {}
    data = json.loads(data) if isinstance(data, str) else data
    change(data.setdefault('global', {}))
    settings = {k: v for k, v in (wf.get('settings') or {}).items() if k in ('executionOrder', 'errorWorkflow')}
    api('PUT', f'/api/v1/workflows/{r.wid}', {'name': wf['name'], 'nodes': wf['nodes'], 'connections': wf['connections'],
                                             'settings': settings, 'staticData': data})
    time.sleep(2)


def memory(r):
    static = api('GET', f'/api/v1/workflows/{r.wid}').get('staticData') or {}
    static = json.loads(static) if isinstance(static, str) else static
    return static.get('global') or {}


def case(r, cid):
    return (memory(r).get('cases') or {}).get(cid, {})


def shift(g, cid, hours, fields=('firstTextAt', 'lastTextAt', 'offeredAt', 'openedAt')):
    """Moves a case's timestamps back, as if that many hours had passed."""
    c = g['cases'][cid]
    for f in fields:
        if c.get(f):
            c[f] = iso(datetime.fromisoformat(c[f].replace('Z', '+00:00')) - timedelta(hours=hours))


def set_tz(r, cid, tz):
    """The patient's clock: changes the contact's time zone in GoHighLevel and in the agent's memory, which is how the
    test moves a patient between night and day without waiting."""
    CONTACTS[cid]['timezone'] = tz
    edit_static(r, lambda g: g['cases'][cid].update({'timezone': tz}))


# ----------------------------------------------------------------------------------------------------------------------
# Test data (all made up: 555 numbers, example.com emails, a made up physio clinic)
# ----------------------------------------------------------------------------------------------------------------------
PATIENTS = [  # id, first, last, phone
    ('c-sam', 'Sam', 'Lee', '+15550100101'), ('c-ana', 'Ana', 'Cruz', '+15550100102'), ('c-ben', 'Ben', 'Ford', '+15550100103'),
    ('c-cy', 'Cy', 'Young', '+15550100104'), ('c-dee', 'Dee', 'Ross', '+15550100105'), ('c-eve', 'Eve', 'Lam', '+15550100106'),
    ('c-fay', 'Fay', 'Dunn', '+15550100107'), ('c-gil', 'Gil', 'Shaw', '+15550100108'), ('c-hal', 'Hal', 'Bower', '+15550100109'),
    ('c-ivy', 'Ivy', 'Lund', '+15550100110'), ('c-jon', 'Jon', 'Park', '+15550100111'), ('c-kim', 'Kim', 'Vale', '+15550100112'),
    ('c-lou', 'Lou', 'Hart', '+15550100113'), ('c-max', 'Max', 'Reyes', '+15550100114'), ('c-nell', 'Nell', 'Ito', '+15550100115'),
    ('c-nia', 'Nia', 'Brooks', '+15550100116'), ('c-oli', 'Oli', 'Moss', '+15550100117'), ('c-pia', 'Pia', 'Gray', '+15550100118'),
    ('c-quin', 'Quin', 'Hale', '+15550100119'), ('c-rae', 'Rae', 'Kim', '+15550100120'), ('c-tom', 'Tom', 'Banks', '+15550100121'),
    ('c-uma', 'Uma', 'Shah', '+15550100122'), ('c-vic', 'Vic', 'Ortiz', '+15550100123'), ('c-wes', 'Wes', 'Hart', '+15550100124'),
    ('c-xia', 'Xia', 'Wu', '+15550100125'), ('c-yan', 'Yan', 'Cole', '+15550100126'), ('c-zoe', 'Zoe', 'Lane', '+15550100127'),
    ('c-ida', 'Ida', 'Noor', '+15550100128'), ('c-pat', 'Pat', 'Lee', '+15550100129'), ('c-ruby', 'Ruby', 'Diaz', '+15550100130'),
    ('c-sid', 'Sid', 'Kemp', '+15550100131'), ('c-olga', 'Olga', 'Ames', '+15550100132'), ('c-gus', 'Gus', 'Moor', '+15550100133'),
    ('c-hana', 'Hana', 'Sato', '+15550100134'), ('c-jo', 'Jo', 'Wells', '+15550100135'), ('c-kai', 'Kai', 'Reed', '+15550100136'),
    ('c-mia', 'Mia', 'Long', '+15550100137'), ('c-ned', 'Ned', 'Fox', '+15550100138'),
]


def seed():
    for cid, first, last, phone in PATIENTS:
        add_contact(cid, first, last, phone)
    CONTACTS['c-hal']['timezone'] = OUT_TZ                                                     # 3 AM there
    CONTACTS['c-jo']['timezone'] = OUT_TZ
    CONTACTS['c-hana']['timezone'] = OUT_TZ
    CONTACTS['c-fay']['dndSettings'] = {'SMS': {'status': 'active', 'message': 'asked not to be texted'}}
    CONTACTS['c-ivy']['dndSettings'] = {'Email': {'status': 'active', 'message': 'asked by email'}}
    # Missed visits: today at 9:00 clinic time unless noted (it is about 1 PM at the clinic)
    for cid, *_ in PATIENTS:
        if cid not in ('c-olga',):
            add_appointment('appt-' + cid[2:], cid, clinic_today(9), 30)
    APPOINTMENTS['appt-sam']['assignedUserId'] = 'usr-maya'                                      # with Maya, 45 minutes
    APPOINTMENTS['appt-sam']['end'] = APPOINTMENTS['appt-sam']['start'] + timedelta(minutes=45)
    add_appointment('appt-sam-old', 'c-sam', clinic_today(9, days=-30), 45, 'showed', staff='usr-maya')
    APPOINTMENTS['appt-cy']['status'] = 'showed'                                                 # staff fixed the status
    APPOINTMENTS['appt-dee']['status'] = 'cancelled'
    add_appointment('appt-eve-next', 'c-eve', clinic_today(11, days=2), 30, 'confirmed')         # already rebooked
    APPOINTMENTS['appt-gil']['calendarId'] = OTHER_CAL                                           # another calendar
    APPOINTMENTS['appt-hal']['start'] = clinic_today(17, days=-1)                                # yesterday evening at the clinic
    APPOINTMENTS['appt-hal']['end'] = APPOINTMENTS['appt-hal']['start'] + timedelta(minutes=30)
    add_appointment('appt-mia-early', 'c-mia', clinic_today(8), 30, 'confirmed')                 # an 8:00 visit that morning, never marked showed
    # Opportunities: Sam has an open one, Ben only a won one (a past sale that must never be reopened)
    OPPORTUNITIES['opp-sam'] = {'id': 'opp-sam', 'name': 'Sam Lee', 'pipelineId': PIPE, 'pipelineStageId': 'stage-booked', 'status': 'open',
                                'contactId': 'c-sam', 'locationId': LOC, 'monetaryValue': 90, 'updatedAt': iso(NOW - timedelta(days=3))}
    OPPORTUNITIES['opp-ben-won'] = {'id': 'opp-ben-won', 'name': 'Ben Ford', 'pipelineId': PIPE, 'pipelineStageId': 'stage-booked', 'status': 'won',
                                    'contactId': 'c-ben', 'locationId': LOC, 'monetaryValue': 450, 'updatedAt': iso(NOW - timedelta(days=90))}


def first_text(first, options, when='today at 9:00 AM', policy=''):
    lines = '\n'.join(f'{n + 1}) {label(s)}' for n, s in enumerate(options))
    ask = 'Reply 1, 2 or 3 to book' if len(options) == 3 else ('Reply 1 to book' if len(options) == 1 else f'Reply 1 or {len(options)} to book')
    return (f'Hi {first}, this is Harbor Physio Clinic. We missed you at your physio appointment {when}. No problem, things come up. '
            f'Would one of these times work instead?\n{lines}\n{ask}, or tell us a day that suits you.' + (f'\n{policy}' if policy else '') + '\nReply STOP to opt out.')


def run(r):
    # ================================================================================================ the schedule, idle
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    r.check('Scheduled run with nothing due', 'reads the memory only: no GoHighLevel call, no text', not r.new() and node == 'Find Due Work' and ex['status'] == 'success',
            f'ended at {node}')

    # ================================================================================================ no-show webhook
    status, body, ex = no_show(r, 'c-sam', headers={'Authorization': 'Bearer wrong-secret'})
    node, _ = r.stopped_at(ex)
    r.expect_failure(ex, 'bad secret')
    r.check('No-show webhook, wrong secret', 'refused with 401, nothing loaded or sent', status == 401 and not r.new() and node == 'Stop: Bad Secret', f'{status}, ended at {node}')
    status, body, ex = no_show(r, 'c-sam', headers={})
    node, _ = r.stopped_at(ex)
    r.expect_failure(ex, 'no secret')
    r.check('No-show webhook, no secret', 'refused with 401', status == 401 and not r.new() and node == 'Stop: Bad Secret', f'{status}')

    FAIL['sms_429_once'].add('c-sam')
    status, body, ex = no_show(r, 'c-sam')
    node, _ = r.stopped_at(ex)
    calls = [(e['method'], e['path']) for e in r.new('ghl')]
    appt_reads = r.new('ghl', 'GET', '/calendars/events/appointments/appt-sam')
    contact_appts = r.new('ghl', 'GET', '/contacts/c-sam/appointments')
    r.check('No-show (Custom Webhook, JSON)', 'answers 200 at once, then reads the patient, their appointments and the missed appointment back (Version headers per endpoint)',
            status == 200 and json.loads(body) == {'received': True} and calls[:3] == [('GET', '/contacts/c-sam'), ('GET', '/contacts/c-sam/appointments'),
                                                                                       ('GET', '/calendars/events/appointments/appt-sam')]
            and appt_reads and appt_reads[0]['headers'].get('version') == '2021-04-15' and contact_appts[0]['headers'].get('version') == '2021-07-28',
            ', '.join(f'{m} {p}' for m, p in calls[:3]))
    slots = r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
    q = slots[0]['query'] if slots else {}
    r.check('No-show (Custom Webhook, JSON)', 'free slots for the next 7 days in the patient time zone, with the same team member (userId) as the missed visit',
            len(slots) == 1 and slots[0]['status'] == 200 and q.get('timezone') == IN_TZ and q.get('userId') == 'usr-maya'
            and int(q['endDate']) - int(q['startDate']) == 7 * 86400000, json.dumps(q))
    sam_options = options_from(slots[0]) if slots else []
    texts = sms(r.new())
    expected = first_text('Sam', sam_options)
    r.check('No-show (Custom Webhook, JSON)', 'kind first text with 3 real open times (one per day), no blame and no fees, STOP line',
            texts and texts[-1]['status'] == 200 and texts[-1]['body'] == {'type': 'SMS', 'contactId': 'c-sam', 'message': expected}
            and len(sam_options) == 3 and 'fee' not in expected.lower(), (texts[-1]['body']['message'] if texts else 'no SMS').replace('\n', ' | ')[:160])
    seq = [('tag' if e['path'].endswith('/tags') else 'sms') for e in r.new('ghl', 'POST') if e['path'].endswith('/tags') or e['path'] == '/conversations/messages']
    r.check('No-show (Custom Webhook, JSON)', 'tagged no-show before the text, and a GoHighLevel rate limit (429) on the text is retried: exactly one text',
            seq[:1] == ['tag'] and tag_calls(r.new(), 'c-sam')[0]['body'] == {'tags': [ACTIVE]} and [e['status'] for e in texts] == [429, 200],
            f"order {seq}, SMS statuses {[e['status'] for e in texts]}")
    opp_search, opp_put = r.new('ghl', 'GET', '/opportunities/search'), r.new('ghl', 'PUT', '/opportunities/.*')
    r.check('No-show (Custom Webhook, JSON)', 'the open opportunity moves to the No-show stage',
            len(opp_search) == 1 and opp_search[0]['query'] == {'location_id': LOC, 'contact_id': 'c-sam', 'pipeline_id': PIPE, 'status': 'all', 'limit': '20'}
            and len(opp_put) == 1 and opp_put[0]['path'] == '/opportunities/opp-sam'
            and opp_put[0]['body'] == {'pipelineId': PIPE, 'pipelineStageId': 'stage-no-show', 'status': 'open'} and node == 'No-Show Failed?' and ex['status'] == 'success',
            json.dumps(opp_put[0]['body']) if opp_put else 'no update')

    status, body, ex = no_show(r, 'c-sam')
    node, _ = r.stopped_at(ex)
    r.check('Same no-show delivered twice', 'ignored: no tag, no text, nothing in the daily summary',
            not tag_calls(r.new()) and not sms(r.new()) and node == 'Record Skip' and not r.new('ghl', path=r'/calendars/[^/]+/free-slots'), f'ended at {node}')

    status, body, ex = no_show(r, 'c-ana', shape='standard', aid='appt-ana')
    node, _ = r.stopped_at(ex)
    created = r.new('ghl', 'POST', '/opportunities/')
    ana_slots = r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
    r.check('No-show (standard Webhook action payload)', 'secret from custom data, appointment id taken from calendar.appointmentId, no userId without a team member',
            status == 200 and r.new('ghl', 'GET', '/calendars/events/appointments/appt-ana') and ana_slots and 'userId' not in ana_slots[0]['query']
            and len(delivered(r.new())) == 1, f'{status}, ended at {node}')
    r.check('No-show (standard Webhook action payload)', 'no opportunity yet, so one is created in the No-show stage',
            len(created) == 1 and created[0]['status'] == 201 and created[0]['body'] == {'pipelineId': PIPE, 'locationId': LOC, 'name': 'Ana Cruz (no-show rebooking)',
                                                                                        'pipelineStageId': 'stage-no-show', 'status': 'open', 'contactId': 'c-ana'},
            json.dumps(created[0]['body']) if created else 'not created')

    won_before = copy.deepcopy(OPPORTUNITIES['opp-ben-won'])
    status, body, ex = no_show(r, 'c-ben', shape='form')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'won opportunity')
    created = r.new('ghl', 'POST', '/opportunities/')
    r.check('No-show (Custom Webhook, form fields)', 'form-encoded body read the same way; the patient is texted',
            status == 200 and len(delivered(r.new())) == 1 and tag_calls(r.new(), 'c-ben'), f'{status}')
    r.check('No-show (Custom Webhook, form fields)', 'the only opportunity is won: never reopened, a new one is refused as a duplicate, run flagged with the reason',
            not r.new('ghl', 'PUT', '/opportunities/.*') and len(created) == 1 and created[0]['status'] == 400 and OPPORTUNITIES['opp-ben-won'] == won_before
            and node == 'Stop: No-Show Needs Attention' and 'was not moved to the No-show stage' in error and 'Ben Ford' in error, f'ended at {node}')

    status, body, ex = no_show(r, 'c-cy')
    node, _ = r.stopped_at(ex)
    r.check('No appointment marked no-show any more', 'staff fixed the status during the wait: nothing read further, nothing sent',
            node == 'Record Skip' and not r.new('ghl', 'GET', '/calendars/events/appointments/.*') and not sms(r.new()) and not tag_calls(r.new()), f'ended at {node}')
    status, body, ex = no_show(r, 'c-dee', shape='standard', aid='appt-dee')
    node, _ = r.stopped_at(ex)
    r.check('Appointment cancelled meanwhile', 'read back from GoHighLevel: cancelled, so nothing is sent',
            node == 'Record Skip' and r.new('ghl', 'GET', '/calendars/events/appointments/appt-dee') and not sms(r.new()) and not tag_calls(r.new()), f'ended at {node}')
    status, body, ex = no_show(r, 'c-eve')
    node, _ = r.stopped_at(ex)
    r.check('Already rebooked', 'another appointment after the missed one: nothing sent', node == 'Record Skip' and not sms(r.new()) and not tag_calls(r.new()), f'ended at {node}')
    status, body, ex = no_show(r, 'c-mia')
    node, _ = r.stopped_at(ex)
    r.check('Earlier visit the same morning', 'an 8:00 visit before the missed 9:00 one is not a rebooking, whatever time zone the appointment list uses: Mia is texted',
            len(delivered(r.new())) == 1 and tag_calls(r.new(), 'c-mia') and node == 'No-Show Failed?', f'ended at {node}')
    status, body, ex = no_show(r, 'c-fay')
    node, _ = r.stopped_at(ex)
    r.check('SMS DND', 'not texted, not tagged', node == 'Record Skip' and not sms(r.new()) and not tag_calls(r.new()), f'ended at {node}')
    status, body, ex = no_show(r, 'c-gil', shape='standard', aid='appt-gil')
    node, _ = r.stopped_at(ex)
    r.check('No-show in another calendar', 'ignored', node == 'Record Skip' and not sms(r.new()) and not tag_calls(r.new()), f'ended at {node}')
    other = no_show_payload('c-sam')
    other['locationId'] = 'loc-somebody-else'
    status, body, ex = r.post('noshow', other, AUTH)
    node, _ = r.stopped_at(ex)
    r.check('No-show for another location', 'answered 200 and ignored before any GoHighLevel call',
            status == 200 and json.loads(body).get('ignored', '').startswith('belongs to another location') and not r.new() and node == 'Payload Problem?' and ex['status'] == 'success', f'ended at {node}')
    status, body, ex = r.post('noshow', {'event': 'no_show', 'contactId': '{{contact.id}}', 'locationId': LOC}, AUTH)   # merge field not filled
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'no contact id')
    r.check('No-show webhook without a contact id', 'the right secret but an empty merge field: answered 200, nothing loaded, and the run fails so the error alert fires',
            status == 200 and json.loads(body).get('ignored') == 'no contact id in the payload' and not r.new() and node == 'Stop: Bad Payload'
            and ex['status'] == 'error' and 'no contact id in the payload' in error, f'{status}, ended at {node}')

    status, body, ex = no_show(r, 'c-hal')
    node, _ = r.stopped_at(ex)
    hal_slots = r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
    hal_case = case(r, 'c-hal')
    r.check('No-show at night (3 AM for the patient)', 'tag and opportunity now, the text waits for the morning: no SMS',
            not sms(r.new()) and tag_calls(r.new(), 'c-hal') and r.new('ghl', 'POST', '/opportunities/') and node == 'No-Show Failed?'
            and (hal_case.get('pending') or {}).get('kind') == 'first' and not hal_case.get('firstTextAt'), f'ended at {node}')
    hal_queued = (hal_case.get('pending') or {}).get('text', '')
    hal_zone = timezone(timedelta(minutes=tz_minutes(OUT_TZ)))
    hal_morning = datetime.now(hal_zone).replace(hour=9, minute=0, second=0, microsecond=0)   # 3 AM there now, so the window opens at 9 today
    hal_offered = hal_case.get('offered') or []
    r.check('No-show at night (3 AM for the patient)', 'the waiting text names the day of the missed visit, and every offered time starts at least 2 hours after the window opens',
            hal_queued.startswith('Hi Hal, this is Harbor Physio Clinic. We missed you at your physio appointment on ') and 'today' not in hal_queued.split('\n')[0]
            and len(hal_offered) == 3 and all(datetime.fromisoformat(o['start']) >= hal_morning + timedelta(hours=2) for o in hal_offered),
            hal_queued.split('\n')[0][:110])

    for cid in ('c-jo', 'c-hana'):   # two more night no-shows, played out in the morning below
        status, body, ex = no_show(r, cid)
        node, _ = r.stopped_at(ex)
        if node != 'No-Show Failed?' or sms(r.new()) or (case(r, cid).get('pending') or {}).get('kind') != 'first':
            r.check('Setup', f'night no-show for {cid} waits for the morning', False, f'ended at {node}')

    FAIL['slots'] = True
    status, body, ex = no_show(r, 'c-ivy')
    FAIL['slots'] = False
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'calendar down')
    ivy = delivered(r.new())
    r.check('Calendar down at no-show time', 'retried 3 times, the patient still gets a kind text that asks for a day, run flagged',
            len(r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')) == 3 and len(ivy) == 1
            and ivy[0]['body']['message'] == ('Hi Ivy, this is Harbor Physio Clinic. We missed you at your physio appointment today at 9:00 AM. No problem, things come up. '
                                              'Reply with a day and time that suits you and we will find you a new time.\nReply STOP to opt out.')
            and node == 'Stop: No-Show Needs Attention' and 'Could not load open times for Ivy Lund' in error, f'ended at {node}')

    FAIL['get_appointment'].add('appt-jon')
    status, body, ex = no_show(r, 'c-jon')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'appointment lookup')
    r.check('Appointment cannot be read back', 'retried 3 times, nothing tagged or sent, run fails with the contact id',
            len(r.new('ghl', 'GET', '/calendars/events/appointments/appt-jon')) == 3 and not sms(r.new()) and not tag_calls(r.new())
            and node == 'Stop: No-Show Lookup Failed' and 'c-jon' in error, f'ended at {node}')
    FAIL['tag'].add('c-kim')
    status, body, ex = no_show(r, 'c-kim')
    FAIL['tag'].discard('c-kim')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'tag failed')
    r.check('Tag call fails', 'retried 3 times and no text goes out (replies would not reach the agent without the tag)',
            len(tag_calls(r.new(), 'c-kim')) == 3 and not sms(r.new()) and node == 'Stop: Tag Failed' and 'Kim Vale' in error, f'ended at {node}')
    FAIL['sms'].add('c-lou')
    status, body, ex = no_show(r, 'c-lou')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'no-show SMS rejected')
    r.check('No-show text rejected', 'retried 3 times, run flagged with the exact text to send by hand, no follow-ups planned',
            len(sms(r.new())) == 3 and node == 'Stop: No-Show Needs Attention' and 'was NOT sent' in error and 'Text them by hand: "Hi Lou, this is Harbor Physio Clinic.' in error
            and case(r, 'c-lou').get('status') == 'handed_off', f'ended at {node}')

    # Patients whose answers (or silence) are played below
    for cid in ('c-max', 'c-nell', 'c-nia', 'c-oli', 'c-pia', 'c-quin', 'c-rae', 'c-tom', 'c-uma', 'c-vic', 'c-wes', 'c-xia', 'c-yan',
                'c-zoe', 'c-ida', 'c-pat', 'c-ruby', 'c-sid', 'c-gus', 'c-kai', 'c-ned'):
        status, body, ex = no_show(r, cid)
        node, _ = r.stopped_at(ex)
        if node != 'No-Show Failed?' or len(delivered(r.new())) != 1:
            r.check('Setup', f'no-show text to {cid}', False, f'ended at {node}')

    # ================================================================================================ queued first text
    set_tz(r, 'c-hal', IN_TZ)   # morning for Hal
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    hal = delivered(r.new())
    r.check('The morning after a no-show at night', 'the 15 minute run reloads Hal and sends the waiting text, word for word',
            len(hal) == 1 and hal[0]['body']['contactId'] == 'c-hal' and hal[0]['body']['message'] == hal_queued and r.new('ghl', 'GET', '/contacts/c-hal')
            and not case(r, 'c-hal').get('pending') and case(r, 'c-hal').get('firstTextAt') and node == 'Tick Failed?', f'ended at {node}')
    add_appointment('appt-jo-phone', 'c-jo', clinic_today(11, days=1), 30, 'confirmed')   # Jo called the clinic first thing and rebooked
    set_tz(r, 'c-jo', IN_TZ)
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    jo = case(r, 'c-jo')
    r.check('Booked by phone before the morning text', 'the waiting first text is dropped: closed as rebooked, rebooked tag on, no-show tag off, no text',
            not sms(r.new()) and tag_calls(r.new(), 'c-jo') and tag_calls(r.new(), 'c-jo')[0]['body'] == {'tags': [REBOOKED]} and tag_calls(r.new(), 'c-jo', 'DELETE')
            and jo.get('status') == 'rebooked_elsewhere' and not jo.get('pending') and node == 'Tick Failed?', f"{jo.get('status')}, ended at {node}")
    FAIL['sms'].add('c-hana')
    set_tz(r, 'c-hana', IN_TZ)
    status, body, ex = r.post('tick', {})
    FAIL['sms'].discard('c-hana')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'waiting first text rejected')
    hana = case(r, 'c-hana')
    r.check('Waiting first text rejected in the morning', 'retried 3 times, run flagged with the text to send by hand, and the conversation ends instead of staying open for good',
            len(sms(r.new(contact='c-hana'))) == 3 and node == 'Stop: Tick Needs Attention' and 'waiting text to Hana Sato was NOT sent' in error
            and 'The agent will not text them about this visit.' in error and hana.get('status') == 'handed_off' and not hana.get('pending'), f"{hana.get('status')}, ended at {node}")

    # ================================================================================================ replies
    status, body, ex = r.post('reply', reply_payload('c-sam', 'hello'), {'Authorization': 'Bearer nope'})
    node, _ = r.stopped_at(ex)
    r.expect_failure(ex, 'reply bad secret')
    r.check('Reply webhook, wrong secret', 'refused with 401, nothing loaded or sent', status == 401 and not r.new() and node == 'Stop: Bad Secret', f'{status}')

    status, body, ex = says(r, 'c-sam', '2 works for me')
    node, _ = r.stopped_at(ex)
    calls = claude_calls(r)
    req = calls[0]['body'] if calls else {}
    user_text = '\n'.join(b['text'] for b in req['messages'][0]['content']) if calls else ''
    r.check('Patient picks an offered time', 'Claude request: model, forced tool (strict schema), thinking off; patient data only inside tags in the user turn',
            status == 200 and len(calls) == 1 and req['model'] == 'claude-sonnet-5' and req['tool_choice'] == {'type': 'tool', 'name': 'handle_no_show_reply'}
            and req['thinking'] == {'type': 'disabled'} and req['tools'][0].get('strict') is True and 'Harbor Physio Clinic' in req['system']
            and 'Sam' not in req['system'] and '2 works for me' not in req['system']
            and '<newest_message>\n2 works for me\n</newest_message>' in user_text and 'First name: Sam' in user_text
            and first_text('Sam', sam_options).split('\n')[0] in user_text, f'{len(calls)} call(s)')
    r.check('Patient picks an offered time', 'Claude sees the 3 offered times with the numbers Sam saw',
            calls and all(f'{n + 1}) {label(s)}' in user_text for n, s in enumerate(sam_options)), 'offered times in <case>')
    order = [(e['method'], e['path']) for e in r.new('ghl') if e['method'] != 'GET']
    appts = r.new('ghl', 'POST', '/calendars/events/appointments')
    a = appts[0]['body'] if appts else {}
    start2 = datetime.fromisoformat(sam_options[1]) if len(sam_options) > 1 else None
    r.check('Patient picks an offered time', 'the agent-booked flag tag goes on first, then the booking: option 2, same length (45 min) and team member, confirmed, automations on',
            order[:2] == [('POST', '/contacts/c-sam/tags'), ('POST', '/calendars/events/appointments')] and tag_calls(r.new(), 'c-sam')[0]['body'] == {'tags': [FLAG]}
            and len(appts) == 1 and appts[0]['status'] == 200 and a.get('startTime') == sam_options[1] and a.get('endTime') == (start2 + timedelta(minutes=45)).isoformat()
            and a.get('assignedUserId') == 'usr-maya' and a.get('appointmentStatus') == 'confirmed' and a.get('toNotify') is True
            and a.get('calendarId') == CAL and a.get('locationId') == LOC and a.get('contactId') == 'c-sam' and a.get('title') == 'Rebooked visit: Sam Lee',
            json.dumps(a)[:150])
    opp_put = r.new('ghl', 'PUT', '/opportunities/.*')
    r.check('Patient picks an offered time', 'the same opportunity moves on to the Rebooked stage',
            len(opp_put) == 1 and opp_put[0]['path'] == '/opportunities/opp-sam' and opp_put[0]['body']['pipelineStageId'] == 'stage-rebooked'
            and OPPORTUNITIES['opp-sam']['pipelineStageId'] == 'stage-rebooked', json.dumps(opp_put[0]['body']) if opp_put else 'no update')
    reply, slack = delivered(r.new()), slack_texts(r)
    removed = tag_calls(r.new(), 'c-sam', 'DELETE')
    r.check('Patient picks an offered time', 'confirmation SMS, rebooked tag on, no-show tag off, Slack alert with the new time',
            len(reply) == 1 and reply[0]['body']['message'] == f'You are booked for {label(sam_options[1])}. See you then! You will get the usual reminder before your visit.'
            and tag_calls(r.new(), 'c-sam')[-1]['body'] == {'tags': [REBOOKED]} and len(removed) == 1 and removed[0]['body'] == {'tags': [ACTIVE]}
            and ACTIVE not in CONTACTS['c-sam']['tags'] and len(slack) == 1 and slack[0].startswith('*No-show rebooked*: Sam Lee (+15550100101)')
            and label(sam_options[1]) in slack[0] and node == 'Reply Failed?', reply[0]['body']['message'] if reply else 'no SMS')

    status, body, ex = says(r, 'c-sam', 'thanks!')
    node, _ = r.stopped_at(ex)
    r.check('Rebooked patient writes again', 'the agent stays quiet: no Claude call, no SMS, no tags', not r.new('claude') and not sms(r.new()) and not tag_calls(r.new())
            and node == 'Route Reply', f'ended at {node}')

    status, body, ex = says(r, 'c-ana', f'Do you have anything {ASKED_DAY} evening?')
    node, _ = r.stopped_at(ex)
    slots = r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
    resp = slots[0]['response'] if slots else {}
    thursday = [s for day in sorted(resp) for s in resp[day]['slots'] if datetime.fromisoformat(s).strftime('%A') == ASKED_DAY and datetime.fromisoformat(s).hour >= 17]
    reply = delivered(r.new())
    r.check('Patient wants another time', f'real open times filtered to what she asked for ({ASKED_DAY} evening), never the times she already saw',
            len(thursday) == 1 and len(reply) == 1 and reply[0]['body']['message'] == f'Sure, here is what we have:\n1) {label(thursday[0])}\nReply 1 to book.'
            and node == 'Reply Failed?', reply[0]['body']['message'].replace('\n', ' | ') if reply else 'no SMS')
    status, body, ex = says(r, 'c-ana', f'Do you have anything {ASKED_DAY} evening?', duplicate=True)
    node, _ = r.stopped_at(ex)
    r.check('Same reply delivered twice', 'second copy ignored: no Claude call, no SMS', not r.new('claude') and not sms(r.new()) and node == 'Route Reply', f'ended at {node}')
    status, body, ex = says(r, 'c-ana', '1')
    node, _ = r.stopped_at(ex)
    appts = r.new('ghl', 'POST', '/calendars/events/appointments')
    ana_opp = [o for o in OPPORTUNITIES.values() if o['contactId'] == 'c-ana']
    r.check('Patient wants another time', f'"1" now books the {ASKED_DAY} evening time; the opportunity created at no-show time moves to Rebooked',
            len(appts) == 1 and appts[0]['body']['startTime'] == (thursday[0] if thursday else '') and 'assignedUserId' not in appts[0]['body']
            and len(ana_opp) == 1 and ana_opp[0]['pipelineStageId'] == 'stage-rebooked', appts[0]['body']['startTime'] if appts else 'no booking')

    status, body, ex = says(r, 'c-ben', 'Can someone call me? I have a question about my knee', form=True)
    node, _ = r.stopped_at(ex)
    reply, slack = delivered(r.new()), slack_texts(r)
    r.check('Patient wants a call (form-encoded reply)', 'Claude does not answer the question: short reply, needs-human tag, no-show tag off, Slack alert',
            len(reply) == 1 and reply[0]['body']['message'] == SCRIPT['Can someone call me? I have a question about my knee']['reply']
            and tag_calls(r.new(), 'c-ben')[0]['body'] == {'tags': [HUMAN]} and tag_calls(r.new(), 'c-ben', 'DELETE')
            and len(slack) == 1 and slack[0].startswith('*No-show patient needs a person*: Ben Ford') and 'Wants a call about their knee' in slack[0], slack[0].splitlines()[0] if slack else 'no alert')
    add_appointment('appt-ben-2', 'c-ben', clinic_today(11), 30)    # Ben misses another visit while a person looks after him
    status, body, ex = no_show(r, 'c-ben')
    node, _ = r.stopped_at(ex)
    r.check('Another no-show while a person looks after the patient', 'needs-human tag on, so no text (the agent would ignore his replies); counted in the daily summary',
            node == 'Record Skip' and not sms(r.new()) and not tag_calls(r.new()) and r.new('ghl', 'GET', '/calendars/events/appointments/appt-ben-2'), f'ended at {node}')
    add_appointment('appt-kai-phone', 'c-kai', clinic_today(10, days=2), 30, 'confirmed')   # Kai rebooked by phone, then answers the text
    status, body, ex = says(r, 'c-kai', '2')
    node, _ = r.stopped_at(ex)
    slack = slack_texts(r)
    r.check('Patient answers after booking by phone', 'no Claude call, no second booking, no text; closed as rebooked, no-show tag off, Slack note for a person',
            not r.new('claude') and not r.new('ghl', 'POST', '/calendars/events/appointments') and not sms(r.new())
            and tag_calls(r.new(), 'c-kai') and tag_calls(r.new(), 'c-kai')[0]['body'] == {'tags': [REBOOKED]} and tag_calls(r.new(), 'c-kai', 'DELETE')
            and case(r, 'c-kai').get('status') == 'rebooked_elsewhere' and len(slack) == 1 and slack[0].startswith('*No-show patient already rebooked*: Kai Reed')
            and node == 'Reply Failed?', slack[0].splitlines()[0] if slack else f'ended at {node}')

    status, body, ex = says(r, 'c-hal', "No thanks, I'll book when I'm back from my trip")
    node, _ = r.stopped_at(ex)
    reply = delivered(r.new())
    r.check('Patient does not want to rebook', 'kind close: reply, declined tag, no-show tag off, no Slack alert, no follow-ups later',
            len(reply) == 1 and reply[0]['body']['message'] == SCRIPT["No thanks, I'll book when I'm back from my trip"]['reply']
            and tag_calls(r.new(), 'c-hal')[0]['body'] == {'tags': [DECLINED]} and tag_calls(r.new(), 'c-hal', 'DELETE') and not slack_texts(r)
            and case(r, 'c-hal').get('status') == 'declined', reply[0]['body']['message'][:60] if reply else 'no SMS')

    status, body, ex = says(r, 'c-ivy', 'STOP')
    node, _ = r.stopped_at(ex)
    puts = r.new('ghl', 'PUT', '/contacts/c-ivy')
    r.check('Patient texts STOP', 'no Claude call, no reply; SMS DND set and her Email DND kept; opted-out tag, no-show tag off',
            not r.new('claude') and not sms(r.new()) and len(puts) == 1 and puts[0]['status'] == 200
            and puts[0]['body'] == {'dndSettings': {'Email': {'status': 'active', 'message': 'asked by email'},
                                                    'SMS': {'status': 'active', 'message': 'Opted out by text reply to the no-show rebooking agent'}}}
            and tag_calls(r.new(), 'c-ivy')[0]['body'] == {'tags': [STOPPED]} and tag_calls(r.new(), 'c-ivy', 'DELETE'), json.dumps(puts[0]['body'])[:120] if puts else 'no DND update')
    status, body, ex = says(r, 'c-nia', 'please stop texting me')
    r.check('Opt out in plain words', 'Claude reads it as stop: SMS DND set, opted-out tag, no reply',
            len(claude_calls(r)) == 1 and not sms(r.new()) and len(r.new('ghl', 'PUT', '/contacts/c-nia')) == 1
            and CONTACTS['c-nia']['dndSettings'].get('SMS', {}).get('status') == 'active' and tag_calls(r.new(), 'c-nia')[0]['body'] == {'tags': [STOPPED]}, '')

    status, body, ex = says(r, 'c-oli', 'Is there parking near you?')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'Claude 529')
    reply = delivered(r.new())
    r.check('Claude is down (529)', 'retried 3 times, holding reply, needs-human tag, run flagged with their words',
            len(r.new('claude')) == 3 and len(reply) == 1 and reply[0]['body']['message'] == HOLD.format(first='Oli')
            and tag_calls(r.new(), 'c-oli')[0]['body'] == {'tags': [HUMAN]} and node == 'Stop: Reply Needs Attention'
            and 'They got a holding reply.' in error and 'They wrote "Is there parking near you?"' in error, f"{len(r.new('claude'))} attempts, ended at {node}")
    status, body, ex = says(r, 'c-pia', 'Please stop sending these')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'Claude 529, stop hint')
    r.check('Claude is down and the message may be an opt out', 'no holding reply to someone who may have asked to stop; needs-human tag; run flagged',
            len(r.new('claude')) == 3 and not sms(r.new()) and tag_calls(r.new(), 'c-pia')[0]['body'] == {'tags': [HUMAN]}
            and node == 'Stop: Reply Needs Attention' and 'Nothing was sent, because the message may ask us to stop texting.' in error, f'ended at {node}')
    status, body, ex = says(r, 'c-quin', 'hmm ok??')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'no tool call')
    r.check('Claude answers without the tool call', 'treated as a failure: holding reply, needs-human tag, run flagged',
            len(r.new('claude')) == 1 and len(delivered(r.new())) == 1 and node == 'Stop: Reply Needs Attention' and 'no usable decision' in error, f'ended at {node}')
    status, body, ex = says(r, 'c-rae', 'I need to think about it and maybe')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'max_tokens')
    r.check('Claude stops at max_tokens', 'treated as a failure even though a tool call came back', node == 'Stop: Reply Needs Attention' and 'max_tokens' in error
            and HUMAN in CONTACTS['c-rae']['tags'], f'ended at {node}')

    tom_third = ((case(r, 'c-tom').get('offered') or [{}] * 3)[2:3] or [{}])[0].get('start', '')
    if tom_third:
        TAKEN.add(datetime.fromisoformat(tom_third))     # the last places at that time go in the meantime
    status, body, ex = says(r, 'c-tom', '3')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'slot taken')
    reply = delivered(r.new())
    r.check('Picked time was just taken', 'GoHighLevel refuses the booking: honest SMS, flag tag removed again, needs-human tag, run flagged',
            len(r.new('ghl', 'POST', '/calendars/events/appointments')) == 1 and len(reply) == 1
            and reply[0]['body']['message'] == 'Sorry Tom, I could not lock in that time. Someone from the clinic will text you shortly to confirm one.'
            and tag_calls(r.new(), 'c-tom', 'DELETE')[0]['body'] == {'tags': [FLAG]} and FLAG not in CONTACTS['c-tom']['tags']
            and not r.new('ghl', path='/opportunities/.*') and HUMAN in CONTACTS['c-tom']['tags'] and node == 'Stop: Reply Needs Attention' and 'Booking failed' in error,
            f'ended at {node}')

    uma_before = case(r, 'c-uma').get('offered') or []
    edit_static(r, lambda g: g['cases']['c-uma']['offered'][0].update({'start': iso(datetime.now(timezone.utc) - timedelta(hours=2))}))
    status, body, ex = says(r, 'c-uma', 'the first one')
    node, _ = r.stopped_at(ex)
    calls, reply = claude_calls(r), delivered(r.new())
    uma_text = '\n'.join(b['text'] for b in calls[0]['body']['messages'][0]['content']) if calls else ''
    new_times = case(r, 'c-uma').get('offered') or []
    r.check('Patient picks a time that has passed', 'Claude sees it marked as passed; nothing booked; 3 new times she has not seen yet',
            len(uma_before) == 3 and f'1) {uma_before[0]["label"]} (has passed, no longer available)' in uma_text
            and not r.new('ghl', 'POST', '/calendars/events/appointments') and len(new_times) == 3
            and not {t['start'] for t in new_times} & {t['start'] for t in uma_before} and len(reply) == 1
            and reply[0]['body']['message'].startswith('Sorry, that time has already passed. Here are the next open times:\n1) '), reply[0]['body']['message'].split('\n')[0] if reply else 'no SMS')

    FAIL['opportunity_put'].add('c-vic')
    status, body, ex = says(r, 'c-vic', '1')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'opportunity update')
    r.check('Opportunity update fails after booking', 'retried 3 times; the patient is still confirmed and tagged; run flagged, no duplicate Slack alert',
            len(r.new('ghl', 'PUT', '/opportunities/.*')) == 3 and len(delivered(r.new())) == 1 and delivered(r.new())[0]['body']['message'].startswith('You are booked for ')
            and REBOOKED in CONTACTS['c-vic']['tags'] and not slack_texts(r) and node == 'Stop: Reply Needs Attention' and 'was not moved to the Rebooked stage' in error, f'ended at {node}')

    FAIL['put_contact'].add('c-wes')
    status, body, ex = says(r, 'c-wes', 'Stop')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'DND update')
    r.check('DND update fails after STOP', 'retried 3 times, nothing texted, URGENT alert to set DND by hand',
            len(r.new('ghl', 'PUT', '/contacts/c-wes')) == 3 and not sms(r.new()) and node == 'Stop: Reply Needs Attention'
            and 'URGENT: Wes Hart (+15550100124) asked to stop texts' in error, f'ended at {node}')

    FAIL['sms'].add('c-xia')
    status, body, ex = says(r, 'c-xia', 'what?')
    FAIL['sms'].discard('c-xia')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'reply SMS rejected')
    r.check('Reply SMS rejected', 'retried 3 times, run flagged with the text to send by hand',
            len(sms(r.new())) == 3 and node == 'Stop: Reply Needs Attention' and 'was NOT sent' in error and SCRIPT['what?']['reply'] in error, f'ended at {node}')

    for text in ('what?', 'Is the clinic open on Saturdays?', 'what?'):
        says(r, 'c-yan', text)
    status, body, ex = says(r, 'c-yan', 'Ok and one more thing <!here>')
    node, _ = r.stopped_at(ex)
    reply, slack = delivered(r.new()), slack_texts(r)
    r.check('Reply limit (MAX_AI_REPLIES=3)', 'the fourth message goes to a person: no Claude call, holding reply, needs-human tag, Slack alert',
            not r.new('claude') and len(reply) == 1 and reply[0]['body']['message'] == HOLD.format(first='Yan')
            and tag_calls(r.new(), 'c-yan')[0]['body'] == {'tags': [HUMAN]} and len(slack) == 1 and 'already sent 3 replies' in slack[0], slack[0].splitlines()[2] if slack else 'no alert')
    r.check('Reply limit (MAX_AI_REPLIES=3)', "the patient's words are escaped for Slack, so <!here> cannot ping the channel",
            len(slack) == 1 and 'They said: "Ok and one more thing &lt;!here&gt;"' in slack[0] and '<!' not in slack[0], slack[0].splitlines()[1] if slack else '')

    set_tz(r, 'c-zoe', OUT_TZ)   # 3 AM for Zoe
    zoe_options = case(r, 'c-zoe').get('offered') or []
    status, body, ex = says(r, 'c-zoe', '2')
    node, _ = r.stopped_at(ex)
    appts = r.new('ghl', 'POST', '/calendars/events/appointments')
    r.check('Patient answers at 3 AM', 'the booking is made at once (the slot is held), the confirmation text waits for the morning',
            len(appts) == 1 and appts[0]['status'] == 200 and len(zoe_options) == 3 and appts[0]['body']['startTime'] == zoe_options[1]['start'] and not sms(r.new())
            and (case(r, 'c-zoe').get('pending') or {}).get('text', '').startswith('You are booked for ') and REBOOKED in CONTACTS['c-zoe']['tags'], f'ended at {node}')
    set_tz(r, 'c-zoe', IN_TZ)
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    zoe = delivered(r.new())
    r.check('Patient answers at 3 AM', 'the morning run sends the confirmation, and nothing else',
            len(zoe) == 1 and zoe[0]['body']['contactId'] == 'c-zoe' and zoe[0]['body']['message'] == f'You are booked for {zoe_options[1]["label"]}. See you then! You will get the usual reminder before your visit.'
            and not case(r, 'c-zoe').get('pending') and node == 'Tick Failed?', f'ended at {node}')

    status, body, ex = says(r, 'c-olga', 'Hi, is this Harbor Physio?')
    node, _ = r.stopped_at(ex)
    r.check('Reply from a contact with no rebooking conversation', 'ignored: no Claude call, no SMS, no tags', not r.new('claude') and not sms(r.new())
            and not tag_calls(r.new()) and node == 'Route Reply', f'ended at {node}')
    email = reply_payload('c-max', 'Sent from my email')
    email['messageType'] = 'Email'
    status, body, ex = r.post('reply', email, AUTH)
    node, _ = r.stopped_at(ex)
    r.check('Email reply', 'not an SMS: ignored before any GoHighLevel call, and the run does not fail', not r.new() and node == 'Payload Problem?' and ex['status'] == 'success', f'ended at {node}')
    ghost = reply_payload('c-ghost', 'Is this still available? <!channel>')
    status, body, ex = r.post('reply', ghost, AUTH)
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'contact lookup')
    r.check('Contact cannot be loaded', 'retried 3 times, run fails with the contact id and their message (escaped for Slack), nothing sent',
            len(r.new('ghl', 'GET', '/contacts/c-ghost')) == 3 and not r.new('claude') and not sms(r.new()) and node == 'Stop: Reply Lookup Failed'
            and 'c-ghost' in error and '&lt;!channel&gt;' in error and '<!' not in error, f'ended at {node}')
    FAIL['messages'].add(CONVERSATIONS['c-ida'])
    status, body, ex = says(r, 'c-ida', 'what?')
    FAIL['messages'].discard(CONVERSATIONS['c-ida'])
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'history')
    calls = claude_calls(r)
    r.check('Conversation history cannot be loaded', 'retried 3 times, the patient still gets an answer, and the run is flagged',
            len(r.new('ghl', 'GET', '/conversations/conv-c-ida/messages')) == 3 and len(calls) == 1
            and '<conversation>\n(no earlier messages found)\n</conversation>' in calls[0]['body']['messages'][0]['content'][1]['text']
            and len(delivered(r.new())) == 1 and node == 'Stop: Reply Needs Attention' and 'without the conversation history' in error, f'ended at {node}')

    # ================================================================================================ follow-ups
    for cid in ('c-max', 'c-nell', 'c-pat', 'c-ruby', 'c-sid'):
        edit_static(r, lambda g, cid=cid: shift(g, cid, 25))
    max_first = case(r, 'c-max').get('offered') or []
    set_tz(r, 'c-nell', OUT_TZ)                                                  # 3 AM for Nell
    add_appointment('appt-pat-phone', 'c-pat', clinic_today(10, days=3), 30, 'confirmed')   # Pat booked by phone
    CONTACTS['c-ruby']['dndSettings'] = {'SMS': {'status': 'active', 'message': 'STOP received'}}  # Ruby replied STOP to another workflow
    FAIL['sms'].add('c-sid')
    status, body, ex = r.post('tick', {})
    FAIL['sms'].discard('c-sid')
    node, error = r.stopped_at(ex)
    r.expect_failure(ex, 'follow-up SMS rejected')
    texts = delivered(r.new())
    max_text = [e['body']['message'] for e in texts if e['body']['contactId'] == 'c-max']
    max_new = case(r, 'c-max').get('offered') or []
    r.check('Follow-up 1 (24 hours, no answer)', 'Max is reloaded and gets a gentle follow-up with fresh times he has not seen',
            len(max_text) == 1 and max_text[0].startswith('Hi Max, just checking in from Harbor Physio Clinic. Would you like to rebook your physio appointment? These times are open:\n1) ')
            and max_text[0].endswith(', or tell us a day that suits you.\nReply STOP to opt out.') and len(max_new) == 3
            and not {t['start'] for t in max_new} & {t['start'] for t in max_first} and case(r, 'c-max').get('followUps') == 1
            and r.new('ghl', 'GET', '/contacts/c-max/appointments'), max_text[0].split('\n')[0][:90] if max_text else 'no follow-up')
    r.check('Follow-up 1 (24 hours, no answer)', 'patients who answered or are closed get nothing; Nell (3 AM now) waits',
            sorted(e['body']['contactId'] for e in texts) == ['c-max'] and not sms(r.new(contact='c-nell')) and not r.new('ghl', 'GET', '/contacts/c-nell'), str(sorted(e['body']['contactId'] for e in sms(r.new()))))
    r.check('Follow-up 1 (24 hours, no answer)', 'Pat booked by phone meanwhile: closed as rebooked, no-show tag off, no text',
            not sms(r.new(contact='c-pat')) and tag_calls(r.new(), 'c-pat')[0]['body'] == {'tags': [REBOOKED]} and tag_calls(r.new(), 'c-pat', 'DELETE')
            and case(r, 'c-pat').get('status') == 'rebooked_elsewhere', case(r, 'c-pat').get('status', ''))
    r.check('Follow-up 1 (24 hours, no answer)', 'Ruby has SMS DND now: closed as opted out, no text',
            not sms(r.new(contact='c-ruby')) and tag_calls(r.new(), 'c-ruby')[0]['body'] == {'tags': [STOPPED]} and case(r, 'c-ruby').get('status') == 'opted_out', '')
    r.check('Follow-up 1 (24 hours, no answer)', "Sid's follow-up is rejected: the others still go out, and the run fails once at the end with the text to send by hand",
            len(sms(r.new(contact='c-sid'))) == 3 and node == 'Stop: Tick Needs Attention' and 'follow-up 1 to Sid Kemp was NOT sent' in error
            and 'Text them by hand' in error and case(r, 'c-sid').get('followUps') == 1, f'ended at {node}')
    set_tz(r, 'c-nell', IN_TZ)
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    r.check('Follow-up 1 (24 hours, no answer)', "Nell's follow-up goes out on the next run inside her texting window",
            [e['body']['contactId'] for e in delivered(r.new())] == ['c-nell'] and node == 'Tick Failed?', f'ended at {node}')

    edit_static(r, lambda g: shift(g, 'c-max', 48))      # 73 hours after the first text
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    texts = delivered(r.new())
    r.check('Follow-up 2 (72 hours, still no answer)', 'the last, gentle follow-up says so; nobody else is texted',
            [e['body']['contactId'] for e in texts] == ['c-max'] and texts[0]['body']['message'].startswith(
                'Hi Max, one last note from Harbor Physio Clinic: whenever you are ready to rebook your physio appointment, just reply here. These times are open:\n')
            and texts[0]['body']['message'].endswith('This is our last reminder about it.\nReply STOP to opt out.') and case(r, 'c-max').get('followUps') == 2,
            texts[0]['body']['message'].split('\n')[0][:90] if texts else 'no text')
    edit_static(r, lambda g: shift(g, 'c-max', 48))      # 121 hours
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    r.check('No answer after both follow-ups (120 hours)', 'closed as no reply: no text, no-reply tag on, no-show tag off',
            not sms(r.new()) and tag_calls(r.new(), 'c-max')[0]['body'] == {'tags': [NO_REPLY]} and tag_calls(r.new(), 'c-max', 'DELETE')
            and ACTIVE not in CONTACTS['c-max']['tags'] and case(r, 'c-max').get('status') == 'no_reply' and node == 'Tick Failed?', f'ended at {node}')

    edit_static(r, lambda g: shift(g, 'c-gus', 80))     # n8n was down for days: Gus's first follow-up is 56 hours late
    status, body, ex = r.post('tick', {})
    first_late = [e['body']['contactId'] for e in delivered(r.new())]
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    r.check('Follow-up after a long outage', 'the late first follow-up goes out, and the second keeps its own 48 hours instead of following 15 minutes later',
            first_late == ['c-gus'] and not sms(r.new()) and case(r, 'c-gus').get('followUps') == 1 and node == 'Find Due Work', f'{first_late}, then ended at {node}')

    del CONTACTS['c-ned']                                 # merged into another contact in GoHighLevel, so this id is gone
    edit_static(r, lambda g: shift(g, 'c-ned', 25))
    errors = []
    for n in range(3):
        status, body, ex = r.post('tick', {})
        node, error = r.stopped_at(ex)
        r.expect_failure(ex, f'contact gone {n + 1}')
        errors.append(error)
    status, body, ex = r.post('tick', {})
    node, _ = r.stopped_at(ex)
    r.check('Contact deleted or merged in GoHighLevel', 'reported and retried, then after 3 misses in a row the agent lets go instead of failing every 15 minutes for good',
            'tries again in 15 minutes' in errors[0] and 'tries again in 15 minutes' in errors[1] and 'did not find this contact in 3 runs in a row' in errors[2]
            and case(r, 'c-ned').get('status') == 'handed_off' and not r.new('ghl', path='/contacts/c-ned') and node == 'Find Due Work', f'4th run ended at {node}')

    # ================================================================================================ daily summary
    status, body, ex = r.post('summary', {})
    node, _ = r.stopped_at(ex)
    summary = slack_texts(r)
    text = summary[0] if summary else ''
    r.check('Daily summary', 'one Slack message with the counts of the last 24 hours',
            len(summary) == 1 and text.startswith('*No-show rebooking, last 24 hours* (Harbor Physio Clinic)')
            and 'No-shows: 35 (26 texted at once, 3 held for the morning, 1 text failed, 5 not texted: ' in text
            and '1 a person is looking after them (needs-human tag)' in text
            and 'Rebooked by text: 4 (Sam Lee, Ana Cruz, Vic Ortiz, Zoe Lane). Booked another way: 3' in text
            and 'Waiting for an answer: 7 (follow-ups sent: 4)' in text
            and 'Did not want to rebook: 1. No reply after follow-ups: 1. Opted out: 4.' in text and 'Handed to a person: 8 (' in text,
            ' | '.join(text.split('\n')[1:3])[:160])

    # ================================================================================================ error alerts
    failed = sorted((e['id'] for e in r.executions() if e.get('status') == 'error'), key=int)
    linked, got = alerts_for(failed)
    counts = [len(v) for v in linked.values()]
    r.check('Error alert workflow', 'every failed run raised exactly one Slack alert with its execution link, and only the runs that should fail failed',
            set(failed) == set(r.expected_failures) and counts == [1] * len(failed) and len(got) == len(failed),
            f'{len(failed)} failed runs, {len(r.expected_failures)} expected, {len(got)} alerts')
    tick_alert = [e['body']['text'] for e in got if 'Stop: Tick Needs Attention' in e['body']['text'] and 'Sid Kemp' in e['body']['text']]
    r.check('Error alert workflow', 'the alert carries the node and the whole reason',
            tick_alert and f'*{TEMP_NAME}* failed at *Stop: Tick Needs Attention*' in tick_alert[0] and 'Sid Kemp' in tick_alert[0], tick_alert[0].splitlines()[0] if tick_alert else 'no alert')

    # ---------------------------------------------------------------- what n8n saved with each run
    leaks, hidden, total = [], True, 0
    for ex in r.executions():
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        run_data = (data.get('resultData') or {}).get('runData') or {}
        total += 1
        for node, tasks in run_data.items():
            blob = json.dumps(tasks)
            if GHL_KEY in blob or ENV['ANTHROPIC_API_KEY'] in blob:
                leaks.append(f'API key in {node}')
            if node in ('No-Show Webhook', 'Reply Webhook'):
                hidden &= ('Bearer ' + SECRET) not in blob
                body = (tasks[0]['data']['main'][0][0]['json'].get('body') or {}) if tasks and tasks[0].get('data') else {}
                if SECRET in blob and not (isinstance(body, dict) and (body.get('customData') or {}).get('secret') == SECRET):
                    leaks.append(f'shared secret in {node}')
            elif SECRET in blob:
                leaks.append(f'shared secret in {node}')
    r.check('Saved executions', 'the GoHighLevel and Anthropic keys are in no saved run, and the shared secret only where the free Webhook action puts it (custom data)',
            not leaks, f'{total} runs searched' if not leaks else ', '.join(sorted(set(leaks))[:4]))
    r.check('Saved executions', 'a secret sent as Authorization: Bearer (the Custom Webhook action) is hidden in the saved webhook data', hidden, 'n8n redacts that header')

    static = memory(r)
    r.check('Workflow memory', 'one case per patient who was texted, message ids of handled replies only, no-shows remembered by appointment id',
            'c-olga' not in static.get('cases', {}) and 'appt-sam' in static.get('seenAppointments', {}) and 'appt-cy' not in static.get('seenAppointments', {})
            and case(r, 'c-sam').get('status') == 'rebooked', f"{len(static.get('cases', {}))} cases, {len(static.get('seenMessages', {}))} message ids")


def run_bad_settings(results, error_wid):
    """A copy with broken settings: the no-show webhook must refuse with a 500 that names every problem."""
    example_secret = 'make-up-a-long-random-string'   # the placeholder from .env.example, left unchanged
    env = {**ENV, 'BUSINESS_TZ': 'Mars/Olympus', 'GHL_REBOOKED_STAGE_ID': '', 'FOLLOW_UP_HOURS': '72,24', 'ANTHROPIC_MODEL': 'claude-opus-5-5',
           'GHL_WEBHOOK_SECRET': example_secret}
    wid = import_active(env, '-3', error_wid)
    r = Runner(wid, '-3')
    try:
        status, body, ex = no_show(r, 'c-olga', headers={'Authorization': f'Bearer {example_secret}'})
        node, _ = r.stopped_at(ex)
        try:
            answer = json.loads(body)
        except ValueError:
            answer = body
        problems = ['GHL_REBOOKED_STAGE_ID is empty', 'GHL_WEBHOOK_SECRET must be your own random string of at least 16 characters, not the example value',
                    'BUSINESS_TZ "Mars/Olympus" is not a valid time zone',
                    'FOLLOW_UP_HOURS "72,24" must be empty or up to two increasing numbers of hours, for example 24,72',
                    'ANTHROPIC_MODEL claude-opus-5-5 does not accept the forced tool call with thinking off that replies use (use claude-sonnet-5, claude-opus-5 or claude-haiku-4-5)']
        results.append(('Broken settings', 'the no-show webhook answers 500 with the exact list of problems, no GoHighLevel call, run fails',
                        status == 500 and answer == {'received': False, 'error': 'settings', 'problems': problems} and not r.new()
                        and node == 'Stop: Bad Settings' and ex['status'] == 'error', f'{status}, ended at {node}'))
        linked, _ = alerts_for([ex['id']])
        text = (linked[ex['id']] or [{'body': {'text': ''}}])[0]['body']['text']
        results.append(('Broken settings', 'the error alert names every problem',
                        len(linked[ex['id']]) == 1 and 'Settings are not valid, so nothing was done. ' + '. '.join(problems) + '.' in text,
                        text.splitlines()[1][:90] if text.count('\n') else 'no alert'))
    finally:
        remove(wid)


def run_real_schedule(results, error_wid):
    """A copy with the real schedule trigger (every minute here) and a no-show policy text."""
    policy = 'As a reminder, visits missed without 24 hours notice may be charged per our policy.'
    add_appointment('appt-olga', 'c-olga', clinic_today(9), 30)
    wid = import_active({**ENV, 'NO_SHOW_POLICY_TEXT': policy}, '-2', error_wid, real_schedule=True)
    r = Runner(wid, '-2')
    try:
        status, body, ex = no_show(r, 'c-olga')
        slots = r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
        texts = delivered(r.new())
        results.append(('No-show policy text set by the clinic', 'the first text carries the clinic policy, word for word, before the STOP line',
                        len(texts) == 1 and slots and texts[0]['body']['message'] == first_text('Olga', options_from(slots[0]), policy=policy),
                        (texts[0]['body']['message'].split('\n')[-2] if texts else 'no SMS')[:90]))
        end = time.time() + 150
        timed = []
        while time.time() < end:
            timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
            if timed:
                break
            time.sleep(2)
        node, _ = r.stopped_at(timed[0]) if timed else ('none', '')
        results.append(('Real schedule trigger', 'the 15 minute schedule (every minute in this copy) fires by itself and finds nothing due yet',
                        bool(timed) and timed[0]['status'] == 'success' and node == 'Find Due Work', f'{len(timed)} scheduled run(s), ended at {node}'))
    finally:
        remove(wid)


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    leftovers = remove_leftovers()
    if leftovers:
        print(f'Removed {leftovers} temporary workflow(s) left by an interrupted run.')
    seed()
    server = MockServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    try:
        error_wid = import_error_workflow()
        try:
            wid = import_active(ENV, '', error_wid)
            r = Runner(wid)
            try:
                run(r)
            except Exception as e:   # a crash is reported as a failed check, after the checks that ran before it
                import traceback
                r.check('Test run', 'finished without a crash', False, ''.join(traceback.format_exception_only(type(e), e)).strip()[:300])
                traceback.print_exc()
            finally:
                results = r.results
                remove(wid)
            run_bad_settings(results, error_wid)
            run_real_schedule(results, error_wid)
        finally:
            remove(error_wid)
    finally:
        server.shutdown()
        if os.environ.get('SAVE_REQUESTS'):
            saved = [{k: e[k] for k in ('service', 'method', 'path', 'query', 'body', 'status')} for e in LOG]
            Path(os.environ['SAVE_REQUESTS']).write_text(json.dumps(saved, indent=1, default=str), encoding='utf-8')

    passed = sum(ok for *_, ok, _ in results)
    last = None
    for scenario, label_text, ok, detail in results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label_text}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(results)} checks passed across {len({s for s, *_ in results})} scenarios')
    if CONNECTION_ERRORS:
        # A request that breaks between n8n and the mock never reaches the handler, so an exact request count can be off by one.
        print(f'Note: {len(CONNECTION_ERRORS)} connection(s) to the mock broke before a response: ' + '; '.join(CONNECTION_ERRORS[:5]))
    sys.exit(0 if results and passed == len(results) else 1)


if __name__ == '__main__':
    main()
