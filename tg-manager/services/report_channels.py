"""Проверенные каналы подачи обращений, не список целей массовой рассылки."""
CHECKED_ON = '2026-11-08'
FAQ = 'https://telegram.org/faq'
CHANNELS = (
    dict(key='abuse', label='Telegram: нарушение правил', kind='email',
         address='abuse@telegram.org', categories=(), source=FAQ),
    dict(key='copyright', label='Telegram: авторские права', kind='email',
         address='dmca@telegram.org', categories=('copyright',), source=FAQ),
    dict(key='child', label='Telegram: защита детей', kind='email',
         address='stopCA@telegram.org', categories=('csam',),
         source='https://telegram.org/safety'),
    dict(key='impersonation', label='Telegram: выдают себя за вас', kind='manual',
         address='https://t.me/NoToScam', categories=('scam',), source=FAQ),
    dict(key='intimate', label='Telegram: интимные материалы без согласия',
         kind='manual', address='https://t.me/TIDABot',
         categories=('intimate',), source=FAQ),
    dict(key='privacy', label='Telegram: персональные данные', kind='manual',
         address='https://t.me/EURegulation', categories=('privacy',),
         source='https://telegram.org/privacy'),
    dict(key='search', label='Telegram: незаконные поисковые запросы',
         kind='manual', address='https://t.me/SearchReport', categories=(),
         source='https://telegram.org/safety'),
    dict(key='ncmec', label='NCMEC: эксплуатация детей', kind='manual',
         address='https://report.cybertip.org/', categories=('csam',),
         source='https://report.cybertip.org/faqs'),
    dict(key='iwf', label='IWF: материалы насилия над детьми', kind='manual',
         address='https://report.iwf.org.uk/org/', categories=('csam',),
         source='https://www.iwf.org.uk/contact-us/'),
    dict(key='apple', label='App Store: жалоба на приложение', kind='manual',
         address='https://reportaproblem.apple.com/', categories=(),
         source='https://developer.apple.com/news/?id=j5uyprul'),
    dict(key='google', label='Google Play: жалоба на приложение', kind='manual',
         address='https://support.google.com/googleplay/answer/2853570?hl=ru',
         categories=(),
         source='https://support.google.com/googleplay/answer/2853570'),
)


def for_category(category: str) -> list[dict]:
    return [dict(c) for c in CHANNELS
            if not c['categories'] or category in c['categories']]


def email_recipient(category: str, key: str | None = None) -> str:
    default = 'copyright' if category == 'copyright' else 'abuse'
    selected = key or default
    for channel in for_category(category):
        if channel['key'] == selected and channel['kind'] == 'email':
            return channel['address']
    raise ValueError('Получатель не подходит для выбранной категории')
