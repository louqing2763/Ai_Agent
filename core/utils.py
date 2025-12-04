import pytz
from datetime import datetime

def now_taipei():
    return datetime.now(pytz.timezone("Asia/Taipei"))
