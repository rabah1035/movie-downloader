"""نقطة الدخول الافتراضية لـ gunicorn (تتطابق مع الاتفاقية الافتراضية app:app)."""
from web_downloader import app

if __name__ == "__main__":
    app.run()
