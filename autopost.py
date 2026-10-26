"""Автопостинг каруселей из t.me/adresakzn в Instagram @keyby_kzn.

Команды:
  python autopost.py prepare            — выбрать объявление, собрать слайды в posts/<id>/
  python autopost.py publish <id>       — опубликовать карусель posts/<id>/ в Instagram
  python autopost.py refresh            — продлить токен Instagram (печатает новый токен)
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
POSTS = ROOT / "posts"
STATE = ROOT / "state" / "posted.json"
FONTS = ROOT / "fonts"

CHANNEL = os.environ.get("TG_CHANNEL", "adresakzn")
REPO = os.environ.get("GITHUB_REPOSITORY", "mkrvu/keyby_kzn-autopost")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
GRAPH = "https://graph.instagram.com/v23.0"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

W, H = 1080, 1350
ACCENT = (116, 184, 81)  # зелёный кейби
WHITE = (255, 255, 255)


# ---------------------------------------------------------------- Telegram

def fetch_page(before=None):
    url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_messages(soup):
    out = []
    for m in soup.select(".tgme_widget_message"):
        post = m.get("data-post", "")
        if "/" not in post:
            continue
        mid = int(post.split("/")[1])
        photos = []
        for a in m.select("a.tgme_widget_message_photo_wrap"):
            mm = re.search(r"background-image:url\('([^']+)'\)", a.get("style", ""))
            if mm:
                photos.append(mm.group(1))
        t = m.select_one(".tgme_widget_message_text")
        text = t.get_text("\n") if t else ""
        link = ""
        for b in m.select("a.tgme_widget_message_inline_button, a"):
            href = b.get("href", "")
            if "keyby.ru/apartments/" in href:
                link = href.split("?")[0]
                break
        out.append({"id": mid, "photos": photos, "text": text, "link": link})
    return out


def collect_listings(pages=3):
    msgs, before = [], None
    for _ in range(pages):
        soup = fetch_page(before)
        page = parse_messages(soup)
        if not page:
            break
        msgs = page + msgs
        before = min(m["id"] for m in page)
    msgs.sort(key=lambda m: m["id"])
    by_id = {m["id"]: m for m in msgs}
    listings = []
    for i, m in enumerate(msgs):
        if len(m["photos"]) < 5:
            continue
        # текст объявления идёт отдельным сообщением сразу после альбома
        txt = m if "Номер объекта" in m["text"] else by_id.get(m["id"] + len(m["photos"]))
        if not txt or "Номер объекта" not in txt["text"]:
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            txt = nxt if nxt and "Номер объекта" in nxt["text"] else None
        if not txt:
            continue
        info = parse_text(txt["text"])
        if not info:
            continue
        info.update(photos=m["photos"][:10], link=txt["link"], tg=f"https://t.me/{CHANNEL}/{m['id']}")
        listings.append(info)
    return listings


TYPES = [
    (r"студия", "Студия", "СТУДИЯ", "СТУДИЯ"),
    (r"евро-?2\s*к", "Евро-2к", "ЕВРО-ДВУШКА", "ЕВРО-2К КВАРТИРА"),
    (r"евро-?3\s*к", "Евро-3к", "ЕВРО-ТРЁШКА", "ЕВРО-3К КВАРТИРА"),
    (r"евро-?4\s*к", "Евро-4к", "ЕВРО-4К", "ЕВРО-4К КВАРТИРА"),
    (r"1\s*к", "1к", "ОДНУШКА", "1-К КВАРТИРА"),
    (r"2\s*к", "2к", "ДВУШКА", "2-К КВАРТИРА"),
    (r"3\s*к", "3к", "ТРЁШКА", "3-К КВАРТИРА"),
    (r"4\s*к", "4к", "4-КОМНАТНАЯ", "4-К КВАРТИРА"),
    (r"комната", "Комната", "КОМНАТА", "КОМНАТА"),
]


def money(s):
    d = re.sub(r"\D", "", s or "")
    return f"{int(d):,}".replace(",", ".") if d else ""


def parse_text(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None
    head = lines[0]
    t = None
    for rx, short, cover, title in TYPES:
        mm = re.match(rf"^({rx})\s*(.*)$", head, re.I)
        if mm:
            t = (short, cover, title, mm.group(mm.lastindex).strip())
            break
    if not t:
        return None
    info = {"type": t[0], "cover": t[1], "title": t[2], "name": t[3] or ""}
    body = "\n".join(lines)
    pm = re.search(r"^([\d\s.]{4,})\s*(\+\s*ку)?", "\n".join(lines[1:4]), re.I | re.M)
    if not pm:
        return None
    info["price"] = money(pm.group(1))
    info["ku"] = bool(pm.group(2))
    for key, rx in [("deposit", r"Залог[:\s]*([^\n]+)"), ("commission", r"Комиссия[:\s]*([^\n]+)"),
                    ("floor", r"Этаж[:\s]*([^\n]+)"), ("area", r"Площадь[:\s]*([\d.,]+)"),
                    ("object", r"Номер объекта[:\s]*(\d+)")]:
        mm = re.search(rx, body, re.I)
        info[key] = mm.group(1).strip() if mm else ""
    if not info["object"]:
        return None
    if re.search(r"без\s+залога", body, re.I):
        info["deposit"] = "без залога"
    elif info["deposit"]:
        info["deposit"] = money(info["deposit"]) + "₽" if re.search(r"\d", info["deposit"]) else info["deposit"]
    info["pets"] = "можно" if re.search(r"можно с животн", body, re.I) else ("нельзя" if re.search(r"без животн|нельзя с животн", body, re.I) else "")
    addr = lines[1] if len(lines) > 1 and not re.match(r"^[\d\s.]{4,}", lines[1]) else ""
    info["address"] = addr if addr.lower() != info["name"].lower() else ""
    # «1к Правды» + «ул. Правды» — название это просто улица
    if info["address"] and info["name"] and info["name"].lower() in info["address"].lower():
        info["name"] = info["address"]
        info["address"] = ""
    district = ""
    for tag in re.findall(r"#([^\s@#]+)", body):
        if not re.match(r"^(евро)?\d*к$|^студия$|^комната$", tag, re.I):
            district = re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", "-", tag)
            break
    info["district"] = district
    return info


# ---------------------------------------------------------------- рендер
# Шаблон: фото на весь слайд, затемнение; сверху «[ N ]» и характеристики,
# крупно цена/заголовок зелёным, теги-плашки снизу, логотип кейби.рф на каждом слайде.

TEXTS = ROOT / "texts"
LOGO_PNG = ROOT / "assets" / "logo.png"
DARK = (20, 20, 20)
PAD = 64


def font(name, size):
    return ImageFont.truetype(str(FONTS / f"{name}.ttf"), size)


def load_img(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def cover_fit(im, w, h):
    s = max(w / im.width, h / im.height)
    nw, nh = max(w, round(im.width * s)), max(h, round(im.height * s))
    r = im.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return r.crop((l, t, l + w, t + h))


def shade(img, top=0.55, mid=0.25, bottom=0.8):
    """Затемнение: сверху и снизу сильнее, чтобы читался текст."""
    mask = Image.new("L", (1, H))
    for y in range(H):
        t = y / H
        a = top + (mid - top) * (t / 0.35) if t < 0.35 else (mid if t < 0.5 else mid + (bottom - mid) * ((t - 0.5) / 0.5))
        mask.putpixel((0, y), int(255 * a))
    return Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, mask.resize((W, H)))


def base(photo, **kw):
    return shade(cover_fit(photo, W, H), **kw)


def wrap(draw, text, f, max_w):
    words, lines, cur = text.split(" "), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if draw.textlength(t, font=f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def text_block(draw, text, x, y, max_w, name, size, fill, max_lines=3, lh=1.05, min_size=40):
    """Рисует перенесённый текст от y (верх), уменьшая кегль, если не влезает. Возвращает нижнюю границу."""
    while True:
        f = font(name, size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) <= max_lines and all(draw.textlength(l, font=f) <= max_w for l in lines) or size <= min_size:
            break
        size -= 4
    for i, l in enumerate(lines):
        draw.text((x, y + i * size * lh), l, font=f, fill=fill, anchor="lt")
    return y + len(lines) * size * lh


def tag_row(draw, tags, y_bottom, accent_idx):
    """Плашки снизу, переносятся на новую строку; рисуются снизу вверх."""
    f = font("Manrope-Bold", 34)
    h, gap = 72, 14
    rows, cur, cw = [], [], 0
    for i, t in enumerate(tags):
        w = draw.textlength(t, font=f) + 56
        if cur and cw + gap + w > W - 2 * PAD:
            rows.append(cur); cur, cw = [], 0
        cur.append((i, t, w)); cw += (gap if cw else 0) + w
    if cur:
        rows.append(cur)
    y = y_bottom - len(rows) * (h + gap) + gap
    top = y
    for row in rows:
        x = PAD
        for i, t, w in row:
            acc = i == accent_idx
            draw.rounded_rectangle((x, y, x + w, y + h), radius=h / 2, fill=ACCENT if acc else WHITE)
            draw.text((x + w / 2, y + h / 2 + 1), t, font=f, fill=WHITE if acc else DARK, anchor="mm")
            x += w + gap
        y += h + gap
    return top


_logo_cache = {}


def logo(img, cx, cy, width=210):
    if width not in _logo_cache:
        lg = Image.open(LOGO_PNG).convert("RGBA")
        _logo_cache[width] = lg.resize((width, round(lg.height * width / lg.width)), Image.LANCZOS)
    lg = _logo_cache[width]
    img.paste(lg, (int(cx - lg.width / 2), int(cy - lg.height / 2)), lg)


def header(draw, num, right):
    f = font("Manrope-ExtraBold", 30)
    draw.text((PAD, 70), f"[ {num} ]", font=f, fill=WHITE, anchor="lm")
    draw.text((W - PAD, 70), right.upper(), font=f, fill=WHITE, anchor="rm")


def specs(info):
    parts = [info["type"]]
    if info["area"]:
        parts.append(f"{info['area']} м²")
    if info["floor"]:
        parts.append(f"{info['floor']} этаж")
    return " · ".join(parts)


def price_big(info):
    return info["price"].replace(".", " ") + " ₽"


def headline(info):
    """Комнатность и адрес: «Евро-2к, Чистопольская 88»."""
    addr = re.sub(r"^(ул\.?|улица)\s*", "", info["address"], flags=re.I).replace(", ", " ") if info["address"] else ""
    if not addr:
        addr = re.sub(r"^(ул\.?|улица)\s*", "", info["name"], flags=re.I)
    addr = re.sub(r" (\S+)$", "\u00a0\\1", addr)  # номер дома не отрываем от улицы
    return f"{info['type']}, {addr}" if addr else info["type"]


def slide_cover(info, photo, n_total):
    img = base(photo, top=0.7, mid=0.2, bottom=0.85)
    d = ImageDraw.Draw(img)
    header(d, 1, specs(info))
    # цена
    pf = font("Unbounded-Black", 150)
    while d.textlength(price_big(info), font=pf) > W - 2 * PAD:
        pf = font("Unbounded-Black", pf.size - 6)
    d.text((PAD, 130), price_big(info), font=pf, fill=ACCENT, anchor="lt")
    sub = "в месяц" + (" + КУ" if info["ku"] else "")
    d.text((PAD + 4, 130 + pf.size * 1.12), sub, font=font("Manrope-ExtraBold", 40), fill=WHITE, anchor="lt")
    # теги и заголовок снизу
    tags = ["Проверенная квартира"]
    if info["pets"] == "можно":
        tags.append("можно с животными")
    if info["deposit"] == "без залога":
        tags.append("без залога")
    elif info["deposit"]:
        tags.append(f"залог {info['deposit'].replace('.', ' ').replace('₽', ' ₽')}")
    top = tag_row(d, tags, 1170, accent_idx=0)
    # заголовок над тегами
    f_size = 76
    hl = headline(info)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    while True:
        lines = wrap(probe, hl, font("Manrope-ExtraBold", f_size), W - 2 * PAD)
        if len(lines) <= 3 or f_size <= 48:
            break
        f_size -= 4
    y0 = top - 34 - len(lines) * f_size * 1.05
    text_block(d, hl, PAD, y0, W - 2 * PAD, "Manrope-ExtraBold", f_size, WHITE, max_lines=3)
    # низ
    f = font("Manrope-ExtraBold", 30)
    d.text((PAD, H - 70), (info["district"] or "Казань").upper(), font=f, fill=WHITE, anchor="lm")
    d.text((W - PAD, H - 70), "ЛИСТАЙ >>>", font=f, fill=WHITE, anchor="rm")
    logo(img, W / 2, H - 70, 190)
    return img


def slide_photo(num, photo, room, title, tags):
    img = base(photo, top=0.78, mid=0.12, bottom=0.65)
    d = ImageDraw.Draw(img)
    header(d, num, room or "")
    if title:
        text_block(d, title.lower(), PAD, 120, W - 2 * PAD, "Unbounded-Black", 84, ACCENT, max_lines=3, lh=1.08, min_size=56)
    if tags:
        tag_row(d, tags, H - 150, accent_idx=len(tags) - 1)
    logo(img, W / 2, H - 70, 190)
    return img


def slide_terms(num, info, photo, title="готова к заселению"):
    img = base(photo, top=0.75, mid=0.7, bottom=0.8)
    d = ImageDraw.Draw(img)
    header(d, num, "Условия")
    y = text_block(d, title, PAD, 120, W - 2 * PAD, "Unbounded-Black", 84, ACCENT, max_lines=2, lh=1.08)
    rows = [("Аренда", f"{info['price'].replace('.', ' ')} ₽/мес" + (" + КУ" if info["ku"] else "")),
            ("Квартира", specs(info).lower()),
            ("Адрес", ", ".join(x for x in [info["name"], info["address"]] if x)),
            ("Район", info["district"]),
            ("Залог", info["deposit"].replace(".", " ").replace("₽", " ₽")),
            ("Комиссия", info["commission"]),
            ("Животные", info["pets"])]
    rows = [r for r in rows if r[1]]
    y += 40
    fl, fv = font("Manrope-Bold", 36), font("Manrope-ExtraBold", 36)
    for a, b in rows:
        d.text((PAD, y + 40), a, font=fl, fill=WHITE, anchor="ls")
        val = b
        while d.textlength(val, font=fv) > W - 2 * PAD - 260 and len(val) > 8:
            val = val[:-2].rstrip(" ,") + "…"
        d.text((W - PAD, y + 40), val, font=fv, fill=WHITE, anchor="rs")
        y += 64
        d.line((PAD, y, W - PAD, y), fill=(255, 255, 255, 90), width=1)
    # кнопка
    by = H - 300
    d.rounded_rectangle((PAD, by, W - PAD, by + 112), radius=56, fill=ACCENT)
    d.text((PAD + 50, by + 56), "Контакт на сайте", font=font("Manrope-ExtraBold", 46), fill=WHITE, anchor="lm")
    d.text((W - PAD - 50, by + 56), ">>>", font=font("Manrope-ExtraBold", 46), fill=WHITE, anchor="rm")
    d.text((W / 2, by + 160), f"НОМЕР ОБЪЕКТА {info['object']} · КЕЙБИ.РФ", font=font("Manrope-ExtraBold", 30), fill=WHITE, anchor="mm")
    logo(img, W / 2, H - 70, 190)
    return img


AI_PROMPT = """Ты делаешь карусель для Instagram сервиса аренды квартир кейби.рф.
Объявление: {listing}
Тебе даны фото квартиры, пронумерованные с 0. Верни ТОЛЬКО JSON без пояснений:
{{"cover": <номер самого привлекательного светлого общего кадра>,
 "slides": [{{"photo": <номер>, "room": "<комната: Кухня-гостиная, Кухня, Гостиная, Спальня, Санузел, Прихожая, Балкон, Детская, Кабинет>",
   "title": "<короткий цепляющий заголовок о том, что видно на фото, 2-5 слов, строчными, без точки>",
   "tags": ["<2-3 предмета мебели или техники, которые ТОЧНО видны на этом фото, 1-3 слова каждый>"]}}],
 "terms_photo": <номер фото для фона слайда условий, не повторять cover>}}
