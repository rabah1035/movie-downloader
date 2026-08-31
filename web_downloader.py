import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template_string, request

from downloader import (
    DEFAULT_MOVIES_DIR,
    CustomIndexer,
    IMDbResolver,
    check_disk_space,
    ensure_aria2_engine,
    organize_and_clean_movie,
)

app = Flask(__name__)

_lock = threading.RLock()
_state = {
    "logs": [],
    "busy": False,
    "paused": False,
    "result": None,
    "progress": {},
    "note": "",
    "started_at": None,
}

PROGRESS_RE = re.compile(
    r"\[#\w+\s+(?P<done>\S+)/(?P<total>\S+)\((?P<pct>\d+)%\)"
    r"[^\]]*DL:(?P<speed>\S+)(?:\s+ETA:(?P<eta>[^\]\s]+))?"
)

_flags = {
    "pause": False,
    "cancel": False,
}

MAX_RETRIES = 5
RETRY_DELAY = 5
LIBRARY_FILE = Path(__file__).parent / "library.json"


def add_log(msg: str):
    with _lock:
        _state["logs"].append(msg)


def take_logs() -> list:
    with _lock:
        logs = _state["logs"]
        _state["logs"] = []
        return logs


# ====================== المكتبة ======================

def load_library() -> list:
    try:
        return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_library(lib: list):
    LIBRARY_FILE.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")


def add_to_library(imdb_id: str, title: str, year: str, folder: Path):
    lib = load_library()
    lib[:] = [e for e in lib if e.get("folder") != str(folder)]
    lib.append({
        "imdb_id": imdb_id,
        "title": title,
        "year": year,
        "folder": str(folder),
        "added": time.strftime("%Y-%m-%d %H:%M"),
        "poster": "",
        "plot": "",
        "cast": [],
    })
    save_library(lib)


