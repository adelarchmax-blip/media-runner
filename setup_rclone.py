import os
import sys
import base64
import re
import subprocess

def setup_rclone():
    raw_data = os.environ.get("RCLONE_CONFIG_DATA", "").strip()
    if not raw_data:
        print("⚠️ تنبيه: متغير RCLONE_CONFIG_DATA فارغ أو لم يتم العثور عليه في أسرار جيت هب.")
        sys.exit(1)

    # إزالة أي علامات اقتباس محيطة تم لصقها خطأ
    if (raw_data.startswith('"') and raw_data.endswith('"')) or (raw_data.startswith("'") and raw_data.endswith("'")):
        raw_data = raw_data[1:-1].strip()

    config_content = ""

    # فحص ما إذا كانت البيانات نص عادي أصلاً (INI format)
    if "[" in raw_data and "=" in raw_data and ("type" in raw_data or "drive" in raw_data):
        config_content = raw_data
    else:
        # فك تشفير Base64 مع تنظيف أي مسافات أو أسطر زائدة
        clean_b64 = re.sub(r'[^A-Za-z0-9+/=]', '', raw_data)
        missing_padding = len(clean_b64) % 4
        if missing_padding:
            clean_b64 += '=' * (4 - missing_padding)
        try:
            decoded = base64.b64decode(clean_b64).decode('utf-8', errors='ignore')
            if "[" in decoded and "=" in decoded:
                config_content = decoded
            else:
                config_content = raw_data
        except ValueError:  # not Base64: the secret holds the config as plain text
            config_content = raw_data

    # التأكد من إنشاء مسار التكوين
    config_dir = os.path.expanduser("~/.config/rclone")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "rclone.conf")

    # The config holds Drive tokens: readable by this user only.
    with open(os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(config_content.strip() + "\n")
    os.chmod(config_path, 0o600)

    # فحص صلاحية التكوين باستخدام rclone listremotes
    try:
        res = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, check=True)
        print(f"✅ rclone configured ({len(res.stdout.split())} remote(s)).")
    except subprocess.CalledProcessError as err:
        print(f"❌ (rclone exit {err.returncode}) فشل تحميل ملف إعدادات rclone. يرجى التأكد من صحة النص أو ترميز Base64 المخزن في الخزنة المشفرة.")
        sys.exit(1)

if __name__ == "__main__":
    setup_rclone()
