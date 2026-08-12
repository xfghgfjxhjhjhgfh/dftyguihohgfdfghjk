# Telegram → Bale Forwarder

ورود Telegram مستقیماً از داخل ربات Bale انجام می‌شود.

## Railway Variables
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
BALE_BOT_TOKEN=
ADMIN_BALE_USER_ID=
DATA_DIR=/data

پس از اجرا در ربات:
1. `/login`
2. شماره Telegram را بفرست.
3. کد ورود Telegram را بفرست.
4. اگر 2FA فعال است، رمز دوم را بفرست.
5. سپس `/addsource @channel`
6. `/adddest <Bale chat_id یا @username>`
7. `/testdest`
8. `/startforward`

Session در `/data/telegram.session` ذخیره می‌شود؛ برای ماندگاری Railway یک Volume روی `/data` متصل کن.

کد ورود و رمز 2FA در لاگ ذخیره نمی‌شوند.