def fetch_movie_details(entry: dict):
    """يجلب البوستر والقصة والممثلين من صفحة IMDb (JSON-LD) ويخزنهم محلياً."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en-US,en;q=0.9"}
        resp = requests.get(f"https://www.imdb.com/title/{entry['imdb_id']}/", headers=headers, timeout=12)
        if resp.status_code != 200:
            return
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', resp.text, re.S)
        if not m:
            return
        data = json.loads(m.group(1))
        entry["poster"] = data.get("image", "")
        entry["plot"] = data.get("description", "")
        actors = data.get("actor", [])
        if isinstance(actors, dict):
            actors = [actors]
        entry["cast"] = [a.get("name", "") for a in actors if isinstance(a, dict)][:8]
    except Exception:
        pass


# ====================== محرك التحميل ======================

def run_download(magnet: str, folder: Path, title: str, imdb_id: str):
    exe = None
    try:
        exe = ensure_aria2_engine()
    except SystemExit as e:
        with _lock:
            _state["busy"] = False
            _state["result"] = {"success": False, "message": str(e).strip() or "فشل تجهيز محرك التحميل"}
        return

    add_log(f"🚀 بدء التحميل إلى: {folder}")

    def was_cancelled():
        return _flags["cancel"]

    attempt = 0
    cancelled = False
    while True:
        # انتظار بعد الإيقاف المؤقت أو بين المحاولات
        while _flags["pause"] and not _flags["cancel"]:
            time.sleep(0.4)
        if _flags["cancel"]:
            cancelled = True
            break

        cmd = [
            exe, f"--dir={folder}", "--seed-time=0",
            "--console-log-level=warn", "--summary-interval=1",
            "--bt-max-peers=120", "--max-connection-per-server=16",
            magnet,
        ]
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as e:
            add_log(f"❌ تعذر تشغيل المحرك: {e}")
            break

        stopped = False
        for line in iter(process.stdout.readline, ""):
            if _flags["cancel"]:
                process.terminate()
                stopped = True
                cancelled = True
                break
            if _flags["pause"]:
                add_log("⏸ إيقاف مؤقت... جاري حفظ التقدم")
                process.terminate()
                stopped = True
                break
            line_str = line.strip()
            if line_str:
                pm = PROGRESS_RE.search(line_str)
                if pm:
                    with _lock:
                        _state["progress"] = {
                            "done": pm.group("done"),
                            "total": pm.group("total"),
                            "pct": int(pm.group("pct")),
                            "speed": pm.group("speed"),
                            "eta": pm.group("eta") or "",
                        }
                else:
                    add_log(line_str)

        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

        if cancelled:
            break

        if stopped and _flags["pause"]:
            with _lock:
                _state["paused"] = True
                _state["note"] = "⏸ التحميل متوقف مؤقتاً"
            add_log("⏸ تم الإيقاف المؤقت — اضغط ▶️ استئناف للمتابعة")
            while _flags["pause"] and not _flags["cancel"]:
                time.sleep(0.4)
            with _lock:
                _state["paused"] = False
                _state["note"] = ""
            if _flags["cancel"]:
                cancelled = True
                break
            add_log("▶️ استئناف التحميل من نفس النقطة...")
            continue

        if process.returncode == 0:
            add_log("🧹 تنظيم المجلد...")
            organize_and_clean_movie(folder, title)
            add_to_library(imdb_id, title, "", folder)
            with _lock:
                _state["result"] = {"success": True, "message": "تم التحميل وتنظيم المجلد بنجاح!"}
            break

        attempt += 1
        if attempt >= MAX_RETRIES:
            with _lock:
                _state["result"] = {"success": False, "message": f"فشل التحميل بعد {MAX_RETRIES} محاولات (رمز {process.returncode})"}
            break

        add_log(f"⚠️ حدث خطأ (انقطاع إنترنت؟) — إعادة المحاولة {attempt}/{MAX_RETRIES - 1} خلال {RETRY_DELAY} ثوانٍ...")
        with _lock:
            _state["note"] = f"🔄 إعادة محاولة {attempt} من {MAX_RETRIES - 1} بعد {RETRY_DELAY} ثوانٍ..."
        waited = 0.0
        while waited < RETRY_DELAY and not _flags["cancel"]:
            time.sleep(0.4)
            waited += 0.4
        if _flags["cancel"]:
            cancelled = True
            break
        with _lock:
            _state["note"] = ""
        add_log("🔄 استئناف من نقطة التوقف...")

    if cancelled:
        add_log("🗑 إلغاء التحميل وحذف الملفات الجزئية...")
        shutil.rmtree(folder, ignore_errors=True)
        with _lock:
            _state["result"] = {"success": False, "message": "تم إلغاء التحميل وحذف الملفات"}

    with _lock:
        _state["busy"] = False
        _state["paused"] = False
    _flags["pause"] = False
    _flags["cancel"] = False


# ====================== الواجهة الرئيسية ======================

PAGE = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🎬 محمّل الأفلام</title>
<style>
  body { font-family: Tahoma, Arial, sans-serif; background:#12141c; color:#e6e6e6;
         margin:0; padding:24px; }
  h1 { color:#4fc3f7; text-align:center; margin-top:0; }
  h1 a { font-size:14px; color:#ffb74d; text-decoration:none; margin-right:14px;
         border:1px solid #ffb74d; padding:4px 12px; border-radius:20px; }
  .bar { display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap; }
  input[type=text] { flex:1; padding:10px; border-radius:6px; border:1px solid #333;
                     background:#1c1f2b; color:#fff; min-width:180px; }
  #dest { max-width:340px; }
  button { padding:10px 18px; border:none; border-radius:6px; cursor:pointer;
           background:#4fc3f7; color:#000; font-weight:bold; font-size:14px; }
  button:hover:not(:disabled) { filter:brightness(1.15); }
  button:disabled { background:#555; color:#999; cursor:not-allowed; }
  table { width:100%; border-collapse:collapse; margin-top:8px; }
  th, td { padding:9px; border-bottom:1px solid #262a38; text-align:start; font-size:14px; }
  th { background:#1c2030; color:#8ab4ff; }
  .langs { text-align:center; margin-bottom:10px; }
  .langs button { background:#1c2030; color:#c9cdd6; border:1px solid #333; padding:5px 14px;
                  font-size:12.5px; margin:0 3px; font-weight:normal; }
  .langs button.active { background:#4fc3f7; color:#000; font-weight:bold; border-color:#4fc3f7; }
  tr:hover td { background:#191d29; }
  td.title { direction:ltr; text-align:left; word-break:break-all; }
  .rowbtn { background:#2e7d32; color:#fff; padding:6px 14px; }
  #ctrl { display:none; gap:8px; justify-content:center; margin-top:14px; }
  #ctrl button { font-size:15px; }
  #pauseB { background:#ffb74d; } #resumeB { background:#66bb6a; display:none; } #cancelB { background:#c62828; color:#fff; }
  #prog { display:none; margin-top:16px; background:#1a1d29; border:1px solid #262a38;
          border-radius:10px; padding:14px 16px; }
  .pwrap { background:#0b0d13; border-radius:20px; height:22px; overflow:hidden;
           border:1px solid #262a38; }
  #pfill { height:100%; width:0%; background:linear-gradient(90deg,#2e7d32,#66bb6a);
           transition:width 1s linear; border-radius:20px; }
  #ptext { margin-top:9px; font-size:13.5px; color:#c9cdd6; display:flex;
           flex-wrap:wrap; gap:6px 20px; justify-content:center; }
  #pnote { text-align:center; color:#ffb74d; font-size:13px; min-height:18px; margin-top:6px; }
  .pv { color:#8ab4ff; font-weight:bold; }
  .banner { padding:10px 14px; border-radius:8px; margin-top:12px; display:none; }
  .ok { background:#1b3a1d; border:1px solid #2e7d32; }
  .err { background:#3a1b1b; border:1px solid #c62828; }
  .info { background:#152a3a; border:1px solid #4fc3f7; }
</style>
</head>
<body>
<div class="langs">
  <button id="lang-ar" onclick="setLang('ar')">🇸🇦 عربية</button>
  <button id="lang-fr" onclick="setLang('fr')">🇫🇷 Français</button>
  <button id="lang-en" onclick="setLang('en')">🇬🇧 English</button>
</div>
<h1><span data-i18n="appTitle">🎬 محمّل الأفلام</span> <a href="/library" data-i18n="libraryLink">📚 مكتبة أفلامي</a></h1>

<div class="bar">
  <input type="text" id="dest" value="{{ dest }}">
  <input type="text" id="query" data-i18n-ph="searchPh"
         placeholder="اكتب اسم الفيلم... مثال: Bloodsport 1988"
         onkeydown="if(event.key==='Enter') doSearch()">
  <button id="sbtn" data-i18n="searchBtn" onclick="doSearch()">🔍 بحث</button>
</div>

<table id="tbl" style="display:none">
  <thead><tr>
    <th data-i18n="thTitle">العنوان</th>
    <th data-i18n="thSize">الحجم</th>
    <th data-i18n="thSeeds">السييدرز</th>
    <th data-i18n="thSource">المصدر</th>
    <th></th>
  </tr></thead>
  <tbody></tbody>
</table>

<div id="banner" class="banner"></div>

<div id="ctrl">
  <button id="pauseB" data-i18n="pauseB" onclick="ctrl('pause')">⏸ إيقاف مؤقت</button>
  <button id="resumeB" data-i18n="resumeB" onclick="ctrl('resume')">▶️ استئناف</button>
  <button id="cancelB" data-i18n="cancelB" onclick="ctrl('cancel')">❌ إلغاء التحميل</button>
</div>

<div id="prog">
  <div class="pwrap"><div id="pfill"></div></div>
  <div id="ptext"></div>
  <div id="pnote"></div>
</div>

<script>
const I18N = {
  ar: {
    appTitle:'🎬 محمّل الأفلام', libraryLink:'📚 مكتبة أفلامي',
    searchPh:'اكتب اسم الفيلم... مثال: Bloodsport 1988', searchBtn:'🔍 بحث',
    thTitle:'العنوان', thSize:'الحجم', thSeeds:'السييدرز', thSource:'المصدر',
    dlBtn:"⬇️ تحميل", pauseB:'⏸ إيقاف مؤقت', resumeB:'▶️ استئناف', cancelB:'❌ إلغاء التحميل',
    errEmpty:'⚠️ اكتب اسم فيلم أولاً', searching:'⏳ جاري البحث...',
    connErr:'❌ خطأ في الاتصال بالخادم',
    srvDown:'❌ الخادم غير متصل — تأكد أن web_downloader.py يعمل ثم أعد المحاولة',
    dlInfo:'⏳ التحميل قيد التنفيذ — يمكنك ⏸ الإيقاف المؤقت أو ❌ الإلغاء',
    unknownErr:'خطأ غير معروف', startFail:'تعذر بدء التحميل',
    of:'من', speedL:'⬇️ السرعة', etaL:'⏳ متبقي', elL:'⏱ المنقضي',
    connecting:'⏳ جاري الاتصال بالـ Peers وجلب بيانات الفيلم...'
  },
  fr: {
    appTitle:'🎬 Téléchargeur de Films', libraryLink:'📚 Ma Bibliothèque',
    searchPh:"Tapez le nom du film... ex : Bloodsport 1988", searchBtn:'🔍 Rechercher',
    thTitle:'Titre', thSize:'Taille', thSeeds:'Seeds', thSource:'Source',
    dlBtn:'⬇️ Télécharger', pauseB:'⏸ Pause', resumeB:'▶️ Reprendre', cancelB:'❌ Annuler le téléchargement',
    errEmpty:"⚠️ Tapez d'abord un nom de film", searching:'⏳ Recherche en cours...',
    connErr:'❌ Erreur de connexion au serveur',
    srvDown:'❌ Serveur injoignable — vérifiez que web_downloader.py fonctionne puis réessayez',
    dlInfo:'⏳ Téléchargement en cours — ⏸ Pause ou ❌ Annuler disponibles',
    unknownErr:'Erreur inconnue', startFail:'Impossible de démarrer le téléchargement',
    of:'sur', speedL:'⬇️ Vitesse', etaL:'⏳ Restant', elL:'⏱ Écoulé',
    connecting:'⏳ Connexion aux peers et récupération des données du film...'
  },
  en: {
    appTitle:'🎬 Movie Downloader', libraryLink:'📚 My Movie Library',
    searchPh:'Type a movie name... e.g. Bloodsport 1988', searchBtn:'🔍 Search',
    thTitle:'Title', thSize:'Size', thSeeds:'Seeders', thSource:'Source',
    dlBtn:'⬇️ Download', pauseB:'⏸ Pause', resumeB:'▶️ Resume', cancelB:'❌ Cancel Download',
    errEmpty:'⚠️ Enter a movie name first', searching:'⏳ Searching...',
    connErr:'❌ Server connection error',
    srvDown:'❌ Server unreachable — make sure web_downloader.py is running, then retry',
    dlInfo:'⏳ Download in progress — you can ⏸ Pause or ❌ Cancel',
    unknownErr:'Unknown error', startFail:'Could not start the download',
    of:'of', speedL:'⬇️ Speed', etaL:'⏳ ETA', elL:'⏱ Elapsed',
    connecting:'⏳ Connecting to peers and fetching movie data...'
  }
};

const MSGS = {
  'اكتب اسم الفيلم':            {fr:'Tapez le nom du film', en:'Please enter a movie name'},
  'لم يتم العثور على الفيلم على IMDb': {fr:'Film introuvable sur IMDb', en:'Movie not found on IMDb'},
  'هناك تحميل قيد التنفيذ حالياً':   {fr:'Un téléchargement est déjà en cours', en:'A download is already running'},
  'بيانات ناقصة':               {fr:'Données manquantes', en:'Missing data'},
  '❌ المساحة غير كافية على القرص الهدف (التفاصيل في نافذة الخادم)': {
      fr:"❌ Espace disque insuffisant sur le lecteur de destination (détails dans la fenêtre du serveur)",
      en:'❌ Insufficient disk space on target drive (details in server window)'},
  '⏸ التحميل متوقف مؤقتاً':       {fr:'⏸ Téléchargement en pause', en:'⏸ Download paused'},
  'تم التحميل وتنظيم المجلد بنجاح!': {fr:'Téléchargement terminé et dossier organisé avec succès !',
                                    en:'Download completed and folder organized!'},
  'تم إلغاء التحميل وحذف الملفات':  {fr:'Téléchargement annulé et fichiers supprimés',
                                  en:'Download cancelled and files deleted'},
  'فشل تجهيز محرك التحميل':       {fr:'Échec de la préparation du moteur de téléchargement',
                                en:'Failed to set up the download engine'}
};

function TR(text) {
  if (!text) return text;
  if (MSGS[text]) return MSGS[text][LANG] || text;
  let m = text.match(/^لا توجد نسخ متاحة لـ (.+)$/);
  if (m) return LANG === 'fr' ? `Aucune version disponible pour ${m[1]}`
          : LANG === 'en' ? `No releases available for ${m[1]}` : text;
  m = text.match(/^🔄 إعادة محاولة (\d+) من (\d+) بعد (\d+) ثوانٍ\.\.\.$/);
  if (m) return LANG === 'fr' ? `🔄 Nouvelle tentative ${m[1]} sur ${m[2]} dans ${m[3]}s...`
          : LANG === 'en' ? `🔄 Retry ${m[1]} of ${m[2]} in ${m[3]}s...` : text;
  m = text.match(/^فشل التحميل بعد (\d+) محاولات \(رمز (\d+)\)$/);
  if (m) return LANG === 'fr' ? `Échec du téléchargement après ${m[1]} tentatives (code ${m[2]})`
          : LANG === 'en' ? `Download failed after ${m[1]} attempts (code ${m[2]})` : text;
  return text;
}

let LANG = localStorage.getItem('site_lang') || 'ar';

function applyLang(lang) {
  LANG = lang;
  localStorage.setItem('site_lang', lang);
  const dict = I18N[lang];
  document.documentElement.lang = lang;
  document.documentElement.dir = (lang === 'ar') ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach(el => { if (dict[el.dataset.i18n]) el.textContent = dict[el.dataset.i18n]; });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => { if (dict[el.dataset.i18nPh]) el.placeholder = dict[el.dataset.i18nPh]; });
  ['ar','fr','en'].forEach(l => {
    const b = document.getElementById('lang-' + l);
    if (b) b.classList.toggle('active', l === lang);
  });
}
function setLang(lang){ applyLang(lang); }

let releases = [], matched = null;

function showBanner(text, cls) {
  const b = document.getElementById('banner');
  b.textContent = text;
  b.className = 'banner ' + cls;
  b.style.display = 'block';
}
function hideBanner(){ document.getElementById('banner').style.display = 'none'; }

async function doSearch() {
  const q = document.getElementById('query').value.trim();
  if (!q) { showBanner(I18N[LANG].errEmpty, 'err'); return; }
  hideBanner();
  document.getElementById('sbtn').disabled = true;
  showBanner(I18N[LANG].searching, 'info');

  try {
    const resp = await fetch('/search', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({query:q})
    });
    const data = await resp.json();
    if (!resp.ok) { showBanner('❌ ' + TR(data.error || I18N[LANG].unknownErr), 'err'); return; }

    matched = {title:data.title, year:data.year, imdb_id:data.imdb_id};
    releases = data.releases;

    const tbody = document.querySelector('#tbl tbody');
    tbody.innerHTML = '';
    releases.forEach((r, i) => {
      const tr = document.createElement('tr');
      tr.innerHTML =
        `<td class="title">${r.title}</td>` +
        `<td>${r.size}</td>` +
        `<td>${r.seeders}</td>` +
        `<td>${r.indexer}</td>` +
        `<td><button class="rowbtn" onclick='startDownload(${i})'>${I18N[LANG].dlBtn}</button></td>`;
      tbody.appendChild(tr);
    });
    document.getElementById('tbl').style.display = 'table';
    if (data.fallback) {
      const msg = {
        ar:`⚠️ لا توجد نسخة مطابقة حرفياً لعبارة البحث — عرض ${releases.length} نسخة بديلة متاحة`,
        fr:`⚠️ Aucune correspondance exacte — affichage de ${releases.length} versions approximatives`,
        en:`⚠️ No exact title match — showing ${releases.length} approximate releases`
      };
      showBanner(msg[LANG], 'err');
    } else {
      const msg = {
        ar:`✅ تم العثور على ${releases.length} نسخة من: ${data.title} (${data.year})`,
        fr:`✅ ${releases.length} version(s) trouvée(s) pour : ${data.title} (${data.year})`,
        en:`✅ Found ${releases.length} release(s) for: ${data.title} (${data.year})`
      };
      showBanner(msg[LANG], 'ok');
    }
  } catch(e) {
    showBanner(I18N[LANG].connErr, 'err');
  } finally {
    document.getElementById('sbtn').disabled = false;
  }
}

async function startDownload(i) {
  try {
    hideBanner();
    const r = releases[i];
    const resp = await fetch('/download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        magnet: r.magnet,
        title: matched.title,
        year: matched.year,
        imdb_id: matched.imdb_id,
        dest: document.getElementById('dest').value.trim(),
        size: r.size
      })
    });
    const data = await resp.json();
    if (!resp.ok) { showBanner('❌ ' + TR(data.error || I18N[LANG].startFail), 'err'); return; }
    showBanner(I18N[LANG].dlInfo, 'info');
  } catch(e) {
    showBanner(I18N[LANG].srvDown, 'err');
  }
}

async function ctrl(action) {
  try { await fetch('/' + action, {method:'POST'}); } catch(e) {}
}

setInterval(async () => {
  try {
    const s = await fetch('/status').then(r => r.json());
    if (s.result) {
      const cancelled = (s.result.message || '').startsWith('تم إلغاء');
      const icon = s.result.success ? '✅ ' : (cancelled ? '🚫 ' : '❌ ');
      showBanner(icon + TR(s.result.message), s.result.success ? 'ok' : 'err');
    }
    const L = I18N[LANG];
    const pr = document.getElementById('prog');
    if (!s.busy) {
      pr.style.display = 'none';
    } else {
      pr.style.display = 'block';
      const p = s.progress || {};
      const has = p.total && p.total !== '0B' && (p.pct || 0) > 0;
      document.getElementById('pfill').style.width = has ? Math.min(p.pct, 100) + '%' : '4%';
      let txt;
      if (has) {
        txt =
          `<span>📦 <span class="pv">${p.done}</span> ${L.of} <span class="pv">${p.total}</span></span>` +
          `<span>📊 <span class="pv">${p.pct}%</span></span>` +
          `<span>${L.speedL} <span class="pv">${p.speed}/s</span></span>` +
          `<span>${L.etaL} <span class="pv">${(p.eta && p.eta !== 'N/A') ? p.eta : '--'}</span></span>`;
      } else {
        txt = `<span>${L.connecting}</span>`;
      }
      const el = Math.floor(s.elapsed || 0);
      const hh = String(Math.floor(el / 3600)).padStart(2, '0'),
            mm = String(Math.floor((el % 3600) / 60)).padStart(2, '0'),
            ss = String(el % 60).padStart(2, '0');
      txt += `<span>${L.elL} <span class="pv">${hh}:${mm}:${ss}</span></span>`;
      document.getElementById('ptext').innerHTML = txt;
      document.getElementById('pnote').textContent = TR(s.note || '');
    }
    const c = document.getElementById('ctrl');
    c.style.display = s.busy ? 'flex' : 'none';
    document.getElementById('pauseB').style.display = (!s.paused && s.busy) ? 'inline-block' : 'none';
    document.getElementById('resumeB').style.display = (s.paused) ? 'inline-block' : 'none';
  } catch(e) {}
}, 1000);

applyLang(LANG);

document.getElementById('dest').addEventListener('keydown', e => {
  if (e.key === 'Enter') doSearch();
});
</script>
</body>
</html>
"""

