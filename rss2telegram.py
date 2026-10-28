from bs4 import BeautifulSoup
from html import escape
from telebot import types
import feedparser
import os
import random
import re
import sqlite3
import telebot
import time
import requests

DB_FILE = 'rss2telegram.db'
HEADERS = {'User-agent': 'Mozilla/5.1'}
SUMMARY_MAX = 300  # legenda de foto no Telegram aceita no máximo 1024 caracteres


def get_variable(variable):
    """Lê a variável do ambiente ou, se não existir, do arquivo <variavel>.txt."""
    value = os.environ.get(variable)
    if value:
        return value
    try:
        with open(f'{variable}.txt', 'r', encoding='utf-8') as var_file:
            return var_file.read().strip()
    except FileNotFoundError:
        return None


URL = get_variable('URL')
DESTINATION = get_variable('DESTINATION')
BOT_TOKEN = get_variable('BOT_TOKEN')
EMOJIS = os.environ.get('EMOJIS') or '🗞,📰,🗒,🗓,📋,🔗,📝,🗃'
PARAMETERS = os.environ.get('PARAMETERS') or False
MESSAGE_TEMPLATE = os.environ.get('MESSAGE_TEMPLATE') or False
BUTTON_TEXT = os.environ.get('BUTTON_TEXT') or False
DRYRUN = os.environ.get('DRYRUN') == 'failure'

bot = telebot.TeleBot(BOT_TOKEN or '0:dryrun')


# ---------- histórico ----------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('CREATE TABLE IF NOT EXISTS history (link TEXT)')
    return conn


def add_to_history(link):
    with db() as conn:
        conn.execute('INSERT INTO history (link) VALUES (?)', (link,))


def check_history(link):
    with db() as conn:
        return conn.execute('SELECT 1 FROM history WHERE link = ?', (link,)).fetchone()


# ---------- filtros (RULES.txt) ----------

def firewall(text):
    """Regras em RULES.txt, uma por linha: ACCEPT:palavra / DROP:palavra / DROP:ALL.
    A última regra que casar decide. Sem regra que case, o post é aceito."""
    try:
        with open('RULES.txt', 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return True
    result = True
    text = text.lower()
    for rule in lines:
        if ':' not in rule:
            continue
        opt, arg = rule.split(':', 1)
        opt, arg = opt.strip().upper(), arg.strip()
        if not arg:
            continue
        if arg == 'ALL' or arg.lower() in text:
            if opt == 'DROP':
                result = False
            elif opt == 'ACCEPT':
                result = True
    return result


# ---------- montagem da mensagem ----------

def define_link(link):
    if PARAMETERS:
        sep = '&' if '?' in link else '?'
        return f'{link}{sep}{PARAMETERS}'
    return link


def clean_summary(summary):
    text = re.sub('<[^<]+?>', '', summary or '').strip()
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX].rsplit(' ', 1)[0] + '…'
    return text


def set_text_vars(text, topic, html=True):
    """Troca {TITLE}, {SUMMARY}, {LINK}, {SITE_NAME} e {EMOJI} pelos valores do post."""
    values = {
        'SITE_NAME': topic['site_name'],
        'TITLE': topic['title'],
        'SUMMARY': clean_summary(topic['summary']),
        'LINK': define_link(topic['link']),
        'EMOJI': random.choice(EMOJIS.split(',')),
    }
    if html:
        values = {k: escape(v) for k, v in values.items()}

    def repl(match):
        return values.get(match.group(1), match.group(0))

    text = re.sub(r'\{(\w+)\}', repl, text)
    return text.replace('\\n', '\n')


def build_button(topic):
    if not BUTTON_TEXT:
        return None
    markup = types.InlineKeyboardMarkup()
    label = set_text_vars(BUTTON_TEXT, topic, html=False)
    markup.row(types.InlineKeyboardButton(label, url=define_link(topic['link'])))
    return markup


def get_img(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        html = BeautifulSoup(response.content, 'html.parser')
        tag = html.find('meta', {'property': 'og:image'})
        return tag['content'] if tag and tag.get('content') else False
    except requests.exceptions.RequestException:
        return False


def download_photo(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException:
        return None


# ---------- envio ----------

def send_message(topic):
    """Envia o post para todos os destinos. Retorna True se ao menos um recebeu."""
    if MESSAGE_TEMPLATE:
        message = set_text_vars(MESSAGE_TEMPLATE, topic)
    else:
        message = f'<b>{escape(topic["title"])}</b>'
    markup = build_button(topic)
    photo = download_photo(topic['photo']) if topic['photo'] else None

    sent = False
    for dest in DESTINATION.split(','):
        dest = dest.strip()
        if not dest:
            continue
        try:
            if photo:
                try:
                    bot.send_photo(dest, photo, caption=message, parse_mode='HTML', reply_markup=markup)
                    sent = True
                    continue
                except telebot.apihelper.ApiTelegramException as e:
                    print(f'    foto recusada em {dest} ({e}); enviando como texto')
            bot.send_message(dest, message, parse_mode='HTML', reply_markup=markup,
                             disable_web_page_preview=True)
            sent = True
        except telebot.apihelper.ApiTelegramException as e:
            print(f'!!! erro ao enviar para {dest}: {e}')
        time.sleep(0.5)
    return sent


def fetch_feed(url, attempts=4):
    """Baixa o feed com novas tentativas (o RSS do YouTube dá 404/500 intermitentes)."""
    for i in range(attempts):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.ok:
                return feedparser.parse(response.content)
            print(f'    {url} respondeu {response.status_code} (tentativa {i + 1})')
        except requests.exceptions.RequestException as e:
            print(f'    erro em {url}: {e} (tentativa {i + 1})')
        time.sleep(3 * (i + 1))
    return feedparser.parse(b'')


def check_topics(url):
    feed = fetch_feed(url)
    source = feed.get('feed', {}).get('title')
    if not source:
        print(f'\nERRO: {url} não parece um feed RSS válido.')
        return
    print(f'\nChecando {source}: {url}')

    for tpc in reversed(feed.get('entries', [])[:10]):
        link = tpc.get('link') or (tpc.links[0].href if tpc.get('links') else None)
        if not link or check_history(link):
            continue

        topic = {
            'site_name': source,
            'title': (tpc.get('title') or '').strip(),
            'summary': tpc.get('summary', ''),
            'link': link,
            'photo': False,
        }

        if DRYRUN:
            add_to_history(link)
            print(f'--- (dry run) {topic["title"]}')
            continue

        if not firewall(f'{topic["title"]} {topic["summary"]}'):
            add_to_history(link)
            print(f'xxx {topic["title"]}')
            continue

        # YouTube e muitos feeds já trazem a miniatura; senão busca a og:image da página
        thumbs = tpc.get('media_thumbnail')
        topic['photo'] = thumbs[0].get('url') if thumbs else get_img(link)

        # Só marca como enviado se deu certo, para tentar de novo na próxima execução
        if send_message(topic):
            add_to_history(link)
            print(f'... {topic["title"]}')


if __name__ == '__main__':
    if not URL or not DESTINATION:
        raise SystemExit('Configure as variáveis URL e DESTINATION.')
    if not BOT_TOKEN and not DRYRUN:
        raise SystemExit('Configure o secret BOT_TOKEN.')
    for feed_url in URL.split():
        try:
            check_topics(feed_url)
        except Exception as e:  # um feed com problema não derruba os outros
            print(f'\nERRO ao processar {feed_url}: {e}')
