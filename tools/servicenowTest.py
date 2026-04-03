import requests

SNOW_URL  = 'ServiceNOw instance URL'
USERNAME  = 'username'
PASSWORD  = 'password'

resp = requests.get(
    f'{SNOW_URL}/api/now/table/incident',
    params={'sysparm_limit': '1',
            'sysparm_fields': 'number'},
    headers={'Accept': 'application/json'},
    auth=(USERNAME, PASSWORD),
    timeout=15
)
print('Status:', resp.status_code)
if resp.status_code == 200:
    print('RESULT: Basic Auth works on corporate SNOW')
    print('MFA does NOT block REST API on your instance')
elif resp.status_code == 401:
    print('RESULT: Basic Auth blocked')
    print('IT has enforced MFA on API calls')
    print('Solution: Use developer instance instead')
elif resp.status_code == 403:
    print('RESULT: Authenticated but no permission')
    print('Ask IT to grant you itil role on the test environment')