# ====================== صفحة المكتبة ======================

LIBRARY_PAGE = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>📚 مكتبة أفلامي</title>
<style>
  body { font-family: Tahoma, Arial, sans-serif; background:#12141c; color:#e6e6e6; margin:0; padding:24px; }
  h1 { color:#ffb74d; text-align:center; margin-top:0; }
  h1 a { font-size:13px; color:#4fc3f7; text-decoration:none; border:1px solid #4fc3f7;
         padding:4px 12px; border-radius:20px; margin-right:12px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:18px; }
  .card { background:#1a1d29; border:1px solid #262a38; border-radius:12px; overflow:hidden;
          display:flex; flex-direction:column; }
  .card img { width:100%; height:330px; object-fit:cover; background:#22263a; display:block; }
  .noimg { width:100%; height:330px; background:#22263a; display:flex; align-items:center;
           justify-content:center; font-size:52px; }
  .body { padding:12px 14px 16px; display:flex; flex-direction:column; gap:8px; flex:1; }
  .t { font-weight:bold; color:#fff; font-size:15px; }
  .y { color:#8ab4ff; font-size:12px; }
  .p { font-size:12.5px; line-height:1.7; color:#c9cdd6; direction:ltr; text-align:left;
       max-height:110px; overflow-y:auto; }
  .c { font-size:12px; color:#ffb74d; direction:ltr; text-align:left; }
  .f { margin-top:auto; background:#26323f; color:#9fd6ff; border:none; border-radius:6px;
       padding:7px; cursor:pointer; font-size:12.5px; }
  .empty { text-align:center; color:#777; margin-top:60px; font-size:17px; }
  .date { color:#667; font-size:11px; direction:ltr; text-align:left; }
  .langs { text-align:center; margin-bottom:10px; }
  .langs button { background:#1c2030; color:#c9cdd6; border:1px solid #333; padding:5px 14px;
                  font-size:12.5px; margin:0 3px; cursor:pointer; border-radius:6px; }
  .langs button.active { background:#ffb74d; color:#000; font-weight:bold; border-color:#ffb74d; }
</style>
</head>
<body>
<div class="langs">
  <button id="lang-ar" onclick="setLang('ar')">🇸🇦 عربية</button>
  <button id="lang-fr" onclick="setLang('fr')">🇫🇷 Français</button>
  <button id="lang-en" onclick="setLang('en')">🇬🇧 English</button>
</div>
<h1><span data-i18n="libTitle">📚 مكتبة أفلامي</span> <a href="/" data-i18n="backLink">🔍 رجوع للبحث</a></h1>
{% if not movies %}
<p class="empty" data-i18n="libEmpty">لا توجد أفلام محملة بعد — ابحث عن فيلم وحمّله وسيظهر هنا تلقائياً 🎬</p>
{% endif %}
<div class="grid">
{% for m in movies %}
  <div class="card">
    {% if m.poster %}<img src="{{ m.poster }}" alt="{{ m.title }}">
    {% else %}<div class="noimg">🎞️</div>{% endif %}
    <div class="body">
      <span class="t">{{ m.title }}</span>
      <span class="y">{{ m.year or '?' }} • <span data-i18n="added">أُضيف</span> {{ m.added }}</span>
      {% if m.plot %}<div class="p">{{ m.plot }}</div>{% endif %}
      {% if m.cast %}<div class="c">🎭 {{ m.cast|join(', ') }}</div>{% endif %}
      <button class="f" data-i18n="openBtn" onclick="openFolder('{{ m.folder|replace('\\\\','\\\\\\\\') }}')">📂 فتح المجلد</button>
    </div>
  </div>
{% endfor %}
</div>
<script>
const LIB_I18N = {
  ar: { libTitle:'📚 مكتبة أفلامي', backLink:'🔍 رجوع للبحث',
        libEmpty:'لا توجد أفلام محملة بعد — ابحث عن فيلم وحمّله وسيظهر هنا تلقائياً 🎬',
        added:'أُضيف', openBtn:'📂 فتح المجلد' },
  fr: { libTitle:'📚 Ma Bibliothèque', backLink:"🔍 Retour à la recherche",
        libEmpty:"Aucun film téléchargé pour l'instant — recherchez et téléchargez un film pour le voir ici 🎬",
        added:'Ajouté le', openBtn:'📂 Ouvrir le dossier' },
  en: { libTitle:'📚 My Movie Library', backLink:'🔍 Back to Search',
        libEmpty:'No movies downloaded yet — search & download a movie and it will appear here automatically 🎬',
        added:'Added', openBtn:'📂 Open Folder' }
};
let LANG = localStorage.getItem('site_lang') || 'ar';
function applyLang(lang) {
  LANG = lang;
  localStorage.setItem('site_lang', lang);
  const dict = LIB_I18N[lang];
  document.documentElement.lang = lang;
  document.documentElement.dir = (lang === 'ar') ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach(el => { if (dict[el.dataset.i18n]) el.textContent = dict[el.dataset.i18n]; });
  ['ar','fr','en'].forEach(l => {
    const b = document.getElementById('lang-' + l);
    if (b) b.classList.toggle('active', l === lang);
  });
}
function setLang(lang){ applyLang(lang); }

async function openFolder(f) {
  await fetch('/open_folder', {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify({folder:f})});
}
applyLang(LANG);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, dest=DEFAULT_MOVIES_DIR)


@app.route("/search", methods=["POST"])
def search():
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "اكتب اسم الفيلم"}), 400

    resolved = IMDbResolver().resolve_movie(query)
    if not resolved:
        return jsonify({"error": "لم يتم العثور على الفيلم على IMDb"}), 404

    imdb_id, title, year = resolved
    idx = CustomIndexer()
    clean_search_word = re.sub(r'\b(19\d{2}|20\d{2})\b|\(|\)', '', query).strip()
    releases = idx.search_all(imdb_id, title, year, clean_search_word)
    fallback = False

    if not releases:
        raw = idx.search_torrentio(imdb_id) + idx.search_piratebay(title, year) + idx.search_yts(imdb_id)
        seen, approx = set(), []
        for r in raw:
            if r["magnet"] in seen or not idx.is_valid_quality(r["title"]):
                continue
            seen.add(r["magnet"])
            approx.append(r)
        approx.sort(key=lambda x: x["seeders"], reverse=True)
        releases, fallback = approx[:20], True

    if not releases:
        return jsonify({"error": f"لا توجد نسخ متاحة لـ {title} ({year})"}), 404

    return jsonify({"title": title, "year": year, "imdb_id": imdb_id,
                    "releases": releases, "fallback": fallback})


@app.route("/download", methods=["POST"])
def download():
    data = request.json or {}
    magnet, title, year, dest, size = (
        data.get("magnet"), data.get("title"), data.get("year"),
        data.get("dest"), data.get("size", ""),
    )
    imdb_id = data.get("imdb_id", "")

    if not all([magnet, title, dest]):
        return jsonify({"error": "بيانات ناقصة"}), 400

    folder_name = f"{title} ({year})" if year else title
    folder = Path(dest) / folder_name

    with _lock:
        busy = _state["busy"]

    if busy:
        return jsonify({"error": "هناك تحميل قيد التنفيذ حالياً"}), 409

    if not check_disk_space(folder, size):
        return jsonify({"error": "❌ المساحة غير كافية على القرص الهدف (التفاصيل في نافذة الخادم)"}), 507

    with _lock:
        _state["busy"] = True
        _state["paused"] = False
        _state["result"] = None
        _state["logs"] = []
        _state["progress"] = {}
        _state["note"] = ""
        _state["started_at"] = time.time()
    _flags["pause"] = False
    _flags["cancel"] = False

    threading.Thread(target=run_download, args=(magnet, folder, title, imdb_id), daemon=True).start()
    return jsonify({"started": True})


@app.route("/pause", methods=["POST"])
def pause():
    if _state["busy"] and not _state["paused"]:
        _flags["pause"] = True
    return jsonify({"ok": True})


@app.route("/resume", methods=["POST"])
def resume():
    if _state["paused"]:
        _flags["pause"] = False
    return jsonify({"ok": True})


@app.route("/cancel", methods=["POST"])
def cancel():
    if _state["busy"]:
        _flags["pause"] = False
        _flags["cancel"] = True
    return jsonify({"ok": True})


@app.route("/status")
def status():
    with _lock:
        elapsed = int(time.time() - _state["started_at"]) if (_state["busy"] and _state["started_at"]) else 0
        return jsonify({
            "logs": take_logs(),
            "busy": _state["busy"],
            "paused": _state["paused"],
            "result": _state["result"],
            "progress": _state["progress"],
            "note": _state["note"],
            "elapsed": elapsed,
        })


@app.route("/library")
def library():
    movies = load_library()
    for m in movies:
        if not m.get("poster"):
            fetch_movie_details(m)
    if movies:
        save_library(movies)
    return render_template_string(LIBRARY_PAGE, movies=movies)


@app.route("/open_folder", methods=["POST"])
def open_folder():
    folder = (request.json or {}).get("folder", "")
    p = Path(folder)
    if p.is_dir() and os.name == "nt":
        os.startfile(str(p))
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
