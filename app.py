import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, render_template, request, jsonify, send_from_directory
import os, time, threading, re, shutil, subprocess, base64, uuid

# --- CONFIG ---
FIREBASE_URL = "https://ndo-pj-default-rtdb.asia-southeast1.firebasedatabase.app/"
USER_ID = "master01"  # Single-user UID

# --- Firebase Init ---
import glob
json_files = glob.glob(os.path.join(os.path.dirname(__file__), "*.json"))
cred_path = json_files[0] if json_files else os.path.join(os.path.dirname(__file__), "firebase-service-account.json")

if os.path.exists(cred_path):
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})
    except ValueError:
        pass  # Already initialized
    except Exception as e:
        print(f"Firebase init error: {e}")
else:
    print("WARNING: firebase-service-account.json not found!")

# --- Flask App ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'andro-control-secret'

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = CURRENT_DIR
BUILDS_DIR = os.path.join(CURRENT_DIR, "builds")
os.makedirs(BUILDS_DIR, exist_ok=True)
UPLOADS_DIR = os.path.join(CURRENT_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)


# ==================== ROUTES ====================

@app.route('/')
def index():
    last_apk = None
    try:
        u = db.reference(f'panel_users/{USER_ID}').get() or {}
        last_apk = u.get('last_apk', '')
    except:
        pass
    return render_template('index.html',
        uid=USER_ID,
        last_apk=last_apk,
        firebase_url=FIREBASE_URL
    )


@app.route('/control/<uid>/<did>')
def control(uid, did):
    return render_template('control.html',
        user_id=uid,
        device_id=did,
        firebase_url=FIREBASE_URL
    )


@app.route('/api/devices')
def api_devices():
    """Fetch all devices for USER_ID using Admin SDK."""
    try:
        user_data = db.reference(f'users/{USER_ID}/devices').get() or {}
        devices = []
        for did, dev in user_data.items():
            if not isinstance(dev, dict):
                continue
            info = dev.get('info', {}) if isinstance(dev.get('info'), dict) else {}
            health = dev.get('health', {}) if isinstance(dev.get('health'), dict) else {}

            online_val = health.get('online', False)
            is_online = str(online_val).lower() in ('true', '1')

            devices.append({
                'did': did,
                'model': info.get('model', info.get('brand', 'Unknown')),
                'battery': health.get('battery', 0),
                'is_online': is_online,
            })
        return jsonify({'success': True, 'devices': devices, 'timestamp': time.strftime('%H:%M:%S')})
    except Exception as e:
        return jsonify({'success': False, 'devices': [], 'error': str(e)})


@app.route('/build', methods=['POST'])
def build():
    icon_path = None
    if 'app_icon' in request.files:
        f = request.files['app_icon']
        if f and f.filename:
            icon_path = os.path.join(UPLOADS_DIR, f"icon_{USER_ID}.png")
            f.save(icon_path)

    # Save basic config
    try:
        db.reference(f'panel_users/{USER_ID}').update({
            'webview_url': request.form.get('webview_url', ''),
        })
    except Exception as e:
        print(f"Config save error: {e}")

    threading.Thread(target=build_worker, args=(
        USER_ID,
        request.form.get('webview_url', ''),
        request.form.get('app_name', 'Application'),
        icon_path
    ), daemon=True).start()

    return jsonify({'success': True})


@app.route('/download/<path:filename>')
def download(filename):
    safe = os.path.basename(filename)
    path = os.path.join(BUILDS_DIR, safe)
    if os.path.exists(path):
        return send_from_directory(BUILDS_DIR, safe, as_attachment=True)
    return "APK not found", 404


# ==================== BUILD SYSTEM ====================