Правила: в slides от 4 до 7 фото, без cover и без почти одинаковых кадров; порядок — как экскурсия
(общая комната/кухня → спальня → санузел → прочее); не придумывай то, чего нет на фото;
не пиши про цену и адрес; заголовки разные; без эмодзи."""


def ai_plan(info):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    listing = {k: info[k] for k in ("type", "name", "address", "area", "floor", "pets")}
    content = []
    for i, u in enumerate(info["photos"]):
        content += [{"type": "text", "text": f"Фото {i}:"}, {"type": "image", "source": {"type": "url", "url": u}}]
    content.append({"type": "text", "text": AI_PROMPT.format(listing=json.dumps(listing, ensure_ascii=False))})
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=180,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 1500,
                            "messages": [{"role": "user", "content": content}]})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    plan = json.loads(text[text.index("{"): text.rindex("}") + 1])
    n = len(info["photos"])
    plan["slides"] = [s for s in plan["slides"] if 0 <= s["photo"] < n and s["photo"] != plan["cover"]][:8]
    if not (0 <= plan.get("terms_photo", -1) < n):
        plan["terms_photo"] = n - 1
    return plan


def plan_slides(info):
    """Какие фото на каких слайдах: texts/<объект>.json → Claude → по порядку без подписей."""
    tf = TEXTS / f"{info['object']}.json"
    if tf.exists():
        return json.loads(tf.read_text(encoding="utf-8"))
    n = len(info["photos"])
    try:
        plan = ai_plan(info)
    except Exception as e:  # без ИИ пост всё равно выйдет
        print("ai_plan failed:", e)
        plan = None
    if plan:
        TEXTS.mkdir(exist_ok=True)
        tf.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        return plan
    return {"cover": 0, "slides": [{"photo": i} for i in range(1, min(n - 1, 8))], "terms_photo": n - 1}


def render(info, outdir):
    plan = plan_slides(info)
    need = {plan["cover"], plan["terms_photo"], *[s["photo"] for s in plan["slides"]]}
    ims = {i: load_img(info["photos"][i]) for i in need if i < len(info["photos"])}
    slides = [slide_cover(info, ims[plan["cover"]], 0)]
    for s in plan["slides"]:
        if s["photo"] in ims:
            slides.append(slide_photo(len(slides) + 1, ims[s["photo"]], s.get("room", ""), s.get("title", ""), s.get("tags", [])))
    slides.append(slide_terms(len(slides) + 1, info, ims[plan["terms_photo"]], plan.get("terms_title", "готова к заселению")))
    slides = slides[:10]
    outdir.mkdir(parents=True, exist_ok=True)
    for f in outdir.glob("*.jpg"):
        f.unlink()
    for i, sl in enumerate(slides, 1):
        sl.save(outdir / f"{i}.jpg", "JPEG", quality=90, optimize=True)
    (outdir / "caption.txt").write_text(caption(info), encoding="utf-8")
    (outdir / "meta.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(slides)


def caption(info):
    head = f"{info['type']}" + (f", {info['name']}" if info["name"] else "")
    lines = [head, ""]
    place = ", ".join(x for x in [info["address"], info["district"]] if x)
    if place:
        lines.append(f"📍 {place}")
    lines.append(f"💰 {info['price']} ₽/мес" + (" + КУ" if info["ku"] else ""))
    if info["deposit"]:
        lines.append(f"🔐 Залог: {info['deposit']}")
    if info["commission"]:
        lines.append(f"🤝 Комиссия: {info['commission']}")
    extra = " · ".join(x for x in [f"{info['area']} м²" if info["area"] else "", f"этаж {info['floor']}" if info["floor"] else ""] if x)
    if extra:
        lines.append(f"🏢 {extra}")
    if info["pets"] == "можно":
        lines.append("🐾 Можно с животными")
    lines += ["", f"Номер объекта: {info['object']}",
              "Больше проверенных квартир — на кейби.рф, сервисе безопасной долгосрочной аренды в Казани.", "",
              "#аренда #арендаказань #квартиравказани #снятьквартиру #казань #кейби"]
    return "\n".join(lines)


# ---------------------------------------------------------------- состояние

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"posted": []}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- Instagram

def ig(method, path, **params):
    params["access_token"] = os.environ["IG_TOKEN"]
    r = requests.request(method, f"{GRAPH}/{path}", params=params, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"{path}: {r.status_code} {r.text}")
    return r.json()


def publish(obj):
    d = POSTS / obj
    files = sorted(d.glob("*.jpg"), key=lambda p: int(p.stem))
    urls = [f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/posts/{obj}/{f.name}" for f in files]
    cap = (d / "caption.txt").read_text(encoding="utf-8")
    # ждём, пока картинки станут доступны по публичной ссылке
    for u in urls:
        for _ in range(20):
            if requests.head(u, timeout=20).status_code == 200:
                break
            time.sleep(6)
    children = [ig("POST", "me/media", image_url=u, is_carousel_item="true")["id"] for u in urls]
    cont = ig("POST", "me/media", media_type="CAROUSEL", children=",".join(children), caption=cap)["id"]
    for _ in range(30):
        st = ig("GET", cont, fields="status_code").get("status_code")
        if st == "FINISHED":
            break
        if st == "ERROR":
            raise RuntimeError("контейнер карусели в статусе ERROR")
        time.sleep(5)
    res = ig("POST", "me/media_publish", creation_id=cont)
    st = load_state()
    st["posted"].append({"object": obj, "media_id": res.get("id"), "ts": int(time.time())})
    save_state(st)
    print("published", obj, res)


def refresh():
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": os.environ["IG_TOKEN"]}, timeout=30)
    r.raise_for_status()
    print(r.json()["access_token"])


# ---------------------------------------------------------------- main

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if cmd == "prepare":
        done = {p["object"] for p in load_state()["posted"]}
        want = os.environ.get("OBJECT", "").strip()
        listings = collect_listings(pages=8 if want else 3)
        listings = [l for l in listings if l["object"] == want] if want else [l for l in listings if l["object"] not in done]
        if not listings:
            print("нет новых объявлений"); return
        info = listings[-1]  # самое свежее
        n = render(info, POSTS / info["object"])
        print(f"prepared {info['object']} ({n} slides)")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"object={info['object']}\n")
    elif cmd == "publish":
        publish(sys.argv[2])
    elif cmd == "refresh":
        refresh()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
