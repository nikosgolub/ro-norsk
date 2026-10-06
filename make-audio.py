#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Озвучка курса Ro! нейросетевым голосом Azure.

Читает app.html, достаёт все норвежские фразы и слова, прогоняет их через
синтез речи Azure и складывает mp3 рядом с приложением — в audio/p/.
Приложение само подхватит файлы: есть файл — играет его, нет — читает
встроенным голосом телефона, как раньше.

Ничего ставить не надо, нужен только python3.

  # посмотреть, какие норвежские голоса доступны
  python3 make-audio.py --key КЛЮЧ --region westeurope --voices

  # пробный прогон: первые 10 фраз
  python3 make-audio.py --key КЛЮЧ --region westeurope --limit 10

  # весь курс (можно прерывать и запускать снова — готовое пропускается)
  python3 make-audio.py --key КЛЮЧ --region westeurope

  # одна произвольная фраза, просто послушать
  python3 make-audio.py --key КЛЮЧ --region westeurope --text "God morgen!"
"""

import argparse, json, os, re, sys, time, urllib.request, urllib.error

# ─── имена файлов ──────────────────────────────────────────────────
# Тот же хэш считает приложение, поэтому список файлов не надо
# прописывать в коде: оно выводит имя из самой фразы.

def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def fnv(s, seed):
    h = seed
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h

def snd_hash(text):
    t = norm(text)
    return "%08x%08x" % (fnv(t, 0x811C9DC5), fnv(t, 0x7F4A7C15))

# ─── разбор app.html ───────────────────────────────────────────────

def _span(src, decl, open_ch="["):
    start = src.find(decl)
    if start < 0:
        raise SystemExit("не нашёл в файле: " + decl)
    i = start + len(decl)
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    j = i
    while True:
        c = src[j]
        if c == '"':
            j += 1
            while src[j] != '"':
                if src[j] == "\\":
                    j += 1
                j += 1
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                break
        j += 1
    return json.loads(src[i:j + 1])

def find_app(path):
    """Файл приложения может называться app.html или index.html — ищем оба."""
    if os.path.exists(path):
        return path
    here = os.path.dirname(os.path.abspath(path)) or "."
    for name in ("index.html", "app.html", "norsk-v2-app.html"):
        guess = os.path.join(here, name)
        if os.path.exists(guess):
            print("Файл приложения: %s" % name)
            return guess
    raise SystemExit("Не нашёл файл приложения. Положи скрипт рядом с index.html "
                     "или укажи путь: --app ПУТЬ_К_ФАЙЛУ")

def collect(app_path, with_words=True):
    src = open(app_path, encoding="utf-8").read()
    lessons = _span(src, "let LESSONS = ")
    words = _span(src, "let WORDS = ")
    out, seen = [], set()

    def add(text, where):
        t = norm(text)
        if not t or t in seen:
            return
        seen.add(t)
        out.append((t, where))

    for li, lesson in enumerate(lessons):
        for pi, part in enumerate(lesson["parts"]):
            for q in part["q"]:
                add(q["no"], "урок %d, блок %d" % (li + 1, pi + 1))
    if with_words:
        for li, lw in enumerate(words):
            for w in lw:
                add(w["n"], "слова урока %d" % (li + 1))
    return out

# ─── Azure ─────────────────────────────────────────────────────────

def endpoint(region):
    return "https://%s.tts.speech.microsoft.com/cognitiveservices/v1" % region

def say_error(code, body):
    if code == 401 or code == 403:
        return ("Ключ или регион не приняты. Проверь, что ключ скопирован целиком\n"
                "и что регион совпадает с тем, что написан на странице ресурса в Azure\n"
                "(например westeurope, norwayeast).")
    if code == 400:
        return ("Запрос отклонён — чаще всего из-за имени голоса.\n"
                "Запусти с --voices и возьми имя из списка.")
    if code == 429:
        return "Слишком часто. Скрипт подождёт и попробует снова."
    return "Ответ сервиса: %s %s" % (code, (body or "")[:200])

def list_voices(key, region):
    url = "https://%s.tts.speech.microsoft.com/cognitiveservices/voices/list" % region
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(say_error(e.code, e.read().decode("utf-8", "replace")))
    except Exception as e:
        raise SystemExit("Не получилось связаться с Azure: %s" % e)
    no = [v for v in data if (v.get("Locale") or "").lower().startswith(("nb-", "nn-", "no-"))]
    if not no:
        raise SystemExit("В этом регионе норвежских голосов нет. Попробуй другой регион.")
    print("Норвежские голоса в регионе %s:\n" % region)
    for v in sorted(no, key=lambda x: x.get("ShortName", "")):
        print("  %-28s %-8s %s" % (v.get("ShortName"), v.get("Gender"), v.get("LocalName") or ""))
    print("\nПередай нужное имя через --voice.")

def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))

def spoken(text):
    # «min / mi / mitt» и «stor – større – størst» читаются ровнее через запятую
    return re.sub(r"\s*[/–—]\s*", ", ", text)

def synth(key, region, voice, rate, text, tries=4):
    locale = "-".join(voice.split("-")[:2]) if voice.count("-") >= 2 else "nb-NO"
    ssml = ("<speak version='1.0' xml:lang='%s'><voice xml:lang='%s' name='%s'>"
            "<prosody rate='%s%%'>%s</prosody></voice></speak>"
            % (locale, locale, voice, rate, esc(spoken(text))))
    req = urllib.request.Request(
        endpoint(region), data=ssml.encode("utf-8"),
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                 "User-Agent": "ro-audio"})
    wait = 2
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(wait)
                wait *= 2
                continue
            raise SystemExit("\n" + say_error(e.code, body))
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(wait)
                wait *= 2
                continue
            raise SystemExit("\nНе получилось связаться с Azure: %s" % e)

# ─── опись ─────────────────────────────────────────────────────────

def write_index(out_dir, voice):
    ids = sorted(f[:-4] for f in os.listdir(out_dir)
                 if f.endswith(".mp3") and len(f) == 20)
    path = os.path.join(out_dir, "index.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "ext": "mp3", "voice": voice,
                   "made": time.strftime("%Y-%m-%d"), "ids": ids}, f,
                  ensure_ascii=False, separators=(",", ":"))
    return len(ids), path

# ─── запуск ────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--key", default=os.environ.get("AZURE_TTS_KEY", ""),
                    help="ключ Azure Speech (или переменная окружения AZURE_TTS_KEY)")
    ap.add_argument("--region", default="westeurope", help="регион ресурса, например westeurope")
    ap.add_argument("--voice", default="nb-NO-PernilleNeural", help="имя голоса, см. --voices")
    ap.add_argument("--rate", default="-5", help="скорость речи в процентах, по умолчанию -5")
    ap.add_argument("--app", default="app.html", help="путь к app.html")
    ap.add_argument("--out", default="", help="куда класть файлы, по умолчанию audio/p рядом с app.html")
    ap.add_argument("--limit", type=int, default=0, help="озвучить только первые N — для пробы")
    ap.add_argument("--no-words", action="store_true", help="без слов из карточек, только фразы")
    ap.add_argument("--voices", action="store_true", help="показать доступные норвежские голоса")
    ap.add_argument("--text", default="", help="озвучить одну произвольную фразу")
    ap.add_argument("--redo", action="store_true", help="перезаписать уже готовые файлы")
    a = ap.parse_args()

    if not a.key:
        raise SystemExit("Нужен ключ: --key КЛЮЧ (или положи его в AZURE_TTS_KEY).")

    if a.voices:
        list_voices(a.key, a.region)
        return

    a.app = find_app(a.app)
    out_dir = a.out or os.path.join(os.path.dirname(os.path.abspath(a.app)), "audio", "p")
    os.makedirs(out_dir, exist_ok=True)

    if a.text:
        data = synth(a.key, a.region, a.voice, a.rate, a.text)
        path = os.path.join(out_dir, snd_hash(a.text) + ".mp3")
        open(path, "wb").write(data)
        print("Готово: %s (%.1f КБ)" % (path, len(data) / 1024))
        n, ipath = write_index(out_dir, a.voice)
        print("Опись обновлена: %d файлов" % n)
        return

    items = collect(a.app, with_words=not a.no_words)
    if a.limit:
        items = items[:a.limit]

    todo = []
    for text, where in items:
        path = os.path.join(out_dir, snd_hash(text) + ".mp3")
        if a.redo or not os.path.exists(path):
            todo.append((text, where, path))

    print("Всего строк: %d · уже готово: %d · озвучить: %d"
          % (len(items), len(items) - len(todo), len(todo)))
    if not todo:
        n, ipath = write_index(out_dir, a.voice)
        print("Опись обновлена: %d файлов → %s" % (n, ipath))
        return
    print("Голос: %s · регион: %s · скорость: %s%%\n" % (a.voice, a.region, a.rate))

    started, total_bytes = time.time(), 0
    for i, (text, where, path) in enumerate(todo, 1):
        data = synth(a.key, a.region, a.voice, a.rate, text)
        open(path, "wb").write(data)
        total_bytes += len(data)
        left = (time.time() - started) / i * (len(todo) - i)
        sys.stdout.write("\r[%d/%d] %-42.42s  осталось ~%d мин   "
                         % (i, len(todo), text, left / 60 + 0.5))
        sys.stdout.flush()
        time.sleep(0.15)                      # не частим, чтобы не упереться в лимит
    print()

    n, ipath = write_index(out_dir, a.voice)
    print("\nГотово. Записано %d файлов, %.1f МБ." % (len(todo), total_bytes / 1048576))
    print("Всего в папке: %d файлов · опись: %s" % (n, ipath))
    print("\nТеперь залей папку audio/p рядом с app.html — приложение подхватит само.")

if __name__ == "__main__":
    main()