def inject_config(uid, webview, app_name):
    """Inject config into Android source."""
    try:
        manifest = os.path.join(PROJECT_ROOT, "app", "src", "main", "AndroidManifest.xml")
        if os.path.exists(manifest):
            with open(manifest, 'r', encoding='utf-8') as f:
                content = f.read()
            configs = {"webview_url": webview, "user_id": uid, "firebase_url": FIREBASE_URL}
            for k, v in configs.items():
                p = f'android:name="{k}" android:value="[^"]*"'
                r = f'android:name="{k}" android:value="{v}"'
                content = re.sub(p, r, content)
            with open(manifest, 'w', encoding='utf-8') as f:
                f.write(content)

        strings = os.path.join(PROJECT_ROOT, "app", "src", "main", "res", "values", "strings.xml")
        if os.path.exists(strings):
            with open(strings, 'r', encoding='utf-8') as f:
                content = f.read()
            content = re.sub(r'<string name="app_name">.*</string>',
                           f'<string name="app_name">{app_name}</string>', content)
            with open(strings, 'w', encoding='utf-8') as f:
                f.write(content)
        return True
    except Exception as e:
        print(f"Inject error: {e}")
        return False


def build_worker(uid, webview, app_name, icon_path=None):
    """Background APK build."""
    ref = db.reference(f'panel_users/{uid}/build_status')
    try:
        ref.set({'status': '⚙️ Injecting Config', 'percent': 10, 'last_log': 'Writing config...'})
        if not inject_config(uid, webview, app_name):
            ref.set({'status': '❌ Injection Failed', 'percent': 0, 'last_log': 'Config injection failed'})
            return

        ref.update({'status': '🔨 Building', 'percent': 30, 'last_log': 'Starting Gradle...'})
        cmd = 'gradlew.bat' if os.name == 'nt' else './gradlew'
        gradle = os.path.join(PROJECT_ROOT, cmd)

        if not os.path.exists(gradle):
            ref.set({'status': '❌ Error', 'percent': 0, 'last_log': f'{cmd} not found!'})
            return

        # Kill stale processes
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/IM', 'java.exe', '/T'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([gradle, '--stop'], cwd=PROJECT_ROOT, timeout=10,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass

        build_cmd = [gradle, 'clean', 'assembleDebug', '--no-daemon',
                     '--no-configuration-cache', '-Dorg.gradle.daemon=false']

        proc = subprocess.Popen(build_cmd, cwd=PROJECT_ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if ":app:preBuild" in line:
                ref.update({'percent': 35, 'last_log': 'Preparing build...'})
            elif ":app:compileDebug" in line:
                ref.update({'percent': 55, 'last_log': 'Compiling...'})
            elif ":app:mergeDebug" in line:
                ref.update({'percent': 75, 'last_log': 'Merging resources...'})
            elif ":app:packageDebug" in line:
                ref.update({'percent': 90, 'last_log': 'Packaging APK...'})

        proc.wait(timeout=600)

        if proc.returncode != 0:
            ref.set({'status': '❌ Build Failed', 'percent': 0, 'last_log': 'Gradle failed'})
            return

        output_apk = os.path.join(PROJECT_ROOT, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
        if os.path.exists(output_apk):
            clean = re.sub(r'[^\w._\- ]', '', app_name).strip().replace(' ', '_') or 'App'
            fname = f"{clean}_{uid}.apk"
            shutil.copy2(output_apk, os.path.join(BUILDS_DIR, fname))
            db.reference(f'panel_users/{uid}').update({
                'last_apk': fname, 'build_time': time.strftime("%I:%M %p")
            })
            ref.set({'status': '✅ Complete', 'last_log': 'Build successful!', 'percent': 100})
        else:
            ref.set({'status': '❌ APK Not Found', 'percent': 0, 'last_log': 'Output missing'})

    except Exception as e:
        ref.set({'status': f'❌ Error', 'percent': 0, 'last_log': str(e)})


if __name__ == '__main__':
    print("\n  🚀 Andro Control running at http://localhost:7070\n")
    app.run(host='0.0.0.0', port=7070, debug=True)
