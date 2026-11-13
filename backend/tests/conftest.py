import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="rentagent-test-")
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "CHECKPOINT_URL": f"sqlite:///{_tmp}/ckpt.db",
    "ANTHROPIC_API_KEY": "",
    "ENABLE_WEB_SEARCH_DISCOVERY": "false",
    "IMAP_HOST": "", "SMTP_HOST": "", "WHATSAPP_TOKEN": "",
    "USER_NAME": "Asha",
    "DAILY_OUTREACH_CAP": "3",
})

from app.db import init_db  # noqa: E402

init_db()
