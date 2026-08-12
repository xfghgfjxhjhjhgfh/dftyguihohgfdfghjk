import os, asyncio, json, sqlite3, tempfile, shutil, logging
from pathlib import Path
from typing import Optional
import requests
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError

load_dotenv()

BALE_TOKEN = os.getenv('BALE_BOT_TOKEN','').strip()
TG_API_ID = os.getenv('TELEGRAM_API_ID','').strip()
TG_API_HASH = os.getenv('TELEGRAM_API_HASH','').strip()
SESSION_FILE = DATA_DIR / 'telegram.session'
ADMIN_ID = os.getenv('ADMIN_BALE_USER_ID','').strip()
DATA_DIR = Path(os.getenv('DATA_DIR','/data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'forwarder.sqlite3'

if not BALE_TOKEN or not TG_API_ID or not TG_API_HASH:
    raise SystemExit('Missing BALE_BOT_TOKEN, TELEGRAM_API_ID or TELEGRAM_API_HASH')

TG_API_ID = int(TG_API_ID)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('tg2bale')

BALE_BASE = f'https://tapi.bale.ai/bot{BALE_TOKEN}'


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          value TEXT UNIQUE NOT NULL,
          title TEXT DEFAULT '',
          tg_id INTEGER DEFAULT NULL,
          enabled INTEGER DEFAULT 1,
          created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS dests(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          value TEXT UNIQUE NOT NULL,
          title TEXT DEFAULT '',
          enabled INTEGER DEFAULT 1,
          created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS sent(
          source_id INTEGER NOT NULL,
          msg_id INTEGER NOT NULL,
          dest_id INTEGER NOT NULL,
          sent_at INTEGER DEFAULT (strftime('%s','now')),
          PRIMARY KEY(source_id,msg_id,dest_id)
        );
        CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        ''')


def is_admin(uid):
    return not ADMIN_ID or str(uid) == ADMIN_ID


def bale_call(method, *, params=None, files=None, timeout=45):
    url = f'{BALE_BASE}/{method}'
    r = requests.post(url, data=params or {}, files=files or {}, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f'Bale HTTP {r.status_code}: {r.text[:500]}')
    if not r.ok or not data.get('ok', False):
        raise RuntimeError(f"Bale {method}: {data.get('description') or data}")
    return data.get('result')


def bale_get_chat(chat_id):
    return bale_call('getChat', params={'chat_id': str(chat_id)}, timeout=20)


def bale_send_text(chat_id, text):
    return bale_call('sendMessage', params={'chat_id': str(chat_id), 'text': text[:4096]})


def bale_send_file(chat_id, path: Path, caption: str, kind: str):
    method_by_kind = {
        'photo':'sendPhoto','video':'sendVideo','audio':'sendAudio','voice':'sendVoice',
        'document':'sendDocument','animation':'sendAnimation','sticker':'sendSticker'
    }
    method = method_by_kind.get(kind, 'sendDocument')
    field = kind if kind in method_by_kind else 'document'
    mime = 'application/octet-stream'
    if kind == 'photo': mime='image/jpeg'
    elif kind == 'video': mime='video/mp4'
    elif kind == 'audio': mime='audio/mpeg'
    elif kind == 'voice': mime='audio/ogg'
    elif kind == 'animation': mime='video/mp4'
    with open(path,'rb') as f:
        files = {field: (path.name, f, mime)}
        params = {'chat_id': str(chat_id)}
        if caption and kind != 'sticker': params['caption'] = caption[:1024]
        return bale_call(method, params=params, files=files, timeout=180)


def normalize_tg(value):
    v = value.strip()
    if v.startswith('https://t.me/'):
        v = '@' + v.split('https://t.me/',1)[1].split('?',1)[0].strip('/')
    elif v.startswith('t.me/'):
        v = '@' + v.split('t.me/',1)[1].split('?',1)[0].strip('/')
    return v


def normalize_dest(value):
    return value.strip()


def get_sources():
    with db() as c: return c.execute('SELECT * FROM sources WHERE enabled=1 ORDER BY id').fetchall()

def get_dests():
    with db() as c: return c.execute('SELECT * FROM dests WHERE enabled=1 ORDER BY id').fetchall()

def source_ids():
    return {int(r['tg_id']) for r in get_sources() if r['tg_id']}


def already_sent(source_id,msg_id,dest_id):
    with db() as c: return c.execute('SELECT 1 FROM sent WHERE source_id=? AND msg_id=? AND dest_id=?',(source_id,msg_id,dest_id)).fetchone() is not None

def mark_sent(source_id,msg_id,dest_id):
    with db() as c: c.execute('INSERT OR IGNORE INTO sent(source_id,msg_id,dest_id) VALUES(?,?,?)',(source_id,msg_id,dest_id))


def set_state(k,v):
    with db() as c: c.execute('INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,v))

def get_state(k,default=''):
    with db() as c:
        r=c.execute('SELECT v FROM state WHERE k=?',(k,)).fetchone()
        return r['v'] if r else default


def menu():
    return ('📡 Telegram → Bale\n\n'
            '➕ /addsource <کانال تلگرام>\n'
            '📋 /sources\n'
            '🗑 /delsource <id>\n\n'
            '➕ /adddest <chat_id یا @username>\n'
            '🎯 /dests\n'
            '🗑 /deldest <id>\n\n'
            '🧪 /testdest\n'
            '▶️ /startforward\n'
            '⏹ /stopforward\n'
            '📊 /status')


class BaleControl:
    def __init__(self): self.offset = 0
    def send(self, uid, text):
        try: bale_send_text(uid,text)
        except Exception as e: log.error('control send: %s',e)
    def poll(self):
        try:
            data = requests.get(f'{BALE_BASE}/getUpdates', params={'offset':self.offset,'timeout':25}, timeout=35).json()
            if not data.get('ok'): return []
            return data.get('result',[])
        except Exception as e:
            log.warning('Bale getUpdates: %s',e); return []
    async def run(self):
        while True:
            for u in self.poll():
                self.offset=max(self.offset,int(u.get('update_id',0))+1)
                m=u.get('message') or {}
                uid=(m.get('from') or {}).get('id')
                text=(m.get('text') or '').strip()
                if uid and text: await self.handle(uid,text)
            await asyncio.sleep(.5)
    async def handle(self,uid,text):
        if not is_admin(uid):
            self.send(uid,'⛔ دسترسی ندارید.')
            return
        p=text.split(maxsplit=1); cmd=p[0].lower(); arg=p[1].strip() if len(p)>1 else ''
        # Telegram login flow: /login -> phone -> code -> optional 2FA password
        st = login_state.get(uid)
        if st and not text.startswith('/'):
            try:
                if st.get('awaiting_phone'):
                    await telegram_login_start(uid, text)
                    st = login_state.get(uid)
                    st.pop('awaiting_phone', None)
                    self.send(uid,'📨 کد ورود Telegram ارسال شد. حالا فقط کد را بفرست.')
                    return
                if st.get('needs_password'):
                    me = await telegram_login_password(uid, text)
                    self.send(uid, f"✅ تلگرام متصل شد.\nحساب: @{getattr(me,'username',None) or '-'}\nحالا می‌توانی مبدأها را اضافه کنی.")
                    return
                me = await telegram_login_code(uid, text)
                self.send(uid, f"✅ تلگرام متصل شد.\nحساب: @{getattr(me,'username',None) or '-'}")
                return
            except SessionPasswordNeededError:
                st['needs_password'] = True
                self.send(uid,'🔑 تأیید دومرحله‌ای فعاله. رمز 2FA تلگرام را بفرست.')
                return
            except Exception as e:
                log.exception('telegram login failed')
                self.send(uid,f'❌ ورود تلگرام ناموفق بود:\n{type(e).__name__}: {e}')
                return
        try:
            if cmd in ('/start','/menu'):
                self.send(uid,menu())
            elif cmd in ('/login','/telegram'):
                self.send(uid,
                          '🔐 اتصال تلگرام\n\n'
                          'شماره تلگرام را با فرمت بین‌المللی بفرست.\n'
                          'مثال: +989121234567\n\n'
                          'کد ورود از طرف Telegram برایت ارسال می‌شود؛ آن را همینجا بفرست.')
                login_state[uid] = {'awaiting_phone': True}
            elif cmd in ('/logout','/telegramlogout'):
                global tg
                if tg is not None:
                    try: await tg.disconnect()
                    except Exception: pass
                    tg = None
                if SESSION_FILE.exists(): SESSION_FILE.unlink()
                login_state.pop(uid, None)
                self.send(uid,'✅ اتصال تلگرام حذف شد.')
            elif cmd=='/addsource':
                if tg is None or not await tg.is_user_authorized():
                    self.send(uid,'❌ ابتدا /login را بزن و تلگرام را متصل کن.')
                    return
                if not arg: self.send(uid,'فرمت: /addsource @channelusername'); return
                value=normalize_tg(arg)
                ent=await tg.get_entity(value)
                title=getattr(ent,'title',None) or getattr(ent,'first_name',None) or value
                tg_id=int(ent.id)
                with db() as c: c.execute('INSERT OR REPLACE INTO sources(value,title,tg_id,enabled) VALUES(?,?,?,1)',(value,title,tg_id))
                self.send(uid,f'✅ مبدأ تلگرام ثبت شد:\n{title}\n{value}\nID: {tg_id}')
            elif cmd=='/sources':
                rows=get_sources(); self.send(uid,'📋 مبدأها:\n'+'\n'.join(f"{r['id']}. {r['title'] or r['value']} — {r['value']}" for r in rows) if rows else '📭 مبدأی ثبت نشده.')
            elif cmd=='/delsource':
                with db() as c: c.execute('DELETE FROM sources WHERE id=?',(int(arg),))
                self.send(uid,'✅ مبدأ حذف شد.')
            elif cmd=='/adddest':
                if not arg: self.send(uid,'فرمت: /adddest <chat_id یا @username>'); return
                value=normalize_dest(arg)
                chat=bale_get_chat(value)
                title=((chat or {}).get('title') or (chat or {}).get('first_name') or value)
                with db() as c: c.execute('INSERT OR REPLACE INTO dests(value,title,enabled) VALUES(?,?,1)',(value,title))
                self.send(uid,f'✅ مقصد بله ثبت شد:\n{title}\n{value}')
            elif cmd=='/dests':
                rows=get_dests(); self.send(uid,'🎯 مقصدها:\n'+'\n'.join(f"{r['id']}. {r['title'] or r['value']} — {r['value']}" for r in rows) if rows else '📭 مقصدی ثبت نشده.')
            elif cmd=='/deldest':
                with db() as c: c.execute('DELETE FROM dests WHERE id=?',(int(arg),))
                self.send(uid,'✅ مقصد حذف شد.')
            elif cmd=='/testdest':
                rows=get_dests()
                if not rows: self.send(uid,'📭 مقصدی ثبت نشده.'); return
                out=[]
                for r in rows:
                    try:
                        result=bale_send_text(r['value'],'✅ تست اتصال Telegram → Bale\nاین پیام توسط API رسمی Bot بله ارسال شد.')
                        out.append(f"✅ {r['title'] or r['value']} | پاسخ: {str(result)[:160]}")
                    except Exception as e: out.append(f"❌ {r['title'] or r['value']} | {e}")
                self.send(uid,'📤 نتیجه تست مقصدها:\n\n'+'\n'.join(out))
            elif cmd=='/startforward':
                set_state('running','1'); self.send(uid,'▶️ انتقال فعال شد.');
            elif cmd=='/stopforward':
                set_state('running','0'); self.send(uid,'⏹ انتقال متوقف شد.')
            elif cmd=='/status':
                self.send(uid,f"📊 وضعیت\nانتقال: {'فعال' if get_state('running','0')=='1' else 'متوقف'}\nمبدأها: {len(get_sources())}\nمقصدها: {len(get_dests())}")
            else: self.send(uid,menu())
        except Exception as e:
            log.exception('command failed')
            self.send(uid,f'❌ خطا:\n{type(e).__name__}: {e}')


async def forward_event(event):
    if get_state('running','0')!='1': return
    sid=int(event.chat_id)
    rows=[r for r in get_sources() if int(r['tg_id'] or 0)==sid]
    if not rows: return
    source=rows[0]
    dests=get_dests()
    if not dests: return
    msg=event.message
    caption=msg.message or ''
    for d in dests:
        did=int(d['id'])
        if already_sent(int(source['id']),int(msg.id),did): continue
        for attempt in range(1,4):
            tmp=None
            try:
                if msg.media:
                    tmpdir=Path(tempfile.mkdtemp(prefix='tg2bale-'))
                    tmp=Path(await tg.download_media(msg, file=str(tmpdir)))
                    if not tmp or not tmp.exists(): raise RuntimeError('دانلود رسانه از تلگرام انجام نشد')
                    kind='document'
                    if msg.photo: kind='photo'
                    elif msg.video: kind='video'
                    elif msg.audio: kind='audio'
                    elif msg.voice: kind='voice'
                    elif msg.gif: kind='animation'
                    elif msg.sticker: kind='sticker'
                    bale_send_file(d['value'],tmp,caption,kind)
                else:
                    bale_send_text(d['value'],caption)
                mark_sent(int(source['id']),int(msg.id),did)
                log.info('sent tg=%s msg=%s -> bale=%s',sid,msg.id,d['value'])
                break
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                log.warning('send attempt %s failed tg=%s msg=%s dest=%s: %s',attempt,sid,msg.id,d['value'],e)
                if attempt<3: await asyncio.sleep(attempt*2)
            finally:
                if tmp:
                    try: shutil.rmtree(tmp.parent,ignore_errors=True)
                    except Exception: pass


init_db()

tg = None
login_state = {}

def telegram_client():
    return TelegramClient(str(SESSION_FILE), TG_API_ID, TG_API_HASH)

async def telegram_login_start(uid, phone):
    global tg
    if tg is not None and await tg.is_user_authorized():
        return 'already'
    if not phone.startswith('+'):
        phone = '+' + phone
    client = telegram_client()
    await client.connect()
    sent = await client.send_code_request(phone)
    login_state[uid] = {'client': client, 'phone': phone, 'phone_code_hash': sent.phone_code_hash}
    return 'code_sent'

async def telegram_login_code(uid, code):
    global tg
    st = login_state.get(uid)
    if not st:
        raise RuntimeError('ابتدا /login را بزن و شماره تلفن را ارسال کن.')
    client = st['client']
    try:
        await client.sign_in(st['phone'], code, phone_code_hash=st['phone_code_hash'])
    except SessionPasswordNeededError:
        st['needs_password'] = True
        raise
    if not await client.is_user_authorized():
        raise RuntimeError('ورود تلگرام تأیید نشد.')
    tg = client
    tg.add_event_handler(forward_event, events.NewMessage(incoming=True))
    login_state.pop(uid, None)
    return await tg.get_me()

async def telegram_login_password(uid, password):
    global tg
    st = login_state.get(uid)
    if not st:
        raise RuntimeError('جلسه ورود پیدا نشد. دوباره /login را بزن.')
    client = st['client']
    await client.sign_in(password=password)
    if not await client.is_user_authorized():
        raise RuntimeError('رمز دومرحله‌ای تأیید نشد.')
    tg = client
    tg.add_event_handler(forward_event, events.NewMessage(incoming=True))
    login_state.pop(uid, None)
    return await tg.get_me()

async def restore_telegram():
    global tg
    if not SESSION_FILE.exists():
        return False
    client = telegram_client()
    await client.connect()
    if await client.is_user_authorized():
        tg = client
        tg.add_event_handler(forward_event, events.NewMessage(incoming=True))
        me = await tg.get_me()
        log.info('Telegram session restored as %s (%s)', getattr(me,'username',None), me.id)
        return True
    await client.disconnect()
    return False

async def main():
    set_state('running', get_state('running','0'))
    await restore_telegram()
    control=BaleControl()
    await control.run()

if __name__=='__main__':
    asyncio.run(main())
